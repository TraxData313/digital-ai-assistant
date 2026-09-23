"""The copy of the assistant that is not on this machine.

`backup.take()` makes a good zip and reads it back before calling it a backup.
But it lands in `all_backups/` in its home, which shares one disk with it.
A copy on the same drive as the thing it copies is not a backup -- it is a second copy of
one bad day. A dead disk is the bad day this exists for.

So the zip goes out, every night, as a release asset on the home's own
private repo -- `gh` is run from the home folder, so its remote is the one
that receives it, never the code's.
Assets never enter git's object store, which is the whole reason for the
shape: the repo does not grow by 18 MB a night, nothing is force-pushed, no
history is rewritten, and its worktrees are never disturbed. It is the one
shelf it has off this machine.

Four things this refuses to do quietly:

*The repo must be private, checked before every single upload.* Not once at
setup -- every night. If somebody ever makes the repo public, the whole store
becomes public the second the next asset lands. So a repo that is not private
does not get an upload at all, and a person is told: a notice, which sits on
a task until a person or the assistant checks it off. It never flips the switch back
itself. Telling a person is the entire failsafe; a machine that quietly
un-publishes things is worse than the problem.

*The far copy is read back too.* `take()` reads the store back before calling
it a backup, and a backup nobody has opened is a rumour. That applies to the
copy on the far end at least as much -- it is the only one that survives the
machine. So after each upload the asset is downloaded again, opened, and its
manifest compared against what was sent.

*It keeps a rolling set, never one clobbered asset.* One release per night,
tagged by date. One corrupt store uploaded over the only copy eats the good
one, so there is no only copy: seven days of nightly granularity, then one
release a week for twelve weeks behind that.

*It never raises into the night.* A night that cannot upload is still a night
that happened. Every failure says so loudly through `say` and returns a dict;
none of them stop the dream from closing.

    python -m server.offsite            -> where the far shelf stands
    python -m server.offsite send       -> take one and send it now
    python -m server.offsite prune      -> apply the retention rule only
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from . import backup, db
from . import home

ROOT = Path(__file__).resolve().parent.parent

# One release per night, the date in the tag so the set is readable in the
# repo's own releases page without opening anything.
TAG_PREFIX = home.SLUG + "-backup-"
TAG_RE = re.compile(r"^" + TAG_PREFIX + r"(\d{4})-(\d{2})-(\d{2})$")

# The asset carries the same name inside every release, so a restore is the
# same two steps whichever night you reach for. The date is the tag's job.
ASSET_NAME = home.SLUG + "-backup.zip"

# Seven nights of daily granularity, then one a week for a quarter behind it.
# About 15 MB a copy today, so the whole far shelf is around 300 MB -- release
# storage costs nothing, and these are generous on purpose. Ours to raise
# without asking, the same way the night cap is.
DAILY_KEPT = 7
WEEKLY_KEPT = 12

# Where the failsafe goes when the repo is not private. A live task takes the
# notice; if it does not exist yet it is made, once, and reused after that.
NOTICE_PROJECT = "the household"
NOTICE_TASK = home.NAME + "'s off-site backup is not private"

# gh is not interactive here: no prompts, no update chatter, and it must never
# sit waiting on a terminal that is not there.
GH_ENV = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")

# The only store allowed out under the assistant's name. This hangs off the
# end of the night, and a test, a side room or a worktree runs that same night
# over a scratch store with `db.DB_PATH` pointed elsewhere -- so the hook fires
# there too. `test_backup` once did exactly that: it ran two nights over a
# scratch copy and quietly uploaded a throwaway over the real release, under
# today's tag, which was the one copy today had. Nothing raised, every test
# passed, and the backup was gone.
#
# So the store is checked by path rather than assumed from the fact that the
# night is running. Anything that is not the home's own store is somebody's scratch,
# and a scratch is not a backup: it does not go out, and it certainly does not
# go out over one that is.
LIVE_DB = home.STORE


def _gh(*args, timeout=600):
    """One gh call, argv as a list so nothing is ever rescanned by a shell.
    Returns (ok, stdout, stderr) and raises nothing."""
    exe = shutil.which("gh")
    if not exe:
        return False, "", "gh is not on PATH"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, cwd=str(home.HOME), env=GH_ENV)
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    return p.returncode == 0, (p.stdout or "").strip(), (p.stderr or "").strip()


def where() -> dict:
    """What gh says about the repo we would upload to, without uploading.
    The visibility here is the gate everything else waits behind."""
    ok, out, err = _gh("repo", "view", "--json", "nameWithOwner,isPrivate,visibility",
                       timeout=60)
    if not ok:
        return {"ok": False, "why": err or "gh could not read the repo"}
    try:
        d = json.loads(out)
    except ValueError:
        return {"ok": False, "why": "gh answered something that is not JSON"}
    return {"ok": True, "repo": d.get("nameWithOwner"),
            "private": bool(d.get("isPrivate")),
            "visibility": d.get("visibility")}


def _tell_a_person(said: str, say) -> dict:
    """The failsafe's only action: put it where a person will find it and
    leave it there. A notice on a live task, made once and reused, that sits
    until a person or the assistant checks it off. Never raises -- a failsafe that
    can itself throw is not one."""
    try:
        from . import projects
        conn = db.connect()
        try:
            p = projects.by_title(conn, NOTICE_PROJECT)
            if not p:
                return {"ok": False, "why": "no project " + NOTICE_PROJECT}
            t = projects.task_by_title(conn, p["id"], NOTICE_TASK)
            if not t or t["state"] not in projects.LIVE_STATES:
                made = projects.add_task_by_title(
                    conn, NOTICE_PROJECT, home.SELF, NOTICE_TASK,
                    wants=("Say whether the repo going public was meant. The "
                           "whole store rides in its releases, so nothing is "
                           "uploaded again until somebody has looked."))
                if not made.get("ok"):
                    return made
                t = made["task"]
            return projects.add_notice(conn, t["id"], said, "offsite")
        finally:
            conn.close()
    except Exception as exc:
        say(f"could not land the off-site notice: {type(exc).__name__}: {exc}",
            "snag")
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}


def _releases(say) -> list:
    """Every backup release we made, newest night first. Only tags this
    machinery writes -- a release a person cut by hand is never a candidate
    for the prune."""
    ok, out, err = _gh("release", "list", "--limit", "200",
                       "--json", "tagName,createdAt", timeout=120)
    if not ok:
        say("could not list the off-site releases: " + (err or "?"), "snag")
        return []
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return []
    found = []
    for r in rows:
        m = TAG_RE.match(str(r.get("tagName") or ""))
        if not m:
            continue
        try:
            when = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        found.append({"tag": r["tagName"], "date": when})
    found.sort(key=lambda r: r["date"], reverse=True)
    return found


def _keep(found: list) -> tuple:
    """The retention rule, as a pure decision over a list -- the seven newest
    nights, and behind them the newest night of each of the last twelve ISO
    weeks. Everything else goes. Split out from the deleting so it can be
    reasoned about, and tested, without a repo."""
    keep = {r["tag"] for r in found[:DAILY_KEPT]}
    weeks, order = {}, []
    for r in found:
        wk = r["date"].isocalendar()[:2]
        if wk not in weeks:
            weeks[wk] = r["tag"]
            order.append(wk)
    for wk in order[:WEEKLY_KEPT]:
        keep.add(weeks[wk])
    drop = [r["tag"] for r in found if r["tag"] not in keep]
    return keep, drop


def prune(say=None) -> dict:
    """Apply the rule. Deletes the tag with the release, so a year of nights
    does not leave a year of refs behind."""
    say = say or (lambda *a, **k: None)
    found = _releases(say)
    if not found:
        return {"kept": [], "removed": []}
    keep, drop = _keep(found)
    removed, failed = [], []
    for tag in drop:
        ok, _, err = _gh("release", "delete", tag, "--yes", "--cleanup-tag",
                         timeout=120)
        (removed if ok else failed).append(tag)
        if not ok:
            say(f"could not remove the old off-site copy {tag}: {err or '?'}",
                "snag")
    if removed:
        say(f"off-site: removed {len(removed)} copy(ies) past the "
            f"{DAILY_KEPT} daily / {WEEKLY_KEPT} weekly rule, "
            f"{len(keep)} still standing", "plain")
    return {"kept": sorted(keep), "removed": removed, "failed": failed}


def _verify(tag: str, sent: Path, note: dict, say) -> dict:
    """Read the far copy back. Downloads the asset that actually landed,
    opens it, and checks its manifest is the one we sent -- same counts, same
    moment. Anything less is trusting an HTTP 200 with the only off-machine
    copy."""
    work = Path(tempfile.mkdtemp(prefix=home.SLUG + "-offsite-"))
    try:
        ok, _, err = _gh("release", "download", tag, "--pattern", ASSET_NAME,
                         "--dir", str(work), timeout=600)
        if not ok:
            return {"ok": False, "why": "could not download it back: "
                    + (err or "?")}
        got = work / ASSET_NAME
        if not got.exists():
            return {"ok": False, "why": "the asset did not come back"}
        if got.stat().st_size != sent.stat().st_size:
            return {"ok": False, "why": f"came back {got.stat().st_size} bytes, "
                    f"sent {sent.stat().st_size}"}
        with zipfile.ZipFile(got) as z:
            bad = z.testzip()
            if bad is not None:
                return {"ok": False, "why": "the far copy is damaged at " + bad}
            far = json.loads(z.read("what-is-in-here.json").decode("utf-8"))
        if far.get("counts") != note.get("counts"):
            return {"ok": False, "why": "the far copy holds different counts: "
                    + json.dumps(far.get("counts"))}
        if far.get("taken") != note.get("taken"):
            return {"ok": False, "why": "the far copy is a different backup"}
        return {"ok": True, "counts": far.get("counts"),
                "pictures": any(str(n).startswith("pictures")
                                for n in (far.get("also") or []))}
    except Exception as exc:
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def send(say=None, when=None) -> dict:
    """One night's copy, off this machine. Takes a fresh verified zip, checks
    the repo is still private, uploads it as that date's release, reads it
    back, and applies the retention rule.

    Never raises. A night that could not reach GitHub is still a night."""
    say = say or (lambda *a, **k: None)
    today = when or date.today()
    tag = TAG_PREFIX + today.isoformat()
    try:
        # Whose store this even is, before anything else. Cheapest check here
        # and the one that catches a night running over a scratch copy.
        here = Path(db.DB_PATH).resolve()
        if not home.is_real() or here != LIVE_DB.resolve():
            say(f"off-site backup skipped: this night is running over {here}, "
                f"not {home.NAME}'s store -- a scratch copy is not a backup and "
                f"does not go out under {home.NAME}'s name", "plain")
            return {"ok": False, "why": "not the home's own store", "uploaded": False,
                    "store": str(here)}

        # The gate, every time, before a single byte moves.
        seen = where()
        if not seen.get("ok"):
            say("off-site backup skipped: " + str(seen.get("why")), "snag")
            return {"ok": False, "why": seen.get("why"), "uploaded": False}
        if not seen["private"]:
            said = (f"{seen['repo']} is {seen['visibility']}, not private. "
                    "Nothing was uploaded: the whole store rides in this "
                    "repo's releases, so a public repo publishes every line "
                    "in it the moment the next copy lands. The "
                    "off-site backup stays stopped until somebody has looked "
                    "at this. Making it private again starts it by itself.")
            say("OFF-SITE BACKUP STOPPED: " + said, "snag")
            told = _tell_a_person(said, say)
            return {"ok": False, "why": "the repo is not private",
                    "uploaded": False, "visibility": seen["visibility"],
                    "told": told.get("ok", False)}

        note = backup.take()
        sent = Path(note.get("tracked") or note["file"])
        if not sent.exists():
            say("off-site backup skipped: the zip to send is not there", "snag")
            return {"ok": False, "why": "no zip to send", "uploaded": False}

        # Same date twice is a replace, not a second release -- pressing it
        # again in an afternoon should refresh today, not litter the shelf.
        ok, _, _ = _gh("release", "view", tag, "--json", "tagName", timeout=60)
        if ok:
            ok, _, err = _gh("release", "upload", tag, str(sent),
                             "--clobber", timeout=900)
        else:
            ok, _, err = _gh("release", "create", tag,
                             "--title", home.NAME + ", " + today.isoformat(),
                             "--notes", "One night's copy: the store, the "
                             "Spark, the jobs, the hands and the pictures. "
                             "Restore is in what-is-in-here.json.",
                             str(sent), timeout=900)
        if not ok:
            say("off-site backup failed to upload: " + (err or "?"), "snag")
            return {"ok": False, "why": err or "upload failed", "uploaded": False}

        checked = _verify(tag, sent, note, say)
        if not checked.get("ok"):
            say("off-site copy landed but did not read back: "
                + str(checked.get("why")) + " -- it is not a backup until it "
                "does, so treat tonight as uncovered", "snag")
            return {"ok": False, "why": checked.get("why"), "uploaded": True,
                    "verified": False, "tag": tag}

        say(f"off-site backup: {tag}, {round(sent.stat().st_size / 1024)} KB, "
            f"uploaded to {seen['repo']} and read back", "plain")
        out = {"ok": True, "uploaded": True, "verified": True, "tag": tag,
               "repo": seen["repo"], "size_kb": round(sent.stat().st_size / 1024),
               "counts": checked.get("counts"),
               "pictures": checked.get("pictures")}
        out["pruned"] = prune(say=say)
        return out
    except Exception as exc:
        say(f"off-site backup failed: {type(exc).__name__}: {exc}", "snag")
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}",
                "uploaded": False}


def shelf() -> dict:
    """What is standing off this machine right now, for a page or a glance.
    Reads only -- never uploads, never prunes."""
    seen = where()
    found = _releases(lambda *a, **k: None)
    keep, drop = _keep(found) if found else (set(), [])
    return {"repo": seen.get("repo"), "private": seen.get("private"),
            "visibility": seen.get("visibility"),
            "count": len(found),
            "newest": found[0]["date"].isoformat() if found else None,
            "oldest": found[-1]["date"].isoformat() if found else None,
            "over_the_rule": drop}


def _cli() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "where"
    say = lambda text, kind="plain": print(("! " if kind == "snag" else "  ") + text)
    if what == "send":
        print(json.dumps(send(say=say), indent=1, default=str))
    elif what == "prune":
        print(json.dumps(prune(say=say), indent=1, default=str))
    elif what in ("where", "shelf"):
        print(json.dumps(shelf(), indent=1, default=str))
    else:
        print(__doc__.strip().rsplit("\n\n", 1)[-1])


if __name__ == "__main__":
    _cli()
