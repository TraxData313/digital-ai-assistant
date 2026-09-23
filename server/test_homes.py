"""The shelf of assistants: the list, making one, stopping one, and the keys.

    python -m server.test_homes C:\\somewhere\\scratch

Makes assistants only inside the scratch and keeps its list there
(ASSISTANT_REGISTRY), so the install's own homes.json and home.json are never
read. Starts nothing; the manager's own bench is test_manager.
"""

import json
import os
import shutil
import socket
import sys
from pathlib import Path

SCRATCH = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if SCRATCH:
    # Said before the shelf is imported: it reads which list at import.
    os.environ["ASSISTANT_REGISTRY"] = str(SCRATCH / "homes.json")

from . import home, homes, setup  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def refused(label, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        check(label, False, "it went through")
        return ""
    except homes.Refused as exc:
        check(label, True)
        return str(exc)


def main():
    if not SCRATCH:
        print(__doc__)
        sys.exit(2)
    scratch = SCRATCH
    if scratch == home.CODE or home.CODE in scratch.parents:
        print("that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)

    check("the list is the scratch's, not the install's", homes.REGISTRY == scratch / "homes.json")
    check("and home.json is not consulted", homes._pointer() is None)

    # -- an empty shelf ------------------------------------------------------------
    got = homes.listing()
    check("an empty shelf lists nobody -- never the code folder's bench identity",
          got["homes"] == [], got)
    check("the manager answers on 8787, wide, unless told otherwise",
          homes.front() == ("0.0.0.0", 8787), homes.front())

    # -- making one ------------------------------------------------------------
    wren = scratch / "Wren"
    made = homes.create("Wren", str(wren))
    ident = json.loads((wren / "identity.json").read_text(encoding="utf-8"))
    check("a new assistant is made in the folder", made["name"] == "Wren" and ident["slug"] == "wren")
    check("on a port of its own, not one another room uses, nor the manager's",
          ident["port"] not in setup.TAKEN and ident["port"] != homes.front()[1], ident["port"])
    check("on loopback: the manager is the door", ident["bind"] == "127.0.0.1", ident["bind"])
    check("working for the owner it was made by", ident["owner"] == home.OWNER, ident["owner"])
    check("its home is a git repo of its own", (wren / ".git").exists())
    check("it has a Spark to be rewritten", (wren / "data" / "spark.md").is_file())
    listed = homes.listing()["homes"]
    check("the list now holds it, not awake and not stopped",
          len(listed) == 1 and listed[0]["name"] == "Wren" and not listed[0]["awake"]
          and not listed[0]["stopped"], listed)
    check("and the list is kept in the registry, folders only",
          json.loads(homes.REGISTRY.read_text(encoding="utf-8")).get("homes") == [str(wren)])

    mind = {"model": "someone/some-model", "codex_only": True, "credits": {"x": 1}, "at": "then"}
    (wren / "data" / "provider.json").write_text(json.dumps(mind), encoding="utf-8")
    other = homes.create("Kestrel", str(scratch / "Kestrel"))
    kept = json.loads((scratch / "Kestrel" / "data" / "provider.json").read_text(encoding="utf-8"))
    check("a second one gets another port", other["port"] != made["port"], (other["port"], made["port"]))
    check("it thinks through the first one's mind", kept.get("model") == mind["model"]
          and kept.get("codex_only") is True, kept)
    check("but carries none of its account figures", "credits" not in kept and "at" not in kept, kept)
    k_ident = homes.identity(scratch / "Kestrel")
    check("and works for the same person, by the same name",
          k_ident["owner"] == ident["owner"], k_ident.get("owner"))

    # -- what it will not do ---------------------------------------------------
    msg = refused("a second one of the same name is refused", homes.create, "Wren", str(scratch / "Wren2"))
    check("and says why", "already answers to that name" in msg, msg)
    busy = scratch / "busy"
    busy.mkdir()
    (busy / "taxes.xlsx").write_text("x", encoding="utf-8")
    refused("a folder with other things in it is refused", homes.create, "Busy", str(busy))
    check("and nothing was written there", sorted(p.name for p in busy.iterdir()) == ["taxes.xlsx"])
    refused("a relative folder is refused", homes.create, "Rel", "somewhere\\rel")
    refused("no name is refused", homes.create, "  ", str(scratch / "x"))
    refused("a very long name is refused", homes.create, "n" * 41, str(scratch / "y"))
    refused("no folder is refused", homes.create, "Nofolder", "")
    refused("the code folder is refused", homes.create, "Inside", str(home.CODE / "inside"))
    again = homes.create("Whatever", str(wren))
    check("a folder that already holds an assistant is taken in as it is",
          again["name"] == "Wren" and len(homes.listing()["homes"]) == 2, again)

    # -- stopped, and staying stopped -------------------------------------------------
    homes.set_stopped(wren, True)
    check("a stopped assistant is written down as stopped", homes.stopped(wren) and not homes.stopped(scratch / "Kestrel"))
    homes.set_stopped(wren, True)
    check("stopping twice writes it once",
          json.loads(homes.REGISTRY.read_text(encoding="utf-8"))["stopped"] == [str(wren)])
    check("the list says so", [e["stopped"] for e in homes.listing()["homes"]] == [True, False])
    homes.set_stopped(wren, False)
    check("and starting it clears it", not homes.stopped(wren))
    check("the rest of the registry survives the writes",
          json.loads(homes.REGISTRY.read_text(encoding="utf-8"))["homes"] == [str(wren), str(scratch / "Kestrel")])

    # -- the manager's port -----------------------------------------------------------
    data = json.loads(homes.REGISTRY.read_text(encoding="utf-8"))
    data["port"] = 8812
    data["bind"] = "127.0.0.1"
    homes.REGISTRY.write_text(json.dumps(data), encoding="utf-8")
    check("the registry can move the manager", homes.front() == ("127.0.0.1", 8812), homes.front())
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        taken = sock.getsockname()[1]
        sock.listen(1)
        check("a free port skips one something is listening on", homes.free_port(taken) != taken)
    finally:
        sock.close()
    data["port"] = 8850
    homes.REGISTRY.write_text(json.dumps(data), encoding="utf-8")
    check("and never hands out the manager's own", homes.free_port(8850) != 8850)
    data.pop("port")
    data.pop("bind")
    homes.REGISTRY.write_text(json.dumps(data), encoding="utf-8")

    # -- the keys -----------------------------------------------------------------------
    check("a home the room never opened has no owner key yet", homes.owner_key(wren) == "")
    key = homes.mint_owner_key(wren)
    check("the manager can make it", len(key) == 64 and homes.owner_key(wren) == key)
    check("and making it again keeps it -- a paired phone stays paired", homes.mint_owner_key(wren) == key)
    check("the key says whose it is", homes.whose(wren, key) == ident["owner"])
    check("in that home only", homes.whose(scratch / "Kestrel", key) == "")
    check("a wrong key is nobody's", homes.whose(wren, key[:-1] + ("0" if key[-1] != "0" else "1")) == "")
    check("no key is nobody's", homes.whose(wren, "") == "")

    # -- detached, moved, and attached again ---------------------------------------------
    homes.set_stopped(wren, True)
    homes.forget(wren)
    data = json.loads(homes.REGISTRY.read_text(encoding="utf-8"))
    check("forgetting takes it off the list and off the stopped list",
          str(wren) not in data["homes"] and str(wren) not in data["stopped"], data)
    check("and leaves its folder exactly where it was",
          (wren / "identity.json").is_file() and (wren / "data" / "spark.md").is_file())
    moved = scratch / "elsewhere" / "Wren"
    moved.parent.mkdir()
    wren.rename(moved)
    got = homes.attach(str(moved))
    check("attached again from where it went", got["name"] == "Wren" and got["home"] == str(moved)
          and not got["note"] and not homes.stopped(moved), got)
    check("with the same port and the same key", got["port"] == made["port"] and homes.owner_key(moved) == key)
    check("attaching it twice is the same as once",
          homes.attach(str(moved))["note"] == "Wren is already here"
          and json.loads(homes.REGISTRY.read_text(encoding="utf-8"))["homes"].count(str(moved)) == 1)
    msg = ""
    try:
        homes.attach(str(scratch / "busy"))
    except homes.Refused as exc:
        msg = str(exc)
    check("a folder with no assistant in it is refused, saying what to choose", "identity.json" in msg, msg)
    refused("a relative folder is refused", homes.attach, "elsewhere\\Wren")
    refused("the code folder is refused", homes.attach, str(home.CODE))
    refused("no folder is refused", homes.attach, "")
    twin = scratch / "twin"
    shutil.copytree(moved, twin)
    msg = refused("a second one of the same name is refused", homes.attach, str(twin))
    check("and says to detach the other first", "Detach that one first" in msg, msg)
    homes.forget(scratch / "Kestrel")
    ident_k = homes.identity(scratch / "Kestrel")
    ident_k["port"] = made["port"]
    (scratch / "Kestrel" / "identity.json").write_text(json.dumps(ident_k), encoding="utf-8")
    got = homes.attach(str(scratch / "Kestrel"))
    check("one whose port another here uses is given a free one, and told",
          got["port"] not in (made["port"], homes.front()[1]) and str(made["port"]) in got["note"]
          and homes.identity(scratch / "Kestrel")["port"] == got["port"], got)
    check("keeping everything else in its identity", homes.identity(scratch / "Kestrel")["slug"] == "kestrel")
    check("the install's own home is never a folder the list can lose", not homes.is_first(moved))

    print()
    if FAILED:
        print(str(len(FAILED)) + " failed")
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
