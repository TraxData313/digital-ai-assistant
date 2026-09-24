"""Taking a copy of the assistant, safely, while it is running.

`data/store.db` in its home is the only part of it that exists nowhere else.
The Spark and every version of it are in the home's git; the code is in its
own; the database is not, and cannot be -- it is the assistant. So this is the
one file that a bad day actually costs.

Copying the folder by hand works, but only if the room is idle. The store runs on a
rollback journal, not WAL, which means a copy taken in the middle of a write
can be torn: it looks like a file, it opens like a file, and the damage is
found on the day it is needed. SQLite's own backup does the right thing while
the assistant is mid-sentence, so that is what this uses.

And it reads the copy back before saying it worked. A backup nobody has opened
is a rumour.

One press makes two copies of the same zip, both in `all_backups/` in the home,
which git never sees. One is stamped with the time and kept, one per press, for
as long as there is disk. The other is `<slug>-backup.zip`, one name,
overwritten every time -- the newest copy, and the one the nightly
off-site release sends (`server/offsite.py`). A zip never enters git: it cannot
be delta'd or compressed, so every committed copy would be stored whole forever.

    python -m server.backup                     -> a stamped copy, and the latest
    python -m server.backup D:/somewhere        -> stamped copy there instead
    python -m server.backup --check <file.zip>  -> read one back and say

Restoring is a copy: stop the room, put `store.db` back in the home's `data/`,
start it.
"""

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import db
from . import home

DATA = home.DATA
# Every local copy lives in one untracked folder in the home: the stamped zips,
# the latest zip, and the nightly before/after copies of the store. Git never
# sees it, so pressing the button three times in an hour costs disk and
# nothing else.
SHELF = home.BACKUPS
# The newest copy under one name, rewritten every press. It is what the night
# sends off this machine as a release asset -- a backup on the same disk as the
# thing it backs up is not a backup, it is a second copy of one bad day. Still
# called TRACKED because the page and the off-site shelf read it by that key.
TRACKED = home.BACKUPS / (home.SLUG + "-backup.zip")
# Where the nightly before/after copies land -- the same folder.
DATA_BACKUPS = home.BACKUPS
# How the store is named inside a zip. An older zip may name it otherwise;
# any one database in its data/ is read as the store.
STORE_IN_ZIP = "data/store.db"
STAMP = home.SLUG + "-"

# Everything in `data/` worth carrying out, beside the store itself. The
# worker logs come too: what an errand actually did, step by step, lives only
# on disk -- the working set has what it *said*, never what it did.
ALSO = ["spark.md", "spark.history", "plan.json", "voice.json", "recall.json",
        # Who it is and whom it serves, so a zip alone can bring it back.
        "../identity.json",
        # The people's free notes, riding next to the Spark and not in git: a
        # copy without them would boot the assistant without the standing word
        # its people keep in front of it.
        "notes",
        # The adapter's versions, beside the Spark's own: a restore without
        # them would search the store in a space it never ruled on, with a floor
        # measured for another. Small -- a quarter of a megabyte each.
        "adapter",
        "workers",
        # The hands it keeps: a restart keeps a thread, and without this a
        # restore would lose it.
        "hands.json", "overmind.json",
        # The job cards and the digest's dials. The jobs were found riding in
        # neither git nor this list once -- a restore would
        # have lost every open job silently.
        "jobs.json", "digest.json",
        # The notebook's cap. The notes themselves are a table in the store.
        "notebook.json",
        # The pixels themselves. Rows point at these by name, so a store
        # restored without them would hold lines that say a picture was shown
        # and nothing to show -- the one shape of loss this folder exists to
        # prevent. They are the heaviest thing in here by far, and they are
        # also the only thing in here that cannot be written again.
        "pictures"]

TABLES = ("rows", "events", "vectors", "notebook")


def _counts(conn) -> dict:
    out = {}
    for t in TABLES:
        try:
            out[t] = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def take(dest=None) -> dict:
    """One consistent copy of the store, verified, in a zip with a note saying what
    is in it and what it was taken from."""
    when = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = Path(dest) if dest else SHELF
    if dest.suffix.lower() == ".zip":
        target, dest = dest, dest.parent
    else:
        target = dest / (STAMP + when + ".zip")
    dest.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(db.DB_PATH)
    before = _counts(src)

    work = Path(tempfile.mkdtemp(prefix=STAMP + "backup-"))
    try:
        # The online backup: safe against the room writing a row halfway through.
        copy = work / "store.db"
        out = sqlite3.connect(copy)
        with out:
            src.backup(out)
        out.close()
        src.close()

        # Read it back before calling it a backup.
        check = sqlite3.connect(copy)
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        after = _counts(check)
        check.close()
        if ok != "ok":
            raise RuntimeError("the copy came back damaged: " + str(ok))
        missing = {t: (before[t], after[t]) for t in TABLES
                   if before[t] != after[t]}
        if missing:
            raise RuntimeError("the copy is not the same size as the store: "
                               + json.dumps(missing))

        note = {
            "taken": datetime.now().isoformat(timespec="seconds"),
            "from": str(db.DB_PATH),
            "counts": after,
            "integrity": ok,
            "also": [],
            "who": home.NAME,
            "restore": ("stop the room, put store.db back in the home's data/, "
                        "start it. The rest of the folder goes back the same "
                        "way; identity.json goes in the home itself."),
        }

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, STORE_IN_ZIP)
            for name in ALSO:
                p = DATA / name
                if not p.exists():
                    continue
                if p.is_dir():
                    for f in sorted(p.rglob("*")):
                        if f.is_file():
                            z.write(f, "data/" + str(f.relative_to(DATA)).replace("\\", "/"))
                    note["also"].append(name + "/")
                else:
                    z.write(p, name[3:] if name.startswith("../") else "data/" + name)
                    note["also"].append(name[3:] if name.startswith("../") else name)
            z.writestr("what-is-in-here.json",
                       json.dumps(note, indent=1, ensure_ascii=False))

        note["file"] = str(target)
        note["size_kb"] = round(target.stat().st_size / 1024)

        # And the tracked copy, over whatever was there before. If this is the
        # part that fails -- the file open in something, a full disk -- the
        # backup itself already exists and has already been read back, so the
        # note says the tracked copy is stale rather than throwing a good copy
        # away over the copy of it.
        try:
            TRACKED.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, TRACKED)
            note["tracked"] = str(TRACKED)
        except OSError as exc:
            note["tracked"] = None
            note["tracked_error"] = f"{type(exc).__name__}: {exc}"
        return note
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _seen(path: Path) -> dict:
    stat = path.stat()
    return {
        "file": path.name,
        "taken": datetime.fromtimestamp(stat.st_mtime).isoformat(
            timespec="seconds"),
        "size_kb": round(stat.st_size / 1024),
    }


def shelf() -> dict:
    """What is on the shelf already, for the button to say so. Cheap: a listing
    and a stat, no zip opened. A button that says when the last copy was taken
    is the difference between pressing it and wondering."""
    zips = sorted(p for p in SHELF.glob(STAMP + "*.zip") if p != TRACKED) \
        if SHELF.is_dir() else []
    return {
        "count": len(zips),
        "where": str(SHELF),
        "last": _seen(zips[-1]) if zips else None,
        # Said separately, because it is the copy that leaves the disk, and a
        # tracked copy quietly older than the stack is the thing worth seeing.
        "tracked": _seen(TRACKED) if TRACKED.exists() else None,
    }


def check(path) -> dict:
    """Open a backup and say what is actually in it. The point of a backup is
    the day you need it, and that is a bad day to find out."""
    path = Path(path)
    work = Path(tempfile.mkdtemp(prefix=STAMP + "check-"))
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            inside = STORE_IN_ZIP if STORE_IN_ZIP in names else next(
                (n for n in names if re.fullmatch(r"data/[^/]+\.db", n)), None)
            if not inside:
                raise RuntimeError("there is no store in this file")
            z.extract(inside, work)
            note = (json.loads(z.read("what-is-in-here.json"))
                    if "what-is-in-here.json" in names else {})
        conn = sqlite3.connect(work / inside)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(conn)
        newest = conn.execute(
            "SELECT dt FROM rows ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return {"file": str(path), "integrity": ok, "counts": counts,
                "newest_row": newest[0] if newest else None,
                "files": len(names), "taken": note.get("taken"),
                "also": note.get("also")}
    finally:
        shutil.rmtree(work, ignore_errors=True)



# --- the nightly copies -------------------------------------------------------

# Not the zips above -- these live and die entirely on this machine, a copy
# at each edge of a night's work, in the same untracked `all_backups/`.
# `<slug>-YYYYMMDD-HHMMSS-before.db` / `-after.db`, so the
# moment and which side of the night it is are both readable at a glance,
# without opening anything.
NIGHT_RE = re.compile(r"^" + re.escape(STAMP) + r"\d{8}-\d{6}-(before|after)\.db$")

# A gigabyte is generous for a room this size and cheap to raise if it ever
# needs to be -- ours to own, not a knob that asks permission each time it's
# touched.
NIGHT_CAP_BYTES = 1_000_000_000


def _night_files(folder: Path) -> list:
    """Every night copy sitting in `folder`, oldest first. Only files this
    machinery itself wrote -- the tracked zip and anything else a person
    drops in the folder are never candidates."""
    return sorted((p for p in folder.glob(STAMP + "*.db") if NIGHT_RE.match(p.name)),
                  key=lambda p: p.stat().st_mtime)


def night_copy(side: str, dest=None, say=None) -> dict:
    """One copy of the store, taken at one edge of a night's work -- `before` it
    begins, `after` it ends, whether the night went well or broke. The same
    online backup `take` uses above, so a copy started while the room is
    mid-write is never torn -- just unzipped, named for the moment and the
    side, straight into `all_backups/`.

    Never raises. A night's copy failing to be taken must not be the reason
    the night itself fails to run or fails to close -- it says so loudly,
    through `say`, and the night goes on regardless."""
    assert side in ("before", "after")
    say = say or (lambda *a, **k: None)
    folder = Path(dest) if dest else DATA_BACKUPS
    folder.mkdir(parents=True, exist_ok=True)
    when = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"{STAMP}{when}-{side}.db"
    try:
        src = sqlite3.connect(db.DB_PATH)
        try:
            out = sqlite3.connect(target)
            try:
                with out:
                    src.backup(out)
            finally:
                out.close()
        finally:
            src.close()

        check = sqlite3.connect(target)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()

        if ok != "ok":
            say(f"night backup ({side}) came back damaged: {ok}", "snag")
        else:
            say(f"night backup ({side}): {target.name}, "
                f"{round(target.stat().st_size / 1024)} KB, checked", "plain")
        result = {"ok": ok == "ok", "file": str(target), "side": side,
                  "integrity": ok}
    except Exception as exc:
        say(f"night backup ({side}) failed: {type(exc).__name__}: {exc}",
            "snag")
        result = {"ok": False, "file": str(target), "side": side,
                  "error": f"{type(exc).__name__}: {exc}"}
    result["pruned"] = prune_night_backups(dest=folder, say=say)
    return result


def prune_night_backups(cap_bytes=NIGHT_CAP_BYTES, dest=None, say=None) -> dict:
    """Oldest first, until the night's copies are back under the cap. Never
    the newest one -- even a single copy over the cap is a fact to live
    with, not a reason to lose the only copy that exists. Never the latest
    `<slug>-backup.zip` either; that one is rewritten in place each press,
    not part of this rolling window.

    A line here every time anything is actually removed, so the pruning is
    legible after the fact without opening the folder by hand."""
    say = say or (lambda *a, **k: None)
    folder = Path(dest) if dest else DATA_BACKUPS
    files = _night_files(folder)
    removed, freed = [], 0
    while len(files) > 1:
        total = sum(p.stat().st_size for p in files)
        if total <= cap_bytes:
            break
        oldest = files.pop(0)
        size = oldest.stat().st_size
        oldest.unlink()
        removed.append(oldest.name)
        freed += size
    if removed:
        say(f"pruned {len(removed)} night backup(s) over the "
            f"{round(cap_bytes / (1024 ** 3), 2)} GB cap, freed "
            f"{round(freed / (1024 ** 2))} MB: {', '.join(removed)}",
            "plain")
    return {"removed": removed, "freed_bytes": freed}


def _cli() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--check":
        if len(args) < 2:
            print("usage: python -m server.backup --check <file.zip>")
            return
        got = check(args[1])
        print("  " + got["file"])
        print("  taken       :", got.get("taken") or "(not said)")
        print("  integrity   :", got["integrity"])
        print("  newest row  :", got["newest_row"])
        for t, n in got["counts"].items():
            print(f"    {t:9} {n}")
        print("  alongside   :", ", ".join(got.get("also") or []) or "nothing")
        return

    note = take(args[0] if args else None)
    print("  wrote  ", note["file"])
    print("  size   ", note["size_kb"], "KB")
    print("  checked", note["integrity"], "-",
          ", ".join(f"{t} {n}" for t, n in note["counts"].items()))
    print("  also   ", ", ".join(note["also"]))
    print("  latest ", note.get("tracked")
          or ("not rewritten -- " + note.get("tracked_error", "?")))


if __name__ == "__main__":
    _cli()
