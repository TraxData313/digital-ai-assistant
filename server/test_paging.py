"""Lean on the paged room without a model. Run over a scratch folder:

    python -m server.test_paging C:\\somewhere\\scratch

A side room comes up on port 8790 over whatever store is in that folder --
`store.db` if one is already there, a made-up one if not -- and never a turn
loop, so nothing here can think, spend, or write to the live store. The live
data folder is refused by name.

What it leans on is the one thing paging can get wrong quietly: losing a row.
So the whole point of it is property 6 -- walk every page back to the
beginning, and prove that what came over the wire is the store, exactly:
every row, every event, every trail, once each, in order.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import app, brain, db

PORT = 8790
BASE = "http://127.0.0.1:" + str(PORT)

FAILED = []


def check(name, ok, detail=""):
    mark = "ok " if ok else "FAIL"
    print("  " + mark + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def get(path):
    try:
        with urllib.request.urlopen(BASE + path) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def weigh(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def make_a_store(conn):
    """Enough of a life to page through: talk, essences, an errand that came
    home, and events hung on both ways they hang."""
    worker_rows = []
    for i in range(1, 301):
        if i % 25 == 0:
            rid = db.add_row(conn, "essence", "an essence, number " + str(i),
                             replaces=[i - 2, i - 1], title="essence " + str(i))
        elif i % 40 == 0:
            rid = db.add_row(conn, "worker", "a hand's report, number " + str(i),
                             meta={"name": "reader", "page": i})
            worker_rows.append(rid)
        elif i % 2:
            rid = db.add_row(conn, "user", "something he said, number " + str(i))
        else:
            rid = db.add_row(conn, "assistant", "something it said, number " + str(i))
        if i % 3 == 0:
            db.add_event(conn, rid, "turn", "the turn took 1.0s",
                         {"steps": [{"at": 0.1, "text": "a step", "kind": "plain"}],
                          "seconds": 1.0})
    # A worker's account hangs off its newest reply and names the report row
    # inside its own detail. That second way of belonging is the one a plain
    # `IN (reply_row)` would drop.
    for rid in worker_rows:
        db.add_event(conn, db.last_reply_row(conn), "worker",
                     "an errand came home", {"row": rid, "ended": "completed"})
    # Most of it is behind it; a handful is what it is holding.
    ids = [r["id"] for r in conn.execute("SELECT id FROM rows ORDER BY id")]
    db.unload(conn, ids[:-12])


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live store")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch == live or scratch == live.parent:
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)
    db.DB_PATH = scratch / "store.db"
    made = not db.DB_PATH.exists()

    conn = db.connect()
    if made:
        make_a_store(conn)
    rows_now = db.all_rows(conn)
    events_now = db.all_events(conn)
    index = db.trail_index(conn)
    trails_now = {r["id"]: brain.sources_block(index, r["id"])
                  for r in rows_now if r["kind"] == "essence"}
    print("the paged room, leant on over " + str(db.DB_PATH)
          + ("  (made up)" if made else "  (a copy)"))
    print("  " + str(len(rows_now)) + " rows, " + str(len(events_now))
          + " events, " + str(len(trails_now)) + " trails, "
          + format(weigh(rows_now) / 1e6, ".2f") + " MB + "
          + format(weigh(events_now) / 1e6, ".2f") + " MB whole")

    room = app.OneRoom(("127.0.0.1", PORT), app.Handler)
    threading.Thread(target=room.serve_forever, daemon=True).start()
    try:
        lean_on(conn, rows_now, events_now, trails_now)
    finally:
        room.shutdown()
        conn.close()

    print()
    if FAILED:
        print("  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("  all of it holds.")


def lean_on(conn, rows_now, events_now, trails_now):
    loaded = [r["id"] for r in rows_now if r["loaded"]]
    unloaded = [r["id"] for r in rows_now if not r["loaded"]]

    # 1. A look at the room carries its working set whole and a page of the
    #    past -- never all of it.
    _, st = get("/api/state")
    got = [r["id"] for r in st["rows"]]
    check("the state carries every row it is holding",
          set(loaded) <= set(got), str(len(loaded)) + " loaded, "
          + str(len(set(loaded) - set(got))) + " missing")
    check("the state does not carry the whole past",
          len(got) < len(rows_now), str(len(got)) + " of " + str(len(rows_now)))
    check("its page of the past is the newest " + str(db.PAGE_ROWS),
          [i for i in got if i in set(unloaded)] == unloaded[-db.PAGE_ROWS:])
    check("it says where the next page starts", st["oldest"] == unloaded[-db.PAGE_ROWS],
          st["oldest"])
    check("it says how much it held back",
          st["older"] == len(unloaded) - db.PAGE_ROWS, st["older"])
    check("it counts the whole of it",
          st["counts"]["rows"] == len(rows_now)
          and st["counts"]["past"] == len(unloaded), st["counts"])
    check("the keys the room already knew are all still there",
          {"contract", "prompt", "rows", "trails", "events", "last_turn",
           "backup"} <= set(st), sorted(st))

    # 2. Every event belonging to those rows comes with them -- including a
    #    worker's account, which belongs by naming its row in its own detail.
    want = {e["id"] for e in events_now
            if e["reply_row"] in set(got)
            or (e["kind"] == "worker" and (e["detail"] or {}).get("row") in set(got))}
    check("the events for those rows all came",
          {e["id"] for e in st["events"]} == want,
          str(len(want)) + " wanted, " + str(len(st["events"])) + " came")
    check("and no others", all(
        e["reply_row"] in set(got)
        or (e["detail"] or {}).get("row") in set(got) for e in st["events"]))

    # A hand's account belongs to its report by naming it inside its own
    # detail, not by `reply_row`. So ask for the page that report sits on and
    # check the account came with it -- fetching only by `reply_row` would page
    # a report in with no run under it, which reads as an errand that did
    # nothing. Any store with an errand in it can answer this.
    hands = [e for e in events_now
             if e["kind"] == "worker" and (e["detail"] or {}).get("row")]
    if hands:
        one = hands[-1]
        room_for = (one["detail"]["row"], one["reply_row"] or 0)
        _, p = get("/api/rows?before=" + str(max(room_for) + 1) + "&limit=400")
        carried = {r["id"] for r in p["rows"]}
        check("a hand's account came with its report",
              one["detail"]["row"] not in carried
              or one["id"] in {e["id"] for e in p["events"]},
              "report row " + str(one["detail"]["row"]))
    else:
        print("  --   no errand in this store to check an account against")

    # 3. Trails, only for the essences in hand.
    check("a trail for every essence carried, and no more",
          set(map(int, st["trails"])) ==
          {r["id"] for r in st["rows"] if r["kind"] == "essence"})

    # 4. The size of a look at the room is set by the page, not by how much
    #    of it there is. That is the whole fix: the old one carried every row
    #    and every event, so it grew for as long as it lives.
    whole = weigh(rows_now) + weigh(events_now)
    print("     one look at the room: " + format(weigh(st) / 1e6, ".2f")
          + " MB, against " + format(whole / 1e6, ".2f") + " MB of it")
    check("a look at the room is a page, not the whole store",
          len(st["rows"]) <= db.PAGE_ROWS + len(loaded)
          and weigh(st) < 1_000_000, format(weigh(st) / 1e6, ".2f") + " MB")

    # 5. The bounds hold. A page asked for too big is a page; a `before` that
    #    is not a number is not a bound of its own.
    code, said = get("/api/rows?limit=50")
    check("paging back with no `before` is refused", code == 400, said)
    _, big = get("/api/rows?before=" + str(st["oldest"]) + "&limit=99999")
    check("a page asked for too big is capped",
          len(big["rows"]) <= db.MAX_PAGE_ROWS, len(big["rows"]))
    _, junk = get("/api/rows?before=" + str(st["oldest"]) + "&limit=lots")
    check("a limit that is not a number falls back",
          len(junk["rows"]) == min(db.PAGE_ROWS, st["oldest"] - 1),
          len(junk["rows"]))
    _, none = get("/api/rows?before=1&limit=50")
    check("paging back past the beginning gives nothing, and says so",
          none["rows"] == [] and none["older"] == 0 and none["oldest"] == 1)

    # 6. The whole point: walk every page back and prove the wire carried the
    #    store, exactly -- every row, every event, every trail, once each.
    rows, events, trails = {}, {}, {}

    def keep(p):
        for r in p["rows"]:
            rows[r["id"]] = r
        for e in p["events"]:
            events[e["id"]] = e
        trails.update(p["trails"])

    keep(st)
    pages, floor, still = 0, st["oldest"], st["older"]
    while still > 0 and pages < 200:
        _, p = get("/api/rows?before=" + str(floor) + "&limit=" + str(db.PAGE_ROWS))
        if not p["rows"]:
            break
        keep(p)
        floor, still, pages = p["oldest"], p["older"], pages + 1

    check("paging back reaches the beginning", still == 0, still)
    check("every row of its own came over the wire, once",
          sorted(rows) == [r["id"] for r in rows_now],
          str(len(rows)) + " of " + str(len(rows_now)) + " in " + str(pages)
          + " pages")
    check("every event came too",
          sorted(events) == [e["id"] for e in events_now],
          str(len(events)) + " of " + str(len(events_now)))
    check("every essence's trail came with it",
          {int(k): v for k, v in trails.items()} == trails_now,
          str(len(trails)) + " of " + str(len(trails_now)))
    check("and the rows say the same as the store",
          all(rows[r["id"]] == r for r in rows_now))

    # 7. The other half of the two seconds: the room asked the speak server
    #    whether it has a voice on every single look, and on a machine where
    #    nothing is listening on that port each ask is the whole timeout.
    brain._VOICE_SEEN.update(at=0.0, was=None)
    first = time.time()
    get("/api/state")
    first = time.time() - first
    again = time.time()
    _, second = get("/api/state")
    again = time.time() - again
    check("a second look does not ask after its voice again",
          again < max(0.5, first / 2),
          format(first, ".2f") + "s then " + format(again, ".2f") + "s")
    check("and it says the same thing about it",
          second["contract"]["voice"] == st["contract"]["voice"])

    # 8. A row put out of memory while the room holds it: the state stops
    #    carrying it, which is how the page knows to grey it.
    if loaded:
        db.unload(conn, [loaded[-1]])
        try:
            _, after = get("/api/state")
            check("a row let go of drops out of the state",
                  loaded[-1] not in {r["id"] for r in after["rows"]}
                  or loaded[-1] >= after["oldest"])
        finally:
            db.reload_rows(conn, [loaded[-1]])


if __name__ == "__main__":
    main()
