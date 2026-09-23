"""The dreamer: the assistant, at night, with nobody talking to it.

When the room has been quiet long enough after three in the morning, the
assistant comes up on its own with its whole Spark, reads the day's rows,
writes down what mattered while it is warm, tidies its shelf -- names the
untitled, folds the doubles, catches two essences that contradict each other
-- and then wanders, looking for the link between two days it never
connected. That connection is the reason for the whole thing; everything
else is filing.

## The door

The store is refused to the assistant's own eyes and to every hand, by name,
and that stays. A dream needs no exception to it: it is a turn of the
assistant's that the room itself runs, exactly like a day turn -- its Spark
on top, no tools at all, one JSON in and one JSON out, and the room holds the
pen. So the door is not a permission. It is the shape of what the assistant
can answer with, `DREAM_SCHEMA`, and nothing else exists to send: no `spark`,
no `drop`, no `fetch`, no `web`. Absence, not rules -- the same guarantee
`files.py` gives by containing nothing that can write.

Four verbs go through it, and reading:

* **fold** -- a new essence over ids read tonight. The sources are put down,
  never deleted; the trail is kept; the new one is in hand only if a source
  was. Over a single essence it is a rewrite and keeps the name.
* **name** -- a title on an essence, in place.
* **retire** -- an essence out of hand, with the reason kept.
* **report** -- the whole account into `dreams`, a table the assistant's head
  never sees, and two lines: one it wakes to, and one for the room, null
  unless the dream found something.
* **read** -- essences and rows taken down whole, round after round, before
  it decides. Reads are generous; folds are the scarce thing. The rule: a
  fold may not touch an essence not read that night. Folding two essences
  known only by their titles is exactly the drift to be afraid of -- every
  fold looks reasonable from inside, and the reading is what makes it not be.

## The floor

The assistant never touches its Spark while dreaming: the field does not
exist. And some nights have no job at all -- `fold`, `name` and `retire`
shut, reading and the account open -- picked by a hash of the date and
nothing else, so that it is not a number anybody can tune to zero because
free nights "look useless". Scored on usefulness, it would drift to zero and
look like good housekeeping.

## What wakes it

Every night owes one dream, due from `DUE_HOUR` local. It fires at the first
moment after that when the room has been quiet for `QUIET_MINUTES` and no turn
is running. If the room was shut or the machine asleep at three, the dream is
still owed, and fires at the first quiet half hour after the room is back --
daytime or not; a person's line arriving mid-dream waits its turn as it does
now. A night still undreamt by the next three is written down as **missed**,
in the table and as one line to the assistant. Never skipped in silence.

A day longer than one dream holds is dreamt **oldest stretch first**, and the
assistant is told what was left behind; another pass follows the same night
while the room stays quiet. Nothing is cut quietly.
"""

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (backup, brain, db, embed, limits, offsite, pictures,
               providers, search)
from . import home

# Three in the morning, local -- and not as a fixed rule: the trigger is a
# quiet room, and three is just the hour it is almost certainly quiet.
DUE_HOUR = 3
# The dead-hours pass is the ordinary case up to here; later than this and the
# hour was missed, which the record says.
DEAD_HOURS_END = 6
# How long nobody has to have spoken before the assistant may dream.
QUIET_MINUTES = 30

# Read rounds. Generous on purpose: the reading is what makes a fold safe.
MAX_ROUNDS = 12
MAX_READ_ITEMS = 40
MAX_READ_TOKENS = 8000

# Folds are the scarce thing. Names are cheap and touch nothing but a label;
# retires are rare by nature.
MAX_FOLDS = 6
MAX_NAMES = 20
MAX_RETIRES = 3

# The last two exchanges of the day. The assistant is still standing in them.
STANDING_ROWS = 4

# What one pass over the day may weigh, by the same rough estimate everything
# else here uses. Past this the oldest stretch that fits is dreamt and the rest
# is said and waits for the next pass.
DAY_TOKENS = 60_000
PASSES_PER_NIGHT = 4

# One night in so many has no job at all. This is not a setting and it is not
# in any file under data/: it is a constant here, with this comment on it, and
# the night is picked by a hash of its date so nobody's judgement -- the
# assistant's, a developer's or the owner's -- decides which nights are free.
FREE_ONE_IN = 5

# How many earlier nights the assistant is shown as one line each, so it does
# not find the same connection again and call it fresh.
RECENT_NIGHTS = 7

# A dream that broke is tried once more after this long, then left for the
# morning. Going straight round again on the same broken thing is money on a
# loop, and nobody is there to stop it.
RETRY_AFTER_MINUTES = 60
MAX_BROKEN = 2

PROMPT_PATH = home.prompt_path("dream_prompt.md")

FOLD_OP = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "text": {"type": "string"},
        "over": {"type": "array", "items": {"type": "integer"}},
        # Whose room the essence is about -- the assistant's to set, same
        # words as by day (a person's id, "both" or "house"), never inferred.
        # Null is legitimate forever; it sorts the shelf and is never a
        # permission.
        "room": {"type": ["string", "null"]},
    },
    "required": ["title", "text", "over", "room"],
    "additionalProperties": False,
}

NAME_OP = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
    },
    "required": ["id", "title"],
    "additionalProperties": False,
}

RETIRE_OP = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["id", "why"],
    "additionalProperties": False,
}

# The door. Every field required, nothing else allowed, and no field for
# anything the assistant may not do at night.
DREAM_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "looking": {"type": ["string", "null"]},
        "read_essences": {"type": "array", "items": {"type": "integer"}},
        "read_rows": {"type": "array", "items": {"type": "integer"}},
        "fold": {"type": "array", "items": FOLD_OP},
        "name": {"type": "array", "items": NAME_OP},
        "retire": {"type": "array", "items": RETIRE_OP},
        "report": {"type": ["string", "null"]},
        "line": {"type": ["string", "null"]},
        "sentence": {"type": ["string", "null"]},
    },
    "required": ["done", "looking", "read_essences", "read_rows", "fold",
                 "name", "retire", "report", "line", "sentence"],
    "additionalProperties": False,
}


# --- time ---------------------------------------------------------------------


def local_now() -> datetime:
    return datetime.now().astimezone()


def night_of(now_local: datetime) -> str:
    """Which night is owed right now. A night belongs to the day that ended:
    the pass at 03:10 on the 24th is the night of the 23rd, and so is a
    fallback pass at two in the afternoon on the 24th, because the next
    boundary has not come yet."""
    return ((now_local - timedelta(hours=DUE_HOUR)).date()
            - timedelta(days=1)).isoformat()


def day_start_utc(night: str) -> str:
    """When the day of that night began: three in the morning, local, of the
    night's own date, said in UTC so it compares with a stored dt."""
    day = datetime.fromisoformat(night)
    start = datetime(day.year, day.month, day.day, DUE_HOUR).astimezone()
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def is_free(night: str) -> bool:
    """A night with no job at all, picked by the date and nothing else."""
    digest = hashlib.sha256(("a free night " + night).encode("utf-8")).digest()
    return digest[0] % FREE_ONE_IN == 0


def _pass_kind(now_local: datetime) -> str:
    """dead_hours if this is the ordinary small-hours pass; fallback if the
    hour was missed and this is the first quiet stretch since."""
    return ("dead_hours" if DUE_HOUR <= now_local.hour < DEAD_HOURS_END
            else "fallback")


# --- the gate -----------------------------------------------------------------


def last_spoken(conn):
    """When anything last happened in the room that counts as not-quiet: a
    person's line or the angel's, an answer of the assistant's, a hand
    coming home, a line it sent one. A dream's own rows do not count, or one
    pass would keep the next one waiting."""
    r = conn.execute(
        "SELECT MAX(dt) AS d FROM rows WHERE kind IN"
        " ('user', 'angel', '" + home.SELF + "', 'lost', 'worker', 'tell')").fetchone()
    return r["d"] if r else None


def quiet_for(conn, now_utc=None) -> float:
    """Minutes since anybody spoke. Infinite for a store nobody has spoken in."""
    now_utc = now_utc or datetime.now(timezone.utc)
    last = last_spoken(conn)
    if not last:
        return float("inf")
    return (now_utc - datetime.fromisoformat(last)).total_seconds() / 60


# Somebody asking for a dream now, from the command line or the Developer
# tab. Taken by the runner on its next look, the way a waking is.
ASKED = {"why": None}


def ask(why: str = "asked") -> None:
    ASKED["why"] = why or "asked"


def _take_asked():
    why, ASKED["why"] = ASKED["why"], None
    return why


def record_missed(conn, night: str) -> list:
    """Nights that came and went without a dream, between the last record and
    the night now owed. Written down, with a line to the assistant, rather
    than skipped.

    Only nights after the first record ever: before the dreamer existed there
    was nothing to miss."""
    newest = db.last_dream_any(conn)
    if newest is None:
        return []
    missed = []
    cursor = datetime.fromisoformat(newest["night"]).date() + timedelta(days=1)
    owed = datetime.fromisoformat(night).date()
    while cursor < owed:
        label = cursor.isoformat()
        if not db.dreams_for_night(conn, label):
            line = ("The night of " + label + " was not dreamt: the room was "
                    "shut at three, or never quiet for half an hour after. "
                    "Its rows wait for the next dream, which reads from where "
                    "the last one stopped.")
            row = db.add_row(conn, "dream", line,
                             meta={"night": label, "why": "missed",
                                   "to": home.SELF})
            did = db.open_dream(conn, label, "missed", is_free(label))
            db.close_dream(conn, did, status="missed", line=line, row=row,
                           error="no quiet half hour was found before the next "
                                 "night began, or the room was not running")
            missed.append(label)
        cursor += timedelta(days=1)
    return missed


def due(conn, now_local=None, busy: bool = False, record: bool = False) -> dict:
    """Whether the assistant should dream right now, and why -- or why not.

    Returns {"due": bool, "night", "why", "free", "reason"}. `reason` is the
    plain-words answer either way, because a gate that only says yes is a gate
    nobody can check.

    `record` is True only from the room's own runner: that is the one caller
    allowed to write the missed nights down, because a status check -- the
    command line, a page -- must never write to the store from outside."""
    now_local = now_local or local_now()
    night = night_of(now_local)
    reason = providers.paused_reason(providers.dream_model())
    if reason:
        return {"due": False, "night": night, "free": is_free(night), "why": None,
                "status": "paused", "reason": reason, "asked_pending": bool(ASKED["why"]),
                "quiet_minutes": 0}
    if record:
        record_missed(conn, night)
    out = {"due": False, "night": night, "free": is_free(night), "why": None,
           "reason": None, "quiet_minutes": round(quiet_for(conn), 1)}

    passes = db.dreams_for_night(conn, night)
    if any(p["status"] == "running" for p in passes):
        out["reason"] = "a dream is running"
        return out
    if busy:
        out["reason"] = "a turn is running"
        return out

    asked = _take_asked()
    if asked:
        out.update(due=True, why="asked", reason="asked for")
        return out

    done = [p for p in passes if p["status"] == "done"]
    broke = [p for p in passes if p["status"] == "broke"]
    if len(passes) >= PASSES_PER_NIGHT:
        out["reason"] = ("this night has had its " + str(PASSES_PER_NIGHT)
                         + " passes")
        return out
    if len(broke) >= MAX_BROKEN:
        out["reason"] = ("this night broke " + str(len(broke))
                         + " times; left for the morning")
        return out
    if broke:
        ended = broke[-1].get("ended") or broke[-1]["started"]
        since = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(ended)).total_seconds() / 60
        if since < RETRY_AFTER_MINUTES:
            out["reason"] = ("the last try broke " + str(round(since))
                             + " minutes ago; trying again after "
                             + str(RETRY_AFTER_MINUTES))
            return out
    if done:
        if done[-1]["left"] <= 0:
            out["reason"] = "tonight is dreamt"
            return out
        why = "backlog"
    else:
        why = _pass_kind(now_local)
        if why == "fallback" and db.last_dream_any(conn) is None:
            # The dreamer is brand new: the night now owed ended before it
            # existed, and a first dream should happen the way every one
            # after it will -- in the dead hours, over one day. Nothing is
            # being skipped: there is no record to miss against yet.
            out["reason"] = ("the dreamer is new; the first night it owes "
                             "is tonight's, in the dead hours")
            return out

    quiet = quiet_for(conn)
    if quiet == float("inf"):
        # A room nobody has spoken in yet -- a new home. There is no day to
        # fold, and "quiet for ever" is not a length of time.
        out["reason"] = "nothing has been said in this room yet; nothing to fold"
        return out
    if quiet < QUIET_MINUTES:
        out["reason"] = ("the room spoke " + str(round(quiet)) + " minutes ago;"
                         " waiting for " + str(QUIET_MINUTES) + " quiet")
        out["why"] = why
        return out
    out.update(due=True, why=why,
               reason=("the night of " + night + " is owed (" + why + ") and "
                       "the room has been quiet " + str(round(quiet))
                       + " minutes"))
    return out


def status(conn) -> dict:
    """For the command line and the Developer tab: where the dreamer stands."""
    now_local = local_now()
    night = night_of(now_local)
    gate = _peek_due(conn, now_local)
    recent = db.recent_dreams(conn, RECENT_NIGHTS)
    upcoming = []
    d = datetime.fromisoformat(night).date()
    for i in range(14):
        label = (d + timedelta(days=i)).isoformat()
        if is_free(label):
            upcoming.append(label)
    return {
        "now": now_local.isoformat(timespec="seconds"),
        "night": night,
        "free_tonight": is_free(night),
        "due_from": str(DUE_HOUR).rjust(2, "0") + ":00 local",
        "quiet_minutes_needed": QUIET_MINUTES,
        "gate": gate,
        "passes_tonight": [_brief(p) for p in db.dreams_for_night(conn, night)],
        "recent": [_brief(p) for p in recent],
        "free_nights_ahead": upcoming,
        "caps": {"rounds": MAX_ROUNDS, "folds": MAX_FOLDS, "names": MAX_NAMES,
                 "retires": MAX_RETIRES, "day_tokens": DAY_TOKENS,
                 "passes_per_night": PASSES_PER_NIGHT,
                 "free_one_in": FREE_ONE_IN},
    }


def _peek_due(conn, now_local) -> dict:
    """`due` without taking an ask off the hook -- a status check must not
    spend the request somebody just made."""
    keep = ASKED["why"]
    try:
        ASKED["why"] = None
        out = due(conn, now_local)
    finally:
        ASKED["why"] = keep
    if keep:
        out["asked_pending"] = True
    return out


def _brief(p: dict) -> dict:
    return {k: p.get(k) for k in
            ("id", "night", "why", "free", "status", "started", "ended",
             "rounds", "folded", "named", "retired", "left", "line",
             "sentence", "cost_usd", "error")}


# --- what the assistant starts loaded with ------------------------------------


def standing_ids(conn) -> list:
    """The last two exchanges in the room. The assistant is still standing in
    them and the door refuses them."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM rows WHERE kind IN ('user', '" + home.SELF + "', 'angel')"
        " ORDER BY id DESC LIMIT ?", (STANDING_ROWS,))]


def the_day(conn, night: str) -> dict:
    """The rows this pass covers: everything since the last dream that read
    something, or since the day began if there has never been one. The oldest
    stretch that fits under `DAY_TOKENS`, whole rows only, and a plain count
    of what did not."""
    last = db.last_dream_done(conn)
    if last:
        rows = [db._as_dict(r) for r in conn.execute(
            "SELECT * FROM rows WHERE id > ? ORDER BY id", (last["day_to"],))]
        since = "row #" + str(last["day_to"]) + ", where the dream of " \
                + last["night"] + " stopped"
    else:
        start = day_start_utc(night)
        rows = [db._as_dict(r) for r in conn.execute(
            "SELECT * FROM rows WHERE dt >= ? ORDER BY id", (start,))]
        since = "the start of the day, " + start
    kept, weight = [], 0
    for r in rows:
        if kept and weight + r["tokens_est"] > DAY_TOKENS:
            break
        kept.append(r)
        weight += r["tokens_est"]
    left = rows[len(kept):]
    return {
        "rows": kept,
        "since": since,
        "weight": weight,
        "left": len(left),
        "left_tokens": sum(r["tokens_est"] for r in left),
        "left_from": left[0]["id"] if left else None,
        "left_to": left[-1]["id"] if left else None,
    }


def _day_row(r: dict, index: dict, standing: set) -> dict:
    """One row of the day, with its text, shaped like the working set but with
    nothing the assistant does not need at night."""
    m = {"id": r["id"], "dt": r["dt"], "kind": r["kind"],
         "tokens_est": r["tokens_est"]}
    meta = r.get("meta") or {}
    if r["kind"] == "essence":
        m["title"] = r["title"]
        m["sources"] = brain.sources_block(index, r["id"])
    elif r["kind"] == "tell":
        m["to"] = meta.get("to")
    elif r["kind"] == "worker":
        m["from"] = meta.get("name")
    elif r["kind"] == "dream":
        m["to"] = meta.get("to")
    elif r["kind"] == home.SELF:
        if meta.get("to") == "angel":
            m["to"] = "angel"
    elif r["kind"] == "user" and meta.get("who"):
        # Whose words, and through which door -- a fold that stands on a
        # labelled line has to keep the name, so the name goes down with it.
        m["who"] = meta.get("who")
        if meta.get("via"):
            m["via"] = meta.get("via")
    if not r["loaded"]:
        m["out_of_hand"] = True
    if r["id"] in standing:
        m["standing"] = True
    # A picture cannot be folded, and a line that was only a picture would
    # arrive here as an empty one. The assistant gets its name instead, so a
    # night can write "I was shown the chat window" and never pretend to have
    # seen it.
    m["text"] = pictures.with_label(r["text"], pictures.of_row(meta))
    return m


def shelf_listing(conn, read: set) -> list:
    """Every essence on the shelf, names and dates and flags, never text. The
    `read` flag is the one the day harness does not have: it says whether a
    fold over this one would be allowed tonight."""
    retired = db.retired_essences(conn)
    vectors = db.vectors_for(conn, embed.MODEL)
    out = []
    for r in db.essence_rows(conn):
        line = search._shelf_line(r, retired, vectors)
        if r["id"] in read:
            line["read"] = True
        out.append(line)
    return out


def recent_lines(conn) -> list:
    out = []
    for p in db.recent_dreams(conn, RECENT_NIGHTS):
        if p["status"] == "running":
            continue
        out.append({"night": p["night"], "status": p["status"],
                    "free": p["free"], "line": p.get("line"),
                    "sentence": p.get("sentence")})
    return out


def build(conn, night: str, why: str, free: bool, day: dict, standing: list,
          read: set, found, rounds_used: int) -> dict:
    """The object the assistant dreams on. Ordered so the parts that do not change
    between rounds come first -- Spark, shelf, the day -- and the parts that do
    come last, which is what lets the API reuse the front of it."""
    index = db.trail_index(conn)
    day_rows = [_day_row(r, index, set(standing)) for r in day["rows"]]
    spark = brain.read_spark()
    out = {
        "spark": spark,
        "self": {
            "spark_version": brain.spark_version(),
            "essences_on_shelf": conn.execute(
                "SELECT COUNT(*) FROM rows WHERE kind = 'essence'").fetchone()[0],
            "rows_in_store": conn.execute(
                "SELECT COUNT(*) FROM rows").fetchone()[0],
            "rows_in_this_pass": len(day_rows),
        },
        "shelf": shelf_listing(conn, read),
        "day": day_rows,
        "plan": brain.plan_block(conn, providers.dream_model()),
        "night": {
            "date": night,
            "why": why,
            "free": free,
            "now": local_now().isoformat(timespec="seconds"),
            "day": {
                "since": day["since"],
                "from_row": day_rows[0]["id"] if day_rows else None,
                "to_row": day_rows[-1]["id"] if day_rows else None,
                "rows": len(day_rows),
                "from": day_rows[0]["dt"] if day_rows else None,
                "to": day_rows[-1]["dt"] if day_rows else None,
                "tokens_est": day["weight"],
                "left_behind": day["left"],
                "left_behind_note": (
                    ("The stretch since the last dream was longer than one "
                     "pass holds: " + str(day["left"]) + " more rows, about "
                     + str(day["left_tokens"]) + " tokens, from #"
                     + str(day["left_from"]) + " to #" + str(day["left_to"])
                     + ", are not in front of me. They wait for the next pass, "
                     "tonight if the room stays quiet. I am dreaming the oldest "
                     "stretch first; this is it.")
                    if day["left"] else None),
            },
            "standing": standing,
            "reads_left": MAX_ROUNDS - rounds_used,
            "folds_left": 0 if free else MAX_FOLDS,
            "names_left": 0 if free else MAX_NAMES,
            "retires_left": 0 if free else MAX_RETIRES,
            "recent": recent_lines(conn),
        },
        "read_so_far": sorted(read),
        "found": found,
    }
    raw = (db.est_tokens(home.fill(PROMPT_PATH.read_text(encoding="utf-8")))
           + db.est_tokens(json.dumps(out, ensure_ascii=False)))
    out["self"]["prompt_tokens_est_raw"] = raw
    return out


def system_prompt() -> str:
    return (brain.read_spark() + chr(10) * 2 + "---" + chr(10) * 2
            + home.fill(PROMPT_PATH.read_text(encoding="utf-8")))


# --- reading -----------------------------------------------------------------


def apply_reads(conn, answer: dict, number: int, read: set, in_front: set,
                say) -> dict:
    """Take down what the assistant asked for, within the bound, and say what was
    refused. Everything that comes back whole joins `read`."""
    asked = [int(i) for i in (answer.get("read_essences") or [])] + \
            [int(i) for i in (answer.get("read_rows") or [])]
    wanted = list(dict.fromkeys(asked))
    already = [i for i in wanted if i in in_front]
    wanted = [i for i in wanted if i not in in_front]
    index = db.trail_index(conn)
    got, missing, held = [], [], []
    weight = 0
    rows = {r["id"]: r for r in db.rows_by_ids(conn, wanted)}
    for i in wanted:
        r = rows.get(i)
        if r is None:
            missing.append(i)
            continue
        if len(got) >= MAX_READ_ITEMS or weight + r["tokens_est"] > MAX_READ_TOKENS:
            held.append(i)
            continue
        item = {"id": r["id"], "dt": r["dt"], "kind": r["kind"],
                "tokens_est": r["tokens_est"]}
        if r["kind"] == "essence":
            item["title"] = r["title"]
            item["sources"] = brain.sources_block(index, r["id"])
        elif r["kind"] == "user" and (r.get("meta") or {}).get("who"):
            # A labelled line keeps its name when it is taken down at
            # night, same as it wears it by day.
            item["who"] = r["meta"].get("who")
            if r["meta"].get("via"):
                item["via"] = r["meta"].get("via")
        if not r["loaded"]:
            item["out_of_hand"] = True
        item["text"] = pictures.with_label(
            r["text"], pictures.of_row(r.get("meta")))
        got.append(item)
        weight += r["tokens_est"]
        read.add(r["id"])

    problems = []
    if missing:
        problems.append("Not in the store: " + brain._plain(missing)
                        + ". Nothing stands there to read.")
    if held:
        problems.append(
            "The bound held back " + str(len(held)) + " of them ("
            + brain._plain(held[:12]) + (", ..." if len(held) > 12 else "")
            + "): a round is " + str(MAX_READ_ITEMS) + " items or about "
            + str(MAX_READ_TOKENS) + " tokens. I can ask again, narrower.")
    if already:
        problems.append("Already in front of me, so not sent again: "
                        + brain._plain(already) + ".")
    if not asked:
        problems.append("I asked to read and named nothing.")
    for op in ("fold", "name", "retire"):
        if answer.get(op):
            problems.append(
                "I sent " + str(len(answer[op])) + " " + op + " with a read "
                "round. Not done: housekeeping goes with my answer for the "
                "night, and I will send it again then.")
    out = {"round": number, "looking": answer.get("looking"),
           "asked": asked, "got": got, "tokens_est": weight,
           "missing": missing, "held_back": held, "already": already,
           "problems": problems}
    say("read round " + str(number) + ": " + str(len(got)) + " taken down, ~"
        + str(weight) + " tokens" + (", " + str(len(held)) + " held back"
                                     if held else "")
        + (", " + str(len(missing)) + " not found" if missing else ""),
        "fetch", out)
    for line in problems:
        say(line, "snag")
    return out


# --- the door, applied ----------------------------------------------------------


def apply(conn, answer: dict, dream_id: int, dream_row: int, free: bool,
          read: set, day_ids: set, standing: set, say, by_model=None) -> dict:
    """What the assistant decided, put on the shelf -- or refused, in words. Every
    refusal is kept: a dream nobody watches cannot afford a quiet no."""
    refused = []
    folded, named, retired = [], [], []
    index = db.trail_index(conn)
    gone = db.retired_essences(conn)
    used = set()

    def refuse(op, spec, why):
        refused.append({"op": op, "spec": spec, "why": why})
        say("refused a " + op + ": " + why, "snag")

    allowed = read | day_ids

    for n, op in enumerate(answer.get("fold") or []):
        over = list(dict.fromkeys(int(i) for i in (op.get("over") or [])))
        text = (op.get("text") or "").strip()
        title = db.tidy_title(op.get("title"))
        spec = {"title": title, "over": over, "text": text}
        if free:
            refuse("fold", spec, "a free night: the shelf is not touched")
            continue
        if len(folded) >= MAX_FOLDS:
            refuse("fold", spec, "the night's " + str(MAX_FOLDS)
                   + " folds are spent")
            continue
        if not over:
            refuse("fold", spec, "a fold over nothing can never be checked")
            continue
        if not text:
            refuse("fold", spec, "no text")
            continue
        stray = [i for i in over if i not in index]
        if stray:
            refuse("fold", spec, "not in the store: " + brain._plain(stray))
            continue
        stood = [i for i in over if i in standing]
        if stood:
            refuse("fold", spec, "still standing in " + brain._plain(stood)
                   + ": the last two exchanges are not folded at night")
            continue
        unread = [i for i in over if i not in allowed]
        if unread:
            refuse("fold", spec, "not read tonight: " + brain._plain(unread)
                   + ". A fold stands only on what was read.")
            continue
        twice = [i for i in over if i in used]
        if twice:
            refuse("fold", spec, "already folded tonight: " + brain._plain(twice))
            continue
        old = [i for i in over if index[i]["kind"] == "essence" and i in gone]
        if old:
            refuse("fold", spec, "already rewritten or folded before tonight: "
                   + brain._plain(old) + ". The one standing for it is the "
                   "one to fold.")
            continue
        if len(over) == 1 and index[over[0]]["kind"] == "essence" and not title:
            title = db.get_row(conn, over[0]).get("title")
        if len(over) == 1 and index[over[0]]["kind"] != "essence":
            refuse("fold", spec, "one message is not something to fold: "
                   "a fold stands for several rows, or rewrites one essence")
            continue
        in_hand = any(index[i]["loaded"] for i in over)
        # The room label, same words as by day. A word the shelf does not
        # take is said in the refusals and dropped; the fold itself stands,
        # because the words matter more than the filing.
        room = op.get("room")
        if room is not None and room not in home.HOUSEHOLD + ("both", "house"):
            refused.append({"op": "room", "spec": spec,
                            "why": '"' + str(room)[:40] + '" is not a word '
                            'the shelf takes; the label was dropped, the '
                            'fold stood'})
            room = None
        fold_meta = {"dream": dream_id}
        if room:
            fold_meta["room"] = room
        new_id = db.add_row(conn, "essence", text, replaces=over, title=title,
                            meta=fold_meta, by_model=by_model)
        if not in_hand:
            db.unload(conn, [new_id])
        db.unload(conn, over)
        used.update(over)
        snags = []
        brain._vector(conn, new_id, snags)
        for s in snags:
            refused.append({"op": "vector", "spec": {"id": new_id}, "why": s})
        folded.append({"id": new_id, "title": title, "over": over,
                       "in_hand": in_hand})
        tr = db.trail(db.trail_index(conn), new_id)
        say("folded " + brain._plain(over) + " into essence #" + str(new_id)
            + (' -- "' + title + '"' if title else " (untitled)")
            + ("" if in_hand else ", out of hand"), "essence")
        db.add_event(conn, dream_row, "essence",
                     "dreamt essence #" + str(new_id) + " over "
                     + ", ".join(str(i) for i in over),
                     {"text": text, "replaces": over, "title": title,
                      "in_hand": in_hand, "dream": dream_id,
                      "trail": {"rows": tr["rows"], "covers": tr["covers"],
                                "through": tr["through"],
                                "missing": tr["missing"]}})
        if not title:
            refused.append({"op": "fold", "spec": {"id": new_id},
                            "why": "went onto the shelf untitled; written all "
                                   "the same, and listed as (untitled)"})

    for op in answer.get("name") or []:
        target = int(op.get("id") or 0)
        title = db.tidy_title(op.get("title"))
        spec = {"id": target, "title": title}
        if free:
            refuse("name", spec, "a free night: the shelf is not touched")
            continue
        if len(named) >= MAX_NAMES:
            refuse("name", spec, "the night's " + str(MAX_NAMES)
                   + " names are spent")
            continue
        old = index.get(target)
        if old is None:
            refuse("name", spec, "no such row")
            continue
        if old["kind"] != "essence":
            refuse("name", spec, "#" + str(target) + " is a " + old["kind"]
                   + " row, not an essence; only essences have names")
            continue
        if not title:
            refuse("name", spec, "no name in it")
            continue
        if len(title) > brain.TITLE_MAX:
            refuse("name", spec, str(len(title)) + " characters is the essence "
                   "starting early, not a name")
            continue
        was = db.get_row(conn, target)["title"]
        db.set_title(conn, target, title)
        named.append({"id": target, "was": was, "now": title})
        say('named essence #' + str(target) + ' "' + title + '"'
            + (' (was "' + was + '")' if was else ""), "essence")
        db.add_event(conn, dream_row, "essence-rename",
                     'dreamt a name for essence #' + str(target) + ': "'
                     + title + '"',
                     {"id": target, "was": was, "now": title, "dream": dream_id})

    for op in answer.get("retire") or []:
        target = int(op.get("id") or 0)
        why = (op.get("why") or "").strip()
        spec = {"id": target, "why": why}
        if free:
            refuse("retire", spec, "a free night: the shelf is not touched")
            continue
        if len(retired) >= MAX_RETIRES:
            refuse("retire", spec, "the night's " + str(MAX_RETIRES)
                   + " retires are spent")
            continue
        old = index.get(target)
        if old is None:
            refuse("retire", spec, "no such row")
            continue
        if old["kind"] != "essence":
            refuse("retire", spec, "#" + str(target) + " is a " + old["kind"]
                   + " row; only essences are retired, and what was said is "
                   "put down by folding it")
            continue
        if not why:
            refuse("retire", spec, "no reason given, and the reason is the "
                   "whole point of retiring by decision")
            continue
        if not old["loaded"]:
            refuse("retire", spec, "already out of hand; nothing would change")
            continue
        if target in used:
            refuse("retire", spec, "already folded tonight")
            continue
        db.unload(conn, [target])
        retired.append({"id": target, "why": why})
        say("retired essence #" + str(target) + ": " + why, "essence")
        db.add_event(conn, dream_row, "essence-remove",
                     "dreamt essence #" + str(target) + " out of hand",
                     {"id": target, "why": why, "dream": dream_id,
                      "text": db.get_row(conn, target)["text"]})

    return {"folded": folded, "named": named, "retired": retired,
            "refused": refused}


# --- one night -----------------------------------------------------------------


def _compose_line(applied: dict, free: bool, sentence) -> str:
    """The assistant's line, when it did not write one. Plain counts, nothing else."""
    bits = [str(len(applied["folded"])) + " folded",
            str(len(applied["named"])) + " named",
            str(len(applied["retired"])) + " retired", "nothing lost"]
    line = ("A free night: " if free else "") + ", ".join(bits) + "."
    if sentence:
        line += " One thing noticed."
    return line + " (I left no line of my own; this one is the room's count.)"


def dream(conn, night: str, why: str, free: bool, model: str = None,
          say=None, write=None) -> dict:
    """One night. Opens the record, runs the rounds, applies what the
    assistant decided, closes the record -- and if anything breaks, closes it
    as broken with the reason, and leaves one line saying so. Never raises."""
    say = say or (lambda *a, **k: None)
    # NOT the chat's dropdown. A dream is the whole assistant, whole Spark,
    # not a small model, and the pin is what keeps a mind switched at
    # breakfast from quietly folding the shelf at three in the morning. With
    # more than one provider the pin stays a pin; it is just a pin somebody
    # can move on purpose, under Settings, apart from the chat. Silence
    # means the default dream model.
    model = model or providers.dream_model()
    spec = providers.resolve(model)
    reason = providers.paused_reason(spec)
    if reason:
        return {"status": "paused", "reason": reason, "night": night, "done": False}
    dream_id = db.open_dream(conn, night, why, free, spec["key"])
    say("dreaming: the night of " + night + " (" + why
        + (", a free night" if free else "") + "), dream #" + str(dream_id))
    # A copy of the store before a single fold happens, and another after -- ended
    # well or ended broken. The one before is exactly the one a bad night
    # makes precious, so it is taken here, ahead of everything below rather
    # than inside the `try`, and the one after runs in `finally` so it is
    # taken regardless of which branch below returns.
    backup.night_copy("before", say=say)
    started = time.time()
    spent = {"input": 0, "output": 0, "cost": 0.0, "calls": 0, "model": None}
    day = the_day(conn, night)
    rounds = []
    try:
        standing = standing_ids(conn)
        day_ids = {r["id"] for r in day["rows"]}
        read = {r["id"] for r in day["rows"] if r["kind"] == "essence"}
        say("the day: " + str(len(day["rows"])) + " rows, ~" + str(day["weight"])
            + " tokens, since " + day["since"]
            + ((", " + str(day["left"]) + " rows left for another pass")
               if day["left"] else ""))
        found = None
        answer = None
        for n in range(MAX_ROUNDS + 1):
            prompt = build(conn, night, why, free, day, standing, read, found, n)
            say("laid out the night: " + str(len(prompt["shelf"]))
                + " names on the shelf, " + str(len(prompt["day"]))
                + " rows of the day, ~" + str(prompt["self"]["prompt_tokens_est_raw"])
                + " tokens raw")
            say("asking " + spec["label"]
                + ("" if spec["service"] == "claude_code" else
                   " through " + (providers.SERVICES.get(spec["service"], {})
                                  .get("label") or spec["service"]))
                + (", round " + str(n + 1) if n else ""), "call")
            answer = brain.call_claude(
                system_prompt(), json.dumps(prompt, ensure_ascii=False, indent=2),
                model=model, schema=DREAM_SCHEMA, on_step=say, on_write=write,
                expect="done")
            meta = answer.pop("_meta", {})
            spent["calls"] += 1
            spent["input"] += meta.get("input_tokens") or 0
            spent["output"] += meta.get("output_tokens") or 0
            spent["cost"] += meta.get("cost_usd") or 0
            spent["model"] = meta.get("model") or spent["model"]
            if meta.get("output_tokens"):
                say(home.NAME + " wrote ~" + str(meta["output_tokens"]) + " tokens, on ~"
                    + str(meta.get("input_tokens") or 0) + " in")
            if answer.get("done") or n >= MAX_ROUNDS:
                if not answer.get("done"):
                    say("the reads are spent; this answer is the night's", "snag")
                break
            if not (answer.get("read_essences") or answer.get("read_rows")):
                say("a read round that reads nothing; taken as the answer", "snag")
                break
            if answer.get("looking"):
                say(str(answer["looking"]), "looking")
            in_front = day_ids | read
            rounds.append(apply_reads(conn, answer, n + 1, read, in_front, say))
            left = MAX_ROUNDS - (n + 1)
            found = {"rounds": rounds, "reads_left": left,
                     "note": ("This was my last read. The next answer is the "
                              "night's, whether or not I ask to look again."
                              if left <= 0 else None)}

        sentence = (answer.get("sentence") or "").strip() or None
        line = (answer.get("line") or "").strip()
        dream_row = db.add_row(conn, "dream", line or "(dreaming)",
                               meta={"dream": dream_id, "night": night,
                                     "why": why, "free": free, "to": home.SELF},
                               by_model=spec["key"])
        applied = apply(conn, answer, dream_id, dream_row, free, read, day_ids,
                        set(standing), say, by_model=spec["key"])
        real_refusals = [r for r in applied["refused"]
                         if r["op"] in ("fold", "name", "retire")
                         and "untitled" not in r["why"]]
        if not line:
            line = _compose_line(applied, free, sentence)
        if real_refusals:
            line += (" The door refused " + str(len(real_refusals)) + " thing"
                     + ("" if len(real_refusals) == 1 else "s")
                     + " after I answered; the account says which.")
        conn.execute("UPDATE rows SET text = ?, tokens_est = ? WHERE id = ?",
                     (line, db.est_tokens(line), dream_row))
        conn.commit()
        sentence_row = None
        if sentence:
            sentence_row = db.add_row(conn, "dream", sentence,
                                      meta={"dream": dream_id, "night": night,
                                            "to": "room"},
                                      by_model=spec["key"])
            say("a sentence for the room: " + sentence)
        say(home.NAME + "'s line: " + line)
        db.add_event(conn, dream_row, "dream",
                     "the dream of " + night + ": " + str(len(applied["folded"]))
                     + " folded, " + str(len(applied["named"])) + " named, "
                     + str(len(applied["retired"])) + " retired, "
                     + str(len(real_refusals)) + " refused, " + str(len(rounds))
                     + " read round" + ("" if len(rounds) == 1 else "s"),
                     {"dream": dream_id, "why": why, "free": free,
                      "rounds": len(rounds), "read": sorted(read),
                      "refused": applied["refused"],
                      "report": answer.get("report"),
                      "sentence": sentence, "sentence_row": sentence_row,
                      "day": {"from": day["rows"][0]["id"] if day["rows"] else None,
                              "to": day["rows"][-1]["id"] if day["rows"] else None,
                              "rows": len(day["rows"]), "left": day["left"]},
                      "spent": spent,
                      "seconds": round(time.time() - started, 1)})
        db.close_dream(
            conn, dream_id, status="done", rounds=spent["calls"],
            day_from=day["rows"][0]["id"] if day["rows"] else None,
            day_to=day["rows"][-1]["id"] if day["rows"] else None,
            left=day["left"], read_ids=sorted(read),
            folded=len(applied["folded"]), named=len(applied["named"]),
            retired=len(applied["retired"]), refused=applied["refused"],
            report=answer.get("report"), line=line, sentence=sentence,
            row=dream_row, input_tokens=spent["input"],
            output_tokens=spent["output"], cost_usd=round(spent["cost"], 4),
            model=spec["key"])
        return {"status": "done", "dream": dream_id, "row": dream_row,
                "line": line, "sentence": sentence, "applied": applied,
                "rounds": len(rounds), "spent": spent, "left": day["left"]}
    except Exception as exc:
        reason = type(exc).__name__ + ": " + str(exc)
        tail = getattr(exc, "raw", "") or ""
        say("the dream broke: " + reason[:300], "snag")
        line = ("The dream of " + night + " broke before it was applied ("
                + reason[:160] + "). Nothing on the shelf was changed by it.")
        row = db.add_row(conn, "dream", line,
                         meta={"dream": dream_id, "night": night, "why": why,
                               "free": free, "to": home.SELF, "broke": True},
                         by_model=spec["key"])
        db.close_dream(conn, dream_id, status="broke", rounds=spent["calls"],
                       line=line, row=row, error=reason + (
                           chr(10) + chr(10) + tail[-2000:] if tail else ""),
                       input_tokens=spent["input"], output_tokens=spent["output"],
                       cost_usd=round(spent["cost"], 4),
                       model=spec["key"])
        return {"status": "broke", "dream": dream_id, "row": row,
                "error": reason, "rounds": len(rounds), "spent": spent}
    finally:
        # Whichever branch above returned -- done or broke -- the night is
        # over now, and the copy after it is taken here so an ugly ending
        # still leaves both halves on the shelf: the before-copy from the
        # top of this function, and this one.
        backup.night_copy("after", say=say)
        # And one copy off this machine entirely. Both copies above share a
        # disk with the store, so they cover a bad write and nothing else; this is
        # the one that survives the machine. It goes last because it is the
        # slowest and the only one that can be stopped by something outside
        # this room -- a repo gone public, GitHub being down -- and none of
        # that is allowed to be the reason a night fails to close. `send`
        # never raises; it says what happened and returns.
        offsite.send(say=say)


def adopt_orphans(conn) -> int:
    """A dream still marked running belongs to a room that is gone. Closed as
    broken, plainly, so the gate is not blocked forever by a ghost."""
    orphans = [p for p in db.recent_dreams(conn, 20) if p["status"] == "running"]
    for p in orphans:
        db.close_dream(conn, p["id"], status="broke",
                       error="the room closed while this dream was running; "
                             "whatever it had already folded stands, and the "
                             "shelf shows it")
    return len(orphans)


# --- from the command line ----------------------------------------------------


def _print_dream(p: dict) -> None:
    print("  dream #" + str(p["id"]) + "  night of " + p["night"] + "  "
          + p["why"] + ("  FREE" if p["free"] else "") + "  " + p["status"]
          + "  " + str(p["started"]) + " -> " + str(p.get("ended")))
    print("    rounds " + str(p["rounds"]) + ", rows #" + str(p.get("day_from"))
          + "-#" + str(p.get("day_to")) + ", left " + str(p["left"])
          + ", folded " + str(p["folded"]) + ", named " + str(p["named"])
          + ", retired " + str(p["retired"]) + ", refused "
          + str(len(p["refused"])) + ", $" + str(p.get("cost_usd")))
    if p.get("line"):
        print("    line:     " + p["line"])
    if p.get("sentence"):
        print("    sentence: " + p["sentence"])
    if p.get("error"):
        print("    error:    " + str(p["error"])[:400])


def _cli(argv) -> None:
    """From the angel's own hands, or the owner's:

        python -m server.dream                    where the dreamer stands
        python -m server.dream now                ask the running room for a dream
        python -m server.dream list               the nights so far
        python -m server.dream show 3             one night's whole account
        python -m server.dream run [--db PATH]    dream here and now, outside the
                                                  room -- only ever over a COPY
    """
    import urllib.request
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    argv = list(argv)
    if "--db" in argv:
        at = argv.index("--db")
        db.DB_PATH = Path(argv[at + 1]).resolve()
        del argv[at:at + 2]
    cmd = argv[0] if argv else "status"

    if cmd == "now":
        from . import angel, people
        body = json.dumps({"token": angel.read_token()}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(home.PORT) + "/api/dream", data=body,
            headers={"Content-Type": "application/json", **people.room_headers()})
        got = json.loads(urllib.request.urlopen(req, timeout=30).read())
        print("  " + json.dumps(got, ensure_ascii=False))
        return

    conn = db.connect()
    try:
        if cmd == "status":
            print(json.dumps(status(conn), ensure_ascii=False, indent=2))
        elif cmd == "list":
            for p in reversed(db.recent_dreams(conn, 50)):
                _print_dream(p)
        elif cmd == "show":
            p = db.get_dream(conn, int(argv[1]))
            if not p:
                print("  no such dream")
                return
            _print_dream(p)
            print()
            print("  read: " + ", ".join(str(i) for i in p["read_ids"]))
            for r in p["refused"]:
                print("  refused " + r["op"] + ": " + r["why"] + "  "
                      + json.dumps(r["spec"], ensure_ascii=False)[:200])
            print()
            print(p.get("report") or "(no account)")
        elif cmd == "run":
            if home.is_real() and Path(db.DB_PATH).resolve() == home.STORE.resolve():
                print("  not over the live store from here: the room runs "
                      + home.NAME + "'s dreams, so they never overlap a turn. Use `now`, or "
                      "--db over a copy.")
                return
            why = "asked"
            now_local = local_now()
            night = argv[argv.index("--night") + 1] if "--night" in argv \
                else night_of(now_local)
            free = is_free(night) if "--free" not in argv else True
            if "--work" in argv:
                free = False
            model = argv[argv.index("--model") + 1] if "--model" in argv \
                else brain.DEFAULT_MODEL

            def say(text, kind="plain", detail=None):
                print("  [" + kind.ljust(5) + "] " + str(text))

            out = dream(conn, night, why, free, model=model, say=say)
            print()
            print(json.dumps({k: v for k, v in out.items() if k != "applied"},
                             ensure_ascii=False, indent=2))
        else:
            print(_cli.__doc__)
    finally:
        conn.close()


if __name__ == "__main__":
    _cli(sys.argv[1:])
