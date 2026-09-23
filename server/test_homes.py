"""The other assistants: the list, making one, waking one, and who may.

    python -m server.test_homes C:\\somewhere\\scratch

Makes assistants only inside the scratch, keeps its list there, and starts no
room: the launch is swapped for a stand-in that records what it would have
run. The install's own home.json is not consulted. Then the routes, through a
bench room on a port the operating system picks, from the desk and from a
pretended tailnet peer.
"""

import ipaddress
import json
import os
import shutil
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import app, home, homes, people, setup

FAILED = []
PRETEND = {"addr": "127.0.0.1"}


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


def _pretend_peer(self):
    return ipaddress.ip_address(PRETEND["addr"])


class FakePopen:
    runs = []

    def __init__(self, argv, **kwargs):
        FakePopen.runs.append({"argv": argv, **kwargs})


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    if scratch == home.CODE or home.CODE in scratch.parents:
        print("that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)

    homes.REGISTRY = scratch / "homes.json"
    homes._pointer = lambda: None
    mind = {"model": "someone/some-model", "codex_only": True, "credits": {"x": 1}, "at": "then"}
    homes.home.settings = lambda name: dict(mind) if name == "provider.json" else {}

    # -- the list ------------------------------------------------------------
    got = homes.listing()
    check("the list starts with this room, awake",
          len(got["homes"]) == 1 and got["homes"][0]["current"] and got["homes"][0]["awake"], got)

    # -- making one ------------------------------------------------------------
    wren = scratch / "Wren"
    made = homes.create("Wren", str(wren))
    ident = json.loads((wren / "identity.json").read_text(encoding="utf-8"))
    check("a new assistant is made in the folder", made["name"] == "Wren" and ident["slug"] == "wren")
    check("on a port of its own, not one another room uses",
          ident["port"] not in setup.TAKEN and ident["port"] != home.PORT, ident["port"])
    check("working for this room's owner", ident["owner"] == home.OWNER, ident["owner"])
    check("its home is a git repo of its own", (wren / ".git").exists())
    check("it has a Spark to be rewritten", (wren / "data" / "spark.md").is_file())
    kept = json.loads((wren / "data" / "provider.json").read_text(encoding="utf-8"))
    check("it thinks through the same mind", kept.get("model") == mind["model"] and kept.get("codex_only") is True, kept)
    check("but carries none of this room's account figures", "credits" not in kept and "at" not in kept, kept)
    listed = homes.listing()["homes"]
    check("the list now holds it, asleep",
          len(listed) == 2 and listed[1]["name"] == "Wren" and not listed[1]["awake"], listed)
    check("and the list is kept beside the code, folders only",
          json.loads(homes.REGISTRY.read_text(encoding="utf-8")) == {"homes": [str(wren)]})
    other = homes.create("Kestrel", str(scratch / "Kestrel"))
    check("a second one gets another port", other["port"] != made["port"], (other["port"], made["port"]))

    # -- what it will not do ---------------------------------------------------
    msg = refused("a second room of the same name is refused", homes.create, "Wren", str(scratch / "Wren2"))
    check("and says why", "already answers to that name" in msg, msg)
    busy = scratch / "busy"
    busy.mkdir()
    (busy / "taxes.xlsx").write_text("x", encoding="utf-8")
    msg = refused("a folder with other things in it is refused", homes.create, "Busy", str(busy))
    check("and nothing was written there", sorted(p.name for p in busy.iterdir()) == ["taxes.xlsx"])
    refused("a relative folder is refused", homes.create, "Rel", "somewhere\\rel")
    refused("no name is refused", homes.create, "  ", str(scratch / "x"))
    refused("a very long name is refused", homes.create, "n" * 41, str(scratch / "y"))
    refused("no folder is refused", homes.create, "Nofolder", "")
    refused("the code folder is refused", homes.create, "Inside", str(home.CODE / "inside"))
    again = homes.create("Whatever", str(wren))
    check("a folder that already holds an assistant is taken in as it is",
          again["name"] == "Wren" and len(homes.listing()["homes"]) == 3, again)

    # -- waking one -------------------------------------------------------------
    os.environ["ASSISTANT_SUPERVISED"] = "1"
    FakePopen.runs.clear()
    real_popen = homes.subprocess.Popen
    # The one launch in this module; setup's git init above ran for real.
    homes.subprocess.Popen = FakePopen
    awake = homes.start(str(wren))
    run = FakePopen.runs[-1] if FakePopen.runs else {}
    check("an asleep room is started", awake is False and bool(run))
    check("by the launcher the icon uses", run.get("argv", [""])[-1].endswith("start.pyw"), run.get("argv"))
    env = run.get("env") or {}
    check("told its own home and nothing else of this room's",
          env.get("ASSISTANT_HOME") == str(wren)
          and not [k for k in env if k.upper().startswith("ASSISTANT_") and k != "ASSISTANT_HOME"], env.get("ASSISTANT_HOME"))
    check("detached, so it outlives a restart of this room",
          run.get("creationflags", 0) & getattr(homes.subprocess, "DETACHED_PROCESS", 8), run.get("creationflags"))
    refused("a folder that is not on the list is not started", homes.start, str(scratch / "stranger"))

    # -- the key, handed to this browser once ------------------------------------
    port = made["port"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    def answer():
        try:
            while True:
                sock.accept()[0].close()
        except OSError:
            pass            # closed at the end of this section
    threading.Thread(target=answer, daemon=True).start()
    try:
        msg = refused("no key yet: it says so after the wait", homes.open_url, str(wren), wait=1.0)
        check("naming where the room's log is", "logs" in msg, msg)
        (wren / "data" / "people").mkdir(parents=True, exist_ok=True)
        (wren / "data" / "people" / (home.OWNER + ".token")).write_text("k-123\n", encoding="utf-8")
        url = homes.open_url(str(wren), wait=5.0)
        check("once it is up, the address carries its owner's key",
              url == "http://localhost:" + str(port) + "/?k=k-123", url)
        check("and an awake room is not started twice",
              homes.start(str(wren)) is True)
    finally:
        sock.close()
    refused("this room is not opened from itself", homes.open_url, str(home.HOME))
    homes.subprocess.Popen = real_popen

    # -- the routes ---------------------------------------------------------------
    people.TOKEN_DIR = scratch / "people"
    owner = people.mint(home.OWNER)
    second = [p for p in home.HOUSEHOLD if p != home.OWNER]
    guest = people.mint(second[0]) if second else ""
    app.Handler._peer = _pretend_peer
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    base = "http://127.0.0.1:" + str(room.server_port)
    threading.Thread(target=room.serve_forever, daemon=True).start()

    def ask(path, key, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers={"Cookie": home.COOKIE + "=" + key})
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    try:
        PRETEND["addr"] = "127.0.0.1"
        code, body = ask("/api/homes", owner)
        check("at the desk the room lists its assistants",
              code == 200 and [h["name"] for h in body.get("homes", [])][:1] == [home.NAME], (code, body))
        code, body = ask("/api/homes/open", owner, {"home": str(scratch / "stranger")})
        check("a folder not on the list is refused in words", code == 400 and "not one of" in body.get("error", ""), (code, body))
        if guest:
            code, body = ask("/api/homes/new", guest, {"name": "Guest", "folder": str(scratch / "g")})
            check("only the owner makes one", code == 403 and not (scratch / "g").exists(), (code, body))
        code, body = ask("/api/homes/new", owner, {"name": "Busy", "folder": str(busy)})
        check("a refusal comes back as words", code == 400 and "other things" in body.get("error", ""), (code, body))
        PRETEND["addr"] = "100.101.102.103"
        code, body = ask("/api/homes", owner)
        check("from the road the list is not read", code == 403 and "desk" in body.get("error", ""), (code, body))
        code, body = ask("/api/homes/new", owner, {"name": "Road", "folder": str(scratch / "road")})
        check("and nothing is made from the road", code == 403 and not (scratch / "road").exists(), (code, body))
    finally:
        room.shutdown()

    print()
    if FAILED:
        print(str(len(FAILED)) + " failed")
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
