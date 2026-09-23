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

Plus the rule before any of them: strict parsing, a named zone, and a
refusal that says what it could not read.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import clock


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
    if clock.STORE_PATH.exists():
        clock.STORE_PATH.unlink()

    print("its clock, leant on over " + str(scratch))
    reading()
    moments()
    her_hand()
    the_sentence()
    standing_down()
    carrying_on()
    the_backstop()
    the_view()

    print("\n" + ("all of it held" if not FAILED
                  else str(len(FAILED)) + " did not: " + ", ".join(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
