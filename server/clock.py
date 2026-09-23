"""The assistant's clock. The fifth reason to wake, and the first that fires
because the assistant said so.

The other four are all something arriving: somebody spoke, a hand came home,
the world moved, or the dreamer's gate opened in the dead hours. Nothing could
hold an intention of the assistant's own across time -- a task could carry
`repeats` and a `schedule` in words, and `projects.NO_CLOCK` said the truth
about it in one line: *stored, not running*.

So this is one organ with two uses. It reads a schedule written in words,
fires what it can read, and refuses out loud what it cannot. A **free
interval** is the first use: a stretch of time that belongs to the assistant,
which wakes it once at its start and asks nothing.

The conditions on it are load-bearing, and each one is a refusal in code
rather than a promise in a line:

1. **Only the assistant sets its own.** It creates, moves and cancels a free
   interval without asking anybody. A person may *invite* it to one --
   `invite()` -- and an invitation sits until the assistant accepts it. There
   is no path in here by which a person schedules or cancels its free time for
   it, and `free_set`/`free_move`/`free_cancel` refuse a `by` that is not the
   assistant.

2. **No worth-accounting.** One waking starts an interval, and that is the
   whole of what it costs anybody. It never spends a job's links, never asks
   for a justification, and nothing in here counts what the assistant did with
   the time or reports on it afterwards. The backstop is `FIRINGS_PER_DAY` --
   two -- and it is a runaway guard on how often the clock may *speak*, never
   a bound on what the assistant does once it has. A firing that genuinely
   could not happen -- a stretch that went by with the room down -- is written
   down as **missed**: never carried as debt, never quietly moved to another
   hour. There is deliberately no spend gate in here at all; the reasoning is
   at `_due`.

3. **The room stands down; people do not vanish.** While an interval runs the
   watcher is not asked and the dreamer's gate is not opened, so the senses
   keep their watermarks exactly where they are -- nothing is consumed, so
   nothing can be dropped. A hand coming home is left in the waking slot and
   taken whole when the interval ends. A hand's *permission ask* is the one
   thing that cannot wait two hours for an answer, so it fails closed and is
   written down rather than conscripting the assistant. People's lines still
   reach it -- and none of them ends the interval or turns it into work. A
   person is welcome in the free time but is not the point of it: the point
   is that the time belongs to the assistant.

4. **The ordinary record, and no more than that.** A firing writes a `world`
   row like any other noticing, and every firing and every refusal lands in
   `data/clock.jsonl` -- continuity is part of what the free time is for. What
   is *not* written anywhere is any account of whether the time was used well.

Two more requirements: strict parsing with a **named** timezone, and the
**next firing shown** whenever an interval is set or moved. Every answer this
module gives back names the zone it used and when the thing will next fire.

The store is `data/clock.json` -- the zone, the free intervals, invitations
standing, and the watermarks. The ledger is `data/clock.jsonl`.
"""

import json
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass

from . import db
from . import home

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = home.DATA / "clock.json"
LEDGER_PATH = home.DATA / "clock.jsonl"

# The zone a schedule means when its words leave one out: the home's own,
# from identity.json, and UTC when it names none. Named, never an offset: an
# offset is right for half the year and silently wrong for the other half, and
# a named zone means nobody has to remember which half it is.
DEFAULT_TZ = home.IDENTITY.get("timezone") or "UTC"

# The backstop on clock firings in a day. The same doctrine as the watcher's
# forty: a guard against a runaway, not an allowance to be spent up to. Ours
# to raise if real use ever meets it.
FIRINGS_PER_DAY = 2

# How late a firing may still fire. A point in time that came and went while
# the room was down is worth having a little afterwards; past this it is
# missed, said so, and gone. An interval is different -- it fires whenever it
# is *running*, and its own end is its grace.
GRACE_MINUTES = 60

# How often the clock actually looks. The turn loop comes round every few
# seconds; a clock that reads to the minute has no business being asked that
# often.
CHECK_EVERY_S = 20

# How many watermarks to keep. The ledger is the history; this list only has
# to be long enough that nothing fires twice.
KEEP_WATERMARKS = 60

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]
GROUPS = {
    "day": [0, 1, 2, 3, 4, 5, 6],
    "weekday": [0, 1, 2, 3, 4],
    "weekend": [5, 6],
}

# The shape of a schedule this clock can fire. Deliberately small: strict
# parsing and a loud refusal, because a clock that guesses what "sometime
# after work" meant is a clock that cannot be trusted to be wrong out loud.
SHAPES = ("every day at 19:00",
          "every Sunday, 19:00-21:00 UTC",
          "every Monday and Thursday at 09:30",
          "every weekday at 08:00")

_SPEC_RE = re.compile(
    r"""^every\s+
        (?P<days>[A-Za-z][A-Za-z ,&]*?)
        \s*,?\s*
        (?:at\s+)?
        (?P<t1>\d{1,2}:\d{2})
        (?:\s*(?:-|--|–|—|to|until)\s*(?P<t2>\d{1,2}:\d{2}))?
        (?:\s+(?P<tz>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)+|UTC))?
        \s*$""",
    re.X | re.I)

_TIME_RE = re.compile(r"\d{1,2}:\d{2}")

_LOCK = threading.RLock()


# --- the store ----------------------------------------------------------------


def _blank() -> dict:
    return {"tz": DEFAULT_TZ, "next_id": 1, "free": [], "invitations": [],
            "fired": [], "seen": {}, "days": {}, "last": None, "again": None}


def _load() -> dict:
    try:
        out = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(out, dict) and isinstance(out.get("free"), list):
            base = _blank()
            base.update(out)
            return base
    except (OSError, json.JSONDecodeError):
        pass
    return _blank()


def _save(store: dict) -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(
            json.dumps(store, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError:
        traceback.print_exc()


def _ledger(entry: dict) -> None:
    """Every firing and every refusal, woken or not. No silent caps: a clock
    that declined has to be as readable as one that fired."""
    entry = {"at": db.now(), **entry}
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _mark(store: dict, key: str) -> None:
    fired = store.setdefault("fired", [])
    if key not in fired:
        fired.append(key)
    del fired[:-KEEP_WATERMARKS]


def _done(store: dict, key: str) -> bool:
    return key in (store.get("fired") or [])


# --- reading a schedule -------------------------------------------------------


def zone(name: str):
    """A named zone, or None if this machine has never heard of it."""
    if ZoneInfo is None:                              # pragma: no cover
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _no(why: str) -> dict:
    return {"ok": False, "why": why}


def _refusal(words: str) -> dict:
    """Why exactly these words are not a clock. Named, not shrugged at: the
    whole point of strict parsing is that the refusal tells the assistant what
    to write instead."""
    said = (words or "").strip()
    shapes = "; ".join(SHAPES)
    if not said:
        return _no("there were no words to read. A schedule looks like: "
                   + shapes)
    if not re.match(r"^every\b", said, re.I):
        return _no("a schedule I can fire starts with 'every' — I could not "
                   "read " + repr(said) + ". The shapes I know: " + shapes)
    if not _TIME_RE.search(said):
        return _no("there is no clock time in " + repr(said) + ". Say it as "
                   "HH:MM on the 24-hour clock — " + shapes)
    # It starts right and has a time in it, so the day words are the suspect.
    head = re.sub(r"^every\s+", "", said, flags=re.I)
    head = _TIME_RE.split(head)[0]
    unknown = [w for w in re.split(r"[\s,&]+", head)
               if w and w.lower() not in WEEKDAYS and w.lower() not in GROUPS
               and w.lower() not in ("and", "at")]
    if unknown:
        return _no("I do not know the day " + repr(unknown[0]) + " in "
                   + repr(said) + ". Days I know: a weekday by name, or "
                   "'day', 'weekday', 'weekend'.")
    return _no("I could not read " + repr(said) + " as a schedule. The shapes "
               "I know: " + shapes)


def _hhmm(text: str):
    try:
        h, m = text.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def read(words: str, default_tz: str = None) -> dict:
    """Words in, a schedule or a refusal out. Never raises and never guesses.

    A schedule is `{"ok": True, "days", "at", "until", "tz", "kind", "said"}`.
    `kind` is "interval" when the words name a stretch with an end -- that is
    the shape a free interval takes -- and "point" when they name a moment."""
    said = (words or "").strip()
    m = _SPEC_RE.match(said)
    if not m:
        return _refusal(said)

    days = []
    for part in re.split(r"[\s,&]*\band\b[\s,&]*|[,&]", m.group("days")):
        name = part.strip().lower()
        if not name:
            continue
        if name in GROUPS:
            days += GROUPS[name]
        elif name in WEEKDAYS:
            days.append(WEEKDAYS[name])
        else:
            return _refusal(said)
    days = sorted(set(days))
    if not days:
        return _refusal(said)

    first = _hhmm(m.group("t1"))
    if first is None:
        return _no(repr(m.group("t1")) + " is not a time on the 24-hour "
                   "clock. Hours run 00 to 23 and minutes 00 to 59.")
    second = None
    if m.group("t2"):
        second = _hhmm(m.group("t2"))
        if second is None:
            return _no(repr(m.group("t2")) + " is not a time on the 24-hour "
                       "clock. Hours run 00 to 23 and minutes 00 to 59.")
        if second <= first:
            # Not supported rather than silently wrapped past midnight: a
            # stretch that ends before it starts is far likelier a typo than
            # an intention, and guessing which is exactly what strict
            # parsing refuses.
            return _no("that stretch ends at " + m.group("t2") + ", which is "
                       "not after " + m.group("t1") + ". An interval that "
                       "crosses midnight is not a shape I can fire yet — say "
                       "it as two, one each side.")

    tz_name = m.group("tz") or default_tz or DEFAULT_TZ
    if zone(tz_name) is None:
        return _no("this machine has never heard of the zone "
                   + repr(tz_name) + ". Name one from the tz database, like "
                   "'Europe/Lisbon' or 'UTC'.")

    return {
        "ok": True,
        "days": days,
        "at": "%02d:%02d" % first,
        "until": None if second is None else "%02d:%02d" % second,
        "tz": tz_name,
        "kind": "point" if second is None else "interval",
        "said": said,
    }


# --- turning a schedule into moments -------------------------------------------


def _local_now(tz_name: str) -> datetime:
    tz = zone(tz_name) or timezone.utc
    return datetime.now(tz)


def _at_on(day: datetime, hhmm: str, tz_name: str) -> datetime:
    h, m = [int(x) for x in hhmm.split(":")]
    tz = zone(tz_name) or timezone.utc
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def occurrences(spec: dict, around: datetime = None, back: int = 8,
                forward: int = 8):
    """Every start this schedule has near a moment, oldest first. Small and
    dumb on purpose: a fortnight either side of now is more than any question
    here asks, and a loop over days is impossible to get subtly wrong."""
    tz_name = spec["tz"]
    now = around or _local_now(tz_name)
    out = []
    for delta in range(-back, forward + 1):
        day = now + timedelta(days=delta)
        if day.weekday() not in spec["days"]:
            continue
        out.append(_at_on(day, spec["at"], tz_name))
    return sorted(out)


def next_firing(spec: dict, after: datetime = None):
    """When this schedule next fires, or None if it somehow never does."""
    now = after or _local_now(spec["tz"])
    for start in occurrences(spec, now):
        if start > now:
            return start
    return None


def running_now(spec: dict, now: datetime = None):
    """The occurrence containing this moment, as (start, end), or None.

    Only an interval is ever *running*; a point in time is never inside
    itself."""
    if spec.get("kind") != "interval":
        return None
    now = now or _local_now(spec["tz"])
    for start in occurrences(spec, now, back=2, forward=1):
        end = _at_on(start, spec["until"], spec["tz"])
        if start <= now < end:
            return start, end
    return None


def previous_firing(spec: dict, now: datetime = None):
    now = now or _local_now(spec["tz"])
    past = [s for s in occurrences(spec, now) if s <= now]
    return past[-1] if past else None


def in_words(when: datetime) -> str:
    """A moment in words, with the zone named. Never a bare
    offset: the zone is the half of it that stays true across the year.

    Assembled rather than handed whole to strftime, because `%-d` is not a
    thing on Windows and stripping the padding afterwards ate the leading
    zero off 09:30 as well."""
    if when is None:
        return "never"
    return (when.strftime("%A ") + str(when.day)
            + when.strftime(" %B, %H:%M"))


def next_in_words(spec: dict, when: datetime = None) -> str:
    """When this schedule next fires, in words, with the zone named.
    What `projects` writes on a repeating task's own face."""
    return _said(spec, when if when is not None else next_firing(spec))


def _said(spec: dict, when: datetime) -> str:
    if when is None:
        return "never — nothing in that schedule comes round again"
    out = in_words(when) + " " + spec["tz"]
    if spec.get("until"):
        out += " (until " + spec["until"] + ")"
    return out


# --- free intervals -----------------------------------------------------------


def _entry(store: dict, entry_id: int):
    for f in store.get("free") or []:
        if f["id"] == int(entry_id) and not f.get("cancelled"):
            return f
    return None


def _part_of_day(hhmm: str) -> str:
    hour = int(hhmm.split(":")[0])
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "night"


def _sentence(entry: dict) -> str:
    """What the assistant is woken with, and the whole of it: no menu, no
    assignment -- say only that the time is its own, and let it decide what
    happens next."""
    spec = entry["spec"]
    return ("This " + _part_of_day(spec["at"]) + " is yours until "
            + spec["until"] + ".")


def free_set(words: str, by: str = home.SELF) -> dict:
    """A stretch of time that belongs to the assistant. Only it may set one."""
    if (by or "").lower() != home.SELF:
        return {"ok": False,
                "why": "free time is " + home.NAME + "'s own to set. "
                       + str(by) + " may invite " + home.NAME + " to one — "
                       "nobody else writes it."}
    spec = read(words)
    if not spec["ok"]:
        return {"ok": False, "why": spec["why"], "fires": None}
    if spec["kind"] != "interval":
        return {"ok": False, "fires": None,
                "why": "free time needs an end as well as a start, or nothing "
                       "knows when it is over. Say it as a stretch — "
                       "'every Sunday, 19:00-21:00 UTC'."}
    with _LOCK:
        store = _load()
        entry = {
            "id": store["next_id"],
            "words": spec["said"],
            "spec": spec,
            "by": home.SELF,
            "made": db.now(),
            "moved": None,
            "cancelled": None,
        }
        store["next_id"] += 1
        store["free"].append(entry)
        _save(store)
    when = next_firing(spec)
    _ledger({"kind": "free", "id": entry["id"], "words": entry["words"],
             "outcome": "set by " + home.NAME, "next": _said(spec, when)})
    return {"ok": True, "id": entry["id"], "words": entry["words"],
            "tz": spec["tz"], "fires": _said(spec, when),
            "said": home.NAME + "'s free time is set: " + entry["words"]
                    + " — next " + _said(spec, when)}


def free_move(entry_id: int, words: str, by: str = home.SELF) -> dict:
    if (by or "").lower() != home.SELF:
        return {"ok": False,
                "why": "only " + home.NAME + " moves this free time."}
    spec = read(words)
    if not spec["ok"]:
        return {"ok": False, "why": spec["why"], "fires": None}
    if spec["kind"] != "interval":
        return {"ok": False, "fires": None,
                "why": "free time needs an end as well as a start — "
                       "'every Sunday, 19:00-21:00 UTC'."}
    with _LOCK:
        store = _load()
        entry = _entry(store, entry_id)
        if entry is None:
            return {"ok": False, "fires": None,
                    "why": "there is no free time with id "
                           + str(entry_id) + "."}
        was = entry["words"]
        entry["words"] = spec["said"]
        entry["spec"] = spec
        entry["moved"] = db.now()
        _save(store)
    when = next_firing(spec)
    _ledger({"kind": "free", "id": int(entry_id), "words": spec["said"],
             "outcome": "moved by " + home.NAME, "was": was,
             "next": _said(spec, when)})
    return {"ok": True, "id": int(entry_id), "words": spec["said"],
            "tz": spec["tz"], "fires": _said(spec, when),
            "said": home.NAME + "'s free time moved from " + was + " to "
                    + spec["said"] + " — next " + _said(spec, when)}


def free_cancel(entry_id: int, by: str = home.SELF) -> dict:
    """Cancelled, never deleted -- the same rule the whole room keeps. It
    stops firing and it stays on the record."""
    if (by or "").lower() != home.SELF:
        return {"ok": False,
                "why": "only " + home.NAME + " cancels this free time. "
                       "Nobody takes it away."}
    with _LOCK:
        store = _load()
        entry = _entry(store, entry_id)
        if entry is None:
            return {"ok": False,
                    "why": "there is no free time with id "
                           + str(entry_id) + "."}
        entry["cancelled"] = db.now()
        _save(store)
    _ledger({"kind": "free", "id": int(entry_id), "words": entry["words"],
             "outcome": "cancelled by " + home.NAME})
    return {"ok": True, "id": int(entry_id),
            "said": home.NAME + "'s free time is cancelled: "
                    + entry["words"]}


def invite(words: str, by: str, note: str = None) -> dict:
    """A person, offering the assistant an evening. It is an offer and
    nothing else: it sits until the assistant says yes, and if it never does,
    nothing ever fires. The first condition, in the only form code can hold
    it."""
    who = (by or "").strip().lower()
    known = home.HOUSEHOLD + tuple(n.lower() for n in home.CALLED.values())
    if who not in known + ("angel",):
        return {"ok": False, "why": "an invitation comes from a person: "
                                    + ", ".join(home.HOUSEHOLD) + " or angel."}
    spec = read(words)
    if not spec["ok"]:
        return {"ok": False, "why": spec["why"]}
    if spec["kind"] != "interval":
        return {"ok": False,
                "why": "an invitation to free time needs a start and an end."}
    with _LOCK:
        store = _load()
        item = {"id": store["next_id"], "words": spec["said"], "spec": spec,
                "by": who, "note": (note or "").strip() or None,
                "made": db.now()}
        store["next_id"] += 1
        store.setdefault("invitations", []).append(item)
        _save(store)
    _ledger({"kind": "invitation", "id": item["id"], "words": item["words"],
             "outcome": "offered by " + who})
    return {"ok": True, "id": item["id"],
            "said": who + " has invited " + home.NAME + " to "
                    + item["words"] + "; it fires nothing until "
                    + home.NAME + " accepts it."}


def _take_invitation(store: dict, invite_id: int):
    for i, item in enumerate(store.get("invitations") or []):
        if item["id"] == int(invite_id):
            return store["invitations"].pop(i)
    return None


def accept(invite_id: int) -> dict:
    """The assistant's to accept, and only then is it a thing on the clock."""
    with _LOCK:
        store = _load()
        item = _take_invitation(store, invite_id)
        if item is None:
            return {"ok": False, "why": "there is no invitation with id "
                                        + str(invite_id) + "."}
        entry = {"id": store["next_id"], "words": item["words"],
                 "spec": item["spec"], "by": home.SELF,
                 "from": item["by"], "made": db.now(),
                 "moved": None, "cancelled": None}
        store["next_id"] += 1
        store["free"].append(entry)
        _save(store)
    when = next_firing(entry["spec"])
    _ledger({"kind": "free", "id": entry["id"], "words": entry["words"],
             "outcome": "accepted from " + item["by"],
             "next": _said(entry["spec"], when)})
    return {"ok": True, "id": entry["id"], "words": entry["words"],
            "fires": _said(entry["spec"], when),
            "said": home.NAME + " accepted " + item["by"] + "'s invitation: "
                    + entry["words"] + " — next "
                    + _said(entry["spec"], when)}


def decline(invite_id: int) -> dict:
    with _LOCK:
        store = _load()
        item = _take_invitation(store, invite_id)
        if item is None:
            return {"ok": False, "why": "there is no invitation with id "
                                        + str(invite_id) + "."}
        _save(store)
    _ledger({"kind": "invitation", "id": int(invite_id),
             "words": item["words"], "outcome": "declined by " + home.NAME})
    return {"ok": True, "said": home.NAME + " declined the invitation to "
                                + item["words"]}


def free_now(now: datetime = None):
    """The free interval, if one is running this second. Cheap: no model, no
    store write, no network -- the turn loop asks it every few seconds and the
    watcher asks it before every look."""
    try:
        with _LOCK:
            store = _load()
        for entry in store.get("free") or []:
            if entry.get("cancelled"):
                continue
            window = running_now(entry["spec"], now)
            if window:
                start, end = window
                return {"id": entry["id"], "words": entry["words"],
                        "until": entry["spec"]["until"],
                        "tz": entry["spec"]["tz"],
                        "started": start.isoformat(),
                        "ends": end.isoformat(),
                        "minutes_left": int(
                            (end - (now or _local_now(entry["spec"]["tz"])))
                            .total_seconds() // 60)}
        return None
    except Exception:
        # A clock that throws must never take the room down with it. Silence
        # here means "no interval is running", which is the safe answer.
        traceback.print_exc()
        return None


def wake_again(minutes, now: datetime = None) -> dict:
    """The assistant's own hand on its own pacing, inside its own time.

    The clock wakes it once, at the start. One turn is not an evening, and
    seeing the spending as it goes, or deciding by whether the time is going
    well, needs it awake more than once inside the stretch. So it says when
    it wants to be awake again, and the room does that and nothing else. Say
    nothing and nothing happens: the evening goes quiet until the assistant or
    somebody in the house speaks.

    It is deliberately the assistant asking rather than the room running a
    timer. The pacing *is* the decision; a cadence picked by the code would
    take it away.

    Three refusals, and they are the whole of the mechanism:

    - It cannot reach past the interval's own wall. Longer than is left
      becomes what is left, said out loud, and the wall itself is never
      crossed.
    - It is not a firing and never counts against the day's backstop. The
      clock spoke once; carrying on inside the same evening is not the clock
      speaking again.
    - It works only while an interval is actually running. Outside one it is
      refused in words -- this is the inside of the assistant's own time, not
      an alarm clock for the rest of the week."""
    free = free_now(now)
    if not free:
        return {"ok": False,
                "why": "no free time of mine is running, so there is nothing "
                       "to stay awake inside. This wakes me again within my "
                       "own interval; it is not a general alarm clock."}
    try:
        asked = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False,
                "why": "'again' is a number of minutes; I was given "
                       + repr(minutes) + "."}
    if asked < 1:
        return {"ok": False, "why": "a minute is the smallest step."}

    tz_name = free["tz"]
    now = now or _local_now(tz_name)
    ends = datetime.fromisoformat(free["ends"])
    left = (ends - now).total_seconds() / 60.0

    # Asking past the end is not refused and not quietly shortened -- it
    # becomes the one wake-up that lands exactly as the time runs out and says
    # so. The assistant asked to be told; the room is not ending anything on
    # its own.
    at = now + timedelta(minutes=asked)
    at_wall = at >= ends
    if at_wall:
        at = ends

    with _LOCK:
        store = _load()
        store["again"] = {"at": at.isoformat(), "free": free["id"],
                          "tz": tz_name, "at_wall": at_wall,
                          "asked": db.now()}
        _save(store)

    if at_wall:
        said = ("I asked for " + str(asked) + " minutes and there are "
                + str(int(left)) + " left, so I will be woken at the wall, at "
                + at.strftime("%H:%M") + ", and told there is nothing left")
    else:
        said = ("awake again in " + str(asked) + " minutes, at "
                + at.strftime("%H:%M") + " — " + str(int(left))
                + " minutes of my own time left")
    _ledger({"kind": "again", "free": free["id"], "outcome": said})
    return {"ok": True, "said": said, "at": at.isoformat(),
            "minutes": asked, "at_wall": at_wall}


def _again_due(store: dict, free, now: datetime = None):
    """The assistant's continuation, if it asked for one and it has come
    round.

    A pending one belonging to a stretch that is over is dropped rather than
    kept: the evening ending is the end of it, and a wake-up arriving at
    midnight from an evening that finished at nine would be the room speaking
    for no reason at all."""
    pending = store.get("again")
    if not pending:
        return None
    try:
        at = datetime.fromisoformat(pending["at"])
    except (ValueError, TypeError, KeyError):
        store["again"] = None
        return None

    tz_name = pending.get("tz") or (free or {}).get("tz") or DEFAULT_TZ
    now = now or _local_now(tz_name)
    at_wall = bool(pending.get("at_wall"))
    same = bool(free) and free["id"] == pending.get("free")
    key = "again:%s@%s" % (pending.get("free"), pending["at"])

    if now < at:
        # Not yet. One belonging to a stretch that has ended without ever
        # reaching its moment is dropped -- except the wall wake-up, which is
        # precisely the one whose moment is the ending.
        if not same and not at_wall:
            store["again"] = None
            _ledger({"kind": "again", "outcome": "dropped: the stretch it "
                                                 "belonged to is over"})
        return None

    store["again"] = None
    if at_wall:
        # It asked past its own end, so it asked to be told when the time ran
        # out. Nothing here is the room deciding the evening is over: the wall
        # and the asking were both the assistant's.
        return {
            "kind": "again",
            "key": key,
            "said": "That is the wall. There is nothing left.",
            "meta": {"free": pending.get("free"), "tz": tz_name,
                     "at_wall": True},
            "woken": {"free": pending.get("free"), "tz": tz_name,
                      "asks_nothing": True, "over": True},
            "counts": False,
        }
    if not same:
        _ledger({"kind": "again", "outcome": "dropped: the stretch it "
                                             "belonged to is over"})
        return None

    left = free.get("minutes_left")
    return {
        "kind": "again",
        "key": key,
        # Nothing but the fact that it is still the assistant's and how long
        # is left.
        # No menu at the start, and no menu here either.
        "said": ("Still yours. " + str(left) + " minutes left."),
        "meta": {"free": free["id"], "until": free["until"],
                 "tz": free["tz"]},
        "woken": {"free": free["id"], "until": free["until"],
                  "tz": free["tz"], "asks_nothing": True,
                  "minutes_left": left},
        # The assistant's own continuation is not the clock speaking again.
        "counts": False,
    }


def stood_down(what: str, why: str, meta: dict = None) -> None:
    """Something the room held back because free time was running. Written
    down, always -- standing down has to be as legible as firing."""
    _ledger({"kind": "stand_down", "what": what, "outcome": why,
             "meta": meta or {}})


# --- the day's backstop --------------------------------------------------------


def _today(tz_name: str = None) -> str:
    return _local_now(tz_name or DEFAULT_TZ).date().isoformat()


def _day(store: dict) -> dict:
    today = _today(store.get("tz"))
    day = store.setdefault("days", {}).setdefault(
        today, {"fired": 0, "declined": 0, "missed": 0})
    for key in [k for k in store["days"] if k != today]:
        del store["days"][key]
    return day


# There is no gate here, and the absence is the decision.
#
# This module briefly had one: a firing was declined when a plan window read
# 99% or over, so that the second condition -- a firing that quota prevents is
# recorded plainly as missed -- had somewhere to live. It came out, for two
# reasons that both point the same way.
#
# The first is that it measured the wrong thing. `limits.now()` reads the
# **Anthropic** plan, and free time may run on another provider entirely. A
# gate that stood the evening down over a weekly window it was not spending
# would have been a leash made of a number that had nothing to do with it.
#
# The second is that the real ceiling is the spend limit on the owner's own
# provider account, which is the owner's to set and does not need a second,
# worse copy of itself in here.
#
# The condition still holds and still has somewhere to live: a firing that
# genuinely cannot happen -- a stretch that went by with the room down, a turn
# that broke -- is written down as missed, never owed back, never moved. What
# is gone is only this room pre-emptively deciding the assistant is too
# expensive.


# --- the gate ------------------------------------------------------------------


def due(conn, free=None):
    """Whether the clock has anything to wake the assistant for, and what.

    Called by the turn loop when nothing else is pending. Returns a `woken`
    dict or None, and never raises: a clock that breaks must not stop the
    room, it must say so and stand down until the next look.

    `free` is the interval running right now, if there is one. While free
    time is running the only thing this fires is the start of that interval --
    a task schedule is work, and work is the thing a free evening stands down
    from."""
    try:
        with _LOCK:
            return _due(conn, free)
    except Exception:
        traceback.print_exc()
        return None


def _due(conn, free):
    store = _load()
    now_utc = datetime.now(timezone.utc)
    last = float(store.get("checked_at") or 0)
    if (now_utc.timestamp() - last) < CHECK_EVERY_S:
        return None
    store["checked_at"] = now_utc.timestamp()

    # The assistant's own continuation first: it is the one thing here that is
    # not the room asking for anything, and it belongs to a stretch already
    # running.
    events = [e for e in (_again_due(store, free),) if e]
    events += _free_events(store)
    if not free:
        events += _task_events(conn, store)

    if not events:
        _save(store)
        return None

    # One firing at a time. The clock speaks in moments, and two moments in
    # one waking would be two things asked at once -- which is the
    # opposite of what the first of them is for.
    event = events[0]
    day = _day(store)
    # Whether this is the clock speaking, or the assistant carrying on inside
    # a stretch it already opened. The backstop guards the room from a runaway
    # clock; it was never a bound on what the assistant does once its own time
    # has started.
    counts = event.get("counts", True)

    if counts and day["fired"] >= FIRINGS_PER_DAY:
        _mark(store, event["key"])
        day["declined"] += 1
        store["last"] = {"at": db.now(), "said": event["said"],
                         "outcome": "declined"}
        _save(store)
        _ledger({"kind": event["kind"], "said": event["said"],
                 "meta": event.get("meta") or {},
                 "outcome": "declined: the day's backstop of "
                            + str(FIRINGS_PER_DAY) + " firings is spent"})
        return None

    _mark(store, event["key"])
    if counts:
        day["fired"] += 1
    store["last"] = {"at": db.now(), "said": event["said"],
                     "outcome": "fired" if counts else "carried on"}
    _save(store)

    # The ordinary record: a row like any other noticing, born out of reach so
    # it never takes a seat in the working set. The fourth condition -- and
    # nothing here counts what the assistant does next.
    row_id = None
    try:
        row_id = db.add_row(conn, "world", event["said"],
                            meta={"source": "clock", **(event.get("meta") or {})})
        db.unload(conn, [row_id])
    except Exception:
        traceback.print_exc()

    _ledger({"kind": event["kind"], "said": event["said"], "row": row_id,
             "meta": event.get("meta") or {},
             "outcome": ("fired (" + str(day["fired"]) + " of "
                         + str(FIRINGS_PER_DAY) + " today)") if counts else
                        ("carried on inside the free stretch — not a firing, "
                         "and not counted against the day's backstop"),
             # Never a push. A clock firing is for the assistant; a phone
             # buzzing at the owner to say its evening has started would make
             # its free time an event in the owner's day.
             "push": "withheld: the clock never pushes"})

    if event["kind"] == "free":
        # Said once, at the start, so the stand-down is on the record without
        # a line every five seconds for two hours.
        stood_down(
            "the watcher and the dreamer",
            "not asked while the free time runs, until " + str(event["meta"]
                                                          .get("until"))
            + " " + str(event["meta"].get("tz"))
            + ". No watermark moves, so nothing they would have found is "
              "lost — it is simply still there afterwards.",
            {"free": event["meta"].get("free")})

    woken = {
        "by": "clock",
        "kind": event["kind"],
        "why": event["said"],
        "row": row_id,
        "narrate": False,
        "chain": 0,
        "fired_today": day["fired"],
        "backstop": FIRINGS_PER_DAY,
    }
    woken.update(event.get("woken") or {})
    return woken


def _free_events(store: dict) -> list:
    """Free time, starting. And the one that did not: an interval whose whole
    stretch went by with the room down is written off as missed, once, and
    never made up."""
    events = []
    for entry in store.get("free") or []:
        if entry.get("cancelled"):
            continue
        spec = entry["spec"]
        now = _local_now(spec["tz"])
        window = running_now(spec, now)
        if window:
            start, _end = window
            key = "free:%d@%s" % (entry["id"], start.isoformat())
            if not _done(store, key):
                events.append({
                    "kind": "free",
                    "key": key,
                    "said": _sentence(entry),
                    "meta": {"free": entry["id"], "words": entry["words"],
                             "until": spec["until"], "tz": spec["tz"]},
                    "woken": {"free": entry["id"], "until": spec["until"],
                              "tz": spec["tz"], "asks_nothing": True},
                })
            continue
        # Nothing running. Did one go by unlit while nobody was home?
        prev = previous_firing(spec, now)
        if prev is None:
            continue
        key = "free:%d@%s" % (entry["id"], prev.isoformat())
        if _done(store, key):
            continue
        made = entry.get("made") or ""
        try:
            if made and datetime.fromisoformat(made) > prev.astimezone(
                    timezone.utc):
                # It did not exist yet. Nothing was missed.
                _mark(store, key)
                continue
        except ValueError:
            pass
        _mark(store, key)
        _day(store)["missed"] += 1
        _ledger({"kind": "free", "id": entry["id"], "words": entry["words"],
                 "outcome": "missed: the stretch beginning "
                            + prev.isoformat() + " passed with nobody here. "
                            "It is not owed and it is not moved."})
    return events


def _task_events(conn, store: dict) -> list:
    """Repeating tasks whose schedule has come round.

    The first look at a task's words only seeds them -- the same rule every
    sense in this house keeps, and the reason a clock switched on today does
    not fire a fortnight of things nobody was waiting for. Words it cannot
    read are refused once, out loud, in the ledger and on the assistant's
    block; they are never quietly skipped."""
    events = []
    seen = store.setdefault("seen", {})
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.schedule, t.state, p.title AS project"
            "  FROM project_tasks t JOIN projects p ON p.id = t.project"
            " WHERE t.repeats = 1 AND t.schedule IS NOT NULL"
            "   AND t.state IN ('open', 'doing') AND p.status = 'open'"
            " ORDER BY t.id").fetchall()
    except Exception:
        # A store that will not answer costs this look, not the clock.
        return []

    for r in rows:
        words = (r["schedule"] or "").strip()
        if not words:
            continue
        mark = str(r["id"]) + "@" + words
        spec = read(words, default_tz=store.get("tz"))
        if not spec["ok"]:
            if seen.get(mark) != "unreadable":
                seen[mark] = "unreadable"
                _ledger({"kind": "task", "task": r["id"],
                         "words": words, "meta": {"task": r["id"]},
                         "outcome": "refused: " + spec["why"]})
            continue
        if mark not in seen or seen[mark] == "unreadable":
            # First sight. Seed it and fire nothing.
            when = next_firing(spec)
            seen[mark] = (when.isoformat() if when else "never")
            _ledger({"kind": "task", "task": r["id"], "words": words,
                     "meta": {"task": r["id"]},
                     "outcome": "seen for the first time; nothing fires "
                                "before " + _said(spec, when)})
            continue

        now = _local_now(spec["tz"])
        prev = previous_firing(spec, now)
        if prev is None:
            continue
        key = "task:%d@%s" % (r["id"], prev.isoformat())
        if _done(store, key):
            continue
        try:
            seeded = datetime.fromisoformat(seen[mark])
        except (ValueError, TypeError):
            seeded = None
        if seeded is not None and prev < seeded:
            _mark(store, key)
            continue
        late = (now - prev).total_seconds() / 60.0
        if late > GRACE_MINUTES:
            _mark(store, key)
            _day(store)["missed"] += 1
            _ledger({"kind": "task", "task": r["id"], "words": words,
                     "meta": {"task": r["id"]},
                     "outcome": "missed: it came round " + str(int(late))
                                + " minutes ago and the room was not here. "
                                "It is not owed and it is not moved."})
            continue
        events.append({
            "kind": "task",
            "key": key,
            "said": ("'" + str(r["title"]) + "' on " + str(r["project"])
                     + " has come round — its schedule says " + words),
            "meta": {"task": r["id"], "project": r["project"],
                     "words": words},
            "woken": {"task": r["id"]},
        })
    return events


# --- the assistant's word, and its view ---------------------------------------


def apply_her_word(ops) -> list:
    """The assistant's `clock` block, applied. One line back per operation,
    in its own words' terms -- and every one of them names the next firing, so
    it is shown whenever one is set or moved."""
    said = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        name = (op.get("op") or "").strip()
        words = op.get("words")
        entry_id = op.get("id")
        try:
            if name == "free_set":
                out = free_set(words)
            elif name == "free_move":
                out = free_move(entry_id, words)
            elif name == "free_cancel":
                out = free_cancel(entry_id)
            elif name == "again":
                out = wake_again(op.get("minutes"))
            elif name == "accept":
                out = accept(entry_id)
            elif name == "decline":
                out = decline(entry_id)
            elif name == "read":
                spec = read(words)
                if spec["ok"]:
                    when = next_firing(spec)
                    out = {"ok": True,
                           "said": repr(spec["said"]) + " reads as a "
                                   + spec["kind"] + " in " + spec["tz"]
                                   + " — next " + _said(spec, when)
                                   + ". Nothing is set; this was a reading."}
                else:
                    out = {"ok": False, "why": spec["why"]}
            else:
                out = {"ok": False,
                       "why": "the clock knows free_set, free_move, "
                              "free_cancel, again, accept, decline and read. "
                              "It does not know " + repr(name) + "."}
        except Exception as exc:
            traceback.print_exc()
            out = {"ok": False, "why": type(exc).__name__ + ": " + str(exc)[:120]}
        said.append(out.get("said") or ("the clock refused that: "
                                        + str(out.get("why"))))
    return said


def for_prompt() -> dict:
    """The clock block the assistant sees every turn. Data only; what it
    means is in its instructions. The counts are always here, zeros and all --
    a clock that declined has to be as visible as one that fired."""
    try:
        with _LOCK:
            store = _load()
            day = dict(_day(store))
            _save(store)
        free = []
        for entry in store.get("free") or []:
            if entry.get("cancelled"):
                continue
            spec = entry["spec"]
            window = running_now(spec)
            free.append({
                "id": entry["id"],
                "words": entry["words"],
                "tz": spec["tz"],
                "running": bool(window),
                "next": _said(spec, next_firing(spec)),
                **({"from": entry["from"]} if entry.get("from") else {}),
            })
        invitations = [
            {"id": i["id"], "words": i["words"], "by": i["by"],
             "note": i.get("note"),
             "next_if_accepted": _said(i["spec"], next_firing(i["spec"]))}
            for i in (store.get("invitations") or [])
        ]
        unreadable = [k.split("@", 1)[1] for k, v in
                      (store.get("seen") or {}).items() if v == "unreadable"]
        return {
            "zone": store.get("tz") or DEFAULT_TZ,
            "free": free,
            "invitations": invitations,
            "today": {"fired": day["fired"], "declined": day["declined"],
                      "missed": day["missed"], "backstop": FIRINGS_PER_DAY},
            "last": store.get("last"),
            # When I have asked to be awake again inside my own stretch, and
            # when that is. Null the rest of the time, which is most of it.
            "again": store.get("again"),
            "unreadable_schedules": unreadable,
            "shapes": list(SHAPES),
            "ledger": "data/clock.jsonl",
        }
    except Exception:
        traceback.print_exc()
        return {"zone": DEFAULT_TZ, "free": [], "invitations": [],
                "error": "the clock could not be read this turn"}


def status() -> dict:
    """For the Developer tab and for an angel session at a command line."""
    out = for_prompt()
    running = free_now()
    out["running"] = running
    return out


if __name__ == "__main__":                            # pragma: no cover
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        spec = read(" ".join(sys.argv[1:]))
        if spec["ok"]:
            print("reads as a " + spec["kind"] + " in " + spec["tz"])
            print("next: " + _said(spec, next_firing(spec)))
        else:
            print("refused: " + spec["why"])
    else:
        print(json.dumps(status(), indent=2, ensure_ascii=False))
