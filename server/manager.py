"""The Digital Assistant Manager: one icon, one address, every assistant.

Every assistant is still its own room -- its own process, store, port, log and
nights -- but none of them faces the network any more. The manager:

- keeps each room the way the icon in the corner used to keep one: started
  hidden, brought back when it falls over, and left down when someone stops
  it -- still down after a reboot, until someone starts it again. A stopped
  assistant does not dream, run its jobs, or answer;
- answers on one port for all of them: `/` is the list, `/<slug>/` is that
  assistant's room, passed through to it on loopback. One address for a phone
  to know;
- is the door to all of it, with the room's own two walls -- this machine or
  the owner's tailnet, and a paired person's key -- and tells the room behind
  it who really knocked (`Handler._manager_said` in app.py).

    pythonw start.pyw [--open]     # what the shortcuts run: the icon and the door
    python -m server.manager       # the same, saying everything in a console
    python -m server.manager --port 8797 --no-icon   # a side one, for looking

The rooms it starts are handed `ASSISTANT_MANAGER_KEY`, a fresh secret for
every run of the manager, and it is the only thing that makes a room believe
the headers above. A room started some other way cannot be reached through
the manager at all, which is the safe way round.
"""

import argparse
import ctypes
import http.client
import http.cookies
import ipaddress
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from ctypes import wintypes
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import home, homes, people, tray

APP = "Digital Assistant Manager"
LOCK = "DigitalAssistantManager"
CODE = home.CODE
WEB = CODE / "web"
ART = CODE / "artwork" / "manager"
# Beside the list it keeps: a side manager with a list of its own (a bench)
# writes its story there, not into the install's.
LOGS = (homes.REGISTRY.parent / "logs") if os.environ.get("ASSISTANT_REGISTRY") else CODE / "logs"
KEEP_LOGS_FOR = timedelta(days=14)

# The secret the rooms are handed, and the only proof that a request passed
# on to one of them came from here. New every run.
KEY = secrets.token_hex(32)

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Long enough for the slowest honest answer a room gives (a backup, an
# upload); a room that is gone shows up as a refused connection, not a wait.
UPSTREAM_TIMEOUT = 900
BODY_MAX = 64 * 1024 * 1024
API_BODY_MAX = 1024 * 1024
COOKIE_AGE = 365 * 24 * 3600
# Headers that describe one connection rather than the message, never passed on.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade"}

# The words the icon and the page use for where an assistant is.
WORDS = {
    "waking": "waking up",
    "awake": "awake",
    "thinking": "thinking",
    "adopted": "awake, started elsewhere",
    "down": "down, coming back",
    "held": "staying down",
    "stopped": "stopped",
    "elsewhere": "kept by its own icon",
}

kernel32 = tray.kernel32
user32 = tray.user32
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]

_LOG_LOCK = threading.Lock()


def mlog(line: str) -> None:
    """The manager's own story, beside the code: what it started, stopped
    and was refused. Each room still talks into its own home's logs/."""
    try:
        LOGS.mkdir(exist_ok=True)
        with _LOG_LOCK, open(LOGS / ("manager-" + date.today().isoformat() + ".log"),
                             "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except OSError:
        pass


def prune_logs():
    cutoff = datetime.now() - KEEP_LOGS_FOR
    for old in LOGS.glob("manager-*.log"):
        try:
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
        except OSError:
            pass


def _same(a, b) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


# ---------------------------------------------------------------------------
# One room, kept
# ---------------------------------------------------------------------------

class Assistant:
    """One assistant's room: started, watched, brought back, or left down.

    The rule is the icon's rule, per assistant: **the room comes back unless
    somebody asked it not to.** Its own restart, a crash, and the manager's
    Restart are one path; Stop is the only way down that stays down, and it
    is written to homes.json so it outlives the manager and the machine.

    Modes, named out loud: `ours` is a child of ours, up or starting.
    `theirs` is a room started some other way, watched and never touched.
    `elsewhere` is an assistant an old icon of its own still holds. `held`
    has stopped being brought back, for a reason it can name. `stopped` was
    put down on purpose."""

    def __init__(self, keeper, folder):
        self.keeper = keeper
        self.home = Path(folder).resolve()
        self.orders = queue.Queue()
        self._lock = threading.Lock()
        self.state = {"phase": "waking", "since": time.time(), "detail": "", "pid": None}
        self.answered = threading.Event()
        self.proc = None
        self.started = 0.0
        self.log = None
        self.icon_lock = None
        self.mode = "new"
        self.misses = 0
        self.thread = None
        # Its own job, kept across its restarts: a hand it sent is not cut
        # off by a restart, and everything of it goes when it is stopped --
        # or when the manager itself goes, kill-on-close.
        self.job = tray.keep_the_room_with_me()
        self.reload()
        self.strikes = tray.Strikes(self.name)

    def reload(self):
        ident = homes.identity(self.home)
        self.name = str(ident.get("name") or self.home.name)
        self.title = str(ident.get("title") or self.name)
        self.slug = str(ident.get("slug") or "")
        try:
            self.port = int(ident.get("port") or 0)
        except (TypeError, ValueError):
            self.port = 0
        self.owner = str(ident.get("owner") or "").lower()
        self.icon_name = str(ident.get("tray_lock") or ("AssistantTray-" + self.slug))

    # -- what everyone can see ------------------------------------------------

    def look(self) -> dict:
        with self._lock:
            return dict(self.state)

    def set(self, **bits):
        with self._lock:
            before = (self.state["phase"], self.state["detail"], self.state["pid"])
            if "phase" in bits and bits["phase"] != self.state["phase"]:
                self.state["since"] = time.time()
            self.state.update(bits)
            after = (self.state["phase"], self.state["detail"], self.state["pid"])
        if before != after:
            self.leave_mark()
            self.keeper.changed()

    def word(self) -> str:
        return WORDS.get(self.look()["phase"], self.look()["phase"])

    # -- where it talks -----------------------------------------------------------

    def open_log(self):
        logs = self.home / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / ("room-" + date.today().isoformat() + ".log")
        if self.log is None or Path(self.log.name) != path:
            if self.log:
                self.log.close()
            self.log = open(path, "a", encoding="utf-8", errors="replace")
        return self.log

    def log_path(self) -> Path:
        return self.home / "logs" / ("room-" + date.today().isoformat() + ".log")

    def note(self, line):
        """A line in the room's own log, so it reads as one story, and in the
        manager's, so the manager's does too."""
        try:
            log = self.open_log()
            log.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
            log.flush()
        except OSError:
            pass
        mlog(self.name + ": " + line)

    def leave_mark(self):
        """data/tray.json, as the icon used to leave it: `restart.ps1` reads
        it to learn that somebody is watching the room and will bring it
        back. Not for an assistant an old icon of its own still holds."""
        if self.mode == "elsewhere":
            return
        here = self.look()
        try:
            (self.home / "data").mkdir(exist_ok=True)
            (self.home / "data" / "tray.json").write_text(json.dumps({
                "tray": os.getpid(),
                "manager": self.keeper.port,
                "room": here["pid"],
                "phase": here["phase"],
                "since": datetime.fromtimestamp(here["since"]).isoformat(timespec="seconds"),
                "log": str(self.log_path()),
                "port": self.port,
            }, indent=2), encoding="utf-8")
        except OSError:
            pass

    def clear_mark(self):
        if self.mode == "elsewhere":
            return
        try:
            (self.home / "data" / "tray.json").unlink()
        except OSError:
            pass

    # -- the old icon's lock ---------------------------------------------------------

    def claim(self) -> bool:
        """Hold the lock its own icon used to hold, so an old one started by
        hand cannot come up beside the manager -- and so the manager stays
        off an assistant an old icon is still holding."""
        if self.icon_lock:
            return True
        handle = kernel32.CreateMutexW(None, False, "Local\\" + self.icon_name)
        if ctypes.get_last_error() == tray.ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            return False
        self.icon_lock = handle
        return True

    def release(self):
        if self.icon_lock:
            kernel32.CloseHandle(self.icon_lock)
            self.icon_lock = None

    def let_go(self):
        """Everything of its folder the manager holds open -- the room's log,
        its job -- so the folder can be moved once it is detached."""
        if self.log:
            try:
                self.log.close()
            except OSError:
                pass
            self.log = None
        if self.job:
            kernel32.CloseHandle(self.job)
            self.job = None

    # -- the room ---------------------------------------------------------------

    def command(self):
        """What the room is started with. A method because it is the one thing
        a test replaces: a side room over a scratch home, handler only."""
        return [tray.child_python(), "-u", "-m", "server.app"]

    def env(self) -> dict:
        env = dict(os.environ)
        for k in ("ASSISTANT_HOME", "ASSISTANT_PORT", "ASSISTANT_BIND",
                  "ASSISTANT_SUPERVISED", "ASSISTANT_MANAGER_KEY"):
            env.pop(k, None)
        env.update({
            "ASSISTANT_HOME": str(self.home),
            # Its own restart is an exit the manager reads as meant.
            "ASSISTANT_SUPERVISED": "1",
            # Behind the manager, on loopback, whatever identity.json says.
            "ASSISTANT_BIND": "127.0.0.1",
            "ASSISTANT_MANAGER_KEY": KEY,
            # A room writes em-dashes down a handle; nothing negotiates an
            # encoding for a handle, so it is said here.
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        })
        return env

    def launch(self):
        self.answered.clear()
        log = self.open_log()
        self.note("the manager is starting " + self.name)
        # CREATE_NO_WINDOW carries to everything the room starts: git, the
        # claude binary, every hand. See tray.Room.start for the long story.
        self.proc = subprocess.Popen(
            self.command(), cwd=str(CODE), env=self.env(),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=tray.CREATE_NO_WINDOW)
        self.started = time.time()
        tray.put_in_job(self.job, self.proc.pid)
        self.set(phase="waking", pid=self.proc.pid, detail="")

    def lived(self) -> float:
        return time.time() - self.started

    def end_room(self, why):
        """The room only. A hand it sent carries on and reports to the room
        that comes back -- this is a restart."""
        if not self.proc or self.proc.poll() is not None:
            return
        self.note(why)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def end_everything(self, why):
        """The room and everything it started. This is a stop: nothing of it
        goes on working while it is down."""
        if self.proc and self.proc.poll() is None:
            self.note(why)
        if self.job:
            kernel32.TerminateJobObject(self.job, 1)
        if self.proc:
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def ask(self, route="/api/progress", timeout=2.0):
        """The room's own answer, asked on loopback with its owner's key."""
        if not self.port:
            return None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}",
            headers={"Cookie": self.slug + "_who=" + homes.owner_key(self.home)})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as reply:
                return json.loads(reply.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError):
            return None

    def busy(self) -> bool:
        now = self.ask()
        return bool(now and now.get("busy"))

    def misfit(self) -> str:
        """What in its identity keeps it from being started at all, or ""."""
        if not self.slug or not self.port:
            return "its identity.json names no slug or no port"
        if self.port == self.keeper.port:
            return (f"it asks for port {self.port}, which is the manager's own. "
                    "Give it another in its identity.json, then start it")
        if self.keeper.duplicate(self):
            return "another assistant here answers to the same name"
        return ""

    def hold(self, why):
        self.set(phase="held", pid=None, detail=why)
        self.note("not starting: " + why)
        self.keeper.say(self.name + " is staying down", why + ".", "bad")

    def ready_to_launch(self) -> bool:
        """Whether it can be started now; if not, held, with the reason."""
        self.reload()
        why = self.misfit()
        if not why and not self.claim():
            why = "its own icon in the corner still holds it; put that one down first"
        if not why and not self.port_comes_free():
            why = f"something else is holding port {self.port}"
        if why:
            self.hold(why)
            return False
        return True

    def port_comes_free(self, seconds=20.0) -> bool:
        until = time.time() + seconds
        while time.time() < until:
            if not homes.listening(self.port):
                return True
            time.sleep(0.25)
        return not homes.listening(self.port)

    # -- the keeping -------------------------------------------------------------

    def order(self, do, wait=30.0, by="") -> str:
        """Ask the keeper thread for something and wait for its word."""
        reply = queue.Queue()
        self.orders.put({"do": do, "reply": reply, "by": by})
        try:
            return reply.get(timeout=wait)
        except queue.Empty:
            return "asked; still working on it"

    def first_mode(self) -> str:
        if homes.stopped(self.home):
            self.set(phase="stopped", pid=None, detail="")
            return "stopped"
        # Before anything is asked of its port: a port that is the manager's
        # own would answer, and be taken for a room already awake.
        why = self.misfit()
        if why:
            self.hold(why)
            return "held"
        if not self.claim():
            self.set(phase="elsewhere", pid=None,
                     detail="its own icon in the corner still holds it")
            return "elsewhere"
        if self.port and homes.listening(self.port):
            self.set(phase="adopted",
                     detail="it was already awake when the manager arrived")
            return "theirs"
        if not self.ready_to_launch():
            return "held"
        self.launch()
        return "ours"

    def obey(self, order, mode):
        """(new mode, go and start it now, the word for whoever asked)."""
        do, by = order.get("do"), order.get("by") or "someone"
        if do == "start" or (do == "restart" and mode in ("stopped", "held")):
            if mode in ("stopped", "held"):
                homes.set_stopped(self.home, False)
                self.strikes.forgive()
                self.note(f"started at {by}'s word")
                return "ours", True, "starting"
            if mode == "elsewhere":
                return mode, False, "its own icon in the corner holds it; put that one down first"
            return mode, False, "it is already up"
        if do == "stop":
            if mode == "ours":
                self.set(phase="stopped", detail="stopping")
                self.end_everything(f"stopped at {by}'s word")
                homes.set_stopped(self.home, True)
                self.set(phase="stopped", pid=None, detail="")
                return "stopped", False, "stopped"
            if mode == "held":
                homes.set_stopped(self.home, True)
                self.set(phase="stopped", pid=None, detail="")
                return "stopped", False, "stopped"
            if mode == "stopped":
                return mode, False, "it is already stopped"
            if mode == "theirs":
                return mode, False, ("it was started in another window, not by the "
                                     "manager; close that window to stop it")
            return mode, False, "its own icon in the corner holds it; put it down from there"
        if do == "restart":
            if mode == "ours":
                self.set(phase="waking", detail="restarting, at " + by + "'s word")
                self.end_room(f"restarted at {by}'s word")
                self.strikes.forgive()
                return "ours", True, "restarting"
            return mode, False, "only a room the manager started can be restarted from it"
        return mode, False, "no such thing"

    def supervise(self):
        mode = self.first_mode()
        while True:
            self.mode = mode
            ended = False    # our room stopped, and nobody asked it to
            go = False       # start it now

            while not (ended or go):
                if mode == "ours" and self.proc and self.proc.poll() is not None:
                    ended = True
                    break
                if mode == "theirs" and not homes.listening(self.port):
                    self.note("the room started elsewhere has stopped; the manager is taking over")
                    mode, go = "ours", True
                    break
                if mode == "elsewhere" and self.claim():
                    self.note("its own icon has let go; the manager is taking over")
                    mode, go = "ours", True
                    if self.port and homes.listening(self.port):
                        mode, go = "theirs", False
                        self.set(phase="adopted", detail="started elsewhere")
                    break
                try:
                    order = self.orders.get(timeout=1.0)
                except queue.Empty:
                    continue
                if order.get("do") in ("quit", "detach"):
                    detaching = order["do"] == "detach"
                    if mode == "ours":
                        self.end_everything(
                            ("detached at " + (order.get("by") or "someone") + "'s word; "
                             "the manager lets go of this folder") if detaching
                            else "the manager is closing, and the room with it")
                    elif detaching:
                        self.note("detached at " + (order.get("by") or "someone") + "'s word")
                    self.set(phase="down", pid=None,
                             detail="detached" if detaching else "the manager has closed")
                    self.clear_mark()
                    self.release()
                    if detaching:
                        self.let_go()
                    self.mode = "gone" if detaching else mode
                    if order.get("reply"):
                        order["reply"].put("detached" if detaching else "down")
                    return
                mode, go, said = self.obey(order, mode)
                self.mode = mode
                if order.get("reply"):
                    order["reply"].put(said)

            if ended:
                code = self.proc.poll()
                lived = self.lived()
                if code == tray.SHE_ASKED:
                    self.note(f"{self.name} asked for this one -- up for {lived:.0f}s")
                    self.strikes.forgive()
                else:
                    answered = self.answered.is_set()
                    self.note(f"{self.name} stopped with {code} after {lived:.0f}s"
                              + ("" if answered else ", never having opened"))
                    why = self.strikes.death(answered)
                    if why:
                        mode = "held"
                        self.set(phase="held", pid=None, detail=why)
                        self.keeper.say(self.name + " is staying down",
                                        why + ", so the manager has stopped bringing it "
                                        "back. Its log says what it said on the way out.", "bad")
                        continue
                    self.set(phase="down", pid=None, detail="stopped; bringing it back")
                    self.keeper.say(self.name + " stopped",
                                    "Bringing it back." if answered else
                                    "It did not get as far as opening its room. Trying again.",
                                    "warn")
                mode = "ours"

            if mode != "ours":
                continue
            if not self.ready_to_launch():
                mode = "held"
                continue
            self.launch()


# ---------------------------------------------------------------------------
# All of them
# ---------------------------------------------------------------------------

class Keeper:
    def __init__(self, bind, port):
        self.bind, self.port = bind, port
        self._lock = threading.Lock()
        self.assistants = []
        self.balloons = deque()
        self.hwnd = None
        self.door = None
        self.quitting = False

    def load(self):
        for folder in homes.known():
            self.add(folder)

    def add(self, folder) -> Assistant:
        with self._lock:
            for a in self.assistants:
                if _same(a.home, folder):
                    return a
            a = Assistant(self, folder)
            self.assistants.append(a)
        # Its owner's key, if the room has never been up to make it: the
        # manager knows people by their keys, and a browser opened on a room
        # still waking must be let in to wait for it.
        homes.mint_owner_key(a.home)
        mlog("keeping " + a.name + " (" + str(a.home) + ", port " + str(a.port) + ")")
        a.thread = threading.Thread(target=a.supervise, daemon=True, name="keep-" + a.slug)
        a.thread.start()
        return a

    def all(self) -> list:
        with self._lock:
            return list(self.assistants)

    def duplicate(self, a) -> bool:
        """Whether an assistant kept before this one answers to its name."""
        for b in self.all():
            if b is a:
                return False
            if b.slug == a.slug:
                return True
        return False

    def by_slug(self, slug):
        for a in self.all():
            if a.slug == slug:
                return a
        return None

    # -- the icon ----------------------------------------------------------------

    def ring(self, msg):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, msg, 0, 0)

    def changed(self):
        self.ring(tray.TOOLTIP_MSG)

    def say(self, title, text, kind="info"):
        mlog(title + " -- " + text)
        self.balloons.append((title, text, kind))
        self.ring(tray.BALLOON_MSG)

    def summary(self) -> str:
        parts = [a.name + ": " + a.word() for a in self.all()]
        return (APP + ("\n" + " · ".join(parts) if parts else "\nno assistants yet"))[:127]

    def all_down(self) -> bool:
        return all(a.look()["phase"] in ("stopped", "held", "down") for a in self.all())

    # -- the watching -----------------------------------------------------------------

    def watch(self):
        """What each room says about itself, every two seconds. Asked rather
        than guessed at, so `awake` means the room answered, not that a
        process existed."""
        while not self.quitting:
            time.sleep(2.0)
            for a in self.all():
                if a.mode not in ("ours", "theirs"):
                    continue
                here = a.look()
                if here["phase"] in ("stopped", "held"):
                    continue
                # A room deep in a turn or loading a model can take a few
                # seconds to say so; one slow answer is not a room that left.
                now = a.ask(timeout=4.0)
                if now is None:
                    a.misses += 1
                    if a.misses >= 2 and here["phase"] in ("awake", "thinking", "adopted"):
                        a.set(phase="waking", detail="it stopped answering")
                    continue
                a.misses = 0
                a.answered.set()
                if now.get("busy"):
                    steps = now.get("steps") or []
                    last = steps[-1].get("text") if steps and isinstance(steps[-1], dict) else ""
                    a.set(phase="thinking", detail=(last or "thinking")[:60])
                elif a.mode == "theirs":
                    a.set(phase="adopted", detail="started in another window")
                else:
                    a.set(phase="awake", detail="")

    # -- acting, from the icon or the page -------------------------------------------

    def act(self, a, do, by, anyway=False) -> dict:
        """Start, stop or restart one. A stop or a restart in the middle of a
        turn asks first -- a turn cut off half way leaves a line nobody
        answered and nothing on the screen saying why."""
        if do in ("stop", "restart") and not anyway and a.mode == "ours" and a.busy():
            return {"busy": True, "name": a.name}
        said = a.order(do, by=by)
        return {"ok": True, "said": said}

    def detach(self, a, by) -> str:
        """Stop it, forget it, and let go of its folder. Nothing in the
        folder is touched; attaching it again, from wherever it is by then,
        puts it back as it was."""
        if homes.is_first(a.home):
            raise homes.Refused(a.name + " is this install's own home (home.json points at it), "
                                "so it stays. Detach the others.")
        said = a.order("detach", wait=45, by=by)
        with self._lock:
            if a in self.assistants:
                self.assistants.remove(a)
        homes.forget(a.home)
        mlog("detached " + a.name + " (" + str(a.home) + ") at " + by + "'s word")
        self.changed()
        return said

    def open_url(self, a=None) -> str:
        """The address that walks this machine's browser in, carrying the
        owner's key once; the room (or the manager) moves it into a cookie."""
        first = a or (self.all()[0] if self.all() else None)
        key = homes.owner_key(first.home) if first else ""
        where = ("/" + a.slug + "/") if a else "/"
        return f"http://localhost:{self.port}{where}" + ("?k=" + key if key else "")

    def quit_all(self):
        self.quitting = True
        waits = []
        for a in self.all():
            reply = queue.Queue()
            a.orders.put({"do": "quit", "reply": reply})
            waits.append(reply)
        for reply in waits:
            try:
                reply.get(timeout=30)
            except queue.Empty:
                pass
        if self.door:
            threading.Thread(target=self.door.shutdown, daemon=True).start()
        mlog("the manager is closing")
        self.ring(tray.QUIT_MSG)


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------

class Door(ThreadingHTTPServer):
    # Two doors on one port would each take half the knocks; the second one
    # dies on the bind instead. See OneRoom in app.py.
    allow_reuse_address = 0
    daemon_threads = True
    keeper = None


TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json; charset=utf-8",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
STATIC = {"/_/manager.js": WEB / "manager.js", "/_/manager.css": WEB / "manager.css",
          "/_/manifest.webmanifest": WEB / "manager.webmanifest",
          "/_/icon.ico": ART / "manager.ico", "/_/icon-192.png": ART / "icon-192.png",
          "/_/icon-512.png": ART / "icon-512.png"}


class DoorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    @property
    def keeper(self) -> Keeper:
        return self.server.keeper

    # -- small things ---------------------------------------------------------------

    def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _shut(self):
        self.close_connection = True
        self._send(403, b"")

    def _redirect(self, where, extra=()):
        self._send(302, b"", extra=(("Location", where),) + tuple(extra))

    @staticmethod
    def _address(addr):
        addr = str(addr or "").split("%")[0]
        if addr.lower().startswith("::ffff:"):
            addr = addr[7:]
        try:
            return ipaddress.ip_address(addr)
        except ValueError:
            return None

    def _ip(self):
        """The knocker, if it may knock at all: this machine, or a tailnet
        peer. Anything else gets nothing, the same as the rooms give it."""
        ip = self._address(self.client_address[0] if self.client_address else "")
        if ip is None or not (ip.is_loopback or people.in_tailnet(ip)):
            return None
        return ip

    def _desk(self, ip) -> bool:
        if ip.is_loopback:
            return True
        mine = people.my_tailnet_address()
        return bool(mine) and str(ip) == mine

    def _person(self) -> str:
        """Who is at this browser, by any assistant's key it carries. A
        person is the same person in every home on this machine by the name
        they are kept under -- the owner's name is copied into each new one."""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return ""
        for a in self.keeper.all():
            got = jar.get(a.slug + "_who") if a.slug else None
            if got:
                who = homes.whose(a.home, got.value)
                if who:
                    return who
        return ""

    def _visible(self, person) -> list:
        return [a for a in self.keeper.all() if person and person in homes.keys_of(a.home)]

    @staticmethod
    def _may(person, a) -> bool:
        return bool(person) and person == a.owner

    def _body(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self.close_connection = True
            self._json({"error": "that is more than the manager passes on"}, 413)
            return None
        return self.rfile.read(length) if length else b""

    # -- routing ------------------------------------------------------------------

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        ip = self._ip()
        if ip is None:
            return self._shut()
        url = urlparse(self.path)
        path = url.path
        if path == "/" or path.startswith("/_/"):
            return self._manager(ip, url)
        seg = path.split("/")[1]
        a = self.keeper.by_slug(seg) if SLUG.match(seg) else None
        if not a:
            return self._send(404, b"not here")
        if path == "/" + seg:
            return self._redirect("/" + seg + "/" + ("?" + url.query if url.query else ""))
        rest = path[len(seg) + 1:] + ("?" + url.query if url.query else "")
        return self._pass(ip, a, rest)

    # -- passing a knock on to a room -------------------------------------------------

    def _pass(self, ip, a, rest):
        body = self._body(BODY_MAX)
        if body is None:
            return
        if a.mode != "ours" or a.look()["phase"] not in ("awake", "thinking", "waking"):
            return self._resting(a, rest)
        headers = {}
        for k, v in self.headers.items():
            low = k.lower()
            if low in HOP or low.startswith("x-assistant-") or low == "content-length":
                continue
            headers[k] = v
        if body or self.headers.get("Content-Length") is not None:
            headers["Content-Length"] = str(len(body))
        headers.update({"X-Assistant-Manager": KEY, "X-Assistant-Peer": str(ip),
                        "X-Assistant-Prefix": "/" + a.slug, "Connection": "close"})
        who = self._person()
        if who:
            headers["X-Assistant-Who"] = who
        conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.request(self.command, rest, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except OSError:
            conn.close()
            return self._resting(a, rest)
        try:
            self._relay(resp, a)
        except OSError:
            self.close_connection = True
        finally:
            conn.close()

    def _relay(self, resp, a):
        self.send_response_only(resp.status, resp.reason)
        length = None
        for k, v in resp.getheaders():
            low = k.lower()
            if low in HOP:
                continue
            if low == "content-length":
                length = v
                continue
            if low == "location" and v.startswith("/") and not v.startswith("//"):
                v = "/" + a.slug + v
            self.send_header(k, v)
        bodiless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        if length is not None:
            self.send_header("Content-Length", length)
        elif not bodiless:
            # Read to the end and close: the room said how long it is by
            # stopping, so the browser is told the same way.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if bodiless:
            return
        read = getattr(resp, "read1", resp.read)
        while True:
            chunk = read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)

    def _resting(self, a, rest):
        """An assistant that is not answering: its page is the manager's
        page, standing on it, and everything else is a plain refusal the
        room's own page already knows how to wear."""
        # A key handed over while the room is still getting up is kept the
        # way the room would keep it, so the browser waits here paired.
        key = (parse_qs(urlparse(rest).query).get("k") or [""])[0].strip()
        if key and homes.whose(a.home, key):
            return self._redirect("/" + a.slug + "/", extra=(("Set-Cookie",
                a.slug + "_who=" + key + "; Path=/; Max-Age=" + str(COOKIE_AGE)
                + "; SameSite=Lax; HttpOnly"),))
        person = self._person()
        page = rest.split("?")[0] in ("/", "/index.html")
        if not person or person not in homes.keys_of(a.home):
            return self._shut()
        if self.command == "GET" and page:
            return self._page(focus=a.slug)
        return self._json({"error": a.name + " is " + a.word() + " just now"}, 503)

    # -- the manager's own ---------------------------------------------------------------

    def _page(self, focus=""):
        try:
            text = (WEB / "manager.html").read_text(encoding="utf-8")
        except OSError:
            return self._send(500, b"the manager's page is missing")
        text = text.replace("{{app}}", APP).replace("{{focus}}", focus)
        self._send(200, text.encode("utf-8"), TYPES[".html"])

    def _pair(self, key):
        """A key in the address, from the icon or a pairing line: kept in the
        cookie of whichever assistant it belongs to, and the address cleaned."""
        for a in self.keeper.all():
            if a.slug and homes.whose(a.home, key):
                return self._redirect("/", extra=(("Set-Cookie",
                    a.slug + "_who=" + key + "; Path=/; Max-Age=" + str(COOKIE_AGE)
                    + "; SameSite=Lax; HttpOnly"),))
        return self._shut()

    def _entry(self, a, person, desk) -> dict:
        here = a.look()
        return {"slug": a.slug, "name": a.name, "title": a.title,
                "phase": here["phase"], "word": WORDS.get(here["phase"], here["phase"]),
                "detail": here["detail"], "since": here["since"], "mode": a.mode,
                "control": self._may(person, a),
                # The install's own home stays; the page offers no Detach for it.
                "first": homes.is_first(a.home),
                # A folder is shown at the desk only.
                "home": str(a.home) if desk and self._may(person, a) else ""}

    def _manager(self, ip, url):
        path = url.path
        query = parse_qs(url.query)
        if path == "/" and query.get("k"):
            return self._pair(query["k"][0].strip())
        person = self._person()
        if not person:
            return self._shut()
        desk = self._desk(ip)
        seen = self._visible(person)
        owner = any(self._may(person, a) for a in seen)

        if self.command in ("GET", "HEAD"):
            if path == "/":
                # Someone who keeps none of them and is paired with one goes
                # straight into that one's room; the list is for the keeper.
                if not owner and len(seen) == 1:
                    return self._redirect("/" + seen[0].slug + "/")
                return self._page()
            if path == "/_/api/list":
                return self._json({"assistants": [self._entry(a, person, desk) for a in seen],
                                   "owner": owner, "desk": desk, "person": person,
                                   "default_parent": homes.default_parent() if owner else ""})
            if path.startswith("/_/face/"):
                a = self.keeper.by_slug(path[len("/_/face/"):])
                if not a or a not in seen:
                    return self._send(404, b"not here")
                for pic in (a.home / "artwork" / "portrait.png", CODE / "artwork" / "portrait.png"):
                    if pic.is_file():
                        return self._send(200, pic.read_bytes(), "image/png")
                return self._send(404, b"not here")
            if path in STATIC:
                target = STATIC[path]
                if target.is_file():
                    return self._send(200, target.read_bytes(),
                                      TYPES.get(target.suffix, "application/octet-stream"))
                return self._send(404, b"not here")
            return self._send(404, b"not here")

        if self.command != "POST":
            return self._send(405, b"")
        raw = self._body(API_BODY_MAX)
        if raw is None:
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            body = body if isinstance(body, dict) else {}
        except (ValueError, UnicodeDecodeError):
            body = {}

        if path in ("/_/api/start", "/_/api/stop", "/_/api/restart"):
            a = self.keeper.by_slug(str(body.get("slug") or ""))
            if not a or not self._may(person, a):
                return self._json({"error": "only the one it works for can start or stop it"}, 403)
            do = path.rsplit("/", 1)[1]
            by = person.title() + ("" if desk else ", from the road")
            got = self.keeper.act(a, do, by, anyway=bool(body.get("anyway")))
            got["assistant"] = self._entry(a, person, desk)
            return self._json(got)

        if path == "/_/api/new":
            if not owner:
                return self._json({"error": "a new assistant is made by the one who keeps them"}, 403)
            try:
                made = homes.create(body.get("name"), body.get("folder"))
            except homes.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": type(exc).__name__ + ": " + str(exc)}, 500)
            a = self.keeper.add(made["home"])
            mlog("made " + a.name + " at " + str(a.home) + " for " + person)
            return self._json({"slug": a.slug, "name": a.name, "url": "/" + a.slug + "/"})

        # Detach: stopped, forgotten, its folder let go of and left exactly as
        # it is -- to be moved, kept, or attached again. Attach: an assistant
        # that already has a home, from wherever that home is now.
        if path == "/_/api/detach":
            a = self.keeper.by_slug(str(body.get("slug") or ""))
            if not a or not self._may(person, a):
                return self._json({"error": "only the one it works for can detach it"}, 403)
            if not body.get("anyway") and a.mode == "ours" and a.busy():
                return self._json({"busy": True, "name": a.name})
            by = person.title() + ("" if desk else ", from the road")
            try:
                said = self.keeper.detach(a, by)
            except homes.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True, "said": said, "name": a.name,
                               "home": str(a.home) if desk else ""})

        if path == "/_/api/attach":
            if not owner:
                return self._json({"error": "an assistant is attached by the one who keeps them"}, 403)
            try:
                got = homes.attach(body.get("folder"))
            except homes.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": type(exc).__name__ + ": " + str(exc)}, 500)
            known = self.keeper.by_slug(got["slug"])
            a = known if known and _same(known.home, got["home"]) else self.keeper.add(got["home"])
            mlog("attached " + a.name + " from " + str(a.home) + " for " + person)
            return self._json({"slug": a.slug, "name": a.name, "url": "/" + a.slug + "/",
                               "note": got.get("note") or ""})

        if path == "/_/api/browse":
            if not owner or not desk:
                return self._json({"error": "the folder dialog opens at the desk"}, 403)
            title = ("Which folder holds the assistant?" if body.get("for") == "attach"
                     else "Where should the new assistant live?")
            try:
                return self._json({"folder": homes.browse(body.get("from"), title)})
            except homes.Refused as exc:
                return self._json({"error": str(exc)}, 400)

        return self._json({"error": "no such thing"}, 404)


def open_door(keeper, tries_forever=True):
    """The door, on the manager's port. If something is still sitting on it
    -- an old room from before the manager, say -- it keeps trying rather
    than giving up, and says so once."""
    told = False
    while True:
        try:
            door = Door((keeper.bind, keeper.port), DoorHandler)
        except OSError as exc:
            if not told:
                keeper.say("Port " + str(keeper.port) + " is taken",
                           "Something else is on it, so the manager's address is not open "
                           "yet. It opens by itself the moment the port is free.", "warn")
                mlog("the door could not open: " + str(exc))
                told = True
            if not tries_forever or keeper.quitting:
                return None
            time.sleep(3.0)
            continue
        door.keeper = keeper
        keeper.door = door
        mlog(f"the door is open on {keeper.bind}:{keeper.port}")
        threading.Thread(target=door.serve_forever, daemon=True, name="door").start()
        return door


# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------

MF_POPUP = 0x0010
OPEN_MANAGER, QUIT = 1, 2
ACTIONS = ("open", "start", "stop", "restart", "log")


class Icon:
    def __init__(self, keeper):
        self.keeper = keeper
        self.hicon_awake = self._icon(ART / "manager.ico")
        self.hicon_down = self._icon(ART / "manager-down.ico") or self.hicon_awake
        self.proc = tray.WNDPROC(self._wndproc)    # kept, or the first click crashes
        self.hwnd = None
        self.added = False
        self.reborn = user32.RegisterWindowMessageW("TaskbarCreated")

    def _icon(self, path):
        if not path.exists():
            return None
        return user32.LoadImageW(
            None, str(path), tray.IMAGE_ICON,
            user32.GetSystemMetrics(tray.SM_CXSMICON),
            user32.GetSystemMetrics(tray.SM_CYSMICON), tray.LR_LOADFROMFILE)

    def _data(self, flags):
        nid = tray.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(tray.NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def make_window(self):
        cls = tray.WNDCLASSW()
        cls.lpfnWndProc = self.proc
        cls.lpszClassName = LOCK
        cls.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(0, LOCK, APP, 0, 0, 0, 0, 0,
                                           None, None, cls.hInstance, None)
        self.keeper.hwnd = self.hwnd
        self.add()

    def add(self):
        nid = self._data(tray.NIF_MESSAGE | tray.NIF_ICON | tray.NIF_TIP)
        nid.uCallbackMessage = tray.TRAY_MSG
        nid.hIcon = self.hicon_awake
        nid.szTip = self.keeper.summary()
        shell32 = tray.shell32
        shell32.Shell_NotifyIconW(tray.NIM_ADD, ctypes.byref(nid))
        self.added = True

    def refresh(self):
        if not self.added:
            return
        nid = self._data(tray.NIF_ICON | tray.NIF_TIP)
        nid.hIcon = self.hicon_down if self.keeper.all_down() else self.hicon_awake
        nid.szTip = self.keeper.summary()
        tray.shell32.Shell_NotifyIconW(tray.NIM_MODIFY, ctypes.byref(nid))

    def balloon(self):
        while self.keeper.balloons:
            title, text, kind = self.keeper.balloons.popleft()
            nid = self._data(tray.NIF_INFO)
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = tray.BALLOON_KINDS.get(kind, tray.NIIF_INFO)
            tray.shell32.Shell_NotifyIconW(tray.NIM_MODIFY, ctypes.byref(nid))

    def remove(self):
        if self.added:
            tray.shell32.Shell_NotifyIconW(tray.NIM_DELETE, ctypes.byref(self._data(0)))
            self.added = False

    def menu(self):
        keeper = self.keeper
        kept = keeper.all()
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, tray.MF_STRING, OPEN_MANAGER, "Open the manager")
        user32.SetMenuDefaultItem(hmenu, OPEN_MANAGER, 0)
        user32.AppendMenuW(hmenu, tray.MF_SEPARATOR, 0, None)
        for i, a in enumerate(kept):
            sub = user32.CreatePopupMenu()
            base = 100 + i * 10
            if a.mode in ("ours", "theirs"):
                user32.AppendMenuW(sub, tray.MF_STRING, base + 0, "Open " + a.name)
            if a.mode in ("stopped", "held"):
                user32.AppendMenuW(sub, tray.MF_STRING, base + 1, "Start " + a.name)
            if a.mode == "ours":
                user32.AppendMenuW(sub, tray.MF_STRING, base + 3, "Restart " + a.name)
                user32.AppendMenuW(sub, tray.MF_STRING, base + 2, "Stop " + a.name)
            user32.AppendMenuW(sub, tray.MF_STRING, base + 4, "Show the log")
            user32.AppendMenuW(hmenu, MF_POPUP, sub, a.name + " -- " + a.word())
        if kept:
            user32.AppendMenuW(hmenu, tray.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, tray.MF_STRING, QUIT, "Quit (puts them all down)")

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        chose = user32.TrackPopupMenu(hmenu, tray.TPM_RIGHTBUTTON | tray.TPM_RETURNCMD,
                                      point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(hmenu)

        if chose == OPEN_MANAGER:
            threading.Thread(target=webbrowser.open, args=(keeper.open_url(),), daemon=True).start()
        elif chose == QUIT:
            threading.Thread(target=self.quit, daemon=True).start()
        elif chose >= 100:
            i, act = divmod(chose - 100, 10)
            if i < len(kept) and act < len(ACTIONS):
                threading.Thread(target=self.act, args=(kept[i], ACTIONS[act]), daemon=True).start()

    def act(self, a, do):
        keeper = self.keeper
        if do == "open":
            webbrowser.open(keeper.open_url(a))
            return
        if do == "log":
            try:
                os.startfile(str(a.log_path()))
            except OSError:
                try:
                    os.startfile(str(a.home / "logs"))
                except OSError:
                    pass
            return
        got = keeper.act(a, do, home.OWNER_NAME if home.is_real() else "the owner")
        if got.get("busy"):
            if not self.ask(a.name + " is in the middle of a turn right now.\n\n"
                            + do.title() + " anyway? The turn will be lost."):
                return
            keeper.act(a, do, home.OWNER_NAME if home.is_real() else "the owner", anyway=True)

    def ask(self, text) -> bool:
        return user32.MessageBoxW(None, text, APP,
                                  tray.MB_YESNO | tray.MB_ICONQUESTION | tray.MB_TOPMOST) == tray.IDYES

    def quit(self):
        busy = [a.name for a in self.keeper.all() if a.mode == "ours" and a.busy()]
        if busy and not self.ask(" and ".join(busy) + (" is" if len(busy) == 1 else " are")
                                 + " in the middle of a turn.\n\nQuit anyway? The turn will be lost."):
            return
        self.keeper.quit_all()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == tray.TRAY_MSG:
            if lparam in (tray.WM_LBUTTONUP, tray.WM_LBUTTONDBLCLK):
                threading.Thread(target=webbrowser.open, args=(self.keeper.open_url(),),
                                 daemon=True).start()
            elif lparam == tray.WM_RBUTTONUP:
                self.menu()
            return 0
        if msg == tray.TOOLTIP_MSG:
            self.refresh()
            return 0
        if msg == tray.BALLOON_MSG:
            self.balloon()
            return 0
        if msg == tray.QUIT_MSG:
            self.remove()
            user32.PostQuitMessage(0)
            return 0
        if msg == self.reborn:
            self.added = False
            self.add()
            return 0
        if msg == tray.WM_DESTROY:
            self.remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def only_one() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\" + LOCK)
    return ctypes.get_last_error() != tray.ERROR_ALREADY_EXISTS


def open_when_ready(keeper, seconds=90):
    """The manager's page once the door is open -- and, when there is one
    assistant awake or waking, once it answers, so the first thing on the
    screen is not a list of rooms still getting up."""
    until = time.time() + seconds
    while time.time() < until and not keeper.door:
        time.sleep(0.5)
    if keeper.door:
        webbrowser.open(keeper.open_url())


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--port", type=int, help="the port the manager answers on (homes.json, else 8787)")
    ap.add_argument("--bind", help="how wide it answers (homes.json, else 0.0.0.0)")
    ap.add_argument("--no-icon", action="store_true", help="no icon in the corner; for a side one")
    ap.add_argument("--open", action="store_true", help="open the manager's page once it is up")
    args = ap.parse_args(argv)

    bind, port = homes.front()
    keeper = Keeper(args.bind or bind, args.port or port)

    if not args.no_icon and not only_one():
        user32.MessageBoxW(None, "The manager is already in the corner of the screen.",
                           APP, tray.MB_TOPMOST)
        return 0
    prune_logs()
    mlog(f"the manager is starting, pid {os.getpid()}, on {keeper.bind}:{keeper.port}")

    icon = None
    if not args.no_icon:
        icon = Icon(keeper)
        icon.make_window()

    threading.Thread(target=open_door, args=(keeper,), daemon=True, name="door-opener").start()
    keeper.load()
    threading.Thread(target=keeper.watch, daemon=True, name="watch").start()
    if args.open:
        threading.Thread(target=open_when_ready, args=(keeper,), daemon=True).start()

    if icon:
        try:
            icon.run()
        finally:
            icon.remove()
        return 0
    print(f"{APP} on http://localhost:{keeper.port}/  (Ctrl+C puts them all down)", flush=True)
    for a in keeper.all():
        print(f"  {a.name}: /{a.slug}/ -> 127.0.0.1:{a.port}", flush=True)
    try:
        while not keeper.quitting:
            time.sleep(0.5)
    except KeyboardInterrupt:
        keeper.quit_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
