"""Lean on the off-site backup without a network and without a model. Run over
a scratch folder:

    python -m server.test_offsite C:\\somewhere\\scratch

Nothing here reaches GitHub: every gh call is answered by a stub, so the
retention rule, the privacy gate and the read-back are exercised on the same
code paths the night uses without a byte leaving the machine. The store is
pointed into the scratch first, so the notice the failsafe raises lands in a
throwaway database and never in the live one.

What is checked, in the order it matters:

* a night running over a scratch store never reaches GitHub at all --
  this hook fires inside every test that runs a night, and without the
  guard it would put a throwaway store over the real one;
* the privacy gate refuses to upload when the repo is not private, and tells a
  person instead -- the one failure here that would publish everything;
* it also refuses when gh cannot answer at all, rather than assuming private;
* nothing in `send` raises into the night, whatever breaks underneath it;
* the retention rule keeps seven nights and twelve weeks and drops the rest,
  and never drops everything when there is little to keep;
* the read-back catches a short asset and a manifest that is not the one sent.
"""

import json
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

from . import db, offsite


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class Stub:
    """Stands in for gh. Records every call so a test can say what was asked
    as well as what came back -- "it did not upload" is only worth checking if
    an upload was possible in the first place."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, *args, timeout=None):
        self.calls.append(list(args))
        for key, val in self.answers.items():
            if list(args)[:len(key)] == list(key):
                return val
        return False, "", "stub has no answer for " + " ".join(args)

    def asked(self, *prefix) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


def _fake_days(days, end=None):
    end = end or date(2026, 9, 4)
    return [{"tag": offsite.TAG_PREFIX + (end - timedelta(days=i)).isoformat(),
             "date": end - timedelta(days=i)} for i in range(days)]


# --- the rule -----------------------------------------------------------------


def test_rule():
    keep, drop = offsite._keep(_fake_days(100))
    check("a hundred nights keep seven days and twelve weeks",
          len(keep) == offsite.DAILY_KEPT + offsite.WEEKLY_KEPT - 2,
          f"kept {len(keep)}")
    # The first two ISO weeks are already inside the seven dailies, so the
    # arithmetic above is 7 + 12 with the overlap removed. What matters is
    # simpler than the count: nothing recent is lost, and the oldest kept
    # copy is months back rather than days.
    kept = sorted(keep, reverse=True)
    check("the newest night always survives",
          kept[0] == offsite.TAG_PREFIX + "2026-09-04", kept[0])
    check("seven consecutive nights survive",
          kept[:7] == [offsite.TAG_PREFIX + (date(2026, 9, 4) - timedelta(days=i)).isoformat()
                       for i in range(7)], kept[:7])
    check("the oldest kept copy is more than two months back",
          (date(2026, 9, 4) - date.fromisoformat(kept[-1][len(offsite.TAG_PREFIX):])).days > 60,
          kept[-1])
    check("everything not kept is dropped and nothing is both",
          not (set(drop) & keep) and len(drop) == 100 - len(keep))

    keep, drop = offsite._keep(_fake_days(3))
    check("three nights are all kept and none dropped",
          len(keep) == 3 and drop == [], f"{len(keep)} kept, {len(drop)} dropped")

    keep, drop = offsite._keep([])
    check("an empty shelf drops nothing", keep == set() and drop == [])

    # Nights with gaps -- the room was shut for a fortnight -- must not make
    # the weekly rule reach back further than twelve real weeks of copies.
    sparse = [{"tag": offsite.TAG_PREFIX + d.isoformat(), "date": d}
              for d in [date(2026, 9, 4), date(2026, 8, 1), date(2025, 1, 5)]]
    keep, drop = offsite._keep(sparse)
    check("gaps in the nights keep every copy that is all there is",
          len(keep) == 3 and drop == [])


# --- the gate -----------------------------------------------------------------


def test_scratch_store(scratch: Path):
    """The regression that once cost a real backup. This hangs off the end of
    the night, so every test that runs a night runs it too -- and without
    this guard `test_backup` uploaded its scratch store straight over the
    only real copy of that date. A night over anything but the live store
    must not reach GitHub at all."""
    stub = Stub({("repo", "view"): (True, json.dumps(
        {"nameWithOwner": "x/y", "isPrivate": True, "visibility": "PRIVATE"}), "")})
    real_gh, real_path = offsite._gh, db.DB_PATH
    offsite._gh = stub
    db.DB_PATH = scratch / "somebody-elses.db"
    try:
        out = offsite.send(say=lambda t, k="plain": None)
    finally:
        offsite._gh, db.DB_PATH = real_gh, real_path
    check("a night over a scratch store does not upload",
          out["uploaded"] is False, out)
    check("it did not even ask GitHub about the repo",
          not stub.calls, stub.calls)
    check("and it says which store it was", "somebody-elses" in str(out.get("store")),
          out)


def test_gate_public():
    """The one that matters: a public repo must not receive the store."""
    told = []
    stub = Stub({("repo", "view"): (True, json.dumps(
        {"nameWithOwner": "x/y", "isPrivate": False, "visibility": "PUBLIC"}), "")})
    real_gh, real_tell = offsite._gh, offsite._tell_a_person
    offsite._gh = stub
    offsite._tell_a_person = lambda said, say: told.append(said) or {"ok": True}
    undo = _as_a_real_home()
    try:
        lines = []
        out = offsite.send(say=lambda t, k="plain": lines.append((k, t)))
    finally:
        offsite._gh, offsite._tell_a_person = real_gh, real_tell
        undo()
    check("a public repo is not uploaded to", out["uploaded"] is False, out)
    check("and it is not called ok", out["ok"] is False)
    check("no release was created or uploaded",
          not stub.asked("release", "create") and not stub.asked("release", "upload"),
          stub.calls)
    check("a person is told", len(told) == 1 and "not private" in told[0].lower(), told)
    check("and it says so loudly in the night",
          any(k == "snag" for k, _ in lines), lines)


def test_gate_unreadable():
    """gh silent, offline, logged out: unknown is not private."""
    stub = Stub({("repo", "view"): (False, "", "gh: could not connect")})
    real = offsite._gh
    offsite._gh = stub
    undo = _as_a_real_home()
    try:
        out = offsite.send(say=lambda t, k="plain": None)
    finally:
        offsite._gh = real
        undo()
    check("an unreadable repo is not uploaded to", out["uploaded"] is False, out)
    check("nothing was created", not stub.asked("release", "create"), stub.calls)


def _as_a_real_home():
    """The night's store only goes out from a real home. The bench runs from
    the code folder, so it stands in for one: the live path is the store
    in use, and the home says it is real, for the length of one test."""
    real = (offsite.home.is_real, offsite.LIVE_DB)
    offsite.home.is_real = lambda: True
    offsite.LIVE_DB = Path(db.DB_PATH)
    return lambda: (setattr(offsite.home, "is_real", real[0]),
                    setattr(offsite, "LIVE_DB", real[1]))


def test_never_raises():
    """Whatever breaks underneath, the night still closes."""
    undo = _as_a_real_home()
    real_gh, real_take = offsite._gh, offsite.backup.take
    offsite._gh = Stub({("repo", "view"): (True, json.dumps(
        {"nameWithOwner": "x/y", "isPrivate": True, "visibility": "PRIVATE"}), "")})

    def explode():
        raise RuntimeError("the disk went away mid-copy")

    offsite.backup.take = explode
    try:
        out = offsite.send(say=lambda t, k="plain": None)
    except Exception as exc:
        offsite._gh, offsite.backup.take = real_gh, real_take
        check("send never raises", False, f"{type(exc).__name__}: {exc}")
        return
    offsite._gh, offsite.backup.take = real_gh, real_take
    undo()
    check("send never raises", True)
    check("a broken copy is reported, not swallowed",
          out["ok"] is False and "disk went away" in str(out.get("why")), out)


# --- the read-back ------------------------------------------------------------


def test_read_back(scratch: Path):
    note = {"taken": "2026-09-04T12:00:00", "counts": {"rows": 10},
            "also": ["pictures/"]}
    good = scratch / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("what-is-in-here.json", json.dumps(note))

    def stub_download(landed):
        def _gh(*args, timeout=None):
            if list(args)[:2] == ["release", "download"]:
                dest = Path(args[args.index("--dir") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / offsite.ASSET_NAME).write_bytes(landed.read_bytes())
                return True, "", ""
            return True, "", ""
        return _gh

    real = offsite._gh
    try:
        offsite._gh = stub_download(good)
        check("a copy that matches reads back",
              offsite._verify("t", good, note, lambda *a, **k: None)["ok"] is True)
        check("and it sees the pictures rode along",
              offsite._verify("t", good, note, lambda *a, **k: None)["pictures"] is True)

        short = scratch / "short.zip"
        short.write_bytes(good.read_bytes()[:-20])
        offsite._gh = stub_download(short)
        out = offsite._verify("t", good, note, lambda *a, **k: None)
        check("a truncated copy is refused", out["ok"] is False, out)

        other = scratch / "other.zip"
        wrong = dict(note, taken="2026-01-01T00:00:00")
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("what-is-in-here.json", json.dumps(wrong))
        # Same length on the wire, different backup inside.
        offsite._gh = stub_download(other)
        out = offsite._verify("t", other, note, lambda *a, **k: None)
        check("a copy of a different night is refused", out["ok"] is False, out)
    finally:
        offsite._gh = real


# --- the notice ---------------------------------------------------------------


def test_notice(scratch: Path):
    """The failsafe's only action, against a real store -- a throwaway one."""
    from . import projects
    real_path = db.DB_PATH
    db.DB_PATH = scratch / "store.db"
    try:
        conn = db.connect()
        projects.add_project(conn, offsite.NOTICE_PROJECT, "sam")
        conn.close()
        said = "x/y is PUBLIC, not private."
        out = offsite._tell_a_person(said, lambda *a, **k: None)
        check("the failsafe lands a notice", out.get("ok") is True, out)

        conn = db.connect()
        p = projects.by_title(conn, offsite.NOTICE_PROJECT)
        t = projects.task_by_title(conn, p["id"], offsite.NOTICE_TASK)
        check("on a live task somebody can check off",
              t is not None and t["state"] in projects.LIVE_STATES, t)
        again = offsite._tell_a_person(said, lambda *a, **k: None)
        check("a second night reuses the task rather than making another",
              again.get("ok") is True
              and again.get("task", {}).get("id") == t["id"], again)
        conn.close()
    finally:
        db.DB_PATH = real_path


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2].strip())
        raise SystemExit(2)
    scratch = Path(sys.argv[1]).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    if scratch == Path(__file__).resolve().parent.parent / "data":
        raise SystemExit("not over the live data folder.")

    print("the retention rule")
    test_rule()
    print("whose store it is")
    test_scratch_store(scratch)
    print("the privacy gate")
    test_gate_public()
    test_gate_unreadable()
    test_never_raises()
    print("the read-back")
    test_read_back(scratch)
    print("the notice")
    test_notice(scratch)

    print()
    if FAILED:
        print(f"{len(FAILED)} failed: " + ", ".join(FAILED))
        raise SystemExit(1)
    print("all good.")


if __name__ == "__main__":
    main()
