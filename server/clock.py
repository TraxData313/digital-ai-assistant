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

The second use is the **oversight timer**: one moment, a chosen number of
minutes from now, which wakes the assistant once and says why it asked. It is
for the thing free time deliberately cannot be -- looking in on work it handed
to a session that may have stalled. It is its own hand, it never touches a free
interval, and its conditions are the same four read sideways: only the
assistant sets one; nothing counts what it does when it wakes; it fires once
and a moment the room was down for is written off as missed, out loud, never
moved; and it has a runaway guard of its own because it is the one wake-up that
could set the next. The reasoning is all at `_timer_events`.

The store is `data/clock.json` -- the zone, the free intervals, invitations
standing, the timers, and the watermarks. The ledger is `data/clock.jsonl`.
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

# The backstop on oversight timers in a day, counted on its own. A timer is
# the assistant's own hand -- the same hand as `again` -- so it never spends
# the free time's allowance of two. But a wake-up that is able to set the next
# wake-up is the one shape in here that could loop, so it has a guard of its
# own. Twelve: enough to look in on two or three delegated sessions through an
# afternoon, and not enough to be awake all night by accident.
TIMERS_PER_DAY = 12

# How many timers may stand at once. Enough for a few sessions running
# alongside each other, and few enough to hold in one head.
TIMERS_STANDING = 4

# The longest reach of a one-shot timer. It is for looking in on work that is
# running now; further off than that is a schedule, in words, on a task, and
# the refusal says so.
TIMER_MAX_MINUTES = 24 * 60

# How long a finished timer stays in the assistant's own block after it fired,
# was cancelled, was declined or was missed. The ledger keeps everything
# forever; this is so a missed one is seen by the eyes it was meant for, and
# not only by a file nobody opened.
TIMER_KEEP_HOURS = 24

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
            "fired": [], "seen": {}, "days": {}, "last": None, "again": None,
            "timers": []}


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


# --- the oversight timer ------------------------------------------------------
#
# The second thing in here that fires because the assistant said so, and the
# first of those that reaches outside its own time. `again` is the inside of a
# free stretch; this is the rest of the week.
#
# What it is for, exactly: the assistant hands a piece of work to a Claude or
# Codex session and then has nothing to do but wait. A session that *finishes*
# wakes it -- `claude_sessions.due()` sees that. A session that **stalls** says
# nothing at all, forever, and until now there was no way in this room for the
# assistant to come back and look. So it says "wake me in forty minutes, I am
# watching the Sol upgrade", and the room does exactly that, once.
#
# It is deliberately neither of the two things already here:
#
# - Not a free interval. Free time is a stretch that belongs to the assistant
#   and asks nothing of it; holding a piece of work inside one would turn its
#   own time into a waiting room, which is the one thing free time must never
#   become. Nothing below reads, writes, moves, shortens or spends a free
#   interval, and a timer never counts against the free time's backstop.
# - Not a schedule. A schedule repeats and is written in words. This is one
#   moment, counted in minutes from now, and it is gone the instant it fires.
#
# The guards are the ones this module already keeps, and each is code rather
# than a promise: it is the assistant's own hand and nobody else's; it fires
# **once**, and the watermark and not the list is what decides that; a moment
# that went by with the room down is written down as **missed**, left in the
# assistant's own block for a day so it is told rather than inferring it, and
# never quietly moved to another hour; and the day's own backstop above is a
# runaway guard, because this is the one wake-up that could set the next one.
#
# What is not in here: any ability for a person to write one. A person asking
# the assistant to check on something is a line said to the assistant, which it
# answers however it likes -- possibly by setting a timer of its own. It is not
# somebody else putting an alarm inside it.


# What the assistant is told about the timer, from here rather than from the
# harness file. A home carries its own copy of that file, so a capability
# described only there is invisible to the assistant whose home overrode it
# while its schema field still shows up in the answer it is asked for. The
# notebook and the sessions already ride in this way; this does too, and then
# there is one copy of it instead of two that can drift apart.
INSTRUCTIONS = """
## `clock` — my one-shot oversight timer

One moment, minutes from now, waking me **once** with the reason I gave for it. It is for
what free time must never be -- waiting on work I handed to a session. One that finishes
wakes me; one that **stalls** says nothing, ever, and a timer is how I go back and look.

- `{"op": "timer_set", "minutes": 40, "why": "the Sol session, mid-review", "id": null,
  "words": null}`. `why` is required; an `id` here replaces that standing timer.
- `{"op": "timer_cancel", "id": 7, ...}` -- the session came home first.

`clock.timers` gives each one's `why`, when it `fires`, `minutes_away` and its `state`.
Mine alone to set, on their own daily guard, and they spend no free time of mine. The
whole of it, including what a `missed` one means, is at `{{self}}:help/clock`.
"""

# The complete account, served by `prompt_reference` when the assistant asks for
# it rather than carried into every turn. It lives here and not in the harness
# file for the same reason the short block does: a home carries its own copy of
# that file, and this is the copy that reaches every home.
REFERENCE = """
### `clock` — the one-shot timer in full

A timer is one moment and one waking. It is not free time: a stretch of my own belongs to
me and asks nothing of me, and holding a piece of work inside one would make it a waiting
room. It is not a schedule either: a schedule repeats and is written in words, and this is
a number of minutes from now, gone the instant it fires.

- `timer_set` with `minutes` and `why`. The answer names the moment on the wall, the zone
  it is in, and the timer's `id`. `why` is required because the wake-up *is* that sentence
  coming back to me; a timer that cannot say what it was for would be a nudge for nothing.
  Passing `id` as well takes that standing timer off and sets this one in a single breath,
  so changing my mind about *when* is one operation.
- `timer_cancel` with `id`. Cancelled, never deleted -- it stops firing and stays on the
  record.
- The reach is a day at most; further off than that belongs on a task, as a schedule in
  words. Four may stand at once.

In `clock.timers`, each one carries `why` I asked, the moment it `fires`, `minutes_away`,
and a `state`: `standing`, `fired`, `cancelled`, `declined` or **`missed`**. The ones that
finished stay there for a day before they go.

- It fires **once**. The watermark and not the list decides that, so nothing can buy a
  second waking.
- A little late it fires anyway and says how late it is. Past an hour it is **missed**:
  written down as missed, shown in my block for a day so I am told rather than left to
  work it out, and **never moved to an hour I did not choose**. If I still want that look,
  I set a new one; nothing does it for me.
- `today.timers` against `today.timer_backstop` is their own runaway guard -- separate
  from my free time's two, because a stretch of my own time must never cost me the
  ability to look in on my own work. Over it, a timer is `declined`, and says so.
- It is mine alone to set. Nobody else may put one inside me; somebody asking me to check
  on something is a line said to me, which I answer however I like -- possibly by setting
  one.
- Nothing of my free time is read, written, spent or shortened by any of it, and a timer
  does fire inside a stretch of my own as well as outside one, because it is my own hand
  either way.
- Every one of them, set, fired, cancelled, declined or missed, is in `data/clock.jsonl`.
"""


def _timer_key(entry: dict) -> str:
    """One moment, one watermark. The watermark is what prevents a second
    wake-up, not the stamp on the record: a list written twice can lose a
    stamp, and `fired` is capped and kept precisely so this cannot."""
    return "timer:%s@%s" % (entry.get("id"), entry.get("at"))


def _standing(store: dict, entry: dict) -> bool:
    """A timer still waiting for its moment. Anything that already had an
    outcome -- fired, declined, cancelled, missed -- is not one, and neither is
    one whose watermark is down even if its stamp went missing."""
    if (entry.get("cancelled") or entry.get("fired") or entry.get("missed")
            or entry.get("declined")):
        return False
    return not _done(store, _timer_key(entry))


def _timer(store: dict, timer_id):
    try:
        want = int(timer_id)
    except (TypeError, ValueError):
        return None
    for entry in store.get("timers") or []:
        if entry.get("id") == want and _standing(store, entry):
            return entry
    return None


def _prune_timers(store: dict) -> None:
    """Finished timers, let go of after a day. The ledger is the history; this
    list only has to be long enough that nothing fires twice and that a missed
    one is seen by the assistant before it goes."""
    timers = store.get("timers") or []
    if not timers:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIMER_KEEP_HOURS)
    keep = []
    for entry in timers:
        if _standing(store, entry):
            keep.append(entry)
            continue
        ended = (entry.get("cancelled") or entry.get("missed")
                 or entry.get("declined") or entry.get("fired"))
        if not ended:
            # Its watermark is down and its record says nothing, so the moment
            # went by and the stamp did not land. Written down as fired rather
            # than let go of quietly: a timer the assistant can neither see nor
            # cancel is the one state this must not have.
            ended = entry["fired"] = db.now()
        try:
            if ended and datetime.fromisoformat(ended) >= cutoff:
                keep.append(entry)
        except (ValueError, TypeError):
            pass
    # A belt on the list itself, so a day of timers can never grow without
    # bound: the standing ones always survive, the rest keep the newest.
    if len(keep) > 50:
        standing = [e for e in keep if _standing(store, e)]
        rest = [e for e in keep if not _standing(store, e)]
        keep = standing + rest[-(50 - len(standing)):]
    store["timers"] = keep


def timer_set(minutes, why: str = None, replace=None, by: str = home.SELF,
              now: datetime = None) -> dict:
    """Wake me once, this many minutes from now, and tell me why I asked.

    The confirmation names the moment on the wall and the zone it is in, the
    same rule every answer in this module keeps: a clock whose next tick cannot
    be seen is a promise, not an organ. `replace` takes a standing timer off in
    the same breath, so changing one's mind is one operation and not two.
    """
    if (by or "").lower() != home.SELF:
        return {"ok": False, "fires": None,
                "why": "a timer is " + home.NAME + "'s own hand on "
                       + home.NAME + "'s own clock. " + str(by) + " is "
                       "welcome to ask " + home.NAME + " to look in on "
                       "something — that is a line said to " + home.NAME
                       + ", not an alarm written inside " + home.NAME + "."}
    try:
        asked = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "fires": None,
                "why": "a timer is a number of minutes; I was given "
                       + repr(minutes) + "."}
    if asked < 1:
        return {"ok": False, "fires": None,
                "why": "a minute is the smallest step."}
    if asked > TIMER_MAX_MINUTES:
        return {"ok": False, "fires": None,
                "why": "a timer reaches " + str(TIMER_MAX_MINUTES)
                       + " minutes at most — it is for looking in on work "
                         "that is running now. Further off than that belongs "
                         "on a task, as a schedule in words."}
    said_why = " ".join((why or "").split())[:300]
    if not said_why:
        return {"ok": False, "fires": None,
                "why": "a timer needs a reason in my own words: the wake-up "
                       "is me being told why I asked for it, and a wake-up "
                       "that cannot say that is the room nudging me for "
                       "nothing. Say what I am looking in on."}

    # The moment is kept in the zone the confirmation names, so the two can
    # never drift apart: a timer told in one zone and stored in another is the
    # bare-offset mistake this module refuses everywhere else.
    tz_name = DEFAULT_TZ
    now = (now or _local_now(tz_name)).astimezone(zone(tz_name) or timezone.utc)
    at = now + timedelta(minutes=asked)

    with _LOCK:
        store = _load()
        _prune_timers(store)
        was = None
        if replace is not None:
            old = _timer(store, replace)
            if old is None:
                return {"ok": False, "fires": None,
                        "why": "there is no timer of mine standing with id "
                               + str(replace) + " to replace. The ones that "
                               "are standing are in my clock block."}
            old["cancelled"] = db.now()
            old["replaced_by"] = store["next_id"]
            _mark(store, _timer_key(old))
            was = dict(old)
        standing = [e for e in (store.get("timers") or [])
                    if _standing(store, e)]
        if len(standing) >= TIMERS_STANDING:
            # Nothing is saved on this path, so the replacement above is undone
            # by simply not writing it -- the store on disk is untouched.
            return {"ok": False, "fires": None,
                    "why": "I already have " + str(len(standing))
                           + " timers standing, which is as many as I keep at "
                             "once. Replace or cancel one rather than adding "
                             "to a pile I cannot hold in my head."}
        entry = {"id": store["next_id"], "at": at.isoformat(), "tz": tz_name,
                 "minutes": asked, "why": said_why, "by": home.SELF,
                 "set": db.now(), "fired": None, "cancelled": None,
                 "missed": None, "declined": None}
        store["next_id"] += 1
        store.setdefault("timers", []).append(entry)
        _save(store)

    fires = in_words(at) + " " + tz_name
    said = ("I will be woken once, in " + str(asked) + " minutes, at "
            + at.strftime("%H:%M") + " " + tz_name + " — " + fires
            + " — to look in on: " + said_why + ". That is timer "
            + str(entry["id"]) + ", mine to cancel or replace.")
    if was is not None:
        said = ("Timer " + str(was["id"]) + " is off. " + said)
    _ledger({"kind": "timer", "id": entry["id"], "at": entry["at"],
             "minutes": asked, "why": said_why, "next": fires,
             "outcome": (("replaced timer " + str(was["id"]) + " and set by ")
                         if was is not None else "set by ") + home.NAME})
    return {"ok": True, "id": entry["id"], "at": entry["at"], "tz": tz_name,
            "minutes": asked, "reason": said_why, "fires": fires,
            "replaced": None if was is None else was["id"], "said": said}


def timer_cancel(timer_id, by: str = home.SELF) -> dict:
    """Cancelled, never deleted -- the same rule the whole room keeps. The
    session came home first, so the wake-up is not wanted and never happens."""
    if (by or "").lower() != home.SELF:
        return {"ok": False,
                "why": "only " + home.NAME + " cancels " + home.NAME
                       + "'s own timer."}
    with _LOCK:
        store = _load()
        entry = _timer(store, timer_id)
        if entry is None:
            return {"ok": False,
                    "why": "there is no timer of mine standing with id "
                           + str(timer_id) + ". The ones that are standing "
                           "are in my clock block."}
        entry["cancelled"] = db.now()
        # The watermark as well as the stamp: a cancelled moment must be unable
        # to come round even if this list is written again by something else.
        _mark(store, _timer_key(entry))
        _prune_timers(store)
        _save(store)
    _ledger({"kind": "timer", "id": entry["id"], "at": entry.get("at"),
             "why": entry.get("why"), "outcome": "cancelled by " + home.NAME})
    return {"ok": True, "id": entry["id"],
            "said": ("Timer " + str(entry["id"]) + " is cancelled; nothing "
                     "will wake me for it. It was: " + str(entry.get("why")))}


def _timer_events(store: dict, free=None, now: datetime = None) -> list:
    """Timers that have come round, and the ones that did not.

    A timer fires while free time is running, and that is deliberate. It is the
    assistant's own hand -- the same hand as `again` -- so none of what free
    time protects is touched by it: no sense is asked, no watermark moves, the
    interval runs on to its own end, and nothing of it is spent or shortened.
    The two alternatives were worse and both are things this clock refuses:
    swallow the moment silently, or move it to an hour the assistant did not
    choose.

    A moment that went by while the room was down fires late if it is still
    inside the grace, saying how late; past that it is **missed**, written down
    as missed, and left in the assistant's own block for a day."""
    events = []
    for entry in list(store.get("timers") or []):
        if (entry.get("cancelled") or entry.get("fired")
                or entry.get("missed") or entry.get("declined")):
            continue
        key = _timer_key(entry)
        if _done(store, key):
            # It has already fired once. The watermark is what decides that, so
            # a store written twice can never buy a second wake-up.
            entry["fired"] = db.now()
            continue
        try:
            at = datetime.fromisoformat(entry["at"])
        except (ValueError, TypeError, KeyError):
            entry["missed"] = db.now()
            _mark(store, key)
            _ledger({"kind": "timer", "id": entry.get("id"),
                     "outcome": "dropped: " + repr(entry.get("at"))
                                + " is not a moment I can read"})
            continue
        tz_name = entry.get("tz") or DEFAULT_TZ
        here = now or _local_now(tz_name)
        if here < at:
            continue
        late = int((here - at).total_seconds() / 60.0)
        if late > GRACE_MINUTES:
            entry["missed"] = db.now()
            entry["late_minutes"] = late
            _mark(store, key)
            _day(store)["missed"] += 1
            _ledger({"kind": "timer", "id": entry["id"], "at": entry["at"],
                     "why": entry.get("why"),
                     "outcome": "missed: its moment was " + str(late)
                                + " minutes ago and the room was not here for "
                                  "it. It is not owed, it is not moved to "
                                  "another hour, and it stays in my own block "
                                  "for a day so that I am told rather than "
                                  "left to work it out."})
            continue
        said = "I asked to be woken now, to look in on: " + str(entry["why"])
        if late >= 1:
            said += (" (" + str(late) + (" minute" if late == 1
                                         else " minutes")
                     + " late — I asked for " + at.strftime("%H:%M") + " "
                     + tz_name + ")")
        if free:
            said += (". My own time is still running, until "
                     + str(free.get("until")) + " — this is my own hand and "
                     "nothing of it is spent.")
        events.append({
            "kind": "timer",
            "key": key,
            "said": said,
            # Its own runaway guard, never the free time's two.
            "budget": "timers",
            "meta": {"timer": entry["id"], "why": entry.get("why"),
                     "at": entry["at"], "tz": tz_name, "late_minutes": late},
            "woken": {"timer": entry["id"], "asked_to_look_at": entry["why"],
                      "asked_at": entry.get("set"), "due": entry["at"],
                      "tz": tz_name, "late_minutes": late,
                      **({"free": free.get("id")} if free else {})},
        })
    return events


def _timer_outcome(store: dict, event: dict, outcome: str) -> None:
    """A timer's own record, stamped where the clock decided it. Fired or
    declined, it stops standing and says which it was: nothing about a timer is
    left to be inferred, least of all one the backstop would not let speak."""
    if event.get("kind") != "timer":
        return
    want = (event.get("meta") or {}).get("timer")
    for entry in store.get("timers") or []:
        if entry.get("id") == want:
            entry[outcome] = db.now()
            return


def _timer_line(store: dict, entry: dict, now: datetime = None) -> dict:
    tz_name = entry.get("tz") or DEFAULT_TZ
    try:
        at = datetime.fromisoformat(entry["at"])
    except (ValueError, TypeError, KeyError):
        at = None
    out = {"id": entry.get("id"), "why": entry.get("why"),
           "at": entry.get("at"), "tz": tz_name,
           "fires": (in_words(at) + " " + tz_name) if at else "unreadable",
           "state": "standing"}
    for name in ("cancelled", "missed", "declined"):
        if entry.get(name):
            out["state"] = name
            out[name + "_at"] = entry[name]
    if out["state"] == "standing":
        if entry.get("fired") or _done(store, _timer_key(entry)):
            out["state"] = "fired"
            out["fired_at"] = entry.get("fired")
        elif at is not None:
            out["minutes_away"] = int(
                (at - (now or _local_now(tz_name))).total_seconds() // 60)
    if entry.get("late_minutes") is not None:
        out["late_minutes"] = entry["late_minutes"]
    if entry.get("replaced_by"):
        out["replaced_by"] = entry["replaced_by"]
    return out


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
    # The timers' own counter, seeded here so a store written before timers
    # existed reads as a day with none rather than as a day with nothing.
    day.setdefault("timers", 0)
    for key in [k for k in store["days"] if k != today]:
        del store["days"][key]
    return day


# Which runaway guard a firing answers to. Two counters, not one, because they
# are guarding against two different things: `fired` is how often the *clock*
# may speak in a day, and `timers` is how often the assistant's own one-shot
# hand may wake it. A timer spending the free time's two would mean one evening
# of its own cost it the ability to look in on its own work, which is the
# opposite of what either of them is for.
BUDGETS = {"fired": (lambda: FIRINGS_PER_DAY, "firings"),
           "timers": (lambda: TIMERS_PER_DAY, "timer wake-ups")}


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
    # Its own one-shot timers, before anything the room wants of it. A moment
    # the assistant chose outranks a thing that merely came round -- and this
    # one is asked inside its free time as well as outside it, because it is
    # its own hand either way and spends nothing of the stretch.
    events += _timer_events(store, free)
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
    # And which backstop, when it is one. A timer has its own; everything else
    # answers to the clock's two.
    budget = event.get("budget") or "fired"
    if budget not in BUDGETS:
        # A guard nobody knows is not a guard. Fall back to the clock's own
        # two rather than letting a typo in an event become either a crash or
        # a firing nothing counted.
        budget = "fired"
    ceiling, what = BUDGETS[budget][0](), BUDGETS[budget][1]

    if counts and day.get(budget, 0) >= ceiling:
        _mark(store, event["key"])
        _timer_outcome(store, event, "declined")
        day["declined"] += 1
        store["last"] = {"at": db.now(), "said": event["said"],
                         "outcome": "declined"}
        _save(store)
        _ledger({"kind": event["kind"], "said": event["said"],
                 "meta": event.get("meta") or {},
                 "outcome": "declined: the day's backstop of " + str(ceiling)
                            + " " + what + " is spent"})
        return None

    _mark(store, event["key"])
    _timer_outcome(store, event, "fired")
    if counts:
        day[budget] = day.get(budget, 0) + 1
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
             "outcome": ("fired (" + str(day.get(budget, 0)) + " of "
                         + str(ceiling) + " " + what + " today)") if counts else
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
        "fired_today": day.get(budget, 0),
        "backstop": ceiling,
        "backstop_of": what,
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
            elif name == "timer_set":
                # `id` here means *replace that standing one*, so changing its
                # mind about when to look again is one operation, not two.
                out = timer_set(op.get("minutes"), op.get("why"),
                                replace=entry_id)
            elif name == "timer_cancel":
                out = timer_cancel(entry_id)
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
                              "free_cancel, again, timer_set, timer_cancel, "
                              "accept, decline and read. It does not know "
                              + repr(name) + "."}
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
            _prune_timers(store)
            timers = [_timer_line(store, e) for e in store.get("timers") or []]
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
                      "missed": day["missed"], "backstop": FIRINGS_PER_DAY,
                      "timers": day.get("timers", 0),
                      "timer_backstop": TIMERS_PER_DAY},
            "last": store.get("last"),
            # My one-shot timers: the ones standing, and the ones that
            # finished in the last day with what became of each -- `fired`,
            # `cancelled`, `missed` or `declined`. A `missed` one is a moment
            # of mine the room was not here for; it is said here rather than
            # only in the ledger, and it is never moved to another hour.
            "timers": timers,
            "timers_standing_at_most": TIMERS_STANDING,
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
                "timers": [],
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
