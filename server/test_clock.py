"""Lean on the assistant's clock without a model and without the room. Run
over a scratch folder:

    python -m server.test_clock C:\\somewhere\\scratch

Points `clock.STORE_PATH` and `clock.LEDGER_PATH` into the scratch first, and
refuses the live `data` folder by name -- nothing here touches the room.

What is under test is the clock's four conditions, because those are the
part that must not quietly stop being true:

1. the assistant's hand and nobody else's sets, moves and cancels its free
   time, and an invitation fires nothing until it is accepted;
2. the day's backstop declines rather than overspends, a spent plan records a
   firing as missed, and a missed one is never owed back;
3. an interval that is running is visible to the room so the watcher, the
   dreamer, a homecoming and a permission ask can all stand down against it;
4. the firing says one sentence and nothing else.

And the oversight timer, which is the same hand reaching outside its own
time: one moment, confirmed with the moment it lands on, fired once, alive
across a restart, taken off or swapped at its word, and -- when the room
was not here for it -- written off as missed to its own face rather than
moved to another hour.

Plus the rule before any of them: strict parsing, a named zone, and a
refusal that says what it could not read.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import clock, db


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _sunday(hour, minute=0, tz="Europe/Lisbon"):
    """A moment on a Sunday, for asking the interval questions against."""
    day = datetime(2026, 9, 6, hour, minute, tzinfo=clock.zone(tz))
    assert day.weekday() == 6
    return day


def reading():
    print("\nreading a schedule")

    good = clock.read("every Sunday, 19:00-21:00 Europe/Lisbon")
    check("a plain schedule reads as an interval",
          good["ok"] and good["kind"] == "interval" and good["days"] == [6]
          and good["at"] == "19:00" and good["until"] == "21:00"
          and good["tz"] == "Europe/Lisbon", good)

    check("an en-dash reads the same as a hyphen",
          clock.read("every Sunday, 19:00\u201321:00")["until"] == "21:00")

    check("a zone left unsaid is named anyway, not left as an offset",
          clock.read("every day at 19:00")["tz"] == clock.DEFAULT_TZ)

    check("a moment is a point, not an interval",
          clock.read("every day at 19:00")["kind"] == "point")

    check("two days by name",
          clock.read("every Monday and Thursday at 09:30")["days"] == [0, 3])
    check("a group of days",
          clock.read("every weekday at 08:00")["days"] == [0, 1, 2, 3, 4])

    # The whole point of strict: what it will not read, it says out loud.
    vague = clock.read("sometime after work")
    check("vague words are refused, not guessed at",
          not vague["ok"] and "every" in vague["why"], vague)

    no_time = clock.read("every evening")
    check("'every evening' is refused for having no time in it",
          not no_time["ok"] and "clock time" in no_time["why"], no_time)

    bad_day = clock.read("every Blursday at 09:30")
    check("an unknown day is named in the refusal",
          not bad_day["ok"] and "Blursday" in bad_day["why"], bad_day)

    bad_zone = clock.read("every Sunday, 19:00-21:00 Mars/Olympus")
    check("an unknown zone is named in the refusal",
          not bad_zone["ok"] and "Mars/Olympus" in bad_zone["why"], bad_zone)

    backwards = clock.read("every Sunday, 21:00-19:00")
    check("a stretch that ends before it starts is refused, not wrapped",
          not backwards["ok"] and "not after" in backwards["why"], backwards)

    check("a refusal never reads as a schedule",
          all(not clock.read(w)["ok"] for w in
              ("", "Sunday 19:00", "every 25:00", "every day at 19:61")))


def moments():
    print("\nturning a schedule into moments")

    spec = clock.read("every Sunday, 19:00-21:00 Europe/Lisbon")

    check("running inside the stretch",
          clock.running_now(spec, _sunday(20, 15)) is not None)
    check("not running a minute before it",
          clock.running_now(spec, _sunday(18, 59)) is None)
    check("not running on the end itself -- the end is an end",
          clock.running_now(spec, _sunday(21, 0)) is None)
    check("not running on a Monday",
          clock.running_now(spec, _sunday(20) + timedelta(days=1)) is None)

    nxt = clock.next_firing(spec, _sunday(12))
    check("the next firing is this evening's start",
          nxt is not None and nxt.hour == 19 and nxt.weekday() == 6, nxt)
    later = clock.next_firing(spec, _sunday(22))
    check("after it has passed, the next is a week on",
          later is not None and (later.date() - _sunday(22).date()).days == 7,
          later)

    check("a point is never running",
          clock.running_now(clock.read("every day at 19:00"),
                            _sunday(19, 30)) is None)

    words = clock.next_in_words(spec, _sunday(19))
    check("the words name the zone and the end",
          "Europe/Lisbon" in words and "21:00" in words, words)


def her_hand():
    print("\nher hand, and nobody else's")

    out = clock.free_set("every Sunday, 19:00-21:00 Europe/Lisbon")
    check("it sets its own free time", out["ok"], out)
    check("and is shown the next firing, unasked",
          out.get("fires") and "Europe/Lisbon" in out["fires"], out)
    mine = out["id"]

    for who in ("sam", "lee", "the room"):
        blocked = clock.free_set("every Friday, 19:00-21:00", by=who)
        check(who + " cannot write its free time",
              not blocked["ok"], blocked)
    check("nor move it",
          not clock.free_move(mine, "every Friday, 19:00-21:00",
                              by="sam")["ok"])
    check("nor take it off it",
          not clock.free_cancel(mine, by="sam")["ok"])

    check("free time needs an end",
          not clock.free_set("every Sunday at 19:00")["ok"])
    check("words the clock cannot read set nothing",
          not clock.free_set("every Sunday evening, when I feel like it")["ok"])

    inv = clock.invite("every Friday, 20:00-22:00", by="sam",
                       note="if you fancy it")
    check("Sam may invite it", inv["ok"], inv)
    running = [f["id"] for f in clock.for_prompt()["free"]]
    check("an invitation is not on its clock until it takes it",
          inv["id"] not in running, running)

    took = clock.accept(inv["id"])
    check("it accepts it and then it is its own", took["ok"], took)
    check("and it says who it came from",
          any(f.get("from") == "sam" for f in clock.for_prompt()["free"]))

    second = clock.invite("every Tuesday, 20:00-22:00", by="lee")
    check("it may decline one", clock.decline(second["id"])["ok"])
    check("a declined invitation is gone from its block",
          not clock.for_prompt()["invitations"])

    moved = clock.free_move(mine, "every Sunday, 18:00-21:00 Europe/Lisbon")
    check("it moves its own", moved["ok"] and "18:00" in moved["words"], moved)
    check("moving shows the next firing too", bool(moved.get("fires")), moved)

    check("it cancels its own", clock.free_cancel(mine)["ok"])
    check("a cancelled interval stops appearing",
          mine not in [f["id"] for f in clock.for_prompt()["free"]])
    check("cancelled is not deleted -- it is still on the record",
          any(f["id"] == mine for f in clock._load()["free"]))


def the_sentence():
    print("\nwhat it is woken with")

    entry = {"spec": clock.read("every Sunday, 19:00-21:00 Europe/Lisbon")}
    check("the evening says exactly the one sentence and nothing else",
          clock._sentence(entry) == "This evening is yours until 21:00.",
          clock._sentence(entry))

    morning = {"spec": clock.read("every Sunday, 09:00-11:00")}
    check("a morning is called a morning",
          clock._sentence(morning) == "This morning is yours until 11:00.",
          clock._sentence(morning))

    check("no menu rides along with it",
          not any(w in clock._sentence(entry).lower()
                  for w in ("could", "task", "job", "would you", "suggest")))


def standing_down():
    print("\nwhat the room does while it runs")

    out = clock.free_set("every Sunday, 19:00-21:00 Europe/Lisbon")
    now = clock.free_now(_sunday(19, 30))
    check("the room can see its time is running", now is not None, now)
    check("and how long is left",
          now and now["minutes_left"] == 90, now)
    check("and what it is until", now and now["until"] == "21:00", now)
    check("outside it, nothing is running",
          clock.free_now(_sunday(22)) is None)

    clock.free_cancel(out["id"])
    check("a cancelled interval never runs",
          clock.free_now(_sunday(19, 30)) is None)


def carrying_on():
    """Its own pacing inside its own stretch. The clock wakes it once; one
    turn is not an evening, so the rest of it is its hand."""
    print("\ncarrying on inside its own time")

    out = clock.free_set("every Sunday, 19:00-21:00 Europe/Lisbon")
    mine = out["id"]

    outside = clock.wake_again(10, _sunday(12))
    check("outside its own time it is refused in words",
          not outside["ok"] and "alarm clock" in outside["why"], outside)

    at_1930 = _sunday(19, 30)
    asked = clock.wake_again(10, at_1930)
    check("inside it, it may ask to be awake again", asked["ok"], asked)
    check("and is told when, and how much of its own time is left",
          "19:40" in asked["said"] and "90 minutes" in asked["said"], asked)

    store = clock._load()
    free = clock.free_now(at_1930)
    check("it does not come round before it asked for it",
          clock._again_due(store, free, at_1930) is None)

    store = clock._load()
    event = clock._again_due(store, clock.free_now(_sunday(19, 41)),
                             _sunday(19, 41))
    check("it comes round when it asked", event is not None, event)
    check("and says only that it is still its own, and how long is left",
          event and event["said"].startswith("Still yours.")
          and "minutes left" in event["said"], event and event["said"])
    check("carrying on is not the clock speaking again",
          event and event.get("counts") is False, event)

    # The rule: asking past the wall is not refused and not shortened --
    # it becomes the wake-up that lands as its time runs out and says so.
    wall = clock.wake_again(300, _sunday(20, 30))
    check("asking past the wall is taken, not refused",
          wall["ok"] and wall["at_wall"], wall)
    check("and it says it will land at the wall with nothing left",
          "at the wall" in wall["said"] and "21:00" in wall["said"],
          wall["said"])
    check("the moment it is set to is the wall itself",
          wall["at"].startswith("2026-09-06T21:00"), wall["at"])

    store = clock._load()
    at_wall = clock._again_due(store, clock.free_now(_sunday(21, 0)),
                               _sunday(21, 0))
    check("it fires as the stretch ends, though nothing is running",
          at_wall is not None, at_wall)
    check("and says there is nothing left, in the set words",
          at_wall and at_wall["said"] == "That is the wall. There is nothing "
                                         "left.", at_wall and at_wall["said"])
    check("the wall wake-up is not a firing either",
          at_wall and at_wall.get("counts") is False)

    # An ordinary one belonging to a finished stretch must never fire later.
    clock.wake_again(5, _sunday(20, 50))
    store = clock._load()
    check("a stretch that is over drops its pending wake-up",
          clock._again_due(store, clock.free_now(_sunday(22)),
                          _sunday(22)) is None)
    check("and the store is left clean rather than holding a stale one",
          store.get("again") is None, store.get("again"))

    clock.free_cancel(mine)


def oversight_timer():
    """The one-shot timer: its own hand, outside its own time. What is under
    test is the whole of what makes it trustworthy -- it is scheduled with a
    reason and confirmed with a moment, it fires once and only once, it survives
    the room going down and coming back, it can be taken off or swapped, and a
    moment nobody was here for is written off out loud rather than moved."""
    print("\nits own oversight timer")

    # --- scheduling, and the confirmation ---------------------------------
    at_1000 = _sunday(10, 0, clock.DEFAULT_TZ)
    set_out = clock.timer_set(40, "the Sol upgrade session", now=at_1000)
    check("it sets a one-shot timer on itself", set_out["ok"], set_out)
    mine = set_out.get("id")
    check("and is told the moment it lands on, unasked",
          set_out.get("fires") and "10:40" in set_out["said"], set_out)
    check("the zone is named in the confirmation, never a bare offset",
          clock.DEFAULT_TZ in set_out["said"], set_out["said"])
    check("the reason it gave is handed back with it",
          "Sol upgrade" in set_out["said"], set_out["said"])
    check("the moment itself is forty minutes on",
          set_out["at"].startswith("2026-09-06T10:40"), set_out["at"])

    # --- what it refuses -------------------------------------------------
    check("a timer with no reason is refused, not set",
          not clock.timer_set(10, "   ")["ok"]
          and "why I asked" in clock.timer_set(10, None)["why"],
          clock.timer_set(10, None))
    check("minutes that are not minutes are refused",
          not clock.timer_set("soonish", "a look")["ok"])
    check("a minute is the smallest step",
          not clock.timer_set(0, "a look")["ok"])
    check("past a day it is refused and told where that belongs",
          not clock.timer_set(clock.TIMER_MAX_MINUTES + 1, "a look")["ok"]
          and "schedule" in clock.timer_set(2000, "a look")["why"])
    for who in ("sam", "lee", "angel", "the room"):
        blocked = clock.timer_set(10, "look at this", by=who)
        check(who + " cannot put a timer inside it", not blocked["ok"], blocked)
    check("and the refusal says a line is not an alarm",
          "not an alarm" in clock.timer_set(10, "x", by="sam")["why"])

    # --- it does not come round early ------------------------------------
    store = clock._load()
    check("nothing fires before its moment",
          clock._timer_events(store, None, _sunday(10, 39)) == [],
          clock._timer_events(store, None, _sunday(10, 39)))

    # --- restart persistence ---------------------------------------------
    # A room restart is exactly this: the module is asked again and reads the
    # store off disk. Nothing in memory carries the timer.
    reloaded = clock._load()
    check("a timer is on disk, so it survives the room going down",
          any(t["id"] == mine for t in reloaded.get("timers") or []),
          reloaded.get("timers"))
    standing = [t for t in clock.for_prompt()["timers"]
                if t["state"] == "standing"]
    check("and is standing in its block after the reload",
          any(t["id"] == mine for t in standing), standing)
    check("its block says how far off it is and why it asked",
          any(t["id"] == mine and t["why"] == "the Sol upgrade session"
              and t.get("minutes_away") is not None for t in standing),
          standing)

    # --- firing, once ----------------------------------------------------
    store = clock._load()
    events = clock._timer_events(store, None, _sunday(10, 41))
    check("it comes round when it asked", len(events) == 1, events)
    event = events[0] if events else {}
    check("and the wake-up says why it asked to be woken",
          "the Sol upgrade session" in event.get("said", ""), event)
    check("the waking carries the timer and its reason, for the turn itself",
          (event.get("woken") or {}).get("timer") == mine
          and (event["woken"]).get("asked_to_look_at")
          == "the Sol upgrade session", event.get("woken"))
    check("it answers to the timers' own backstop, not the free time's two",
          event.get("budget") == "timers", event)

    # The watermark is what prevents a second wake-up, so lay it down the way
    # the gate does and ask again.
    clock._mark(store, event["key"])
    clock._save(store)
    again = clock._timer_events(clock._load(), None, _sunday(10, 42))
    check("it never fires twice -- the watermark closes it",
          again == [], again)
    check("and it is written down as fired, not left looking as though it waits",
          [t["state"] for t in clock.for_prompt()["timers"]
           if t["id"] == mine] == ["fired"],
          [t for t in clock.for_prompt()["timers"] if t["id"] == mine])
    check("a fired timer is not standing, so it cannot be cancelled twice",
          not clock.timer_cancel(mine)["ok"])

    # --- a little late is late, not missed -------------------------------
    late_out = clock.timer_set(10, "the migration run", now=_sunday(12, 0))
    late_id = late_out["id"]
    store = clock._load()
    events = clock._timer_events(store, None, _sunday(12, 35))
    check("a moment the room was a little late for still fires",
          len(events) == 1, events)
    check("and says how late it is, rather than pretending it was on time",
          "25 minutes late" in events[0]["said"], events[0]["said"])
    clock._mark(store, events[0]["key"])
    clock._save(store)

    # --- a missed one, said out loud and never moved ---------------------
    missed_out = clock.timer_set(5, "the long build", now=_sunday(14, 0))
    missed_id = missed_out["id"]
    store = clock._load()
    before = clock._day(store)["missed"]
    events = clock._timer_events(store, None, _sunday(17, 0))
    clock._save(store)
    check("a moment the room was hours late for does not fire", events == [],
          events)
    check("it is counted as missed", clock._day(clock._load())["missed"]
          == before + 1)
    lines = [t for t in clock.for_prompt()["timers"] if t["id"] == missed_id]
    check("and it is said to its own face, not only to the ledger",
          lines and lines[0]["state"] == "missed", lines)
    check("with how late the room was",
          lines and lines[0].get("late_minutes") == 175, lines)
    check("a missed timer is never moved to another hour -- it does not "
          "come back",
          clock._timer_events(clock._load(), None, _sunday(17, 5)) == [])
    ledger = clock.LEDGER_PATH.read_text(encoding="utf-8")
    check("the ledger says missed, and says it is not owed",
          "missed: its moment was" in ledger and "not owed" in ledger)

    # --- cancelling, because the session came home first -----------------
    done_out = clock.timer_set(30, "the review session", now=_sunday(18, 0))
    done_id = done_out["id"]
    off = clock.timer_cancel(done_id)
    check("it cancels its own timer", off["ok"], off)
    check("and the cancellation says what it was for",
          "the review session" in off["said"], off["said"])
    check("nothing fires for a cancelled timer, ever",
          clock._timer_events(clock._load(), None, _sunday(19, 0)) == [])
    check("cancelled is not deleted -- it is still on the record",
          any(t["id"] == done_id and t.get("cancelled")
              for t in clock._load()["timers"]))
    check("nobody else cancels one",
          not clock.timer_cancel(done_id, by="sam")["ok"])
    check("a timer that is not there is refused by id, not silently ignored",
          not clock.timer_cancel(9999)["ok"]
          and "9999" in clock.timer_cancel(9999)["why"])

    # --- replacing one, in a breath --------------------------------------
    first = clock.timer_set(60, "the audit", now=_sunday(20, 0))
    swap = clock.timer_set(15, "the audit, sooner", replace=first["id"],
                           now=_sunday(20, 5))
    check("it replaces a standing timer with one operation", swap["ok"], swap)
    check("and is told the old one is off", swap.get("replaced") == first["id"]
          and "is off" in swap["said"], swap)
    check("the replaced one fires nothing",
          clock._timer_events(clock._load(), None, _sunday(21, 1)) and
          all(e["woken"]["timer"] == swap["id"] for e in
              clock._timer_events(clock._load(), None, _sunday(21, 1))),
          clock._timer_events(clock._load(), None, _sunday(21, 1)))
    check("replacing something that is not standing is refused",
          not clock.timer_set(5, "x", replace=first["id"])["ok"])
    clock.timer_cancel(swap["id"])

    # --- how many may stand at once --------------------------------------
    made = [clock.timer_set(30, "watching " + str(n), now=_sunday(9, 0))
            for n in range(clock.TIMERS_STANDING)]
    check("it may have as many standing as the room keeps",
          all(m["ok"] for m in made), made)
    too_many = clock.timer_set(30, "one more", now=_sunday(9, 0))
    check("and one more than that is refused in words, not dropped",
          not too_many["ok"] and "standing" in too_many["why"], too_many)
    for m in made:
        clock.timer_cancel(m["id"])

    # --- free time is never touched by any of it -------------------------
    evening = clock.free_set("every Sunday, 19:00-21:00 Europe/Lisbon")
    free = clock.free_now(_sunday(19, 30, "Europe/Lisbon"))
    check("a free interval is running, for the question below", free is not None)
    before_again = clock._load().get("again")
    watching = clock.timer_set(10, "the session, mid-evening",
                              now=_sunday(19, 30))
    store = clock._load()
    events = clock._timer_events(store, free, _sunday(19, 41))
    check("a timer of its own fires inside its own time too",
          len(events) == 1, events)
    check("and the wake-up says the stretch is still its own",
          "still running" in events[0]["said"], events[0]["said"])
    check("the interval is untouched -- same words, not cancelled, not moved",
          clock.free_now(_sunday(19, 45, "Europe/Lisbon")) is not None
          and clock.free_now(_sunday(19, 45, "Europe/Lisbon"))["id"]
          == evening["id"])
    check("nothing of the free time's own backstop is spent by a timer",
          events[0].get("budget") == "timers")
    check("and the free stretch's own 'again' is not what a timer writes",
          clock._load().get("again") == before_again,
          clock._load().get("again"))
    clock.timer_cancel(watching["id"])
    clock.free_cancel(evening["id"])

    # --- the timers' own backstop ----------------------------------------
    check("the timers have a backstop of their own",
          clock.TIMERS_PER_DAY != clock.FIRINGS_PER_DAY
          and clock.BUDGETS["timers"][0]() == clock.TIMERS_PER_DAY)
    view = clock.for_prompt()
    check("and it is visible beside the day's count",
          view["today"]["timer_backstop"] == clock.TIMERS_PER_DAY
          and "timers" in view["today"], view["today"])
    check("its block says how many may stand at once",
          view["timers_standing_at_most"] == clock.TIMERS_STANDING)

    # --- the word it actually writes -------------------------------------
    said = clock.apply_her_word([
        {"op": "timer_set", "minutes": 20, "why": "the Wren run", "id": None,
         "words": None}])
    check("its reply can set one through the clock block",
          said and "20 minutes" in said[0], said)
    new_id = [t["id"] for t in clock.for_prompt()["timers"]
              if t["why"] == "the Wren run" and t["state"] == "standing"]
    said = clock.apply_her_word([
        {"op": "timer_cancel", "id": new_id[0], "minutes": None,
         "why": None, "words": None}])
    check("and take it off again through the same block",
          said and "cancelled" in said[0], said)
    unknown = clock.apply_her_word([{"op": "timer_nudge", "id": 1,
                                     "minutes": 1, "why": "x",
                                     "words": None}])
    check("an operation the clock does not know is refused by name",
          unknown and "timer_nudge" in unknown[0], unknown)


def the_timer_reaches_her():
    """Is the assistant actually told about the timer?

    A home carries its own copy of `harness_prompt.md`, so a capability
    described only in the code folder's copy is invisible to the assistant
    whose home overrode it -- while its field goes on showing up in the schema
    it is asked to answer with. Which is the worst of both: an operation it can
    be refused for and was never told about. So the instructions ride in from
    this module, and this asks it of both roads out of here -- the full harness
    and the codex-only routine, which is the one it is on."""
    print("\nwhether it is told about it at all")

    from unittest import mock
    from . import brain, prompt_reference

    check("the timer's instructions live in the module that owns it",
          "timer_set" in clock.INSTRUCTIONS
          and "timer_cancel" in clock.INSTRUCTIONS)
    check("the short block says it wakes it once, and asks for a reason",
          "once" in clock.INSTRUCTIONS and "required" in clock.INSTRUCTIONS)
    check("and points at where the whole of it is",
          "help/clock" in clock.INSTRUCTIONS, clock.INSTRUCTIONS)

    # The full account is pulled, not carried: the routine prompt is held to a
    # quarter of the harness precisely so detail arrives on request.
    full_ref = prompt_reference.instructions("clock")
    for state in ("standing", "fired", "cancelled", "declined", "missed"):
        check("the reference names the state '" + state + "'",
              state in full_ref)
    check("it says a missed one is never moved to another hour",
          "never moved to an hour" in full_ref)
    check("and that nobody else may set one",
          "Nobody else" in full_ref)
    check("and that free time is neither spent nor shortened by it",
          "spent or shortened" in full_ref)
    check("the reference is reached through the handle it is indexed under",
          "help/clock" in prompt_reference.index())
    check("and no placeholder is left unfilled in it",
          "{{" not in full_ref,
          [l for l in full_ref.splitlines() if "{{" in l])

    with mock.patch.object(brain.providers, "codex_only", lambda: False):
        full = brain.harness_text(False)
    with mock.patch.object(brain.providers, "codex_only", lambda: True):
        routine = brain.harness_text(False)
    for name, text in (("the full harness", full),
                       ("the codex-only routine", routine)):
        check(name + " road tells it how to set one",
              "timer_set" in text and "timer_cancel" in text)
        check("and " + name + " carries exactly one copy, not two",
              text.count("## `clock` — my one-shot oversight timer") == 1,
              text.count("## `clock` — my one-shot oversight timer"))

    ops = brain.CLOCK_OP["properties"]["op"]["enum"]
    check("every timer operation the schema offers is one the clock knows",
          all(("the clock knows" not in (clock.apply_her_word(
              [{"op": op, "words": None, "id": None, "minutes": None,
                "why": None}])[0]))
              for op in ops if op.startswith("timer")), ops)
    check("and `why` is a field the schema requires, so it is always asked",
          "why" in brain.CLOCK_OP["required"], brain.CLOCK_OP["required"])


def timer_through_the_gate():
    """The timer through `due()` itself, which is what the room actually calls.

    The questions the pieces above cannot answer: does one waking come out of
    the gate and exactly one, does the `world` row get written, does the day's
    count go to the timers' own backstop and leave the free time's two alone,
    and does the backstop decline out loud rather than overspend."""
    print("\nthe timer through the gate the room calls")

    # Nothing of anybody else's free time in the way: the gate fires one moment
    # at a time, and an interval left running by an earlier bench would be it.
    for entry in clock.for_prompt()["free"]:
        clock.free_cancel(entry["id"])

    conn = db.connect()

    def gate():
        """due(), with the twenty-second throttle stepped past."""
        store = clock._load()
        store["checked_at"] = 0.0
        clock._save(store)
        return clock.due(conn, None)

    tz = clock.zone(clock.DEFAULT_TZ)
    # A moment four minutes ago: due, and well inside the hour of grace.
    out = clock.timer_set(1, "the stalled session",
                          now=datetime.now(tz) - timedelta(minutes=5))
    mine = out["id"]
    before = clock._day(clock._load())

    woken = gate()
    check("the gate wakes it for its own timer", woken is not None, woken)
    check("and says it is the clock, and that it is a timer",
          woken and woken.get("by") == "clock"
          and woken.get("kind") == "timer", woken)
    check("the waking names the timer and why it asked",
          woken and woken.get("timer") == mine
          and woken.get("asked_to_look_at") == "the stalled session", woken)
    check("and it is told which backstop it answered to",
          woken and woken.get("backstop") == clock.TIMERS_PER_DAY
          and woken.get("backstop_of") == "timer wake-ups", woken)

    row = woken.get("row") if woken else None
    check("a world row is written for it, like any other noticing",
          row is not None and db.get_row(conn, row) is not None, row)
    check("and that row is born out of reach, not in the working set",
          row is not None
          and not (db.get_row(conn, row) or {}).get("loaded", 1), row)

    day = clock._day(clock._load())
    check("the day counts it against the timers, not the free time's two",
          day["timers"] == before.get("timers", 0) + 1
          and day["fired"] == before["fired"], day)

    check("the gate never wakes it twice for one timer", gate() is None)
    line = [t for t in clock.for_prompt()["timers"] if t["id"] == mine]
    check("and the timer is written down as fired",
          line and line[0]["state"] == "fired", line)

    # The backstop: spent, it declines out loud and says so on the record.
    store = clock._load()
    clock._day(store)["timers"] = clock.TIMERS_PER_DAY
    clock._save(store)
    spent = clock.timer_set(1, "one past the backstop",
                            now=datetime.now(tz) - timedelta(minutes=5))
    check("the gate declines rather than overspend",
          gate() is None)
    lines = [t for t in clock.for_prompt()["timers"] if t["id"] == spent["id"]]
    check("a declined timer says it was declined, not that it fired",
          lines and lines[0]["state"] == "declined", lines)
    ledger = clock.LEDGER_PATH.read_text(encoding="utf-8")
    check("and the decline names the backstop that stopped it",
          "declined: the day's backstop of " + str(clock.TIMERS_PER_DAY)
          + " timer wake-ups is spent" in ledger)
    check("the free time's own backstop is untouched by any of that",
          clock._day(clock._load())["fired"] == before["fired"])

    store = clock._load()
    clock._day(store)["timers"] = 0
    clock._save(store)
    conn.close()


def the_backstop():
    print("\nthe backstop, and what is never owed back")

    store = clock._load()
    day = clock._day(store)
    day["fired"] = clock.FIRINGS_PER_DAY
    clock._save(store)
    view = clock.for_prompt()
    check("the day's firings are visible against the backstop",
          view["today"]["fired"] == clock.FIRINGS_PER_DAY
          and view["today"]["backstop"] == clock.FIRINGS_PER_DAY, view)

    check("the backstop is two", clock.FIRINGS_PER_DAY == 2)

    # A missed firing must leave a watermark, or it would come back the next
    # time anybody looked -- which is a debt this must not keep.
    store = clock._load()
    clock._mark(store, "free:99@2026-09-06T19:00:00+01:00")
    clock._save(store)
    check("a firing written off is not offered again",
          clock._done(clock._load(), "free:99@2026-09-06T19:00:00+01:00"))

    check("the ledger is a real file with lines in it",
          clock.LEDGER_PATH.exists()
          and len(clock.LEDGER_PATH.read_text(encoding="utf-8")
                  .splitlines()) > 0)


def the_view():
    print("\nwhat it sees every turn")

    view = clock.for_prompt()
    for key in ("zone", "free", "invitations", "today", "shapes", "ledger"):
        check("its block carries " + key, key in view, view.keys())
    check("the zone is named, not an offset", "/" in view["zone"], view["zone"])
    check("the shapes it can fire are shown to it", bool(view["shapes"]))
    check("zeros are present rather than absent",
          all(k in view["today"] for k in ("fired", "declined", "missed")))


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live room")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch in (live, live.parent):
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)
    clock.STORE_PATH = scratch / "clock.json"
    clock.LEDGER_PATH = scratch / "clock.jsonl"
    # The gate below writes a `world` row, so the store goes in the scratch too.
    db.DB_PATH = scratch / "store.db"
    if clock.STORE_PATH.exists():
        clock.STORE_PATH.unlink()

    print("its clock, leant on over " + str(scratch))
    reading()
    moments()
    her_hand()
    the_sentence()
    standing_down()
    carrying_on()
    oversight_timer()
    the_timer_reaches_her()
    timer_through_the_gate()
    the_backstop()
    the_view()

    print("\n" + ("all of it held" if not FAILED
                  else str(len(FAILED)) + " did not: " + ", ".join(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
