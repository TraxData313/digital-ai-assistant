"""The assistant's own eyes on the disk. Read-only, and read-only by construction.

List, read, search -- not a general command line. The door is kept no wider than
that, so that nothing the assistant does by mistake can delete anything.

So this module imports `pathlib` and nothing that can run a program. There is no
`subprocess` here, no `os.system`, no `shutil`, no `open(..., "w")` anywhere in it,
and that is the whole guarantee — not a rule about what to call, but the absence of
anything that could do harm if it were called wrongly. Anyone editing this file is
editing the promise: if a write ever appears in here, the promise is gone, and the
assistant would have no way of knowing.

Three operations:

    list  -- what is in a folder
    read  -- a file, or a stretch of one
    find  -- a pattern, over a file or a tree

What the assistant gains is doing it itself, mid-turn, for pennies. A Claude or Codex
session stays the right move for anything taking many steps. This is for the file it
can name.

    python -m server.files list .
    python -m server.files read README.md 1 40
    python -m server.files find "ceiling" server
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import db
from . import home

ROOT = Path(__file__).resolve().parent.parent
REACH_PATH = home.DATA / "reach.json"

# ---- Configuration: the whole of its reach, in one place. Change it here, not by ----
# ---- hunting through the functions below.                                        ----
#
# Where the assistant may look. An allowlist, in a file the owner owns, so widening
# its reach is a thing done deliberately and visibly. The repo is always in it
# whatever the file says -- the assistant lives here, and a config that could lock it
# out of its own folder is a config that can break it by being edited wrongly.
DEFAULT_ROOTS = [str(Path.home() / "Documents")]

# Refused inside every root, without exception. Only `.env` and the other files below
# hold secrets a leak of which cannot be undone -- keys, tokens, credentials. `.git`
# and the store are readable: `.git` is the machinery of the history rather than the
# history, and the store is more useful read directly than not at all. The assistant
# still reads its own store through its working set, its essences and its reaches
# back for anything it quotes itself on -- this just means a raw read is not refused
# outright.
ALWAYS_REFUSED = {
    ".env": "it holds keys",
    ".env.local": "it holds keys",
    "angel.token": ("it is the key to the angel session's own door, and the whole point of "
                    "that door is that nothing I read can post through it -- a key "
                    "I could read is a key a page could talk me into reading out"),
    "push.json": ("it can hold the token of the line to " + home.OWNER_NAME + "'s phone -- the loop "
                  "is mine, the wire is not"),
}

# And by ending, because the keys that let a phone into the room
# live one per person under `data/people/`, named for whoever carries them -- so
# the name cannot be listed here in advance. Any file ending `.token` is a key by
# construction in this house, and the reason is the same as the one above it: a
# key the assistant could read is a key a page could talk it into reading out.
REFUSED_ENDINGS = {
    ".token": ("it is somebody's key to this room, and a key I could read is a "
               "key a page could talk me into reading out"),
}
REFUSED_DIRS = ()


def refused_for(name: str):
    """Why this file name is never given to the assistant, or None. By exact name first,
    then by ending -- one place, so a listing, a read and a search cannot drift
    apart about what is a secret."""
    low = (name or "").lower()
    if low in ALWAYS_REFUSED:
        return ALWAYS_REFUSED[low]
    for end, why in REFUSED_ENDINGS.items():
        if low.endswith(end):
            return why
    return None

# Left out of listings and searches, and *counted* rather than silently skipped -- a
# folder that says "14 files" when it has 4000 is a lie the assistant would build on.
NOISE_DIRS = ("__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
              ".pytest_cache", ".idea", ".vscode", "dist", "build", ".next")

MAX_ENTRIES = 200          # one listing
MAX_LINES = 400            # one read, hard
DEFAULT_LINES = 200        # one read, when the assistant does not say
# The same bound its reaches back have, and for the same reason: a file big enough to
# fill its context is a file it should be reading a piece at a time.
MAX_READ_TOKENS = 6000
MAX_HITS = 100             # one find
MAX_FILES_SCANNED = 2000   # one find
MAX_FILE_BYTES = 5_000_000
MAX_OPS_PER_TURN = 4
LINE_CLIP = 200            # a matched line, in a find


def roots() -> list:
    """Every folder the assistant may look in, resolved. The repo is always among them."""
    named = list(DEFAULT_ROOTS)
    try:
        cfg = json.loads(REACH_PATH.read_text(encoding="utf-8"))
        if isinstance(cfg.get("roots"), list):
            named = [str(r) for r in cfg["roots"] if str(r).strip()]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass                       # a missing or broken file means the default

    out = []
    for name in named + [str(ROOT), str(home.HOME)]:
        try:
            p = Path(name).expanduser().resolve()
        except (OSError, ValueError):
            continue
        # A root already inside another root is not a second place it may look,
        # it is the same permission written twice, and it makes the refusal
        # message read like a list of options when it is one.
        if p.is_dir() and p not in out and _under(p, out) is None:
            out.append(p)
    return out


def _under(path: Path, allowed: list):
    """The root this path sits in, or None. Windows compares without case, and a
    path that resolved through a symlink is checked where it actually landed rather
    than where it claimed to be."""
    here = os.path.normcase(str(path))
    for root in allowed:
        top = os.path.normcase(str(root))
        if here == top or here.startswith(top + os.sep):
            return root
    return None


class Refused(Exception):
    """Said to the assistant in words, and never silently. Every one of these carries
    what it asked for and why it was not allowed, because a bound it cannot see the
    shape of is a bound it will walk into again next turn."""


def resolve(raw, must_be=None) -> Path:
    """One path, checked. `must_be` is "dir" or "file" when it matters.

    A relative path is relative to the repo, which is where the assistant
    lives -- and when nothing is there by that name, it is tried against each
    root of its reach in turn, so "Projects/notes.md" finds a file in another
    root without the whole path spelled out. Reading plain paths against this
    repo only meant naming the full path to reach anything outside it."""
    allowed = roots()
    text = (str(raw).strip() if raw is not None else "") or "."
    try:
        p = Path(text).expanduser()
        if p.is_absolute():
            p = p.resolve()
        else:
            tries = [ROOT / p] + [r / p for r in allowed]
            p = next((t for t in tries if t.exists()), tries[0]).resolve()
    except (OSError, ValueError) as exc:
        raise Refused("I could not make sense of the path " + repr(text)
                      + " (" + type(exc).__name__ + ").")

    from . import native_tools
    if native_tools.ACTIVE.get() and native_tools.private_path(p):
        raise Refused("This credential or room-private path is unavailable during native tool use.")

    if _under(p, allowed) is None:
        raise Refused(
            "The path " + str(p) + " is outside everywhere I may look. I can read "
            "inside: " + "; ".join(str(r) for r in allowed) + ". This is a bound "
            + home.OWNER_NAME + " sets, not something I can talk my way past -- if I need it, I "
            "ask " + home.OWNER_NAME + ".")

    parts = [q.lower() for q in p.parts]
    if any(d in parts for d in REFUSED_DIRS):
        raise Refused(
            "That is inside " + ", ".join(REFUSED_DIRS) + ", which I am never given. "
            "It is the machinery of the history rather than the history.")
    why = refused_for(p.name)
    if why:
        raise Refused("I am never given " + p.name + ": " + why + ".")

    if not p.exists():
        raise Refused("There is nothing at " + str(p) + ".")
    if must_be == "dir" and not p.is_dir():
        raise Refused(str(p) + " is a file, not a folder. `read` opens a file.")
    if must_be == "file" and not p.is_file():
        raise Refused(str(p) + " is a folder, not a file. `list` opens a folder.")
    return p


def _hidden(p: Path) -> bool:
    """Whether this entry is one of the ones left out of a listing."""
    from . import native_tools
    if native_tools.ACTIVE.get() and native_tools.private_path(p):
        return True
    low = p.name.lower()
    if refused_for(low) or low in REFUSED_DIRS:
        return True
    if p.is_dir() and low in NOISE_DIRS:
        return True
    return low.startswith(".") and low not in (".gitignore", ".env.example")


def _shown(p: Path) -> str:
    """How a path is said back to the assistant: relative to the repo when it is inside it,
    and whole when it is not. Short where short is unambiguous."""
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _when(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat(
            timespec="minutes")
    except OSError:
        return ""


def list_dir(spec: dict) -> dict:
    p = resolve(spec.get("path"), must_be="dir")
    entries, left_out, total = [], 0, 0
    try:
        kids = sorted(p.iterdir(), key=lambda k: (k.is_file(), k.name.lower()))
    except OSError as exc:
        raise Refused("I could not read that folder (" + type(exc).__name__ + ").")

    for kid in kids:
        total += 1
        if _hidden(kid):
            left_out += 1
            continue
        if len(entries) >= MAX_ENTRIES:
            continue
        try:
            is_dir = kid.is_dir()
            entries.append({
                "name": kid.name + ("/" if is_dir else ""),
                "kind": "dir" if is_dir else "file",
                "bytes": None if is_dir else kid.stat().st_size,
                "changed": _when(kid),
            })
        except OSError:
            left_out += 1

    shown = len(entries)
    not_shown = max(0, total - left_out - shown)
    out = {"op": "list", "path": _shown(p), "entries": entries,
           "shown": shown, "in_folder": total}
    notes = []
    if left_out:
        notes.append(str(left_out) + " left out: hidden things, caches, and what I "
                     "am never given. They are there; I am not shown them.")
    if not_shown:
        notes.append(str(not_shown) + " more not listed -- a listing stops at "
                     + str(MAX_ENTRIES) + ". I can look at a folder inside it "
                     "instead of asking for this one again.")
    out["notes"] = notes
    out["summary"] = ("listed " + _shown(p) + ": " + str(shown) + " of "
                      + str(total) + " entries")
    return out


def _text_of(p: Path) -> str:
    """A file as text, or refused in words. Binary is not something the assistant
    can read, and handing it the mangled version would be worse than saying so."""
    from . import native_tools
    if native_tools.ACTIVE.get() and native_tools.private_path(p):
        raise Refused("Credential or room-private file unavailable during native proof.")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise Refused(
            _shown(p) + " is " + str(round(size / 1_000_000, 1)) + " MB, and I do "
            "not open anything over " + str(MAX_FILE_BYTES // 1_000_000) + " MB. If "
            "I need something out of it, that is an errand for somebody with more "
            "turns than I have.")
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise Refused("I could not read that file (" + type(exc).__name__ + ").")
    if b"\x00" in raw[:8000]:
        raise Refused(
            _shown(p) + " is not text -- there are zero bytes in it. Reading it "
            "would hand me nonsense that looks like words.")
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Refused(_shown(p) + " is text in an encoding I could not work out.")


def read_file(spec: dict, conn=None) -> dict:
    named = str(spec.get("path") or "")
    if named.startswith(home.SELF + ":"):
        # Named, read-only room references use this same bounded read protocol.
        # The caller supplies its connection; never open or migrate a store here.
        from . import prompt_reference
        text = prompt_reference.read(conn, named)
        shown = named
    else:
        p = resolve(spec.get("path"), must_be="file")
        text = _text_of(p)
        shown = _shown(p)
    lines = text.splitlines()
    total = len(lines)

    start = spec.get("from_line")
    start = 1 if start is None else int(start)
    if start < 1:
        start = 1
    want = spec.get("lines")
    want = DEFAULT_LINES if want is None else int(want)
    want = max(1, min(want, MAX_LINES))

    chunk = lines[start - 1:start - 1 + want]
    notes = []
    if start > total and total:
        notes.append("There are only " + str(total) + " lines in it, so asking from "
                     + str(start) + " got me nothing. The file is not empty; my "
                     "starting point was past the end.")

    # The token bound, cutting the chunk further if the lines are long. A file can be
    # forty lines and still be most of what the assistant can hold.
    kept, spent = [], 0
    for line in chunk:
        cost = db.est_tokens(line) + 1
        if spent + cost > MAX_READ_TOKENS and kept:
            break
        kept.append(line)
        spent += cost
    if len(kept) < len(chunk):
        notes.append("I stopped at line " + str(start + len(kept) - 1) + " of "
                     + str(total) + ": the rest would not fit in what one read is "
                     "allowed. I ask again from there if I want more.")
    elif start + len(kept) - 1 < total:
        notes.append("That is lines " + str(start) + " to "
                     + str(start + len(kept) - 1) + " of " + str(total)
                     + ". There is more below.")

    summary = ("read " + shown + ", lines " + str(start) + "-"
               + str(start + len(kept) - 1) + " of " + str(total)) if kept else (
               "read " + shown + " and got no lines back: it has "
               + str(total) + " and I started at " + str(start))
    return {
        "op": "read", "path": shown,
        "from_line": start, "lines_shown": len(kept), "lines_in_file": total,
        "text": "\n".join(kept), "notes": notes, "summary": summary,
    }


def find(spec: dict) -> dict:
    p = resolve(spec.get("path"))
    pattern = (spec.get("pattern") or "").strip()
    if not pattern:
        raise Refused("I asked to find something and gave no pattern, so there was "
                      "nothing to look for.")
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise Refused(
            "That pattern is not a regular expression I could compile: " + str(exc)
            + ". It was not searched for as plain words instead -- that would find "
            "something and let me think I had searched for what I asked.")

    if p.is_file():
        targets, walked, skipped = [p], 1, 0
    else:
        targets, walked, skipped = [], 0, 0
        for here, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in sorted(dirnames)
                           if d.lower() not in NOISE_DIRS
                           and d.lower() not in REFUSED_DIRS
                           and not d.startswith(".")]
            for name in sorted(filenames):
                if refused_for(name):
                    skipped += 1
                    continue
                if walked >= MAX_FILES_SCANNED:
                    break
                walked += 1
                targets.append(Path(here) / name)
            if walked >= MAX_FILES_SCANNED:
                break

    hits, unreadable = [], 0
    for target in targets:
        if len(hits) >= MAX_HITS:
            break
        try:
            text = _text_of(target)
        except Refused:
            unreadable += 1
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append({"path": _shown(target), "line": n,
                             "text": line.strip()[:LINE_CLIP]})
                if len(hits) >= MAX_HITS:
                    break

    notes = []
    if len(hits) >= MAX_HITS:
        notes.append("I stopped at " + str(MAX_HITS) + " hits. There may be more, "
                     "and if it matters I narrow the pattern or the folder rather "
                     "than assuming that was all of them.")
    if walked >= MAX_FILES_SCANNED:
        notes.append("I stopped after looking in " + str(MAX_FILES_SCANNED)
                     + " files. This was not the whole tree.")
    if unreadable:
        notes.append(str(unreadable) + " file(s) I could not read as text were "
                     "skipped -- pictures, binaries, and the like.")
    if skipped:
        notes.append(str(skipped) + " file(s) I am never given were skipped.")
    if not hits:
        notes.append("Nothing matched. That is a real answer: either it is not "
                     "there, or I am searching for it in the wrong words.")

    return {"op": "find", "path": _shown(p), "pattern": pattern,
            "hits": hits, "files_looked_at": walked, "notes": notes,
            "summary": ("found " + str(len(hits)) + " hit"
                        + ("" if len(hits) == 1 else "s") + " for "
                        + repr(pattern) + " in " + _shown(p) + ", over "
                        + str(walked) + " file"
                        + ("" if walked == 1 else "s"))}


OPS = {"list": list_dir, "read": read_file, "find": find}


def run(spec: dict, conn=None) -> dict:
    """One operation, and never an exception into the turn. A refusal is an answer
    with words in it; a crash is a silence the assistant cannot tell from nothing found."""
    op = (spec.get("op") or "").strip().lower()
    if op not in OPS:
        return {"op": op or None, "refused":
                "There is no '" + str(op) + "' among the things I can do with files. "
                "They are: " + ", ".join(OPS) + ".",
                "summary": "asked for an operation that does not exist"}
    try:
        if op == "read":
            return read_file(spec, conn=conn)
        return OPS[op](spec)
    except Refused as no:
        return {"op": op, "path": spec.get("path"), "refused": str(no),
                "summary": "refused: " + str(no)[:120]}
    except Exception as exc:            # never costs the assistant the turn
        return {"op": op, "path": spec.get("path"),
                "refused": ("Looking broke on the way (" + type(exc).__name__ + ": "
                            + str(exc) + "). Nothing was changed by it."),
                "summary": "looking broke: " + type(exc).__name__}


def apply(specs, say=None, conn=None) -> dict:
    """Everything the assistant asked to look at this turn, in order. Same shape as
    its other reaches, so the room and its next turn read it the same way."""
    specs = [s for s in (specs or []) if s]
    if not specs:
        return None

    say = say or (lambda *a, **k: None)
    problems, ran = [], []
    refused_extra = specs[MAX_OPS_PER_TURN:]
    if refused_extra:
        problems.append(
            "I asked to look at " + str(len(specs)) + " things and "
            + str(MAX_OPS_PER_TURN) + " is the most in one turn, so "
            + str(len(refused_extra)) + " of them "
            + ("was" if len(refused_extra) == 1 else "were")
            + " not done. They are unasked, not empty.")

    for spec in specs[:MAX_OPS_PER_TURN]:
        got = run(spec, conn=conn)
        ran.append(got)
        say(got["summary"], "files", got)
        if got.get("refused"):
            problems.append(got["refused"])

    done = [g for g in ran if not g.get("refused")]
    summary = (str(len(done)) + " of " + str(len(ran)) + " looked at: "
               + "; ".join(g["summary"] for g in ran)[:300])
    return {"ran": ran, "problems": problems, "summary": summary,
            "roots": [str(r) for r in roots()]}


def _cli(argv) -> None:
    if not argv:
        print(__doc__)
        print("  may look in:")
        for r in roots():
            print("   ", r)
        return
    op = argv[0]
    spec = {"op": op, "path": argv[1] if len(argv) > 1 else None,
            "pattern": None, "from_line": None, "lines": None}
    if op == "find":
        spec["pattern"], spec["path"] = argv[1], (argv[2] if len(argv) > 2 else ".")
    elif op == "read":
        spec["from_line"] = int(argv[2]) if len(argv) > 2 else None
        spec["lines"] = int(argv[3]) if len(argv) > 3 else None

    got = run(spec)
    print("  " + got["summary"])
    if got.get("refused"):
        print("  " + got["refused"])
        return
    if got["op"] == "list":
        for e in got["entries"]:
            size = "" if e["bytes"] is None else str(e["bytes"]).rjust(9)
            print("   ", e["name"].ljust(34), size, " ", e["changed"])
    elif got["op"] == "read":
        for i, line in enumerate(got["text"].splitlines(), got["from_line"]):
            print("   ", str(i).rjust(4), line)
    elif got["op"] == "find":
        for h in got["hits"]:
            print("   ", h["path"] + ":" + str(h["line"]) + ":", h["text"])
    for note in got.get("notes") or []:
        print("  --", note)


if __name__ == "__main__":
    import sys
    _cli(sys.argv[1:])
