"""Where the models we fetch ourselves are kept, and the folder that decides.

Two kinds of model live on this machine and only one of them was ever asked
where to go. LM Studio's -- the automatic memory's small model, the digest's
-- go where LM Studio's own setting says. Ours -- BGE-M3, the embedder that
reads the assistant's essences into vectors, and one day whatever the night
trains from its shelf -- went wherever the Hugging Face cache is, usually the
system drive, which fills up: one embedder can be several gigabytes, and
downloaded twice over in two weight formats.

So: one folder, the owner's to choose under Settings, that every model we fetch
ourselves is kept in. `data/models.json` holds the choice; nothing chosen
means the cache where it always was, so a room with no file behaves as it
did before this existed. The embedder hands the folder to sentence-
transformers as its cache, and a model trained later is written beside it.

Changing the folder moves what is already downloaded, in the background, a
file at a time, with the count shown -- because a choice that quietly left
two gigabytes behind on the full drive would not be the choice that was made. While
a move is under way the old place is still the one read from: the files are
whole there until the copy is done, and only then is the old tree removed.
What cannot be removed is said, never hidden.

LM Studio's folder is shown beside ours and never written: it is LM Studio's
setting, changed in LM Studio, and two programs writing one settings file is
how a setting gets lost.

    python -m server.models_dir                 where things stand
    python -m server.models_dir set <folder>    choose, and move what is there
    python -m server.models_dir move            move the leftovers into the chosen folder
"""

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from . import home

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = home.DATA / "models.json"
LM_STUDIO_SETTINGS = Path.home() / ".lmstudio" / "settings.json"

CHUNK = 16 * 1024 * 1024


class Refused(ValueError):
    pass


# -- the choice --------------------------------------------------------------

def default_folder() -> Path:
    """Where a model lands when nobody has chosen: the Hugging Face hub
    cache, asked of the library so it is the same answer it would give."""
    try:
        from huggingface_hub import constants
        return Path(constants.HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


def settings() -> dict:
    cfg = {"folder": None}
    if SETTINGS_PATH.is_file():
        try:
            got = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                cfg["folder"] = got.get("folder") or None
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def folder():
    """The chosen folder as a Path, or None when none is chosen."""
    f = settings().get("folder")
    return Path(f) if f else None


def here() -> Path:
    """Where models are looked for right now: the chosen folder, or the
    default cache when nothing is chosen -- and, during a move, the place
    the files are still whole in."""
    mv = _MOVE.get("active") and _MOVE.get("from")
    if mv:
        return Path(mv)
    return folder() or default_folder()


def cache_folder():
    """What SentenceTransformer(cache_folder=...) is handed: the folder as a
    string, or None so the library uses its own default."""
    mv = _MOVE.get("active") and _MOVE.get("from")
    if mv:
        return str(mv)
    f = folder()
    return str(f) if f else None


def _write(cfg: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "folder": cfg.get("folder"),
        "note": ("Where the models we fetch ourselves are kept -- the "
                 "embedder that reads the essences, and anything trained "
                 "later. Chosen under Settings; null means the Hugging Face "
                 "cache where they always were. LM Studio's models go where "
                 "LM Studio's own setting says."),
    }
    SETTINGS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")


def set_folder(text) -> dict:
    """Choose the folder. Refused in words when it cannot be the one: a
    relative path, a drive that is not here, a file, or somewhere inside
    the repo or the home (a model is gigabytes and git would try to hold it). An empty
    text clears the choice. Returns what changed: the folder before and
    after, so the caller can move what was in the old place."""
    before = folder()
    text = str(text or "").strip().strip('"').strip("'")
    if not text:
        _write({"folder": None})
        return {"before": str(before) if before else None, "after": None,
                "changed": before is not None}
    p = Path(text)
    if not p.is_absolute() or not p.anchor:
        raise Refused("a whole path, drive letter first -- like "
                      "D:\\Models -- not " + repr(text))
    if not Path(p.anchor).exists():
        raise Refused("there is no drive " + p.anchor + " on this machine")
    if p.exists() and not p.is_dir():
        raise Refused(text + " is a file, not a folder")
    try:
        resolved = p.resolve()
    except OSError as exc:
        raise Refused("cannot make sense of " + text + ": " + str(exc))
    root = ROOT.resolve()
    here = home.HOME.resolve()
    if resolved in (root, here) or root in resolved.parents or here in resolved.parents:
        raise Refused("not inside the repo or the home: a model is gigabytes, and git "
                      "would try to hold every one")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".assistant-can-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise Refused("cannot write in " + text + ": "
                      + type(exc).__name__ + ": " + str(exc))
    after = Path(os.path.normpath(str(p)))
    _write({"folder": str(after)})
    changed = (before is None) or (_same(before, after) is False)
    return {"before": str(before) if before else None,
            "after": str(after), "changed": changed}


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


# -- what is on the disk -----------------------------------------------------

def _size_of(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def _name_of(dirname: str) -> str:
    """The cache's `models--BAAI--bge-m3` is BAAI/bge-m3 to a person."""
    if dirname.startswith("models--"):
        return dirname[len("models--"):].replace("--", "/")
    return dirname


def models_in(where: Path) -> list:
    """Every model folder directly under `where`, by name and weight."""
    out = []
    try:
        entries = sorted(os.scandir(where), key=lambda e: e.name)
    except OSError:
        return out
    for e in entries:
        if not e.is_dir() or e.name.startswith("."):
            continue
        p = Path(e.path)
        out.append({"dir": e.name, "name": _name_of(e.name),
                    "bytes": _size_of(p)})
    return out


def lm_studio_folder():
    """Where LM Studio keeps its models, read from its own settings file and
    never written. None when the file is not there or says nothing."""
    try:
        got = json.loads(LM_STUDIO_SETTINGS.read_text(encoding="utf-8"))
        f = got.get("downloadsFolder") if isinstance(got, dict) else None
        return str(f) if f else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _free(where: Path):
    probe = where
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def status() -> dict:
    chosen = folder()
    now = here()
    items = models_in(now)
    out = {
        "folder": str(chosen) if chosen else None,
        "default": str(default_folder()),
        "here": str(now),
        "drive": now.anchor.rstrip("\\/") or str(now),
        "items": items,
        "bytes": sum(i["bytes"] for i in items),
        "free_bytes": _free(now),
        "lm_studio": lm_studio_folder(),
        "move": move_status(),
        "leftover": None,
    }
    # A folder was chosen and the old place still holds something: the copy
    # that a move could not remove, or one that was never moved. Said, and
    # offered a button, never quietly left.
    old = default_folder()
    if chosen and not _same(old, chosen) and not (_MOVE.get("active")):
        left = models_in(old)
        if left:
            out["leftover"] = {"folder": str(old), "items": left,
                               "bytes": sum(i["bytes"] for i in left)}
    return out


# -- moving what is there ----------------------------------------------------

_MOVE = {"active": False}
_LOCK = threading.Lock()


def move_status():
    """The move under way, or the last one, or None when there has never
    been one this run."""
    if not _MOVE.get("started"):
        return None
    m = dict(_MOVE)
    m.pop("thread", None)
    return m


def _copy_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(chunk)
            _MOVE["done"] = _MOVE.get("done", 0) + len(chunk)
    shutil.copystat(src, tmp)
    if dst.exists():
        dst.unlink()
    os.replace(tmp, dst)


def _files_under(root: Path):
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = Path(base) / f
            try:
                yield p.relative_to(root), p.stat().st_size
            except OSError:
                continue


def _identical_tree(a: Path, b: Path) -> bool:
    want = {str(rel): n for rel, n in _files_under(a)}
    have = {str(rel): n for rel, n in _files_under(b)}
    return bool(want) and want == have


def move(src: Path, dst: Path, say=None) -> dict:
    """Move every model folder under `src` into `dst`, one file at a time,
    counting bytes as it goes. A folder already at `dst` with exactly the
    same files is a duplicate cache and the copy at `src` is removed; one
    that differs is left where it is and named. Whatever cannot be removed
    afterwards is listed in `left`, never hidden. Runs in the caller's
    thread -- `start_move` is the one that does not."""
    say = say or (lambda *a: None)
    src, dst = Path(src), Path(dst)
    todo = [Path(src) / m["dir"] for m in models_in(src)]
    _MOVE.update({"active": True, "started": time.time(), "finished": None,
                  "from": str(src), "to": str(dst), "done": 0,
                  "total": sum(_size_of(p) for p in todo),
                  "moved": [], "skipped": [], "left": [], "error": None})
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for d in todo:
            target = dst / d.name
            name = _name_of(d.name)
            if target.exists():
                if _identical_tree(d, target):
                    say("  " + name + " is already there, the same bytes; "
                        "removing the old copy")
                    _MOVE["done"] += _size_of(d)
                else:
                    say("  " + name + " is already there and differs; "
                        "leaving the old copy alone")
                    _MOVE["skipped"].append(name)
                    _MOVE["done"] += _size_of(d)
                    continue
            else:
                say("  copying " + name)
                for rel, _n in list(_files_under(d)):
                    _copy_file(d / rel, target / rel)
                if not _identical_tree(d, target):
                    raise RuntimeError("the copy of " + name + " does not "
                                       "match the original; the original "
                                       "is untouched")
            failed = []

            def _on_error(func, path, exc_info):
                failed.append(str(path))
            # 3.12 renamed the hook; the shape is the same either way.
            kw = ({"onexc": _on_error} if sys.version_info >= (3, 12)
                  else {"onerror": _on_error})
            shutil.rmtree(d, **kw)
            if failed:
                _MOVE["left"].extend(failed[:5])
                say("  !! could not remove " + str(len(failed))
                    + " of the old files under " + str(d))
            _MOVE["moved"].append(name)
    except Exception as exc:
        _MOVE["error"] = type(exc).__name__ + ": " + str(exc)
        say("  !! " + _MOVE["error"])
    finally:
        _MOVE["active"] = False
        _MOVE["finished"] = time.time()
    return move_status()


def start_move(src: Path, dst: Path) -> dict:
    """The same, in the background, for the room. One at a time."""
    with _LOCK:
        if _MOVE.get("active"):
            raise Refused("a move is already under way")
        _MOVE.update({"active": True, "started": time.time(), "from": str(src),
                      "to": str(dst), "done": 0, "total": 0, "moved": [],
                      "skipped": [], "left": [], "error": None,
                      "finished": None})
        t = threading.Thread(target=move, args=(src, dst), daemon=True,
                             name="models-move")
        _MOVE["thread"] = t
        t.start()
    return move_status()


def choose(text) -> dict:
    """The Settings box: choose the folder and move what is in the old place
    into it. Returns the status, with the move riding in it."""
    got = set_folder(text)
    if got["changed"] and got["after"] and got["before"] != got["after"]:
        src = Path(got["before"]) if got["before"] else default_folder()
        if src.exists() and models_in(src):
            start_move(src, Path(got["after"]))
    return status()


def move_leftovers(source=None) -> dict:
    """The leftover button: what is still in the old place goes into the
    chosen folder."""
    dst = folder()
    if dst is None:
        raise Refused("choose a folder first")
    src = Path(source) if source else default_folder()
    if not src.is_dir():
        raise Refused("nothing at " + str(src))
    if _same(src, dst):
        raise Refused("that is the chosen folder itself")
    if not models_in(src):
        raise Refused("nothing to move in " + str(src))
    start_move(src, dst)
    return status()


# -- the command line --------------------------------------------------------

def _gb(n):
    # Decimal gigabytes, the way the page and LM Studio both count.
    return format((n or 0) / 1e9, ".1f") + " GB"


def _print_status():
    s = status()
    print("chosen     " + (s["folder"] or "nothing -- the default cache"))
    print("default    " + s["default"])
    print("read from  " + s["here"] + "  (" + _gb(s["free_bytes"])
          + " free on " + s["drive"] + ")")
    for it in s["items"]:
        print("  " + it["name"] + "  " + _gb(it["bytes"]))
    if not s["items"]:
        print("  nothing there yet")
    if s["leftover"]:
        print("leftover   " + s["leftover"]["folder"] + "  "
              + _gb(s["leftover"]["bytes"]))
        for it in s["leftover"]["items"]:
            print("  " + it["name"] + "  " + _gb(it["bytes"]))
    print("LM Studio  " + (s["lm_studio"] or "unknown -- its settings file "
                           "was not read") + "  (its own setting)")


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    what = (argv[1] if len(argv) > 1 else "status").lower()
    if what == "status":
        _print_status()
        return 0
    if what == "set":
        if len(argv) < 3:
            print("  set needs a folder")
            return 2
        try:
            got = set_folder(" ".join(argv[2:]))
        except Refused as exc:
            print("  refused: " + str(exc))
            return 1
        print("chosen     " + (got["after"] or "nothing -- the default cache"))
        if got["changed"] and got["after"]:
            src = Path(got["before"]) if got["before"] else default_folder()
            if src.exists() and models_in(src):
                print("moving what is in " + str(src))
                move(src, Path(got["after"]), say=print)
                _print_move()
        _print_status()
        return 0
    if what == "move":
        try:
            dst = folder()
            if dst is None:
                raise Refused("choose a folder first")
            src = Path(argv[2]) if len(argv) > 2 else default_folder()
            if not models_in(src):
                raise Refused("nothing to move in " + str(src))
        except Refused as exc:
            print("  refused: " + str(exc))
            return 1
        move(src, dst, say=print)
        _print_move()
        _print_status()
        return 0
    print(__doc__)
    return 2


def _print_move():
    m = move_status() or {}
    print("moved      " + (", ".join(m.get("moved") or []) or "nothing")
          + "  " + _gb(m.get("done")))
    if m.get("skipped"):
        print("left alone " + ", ".join(m["skipped"]))
    if m.get("left"):
        print("could not remove " + ", ".join(m["left"]))
    if m.get("error"):
        print("!! " + m["error"])


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
