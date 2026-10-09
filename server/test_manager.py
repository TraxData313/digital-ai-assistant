"""The manager, leaned on: its door, its passing-on, its keeping, and the
room's side of the bargain.

    python -m server.test_manager C:\\somewhere\\scratch

Everything lives in the scratch: the list (ASSISTANT_REGISTRY), the homes,
and the rooms, which are stand-ins -- a few lines of HTTP that echo what they
were sent and can be told to crash, restart themselves or be busy. No real
room boots and no model is called. The room's own door is then leaned on
in-process, the real Handler over the bench identity, with the headers the
manager sends and every way of getting them wrong.
"""

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRATCH = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if SCRATCH:
    os.environ["ASSISTANT_REGISTRY"] = str(SCRATCH / "homes.json")

from . import app, home, homes, manager, people  # noqa: E402

FAILED = []
PRETEND = {"addr": None}

STAND_IN = r'''
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port, flags = int(sys.argv[1]), sys.argv[2]
os.makedirs(flags, exist_ok=True)

def leave(code):
    threading.Timer(0.2, lambda: os._exit(code)).start()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def reply(self, code, obj, extra=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.route()
    def do_POST(self):
        self.route()
    def route(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        p = self.path
        if p.startswith("/api/progress"):
            return self.reply(200, {"busy": os.path.exists(os.path.join(flags, "busy")), "steps": []})
        if p.startswith("/api/redirect"):
            self.send_response(302)
            self.send_header("Location", "/somewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            for part in (b"one ", b"two ", b"three"):
                self.wfile.write(part)
                self.wfile.flush()
            self.close_connection = True
            return
        if p.startswith("/api/exit7"):
            self.reply(200, {})
            return leave(7)
        if p.startswith("/api/crash"):
            self.reply(200, {})
            return leave(3)
        return self.reply(200, {"path": p, "method": self.command, "len": len(body),
                                "pid": os.getpid(), "headers": dict(self.headers.items())})

ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name + (("  -- " + str(detail)[:300]) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def until(test, seconds=20.0):
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.2)
    return test()


class Pretend(manager.DoorHandler):
    """The door, with the knocker's address swapped for a pretended one."""

    def setup(self):
        super().setup()
        if PRETEND["addr"]:
            self.client_address = (PRETEND["addr"], 0)


def ask(port, method, path, cookie="", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    got = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return r.status, raw, got, parsed


def echoed(parsed, name):
    heads = {k.lower(): v for k, v in ((parsed or {}).get("headers") or {}).items()}
    return heads.get(name.lower())


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
    stand_in = scratch / "stand_in.py"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    flags = scratch / "flags"

    # Homes on ports well away from any real room's.
    real_free = homes.free_port
    homes.free_port = lambda start=8788: real_free(18850)
    finch = Path(homes.create("Finch", str(scratch / "Finch"))["home"])
    heron = Path(homes.create("Heron", str(scratch / "Heron"))["home"])
    manager.Assistant.command = lambda self: [sys.executable, "-u", str(stand_in), str(self.port),
                                              str(flags / self.slug)]

    door_port = free_port()
    keeper = manager.Keeper("127.0.0.1", door_port)
    door = manager.Door(("127.0.0.1", door_port), Pretend)
    door.keeper = keeper
    keeper.door = door
    threading.Thread(target=door.serve_forever, daemon=True).start()
    keeper.load()
    threading.Thread(target=keeper.watch, daemon=True).start()

    F, H = keeper.by_slug("finch"), keeper.by_slug("heron")
    check("both assistants are kept", F is not None and H is not None)
    check("and both wake", until(lambda: F.look()["phase"] == "awake" and H.look()["phase"] == "awake"),
          (F.look(), H.look()))
    owner = homes.owner_key(finch)
    check("an owner key was made for a home that never had a room up", len(owner) == 64)
    me = "finch_who=" + owner

    try:
        # -- the door ------------------------------------------------------------------
        code, _, _, _ = ask(door_port, "GET", "/")
        check("no key: nothing", code == 403, code)
        code, _, got, _ = ask(door_port, "GET", "/?k=" + owner)
        check("a key in the address is kept in that assistant's cookie",
              code == 302 and got.get("location") == "/" and got.get("set-cookie", "").startswith("finch_who=" + owner),
              (code, got))
        code, raw, _, _ = ask(door_port, "GET", "/", me)
        check("the manager's page", code == 200 and b"Digital Assistant Manager" in raw and b'data-focus=""' in raw, code)
        code, _, _, listing = ask(door_port, "GET", "/_/api/list", me)
        names = [a["slug"] for a in (listing or {}).get("assistants", [])]
        check("lists both, to their owner, at the desk",
              code == 200 and names == ["finch", "heron"] and listing["owner"] and listing["desk"], listing)
        check("with their folders, at the desk",
              all(a["home"] for a in listing["assistants"]), listing)
        for path, kind in (("/_/manager.js", "javascript"), ("/_/manager.css", "css"),
                           ("/_/icon.ico", "icon"), ("/_/face/finch", "png")):
            code, _, got, _ = ask(door_port, "GET", path, me)
            check("serves " + path, code == 200 and kind in got.get("content-type", ""), (code, got.get("content-type")))

        # -- passing on ------------------------------------------------------------------
        code, _, got, _ = ask(door_port, "GET", "/finch", me)
        check("an assistant's bare name gets its slash", code == 302 and got.get("location") == "/finch/", got)
        code, _, _, e = ask(door_port, "GET", "/finch/api/echo?x=1", me,
                            headers={"X-Assistant-Who": "evil", "X-Assistant-Manager": "nope",
                                     "X-Assistant-Peer": "100.64.0.1"})
        check("a knock reaches the room with its prefix taken off", code == 200 and e["path"] == "/api/echo?x=1", e)
        check("carrying the manager's key", echoed(e, "X-Assistant-Manager") == manager.KEY)
        check("and the real knocker's address, not one the browser claimed",
              echoed(e, "X-Assistant-Peer") == "127.0.0.1", echoed(e, "X-Assistant-Peer"))
        check("and who the manager knows it to be", echoed(e, "X-Assistant-Who") == home.OWNER,
              echoed(e, "X-Assistant-Who"))
        check("and where it sits", echoed(e, "X-Assistant-Prefix") == "/finch")
        check("with the Host the browser used, for the room's own origin check",
              echoed(e, "Host") == "127.0.0.1:" + str(door_port), echoed(e, "Host"))
        code, _, _, e = ask(door_port, "POST", "/finch/api/echo", me, body={},
                            headers={"X-Assistant-Voice": "1", "Origin": "http://127.0.0.1:" + str(door_port)})
        check("and the page's own guard headers, for the voice routes",
              echoed(e, "X-Assistant-Voice") == "1"
              and echoed(e, "Origin") == "http://127.0.0.1:" + str(door_port), e)
        code, _, _, e = ask(door_port, "GET", "/finch/api/echo", headers={"X-Assistant-Who": "evil"})
        check("while a browser nobody knows cannot say who it is",
              echoed(e, "X-Assistant-Who") is None, echoed(e, "X-Assistant-Who"))
        code, _, _, e = ask(door_port, "POST", "/finch/api/echo", me, body={"said": "x" * 5000})
        check("a body goes through whole", code == 200 and e["method"] == "POST"
              and e["len"] == len(json.dumps({"said": "x" * 5000})), e)
        code, _, got, _ = ask(door_port, "GET", "/finch/api/redirect", me)
        check("a room's redirect stays inside its own place",
              code == 302 and got.get("location") == "/finch/somewhere", got)
        code, raw, _, _ = ask(door_port, "GET", "/finch/api/stream", me)
        check("an answer with no length is passed on to its end", code == 200 and raw == b"one two three", raw)
        code, _, _, _ = ask(door_port, "GET", "/nobody/", me)
        check("an assistant nobody keeps is not found", code == 404, code)
        code, _, _, e = ask(door_port, "GET", "/heron/api/echo", me)
        check("a person known by one assistant's key is vouched for at another's",
              code == 200 and echoed(e, "X-Assistant-Who") == home.OWNER, e)

        # -- stop, start, busy -------------------------------------------------------------
        pid = H.look()["pid"]
        code, _, _, got = ask(door_port, "POST", "/_/api/stop", me, body={"slug": "heron"})
        check("stop", code == 200 and got.get("said") == "stopped", got)
        check("and it is down, and written down", H.mode == "stopped" and homes.stopped(heron)
              and H.proc.poll() is not None, (H.mode, homes.stopped(heron)))
        code, raw, _, _ = ask(door_port, "GET", "/heron/", me)
        check("its address shows the manager's page standing in for it",
              code == 200 and b'data-focus="heron"' in raw, code)
        code, _, _, got = ask(door_port, "GET", "/heron/api/progress", me)
        check("and its API says it is not there", code == 503 and "stopped" in (got or {}).get("error", ""), got)
        check("a stopped one is not watched back to life", not until(lambda: H.look()["phase"] != "stopped", 4.5))
        code, _, _, got = ask(door_port, "POST", "/_/api/start", me, body={"slug": "heron"})
        check("start", code == 200 and got.get("said") == "starting", got)
        check("and it wakes, no longer written down as stopped",
              until(lambda: H.look()["phase"] == "awake") and not homes.stopped(heron)
              and H.look()["pid"] != pid, H.look())
        (flags / "heron").mkdir(parents=True, exist_ok=True)
        (flags / "heron" / "busy").write_text("", encoding="utf-8")
        until(lambda: H.look()["phase"] == "thinking", 6)
        code, _, _, got = ask(door_port, "POST", "/_/api/stop", me, body={"slug": "heron"})
        check("in the middle of a turn it asks first", code == 200 and got.get("busy") is True
              and H.mode == "ours", got)
        code, _, _, got = ask(door_port, "POST", "/_/api/stop", me, body={"slug": "heron", "anyway": True})
        check("and stops when told to anyway", got.get("said") == "stopped", got)
        (flags / "heron" / "busy").unlink()
        ask(door_port, "POST", "/_/api/start", me, body={"slug": "heron"})
        check("started again", until(lambda: H.look()["phase"] == "awake"), H.look())

        # -- falling over, and its own restart ---------------------------------------------
        pid = H.look()["pid"]
        ask(door_port, "GET", "/heron/api/crash", me)
        check("a room that falls over is brought back",
              until(lambda: H.look()["phase"] == "awake" and H.look()["pid"] != pid, 25), H.look())
        check("and the fall is counted", len(H.strikes.falls) == 1, list(H.strikes.falls))
        pid = H.look()["pid"]
        ask(door_port, "GET", "/heron/api/exit7", me)
        check("its own restart brings it back",
              until(lambda: H.look()["phase"] == "awake" and H.look()["pid"] != pid, 25), H.look())
        check("and forgives the falls", len(H.strikes.falls) == 0, list(H.strikes.falls))
        pid = H.look()["pid"]
        code, _, _, got = ask(door_port, "POST", "/_/api/restart", me, body={"slug": "heron"})
        check("restart from the page", got.get("said") == "restarting"
              and until(lambda: H.look()["phase"] == "awake" and H.look()["pid"] != pid, 25), (got, H.look()))

        # -- someone who keeps none of them ------------------------------------------------
        guest = "g" * 64
        (finch / "data" / "people" / "lee.token").write_text(guest, encoding="utf-8")
        them = "finch_who=" + guest
        code, _, got, _ = ask(door_port, "GET", "/", them)
        check("a guest paired with one goes straight into that room",
              code == 302 and got.get("location") == "/finch/", (code, got))
        code, _, _, listing = ask(door_port, "GET", "/_/api/list", them)
        check("sees only the one they are paired with, and no controls",
              [a["slug"] for a in listing["assistants"]] == ["finch"] and not listing["owner"]
              and not listing["assistants"][0]["control"] and not listing["assistants"][0]["home"], listing)
        code, _, _, got = ask(door_port, "POST", "/_/api/stop", them, body={"slug": "finch"})
        check("and cannot stop it", code == 403 and F.mode == "ours", (code, got))
        code, _, _, got = ask(door_port, "POST", "/_/api/new", them, body={"name": "Gull", "folder": str(scratch / "Gull")})
        check("or make one", code == 403 and not (scratch / "Gull").exists(), (code, got))
        code, _, _, e = ask(door_port, "GET", "/finch/api/echo", them)
        check("their knocks are vouched as theirs", echoed(e, "X-Assistant-Who") == "lee", e)
        code, _, _, _ = ask(door_port, "GET", "/heron/", them)
        check("and an assistant they are not paired with shows them nothing", code in (200, 403), code)

        # -- from the road, and from outside ---------------------------------------------
        PRETEND["addr"] = "8.8.8.8"
        code, _, _, _ = ask(door_port, "GET", "/", me)
        check("a knock from outside the tailnet gets nothing, key or not", code == 403, code)
        code, _, _, _ = ask(door_port, "GET", "/finch/api/echo", me)
        check("and is not passed on", code == 403, code)
        PRETEND["addr"] = "100.64.0.9"
        code, _, _, listing = ask(door_port, "GET", "/_/api/list", me)
        check("from a paired phone on the tailnet, the list", code == 200 and not listing["desk"]
              and all(not a["home"] for a in listing["assistants"]), listing)
        code, _, _, got = ask(door_port, "POST", "/_/api/browse", me, body={})
        check("but no folder dialog on the desk's screen", code == 403, (code, got))
        code, _, _, e = ask(door_port, "GET", "/finch/api/echo", me)
        check("and the room is told it came from the road", echoed(e, "X-Assistant-Peer") == "100.64.0.9", e)
        PRETEND["addr"] = None

        # -- one started elsewhere, one on the manager's port --------------------------------
        dove = Path(homes.create("Dove", str(scratch / "Dove"))["home"])
        dove_port = homes.identity(dove)["port"]
        outside = subprocess.Popen([sys.executable, "-u", str(stand_in), str(dove_port), str(flags / "dove")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until(lambda: homes.listening(dove_port), 10)
        D = keeper.add(dove)
        check("a room already up is watched, not doubled",
              until(lambda: D.mode == "theirs" and D.look()["phase"] == "adopted", 8), (D.mode, D.look()))
        code, raw, _, _ = ask(door_port, "GET", "/dove/", me)
        check("and is not passed on to -- it has no key of ours",
              code == 200 and b'data-focus="dove"' in raw, code)
        code, _, _, got = ask(door_port, "POST", "/_/api/stop", me, body={"slug": "dove"})
        check("and cannot be stopped from here, in words", "another window" in got.get("said", ""), got)
        outside.kill()
        outside.wait()
        check("when it goes, the manager takes it over",
              until(lambda: D.mode == "ours" and D.look()["phase"] == "awake", 25), (D.mode, D.look()))

        owl = Path(homes.create("Owl", str(scratch / "Owl"))["home"])
        ident = homes.identity(owl)
        ident["port"] = door_port
        (owl / "identity.json").write_text(json.dumps(ident), encoding="utf-8")
        O = keeper.add(owl)
        check("one that asks for the manager's own port stays down, saying why",
              until(lambda: O.look()["phase"] == "held", 8) and "manager's own" in O.look()["detail"], O.look())

        # -- a new one, from the page ---------------------------------------------------
        code, _, _, got = ask(door_port, "POST", "/_/api/new", me, body={"name": "Cy", "folder": str(scratch / "Cy")})
        C = keeper.by_slug("cy")
        check("a new one from the page", code == 200 and got.get("url") == "/cy/" and C is not None, (code, got))
        check("kept, keyed, and woken", C is not None and homes.owner_key(scratch / "Cy")
              and until(lambda: C.look()["phase"] == "awake"), C and C.look())
        code, _, _, got = ask(door_port, "POST", "/_/api/new", me, body={"name": "Cy", "folder": str(scratch / "Cy2")})
        check("a second of the same name is refused in words", code == 400 and "already answers" in got.get("error", ""), got)

        # -- detached, moved, attached again ----------------------------------------------
        until(lambda: H.look()["phase"] == "awake")
        (flags / "heron" / "busy").write_text("", encoding="utf-8")
        until(lambda: H.look()["phase"] == "thinking", 6)
        code, _, _, got = ask(door_port, "POST", "/_/api/detach", me, body={"slug": "heron"})
        check("detaching in the middle of a turn asks first", got.get("busy") is True and H.mode == "ours", got)
        code, _, _, got = ask(door_port, "POST", "/_/api/detach", me, body={"slug": "heron", "anyway": True})
        (flags / "heron" / "busy").unlink()
        check("detach", code == 200 and got.get("said") == "detached" and got.get("home") == str(heron), got)
        check("it is stopped and forgotten", keeper.by_slug("heron") is None and H.mode == "gone"
              and H.proc.poll() is not None
              and str(heron) not in json.loads(homes.REGISTRY.read_text(encoding="utf-8"))["homes"], H.mode)
        code, _, _, _ = ask(door_port, "GET", "/heron/", me)
        check("and its address is nobody's now", code == 404, code)
        check("its folder is left as it was", (heron / "identity.json").is_file() and (heron / "data").is_dir())
        moved = scratch / "moved" / "Heron"
        moved.parent.mkdir()
        try:
            heron.rename(moved)
            went = True
        except OSError as exc:
            went = exc
        check("and the manager has let go of it: the folder can be moved", went is True, went)
        code, _, _, got = ask(door_port, "POST", "/_/api/attach", me, body={"folder": str(moved)})
        H2 = keeper.by_slug("heron")
        check("attached again from where it went", code == 200 and got.get("url") == "/heron/"
              and H2 is not None and H2.home == moved.resolve(), (code, got))
        check("and it wakes there", H2 is not None and until(lambda: H2.look()["phase"] == "awake"), H2 and H2.look())
        code, _, _, e = ask(door_port, "GET", "/heron/api/echo", me)
        check("at the same address as before", code == 200 and e["path"] == "/api/echo", code)
        code, _, _, got = ask(door_port, "POST", "/_/api/attach", me, body={"folder": str(scratch / "flags")})
        check("a folder with no assistant in it is refused in words",
              code == 400 and "identity.json" in got.get("error", ""), got)
        code, _, _, got = ask(door_port, "POST", "/_/api/detach", them, body={"slug": "heron"})
        check("a guest cannot detach one", code == 403 and keeper.by_slug("heron") is not None, (code, got))
        code, _, _, got = ask(door_port, "POST", "/_/api/attach", them, body={"folder": str(moved)})
        check("or attach one", code == 403, (code, got))
        real_pointer = homes._pointer
        homes._pointer = lambda: finch
        try:
            code, _, _, listing = ask(door_port, "GET", "/_/api/list", me)
            first = {a["slug"]: a["first"] for a in listing["assistants"]}
            check("the install's own home is marked, so the page offers no Detach",
                  first.get("finch") is True and first.get("heron") is False, first)
            code, _, _, got = ask(door_port, "POST", "/_/api/detach", me, body={"slug": "finch"})
            check("and it is not detached, in words", code == 400 and "stays" in got.get("error", "")
                  and keeper.by_slug("finch") is F and F.mode == "ours", (code, got))
        finally:
            homes._pointer = real_pointer

        # -- the marks the scripts read --------------------------------------------------
        mark = json.loads((finch / "data" / "tray.json").read_text(encoding="utf-8"))
        check("each home says who is keeping it", mark.get("tray") == os.getpid()
              and mark.get("manager") == door_port and mark.get("port") == F.port, mark)
    finally:
        keeper.quit_all()
        door.server_close()

    check("quitting puts every room down", all(a.proc is None or a.proc.poll() is not None
                                               for a in keeper.all()), [a.look() for a in keeper.all()])
    check("and takes the marks away", not (finch / "data" / "tray.json").exists())
    check("without writing anyone down as stopped", json.loads(homes.REGISTRY.read_text(encoding="utf-8")).get("stopped") == [])

    # -- the room's side: the real door, over the bench identity --------------------------
    people.TOKEN_DIR = scratch / "room-people"
    sam = people.mint(home.OWNER)
    K = "k" * 64
    os.environ["ASSISTANT_MANAGER_KEY"] = K
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    rp = room.server_port
    threading.Thread(target=room.serve_forever, daemon=True).start()
    slug = "/" + home.SLUG

    def said(peer="127.0.0.1", who=home.OWNER, key=K, prefix=slug):
        out = {"X-Assistant-Manager": key, "X-Assistant-Peer": peer, "X-Assistant-Prefix": prefix}
        if who:
            out["X-Assistant-Who"] = who
        return out

    try:
        code, _, _, _ = ask(rp, "GET", "/api/progress", home.COOKIE + "=" + sam)
        check("room: reached directly with its own key, as ever", code == 200, code)
        code, _, _, _ = ask(rp, "GET", "/api/progress", headers=said(peer="100.64.0.5"))
        check("room: through the manager, the person it vouches for is let in", code == 200, code)
        code, _, _, _ = ask(rp, "GET", "/api/progress", headers=said(peer="8.8.8.8"))
        check("room: but not from outside the tailnet", code == 403, code)
        code, _, _, _ = ask(rp, "GET", "/api/progress", home.COOKIE + "=" + sam, headers=said(key="x" * 64))
        check("room: a wrong manager key shuts the door, whatever else is carried", code == 403, code)
        code, _, _, _ = ask(rp, "GET", "/api/progress", headers=said(who="stranger"))
        check("room: someone not paired here is not let in on another room's word", code == 403, code)
        code, _, _, _ = ask(rp, "GET", "/api/progress", home.COOKIE + "=" + sam, headers=said(prefix="/elsewhere"))
        check("room: a knock meant for another assistant is refused", code == 403, code)
        os.environ.pop("ASSISTANT_MANAGER_KEY")
        code, _, _, _ = ask(rp, "GET", "/api/progress", home.COOKIE + "=" + sam, headers=said())
        check("room: a room with no manager's key refuses anything claiming one", code == 403, code)
        os.environ["ASSISTANT_MANAGER_KEY"] = K
        code, _, got, _ = ask(rp, "GET", "/?k=" + sam, headers=said(who=""))
        check("room: pairing through the manager redirects bare -- the manager adds the place",
              code == 302 and got.get("location") == "/", (code, got))
        # The real room behind a real door: the final target must carry the
        # slug once, not twice.
        class Lodger:
            mode, port, slug, home = "ours", rp, home.SLUG, None

            def look(self):
                return {"phase": "awake"}

        class Lodgings:
            def by_slug(self, s):
                return Lodger() if s == home.SLUG else None

            def all(self):
                return []

        front = manager.Door(("127.0.0.1", 0), manager.DoorHandler)
        front.keeper = Lodgings()
        threading.Thread(target=front.serve_forever, daemon=True).start()
        os.environ["ASSISTANT_MANAGER_KEY"] = manager.KEY  # the key a real door sends
        try:
            code, _, got, _ = ask(front.server_port, "GET", slug + "/?k=" + sam)
            check("room: pairing through a real door lands on its place once, not doubled",
                  code == 302 and got.get("location") == slug + "/"
                  and got.get("set-cookie", "").startswith(home.COOKIE + "=" + sam), (code, got))
        finally:
            os.environ["ASSISTANT_MANAGER_KEY"] = K
            front.shutdown()
            front.server_close()
        code, raw, _, _ = ask(rp, "GET", "/", headers=said())
        text = raw.decode("utf-8", "replace")
        check("room: the page knows it sits behind the manager",
              code == 200 and '"managed": true' in text and '"base": "' + slug + '/"' in text, code)
        check("room: and asks for everything relative to where it is",
              'src="app.js"' in text and 'href="style.css"' in text and 'src="/app.js"' not in text)
        code, raw, _, _ = ask(rp, "GET", "/manifest.webmanifest", headers=said())
        man = json.loads(raw.decode("utf-8"))
        check("room: its phone app starts at its own place",
              man.get("start_url") == slug + "/" and man.get("scope") == slug + "/", man)
        code, raw, _, _ = ask(rp, "GET", "/", home.COOKIE + "=" + sam)
        check("room: reached directly, it knows it is not",
              '"managed": false' in raw.decode("utf-8", "replace"))
        code, _, _, got = ask(rp, "GET", "/api/pair", headers=said())
        check("room: its pairing line is the manager's address and its own place",
              code == 200 and (":" + str(homes.front()[1]) + slug + "/?k=") in got.get("template", ""), got)
        code, _, _, _ = ask(rp, "GET", "/api/pair", headers=said(peer="100.64.0.5"))
        check("room: and is still only ever read at the desk", code == 403, code)
    finally:
        room.shutdown()

    print()
    if FAILED:
        print(str(len(FAILED)) + " failed")
        sys.exit(1)
    print("the manager holds")


if __name__ == "__main__":
    main()
