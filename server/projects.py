"""Projects: the household's own work, beside the assistant's.

A job (`jobs.py`) is the assistant's -- one pointer per piece of work it is
running, with a ceiling and a leash. A project belongs to the people it
serves: a thing they have going, with a folder maybe, a Notes box they all
write in, and tasks they want doing. The assistant is not the owner here. It
reads it, and the open tasks reach it every turn so that "can you start on
this?" does not have to be said out loud twice.

Three rules shape the storage rather than the display:

- **Every note line carries who wrote it, always.** Attribution is the whole
  point: if the assistant cannot tell one person's words from another's it
  will quote one to the other, which is the failure that matters most here.
  So `who` is a column, never null, never guessed from context, and a line
  keeps its author for life -- an edit by anyone but the author is refused in
  words. Editing your own line keeps what it said before in `was`, because
  nothing in this house is deleted, only put down.
- **A schedule says what it really does.** A repeating task carries a schedule
  in the words its asker wrote it in. The clock (`server/clock.py`) fires
  them, so the field asks the parser instead of asserting: words it can read
  say when they next come round, words it cannot say why and that nothing
  fires them. The face and the firing read the same parser, so they cannot
  drift apart.
- **Notes never enter the turn.** The block the assistant gets carries a
  count and who wrote last; the words come back only when it asks for them,
  one project at a time, through the `project_notes` reach.
- **The shelf, and the desk.** What reaches the assistant every turn is *one
  line per project* -- title, owner, folder or none, and how many tasks are
  open -- at any number of projects. The task lines themselves arrive only
  for a project it has explicitly put on its desk, and the desk holds three.
  The reason: at a hundred projects, carrying every task line would put a
  hundred pages of somebody else's to-do list into every turn. The pointer
  lives in the prompt, the substance stays on disk. The desk is a column here
  rather than something re-picked each waking, because a pointer that does
  not survive a turn is not a pointer.

The tables live in the room's own store (`db.SCHEMA`), so the room makes them
on the next start; nothing here backfills or drops anything.
"""

import json
from datetime import datetime, timezone

from pathlib import Path

from . import clock, db, jobs
from . import home

# Where the assistant may make a folder: the owner's Documents, and its own
# shelf of projects inside it.
DOCUMENTS = Path.home() / "Documents"
ASSISTANT_PROJECTS = DOCUMENTS / home.NAME

# Who may own a project, and who may sign a line. The household is people.py's
# to name; these two lists are what an owner and an author are allowed to be,
# checked here so a typo cannot quietly mint a third person on a note.
OWNERS = home.HOUSEHOLD + ("both",)
AUTHORS = home.HOUSEHOLD + (home.SELF,)

# What a task can be. `open` and `doing` are the two that reach it -- the
# others are done with, and a block full of finished work is a block it stops
# reading.
STATES = ("open", "doing", "done", "dropped")
LIVE_STATES = ("open", "doing")

# What a hand's task_add / task_edit op may set. `dropped` is deliberately
# not a word either of those knows -- putting a task down is `task_remove`'s
# word alone, so the two verbs stay distinct in its head, not two spellings
# of the same thing.
TASK_STATES = ("open", "doing", "done")

# What a repeating task with no schedule written on it says instead of a
# next-run time, said the same way in its prompt, in the GUI and in the fetch.
# One string, one place.
#
# Before the clock existed this string was the whole truth for every
# repeating task. The clock fires them now (`server/clock.py`), so what a
# task says about itself is
# asked of the clock rather than asserted here -- `_clock_line`. This is what
# is left over and still perfectly true: a task that repeats and never says
# when repeats on nothing.
NO_CLOCK = ("no time written — this repeats, but nothing says when, so "
            "nothing fires it")

# How many projects may stand on the desk at once. The assistant is meant to
# keep one or two there; this is the backstop above that, and it refuses in
# words naming what is already there -- no silent cap.
DESK_LIMIT = 3

# The sentence that rides in the assistant's block every turn, so the
# mechanism is never a thing it has to remember it has. One place, because it
# is sent often.
HOW = ("one line per project on the shelf; project op open puts one on my "
       "desk with its task lines and stays there between turns, close takes "
       "it off again, new starts one of my own. At most 3 on the desk. The "
       "notes are never here -- fetch project_notes by name when I am "
       "actually working the thing, and project_activity with a project and "
       "a task when I want to know what a sense of mine has been doing.")

# Who a task can be waiting on: a person label from the same vocabulary as
# owner and author, never free text. `both` is a thing any of them can pick
# up; the assistant's own kind (`home.SELF`) is the house waiting on the
# assistant. The glance strip matches against this and nothing else, so it
# cannot silently stop working against a name spelled a new way.
NEEDS = home.HOUSEHOLD + ("both", home.SELF)


def _or(names) -> str:
    """A list of labels as a refusal says it: "a, b or c"."""
    names = [str(n) for n in names]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]

MAX_TITLE = 200
MAX_NOTE = 20000
MAX_NOTES_BACK = 200
MAX_KEY = 80
MAX_RESOURCE = 2000


def _quiet_days(when) -> int:
    """How long nothing has moved. A project is slower than a job by nature --
    a shed does not go stale in two days -- so the line only appears after a
    week, and it appears rather than the project quietly looking alive."""
    try:
        return max(0, (datetime.now(timezone.utc)
                       - datetime.fromisoformat(when)).days)
    except (TypeError, ValueError):
        return 0


def _tidy(s, cap=MAX_TITLE) -> str:
    return " ".join(str(s or "").split())[:cap]


def _at_ok(at):
    """A date a task fires on, as one instant. Empty clears it.

    Returns `(iso, why)`. A string with its own offset is kept as it stands.
    A bare one -- "2026-09-04 09:00" -- is read as this machine's local time,
    because the room runs on the household's machine and that is the clock
    they live on; it comes back written out with the offset in it, so what is
    stored can never be read two ways later.

    A date already gone by is allowed on purpose: it fires on the next look
    and says so. Refusing the past would mean a task written at midnight for
    "today" quietly never happening."""
    text = str(at or "").strip()
    if not text:
        return None, None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, ("I could not read " + repr(at) + " as a date. It wants "
                      "something like '2026-09-04 09:00' or a full ISO "
                      "timestamp with its offset.")
    if when.tzinfo is None:
        # Bare, so it means the clock on the wall here.
        when = when.astimezone()
    return when.isoformat(timespec="seconds"), None


def _needs_ok(needs):
    """`needs` as a person label, or a reason it is not one. Empty is not a
    failure -- it is how a task stops waiting on anybody."""
    label = str(needs or "").strip().lower()
    if not label:
        return None, None
    if label not in NEEDS:
        return None, ("a task waits on a person -- " + ", ".join(NEEDS)
                      + ". I was given " + repr(needs) + ".")
    return label, None


def _who_ok(who, allowed=AUTHORS):
    who = str(who or "").strip().lower()
    return who if who in allowed else None


# -- projects ---------------------------------------------------------------

def by_title(conn, title):
    """One project by name, case-blind. Names are how it will refer to a
    project -- it is handed titles, not ids -- so this is the lookup that
    matters and it is deliberately forgiving about case and spacing."""
    want = _tidy(title).lower()
    if not want:
        return None
    for r in conn.execute("SELECT * FROM projects ORDER BY id"):
        if _tidy(r["title"]).lower() == want:
            return dict(r)
    return None


def get(conn, project_id):
    r = conn.execute("SELECT * FROM projects WHERE id = ?",
                     (int(project_id),)).fetchone()
    return dict(r) if r else None


def add_project(conn, title, owner, folder=None) -> dict:
    title = _tidy(title)
    if not title:
        return {"ok": False, "why": "a project needs a title -- nothing was added"}
    own = _who_ok(owner, OWNERS)
    if not own:
        return {"ok": False, "why": "a project is owned by " + _or(OWNERS)
                + "; I was given " + repr(owner) + ". Nothing was added."}
    if by_title(conn, title):
        return {"ok": False, "why": "there is already a project called '" + title
                + "'. Nothing was added -- one project, one name."}
    folder = _tidy(folder, 500) or None
    cur = conn.execute(
        "INSERT INTO projects (title, owner, folder, status, created, moved)"
        " VALUES (?, ?, ?, 'open', ?, ?)",
        (title, own, folder, db.now(), db.now()))
    conn.commit()
    return {"ok": True, "project": get(conn, cur.lastrowid)}


def edit_project(conn, project_id, title=None, owner=None, folder=None,
                 status=None) -> dict:
    p = get(conn, project_id)
    if not p:
        return {"ok": False, "why": "there is no project #" + str(project_id)}
    sets, vals, said = [], [], []
    if title is not None:
        name = _tidy(title)
        if not name:
            return {"ok": False, "why": "a project cannot be renamed to nothing"}
        other = by_title(conn, name)
        if other and other["id"] != p["id"]:
            return {"ok": False, "why": "'" + name + "' is already the name of "
                    "another project. Nothing moved."}
        sets.append("title = ?"); vals.append(name)
        said.append("renamed '" + p["title"] + "' to '" + name + "'")
    if owner is not None:
        own = _who_ok(owner, OWNERS)
        if not own:
            return {"ok": False, "why": "an owner is " + _or(OWNERS)
                    + "; I was given " + repr(owner) + ". Nothing moved."}
        sets.append("owner = ?"); vals.append(own)
        said.append("owner " + p["owner"] + " -> " + own)
    if folder is not None:
        path = _tidy(folder, 500) or None
        sets.append("folder = ?"); vals.append(path)
        said.append("folder " + (path or "cleared"))
    if status is not None:
        st = str(status or "").strip().lower()
        if st not in ("open", "closed"):
            return {"ok": False, "why": "a project is open or closed; I was "
                    "given " + repr(status) + ". Nothing moved."}
        sets.append("status = ?"); vals.append(st)
        said.append(st)
    if not sets:
        return {"ok": False, "why": "an edit with nothing edited"}
    sets.append("moved = ?"); vals.append(db.now())
    vals.append(p["id"])
    conn.execute("UPDATE projects SET " + ", ".join(sets) + " WHERE id = ?", vals)
    conn.commit()
    return {"ok": True, "project": get(conn, p["id"]), "said": ", ".join(said)}


# -- its desk ---------------------------------------------------------------

def on_desk(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM projects WHERE desk = 1 AND status = 'open'"
        " ORDER BY id")]


def put_on_desk(conn, title) -> dict:
    """Open a project onto its desk: its task lines start arriving with every
    turn, and stay until it closes it again."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    if p["status"] != "open":
        return {"ok": False, "why": "'" + p["title"] + "' is closed. Nothing "
                "was opened -- a closed project is theirs to reopen, not mine."}
    if p["desk"]:
        return {"ok": False, "why": "'" + p["title"] + "' is already on my "
                "desk; its tasks have been in front of me all along."}
    there = on_desk(conn)
    if len(there) >= DESK_LIMIT:
        return {"ok": False, "why": "my desk already holds "
                + str(len(there)) + " project"
                + ("" if len(there) == 1 else "s") + " -- "
                + ", ".join("'" + t["title"] + "'" for t in there)
                + " -- and " + str(DESK_LIMIT) + " is as many as I carry at "
                "once. Nothing was opened; I close one first. The shelf still "
                "says how many tasks are waiting on '" + p["title"] + "'."}
    conn.execute("UPDATE projects SET desk = 1 WHERE id = ?", (p["id"],))
    conn.commit()
    return {"ok": True, "project": get(conn, p["id"])}


def take_off_desk(conn, title) -> dict:
    """Close it again: back to one line on the shelf. Nothing about the
    project changes -- only how much of it I am carrying."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    if not p["desk"]:
        return {"ok": False, "why": "'" + p["title"] + "' is not on my desk; "
                "it has only ever been a line on the shelf. Nothing changed."}
    conn.execute("UPDATE projects SET desk = 0 WHERE id = ?", (p["id"],))
    conn.commit()
    return {"ok": True, "project": get(conn, p["id"])}


def _no_such(conn, title) -> str:
    names = [r["title"] for r in conn.execute(
        "SELECT title FROM projects WHERE status = 'open' ORDER BY id")]
    return ("there is no project called '" + _tidy(title) + "'"
            + (" -- the ones on the shelf are " + ", ".join(names) if names
               else " -- the shelf is empty") + ".")


def make_folder(name) -> dict:
    """A folder for a project the assistant is starting, under its own
    projects shelf and nowhere else: `ASSISTANT_PROJECTS`, and only the last
    part of whatever name it gave, so nothing it writes can walk out of that
    folder."""
    leaf = Path(_tidy(name, 200).replace("\\", "/")).name
    if not leaf or leaf in (".", ".."):
        return {"ok": False, "why": "'" + str(name) + "' is not a folder name "
                "I can make. Nothing was made."}
    made = ASSISTANT_PROJECTS / leaf
    there = made.is_dir()
    try:
        made.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "why": "the folder could not be made: " + str(exc)}
    return {"ok": True, "path": str(made), "made": not there}


def _under(child, parent) -> bool:
    """Whether a path sits inside another, on a filesystem that does not care
    about case. Compared as text after both are resolved: the folder need not
    exist yet, which is the whole point -- the question is asked *before*
    anything is made."""
    import os
    from pathlib import Path
    try:
        a = os.path.normcase(str(Path(child).resolve()))
        b = os.path.normcase(str(Path(parent).resolve()))
    except (OSError, ValueError):
        return False
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def set_folder(conn, title, folder) -> dict:
    """Point a project at a folder on the assistant's own word, and make it if
    it is not there -- inside one fence, which the assistant keeps as its own
    stated bound:

    - It may **create** a directory only inside the owner's Documents folder
      (`DOCUMENTS`). A bare name with no separator in it lands under the
      projects shelf, `ASSISTANT_PROJECTS`.
    - It may **point** a project at a path outside Documents -- it already
      exists and it is only naming it -- but it may never bring one into
      being out there. The acceptance case is exactly this: an existing
      folder anywhere sticks; a path that is not there and not under
      Documents is refused.
    - The room's own `data/` folder is refused either way. The store and the
      keys are not a place a project points at, and that refusal is not the
      assistant's to overturn.

    Every refusal says the path in words. Nothing here fails quietly."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}

    raw = _tidy(folder, 500) if folder is not None else ""
    if not raw:
        out = edit_project(conn, p["id"], folder="")
        if out["ok"]:
            out["said"] = ("cleared the folder on '" + p["title"]
                           + "'; the project stands, it just points nowhere now")
        return out

    store = Path(db.DB_PATH).resolve().parent
    bare = ("/" not in raw and "\\" not in raw and ":" not in raw)
    if bare:
        target = ASSISTANT_PROJECTS / raw
    else:
        target = Path(raw)
        if not target.is_absolute():
            return {"ok": False, "why": "'" + raw + "' is neither a plain name "
                    "nor a whole path, so I cannot tell where it is meant to "
                    "be. A bare name goes under " + str(ASSISTANT_PROJECTS)
                    + "; anything else I write out in full. Nothing was set."}

    if _under(target, store):
        return {"ok": False, "why": "'" + str(target) + "' is inside the "
                "room's own data folder (" + str(store) + "). My store and "
                "the keys are not a place a project points at, and that one "
                "is not mine to overturn. Nothing was set."}

    here = target.is_dir()
    made = False
    if not here:
        if not _under(target, DOCUMENTS):
            return {"ok": False, "why": "there is no folder at '" + str(target)
                    + "', and it is outside " + str(DOCUMENTS)
                    + " -- I may point at a path out there that already "
                    "exists, but I may not bring one into being. That is my "
                    "own bound, said to " + home.OWNER_NAME + ". Nothing was "
                    "set: either the path is wrong, or it is for "
                    + home.OWNER_NAME + " to make."}
        try:
            target.mkdir(parents=True, exist_ok=True)
            made = True
        except OSError as exc:
            return {"ok": False, "why": "'" + str(target) + "' could not be "
                    "made: " + str(exc) + ". Nothing was set."}

    out = edit_project(conn, p["id"], folder=str(target))
    if not out["ok"]:
        return out
    out["path"] = str(target)
    out["made"] = made
    out["said"] = ("'" + p["title"] + "' now points at " + str(target)
                   + (" (made just now)" if made else " (already there)"))
    return out


def start_project(conn, title, folder=None, owner="both", desk=True) -> dict:
    """A project of the assistant's own, started from something it noticed.
    A name, a folder if it wants one -- made under its own projects shelf,
    never loose in the owner's Documents -- and it lands on its desk, because
    it would not have started it otherwise."""
    made = None
    if folder:
        out = make_folder(folder)
        if not out["ok"]:
            return out
        made = out
    got = add_project(conn, title, owner, made["path"] if made else None)
    if not got["ok"]:
        return got
    if desk and len(on_desk(conn)) < DESK_LIMIT:
        conn.execute("UPDATE projects SET desk = 1 WHERE id = ?",
                     (got["project"]["id"],))
        conn.commit()
        got["project"] = get(conn, got["project"]["id"])
    got["folder"] = made
    return got


# -- notes ------------------------------------------------------------------

def add_note(conn, project_id, who, text) -> dict:
    """One line in the box, signed. `who` is required and not defaulted: a
    note whose author the room had to guess is exactly the note the
    assistant must not be handed."""
    p = get(conn, project_id)
    if not p:
        return {"ok": False, "why": "there is no project #" + str(project_id)}
    author = _who_ok(who)
    if not author:
        return {"ok": False, "why": "every note line carries who wrote it -- "
                + _or(AUTHORS) + ". I was given " + repr(who) + ". Nothing "
                "was written."}
    body = str(text or "").strip()[:MAX_NOTE]
    if not body:
        return {"ok": False, "why": "an empty note line was not written"}
    cur = conn.execute(
        "INSERT INTO project_notes (project, who, dt, text, was, gone)"
        " VALUES (?, ?, ?, ?, NULL, 0)",
        (p["id"], author, db.now(), body))
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?", (db.now(), p["id"]))
    conn.commit()
    return {"ok": True, "note_id": cur.lastrowid, "who": author}


def edit_note(conn, note_id, who, text) -> dict:
    """A line's author may rewrite it; nobody else may. What it said before is
    kept beside it -- an edit is a change of words, never a change of hands."""
    r = conn.execute("SELECT * FROM project_notes WHERE id = ?",
                     (int(note_id),)).fetchone()
    if not r:
        return {"ok": False, "why": "there is no note #" + str(note_id)}
    author = _who_ok(who)
    if author != r["who"]:
        return {"ok": False, "why": "that line was written by " + r["who"]
                + " and only " + r["who"] + " may rewrite it. A line keeps its "
                "author; that is the whole point of signing them. Nothing "
                "was changed."}
    body = str(text or "").strip()[:MAX_NOTE]
    if not body:
        return {"ok": False, "why": "an edit to nothing is a deletion, and "
                "nothing here is deleted. Hide the line instead."}
    was = []
    try:
        was = json.loads(r["was"] or "[]")
    except (TypeError, ValueError):
        was = []
    was.append({"dt": r["dt"], "text": r["text"]})
    conn.execute("UPDATE project_notes SET text = ?, was = ?, dt = ? WHERE id = ?",
                 (body, json.dumps(was[-20:], ensure_ascii=False), db.now(),
                  r["id"]))
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?",
                 (db.now(), r["project"]))
    conn.commit()
    return {"ok": True, "note_id": r["id"]}


def hide_note(conn, note_id, who, back=False) -> dict:
    """Put a line down, or pick it up again. Not a delete: the words stay on
    disk with their author, exactly like a row that leaves its working set."""
    r = conn.execute("SELECT * FROM project_notes WHERE id = ?",
                     (int(note_id),)).fetchone()
    if not r:
        return {"ok": False, "why": "there is no note #" + str(note_id)}
    author = _who_ok(who)
    if author != r["who"]:
        return {"ok": False, "why": "that line was written by " + r["who"]
                + "; only " + r["who"] + " may put it down. Nothing was changed."}
    conn.execute("UPDATE project_notes SET gone = ? WHERE id = ?",
                 (0 if back else 1, r["id"]))
    conn.commit()
    return {"ok": True, "note_id": r["id"], "gone": not back}


def notes_of(conn, project_id, include_gone=False, limit=MAX_NOTES_BACK) -> list:
    rows = conn.execute(
        "SELECT * FROM project_notes WHERE project = ? ORDER BY id",
        (int(project_id),)).fetchall()
    out = []
    for r in rows:
        if r["gone"] and not include_gone:
            continue
        line = {"id": r["id"], "who": r["who"], "dt": r["dt"], "text": r["text"]}
        if r["gone"]:
            line["put_down"] = True
        if r["was"]:
            try:
                line["edited"] = len(json.loads(r["was"]))
            except (TypeError, ValueError):
                pass
        out.append(line)
    return out[-limit:]


def notes_head(conn, project_id) -> dict:
    """What its block carries about the box: how many lines, whose they are,
    and who wrote last. Never a word of the text -- that is the reach."""
    lines = notes_of(conn, project_id)
    by = {}
    for line in lines:
        by[line["who"]] = by.get(line["who"], 0) + 1
    head = {"lines": len(lines), "by": by}
    if lines:
        head["last"] = {"who": lines[-1]["who"], "dt": lines[-1]["dt"]}
    return head


# -- tasks ------------------------------------------------------------------

def add_task(conn, project_id, who, title, wants=None, repeats=False,
             schedule=None, job=None, needs=None) -> dict:
    p = get(conn, project_id)
    if not p:
        return {"ok": False, "why": "there is no project #" + str(project_id)}
    asker = _who_ok(who)
    if not asker:
        return {"ok": False, "why": "a task carries who asked for it -- "
                + _or(AUTHORS) + ". I was given " + repr(who)
                + ". Nothing was added."}
    name = _tidy(title)
    if not name:
        return {"ok": False, "why": "a task needs a title -- nothing was added"}
    sched = _tidy(schedule, 200) or None
    if sched and not repeats:
        repeats = True
    waits, why = _needs_ok(needs)
    if why:
        return {"ok": False, "why": why + " Nothing was added."}
    cur = conn.execute(
        "INSERT INTO project_tasks (project, title, wants, state, who, repeats,"
        " schedule, job, needs, created, moved)"
        " VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
        (p["id"], name, (str(wants or "").strip() or None), asker,
         1 if repeats else 0, sched, _tidy(job) or None, waits,
         db.now(), db.now()))
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?", (db.now(), p["id"]))
    conn.commit()
    return {"ok": True, "task": task(conn, cur.lastrowid)}


def task(conn, task_id):
    r = conn.execute("SELECT * FROM project_tasks WHERE id = ?",
                     (int(task_id),)).fetchone()
    return dict(r) if r else None


def set_task(conn, task_id, title=None, wants=None, state=None, repeats=None,
             schedule=None, job=None, needs=None, sense=None, sense_item=None,
             at=None, known_senses=None) -> dict:
    """Change one task. Every argument left at None is left alone.

    `at` is a real date and really fires: the calendar sense reads it, and it
    opened in the same page that sense did, never a page earlier. A task that
    only carries schedule *words* still fires nothing, and still says so.

    `known_senses` is the room's list of its own senses, handed in rather
    than imported -- the watcher reads tasks, and reaching the other way from
    here would close that circle."""
    t = task(conn, task_id)
    if not t:
        return {"ok": False, "why": "there is no task #" + str(task_id)}
    sets, vals, said = [], [], []
    if title is not None:
        name = _tidy(title)
        if not name:
            return {"ok": False, "why": "a task cannot be renamed to nothing"}
        sets.append("title = ?"); vals.append(name)
        said.append("renamed to '" + name + "'")
    if wants is not None:
        sets.append("wants = ?"); vals.append(str(wants).strip() or None)
        said.append("what it wants rewritten")
    if state is not None:
        st = str(state or "").strip().lower()
        if st not in STATES:
            return {"ok": False, "why": "a task is " + ", ".join(STATES)
                    + "; I was given " + repr(state) + ". Nothing moved."}
        sets.append("state = ?"); vals.append(st)
        said.append(t["state"] + " -> " + st)
    if repeats is not None:
        sets.append("repeats = ?"); vals.append(1 if repeats else 0)
        said.append("repeating" if repeats else "one-time")
    if schedule is not None:
        sched = _tidy(schedule, 200) or None
        sets.append("schedule = ?"); vals.append(sched)
        # What the clock made of the new words, said back at whoever set
        # them. A schedule accepted in silence and never fired is the exact
        # failure this line exists to prevent.
        said.append("schedule " + (sched or "cleared") + " ("
                    + (_clock_line({"repeats": 1, "schedule": sched})
                       or NO_CLOCK) + ")")
    if job is not None:
        link = _tidy(job) or None
        sets.append("job = ?"); vals.append(link)
        said.append("job " + (link or "unlinked"))
    if needs is not None:
        waits, why = _needs_ok(needs)
        if why:
            return {"ok": False, "why": why + " Nothing changed."}
        sets.append("needs = ?"); vals.append(waits)
        said.append("waiting on " + (waits or "nobody"))
    if sense is not None:
        name = (_tidy(sense, 60) or "").lower() or None
        if name and known_senses is None:
            return {"ok": False, "why": "I was not handed the room's list of "
                    "senses, so I cannot tell whether '" + name + "' is one. "
                    "Nothing was bound."}
        if name and name not in known_senses:
            return {"ok": False, "why": "there is no sense called '" + name
                    + "' -- the ones there are: " + ", ".join(known_senses)
                    + ". Nothing was bound."}
        sets.append("sense = ?"); vals.append(name)
        said.append("bound to the sense " + name if name else "sense unbound")
    if sense_item is not None:
        which = _tidy(sense_item, 120) or None
        sets.append("sense_item = ?"); vals.append(which)
        said.append("watching " + which if which else "item cleared")
    if at is not None:
        when, why = _at_ok(at)
        if why:
            return {"ok": False, "why": why + " Nothing changed."}
        sets.append("at = ?"); vals.append(when)
        said.append(("due " + when) if when else "date cleared")
    if not sets:
        return {"ok": False, "why": "an edit with nothing edited"}
    sets.append("moved = ?"); vals.append(db.now())
    vals.append(t["id"])
    conn.execute("UPDATE project_tasks SET " + ", ".join(sets) + " WHERE id = ?",
                 vals)
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?",
                 (db.now(), t["project"]))
    conn.commit()
    return {"ok": True, "task": task(conn, t["id"]), "said": ", ".join(said)}


def tasks_of(conn, project_id, live_only=False) -> list:
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE project = ? ORDER BY id",
        (int(project_id),)).fetchall()
    out = []
    for r in rows:
        if live_only and r["state"] not in LIVE_STATES:
            continue
        out.append(dict(r))
    return out


def task_by_title(conn, project_id, title):
    """One task on a project by name, case-blind -- the same manners as
    `by_title` above, because a hand's op hands its tasks by name too, never
    an id it was never given."""
    want = _tidy(title).lower()
    if not want:
        return None
    for r in conn.execute(
            "SELECT * FROM project_tasks WHERE project = ? ORDER BY id",
            (int(project_id),)):
        if _tidy(r["title"]).lower() == want:
            return dict(r)
    return None


def _no_such_task(conn, project_id, title) -> str:
    names = [r["title"] for r in conn.execute(
        "SELECT title FROM project_tasks WHERE project = ? ORDER BY id",
        (int(project_id),))]
    return ("there is no task called '" + _tidy(title) + "' on this project"
            + (" -- its tasks are " + ", ".join(names) if names
               else " -- it has no tasks on record") + ".")


# -- the job a task points at ----------------------------------------------

def _jobs_by_title() -> dict:
    """Every job it has, open or closed, by lowered title. Read-only: a task
    naming a job it does not have is a fact about the task, not a reason to
    write anything."""
    try:
        return {j["title"].lower(): j for j in jobs.status()["jobs"]}
    except Exception:
        return {}


def job_card(job_title, index=None):
    """The job a task links to, as the card the GUI draws. `None` when the
    task links to nothing; a `missing` card when it names a job that is not
    there, because a broken link said out loud beats a link drawn as if it
    worked."""
    name = _tidy(job_title)
    if not name:
        return None
    index = _jobs_by_title() if index is None else index
    j = index.get(name.lower())
    if not j:
        return {"title": name, "missing": True}
    return {
        "title": j["title"], "goal": j.get("goal"), "status": j["status"],
        "state": jobs.state_of(j), "spent_usd": round(j.get("spent_usd") or 0, 2),
        "ceiling_usd": j.get("ceiling_usd"), "links_left": j.get("links_left"),
        "links": j.get("links"), "decided": j.get("decided"),
        "outcome": j.get("outcome"),
    }


# -- the assistant's hand on a task, by name -- the four ops a hand calls ---
#
# The four verbs the harness gives the assistant: add one, edit one, put one
# down, point one at a job. Every one takes the project and the task by
# title, the same way `put_on_desk` and `set_folder` already do, and every
# one refuses in words -- naming what does exist -- when the project or the
# task is not there. A task added this way always carries the assistant's
# own kind (`home.SELF`) as `asked_by`; nothing here
# ever touches the `who` of a task it did not create, because relabelling
# who asked is the one failure that matters more than any of the rest.

def add_task_by_title(conn, title, who, task_title, wants=None, repeats=False,
                      schedule=None, state=None, needs=None) -> dict:
    """`task_add`: a task under a named project, landed in whatever state the
    assistant said (`open` if it said nothing). `who` is the harness's to fix,
    not the assistant's to type -- it calls this with `home.SELF` and nothing
    else ever will."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    if state is not None and str(state).strip().lower() not in TASK_STATES:
        return {"ok": False, "why": "a task is added open, doing or done; I "
                "was given " + repr(state) + ". Nothing was added."}
    out = add_task(conn, p["id"], who, task_title, wants, repeats, schedule,
                   needs=needs)
    if not out["ok"]:
        return out
    st = str(state or "").strip().lower()
    if st and st != "open":
        moved = set_task(conn, out["task"]["id"], state=st)
        if moved["ok"]:
            out["task"] = moved["task"]
    out["project"] = p
    return out


def edit_task_by_title(conn, title, task_title, new_title=None, wants=None,
                       state=None, schedule=None, needs=None, sense=None,
                       sense_item=None, at=None, known_senses=None) -> dict:
    """`task_edit`: title, what it wants, its state or its schedule, on a
    task found by its current name. Never `who` -- there is no argument for
    it here, so an edit cannot relabel who asked even by accident. A
    schedule handed to a one-time task makes it repeating, the same
    convenience `add_task` already gives -- a schedule that does not turn
    the flag on is a schedule it would set and never see again."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    t = task_by_title(conn, p["id"], task_title)
    if not t:
        return {"ok": False, "why": _no_such_task(conn, p["id"], task_title)}
    if state is not None and str(state).strip().lower() not in TASK_STATES:
        return {"ok": False, "why": "a task is edited to open, doing or done "
                "here -- 'dropped' is task_remove's word, not this one. I was "
                "given " + repr(state) + ". Nothing changed."}
    repeats = None
    if schedule is not None and _tidy(schedule, 200) and not t["repeats"]:
        repeats = True
    out = set_task(conn, t["id"], title=new_title, wants=wants, state=state,
                   schedule=schedule, repeats=repeats, needs=needs,
                   sense=sense, sense_item=sense_item, at=at,
                   known_senses=known_senses)
    if out["ok"]:
        out["project"] = p
    return out


def remove_task_by_title(conn, title, task_title) -> dict:
    """`task_remove`: put a task down. Nothing here is deleted -- the row
    stands on disk, `state` moves to `dropped`, which is already how a
    finished task leaves its block, and it is the one state neither
    `task_add` nor `task_edit` will ever set."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    t = task_by_title(conn, p["id"], task_title)
    if not t:
        return {"ok": False, "why": _no_such_task(conn, p["id"], task_title)}
    if t["state"] == "dropped":
        return {"ok": False, "why": "'" + t["title"] + "' on '" + p["title"]
                + "' is already removed. Nothing changed."}
    out = set_task(conn, t["id"], state="dropped")
    if out["ok"]:
        out["project"] = p
    return out


def link_task_by_title(conn, title, task_title, job_title) -> dict:
    """`task_link`: point a task at one of its jobs by title. The job need
    not exist -- a broken link is said on the task, not refused, the same
    rule `job_card` already keeps -- but the project and the task must."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    t = task_by_title(conn, p["id"], task_title)
    if not t:
        return {"ok": False, "why": _no_such_task(conn, p["id"], task_title)}
    name = _tidy(job_title)
    if not name:
        return {"ok": False, "why": "a link needs a job title -- nothing "
                "was linked"}
    out = set_task(conn, t["id"], job=name)
    if out["ok"]:
        out["project"] = p
        out["job_card"] = job_card(name)
    return out


# -- where a project lives --------------------------------------------------

def resources_of(conn, project_id, include_gone=False) -> list:
    """A project's key:value rows, oldest first. Put-down ones stay on disk
    and stay out of this unless they are asked for by name."""
    q = "SELECT * FROM project_resources WHERE project = ?"
    if not include_gone:
        q += " AND gone = 0"
    return [dict(r) for r in
            conn.execute(q + " ORDER BY id", (int(project_id),))]


def resource_set(conn, project_id, who, key, value) -> dict:
    """One key:value on a project, stamped with whoever wrote it.

    `who` is the room's to fix, exactly as `asked_by` is on a task, so there
    is no argument here that could put the assistant's words under a
    person's name. Setting a key that is already there does not
    overwrite it -- the old line is put down and a new one written, so what
    a resource used to say is still on disk. Nothing here is deleted."""
    p = get(conn, project_id)
    if not p:
        return {"ok": False, "why": "there is no project #" + str(project_id)}
    author = _who_ok(who)
    if not author:
        return {"ok": False, "why": "a resource carries who wrote it -- "
                + _or(AUTHORS) + ". I was given " + repr(who) + ". Nothing was "
                "written."}
    k = _tidy(key, MAX_KEY)
    v = _tidy(value, MAX_RESOURCE)
    if not k:
        return {"ok": False, "why": "a resource needs a name, like 'github' or"
                " 'steam workshop' -- nothing was written"}
    if not v:
        return {"ok": False, "why": "a resource needs something to say; to "
                "take one off, that is resource_clear -- nothing was written"}
    old = conn.execute(
        "SELECT id, value FROM project_resources WHERE project = ? AND gone = 0"
        " AND lower(key) = lower(?)", (p["id"], k)).fetchone()
    if old:
        conn.execute("UPDATE project_resources SET gone = 1 WHERE id = ?",
                     (old["id"],))
    cur = conn.execute(
        "INSERT INTO project_resources (project, key, value, who, dt, gone)"
        " VALUES (?, ?, ?, ?, ?, 0)", (p["id"], k, v, author, db.now()))
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?",
                 (db.now(), p["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM project_resources WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return {"ok": True, "resource": dict(row), "project": dict(p),
            "said": ("'" + k + "' rewritten; what it said before is kept"
                     if old else "'" + k + "' set")}


def resource_clear(conn, project_id, key) -> dict:
    """Put a resource down. The row stands on disk with `gone` set, the same
    way a note line does -- and a name that is not there is refused in words
    naming the ones that are."""
    p = get(conn, project_id)
    if not p:
        return {"ok": False, "why": "there is no project #" + str(project_id)}
    k = _tidy(key, MAX_KEY)
    row = conn.execute(
        "SELECT * FROM project_resources WHERE project = ? AND gone = 0"
        " AND lower(key) = lower(?)", (p["id"], k)).fetchone()
    if not row:
        have = [r["key"] for r in resources_of(conn, p["id"])]
        return {"ok": False, "why": ("'" + p["title"] + "' has no resource "
                "called '" + k + "'"
                + (" -- it has " + ", ".join(have) if have
                   else " -- it has none at all") + ". Nothing changed.")}
    conn.execute("UPDATE project_resources SET gone = 1 WHERE id = ?",
                 (row["id"],))
    conn.execute("UPDATE projects SET moved = ? WHERE id = ?",
                 (db.now(), p["id"]))
    conn.commit()
    return {"ok": True, "project": dict(p),
            "said": "'" + row["key"] + "' put down; the line is still on disk"}


def resource_set_by_title(conn, title, who, key, value) -> dict:
    """`resource_set`: its op, on a project named rather than numbered."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    return resource_set(conn, p["id"], who, key, value)


def resource_clear_by_title(conn, title, key) -> dict:
    """`resource_clear`: its op, by name."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    return resource_clear(conn, p["id"], key)


# -- what the assistant is handed, and what it may ask for -----------------

# --- notices: what arrived on a task, waiting for somebody to say seen -------
#
# A notice is the plain fact that something arrived on a task: a comment one
# of the senses detected, a date that came, a line the assistant or a person
# put there. It stands until somebody -- a person or the assistant -- checks
# it off, with a note if they want one. Nothing here decides whether the
# thing was dealt with; the check IS the word that it was. It is general on
# purpose -- a project can be any kind of work -- so there is no rule about
# one kind of item, and no code that reads a page back to decide.
#
# The people's tab counts open notices on its own button and lists them
# under "waiting on you"; the assistant's block carries them on a desk task
# and as a count on the shelf, so it can check one off itself or say that it
# is there.
#
# An earlier shape read the watched pages back to work out which comments
# had no reply yet -- and a comment just answered stayed "waiting" until the
# next look, hours later. So: pop when it arrives, clear when somebody says
# so.

CHECKERS = home.HOUSEHOLD + (home.SELF,)
NOTICE_MAX = 300


def look_for(t, looks):
    """The watcher's look for THIS task's page -- by its sense as well as
    its item, never by item alone.

    `watch.looks()` is keyed by item id and, as a convenience, by label, and
    a label is claimed by the first sense to carry it. Two senses can watch
    items that share one label, and tasks are bound by that label, so a task
    of the second sense looking the label up got the first sense's look: the
    same "nothing new" either way, which is why the mix-up hid -- until the
    look started carrying the page's waiting list, and the second sense's
    comments were nowhere.

    A look under the item as written that belongs to another sense is not
    this task's; the one that is has the task's sense and either this item
    id or this label. A look that names no sense at all cannot contradict
    the task and is taken as it stands. None when there is none -- "no
    look" is a fact the page draws as such."""
    t = dict(t or {})
    item, sense = t.get("sense_item"), t.get("sense")
    if not item or not looks:
        return None
    one = looks.get(item)
    if one and (not sense or not one.get("sense")
                or one.get("sense") == sense):
        return one
    for cand in looks.values():
        if (cand.get("sense") == sense
                and (str(cand.get("item")) == str(item)
                     or cand.get("label") == item)):
            return cand
    return None if sense else one


def notices_of(conn, task_id, open_only=True, limit=50) -> list:
    """The notices on one task, newest first: the open ones by default, or
    -- `open_only` false -- the checked ones as well, each wearing who
    checked it, when, and their note if they left one."""
    try:
        rows = conn.execute(
            "SELECT * FROM task_notices WHERE task = ?"
            + (" AND done = 0" if open_only else "")
            + " ORDER BY id DESC LIMIT ?", (task_id, int(limit))).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        one = {"id": r["id"], "task": r["task"], "at": r["dt"],
               "said": r["said"], "source": r["source"],
               "done": bool(r["done"])}
        if r["meta"]:
            try:
                one["meta"] = json.loads(r["meta"])
            except ValueError:
                pass
        if r["done"]:
            one["done_by"] = r["done_by"]
            one["done_at"] = r["done_dt"]
            if r["note"]:
                one["note"] = r["note"]
        out.append(one)
    return out


def add_notice(conn, task_id, said, source, meta=None) -> dict:
    """One thing arrived on a task. `source` is the sense that saw it, or
    who put it there. A task that is not live takes no notice: a finished
    or dropped task is not waiting on anybody."""
    t = task(conn, task_id)
    if not t:
        return {"ok": False, "why": "no task " + str(task_id)}
    if t["state"] not in LIVE_STATES:
        return {"ok": False, "why": "'" + t["title"] + "' is " + t["state"]
                + ", so nothing on it waits on anybody"}
    text = _tidy(said, NOTICE_MAX)
    if not text:
        return {"ok": False, "why": "a notice has to say what arrived"}
    cur = conn.execute(
        "INSERT INTO task_notices (task, dt, said, source, meta) "
        "VALUES (?, ?, ?, ?, ?)",
        (t["id"], db.now(), text, str(source or "") or None,
         json.dumps(meta) if meta else None))
    conn.commit()
    return {"ok": True, "notice": cur.lastrowid, "task": dict(t)}


def notice_from_event(conn, event) -> list:
    """The watcher's door: what a sense found lands on the tasks that watch
    for it. A calendar waking names its task outright (`meta.task`); a
    comment waking names the sense's item (`meta.item`, `meta.label`) and
    lands on every live task bound to that sense and that item -- by the
    watcher's id or by label, since a task may say either. A waking
    nothing is bound to leaves no notice: it still wakes it, as it always
    did. Never raises into a look; the ids made, for the bench."""
    try:
        source = str(event.get("source") or "")
        meta = event.get("meta") or {}
        said = str(event.get("said") or "")
        made = []
        if meta.get("task") is not None:
            out = add_notice(conn, meta["task"], said, source, meta)
            if out.get("ok"):
                made.append(out["notice"])
            return made
        item, label = meta.get("item"), meta.get("label")
        if item is None and label is None:
            return made
        rows = conn.execute(
            "SELECT * FROM project_tasks WHERE sense = ? AND state IN (?, ?)",
            (source, *LIVE_STATES)).fetchall()
        for r in rows:
            bound = str(r["sense_item"] or "")
            if bound and (bound == str(item) or bound == str(label)):
                out = add_notice(conn, r["id"], said, source, meta)
                if out.get("ok"):
                    made.append(out["notice"])
        return made
    except Exception:
        return []


def check_notice(conn, notice_id, who, note=None, back=False) -> dict:
    """Somebody says a notice has been seen to -- or, with `back`, that it
    has not after all. `who` is the room's stamp, one of `CHECKERS`. The
    note is optional and kept as written: a note that a thing will be left
    unanswered is a decision, and it is worth more on the row than in a
    chat line the next line buries. A second check on a checked notice keeps
    the first word and adds the new note, if any."""
    if who not in CHECKERS:
        return {"ok": False, "why": "a notice is checked off by "
                + ", ".join(CHECKERS) + "; '" + str(who) + "' cannot"}
    try:
        nid = int(notice_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "which notice? none was named"}
    r = conn.execute("SELECT * FROM task_notices WHERE id = ?",
                     (nid,)).fetchone()
    if not r:
        return {"ok": False, "why": "there is no notice " + str(nid)}
    t = task(conn, r["task"])
    t = dict(t) if t else None
    if back:
        if not r["done"]:
            return {"ok": False, "why": "notice " + str(nid)
                    + " is not checked off"}
        conn.execute("UPDATE task_notices SET done = 0, done_by = NULL, "
                     "done_dt = NULL WHERE id = ?", (nid,))
        conn.commit()
        return {"ok": True, "notice": nid, "back": True, "said": r["said"],
                "task": t}
    text = (_tidy(note, NOTICE_MAX) if note and str(note).strip() else None)
    if r["done"]:
        if text:
            conn.execute("UPDATE task_notices SET note = ? WHERE id = ?",
                         (text, nid))
            conn.commit()
        return {"ok": True, "notice": nid, "already": r["done_by"],
                "said": r["said"], "task": t}
    conn.execute("UPDATE task_notices SET done = 1, done_by = ?, done_dt = ?, "
                 "note = ? WHERE id = ?", (who, db.now(), text, nid))
    conn.commit()
    return {"ok": True, "notice": nid, "said": r["said"], "task": t}


def raise_notice_by_title(conn, title, task_title, said, who) -> dict:
    """The assistant's op, by the project's and the task's names: put a
    thing on a task for somebody to check off. `who` is the room's stamp and goes on the
    row as its source. Refused in words naming what does exist."""
    p = by_title(conn, title)
    if not p:
        return {"ok": False, "why": _no_such(conn, title)}
    t = task_by_title(conn, p["id"], task_title)
    if not t:
        return {"ok": False, "why": _no_such_task(conn, p["id"], task_title)}
    out = add_notice(conn, t["id"], said, who)
    if out["ok"]:
        out["project"] = dict(p)
    return out


def _clock_line(t) -> str:
    """What actually happens to this task's schedule, on its own face.

    This used to be one constant saying nothing runs it, and that was honest
    while nothing did. The clock runs it now, so this asks the clock instead
    of asserting: a schedule it can read says when it next comes round, and
    one it cannot says exactly why and that nothing fires it. Neither is a
    promise -- both are read off the same parser the firing uses, so the face
    of the task and the thing that wakes the assistant cannot drift."""
    if not t["repeats"]:
        return None
    words = (t["schedule"] or "").strip()
    if not words:
        return NO_CLOCK
    spec = clock.read(words)
    if not spec["ok"]:
        return "nothing fires this — " + spec["why"]
    return "on my clock: next " + clock.next_in_words(spec)


def _task_line(conn, t, index, senses=None, looks=None) -> dict:
    """One open task, as it reaches the assistant: what it is, what it wants,
    who asked for it, and -- if it repeats -- the schedule in their words
    with `clock` saying on its face what fires it.

    Four things beyond that and no more: the trigger as one phrase, `needs`
    when set, `at` when set, and the last look for a sense task. Every one of
    them absent when there is nothing to say -- a field spelled out as null
    costs prompt space for nothing. The raw
    `sense` and `sense_item` columns are deliberately not here: the phrase
    already carries both, and two spellings of one fact is how a block gets
    long enough to stop being read."""
    entry = {
        "title": t["title"],
        "state": t["state"],
        "asked_by": t["who"],
        "repeats": bool(t["repeats"]),
    }
    tr = trigger(t, senses)
    # `by hand` is the absence of a trigger rather than one of them, and it
    # is the common case; `repeats: false` above already says it.
    if tr["kind"] != "hand":
        entry["trigger"] = tr["phrase"]
    if dict(t).get("needs"):
        entry["needs"] = dict(t)["needs"]
    if dict(t).get("at"):
        entry["at"] = dict(t)["at"]
    if tr["kind"] == "sense":
        look = look_for(t, looks)
        if look and look.get("said"):
            entry["last_look"] = look["said"]
    # What has arrived on this task and nobody has checked off yet -- the
    # same list their tab draws under "waiting on you". The first five with
    # their ids, so it can check one off; the rest as a count. Absent when
    # there is nothing.
    open_n = notices_of(conn, t["id"])
    if open_n:
        entry["notices"] = [{"id": n["id"], "at": n["at"], "said": n["said"]}
                            for n in open_n[:5]]
        if len(open_n) > 5:
            entry["more_notices"] = len(open_n) - 5
    if t["wants"]:
        entry["wants"] = t["wants"]
    if t["repeats"]:
        if t["schedule"]:
            entry["schedule"] = t["schedule"]
        entry["clock"] = _clock_line(t)
    if t["job"]:
        entry["job"] = t["job"]
        card = job_card(t["job"], index)
        if card and card.get("missing"):
            entry["job_missing"] = ("this task names the job '" + t["job"]
                                    + "', which I do not have; the link is "
                                    "broken, not the task")
    return entry


def for_prompt(conn, senses=None, looks=None) -> dict:
    """The assistant's block.

    Two parts, and the split is the whole point. `shelf` is **one line per
    project** -- title, owner, folder or none, and how many tasks are open --
    however many projects there are, because at a hundred of them a block of
    every task would put a hundred pages of somebody else's to-do list into
    every turn. `on_my_desk` is the few it has explicitly opened: those, and
    only those, carry their task lines and their note count.

    Never the notes themselves, on either side. Never raises into a turn."""
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = 'open' ORDER BY id").fetchall()
    except Exception:
        return {"shelf": [], "on_my_desk": [], "how": HOW}
    index = _jobs_by_title()
    shelf, desks = [], []
    for p in rows:
        live = tasks_of(conn, p["id"], live_only=True)
        line = {
            "title": p["title"],
            "owner": p["owner"],
            "folder": p["folder"],
            "open_tasks": len(live),
        }
        # Something arrived on one of its tasks and nobody has checked it
        # off: a count on the shelf line, so a project not on its desk can
        # still say it is waiting on somebody. Absent when zero.
        waiting = sum(len(notices_of(conn, t["id"])) for t in live)
        if waiting:
            line["notices"] = waiting
        if p["desk"]:
            line["on_my_desk"] = True
        shelf.append(line)
        if not p["desk"]:
            continue
        entry = {
            "title": p["title"],
            "owner": p["owner"],
            "folder": p["folder"],
            "notes": notes_head(conn, p["id"]),
            "open_tasks": [_task_line(conn, t, index, senses, looks)
                           for t in live],
        }
        quiet = _quiet_days(p["moved"])
        if quiet >= 7:
            entry["quiet"] = ("nothing has moved on this project for "
                              + str(quiet) + " days")
        desks.append(entry)
    return {"shelf": shelf, "on_my_desk": desks, "how": HOW}


def read_notes(conn, title) -> dict:
    """The reach: one project's box, whole, every line with its author. What
    comes back when it asks -- and a refusal in words when the name is not
    one of theirs, with the names that are."""
    p = by_title(conn, title)
    if not p:
        names = [r["title"] for r in conn.execute(
            "SELECT title FROM projects WHERE status = 'open' ORDER BY id")]
        return {"asked": _tidy(title), "found": False,
                "why": ("there is no project called '" + _tidy(title) + "'"
                        + (" -- the open ones are " + ", ".join(names) if names
                           else " -- there are no projects yet") + ".")}
    lines = notes_of(conn, p["id"])
    return {
        "asked": p["title"], "found": True, "owner": p["owner"],
        "folder": p["folder"], "lines": lines,
        "note": ("every line carries who wrote it; nothing here is mine unless "
                 "it says " + home.SELF),
    }


def read_activity(conn, title, task_title, senses=None, looks=None,
                  ledger=None) -> dict:
    """The reach: what one task's sense has been doing, one task at a time.

    This is a fetch rather than a field so that on the many turns nobody
    needs it, it costs nothing. A refusal names
    what does exist, the same way `read_notes` does -- and a task nothing
    watches says so plainly rather than coming back with an empty list that
    reads like a sense which has never fired."""
    p = by_title(conn, title)
    if not p:
        return {"asked": _tidy(title), "found": False,
                "why": _no_such(conn, title)}
    t = task_by_title(conn, p["id"], task_title)
    if not t:
        return {"asked": _tidy(task_title), "found": False,
                "why": _no_such_task(conn, p["id"], task_title)}
    tr = trigger(t, senses)
    act = activity_for(t, looks, ledger)
    if not act:
        return {"asked": t["title"], "project": p["title"], "found": True,
                "watched": False, "trigger": tr["phrase"],
                "why": ("'" + t["title"] + "' is not bound to a sense, so "
                        "nothing looks at it on its own and there is no "
                        "activity to read. Its trigger is: " + tr["phrase"])}
    return {"asked": t["title"], "project": p["title"], "found": True,
            "watched": True, "trigger": tr["phrase"], **act,
            "note": ("last_look is overwritten every look and is usually "
                     "quiet; the lines are the notable ones the ledger kept")}


# The trigger taxonomy: what actually makes a task happen, in the one short
# phrase the page and its turn block both wear. Derived from what is stored,
# never migrated -- a task grows a trigger by growing a field.
#
# `NO_CLOCK`'s honesty rides on the face of it. A task whose only trigger is
# the words its asker wrote says *in words* before it says the words, because
# a chip reading "every first Sunday" on a shut row is a clock to anyone who
# reads it and there is no clock behind it. When a real cadence arrives it
# arrives in the watcher's own figures, handed in, never worked out here.
TRIGGERS = ("hand", "words", "sense", "date", "event")


def trigger(t, senses=None) -> dict:
    """What fires this task, as `{kind, said, phrase}` and whatever else that
    kind carries. `senses` is the watcher's word about each sense, passed in.

    Two forms of the same fact, because there are two readers. `said` is the
    chip the page draws, with the item as a chip of its own beside it.
    `phrase` is the whole thing in one sentence, for the assistant's block --
    it never sees a chip, and the sense means little without its item."""
    t = dict(t)
    sense = t.get("sense")
    if sense:
        # The cadence is the watcher's to state. Until it hands one over the
        # chip names the sense and claims no clock at all.
        cadence = ((senses or {}).get(sense) or {}).get("said")
        item = t.get("sense_item")
        return {"kind": "sense", "sense": sense,
                "said": cadence or ("sense: " + str(sense)),
                "item": item,
                "phrase": (str(sense) + (" on " + str(item) if item else "")
                           + (", " + cadence if cadence else ""))}
    if t.get("at"):
        # The page reformats this for a human; its block gets it whole,
        # offset and all, because a time without one is a time read two ways.
        return {"kind": "date", "said": "on " + str(t["at"]), "at": t["at"],
                "phrase": "on " + str(t["at"])}
    if t.get("repeats"):
        sched = _tidy(t.get("schedule") or "", 80)
        said = ("in words: " + sched) if sched else "in words"
        return {"kind": "words", "said": said, "phrase": said}
    return {"kind": "hand", "said": "by hand", "phrase": "by hand"}


def activity_for(t, looks=None, ledger=None, limit=12) -> dict:
    """What a sense-bound task has actually been doing, in plain sentences.

    Two halves that mean different things and are kept apart. The **last
    look** is one value overwritten every time the sense looks, and is
    usually "nothing new" -- that is the half that answers "is this thing
    even running". The **lines** are the notable events the ledger keeps.
    `page` is where a person goes to see the thing the sense watches -- the
    item's own page -- linked beside a notice.

    A task bound to no sense has no activity at all -- None, not an empty
    list. A to-do item has never done anything on its own and a page that
    drew it an empty log would be inventing a machine for it.

    On matching a ledger line to an item: newer lines carry the item in
    their `meta` and are matched on it. Older ones can only be matched on
    their words, and one that does not name the item is left out rather than
    shown -- attributing one item's waking to another task is the same
    failure as quoting one person's words to the other."""
    sense = (t or {}).get("sense")
    if not sense:
        return None
    item = (t or {}).get("sense_item")
    look = look_for(t, looks)
    lines = []
    for e in reversed(list(ledger or [])):
        if (e.get("source") or "") != sense:
            continue
        said, out = e.get("said") or "", e.get("outcome") or ""
        if item:
            meta = e.get("meta") or {}
            if meta:
                if (str(meta.get("item")) != str(item)
                        and str(meta.get("label")) != str(item)):
                    continue
            elif str(item) not in said and str(item) not in out:
                continue
        # The ledger carries what happened and what was done about it. A
        # waking is best read as the fact; anything else is best read as the
        # decision, which is where the reason lives.
        text = said if (said and out.startswith("woke")) else (out or said)
        if not text:
            continue
        lines.append({"at": e.get("at"), "said": text})
        if len(lines) >= int(limit):
            break
    return {"sense": sense, "item": item,
            "last_look": (look or {}).get("at"),
            "last_result": (look or {}).get("said"),
            "page": (look or {}).get("page"),
            "lines": lines}


def _folder_here(folder):
    """Whether the path on a project is a folder on this machine right now.

    `None` when there is no path at all. It is a remark and never a rule:
    what a person typed is stored exactly as typed, unchecked, because a
    project can point at a folder on another machine, on a drive that is
    not plugged in, or at one they are about to make. The page says *not found*
    quietly beside it rather than refusing to keep the words."""
    if not folder:
        return None
    try:
        from pathlib import Path
        return Path(folder).is_dir()
    except (OSError, ValueError):
        return False


def overview(conn, include_closed=True, senses=None, looks=None,
             ledger=None) -> dict:
    """Everything, for the GUI: projects, their notes and their tasks, with
    each linked job's card beside the task that named it.

    `senses` is the watcher's word about its own senses, handed in rather
    than fetched: what a sense costs to look at is the watcher's fact, and
    reaching for it from here would be a circle the day one reads tasks."""
    index = _jobs_by_title()
    out = []
    for p in conn.execute("SELECT * FROM projects ORDER BY id").fetchall():
        if not include_closed and p["status"] != "open":
            continue
        tasks = []
        for t in tasks_of(conn, p["id"]):
            t = dict(t)
            t["repeats"] = bool(t["repeats"])
            t["clock"] = _clock_line(t)
            t["trigger"] = trigger(t, senses)
            t["activity"] = activity_for(t, looks, ledger)
            # What arrived here and waits, and the last few checked off --
            # each of those with who said so, when, and their note.
            t["notices"] = notices_of(conn, t["id"])
            t["checked"] = [n for n in notices_of(conn, t["id"],
                                                  open_only=False, limit=12)
                            if n["done"]]
            t["job_card"] = job_card(t["job"], index) if t["job"] else None
            tasks.append(t)
        out.append({**dict(p), "notes": notes_of(conn, p["id"]),
                    "tasks": tasks, "folder_here": _folder_here(p["folder"]),
                    "resources": resources_of(conn, p["id"])})
    return {"projects": out, "owners": list(OWNERS), "states": list(STATES),
            "no_clock": NO_CLOCK, "needs": list(NEEDS),
            "jobs": sorted(j["title"] for j in index.values()
                           if j["status"] == "open")}


def main():
    conn = db.connect()
    try:
        view = overview(conn)
    finally:
        conn.close()
    print("projects: " + str(len(view["projects"])))
    for p in view["projects"]:
        print("  " + p["title"] + "  " + p["owner"] + "  " + p["status"]
              + "  " + str(len(p["notes"])) + " note lines, "
              + str(len(p["tasks"])) + " tasks")
        for t in p["tasks"]:
            print("    - " + t["title"] + "  " + t["state"] + "  asked by "
                  + t["who"] + ("  [repeats: " + NO_CLOCK + "]"
                                if t["repeats"] else ""))


if __name__ == "__main__":
    main()
