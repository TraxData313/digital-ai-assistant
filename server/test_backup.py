"""Lean on the nightly backup machinery without a model. Run over a scratch
folder:

    python -m server.test_backup C:\\somewhere\\scratch

The store and the night's before/after copies are both pointed into the
scratch first; nothing here touches `data/store.db` or `data_backups/`.
Checks the before-copy exists, the after-copy exists, a night that ends
badly still leaves the before-copy (and still takes the after-copy), the
cap prunes oldest first until the folder is under budget, and the newest
copy is never pruned -- even alone, over the cap.
"""

import os
import sys
import time
from pathlib import Path

from . import backup, brain, db, dream


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _fake_answer(**over):
    """A minimal answer through DREAM_SCHEMA's own shape -- a quiet night,
    nothing folded, nothing named, nothing retired."""
    answer = {"done": True, "looking": None, "read_essences": [],
              "read_rows": [], "fold": [], "name": [], "retire": [],
              "report": None, "line": "a quiet night", "sentence": None}
    answer.update(over)
    answer["_meta"] = {"input_tokens": 10, "output_tokens": 10,
                        "cost_usd": 0.0, "model": "test"}
    return answer


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live data")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live_data = Path(__file__).resolve().parent.parent / "data"
    live_backups = Path(__file__).resolve().parent.parent / "data_backups"
    if scratch in (live_data, live_data.parent, live_backups):
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)

    # Point every path this touches into the scratch before anything opens
    # anything -- the store itself, and where the night's copies land.
    db.DB_PATH = scratch / "store.db"
    backup.DATA_BACKUPS = scratch / "data_backups"
    conn = db.connect()
    db.add_row(conn, "user", "hello, a quiet evening", meta={})
    conn.commit()

    print("the nightly backup, leant on over " + str(scratch))

    said = []
    say = lambda t, k="plain", d=None: said.append((k, str(t)))

    # 0. The room's own default cap is the one gigabyte asked for -- checked
    #    here as a fact, not exercised with a real gigabyte of disk below.
    check("the default cap is one gigabyte",
          backup.NIGHT_CAP_BYTES == 1_000_000_000)

    # 1. A before-copy, taken directly.
    before = backup.night_copy("before", say=say)
    check("the before-copy is taken and checks out",
          before["ok"] and Path(before["file"]).exists())
    check("its name carries the timestamp and says 'before'",
          Path(before["file"]).name.endswith("-before.db"))

    # 2. An after-copy, taken directly, a different file from the before-copy.
    after = backup.night_copy("after", say=say)
    check("the after-copy is taken and checks out",
          after["ok"] and Path(after["file"]).exists())
    check("its name says 'after'", Path(after["file"]).name.endswith("-after.db"))
    check("before and after are two distinct files",
          before["file"] != after["file"])

    # 3. A whole night, through dream.dream() itself, ending well: both
    #    copies land around it.
    night_dir = backup.DATA_BACKUPS
    for p in night_dir.glob(backup.STAMP + "*.db"):
        p.unlink()
    brain.call_claude = lambda *a, **k: _fake_answer()
    result = dream.dream(conn, "2026-08-30", "asked", False, model="test",
                         say=say)
    files = sorted(night_dir.glob(backup.STAMP + "*.db"))
    check("a clean night is reported done", result["status"] == "done")
    check("a clean night leaves exactly a before-copy and an after-copy",
          len(files) == 2
          and any(f.name.endswith("-before.db") for f in files)
          and any(f.name.endswith("-after.db") for f in files),
          [f.name for f in files])

    # 4. A night that ends badly: the before-copy survives it, and the
    #    after-copy is still taken -- that is the whole point of running it
    #    in `finally` rather than after the `try`.
    for p in night_dir.glob(backup.STAMP + "*.db"):
        p.unlink()

    def _broken(*a, **k):
        raise RuntimeError("brain is unreachable, on purpose")
    brain.call_claude = _broken
    result = dream.dream(conn, "2026-08-30", "asked", False, model="test",
                         say=say)
    files = sorted(night_dir.glob(backup.STAMP + "*.db"))
    check("a night that breaks is reported broken", result["status"] == "broke")
    check("the before-copy is still there after a broken night",
          any(f.name.endswith("-before.db") for f in files), [f.name for f in files])
    check("the after-copy is taken even though the night broke",
          any(f.name.endswith("-after.db") for f in files), [f.name for f in files])

    # 5. The cap: oldest pruned first, until the folder is back under
    #    budget. Sizes and the cap are both kept tiny here on purpose --
    #    the algorithm is what is under test, not a real gigabyte of disk.
    cap_dir = scratch / "cap"
    cap_dir.mkdir(parents=True, exist_ok=True)
    names = [backup.STAMP + "20260101-000000-before.db", backup.STAMP + "20260101-000100-after.db",
             backup.STAMP + "20260102-000000-before.db", backup.STAMP + "20260102-000100-after.db",
             backup.STAMP + "20260103-000000-before.db"]
    for i, name in enumerate(names):
        p = cap_dir / name
        p.write_bytes(b"0" * 300)          # 300 bytes each, 1500 bytes total
        t = time.time() - (len(names) - i) * 10   # oldest first, unambiguous
        os.utime(p, (t, t))
    pruned = backup.prune_night_backups(cap_bytes=1000, dest=cap_dir, say=say)
    remaining = sorted(p.name for p in cap_dir.glob("*.db"))
    total = sum(p.stat().st_size for p in cap_dir.glob("*.db"))
    check("pruning brings the folder back under the cap", total <= 1000, total)
    check("the two oldest were the ones removed",
          pruned["removed"] == names[:2], pruned["removed"])
    check("the newest file was never touched",
          names[-1] in remaining, remaining)
    check("it said how much room it freed", pruned["freed_bytes"] == 600,
          pruned["freed_bytes"])

    # 6. The newest copy is never pruned, even alone and over the cap.
    lone_dir = scratch / "lone"
    lone_dir.mkdir(parents=True, exist_ok=True)
    lone = lone_dir / (backup.STAMP + "20260101-000000-before.db")
    lone.write_bytes(b"0" * 1200)          # alone, over a 1000-byte cap
    pruned = backup.prune_night_backups(cap_bytes=1000, dest=lone_dir, say=say)
    check("the one copy that exists is never pruned, even over the cap",
          lone.exists() and pruned["removed"] == [])

    conn.close()

    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
