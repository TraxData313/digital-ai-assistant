"""Lean on the watcher without a model. Run over a scratch folder:

    python -m server.test_watch C:\\somewhere\\scratch

Every path the watcher touches is pointed into the scratch first, so nothing
here can brush the live store -- and like `test_door`, the point is to lean on
every rule: the watermarks, the ceiling, the decline ledger, its mute, the
rebaseline on unmute, the once-per-window quota, the source that is off by
default staying off, and the fifth sense, `quiet`, which
fires because nothing happened, and the mute of its own that dies of old age;
and the sixth, `steam_comment`, which polls a world outside its own store,
one item at a time from a list, each on its own clock, watermark and
backoff -- ignoring its own poll timer's owner (his replies) and backing off
hard rather than retry a 429, on the item that hit it and no other.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, jobs, projects, watch


FAILED = []


def check(name, ok, detail=""):
    mark = "ok " if ok else "FAIL"
    print("  " + mark + "  " + name + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def fresh_due(conn):
    """due() with the half-minute throttle stepped past."""
    st = watch._state()
    st["checked_at"] = 0.0
    watch._save_state(st)
    return watch.due(conn)


def age_rows(conn, hours):
    """Push every row in the room back in time, so it reads as still. The
    only way to test a six-hour silence without waiting six hours."""
    when = (datetime.now(timezone.utc)
            - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("UPDATE rows SET dt = ?", (when,))
    conn.commit()


def forget_quiet_day():
    """Clear quiet's once-a-day marker, as a new day would."""
    st = watch._state()
    st["quiet_day"] = None
    watch._save_state(st)


def add_refusal(line: dict):
    with watch.REFUSALS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live data")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch == live or scratch == live.parent:
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)

    # Point every path into the scratch before anything opens anything.
    db.DB_PATH = scratch / "store.db"
    watch.CONFIG_PATH = scratch / "watch.json"
    watch.STATE_PATH = scratch / "watch_state.json"
    watch.LEDGER_PATH = scratch / "wakings.jsonl"
    watch.REFUSALS_PATH = scratch / "refusals.jsonl"
    watch.push.CONFIG_PATH = scratch / "push.json"
    # Which mind the room is on decides which senses exist; the bench must
    # not inherit whatever the folder it runs from happens to have chosen.
    from . import providers
    providers.CHOICE_PATH = scratch / "provider.json"
    watch.REFUSALS_PATH.write_text("", encoding="utf-8")
    jobs.JOBS_PATH = scratch / "jobs.json"

    # The mods it watches come from the home's comments.json, and a bench
    # has no home -- so a made-up pair, set before anything reads the lists.
    watch.STEAM_STEAMID64 = "76561190000000000"
    watch.STEAM_ITEMS = ({"id": "1000000001", "label": "Lantern Mod"},
                         {"id": "1000000002", "label": "Field Drills"})
    watch.STEAM_COMMENT_URL_TMPL = (
        "https://steamcommunity.com/comment/PublishedFile_Public/render/"
        + watch.STEAM_STEAMID64 + "/{item_id}/")
    watch.STEAM_IGNORE_AUTHOR = "OwnerOnSteam"
    watch.NEXUS_ITEMS = (
        {"id": "20001", "label": "Lantern Mod",
         "url": "https://www.nexusmods.com/examplegame/mods/20001?tab=posts"},
        {"id": "20002", "label": "Field Drills",
         "url": "https://www.nexusmods.com/examplegame/mods/20002?tab=posts"})
    watch.NEXUS_IGNORE_AUTHOR = "owner_on_nexus"
    # And the two comment senses are off unless a home turns them on; a home
    # with mods does. On the defaults, so every watch.json below keeps them.
    watch.DEFAULTS["sources"]["steam_comment"]["on"] = True
    watch.DEFAULTS["sources"]["nexus_comment"]["on"] = True

    fake_limits = {"rows": []}
    watch._limits_now = lambda: fake_limits["rows"]

    fake_steam = {item["id"]: {"comments": [], "error": None}
                  for item in watch.STEAM_ITEMS}
    watch._steam_now = lambda item: (fake_steam[item["id"]]["comments"],
                                     fake_steam[item["id"]]["error"])

    # nexus_comment: mocked at the same seam steam_comment is, one level
    # below the browser -- _nexus_now stands in for "a poll happened and
    # this is what page.evaluate(NEXUS_EXTRACT_JS) returned", the same shape
    # the owner's own script's DOM walk produces (id, author, text, ...). This
    # is NO NETWORK; the real fetch path (Playwright, Chromium, the actual
    # page) is proven separately, live, against real mod pages -- not
    # here, where the point is the sense's own logic in isolation. The two
    # made-up mods set above (Lantern Mod 20001, Field Drills 20002) give
    # per-item isolation somewhere to stand on.
    fake_nexus = {item["id"]: {"comments": [], "error": None}
                  for item in watch.NEXUS_ITEMS}
    watch._nexus_now = lambda item: (fake_nexus[item["id"]]["comments"],
                                     fake_nexus[item["id"]]["error"])

    conn = db.connect()
    print("the watcher, leant on over " + str(scratch))

    # 1. The first ever look is history, not news: watermarks set, no waking.
    add_refusal({"name": "old-hand", "tool_name": "Bash",
                 "why": "from before the watcher existed"})
    out = fresh_due(conn)
    check("first look wakes nobody", out is None)

    # 2. A silent refusal and a missed night wake it, gathered into one.
    add_refusal({"name": "wren", "tool_name": "Bash",
                 "why": "restart.ps1 is refused by name"})
    did = db.open_dream(conn, "2026-08-27", "missed", False)
    db.close_dream(conn, did, status="missed", line="the machine slept", row=None)
    out = fresh_due(conn)
    check("one waking, two events", bool(out) and len(out["events"]) == 2,
          out and [e["source"] for e in out["events"]])
    check("it says by world", bool(out) and out.get("by") == "world")
    check("count is 1 of the default ceiling",
          bool(out) and out["count_today"] == 1
          and out["ceiling"] == watch.DEFAULTS["per_day"])
    rows = [db.get_row(conn, e["row"]) for e in out["events"]] if out else []
    check("world rows born out of reach",
          rows and all(r["kind"] == "world" and not r["loaded"] for r in rows))

    # 3. A refusal that already woke it (an ask) is not news twice.
    add_refusal({"name": "wren", "tool_name": "WebFetch",
                 "why": "outside its reach", "asked_her": True})
    out = fresh_due(conn)
    check("an asked refusal wakes nobody", out is None)

    # 4. The quota sense fires once per window instance.
    fake_limits["rows"] = [{"window": "five_hour", "label": "5-hour limit",
                            "used_fraction": 0.91, "resets_at": "later"}]
    out = fresh_due(conn)
    check("quota low wakes it", bool(out)
          and out["events"][0]["source"] == "quota_low")
    out = fresh_due(conn)
    check("the same window does not wake twice", out is None)

    # 4b. quota_low wakes it but never reaches his phone, even alone.
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    quota_entry = next(l for l in reversed(ledger)
                        if l.get("source") == "quota_low"
                        and (l.get("outcome") or "").startswith("woke"))
    check("quota_low never pushes",
          "never pushes" in (quota_entry.get("push") or ""),
          quota_entry.get("push"))

    # 4c. A push-eligible event still pushes normally when he is not here.
    add_refusal({"name": "wren", "tool_name": "Bash", "why": "unrelated"})
    out = fresh_due(conn)
    check("a fresh refusal still wakes it", bool(out))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    refusal_entry = next(l for l in reversed(ledger)
                          if l.get("source") == "refusal"
                          and (l.get("outcome") or "").startswith("woke"))
    check("a normal push is attempted, not withheld",
          "withheld" not in (refusal_entry.get("push") or ""),
          refusal_entry.get("push"))

    # 4d. He spoke in the room in the last hour: a sense still wakes it,
    #     but the push stays home, and the ledger says why.
    db.add_row(conn, "user", "hey")
    add_refusal({"name": "wren", "tool_name": "Bash", "why": "he is right here"})
    out = fresh_due(conn)
    check("a sense still wakes it while he is here", bool(out))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    here_row = out["events"][0]["row"] if out else None
    here_entry = next((l for l in reversed(ledger)
                        if l.get("source") == "refusal" and l.get("row") == here_row),
                       None)
    check("the push is withheld while he is here",
          bool(here_entry) and "withheld" in (here_entry.get("push") or "")
          and "owner spoke" in (here_entry.get("push") or ""),
          here_entry and here_entry.get("push"))

    # 5. The ceiling is Sam's, and declines are written down as declines.
    watch.CONFIG_PATH.write_text(json.dumps(
        {"per_day": 2, "sources": {}}), encoding="utf-8")
    add_refusal({"name": "lark", "tool_name": "Bash", "why": "the veto"})
    out = fresh_due(conn)
    check("over the ceiling wakes nobody", out is None)
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    check("the decline is in the ledger",
          any("declined" in (l.get("outcome") or "") for l in ledger))
    view = watch.for_prompt()
    check("it sees the decline", view["today"]["declined"] >= 1
          and view["today"]["woke"] == 4)

    # 6. Declined is an answer, not a postponement: the same fact does not
    #    come back tomorrow-shaped after the ceiling is raised.
    watch.CONFIG_PATH.write_text(json.dumps(
        {"per_day": 6, "sources": {}}), encoding="utf-8")
    out = fresh_due(conn)
    check("a declined event stays declined", out is None)

    # 7. Its mute: the sense is not observed at all.
    said = watch.apply_her_word(["refusal"], [])
    check("the mute is said back", any("muted" in s for s in said))
    add_refusal({"name": "lark", "tool_name": "Bash", "why": "again"})
    out = fresh_due(conn)
    check("a muted sense sees nothing", out is None)
    view = watch.for_prompt()
    check("the block says muted by me",
          view["senses"]["refusal"] == "muted by me")

    # 8. Unmute watches from now: what fired while muted stays unwatched.
    said = watch.apply_her_word([], ["refusal"])
    check("the unmute says from now", any("from now" in s for s in said))
    out = fresh_due(conn)
    check("no backlog is delivered", out is None)

    # 9. A name that is not a sense is said back, never swallowed.
    said = watch.apply_her_word(["the_moon"], [])
    check("a made-up sense is refused in words",
          any("not a sense" in s for s in said))

    # 10. window_reset fires only when it has somebody to be for: a job open
    #     and parked at zero links. With no such job, a genuine reset is
    #     declined outright -- written to the ledger, never silently
    #     dropped -- and nothing is pushed.
    from datetime import datetime, timedelta, timezone
    reset_past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    reset_future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    cfg = watch.config()
    check("no job is open yet", not jobs.any_open_parked())
    st = {"resets_seen": {"reset_test": {"resets_at": reset_past, "fraction": 0.30}}}
    fake_limits["rows"] = [{"window": "reset_test", "label": "reset test window",
                            "used_fraction": 0.05, "resets_at": reset_future}]
    check("no job parked: the reset does not fire",
          watch._resets(st, cfg) == [])
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    decline = next((l for l in reversed(ledger)
                     if l.get("source") == "window_reset"
                     and "declined" in (l.get("outcome") or "")), None)
    check("the decline is written to the ledger", bool(decline), decline)
    check("the decline names why",
          bool(decline) and "nobody to be for" in (decline.get("outcome") or ""),
          decline and decline.get("outcome"))

    # 11. Open a job and spend its one link to zero: now a reset has
    #     somebody to be for, and it fires.
    jobs.open_job("parked-job", "wait for a link", 5.0, 1)
    jobs.spend_link("parked-job")
    check("the job is parked at zero links",
          jobs.get_open("parked-job")["links_left"] == 0)
    st2 = {"resets_seen": {"reset_test2": {"resets_at": reset_past, "fraction": 0.30}}}
    fake_limits["rows"] = [{"window": "reset_test2", "label": "reset test window 2",
                            "used_fraction": 0.05, "resets_at": reset_future}]
    events = watch._resets(st2, cfg)
    check("a job parked at zero links lets the reset fire",
          len(events) == 1 and events[0]["source"] == "window_reset", events)

    # 11b. The same, through the full due() path: it wakes it, and it never
    #      reaches his phone -- waking-only, under any condition.
    watch.CONFIG_PATH.write_text(json.dumps(
        {"per_day": 40, "sources": {}}), encoding="utf-8")
    fake_limits["rows"] = [{"window": "reset_test3", "label": "reset test window 3",
                            "used_fraction": 0.05, "resets_at": reset_future}]
    st_full = watch._state()
    st_full["resets_seen"]["reset_test3"] = {"resets_at": reset_past, "fraction": 0.30}
    watch._save_state(st_full)
    out = fresh_due(conn)
    check("a real reset with a parked job wakes it",
          bool(out) and any(e["source"] == "window_reset" for e in out["events"]))
    check("the waking's own push is withheld",
          bool(out) and "withheld" in (out.get("push") or ""), out and out.get("push"))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    reset_entry = next((l for l in reversed(ledger)
                         if l.get("source") == "window_reset"
                         and (l.get("outcome") or "").startswith("woke")), None)
    check("its own ledger row says the push was withheld and why",
          bool(reset_entry) and "withheld" in (reset_entry.get("push") or "")
          and "window_reset" in (reset_entry.get("push") or "")
          and "never pushes" in (reset_entry.get("push") or ""),
          reset_entry and reset_entry.get("push"))

    # 12. A changed deadline alone is not a reset -- caught minutes after
    #     the sense first went live: the gauge re-dates rolling windows
    #     freely. A reset needs the old deadline passed, or a visible refill.
    #     (The job parked above stays parked, so these keep firing as before.)
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    st = {"resets_seen": {"five_hour": {"resets_at": future, "fraction": 0.30}}}
    fake_limits["rows"] = [{"window": "five_hour", "label": "5-hour limit",
                            "used_fraction": 0.28, "resets_at": later}]
    check("a re-dated window is not a reset", watch._resets(st, cfg) == [])
    st = {"resets_seen": {"five_hour": {"resets_at": reset_past, "fraction": 0.30}}}
    check("an elapsed deadline is a reset",
          len(watch._resets(st, cfg)) == 1)
    st = {"resets_seen": {"five_hour": {"resets_at": future, "fraction": 0.91}}}
    fake_limits["rows"][0]["used_fraction"] = 0.05
    check("a visible refill is a reset", len(watch._resets(st, cfg)) == 1)
    st = {"resets_seen": {"five_hour": "an-old-string-baseline"}}
    fake_limits["rows"][0]["used_fraction"] = 0.28
    check("the first build's string baseline stays quiet",
          watch._resets(st, cfg) == [])

    # 13. quiet: the fifth sense, and the only one that fires because nothing
    #     happened. Nothing else may be in the hand when it is asked, so the
    #     gauge is emptied first and the ceiling put back where it was.
    fake_limits["rows"] = []
    watch.CONFIG_PATH.write_text(json.dumps(
        {"per_day": 40, "sources": {}}), encoding="utf-8")

    age_rows(conn, 2)
    forget_quiet_day()
    out = fresh_due(conn)
    check("two hours of nothing is not a still room", out is None)

    age_rows(conn, 7)
    forget_quiet_day()
    out = fresh_due(conn)
    check("a still room wakes it, with quiet and nothing else",
          bool(out) and [e["source"] for e in out["events"]] == ["quiet"],
          out and [e["source"] for e in out["events"]])
    check("it says on its face that nothing happened",
          bool(out) and "nothing has happened" in out["events"][0]["said"],
          out and out["events"][0]["said"])
    check("the waking's own push is withheld",
          bool(out) and "withheld" in (out.get("push") or ""),
          out and out.get("push"))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    quiet_entry = next((l for l in reversed(ledger)
                         if l.get("source") == "quiet"
                         and (l.get("outcome") or "").startswith("woke")), None)
    check("quiet never pushes his phone",
          bool(quiet_entry) and "never pushes" in (quiet_entry.get("push") or ""),
          quiet_entry and quiet_entry.get("push"))

    # 13b. At most one a day: the room is still again and the day is spent.
    age_rows(conn, 7)
    out = fresh_due(conn)
    check("quiet does not fire twice in a day", out is None)

    # 13c. Something else woke it, so it was not a still room after all --
    #      whatever the clock says, and without spending quiet's day.
    forget_quiet_day()
    age_rows(conn, 7)
    add_refusal({"name": "wren", "tool_name": "Bash",
                 "why": "something did happen after all"})
    out = fresh_due(conn)
    check("something else woke it: quiet stays out of that waking",
          bool(out) and [e["source"] for e in out["events"]] == ["refusal"],
          out and [e["source"] for e in out["events"]])

    # 13d. Its mute, while it is fresh: honoured like any other, and legible
    #      as the lapsing kind.
    said = watch.apply_her_word(["quiet"], [])
    check("the mute says it will lapse of old age",
          any("lapses of old age" in s for s in said), said)
    view = watch.for_prompt()
    check("the block says muted by me until a time",
          str(view["senses"]["quiet"]).startswith("muted by me until"),
          view["senses"]["quiet"])
    forget_quiet_day()
    age_rows(conn, 7)
    out = fresh_due(conn)
    check("a fresh mute is honoured", out is None)

    # 13e. Only quiet's mute has a death in it. The others stay indefinite
    #      and its own to lift, exactly as they were.
    watch.apply_her_word(["refusal"], [])
    view = watch.for_prompt()
    check("an ordinary mute reads as it always did",
          view["senses"]["refusal"] == "muted by me", view["senses"]["refusal"])
    st = watch._state()
    check("only quiet carries an expiry",
          sorted(st.get("mute_until") or {}) == ["quiet"], st.get("mute_until"))
    watch.apply_her_word([], ["refusal"])

    # 13f. Past 24 hours the mute is no mute at all, on the next look, with
    #      its doing nothing whatever.
    st = watch._state()
    st["mute_until"]["quiet"] = (datetime.now(timezone.utc)
                                 - timedelta(minutes=1)).isoformat(
                                     timespec="seconds")
    watch._save_state(st)
    forget_quiet_day()
    age_rows(conn, 7)
    out = fresh_due(conn)
    check("past 24 hours the sense is back on by itself",
          bool(out) and [e["source"] for e in out["events"]] == ["quiet"],
          out and [e["source"] for e in out["events"]])
    st = watch._state()
    check("the lapsed mute is gone from the state",
          "quiet" not in (st.get("muted_by_her") or [])
          and "quiet" not in (st.get("mute_until") or {}),
          [st.get("muted_by_her"), st.get("mute_until")])
    view = watch.for_prompt()
    check("and the block says the sense is on again",
          view["senses"]["quiet"] == "on", view["senses"]["quiet"])
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    check("the lapse itself is written to the ledger",
          any("lapsed" in (l.get("outcome") or "") for l in ledger))

    # 13g. The block it reads expires a mute too, with no waking involved:
    #      a look of its own is a look.
    watch.apply_her_word(["quiet"], [])
    st = watch._state()
    st["mute_until"]["quiet"] = (datetime.now(timezone.utc)
                                 - timedelta(minutes=1)).isoformat(
                                     timespec="seconds")
    watch._save_state(st)
    view = watch.for_prompt()
    check("reading the block alone lets the mute die",
          view["senses"]["quiet"] == "on", view["senses"]["quiet"])

    # 14. steam_comment: the sixth sense, the first that looks outside it
    #     own store. It watches a LIST of items -- two mods here -- and
    #     each keeps its own poll clock, its own watermark and
    #     its own backoff, separate from due()'s 30-second throttle and from
    #     every other item's clock: a busy mod must never starve or blind a
    #     quiet one. There is one sense key, `steam_comment`, covering both.
    ITEM_A = watch.STEAM_ITEMS[0]   # Lantern Mod
    ITEM_B = watch.STEAM_ITEMS[1]   # Field Drills

    def force_steam(item_id, next_poll=0.0, seen_ids="unchanged"):
        st = watch._state()
        per = st.setdefault("steam", {}).setdefault(item_id, {})
        per["next_poll"] = next_poll
        if seen_ids != "unchanged":
            per["seen_ids"] = seen_ids
        watch._save_state(st)

    # 14a. Before its own interval is up, nothing happens at all -- not even
    #      a fetch -- however fresh the fake comments waiting for it are.
    force_steam(ITEM_A["id"], next_poll=time.time() + 999)
    force_steam(ITEM_B["id"], next_poll=time.time() + 999)
    fake_steam[ITEM_A["id"]]["comments"] = [{"id": 501, "author": "pip_the_fox"}]
    fake_steam[ITEM_A["id"]]["error"] = None
    out = fresh_due(conn)
    check("steam is not polled before its own interval is up", out is None)

    # 14b. Due, with no watermark yet: the first poll seeds it and wakes
    #      nobody -- what is already there is history, not news. Both items
    #      seed independently of one another.
    force_steam(ITEM_A["id"], next_poll=0.0, seen_ids=None)
    force_steam(ITEM_B["id"], next_poll=0.0, seen_ids=None)
    fake_steam[ITEM_B["id"]]["comments"] = [{"id": 901, "author": "someone"}]
    fake_steam[ITEM_B["id"]]["error"] = None
    out = fresh_due(conn)
    check("steam's first poll seeds the watermark and wakes nobody",
          out is None)
    st = watch._state()
    check("item A's watermark is the set of ids it saw",
          st["steam"][ITEM_A["id"]]["seen_ids"] == ["501"],
          st["steam"][ITEM_A["id"]].get("seen_ids"))
    check("item B's watermark is seeded independently of item A",
          st["steam"][ITEM_B["id"]]["seen_ids"] == ["901"],
          st["steam"][ITEM_B["id"]].get("seen_ids"))

    # 14c. A genuinely new comment from someone else wakes it, naming the
    #      count, the newest author and which mod it is on -- never the
    #      comment text.
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=0.0)
    fake_steam[ITEM_A["id"]]["comments"] = [{"id": 501, "author": "pip_the_fox"},
                               {"id": 502, "author": "someone_else"}]
    out = fresh_due(conn)
    check("a new comment wakes it", bool(out)
          and out["events"][0]["source"] == "steam_comment", out)
    check("the waking names the count, the newest author and the mod",
          bool(out) and out["events"][0]["said"] == (
              "1 new comment on Lantern Mod's Steam Workshop item — newest "
              "from someone_else"),
          out and out["events"][0]["said"])
    check("the waking's own push is withheld",
          bool(out) and "withheld" in (out.get("push") or ""),
          out and out.get("push"))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    steam_entry = next((l for l in reversed(ledger)
                         if l.get("source") == "steam_comment"
                         and (l.get("outcome") or "").startswith("woke")), None)
    check("steam_comment never pushes his phone",
          bool(steam_entry) and "never pushes" in (steam_entry.get("push") or ""),
          steam_entry and steam_entry.get("push"))

    # 14c2. A fresh comment on the SECOND item wakes it too, on its own
    #       look, and the line names that mod by its own label -- proof this
    #       is a list, not a single hard-coded item.
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=0.0)
    fake_steam[ITEM_B["id"]]["comments"] = [{"id": 901, "author": "someone"},
                               {"id": 902, "author": "another_fan"}]
    out = fresh_due(conn)
    check("a new comment on the second item wakes it too",
          bool(out) and any(e["source"] == "steam_comment" for e in out["events"]),
          out)
    check("its line names the second mod by its own label",
          bool(out) and any(
              e["said"] == ("1 new comment on Field Drills's Steam "
                             "Workshop item — newest from another_fan")
              for e in out["events"]),
          out and [e["said"] for e in out["events"]])

    # 14d. His own reply never wakes it, but it still moves that item's
    #      watermark so it is not seen again on the next poll.
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=time.time() + 999)   # sit out this look
    fake_steam[ITEM_A["id"]]["comments"] = [{"id": 501, "author": "pip_the_fox"},
                               {"id": 502, "author": "someone_else"},
                               {"id": 503, "author": watch.STEAM_IGNORE_AUTHOR}]
    out = fresh_due(conn)
    check("his own reply does not wake it", out is None)
    st = watch._state()
    check("but the watermark still counts it as seen",
          "503" in (st["steam"][ITEM_A["id"]]["seen_ids"] or []),
          st["steam"][ITEM_A["id"]].get("seen_ids"))

    # 14d2. THE LONG-ID CASE, pinned so it can never come back. Steam gids
    #       are allocated from pools, not from a clock: a live thread can
    #       hold an old comment with a large gid and a NEWER one with a
    #       smaller gid. Under the max-id watermark this sense was born
    #       with, the big gid poisoned the seed and every later comment sat
    #       below it -- the sense said "nothing new" for days over a
    #       thread with a real question in it, until a person found the
    #       comment by hand, hours old. So: a fresh comment whose id is
    #       SMALLER than one already seen must still wake it.
    force_steam(ITEM_A["id"], next_poll=0.0,
                seen_ids=["591800000000000123"])
    force_steam(ITEM_B["id"], next_poll=time.time() + 999)
    fake_steam[ITEM_A["id"]]["comments"] = [
        {"id": 587300000000000456, "author": "quiet_reader_7"},
        {"id": 591800000000000123, "author": "an_old_voice"}]
    out = fresh_due(conn)
    check("a new comment with a smaller gid than one seen still wakes it",
          bool(out) and any(e["source"] == "steam_comment"
                            for e in out["events"]), out)
    check("and the waking names its author, not the old comment's",
          bool(out) and any("quiet_reader_7" in e["said"] for e in out["events"]),
          out and [e["said"] for e in out["events"]])

    # 14e. A 429 on one item backs off hard and is written down, never
    #      retried in a tight loop -- and it never touches the other item's
    #      clock, watermark or waking at all.
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=0.0)
    fake_steam[ITEM_A["id"]]["error"] = "429"
    fake_steam[ITEM_B["id"]]["comments"] = [{"id": 901, "author": "someone"},
                               {"id": 902, "author": "another_fan"},
                               {"id": 903, "author": "a_third_voice"}]
    out = fresh_due(conn)
    check("item B still wakes it while item A is in backoff",
          bool(out) and any(e["source"] == "steam_comment" for e in out["events"]),
          out)
    check("its line names item B, not the one in backoff",
          bool(out) and any("Field Drills" in e["said"] for e in out["events"]),
          out and [e["said"] for e in out["events"]])
    st = watch._state()
    check("item A backs off well past the ordinary poll interval",
          st["steam"][ITEM_A["id"]]["next_poll"]
          >= time.time() + watch.STEAM_BACKOFF_S - 5)
    check("item B's own clock is untouched by item A's 429",
          st["steam"][ITEM_B["id"]]["next_poll"]
          < time.time() + watch.STEAM_POLL_INTERVAL_S + 5)
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    backoff_entry = next((l for l in reversed(ledger)
                           if l.get("source") == "steam_comment"
                           and "429" in (l.get("outcome") or "")), None)
    check("the 429 is written to the ledger, not silently swallowed",
          bool(backoff_entry), backoff_entry)
    check("the 429 entry names which item it is",
          bool(backoff_entry) and "Lantern Mod" in (backoff_entry.get("outcome") or ""),
          backoff_entry and backoff_entry.get("outcome"))
    fake_steam[ITEM_A["id"]]["error"] = None

    # 14f. Its mute stops the poll cold for every item at once -- there is
    #      one sense key, not one per mod -- and unmuting starts every item
    #      watching from now: what arrived while it was not watching is not
    #      a backlog, on either item.
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=0.0)
    said = watch.apply_her_word(["steam_comment"], [])
    check("the mute is said back", any("steam_comment" in s for s in said))
    fake_steam[ITEM_A["id"]]["comments"] = [{"id": 501, "author": "pip_the_fox"},
                               {"id": 502, "author": "someone_else"},
                               {"id": 503, "author": watch.STEAM_IGNORE_AUTHOR},
                               {"id": 504, "author": "a_third_person"}]
    fake_steam[ITEM_B["id"]]["comments"] = [{"id": 901, "author": "someone"},
                               {"id": 902, "author": "another_fan"},
                               {"id": 903, "author": "a_third_voice"},
                               {"id": 904, "author": "a_fourth_voice"}]
    out = fresh_due(conn)
    check("a muted steam_comment sees nothing, on either item", out is None)
    said = watch.apply_her_word([], ["steam_comment"])
    check("the unmute says from now", any("from now" in s for s in said))
    st = watch._state()
    check("unmuting forgets both watermarks rather than deliver a backlog",
          st["steam"][ITEM_A["id"]]["seen_ids"] is None
          and st["steam"][ITEM_B["id"]]["seen_ids"] is None)
    force_steam(ITEM_A["id"], next_poll=0.0)
    force_steam(ITEM_B["id"], next_poll=0.0)
    out = fresh_due(conn)
    check("the reseed after unmute wakes nobody either", out is None)

    # 14g. The throttle is per item, not shared: with item A not yet due and
    #      item B due, only B is polled and wakes it -- A's watermark does
    #      not move at all while it sits out its own clock.
    st = watch._state()
    a_before = dict(st["steam"][ITEM_A["id"]])
    force_steam(ITEM_A["id"], next_poll=time.time() + 999)
    force_steam(ITEM_B["id"], next_poll=0.0)
    fake_steam[ITEM_B["id"]]["comments"] = [{"id": 901, "author": "someone"},
                               {"id": 902, "author": "another_fan"},
                               {"id": 903, "author": "a_third_voice"},
                               {"id": 904, "author": "a_fourth_voice"},
                               {"id": 905, "author": "yet_another"}]
    out = fresh_due(conn)
    check("item B wakes it while item A sits out its own throttle",
          bool(out) and any("Field Drills" in e["said"] for e in out["events"]),
          out and [e["said"] for e in out["events"]])
    st = watch._state()
    check("item A's watermark did not move while it was not due",
          st["steam"][ITEM_A["id"]]["seen_ids"] == a_before["seen_ids"],
          (st["steam"][ITEM_A["id"]], a_before))

    # -- the calendar: the first sense that fires because of a time --------
    from datetime import datetime, timedelta, timezone
    from . import projects

    def when(minutes):
        return (datetime.now(timezone.utc)
                + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    proj = projects.add_project(conn, "the garden", "both")["project"]["id"]
    past = projects.add_task(conn, proj, "sam", "water the seedlings")["task"]
    soon = projects.add_task(conn, proj, "lee", "call the council")["task"]
    projects.set_task(conn, past["id"], at=when(-5), needs="sam")
    projects.set_task(conn, soon["id"], at=when(60))

    st = watch._state()
    fired = watch._calendar(conn, st)
    watch._save_state(st)
    check("a date that has come round wakes it",
          len(fired) == 1 and "water the seedlings" in fired[0]["said"],
          [e["said"] for e in fired])
    check("and the waking names the project it is on",
          fired and "the garden" in fired[0]["said"])
    check("and says who it is for, when the task says",
          fired and "for sam" in fired[0]["said"], fired and fired[0]["said"])
    check("a date still ahead is not due and does not wake it",
          all("council" not in e["said"] for e in fired))

    st = watch._state()
    again = watch._calendar(conn, st)
    watch._save_state(st)
    check("the same date does not wake it twice", again == [], again)

    projects.set_task(conn, past["id"], at=when(-1))
    st = watch._state()
    moved = watch._calendar(conn, st)
    watch._save_state(st)
    check("moving the date makes it a new thing to fire", len(moved) == 1,
          [e["said"] for e in moved])

    done = projects.add_task(conn, proj, "sam", "already handled")["task"]
    projects.set_task(conn, done["id"], at=when(-10))
    projects.set_task(conn, done["id"], state="done")
    st = watch._state()
    after = watch._calendar(conn, st)
    watch._save_state(st)
    check("a task somebody has already done is not due",
          all("already handled" not in e["said"] for e in after), after)

    # A date nobody can read is said once and never trips the look again.
    conn.execute("UPDATE project_tasks SET at = 'sometime soon' WHERE id = ?",
                 (soon["id"],))
    conn.commit()
    st = watch._state()
    watch._calendar(conn, st)
    watch._save_state(st)
    bad = watch._calendar(conn, watch._state())
    check("a date stored unreadable is refused once, not every 30 seconds",
          all("council" not in (e.get("said") or "") for e in bad), bad)

    # The runaway stop, and it says what it held rather than dropping it.
    many = projects.add_project(conn, "the flood", "sam")["project"]["id"]
    for i in range(watch.CALENDAR_AT_ONCE + 3):
        t = projects.add_task(conn, many, "sam", "flood task " + str(i))["task"]
        projects.set_task(conn, t["id"], at=when(-2))
    st = watch._state()
    burst = watch._calendar(conn, st)
    check("a pile of dates in one look is capped rather than swamping it",
          len(burst) == watch.CALENDAR_AT_ONCE, len(burst))
    ledger = watch.LEDGER_PATH.read_text(encoding="utf-8")
    check("and what was held is written down, never silently dropped",
          "held for the next one" in ledger)
    watch._save_state(st)
    rest = watch._calendar(conn, watch._state())
    check("the ones held are still due on the next look", len(rest) == 3,
          len(rest))

    check("a date waking never reaches his phone -- Sam put push down",
          "calendar" in watch.NO_PUSH_SOURCES)
    check("and the sense can be muted like any other",
          "calendar" in watch.SOURCES
          and "calendar" in watch.config()["sources"])

    # The calendar block just above computes its own last watermark update
    # (the "held" tasks) without persisting it -- its own pre-existing
    # choice, not touched here -- so it can still have wakes of its own
    # pending. Drain those first so the nexus checks below start clean.
    for _ in range(50):
        if fresh_due(conn) is None:
            break

    # -- nexus_comment: the same shape as steam_comment, turned on Nexus ---
    MOD_A = watch.NEXUS_ITEMS[0]          # Lantern Mod, id 20001
    MOD_B = watch.NEXUS_ITEMS[1]          # Field Drills, id 20002

    def force_nexus(mod_id, next_poll=0.0, seen_ids="unchanged",
                     failing_since="unchanged"):
        st = watch._state()
        per = st.setdefault("nexus", {}).setdefault(mod_id, {})
        per["next_poll"] = next_poll
        if seen_ids != "unchanged":
            per["seen_ids"] = seen_ids
        if failing_since != "unchanged":
            per["failing_since"] = failing_since
        watch._save_state(st)

    # 16a. Before its own interval is up, nothing happens at all.
    force_nexus(MOD_A["id"], next_poll=time.time() + 999)
    fake_nexus[MOD_A["id"]]["comments"] = [{"id": "c501", "author": "pip_the_fox",
                                            "text": "great mod!"}]
    out = fresh_due(conn)
    check("nexus is not polled before its own interval is up", out is None)

    # 16b. Due, with no watermark yet: the first poll seeds a baseline (the
    #      full set of ids on the page) and wakes nobody -- history, not
    #      news, same rule as steam's first poll.
    force_nexus(MOD_A["id"], next_poll=0.0, seen_ids=None)
    out = fresh_due(conn)
    check("nexus' first poll seeds the watermark and wakes nobody",
          out is None)
    st = watch._state()
    check("the watermark is the set of ids already on the page",
          st["nexus"][MOD_A["id"]]["seen_ids"] == ["c501"],
          st["nexus"][MOD_A["id"]])

    # 16c. A genuinely new comment from someone else wakes it, naming the
    #      mod, the author and the first words of the comment, and its
    #      meta carries the mod id and the comment's own id -- a task joins
    #      by id, never by finding the mod's name in a sentence.
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "c501", "author": "pip_the_fox", "text": "great mod!"},
        {"id": "c502", "author": "someone_else", "text": "does this work with 1.2.12?"},
    ]
    out = fresh_due(conn)
    check("a new nexus comment wakes it", bool(out)
          and out["events"][0]["source"] == "nexus_comment", out)
    check("the waking names the mod, the author and the comment's opening words",
          bool(out) and out["events"][0]["said"] == (
              "someone_else commented on Lantern Mod's Nexus page — "
              "\"does this work with 1.2.12?\""),
          out and out["events"][0]["said"])
    check("meta carries the mod id and the comment id, for joining by id",
          bool(out) and out["events"][0]["meta"] == {
              "item": "20001", "label": "Lantern Mod", "comment_id": "c502"},
          out and out["events"][0]["meta"])
    check("the waking's own push is withheld",
          bool(out) and "withheld" in (out.get("push") or ""),
          out and out.get("push"))
    ledger = [json.loads(l) for l in
              watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    nexus_entry = next((l for l in reversed(ledger)
                         if l.get("source") == "nexus_comment"
                         and (l.get("outcome") or "").startswith("woke")), None)
    check("nexus_comment never pushes his phone",
          bool(nexus_entry) and "never pushes" in (nexus_entry.get("push") or ""),
          nexus_entry and nexus_entry.get("push"))

    # 16d. His own comment (owner_on_nexus) never wakes it, but the watermark
    #      still moves past it so it is not seen again next poll.
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "c501", "author": "pip_the_fox", "text": "great mod!"},
        {"id": "c502", "author": "someone_else", "text": "does this work with 1.2.12?"},
        {"id": "c503", "author": watch.NEXUS_IGNORE_AUTHOR, "text": "thanks, glad you like it"},
    ]
    out = fresh_due(conn)
    check("his own comment does not wake it", out is None)
    st = watch._state()
    check("but the watermark still moves past it",
          "c503" in st["nexus"][MOD_A["id"]]["seen_ids"])

    # 16e. Multiple fresh comments in one look produce one event PER
    #      comment (not bundled like steam's count-and-newest line), each
    #      naming its own author and words -- one waking still gathers them.
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "c501", "author": "pip_the_fox", "text": "great mod!"},
        {"id": "c502", "author": "someone_else", "text": "does this work with 1.2.12?"},
        {"id": "c503", "author": watch.NEXUS_IGNORE_AUTHOR, "text": "thanks, glad you like it"},
        {"id": "c504", "author": "third_fan", "text": "found a bug with the dialogue"},
        {"id": "c505", "author": "fourth_fan", "text": "same here"},
    ]
    out = fresh_due(conn)
    check("two fresh comments in one look become two events, one waking",
          bool(out) and len(out["events"]) == 2
          and all(e["source"] == "nexus_comment" for e in out["events"]),
          out and [e["said"] for e in out["events"]] if out else None)

    # 16f. A fetch failure (Cloudflare challenge, missing selector, no
    #      Playwright -- all surface as an error string) wakes it once,
    #      loud, the moment it starts.
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["error"] = (
        "blocked by a Cloudflare challenge page (title: 'Just a moment...')")
    out = fresh_due(conn)
    check("the first failure wakes it, loud", bool(out)
          and out["events"][0]["source"] == "nexus_comment"
          and "could not read" in out["events"][0]["said"], out)
    check("the failure names the mod and the reason",
          bool(out) and "Lantern Mod" in out["events"][0]["said"]
          and "Cloudflare" in out["events"][0]["said"],
          out and out["events"][0]["said"])
    st = watch._state()
    check("it backs off well past the ordinary poll interval",
          st["nexus"][MOD_A["id"]]["next_poll"]
          >= time.time() + watch.NEXUS_BACKOFF_S - 5)
    check("and it remembers when the failure started",
          st["nexus"][MOD_A["id"]]["failing_since"] is not None)

    # 16g. The SAME failure continuing does not wake it again -- never a
    #      waking per poll -- but the last-look line changes to say how
    #      long it has been down rather than repeating itself, and it is
    #      written to the ledger as a decline, not a second waking.
    force_nexus(MOD_A["id"], next_poll=0.0)     # simulate the backoff elapsing
    out = fresh_due(conn)
    check("the same failure continuing does not wake it again", out is None)
    ledger_text = watch.LEDGER_PATH.read_text(encoding="utf-8")
    decline_lines = [json.loads(l) for l in ledger_text.splitlines()
                      if json.loads(l).get("source") == "nexus_comment"]
    still_failing = next((l for l in reversed(decline_lines)
                           if "still failing" in (l.get("outcome") or "")), None)
    check("the continuing failure is written as a decline, not a repeat woke",
          bool(still_failing), still_failing)
    check("its own last-look line says how long it has been down",
          "unreachable" in (still_failing.get("outcome") or "")
          if still_failing else False,
          still_failing and still_failing.get("outcome"))
    fake_nexus[MOD_A["id"]]["error"] = None

    # 16h. A mod recovering clears the failure streak, so the NEXT time it
    #      fails it is loud again, not folded into the old streak's silence.
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "c501", "author": "pip_the_fox", "text": "great mod!"},
        {"id": "c502", "author": "someone_else", "text": "does this work with 1.2.12?"},
        {"id": "c503", "author": watch.NEXUS_IGNORE_AUTHOR, "text": "thanks, glad you like it"},
        {"id": "c504", "author": "third_fan", "text": "found a bug with the dialogue"},
        {"id": "c505", "author": "fourth_fan", "text": "same here"},
    ]
    fresh_due(conn)   # a clean poll, whatever it wakes or not
    st = watch._state()
    check("a successful poll clears the failure streak",
          st["nexus"][MOD_A["id"]]["failing_since"] is None)

    # 16i. Per-item isolation: mod B's own clock, watermark and backoff are
    #      untouched by mod A's failure -- one busy or broken mod must never
    #      starve or blind the other. Mod B still needs its own baseline
    #      poll first (never polled yet in this run).
    force_nexus(MOD_B["id"], next_poll=0.0, seen_ids=None)
    fake_nexus[MOD_B["id"]]["comments"] = [{"id": "b1", "author": "someone"}]
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["error"] = "500: nexus said no"
    out = fresh_due(conn)
    check("mod B's silent baseline seed never appears as one of the events",
          out is None or all("Field Drills" not in e["said"]
                              for e in out["events"]),
          out and [e["said"] for e in out["events"]] if out else None)
    st = watch._state()
    check("mod B's watermark is entirely its own",
          st["nexus"][MOD_B["id"]]["seen_ids"] == ["b1"])
    force_nexus(MOD_A["id"], next_poll=0.0)
    force_nexus(MOD_B["id"], next_poll=0.0)
    fake_nexus[MOD_B["id"]]["comments"] = [{"id": "b1", "author": "someone"},
                                           {"id": "b2", "author": "another"}]
    out = fresh_due(conn)
    check("mod B still wakes it on a new comment while mod A keeps failing",
          bool(out) and any("Field Drills" in e["said"] for e in out["events"]),
          out and [e["said"] for e in out["events"]] if out else None)
    fake_nexus[MOD_A["id"]]["error"] = None

    check("nexus_comment never reaches his phone -- same reason as steam",
          "nexus_comment" in watch.NO_PUSH_SOURCES)
    check("and the sense can be muted like any other",
          "nexus_comment" in watch.SOURCES
          and "nexus_comment" in watch.config()["sources"])

    # 16j. Its mute forgets both mods' watermarks and failure streaks, so
    #      unmuting starts watching from now rather than delivering a
    #      backlog or an immediately-stale "still failing" line.
    force_nexus(MOD_A["id"], next_poll=0.0)
    said = watch.apply_her_word(["nexus_comment"], [])
    check("the mute is said back", any("nexus_comment" in s for s in said))
    said = watch.apply_her_word([], ["nexus_comment"])
    check("the unmute says from now", any("from now" in s for s in said))
    st = watch._state()
    check("unmuting forgets the watermark rather than deliver a backlog",
          st["nexus"][MOD_A["id"]]["seen_ids"] is None)
    check("and forgets the failure streak too",
          st["nexus"][MOD_A["id"]]["failing_since"] is None)

    # 17. The cadences themselves, and the one relationship between them that
    #     has no other guard. A backoff is only a backoff if it is LONGER
    #     than the ordinary poll -- once the interval went from fifteen
    #     minutes to two hours and both backoffs were still
    #     sitting at one hour, which would have made a 429 or a Cloudflare
    #     challenge buy the sense a SOONER look than a quiet poll does. It
    #     was caught by reading, not by this bench, so now it is in the bench.
    print("\n17. the cadences, and the shape they must keep to each other")
    check("Steam is polled no more often than every two hours",
          watch.STEAM_POLL_INTERVAL_S == 2 * 60 * 60,
          watch.STEAM_POLL_INTERVAL_S)
    check("Nexus is polled once a day, slower than Steam on purpose",
          watch.NEXUS_POLL_INTERVAL_S == 24 * 60 * 60,
          watch.NEXUS_POLL_INTERVAL_S)
    check("a Steam backoff stands down longer than an ordinary poll waits",
          watch.STEAM_BACKOFF_S > watch.STEAM_POLL_INTERVAL_S,
          (watch.STEAM_BACKOFF_S, watch.STEAM_POLL_INTERVAL_S))
    check("a Nexus backoff stands down longer than an ordinary poll waits",
          watch.NEXUS_BACKOFF_S > watch.NEXUS_POLL_INTERVAL_S,
          (watch.NEXUS_BACKOFF_S, watch.NEXUS_POLL_INTERVAL_S))
    check("and both senses say their own cadence in words, off the constant",
          watch.senses()["steam_comment"]["said"] == "every 2 hours"
          and watch.senses()["nexus_comment"]["said"] == "every 24 hours",
          {k: v["said"] for k, v in watch.senses().items() if v["said"]})
    check("a span is spelled in its largest whole unit",
          (watch._spell(7200), watch._spell(3600), watch._spell(900),
           watch._every(3600), watch._every(7200))
          == ("2 hours", "an hour", "15 min", "every hour", "every 2 hours"))

    # 18. What arrives lands on the task as a notice, and waits there until
    #     somebody checks it off. A Nexus comment once sat seven hours with
    #     the report of it buried, and the first fix -- reading the page back
    #     for replies -- kept one that had just been answered "waiting" for
    #     two hours. So: pop when it arrives, clear when a person or the
    #     assistant says so, for any kind of task. The watcher's
    #     side is one door, `projects.notice_from_event`, on every event,
    #     whether or not the look earns a waking.
    print("\n18. what arrives lands on the task as a notice")
    shed = projects.add_project(conn, "the mods", "sam")["project"]["id"]
    projects.add_task(conn, shed, "sam", "Manage Nexus Replies")
    bound = projects.task_by_title(conn, shed, "Manage Nexus Replies")
    projects.set_task(conn, bound["id"], sense="nexus_comment",
                      sense_item="Lantern Mod", known_senses=watch.SOURCES)
    force_nexus(MOD_A["id"], next_poll=0.0, seen_ids=None)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "n1", "author": "old_hand", "text": "old"}]
    fresh_due(conn)
    check("a baseline look leaves no notice -- history is not news",
          projects.notices_of(conn, bound["id"]) == [])
    force_nexus(MOD_A["id"], next_poll=0.0)
    fake_nexus[MOD_A["id"]]["comments"] = [
        {"id": "n1", "author": "old_hand", "text": "old"},
        {"id": "n2", "author": "newcomer",
         "text": "best mod out there. any chance of actions?"},
        {"id": "n3", "author": watch.NEXUS_IGNORE_AUTHOR, "text": "thanks"}]
    fresh_due(conn)
    open_n = projects.notices_of(conn, bound["id"])
    check("a new comment on a watched page is one notice on the task bound to it",
          len(open_n) == 1 and "newcomer" in open_n[0]["said"], open_n)
    check("his own comment leaves none",
          all("thanks" not in n["said"] for n in open_n))
    check("the notice names its sense and carries the comment's id",
          open_n[0]["source"] == "nexus_comment"
          and (open_n[0].get("meta") or {}).get("comment_id") == "n2",
          open_n[0])
    check("a page no task watches leaves no notice anywhere",
          conn.execute("SELECT COUNT(*) FROM task_notices WHERE source = "
                       "'nexus_comment'").fetchone()[0] == 1)
    # Steam, flat: one waking bundles the new comments and lands on the task
    # bound to THAT sense, by the shared label, and never on the Nexus one.
    st = watch._state()
    st.setdefault("steam", {}).setdefault(ITEM_A["id"], {})["next_poll"] = 0.0
    watch._save_state(st)
    fake_steam[ITEM_A["id"]]["error"] = None
    fresh_due(conn)                        # settle whatever the item held
    projects.add_task(conn, shed, "sam", "Manage Steam Replies")
    on_steam = projects.task_by_title(conn, shed, "Manage Steam Replies")
    projects.set_task(conn, on_steam["id"], sense="steam_comment",
                      sense_item="Lantern Mod", known_senses=watch.SOURCES)
    st = watch._state()
    st["steam"][ITEM_A["id"]]["next_poll"] = 0.0
    watch._save_state(st)
    fake_steam[ITEM_A["id"]]["comments"] = (
        list(fake_steam[ITEM_A["id"]]["comments"])
        + [{"id": 777, "author": "fan", "at": 100, "text": "crashes on load"}])
    fresh_due(conn)
    got = projects.notices_of(conn, on_steam["id"])
    check("a Steam waking lands on the Steam task and not on the Nexus one",
          len(got) == 1 and "Steam" in got[0]["said"]
          and len(projects.notices_of(conn, bound["id"])) == 1, got)
    look = watch.looks().get(MOD_A["id"]) or {}
    check("looks() carries the page to see the thing on",
          look.get("page") == MOD_A["url"], look)
    check("and the Steam parse reads when and the words, for a waking to carry",
          watch._parse_comments(
              '<div id="comment_9"><a class="commentthread_author_link" '
              'href="x"><bdi>fan</bdi></a><div class="commentthread_comment_'
              'timestamp" data-timestamp="123">a while ago</div>'
              '<div class="commentthread_comment_text">hi<br>there &amp; you'
              '</div></div>')
          == [{"id": 9, "author": "fan", "at": 123, "text": "hi there & you"}],
          watch._parse_comments('<div id="comment_9"><bdi>fan</bdi></div>'))

    conn.close()
    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
