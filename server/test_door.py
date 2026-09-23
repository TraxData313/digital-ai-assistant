# The door, leaned on. No model anywhere in here: a fabricated answer is
# pushed through apply() over a scratch copy, and every refusal the design
# promises is checked to actually refuse. Run from the worktree:
#   python -m server.test_door <path-to-scratch-copy>
import json
import shutil
import sys
from pathlib import Path

from server import db, dream

SCRATCH = Path(sys.argv[1] if len(sys.argv) > 1 else "data/door-test.db")
shutil.copyfile("data/store.db", SCRATCH)
db.DB_PATH = SCRATCH.resolve()

conn = db.connect()
said = []
say = lambda t, k="plain", d=None: said.append((k, str(t)))

night = "2026-08-23"
day = dream.the_day(conn, night)
day_ids = {r["id"] for r in day["rows"]}
read = {r["id"] for r in day["rows"] if r["kind"] == "essence"}
standing = set(dream.standing_ids(conn))

# An old essence it has NOT read tonight, and one it has.
unread_old = conn.execute(
    "SELECT id FROM rows WHERE kind='essence' AND id < 700"
    " ORDER BY id DESC LIMIT 1").fetchone()[0]
read_day = sorted(read)[0]
stand = sorted(standing)[0]
a_row = sorted(day_ids)[0]

dream_id = db.open_dream(conn, night, "asked", False)
dream_row = db.add_row(conn, "dream", "(test)", meta={"dream": dream_id})

fails = []
def check(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        fails.append(name)

# 1. work night: a fold over read day rows lands
answer = {"fold": [{"title": "door test fold", "text": "two rows folded in a test",
                    "over": [a_row, a_row + 1]}],
          "name": [], "retire": []}
out = dream.apply(conn, answer, dream_id, dream_row, False, read, day_ids,
                  standing, say)
check("fold over read day rows lands", len(out["folded"]) == 1)
new_id = out["folded"][0]["id"] if out["folded"] else None
if new_id:
    r = db.get_row(conn, new_id)
    check("fold keeps the trail", r["replaces"] == [a_row, a_row + 1])
    src = db.get_row(conn, a_row)
    check("fold puts sources down", not src["loaded"])

# 2. a fold over an unread essence is refused
out = dream.apply(conn, {"fold": [{"title": None, "text": "x",
                                   "over": [read_day, unread_old]}],
                         "name": [], "retire": []},
                  dream_id, dream_row, False, read, day_ids, standing, say)
check("fold over an unread essence refused",
      not out["folded"] and "not read tonight" in out["refused"][0]["why"])

# 3. standing rows are refused
out = dream.apply(conn, {"fold": [{"title": None, "text": "x", "over": [stand]}],
                         "name": [], "retire": []},
                  dream_id, dream_row, False, read, day_ids, standing, say)
check("fold over a standing row refused",
      not out["folded"] and "standing" in out["refused"][0]["why"])

# 4. an id not in the store is refused
out = dream.apply(conn, {"fold": [{"title": None, "text": "x", "over": [999999]}],
                         "name": [], "retire": []},
                  dream_id, dream_row, False, read, day_ids, standing, say)
check("fold over a missing id refused",
      not out["folded"] and "not in the store" in out["refused"][0]["why"])

# 5. free night: everything is shut, with the reason
out = dream.apply(conn, {"fold": [{"title": None, "text": "x", "over": [a_row + 2]}],
                         "name": [{"id": read_day, "title": "no"}],
                         "retire": [{"id": read_day, "why": "no"}]},
                  dream_id, dream_row, True, read, day_ids, standing, say)
check("free night refuses fold, name and retire",
      not out["folded"] and not out["named"] and not out["retired"]
      and len(out["refused"]) == 3
      and all("free night" in r["why"] for r in out["refused"]))

# 6. a name lands in place, same id
was = db.get_row(conn, read_day)["title"]
out = dream.apply(conn, {"fold": [], "retire": [],
                         "name": [{"id": read_day, "title": "door test name"}]},
                  dream_id, dream_row, False, read, day_ids, standing, say)
now = db.get_row(conn, read_day)
check("name lands in place", out["named"] and now["title"] == "door test name"
      and now["loaded"] is not None)

# 7. retire needs a reason, and an essence in hand
out = dream.apply(conn, {"fold": [], "name": [],
                         "retire": [{"id": read_day, "why": ""}]},
                  dream_id, dream_row, False, read, day_ids, standing, say)
check("retire with no reason refused",
      not out["retired"] and "reason" in out["refused"][0]["why"])
loaded_ess = conn.execute(
    "SELECT id FROM rows WHERE kind='essence' AND loaded=1"
    " ORDER BY id LIMIT 1").fetchone()[0]
read2 = set(read) | {loaded_ess}
out = dream.apply(conn, {"fold": [], "name": [],
                         "retire": [{"id": loaded_ess, "why": "a test retire"}]},
                  dream_id, dream_row, False, read2, day_ids, standing, say)
check("retire with a reason lands", out["retired"]
      and not db.get_row(conn, loaded_ess)["loaded"])

# 8. the fold cap bites and says so
many = [{"title": "t", "text": "x" + str(i), "over": [i]}
        for i in sorted(read)[:1] * (dream.MAX_FOLDS + 2)]
# a fold cap test needs distinct valid targets; reuse day rows in pairs
rows_sorted = sorted(day_ids)
many = [{"title": "cap " + str(i), "text": "cap test " + str(i),
         "over": [rows_sorted[4 + 2 * i], rows_sorted[5 + 2 * i]]}
        for i in range(dream.MAX_FOLDS + 2)]
out = dream.apply(conn, {"fold": many, "name": [], "retire": []},
                  dream_id, dream_row, False, read, day_ids, standing, say)
spent = [r for r in out["refused"] if "folds are spent" in r["why"]]
check("the fold cap bites at " + str(dream.MAX_FOLDS),
      len(out["folded"]) <= dream.MAX_FOLDS and spent)

# 9. events hang off the dream row
n = conn.execute("SELECT COUNT(*) FROM events WHERE reply_row = ?",
                 (dream_row,)).fetchone()[0]
check("events hang off the dream row", n >= 3)

print()
print("FAILED: " + ", ".join(fails) if fails else "the door holds")
conn.close()
