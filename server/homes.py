"""The assistants this install knows, and making another.

The manager (`server/manager.py`) holds every one of them: it starts each
room, brings it back when it falls, leaves it down when someone stopped it,
and is the one door they are all reached through. This is the shelf it reads.

`homes.json` beside the code says which folders, which of them were stopped
on purpose, and which port the manager answers on. It is per machine, like
`home.json`, and never committed. The home `home.json` points at is on the
shelf whether or not the list names it.

    python -m server.homes          # what this install knows, and who is awake
"""

import json
import os
import secrets
import socket
import threading
from pathlib import Path

from . import home, setup

# A bench keeps its own list, and then home.json is not consulted either:
# nothing a test does may find the real assistants.
_OWN_REGISTRY = os.environ.get("ASSISTANT_REGISTRY")
REGISTRY = Path(_OWN_REGISTRY) if _OWN_REGISTRY else home.CODE / "homes.json"
# The one port the manager answers on for all of them, and how wide. Wide on
# purpose, as the room used to be: narrowed at the door to this machine and
# the owner's tailnet, never at the socket (see the room's own reasons in
# app.main).
FRONT_PORT = 8787
FRONT_BIND = "0.0.0.0"
# What a new assistant keeps from the one that made it: which mind it
# thinks through. Not the balance or when it was set -- those are one
# account's figures, and they belong to the room that read them.
MIND_KEYS = ("model", "dream_model", "codex_only", "native_tools")
NAME_MAX = 40

_LOCK = threading.Lock()
_BROWSING = threading.Lock()


class Refused(ValueError):
    """Something that will not be done, with a reason fit for the page."""


def _same(a, b) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _read() -> dict:
    try:
        got = json.loads(REGISTRY.read_text(encoding="utf-8"))
        got = got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        got = {}

    def paths(key):
        v = got.get(key)
        return [str(p) for p in v if isinstance(p, str)] if isinstance(v, list) else []

    port = got.get("port")
    return {"homes": paths("homes"), "stopped": paths("stopped"),
            "port": port if isinstance(port, int) and 0 < port < 65536 else FRONT_PORT,
            "bind": str(got.get("bind") or FRONT_BIND)}


def _write(data: dict) -> None:
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY)


def front() -> tuple:
    """Where the manager answers: (bind, port)."""
    got = _read()
    return got["bind"], got["port"]


def remember(folder) -> None:
    """Add a home to this install's list. Idempotent."""
    folder = Path(folder).resolve()
    with _LOCK:
        data = _read()
        if any(_same(p, folder) for p in data["homes"]):
            return
        data["homes"].append(str(folder))
        _write(data)


def forget(folder) -> None:
    """Take a home off this install's list, and off the stopped list. Its
    folder is not touched: it can be moved anywhere and attached again."""
    folder = Path(folder).resolve()
    with _LOCK:
        data = _read()
        data["homes"] = [p for p in data["homes"] if not _same(p, folder)]
        data["stopped"] = [p for p in data["stopped"] if not _same(p, folder)]
        _write(data)


def is_first(folder) -> bool:
    """Whether this is the home home.json runs -- the install's own, which
    stays on the shelf whatever the list says."""
    first = _pointer()
    return bool(first) and _same(first, folder)


def stopped(folder) -> bool:
    """Whether someone put this assistant down and meant it to stay down."""
    return any(_same(p, folder) for p in _read()["stopped"])


def set_stopped(folder, down: bool) -> None:
    """Kept across restarts of the manager and of the machine: a stopped
    assistant does not come back at login, dream at night, or run its jobs
    until someone starts it again."""
    folder = Path(folder).resolve()
    with _LOCK:
        data = _read()
        rest = [p for p in data["stopped"] if not _same(p, folder)]
        data["stopped"] = rest + ([str(folder)] if down else [])
        _write(data)


def _pointer():
    if _OWN_REGISTRY:
        return None
    try:
        chosen = json.loads(home.POINTER.read_text(encoding="utf-8")).get("home")
        return Path(chosen) if chosen else None
    except (OSError, ValueError, AttributeError):
        return None


def identity(folder) -> dict:
    """A home's identity.json as it is on disk now, or {} if the folder holds
    no assistant."""
    try:
        got = json.loads((Path(folder) / "identity.json").read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def known() -> list:
    """Every home this install knows: the one home.json runs, then the list,
    then this room's own -- once each, and only folders that still hold an
    assistant. Never the code folder, whose identity is a bench's."""
    out = []
    for p in [_pointer(), *_read()["homes"], home.HOME if home.is_real() else None]:
        if not p:
            continue
        p = Path(p)
        if _same(p, home.CODE) or not identity(p) or any(_same(p, q) for q in out):
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
    port = ident.get("port")
    return {
        "home": str(Path(folder).resolve()),
        "name": ident.get("name") or Path(folder).name,
        "title": ident.get("title") or ident.get("name") or Path(folder).name,
        "slug": ident.get("slug") or "",
        "owner": ident.get("owner") or "",
        "port": port,
        "awake": listening(port),
        "stopped": stopped(folder),
    }


def default_parent() -> str:
    """Where a new one goes unless told otherwise: beside the first one."""
    first = known()
    return str(first[0].parent if first else Path.home() / "Documents")


def listing() -> dict:
    return {"homes": [entry(p) for p in known()], "default_parent": default_parent()}


# -- keys ---------------------------------------------------------------------
# Each home keeps its own people's keys under data/people. The manager reads
# them to know who is at a browser, never to hand one out.

def keys_of(folder) -> dict:
    """Every paired person in a home, name to key, read off its disk now."""
    out = {}
    try:
        for f in sorted((Path(folder) / "data" / "people").glob("*.token")):
            try:
                key = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if key:
                out[f.stem.lower()] = key
    except OSError:
        pass
    return out


def whose(folder, key) -> str:
    """Whose key this is in that home, or "" -- compared in constant time and
    against every candidate, like the room's own door."""
    key = (key or "").strip()
    found = ""
    for name, mine in keys_of(folder).items():
        if len(key) == len(mine) and secrets.compare_digest(key, mine):
            found = found or name
    return found if key else ""


def owner_key(folder) -> str:
    return keys_of(folder).get(str(identity(folder).get("owner") or "").lower(), "")


def mint_owner_key(folder) -> str:
    """The owner's key in that home, made if it is missing and kept if it is
    not -- the same file, and the same promise, as people.mint: a paired
    phone is never un-paired by this."""
    have = owner_key(folder)
    owner = str(identity(folder).get("owner") or "").lower()
    if have or not people_name_ok(owner):
        return have
    path = Path(folder) / "data" / "people" / (owner + ".token")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(secrets.token_hex(32), encoding="utf-8")
    os.replace(tmp, path)
    return owner_key(folder)


def people_name_ok(name) -> bool:
    import re
    return bool(re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", name or ""))


# -- making one ---------------------------------------------------------------

def free_port(start=8788) -> int:
    """A port no room on this install uses, that is not the manager's, and
    that nothing is listening on."""
    used = set(setup.TAKEN) | {front()[1]}
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
                          "this machine; two of one name would share an address and a "
                          "browser key. Choose another name.")
    # Whom it works for and when its day is, from the first assistant on the
    # shelf -- the same person, on the same machine.
    first = known()
    elder = identity(first[0]) if first else dict(home.IDENTITY)
    owner = str(elder.get("owner") or home.OWNER)
    owner_name = ((elder.get("people") or {}).get(owner) or {}).get("called") or home.OWNER_NAME
    ident = setup.make(folder, name, owner_name, free_port(), owner_id=owner,
                       timezone=elder.get("timezone") or "UTC")
    # It thinks through what the first one thinks through until someone
    # changes it under its own Settings -- a new room with no working mind is
    # a room whose first line breaks.
    mind = _settings(first[0], "provider.json") if first else {}
    kept = {k: mind[k] for k in MIND_KEYS if k in mind}
    if kept:
        (folder / "data" / "provider.json").write_text(
            json.dumps(kept, indent=1) + "\n", encoding="utf-8")
    remember(folder)
    return entry(folder)


def attach(folder) -> dict:
    """Put an assistant that already has a home back on the shelf -- one that
    was detached and moved, say, or made on another machine. Its folder must
    hold its identity.json. If its port is one another assistant here uses
    (or the manager's), it is given a free one, and `note` says so."""
    raw = str(folder or "").strip().strip('"')
    if not raw:
        raise Refused("choose the folder the assistant lives in")
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise Refused("the folder must be a whole path, like C:\\Users\\you\\Documents\\Wren")
    folder = folder.resolve()
    if _same(folder, home.CODE) or home.CODE in folder.parents:
        raise Refused("an assistant does not live inside the code folder")
    ident = identity(folder)
    if not ident:
        raise Refused("no assistant lives in " + str(folder) + ": there is no identity.json in it. "
                      "Choose the folder that holds identity.json and data.")
    name = str(ident.get("name") or folder.name)
    others = [p for p in known() if not _same(p, folder)]
    if len(others) < len(known()):
        return dict(entry(folder), note=name + " is already here")
    slug = ident.get("slug")
    for p in others:
        if identity(p).get("slug") == slug:
            raise Refused(identity(p).get("name", slug) + " already answers to that name here; two of "
                          "one name would share an address and a browser key. Detach that one first.")
    note = ""
    used = {front()[1]} | {identity(p).get("port") for p in others}
    if not isinstance(ident.get("port"), int) or ident["port"] in used:
        was = ident.get("port")
        ident["port"] = free_port()
        path = folder / "identity.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ident, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        note = (name + "'s port " + str(was) + " is taken here, so it has " + str(ident["port"])
                + " now (in its identity.json)")
    remember(folder)
    set_stopped(folder, False)
    return dict(entry(folder), note=note)


def _settings(folder, name) -> dict:
    try:
        got = json.loads((Path(folder) / "data" / name).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def browse(start_in=None, title="Where should the new assistant live?"):
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
                initialdir=str(start_in or default_parent()),
                title=title)
        finally:
            root.destroy()
        return str(Path(got)) if got else None
    finally:
        _BROWSING.release()


if __name__ == "__main__":
    bind, port = front()
    print("the manager answers on " + bind + ":" + str(port))
    for e in listing()["homes"]:
        print("  " + e["name"].ljust(16) + " /" + (e["slug"] + "/").ljust(12) + " port "
              + str(e["port"]).ljust(6)
              + ("awake  " if e["awake"] else "stopped" if e["stopped"] else "asleep ")
              + "  " + e["home"])
