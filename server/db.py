"""The store. One table. Nothing is ever deleted -- only unloaded."""

import hashlib
import json
import re
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import home

DB_PATH = home.store_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dt         TEXT    NOT NULL,          -- ISO8601 UTC
    kind       TEXT    NOT NULL,          -- user | the assistant's own kind | essence
    text       TEXT    NOT NULL,
    tokens_est INTEGER NOT NULL DEFAULT 0,
    loaded     INTEGER NOT NULL DEFAULT 1,-- 1 = in the working set
    replaces   TEXT,                      -- JSON array of row ids an essence stands for
    meta       TEXT,                      -- JSON, free-form
    title      TEXT,                      -- one short line, its own, on essences only
    by_model   TEXT                       -- which mind wrote it: "service/model"
);
CREATE INDEX IF NOT EXISTS idx_rows_loaded ON rows(loaded);
-- `by_model` is indexed in _migrate, not here. This script runs against
-- stores that predate the column, where CREATE TABLE IF NOT EXISTS is a
-- no-op and an index on a column that is not there yet is a hard error --
-- which is exactly how it announced itself the first time.

-- What the assistant did to its own memory, in the order it did it.
-- Deliberately not a row: these are for the owner to read, and the assistant
-- already knows what it chose.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    dt       TEXT    NOT NULL,
    reply_row INTEGER,                      -- the reply this belonged to
    kind     TEXT    NOT NULL,
    summary  TEXT    NOT NULL,
    detail   TEXT                          -- JSON, shown when expanded
);
CREATE INDEX IF NOT EXISTS idx_events_row ON events(reply_row);

-- A reading of an essence, never the essence itself. The text in `rows` is the
-- only original; every row in here can be thrown away and made again from it,
-- which is the whole point -- a change of model is a rebuild, not a migration.
-- Keyed by model as well as row, so a second model adds a second reading
-- beside the first rather than standing on top of it.
CREATE TABLE IF NOT EXISTS vectors (
    row_id    INTEGER NOT NULL,
    model     TEXT    NOT NULL,
    dim       INTEGER NOT NULL,
    vec       BLOB    NOT NULL,           -- dim little-endian float32s
    text_hash TEXT    NOT NULL,           -- sha256 of exactly what went in
    n_tokens  INTEGER,                    -- the model's own count, not our guess
    truncated INTEGER NOT NULL DEFAULT 0, -- 1 = the model could not take it whole
    dt        TEXT    NOT NULL,
    PRIMARY KEY (row_id, model)
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model);

-- One night's dream: the whole account of what it did to its shelf while
-- nobody was talking to it. Its own table on purpose -- it can grow as fat
-- as it likes and never enters its working set. What it wakes to is one
-- line, kept as a `dream` row; this is the rest of it, for the day it wants
-- to audit itself. Nothing here is ever deleted either.
CREATE TABLE IF NOT EXISTS dreams (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    night     TEXT    NOT NULL,          -- the local date the night belongs to
    started   TEXT    NOT NULL,          -- ISO8601 UTC
    ended     TEXT,
    why       TEXT    NOT NULL,          -- dead_hours | fallback | backlog | asked | missed
    free      INTEGER NOT NULL DEFAULT 0,-- 1 = a night with no job at all
    status    TEXT    NOT NULL,          -- running | done | broke | missed
    model     TEXT,
    rounds    INTEGER NOT NULL DEFAULT 0,-- calls made, reads included
    day_from  INTEGER,                   -- first row of the stretch it read
    day_to    INTEGER,                   -- last row of it; the next night starts after
    left      INTEGER NOT NULL DEFAULT 0,-- rows of the day that did not fit
    read_ids  TEXT,                      -- JSON: every id it took down whole
    folded    INTEGER NOT NULL DEFAULT 0,
    named     INTEGER NOT NULL DEFAULT 0,
    retired   INTEGER NOT NULL DEFAULT 0,
    refused   TEXT,                      -- JSON list: what the door would not do, and why
    report    TEXT,                      -- its own account, whole
    line      TEXT,                      -- the one line it wakes to
    sentence  TEXT,                      -- the one for the room, or null
    row       INTEGER,                   -- the `dream` row that carries the line
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd  REAL,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dreams_night ON dreams(night);

-- The people's own work, beside the assistant's. A job is the assistant's --
-- a ceiling, a leash, one pointer in its head. A project belongs to the
-- people it serves: owned by one of them or several, with a folder maybe and
-- a Notes box they all write in. Its open tasks reach the assistant every
-- turn; its notes never do unless it asks for one project's box by name.
CREATE TABLE IF NOT EXISTS projects (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT    NOT NULL,
    owner   TEXT    NOT NULL,           -- a person's id | both
    folder  TEXT,                       -- a path on disk, or null
    status  TEXT    NOT NULL DEFAULT 'open',   -- open | closed
    -- Its own, not theirs: whether this project is on its desk. The shelf it
    -- is handed every turn is one line per project; a project on the desk
    -- is the one whose tasks it is actually carrying. It persists between
    -- turns because a pointer it has to re-pick every waking is not a
    -- pointer, it is a question.
    desk    INTEGER NOT NULL DEFAULT 0,
    created TEXT    NOT NULL,
    moved   TEXT    NOT NULL            -- last time anything on it changed
);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

-- One line of a Notes box. `who` is a column and not a convention: if the
-- assistant cannot tell one person's words from another's it will quote one
-- to the other, which is the failure this whole table is shaped around. A line keeps its
-- author for life; an edit keeps what it said before in `was`, and putting a
-- line down sets `gone` -- nothing here is deleted either.
CREATE TABLE IF NOT EXISTS project_notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project INTEGER NOT NULL,
    who     TEXT    NOT NULL,           -- a person's id | the assistant's kind -- never null
    dt      TEXT    NOT NULL,
    text    TEXT    NOT NULL,
    was     TEXT,                       -- JSON list of what it said before
    gone    INTEGER NOT NULL DEFAULT 0  -- 1 = put down, still on disk
);
CREATE INDEX IF NOT EXISTS idx_project_notes ON project_notes(project);

-- What somebody wants doing. `repeats` and `schedule` are stored and read by
-- nobody: there is no clock in this build, and the task says so on its own
-- face wherever it is drawn or sent. `job` names one of its jobs by title.
CREATE TABLE IF NOT EXISTS project_tasks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project  INTEGER NOT NULL,
    title    TEXT    NOT NULL,
    wants    TEXT,                      -- what it wants, in the asker's words
    state    TEXT    NOT NULL,          -- open | doing | done | dropped
    who      TEXT    NOT NULL,          -- who asked
    repeats  INTEGER NOT NULL DEFAULT 0,
    schedule TEXT,                      -- their words; nothing reads it yet
    job      TEXT,                      -- a job's title, or null
    created  TEXT    NOT NULL,
    moved    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_tasks ON project_tasks(project);

-- Where a project lives, beyond the one folder it already had: key and value
-- with whoever wrote it. `who` is stamped by the room the way `asked_by` is
-- on a task, so that no field anywhere lets the assistant write a line in a
-- person's name. Put down, never deleted:
-- `gone` is the house rule, the same one the notes box keeps.
CREATE TABLE IF NOT EXISTS project_resources (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    who     TEXT    NOT NULL,
    dt      TEXT    NOT NULL,
    gone    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_project_resources ON project_resources(project);

-- A notice: something that arrived on a task and waits for somebody to say
-- they have seen to it -- a comment a sense detected, a date that came, a
-- line the assistant or a person put there. It stands until it is checked
-- off, by a person or the assistant, with a note if they want one. Nothing
-- reads the world back to decide: a notice is the fact that something
-- arrived, and a check is the word that it has been seen to. Reading the
-- world back is what let a comment already answered stay "waiting" for
-- hours; instead the tab pops when something arrives and clears when
-- somebody says so -- for any kind of task, not for comments only.
-- `done_by` is stamped by the room. Checked, never deleted:
-- `done` is the house rule, the row stays.
CREATE TABLE IF NOT EXISTS task_notices (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task    INTEGER NOT NULL,
    dt      TEXT    NOT NULL,
    said    TEXT    NOT NULL,
    source  TEXT,
    meta    TEXT,
    done    INTEGER NOT NULL DEFAULT 0,
    done_by TEXT,
    done_dt TEXT,
    note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_notices ON task_notices(task, done);

-- Its notebook: short notes of its own, in front of it every turn. Numbered
-- by the room and never renumbered. Removing one sets `gone` and keeps the
-- row -- for the backup, not for it: nothing it can do reads a removed note
-- back. `born` and `gone` are reply rows, so how many turns a note was kept
-- is counted off its own answers rather than kept by hand. See notebook.py.
CREATE TABLE IF NOT EXISTS notebook (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    dt      TEXT    NOT NULL,           -- when it was written
    text    TEXT    NOT NULL,
    tokens  INTEGER NOT NULL,
    up      INTEGER NOT NULL DEFAULT 0,
    down    INTEGER NOT NULL DEFAULT 0,
    born    INTEGER NOT NULL,           -- the reply row of the turn that wrote it
    gone    INTEGER,                    -- the reply row of the turn that removed it
    gone_dt TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Roughly four characters to a token. One place, so a cap written in tokens
# anywhere else is the same guess as the estimate shown with a ~.
CHARS_PER_TOKEN = 4


def est_tokens(text: str) -> int:
    """Rough estimate. We have no tokenizer here, so this is honest guesswork:
    roughly four characters to a token. Displayed everywhere with a ~."""
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _migrate(conn) -> None:
    """Columns that arrived after there were already rows in here.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so we look first. Nothing in
    this function ever drops, rewrites or backfills anything: a store whose
    whole promise is that nothing is deleted cannot have a migration that
    could lose a word. A new column simply starts out null everywhere, and
    null is allowed to mean "not written yet"."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(rows)")}
    if "title" not in have:
        conn.execute("ALTER TABLE rows ADD COLUMN title TEXT")
        conn.commit()
    # Which mind wrote the row. Null on everything written before the model
    # could vary, and left null: a column filled in by inference is a column
    # that cannot be trusted later, and this one exists to be trusted later.
    if "by_model" not in have:
        conn.execute("ALTER TABLE rows ADD COLUMN by_model TEXT")
        conn.commit()
    # Outside the check above, so a store that got the column from SCHEMA on a
    # fresh create gets the index too.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rows_by_model"
                 " ON rows(by_model)")
    conn.commit()
    # A store that met the projects tables before its desk existed. Same
    # rule as above: the column starts out zero everywhere, which is exactly
    # what it means -- nothing on the desk yet.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    if have and "desk" not in have:
        conn.execute("ALTER TABLE projects ADD COLUMN desk INTEGER NOT NULL"
                     " DEFAULT 0")
        conn.commit()
    # What makes a task happen, and who it waits on. All four start null,
    # which is honest: a task written before these existed was triggered by
    # somebody remembering it, and said so.
    #
    # `at` is here and nothing fills it. The page draws a dated task as a
    # live trigger because one day it will be, and until the calendar sense
    # exists a filled `at` would be a clock that does not tick -- so the
    # column waits, empty, for the page that makes it true.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(project_tasks)")}
    if have:
        for col in ("sense", "sense_item", "at", "needs"):
            if col not in have:
                conn.execute("ALTER TABLE project_tasks ADD COLUMN "
                             + col + " TEXT")
        conn.commit()


def _renamed(conn) -> None:
    """Columns whose name changed, renamed in place before the schema runs --
    the schema's own index names the new column, so on an old store it would
    fail first. A rename moves no data and loses none; SQLite carries the
    index across with it."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    # An older store named the column after its first assistant. Exactly one
    # such `<name>_row` column, and no `reply_row` yet, is that column.
    old = [c for c in have if c.endswith("_row") and c != "reply_row"
           and c.replace("_", "").isalnum()]
    if "reply_row" not in have and len(old) == 1:
        conn.execute('ALTER TABLE events RENAME COLUMN "' + old[0] + '" TO reply_row')
        conn.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _renamed(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def tidy_title(title) -> str:
    """A title is one line. Whatever else is done with it, it is not allowed
    to be two -- a name on a shelf that wraps is not a name, it is prose."""
    one = " ".join(str(title or "").split())
    return one or None


def row_tokens(text: str, meta=None) -> int:
    """What a row costs it while it holds it: its words, plus the pictures
    that came in on it.

    Pixels are counted here rather than anywhere else so that no row can exist
    whose estimate is a lie. A held picture is re-sent every turn it stays
    loaded -- it is rent, not a one-off -- and it does its own budget
    arithmetic off these numbers. The picture's own figure was worked out when
    it was taken in, by the API's own rule; this only adds it up."""
    total = est_tokens(text)
    pics = (meta or {}).get("pictures") if isinstance(meta, dict) else None
    if isinstance(pics, list):
        total += sum(int(p.get("tokens") or 0)
                     for p in pics if isinstance(p, dict))
    return total


def add_row(conn, kind: str, text: str, replaces=None, meta=None,
            title=None, by_model=None) -> int:
    """`by_model` is which mind wrote it, in the room's own spelling --
    "claude_code/claude-opus-5", "openai/gpt-5.6-terra". Passed on everything
    the assistant writes: its replies, its essences, its dreams, and the record
    of a turn that broke. Left null on what people write, because a line a
    person typed was not written by a model at all."""
    cur = conn.execute(
        "INSERT INTO rows (dt, kind, text, tokens_est, loaded, replaces, meta,"
        " title, by_model) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            now(),
            kind,
            text,
            row_tokens(text, meta),
            json.dumps(replaces) if replaces else None,
            json.dumps(meta) if meta else None,
            tidy_title(title),
            by_model or None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_title(conn, row_id: int, title) -> bool:
    """Put a name on a row that already exists.

    This is the one thing that writes over something it wrote, and it is
    allowed because a title is a label on a door and not what is behind it.
    The text is untouched, so the vector still stands, the id does not move,
    and the trail does not grow a link -- which is what made renaming by
    rewriting so expensive: it retired the essence, minted a new id, and read
    the same words into a vector all over again to change the name on the
    front. The name it used to have is kept in the events, where everything
    else it does to itself is kept."""
    name = tidy_title(title)
    if not name:
        return False
    conn.execute("UPDATE rows SET title = ? WHERE id = ?", (name, int(row_id)))
    conn.commit()
    return True


def unload(conn, ids) -> int:
    """Drop rows from the working set. The text stays on disk forever."""
    ids = [int(i) for i in ids or []]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(f"UPDATE rows SET loaded = 0 WHERE id IN ({marks})", ids)
    conn.commit()
    return cur.rowcount


def reload_rows(conn, ids) -> int:
    """Bring rows back into the working set. Forgetting is reversible."""
    ids = [int(i) for i in ids or []]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(f"UPDATE rows SET loaded = 1 WHERE id IN ({marks})", ids)
    conn.commit()
    return cur.rowcount


def _as_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["loaded"] = bool(d["loaded"])
    d["replaces"] = json.loads(d["replaces"]) if d["replaces"] else None
    d["meta"] = json.loads(d["meta"]) if d["meta"] else None
    return d


def rooms_of(kind: str, meta) -> list:
    """Which plain views a said line belongs to. One rule, mirrored in the
    page's own roomsOf, so the two can never disagree about whose room a
    line is in.

    A user line is its speaker's room; absence of `who` means the owner, on
    every row from before the labels too. A line of the assistant's wears
    the room it was answering in `room` -- a name, a list of names, or
    "house" for a waking of the room's own; absent only on its rows from
    before the stamps. Any other person's room is a conversation and nothing
    else: the house lines, the night, the world, the hands, angel me,
    everything with machinery in it, draws in the owner's view alone.
    The stamp on the row does not move for this; only where it is drawn.

    Not for essences: their room label sorts the shelf and is never a
    filter."""
    meta = meta or {}
    if kind == "user":
        return [meta.get("who") or home.OWNER]
    if kind in (home.SELF, home.SELF_INTERIM, "lost"):
        room = meta.get("room")
        if isinstance(room, list):
            return [str(x) for x in room] or [home.OWNER]
        if room == "house":
            return [home.OWNER]
        return [str(room)] if room else [home.OWNER]
    return [home.OWNER]


def delivered_voice_backend(row) -> bool:
    """True for the durable backend copy whose native transcript is visible."""
    if not row or row.get('kind') != home.SELF:
        return False
    meta = row.get('meta') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return False
    delivery = meta.get('voice_backend') if isinstance(meta, dict) else None
    return isinstance(delivery, dict) and delivery.get('delivered') is True


def loaded_rows(conn) -> list:
    return [_as_dict(r) for r in conn.execute(
        "SELECT * FROM rows WHERE loaded = 1 ORDER BY id")]


def all_rows(conn) -> list:
    return [_as_dict(r) for r in conn.execute("SELECT * FROM rows ORDER BY id")]


# How much of the grey past one look at the room carries. Everything it is
# holding comes every time -- that part is small and it is the answer to "what
# is in its head" -- but the conversation behind it is a year of talking, and
# handing all of it over to draw the last screenful of it was the whole cost.
PAGE_ROWS = 120

# What one ask may take, however loudly it asks. A page is a page.
MAX_PAGE_ROWS = 400


def row_counts(conn) -> dict:
    """How much of it there is, so a page can say what it did not carry."""
    r = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN loaded = 0 THEN 1 ELSE 0 END)"
        " AS past FROM rows").fetchone()
    return {"rows": r["n"] or 0, "past": r["past"] or 0}


def past_before(conn, floor) -> int:
    """Rows out of its memory older than `floor` -- what "show earlier" has
    left to offer. A floor of None means the page held nothing back."""
    if floor is None:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM rows WHERE loaded = 0 AND id < ?",
        (int(floor),)).fetchone()[0]


def recent_rows(conn, past: int = PAGE_ROWS):
    """Its working set whole, and the newest stretch of the past behind it.
    Returns (rows, floor) -- `floor` being where that stretch starts, and the
    id the next page is asked for.

    Two different things in one list on purpose, because that is exactly what
    the chat draws: everything it is holding, and the tail of the grey past
    above it. The working set is never cut to a page. A row it is holding is
    its own whatever its id, and a memory drawn with a page missing would be a
    lie about what it has in its head.

    Which is also why `floor` is the floor of the *past*, not the lowest id
    here: one old essence still in its memory must not make the room think it
    has the thousand rows between it and today."""
    past = max(0, min(int(past), MAX_PAGE_ROWS))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM rows WHERE loaded = 0 ORDER BY id DESC LIMIT ?",
        (past,))]
    floor = min(ids) if ids else None
    q, args = "SELECT * FROM rows WHERE loaded = 1", []
    if floor is not None:
        q += " OR id >= ?"
        args.append(floor)
    return [_as_dict(r) for r in conn.execute(q + " ORDER BY id", args)], floor


def rows_before(conn, before, past: int = PAGE_ROWS) -> list:
    """The page of the past that sits just above `before`, oldest first, so it
    prepends where it belongs. Loaded rows in the stretch come along rather
    than leaving a hole in it -- the room already has them and says so."""
    past = max(1, min(int(past), MAX_PAGE_ROWS))
    rows = [_as_dict(r) for r in conn.execute(
        "SELECT * FROM rows WHERE id < ? ORDER BY id DESC LIMIT ?",
        (int(before), past))]
    return rows[::-1]


def search(conn, needle: str, limit: int = 50) -> list:
    """Plain substring search over everything, loaded or not. The assistant's
    grep."""
    return [_as_dict(r) for r in conn.execute(
        "SELECT * FROM rows WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
        (f"%{needle}%", limit))]


def get_row(conn, row_id: int):
    r = conn.execute("SELECT * FROM rows WHERE id = ?", (int(row_id),)).fetchone()
    return _as_dict(r) if r else None


def rows_in_id_range(conn, lo, hi, unloaded_only=True) -> list:
    lo = 0 if lo is None else int(lo)
    hi = 10**12 if hi is None else int(hi)
    q = "SELECT * FROM rows WHERE id BETWEEN ? AND ?"
    if unloaded_only:
        q += " AND loaded = 0"
    return [_as_dict(r) for r in conn.execute(q + " ORDER BY id", (lo, hi))]


# Stored dates are full ISO8601 in UTC. A bound it writes is nearly always
# coarser than that -- a day, or a time to the minute -- and the end of a
# coarse bound has to mean the end of it. Asking for "up to the 20th" and
# getting nothing from the 20th is the kind of wrong nobody ever notices,
# because a reach that under-returns looks exactly like a quiet stretch.
_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

_STEP = {"day": timedelta(days=1), "hour": timedelta(hours=1),
         "minute": timedelta(minutes=1), "second": timedelta(seconds=1)}


def _precision(body: str) -> str:
    """How finely it named the moment. A day is a day, not midnight at the
    start of one."""
    if "T" not in body and " " not in body:
        return "day"
    clock = body.replace(" ", "T").split("T", 1)[1]
    return {0: "hour", 1: "minute"}.get(clock.count(":"), "second")


def dt_bound(value, end: bool = False):
    """One end of a stretch of time, turned into something that compares
    against a stored dt. Returns (bound, complaint).

    The end is exclusive and pushed to the end of whatever unit it named, so
    a day means the whole day and a named second is included rather than
    missed by one. A bound that cannot be read comes back as a complaint --
    never as a silent bound of its own, which is how a typo used to return it
    entire life instead of nothing."""
    if value is None or str(value).strip() == "":
        return None, None
    s = str(value).strip()
    try:
        when = datetime.fromisoformat(s)
    except ValueError:
        return None, ('I could not read "' + s + '" as a date, so I did not '
                      "use it as a bound. I write them the way they come back "
                      "to me: 2026-08-20, or 2026-08-20T18:00:00+00:00.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    if end:
        when += _STEP[_precision(_OFFSET.sub("", s))]
    return when.isoformat(timespec="seconds"), None


def widen(lo, hi, minutes):
    """A span, opened out by so many minutes at each end.

    The rows an essence stands for are usually a handful of minutes, and the
    thing worth having is what was around them -- which is exactly what the
    essence flattened away."""
    step = int(minutes or 0)
    if step <= 0:
        return lo, hi
    gap = timedelta(minutes=step)
    lo_v, _ = dt_bound(lo)
    hi_v, _ = dt_bound(hi)
    if lo_v:
        lo = (datetime.fromisoformat(lo_v) - gap).isoformat(timespec="seconds")
    if hi_v:
        hi = (datetime.fromisoformat(hi_v) + gap).isoformat(timespec="seconds")
    return lo, hi


def rows_in_dt_range(conn, lo, hi, unloaded_only=True) -> list:
    """Everything said in a stretch of time. Both ends are its own to set; either
    left out means open at that end."""
    lo, _ = dt_bound(lo, end=False)
    hi, _ = dt_bound(hi, end=True)
    lo = lo or "0000"
    hi = hi or "9999"
    q = "SELECT * FROM rows WHERE dt >= ? AND dt < ?"
    if unloaded_only:
        q += " AND loaded = 0"
    return [_as_dict(r) for r in conn.execute(q + " ORDER BY id", (lo, hi))]


# An essence may stand for other essences, so following a trail is a walk and
# not a lookup. The depth cap is a guard against a cycle, not a real bound.
MAX_TRAIL_DEPTH = 12


def rows_by_ids(conn, ids) -> list:
    ids = [int(i) for i in ids or []]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return [_as_dict(r) for r in conn.execute(
        f"SELECT * FROM rows WHERE id IN ({marks}) ORDER BY id", ids)]


def trail_index(conn) -> dict:
    """Every row's lineage in one query, so following a chain costs nothing
    per step."""
    out = {}
    for r in conn.execute(
            "SELECT id, dt, kind, loaded, tokens_est, replaces FROM rows"):
        out[r["id"]] = {
            "id": r["id"],
            "dt": r["dt"],
            "kind": r["kind"],
            "loaded": bool(r["loaded"]),
            "tokens_est": r["tokens_est"],
            "replaces": json.loads(r["replaces"]) if r["replaces"] else [],
        }
    return out


def trail(index: dict, essence_id, max_depth: int = MAX_TRAIL_DEPTH) -> dict:
    """Follow an essence back to the rows it was written from.

    An edit retires its predecessor and a fold swallows several, so a source
    is often another essence. Those are followed through to the conversation
    underneath -- that is what stops an edit or a fold breaking the trail.

    Ids that are not in the store come back in `missing`. They are never
    quietly dropped: an unfindable source has to be louder than no source.
    """
    essence_id = int(essence_id)
    start = index.get(essence_id)
    if start is None:
        return {"essence": essence_id, "exists": False, "direct": [],
                "rows": [], "loaded": [], "unloaded": [], "through": [],
                "missing": [], "covers": None, "truncated": False}

    direct = [int(i) for i in start["replaces"]]
    seen, rows, through, missing = {essence_id}, [], [], []
    queue = [(i, 1) for i in direct]
    truncated = False
    while queue:
        rid, depth = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        r = index.get(rid)
        if r is None:
            missing.append(rid)
        elif r["kind"] == "essence":
            through.append(rid)
            if depth >= max_depth:
                truncated = True
            else:
                queue.extend((int(i), depth + 1) for i in r["replaces"])
        else:
            rows.append(rid)

    rows.sort()
    dts = sorted(index[i]["dt"] for i in rows)
    return {
        "essence": essence_id,
        "exists": True,
        "direct": direct,
        "rows": rows,
        "loaded": [i for i in rows if index[i]["loaded"]],
        "unloaded": [i for i in rows if not index[i]["loaded"]],
        "through": sorted(through),
        "missing": sorted(missing),
        "covers": ({"from": dts[0], "to": dts[-1], "rows": len(dts)}
                   if dts else None),
        "truncated": truncated,
    }


def last_reply_row(conn):
    r = conn.execute(
        "SELECT id FROM rows WHERE kind = '" + home.SELF + "'"
        " AND COALESCE(json_extract(meta, '$.voice_handled'), 0) = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return r["id"] if r else None


def _event_dict(r) -> dict:
    d = dict(r)
    d["detail"] = json.loads(d["detail"]) if d["detail"] else None
    return d


def events_for(conn, reply_row) -> list:
    """What happened on one turn, so the next turn can be told about it."""
    if reply_row is None:
        return []
    return [_event_dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE reply_row = ? ORDER BY id", (reply_row,))]


def events_since(conn, reply_row) -> list:
    """Everything logged from the last real reply onward. That is wider than
    one turn on purpose: a turn that broke on the way back leaves its record
    hanging off a row that is not a reply row, and if the report only ever
    looked at the last answer the assistant would never find out it
    happened."""
    lo = 0 if reply_row is None else int(reply_row)
    return [_event_dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE reply_row >= ? ORDER BY id", (lo,))]


# SQLite takes 999 placeholders in the oldest builds still around, and the
# query below uses each id twice. Chunking here rather than trusting a page
# size to stay small is the difference between a limit that is enforced and
# one that is merely believed in.
_IN_CHUNK = 400


def events_for_rows(conn, ids) -> list:
    """Every event belonging to these rows, in the order they happened.

    Two ways of belonging, and both have to be asked for. Most events hang off
    its reply by `reply_row`. A worker's account does not: it hangs off
    whichever reply of its own was newest when the errand walked in, and names
    the report row it is really about inside its own detail. Fetching only by
    `reply_row` would page a hand's report in with no account of the run under
    it, which reads as an errand that did nothing."""
    ids = sorted({int(i) for i in ids or []})
    if not ids:
        return []
    found = {}
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i:i + _IN_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(
                "SELECT * FROM events WHERE reply_row IN (" + marks + ")"
                " OR (kind = 'worker' AND json_extract(detail, '$.row')"
                " IN (" + marks + "))", chunk + chunk):
            found[r["id"]] = r
    return [_event_dict(found[i]) for i in sorted(found)]


def _dropped_entry(r) -> dict:
    """One line of the out-of-reach catalogue. A message gets its first line,
    because the first line of something said is a fair reminder of it. An
    essence gets its title *and* its first line: the name says which one it
    is, the line says whether it is the one it wants. An untitled one comes
    through with a null title rather than borrowing its opening words to look
    named -- the name is its own to write, and a first line standing in for one
    would quietly hide that it was never written."""
    e = {"id": r["id"], "dt": r["dt"], "kind": r["kind"],
         "tokens_est": r["tokens_est"]}
    if r["kind"] == "essence":
        e["title"] = r["title"]
    e["peek"] = r["peek"]
    return e


_DROPPED_COLUMNS = ("id", "dt", "kind", "tokens_est", "title",
                    "substr(text, 1, 90) AS peek")


def dropped_index(conn, limit=30) -> dict:
    """A catalogue of what is out of reach, so it can choose without loading.
    Newest first, capped, and honest about what the cap hid."""
    total = conn.execute("SELECT COUNT(*) FROM rows WHERE loaded = 0").fetchone()[0]
    rows = conn.execute(
        "SELECT " + ", ".join(_DROPPED_COLUMNS) + " FROM rows WHERE loaded = 0"
        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    entries = [_dropped_entry(r) for r in rows]
    return {
        "total_dropped": total,
        "showing": len(rows),
        "not_shown": max(0, total - len(rows)),
        "entries": entries[::-1],
    }


# The newest-first catalogue can only ever be read from one end. These two
# slice it by id or by date instead, so a stretch that fell off the cap can
# still be named and asked for directly. Index lines only -- never text --
# which is what keeps a wide ask cheap enough to make at all.
MAX_DROPPED_RANGE = 200


def dropped_by_id_range(conn, lo, hi, limit=MAX_DROPPED_RANGE) -> dict:
    """The out-of-reach catalogue, sliced by id range and read oldest first
    within it. Bounded and honest about what the bound hid."""
    lo_v = 0 if lo is None else int(lo)
    hi_v = 10**12 if hi is None else int(hi)
    total = conn.execute(
        "SELECT COUNT(*) FROM rows WHERE loaded = 0 AND id BETWEEN ? AND ?",
        (lo_v, hi_v)).fetchone()[0]
    rows = conn.execute(
        "SELECT " + ", ".join(_DROPPED_COLUMNS) + " FROM rows"
        " WHERE loaded = 0 AND id BETWEEN ? AND ? ORDER BY id LIMIT ?",
        (lo_v, hi_v, limit)).fetchall()
    entries = [_dropped_entry(r) for r in rows]
    return {
        "total_matched": total,
        "showing": len(entries),
        "not_shown": max(0, total - len(entries)),
        "entries": entries,
    }


def dropped_by_dt_range(conn, lo, hi, limit=MAX_DROPPED_RANGE) -> dict:
    """The out-of-reach catalogue, sliced by a stretch of time and read oldest
    first within it. Both ends go through the same coarse-date handling as
    every other reach: a bound it wrote badly is the caller's to catch, not
    silently widened here."""
    lo_v, _ = dt_bound(lo, end=False)
    hi_v, _ = dt_bound(hi, end=True)
    lo_v = lo_v or "0000"
    hi_v = hi_v or "9999"
    total = conn.execute(
        "SELECT COUNT(*) FROM rows WHERE loaded = 0 AND dt >= ? AND dt < ?",
        (lo_v, hi_v)).fetchone()[0]
    rows = conn.execute(
        "SELECT " + ", ".join(_DROPPED_COLUMNS) + " FROM rows"
        " WHERE loaded = 0 AND dt >= ? AND dt < ? ORDER BY id LIMIT ?",
        (lo_v, hi_v, limit)).fetchall()
    entries = [_dropped_entry(r) for r in rows]
    return {
        "total_matched": total,
        "showing": len(entries),
        "not_shown": max(0, total - len(entries)),
        "entries": entries,
    }


def add_event(conn, reply_row, kind: str, summary: str, detail=None) -> int:
    cur = conn.execute(
        "INSERT INTO events (dt, reply_row, kind, summary, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (now(), reply_row, kind, summary,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))
    conn.commit()
    return cur.lastrowid


def update_event(conn, event_id: int, summary: str, detail=None) -> None:
    """An event's summary and detail, rewritten in place: for a record that
    has to stand where it happened but can only be finished later."""
    conn.execute("UPDATE events SET summary = ?, detail = ? WHERE id = ?",
                 (summary, json.dumps(detail, ensure_ascii=False)
                  if detail is not None else None, int(event_id)))
    conn.commit()


def all_events(conn) -> list:
    return [_event_dict(r) for r in
            conn.execute("SELECT * FROM events ORDER BY id")]


# --- vectors -----------------------------------------------------------------
#
# Everything below is derived. It reads the text in `rows` and writes nothing
# back to it. Deleting every vector loses nothing that cannot be made again,
# which is why the rebuild is a `DELETE` and not a careful migration.


def text_hash(text: str) -> str:
    """A fingerprint of exactly what was embedded, so a vector can never be
    quietly wrong about which words it came from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_vector(values) -> bytes:
    vals = [float(v) for v in values]
    return struct.pack("<%df" % len(vals), *vals)


def unpack_vector(blob: bytes) -> list:
    return list(struct.unpack("<%df" % (len(blob) // 4), blob))


def essence_rows(conn, loaded_only: bool = False) -> list:
    """Every essence it has ever written, retired ones included. A retired
    essence keeps its vector: it let go of it, it did not unsay it."""
    q = "SELECT * FROM rows WHERE kind = 'essence'"
    if loaded_only:
        q += " AND loaded = 1"
    return [_as_dict(r) for r in conn.execute(q + " ORDER BY id")]


def retired_essences(conn) -> set:
    """Essences a later essence was written over: a predecessor an edit
    replaced, or one a fold swallowed.

    They are not gone and they are not wrong -- they are simply not what it
    would say today. Five versions of one thought coming back from a search is
    five ways of saying the same thing with four of them out of date, so this
    is what `include_retired` decides about."""
    mine = {r["id"] for r in conn.execute(
        "SELECT id FROM rows WHERE kind = 'essence'")}
    out = set()
    for r in conn.execute(
            "SELECT replaces FROM rows WHERE kind = 'essence'"
            " AND replaces IS NOT NULL"):
        out |= {int(i) for i in json.loads(r["replaces"])} & mine
    return out


def essence_shelf(conn, include_retired: bool = False) -> list:
    """What a search runs over. Loaded and unloaded alike -- the ones it has
    put down are the whole point of being able to look -- but not, by default,
    the ones it has already rewritten."""
    rows = essence_rows(conn)
    if include_retired:
        return rows
    gone = retired_essences(conn)
    return [r for r in rows if r["id"] not in gone]


def put_vector(conn, row_id: int, model: str, dim: int, values,
               digest: str, n_tokens=None, truncated: bool = False) -> None:
    """Write one reading. Replaces any earlier reading by the same model --
    that is a re-read of the same text, not a second opinion."""
    conn.execute(
        "INSERT OR REPLACE INTO vectors"
        " (row_id, model, dim, vec, text_hash, n_tokens, truncated, dt)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(row_id), model, int(dim), pack_vector(values), digest,
         None if n_tokens is None else int(n_tokens), 1 if truncated else 0,
         now()))
    conn.commit()


def get_vector(conn, row_id: int, model: str):
    r = conn.execute(
        "SELECT * FROM vectors WHERE row_id = ? AND model = ?",
        (int(row_id), model)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["vec"] = unpack_vector(d["vec"])
    d["truncated"] = bool(d["truncated"])
    return d


def vectors_for(conn, model: str) -> dict:
    """Every reading this model has made, by row id. One query, because a
    thousand of these is still only a few megabytes."""
    out = {}
    for r in conn.execute(
            "SELECT row_id, dim, vec, text_hash, n_tokens, truncated"
            " FROM vectors WHERE model = ? ORDER BY row_id", (model,)):
        out[r["row_id"]] = {
            "row_id": r["row_id"],
            "dim": r["dim"],
            "vec": unpack_vector(r["vec"]),
            "text_hash": r["text_hash"],
            "n_tokens": r["n_tokens"],
            "truncated": bool(r["truncated"]),
        }
    return out


def stale_vectors(conn, model: str) -> dict:
    """Which essences this model has not read, and which it read when they
    said something else. Both are work for the next backfill; the second is
    the one that would otherwise go unnoticed forever."""
    have = {r["row_id"]: r["text_hash"] for r in conn.execute(
        "SELECT row_id, text_hash FROM vectors WHERE model = ?", (model,))}
    missing, drifted = [], []
    for row in essence_rows(conn):
        digest = have.get(row["id"])
        if digest is None:
            missing.append(row["id"])
        elif digest != text_hash(row["text"]):
            drifted.append(row["id"])
    return {"missing": missing, "drifted": drifted}


def drop_vectors(conn, model=None) -> int:
    """Throw the readings away. The essences are untouched, so this is only
    ever a few minutes of compute lost."""
    if model is None:
        cur = conn.execute("DELETE FROM vectors")
    else:
        cur = conn.execute("DELETE FROM vectors WHERE model = ?", (model,))
    conn.commit()
    return cur.rowcount


def vector_models(conn) -> list:
    """What has read it, and how much of it each one got through."""
    return [dict(r) for r in conn.execute(
        "SELECT model, dim, COUNT(*) AS n, SUM(truncated) AS truncated,"
        " MAX(n_tokens) AS max_tokens, MIN(dt) AS first, MAX(dt) AS last"
        " FROM vectors GROUP BY model, dim ORDER BY model")]


# --- dreams ------------------------------------------------------------------
#
# The account of a night. Written in two halves: a row is opened when it
# starts, so a dream that breaks halfway still leaves a record that it was
# tried, and closed with everything it did -- or with what stopped it.


def _dream_dict(r) -> dict:
    d = dict(r)
    d["free"] = bool(d["free"])
    d["read_ids"] = json.loads(d["read_ids"]) if d["read_ids"] else []
    d["refused"] = json.loads(d["refused"]) if d["refused"] else []
    return d


def open_dream(conn, night: str, why: str, free: bool, model=None) -> int:
    cur = conn.execute(
        "INSERT INTO dreams (night, started, why, free, status, model)"
        " VALUES (?, ?, ?, ?, 'running', ?)",
        (night, now(), why, 1 if free else 0, model))
    conn.commit()
    return cur.lastrowid


def close_dream(conn, dream_id: int, **fields) -> None:
    """Fill in what happened. Only the columns named are touched; `ended` is
    set here so a closed dream always says when."""
    cols, vals = ["ended"], [now()]
    for k, v in fields.items():
        if k in ("read_ids", "refused"):
            v = json.dumps(v, ensure_ascii=False) if v is not None else None
        cols.append(k)
        vals.append(v)
    conn.execute(
        "UPDATE dreams SET " + ", ".join(c + " = ?" for c in cols)
        + " WHERE id = ?", vals + [int(dream_id)])
    conn.commit()


def get_dream(conn, dream_id: int):
    r = conn.execute("SELECT * FROM dreams WHERE id = ?",
                     (int(dream_id),)).fetchone()
    return _dream_dict(r) if r else None


def dreams_for_night(conn, night: str) -> list:
    return [_dream_dict(r) for r in conn.execute(
        "SELECT * FROM dreams WHERE night = ? ORDER BY id", (night,))]


def recent_dreams(conn, limit: int = 10) -> list:
    """Newest first."""
    return [_dream_dict(r) for r in conn.execute(
        "SELECT * FROM dreams ORDER BY id DESC LIMIT ?", (int(limit),))]


def last_dream_done(conn):
    """The newest night that actually read something, so the next one knows
    where the day begins."""
    r = conn.execute(
        "SELECT * FROM dreams WHERE status = 'done' AND day_to IS NOT NULL"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return _dream_dict(r) if r else None


def last_dream_any(conn):
    r = conn.execute("SELECT * FROM dreams ORDER BY id DESC LIMIT 1").fetchone()
    return _dream_dict(r) if r else None
