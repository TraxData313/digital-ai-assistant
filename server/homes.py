"""The assistants this install knows, and making another.

One install runs one home by default -- the one `home.json` points at. This
is the rest of the shelf: every home made or opened from the room, so the
page can list them, wake one that is asleep, and make a new one. Each runs as
its own room on its own port, held by its own icon in the corner, with its own
store; nothing here lets one read another's memory.

Everything that acts -- making a home, starting a room, handing the browser
a key -- is for the desk only, and the routes check that before calling in.
The list itself lives in `homes.json` beside the code: which folders, nothing
else. It is per machine, like `home.json`, and never committed.

    python -m server.homes          # what this install knows, and who is awake
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import home, setup

REGISTRY = home.CODE / "homes.json"
START = home.CODE / "start.pyw"
# What a new assistant keeps from the room that made it: which mind it
# thinks through. Not the balance or when it was set -- those are one
# account's figures, and they belong to the room that read them.
MIND_KEYS = ("model", "dream_model", "codex_only", "native_tools")
NAME_MAX = 40

_LOCK = threading.Lock()
_BROWSING = threading.Lock()


class Refused(ValueError):
    """Something the room will not do, with a reason fit for the page."""


def _same(a, b) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _read_registry() -> list:
    try:
        got = json.loads(REGISTRY.read_text(encoding="utf-8")).get("homes")
        return [str(p) for p in got if isinstance(p, str)] if isinstance(got, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def remember(folder) -> None:
    """Add a home to this install's list. Idempotent."""
    folder = Path(folder).resolve()
    with _LOCK:
        have = _read_registry()
        if any(_same(p, folder) for p in have):
            return
        have.append(str(folder))
        tmp = REGISTRY.with_suffix(".tmp")
        tmp.write_text(json.dumps({"homes": have}, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, REGISTRY)


def _pointer():
    try:
        chosen = json.loads(home.POINTER.read_text(encoding="utf-8")).get("home")
        return Path(chosen) if chosen else None
    except (OSError, ValueError, AttributeError):
        return None


def identity(folder) -> dict:
    """A home's identity.json, or {} if the folder holds no assistant. This
    room's own is the one it is running as, file or not."""
    if _same(folder, home.HOME):
        return dict(home.IDENTITY)
    try:
        got = json.loads((Path(folder) / "identity.json").read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def known() -> list:
    """Every home this install knows: this room's, the one home.json runs,
    and the rest of the list -- once each, and only folders that still hold
    an assistant."""
    out = [home.HOME.resolve()]
    for p in [_pointer(), *_read_registry()]:
        if not p:
            continue
        p = Path(p)
        if not identity(p) or any(_same(p, q) for q in out):
            continue
        out.append(p.resolve())
    return out


def listening(port, timeout=0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, TypeError):
        return False


def entry(folder) -> dict:
    ident = identity(folder)
    current = _same(folder, home.HOME)
    port = ident.get("port")
    return {
        "home": str(Path(folder).resolve()),
        "name": ident.get("name") or Path(folder).name,
        "title": ident.get("title") or ident.get("name") or Path(folder).name,
        "slug": ident.get("slug") or "",
        "port": port,
        "current": current,
        # This room is awake by definition; the others are asked.
        "awake": True if current else listening(port),
    }


def listing() -> dict:
    return {"homes": [entry(p) for p in known()],
            # Where a new one goes unless told otherwise: beside this one.
            "default_parent": str(home.HOME.parent if home.is_real() else Path.home() / "Documents")}


def free_port(start=8788) -> int:
    """A port no room on this install uses and nothing is listening on."""
    used = set(setup.TAKEN)
    for p in known():
        port = identity(p).get("port")
        if isinstance(port, int):
            used.add(port)
    for port in range(start, start + 200):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise Refused("no free port was found from " + str(start))


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise Refused("give the new assistant a name")
    if len(name) > NAME_MAX:
        raise Refused("a name of at most " + str(NAME_MAX) + " characters, please")
    return name


def create(name, folder) -> dict:
    """Make a new assistant in `folder` (or take in the one already there),
    put it on this install's list, and return its entry. Starts nothing."""
    name = _clean_name(name)
    raw = str(folder or "").strip().strip('"')
    if not raw:
        raise Refused("choose a folder for it to live in")
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise Refused("the folder must be a whole path, like C:\\Users\\you\\Documents\\" + name)
    folder = folder.resolve()
    if _same(folder, home.CODE) or home.CODE in folder.parents:
        raise Refused("an assistant does not live inside the code folder; choose another")

    shape = setup.state(folder)
    if shape == "other":
        raise Refused(str(folder) + " already holds other things. Choose an empty folder, "
                      "or a new one -- it will be made.")
    if shape == "home":
        # Someone is already there: take it in as it is, name and all.
        remember(folder)
        return entry(folder)

    slug = setup._slug(name)
    for p in known():
        if identity(p).get("slug") == slug:
            raise Refused(identity(p).get("name", slug) + " already answers to that name on "
                          "this machine; two rooms of one name would share a browser key. "
                          "Choose another name.")
    ident = setup.make(folder, name, home.OWNER_NAME, free_port(), owner_id=home.OWNER,
                       timezone=home.IDENTITY.get("timezone") or "UTC")
    # It thinks through what this room thinks through until someone changes
    # it under its own Settings -- a new room with no working mind is a room
    # whose first line breaks.
    mind = home.settings("provider.json")
    kept = {k: mind[k] for k in MIND_KEYS if k in mind}
    if kept:
        (folder / "data" / "provider.json").write_text(
            json.dumps(kept, indent=1) + "\n", encoding="utf-8")
    remember(folder)
    return entry(folder)


def _pythonw() -> str:
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.exists() else exe)


def start(folder) -> bool:
    """Wake a known home's room under its own icon, detached from this one,
    so it outlives a restart of the room that asked. True if it was already
    awake."""
    folder = Path(folder).resolve()
    if not any(_same(folder, p) for p in known()):
        raise Refused("that folder is not one of this install's assistants")
    ident = identity(folder)
    if listening(ident.get("port")):
        return True
    # Nothing of this room's own running rides along: the new one reads its
    # home from ASSISTANT_HOME and nothing else of ours.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ASSISTANT_")}
    env["ASSISTANT_HOME"] = str(folder)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    for extra in (breakaway, 0):
        try:
            subprocess.Popen([_pythonw(), str(START)], cwd=str(home.CODE), env=env,
                             creationflags=flags | extra, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return False
        except OSError:
            if not extra:
                raise
    return False


def open_url(folder, wait=45.0) -> str:
    """The address that walks this browser into another home's room: started
    if it is asleep, and carrying its owner's key once, which the room moves
    into a cookie and out of the address bar at once. For the desk only --
    the caller checks."""
    folder = Path(folder).resolve()
    if _same(folder, home.HOME):
        raise Refused("that is this room")
    start(folder)
    ident = identity(folder)
    port = ident.get("port")
    token = folder / "data" / "people" / (str(ident.get("owner")) + ".token")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if listening(port):
            try:
                key = token.read_text(encoding="utf-8").strip()
            except OSError:
                key = ""
            if key:
                return "http://localhost:" + str(port) + "/?k=" + key
        time.sleep(0.5)
    raise Refused((ident.get("name") or "it") + " did not come up within "
                  + str(int(wait)) + " seconds. What it said is in "
                  + str(folder / "logs"))


def browse(start_in=None):
    """A folder picked in Windows' own dialog, on this machine's screen. None
    if it was closed without a choice. One at a time."""
    if not _BROWSING.acquire(blocking=False):
        raise Refused("a folder dialog is already open on the desk")
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            got = filedialog.askdirectory(
                parent=root, mustexist=False,
                initialdir=str(start_in or listing()["default_parent"]),
                title="Where should the new assistant live?")
        finally:
            root.destroy()
        return str(Path(got)) if got else None
    finally:
        _BROWSING.release()


if __name__ == "__main__":
    for e in listing()["homes"]:
        print(("* " if e["current"] else "  ") + e["name"].ljust(16) + " port "
              + str(e["port"]).ljust(6) + ("awake " if e["awake"] else "asleep") + "  " + e["home"])
