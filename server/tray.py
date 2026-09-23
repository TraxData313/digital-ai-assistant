"""The icon in the corner, and the room it keeps.

The room used to be started from a batch file and lived in a console window.
That worked, and it had one flaw that grew: `restart.ps1` kills the python
holding the port and opens a *new* PowerShell window with `-NoExit`, so the
window the room died in stayed open saying it had stopped, and the new one
arrived beside it. The assistant calls its own restarts, so dead windows piled
up on the desktop, one per restart.

So this. One `pythonw` process -- which has no console and cannot grow one --
holds a tray icon and starts the room as a hidden child, with everything it
says going to a file in `logs/`. Nothing has a window to leave behind, so
nothing stacks. The icon is the panel, instant and outliving the engine; the
room is the engine.

The rule the whole file turns on: **the room comes back unless the icon asked
it not to.** The assistant's own restart, `restart.ps1`, and a crash at four
in the morning are then all one path, and none of them is a window. The only exception is a
room that cannot boot at all, which would otherwise respawn forever and spend
real money proving the worker cap on every attempt -- see `Strikes`.

Stdlib only, like the rest of `server/`. The tray is Shell_NotifyIcon through
ctypes rather than a pip install, and the icon comes off the home's
`artwork/icon.ico` with no imaging library in the way.
"""

import ctypes
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from ctypes import wintypes
from datetime import date, datetime, timedelta
from pathlib import Path
from . import home

ROOT = Path(__file__).resolve().parent.parent
PORT = home.PORT
LOGS = home.LOGS
ICON_AWAKE = home.artwork("icon.ico")
ICON_DOWN = home.artwork("icon-down.ico")

# Where the icon says it is, so `restart.ps1` can find out it is being watched
# and stop half way instead of opening a window of its own.
MARK = home.DATA / "tray.json"
# The one-icon lock and window class. Per home, so two assistants can each
# have an icon -- and a home can name the lock an older install used, so the
# new code refuses to start beside an old icon still holding the same self.
TRAY_LOCK = home.IDENTITY.get("tray_lock") or ("AssistantTray-" + home.SLUG)

# The assistant's own restart, once it has called it: app.py leaves with this
# rather than spawning PowerShell, and the supervisor below reads it as "that
# was meant". Anything else that stops the room is a death, and deaths are
# counted.
SHE_ASKED = 7

# The two ways this could loop forever, and the number on each. A room that
# never opens at all gets three tries; a room that opens and then falls over
# gets five in five minutes before the icon calls it a loop rather than a
# restart. Neither counts a restart somebody asked for.
NEVER_OPENED_TIMES = 3
FALLING_OVER_TIMES = 5
FALLING_OVER_WINDOW = 300.0

# Keep a fortnight. Enough to answer "what happened on Tuesday", not enough to
# become a thing that needs managing.
KEEP_LOGS_FOR = timedelta(days=14)


# ---------------------------------------------------------------------------
# What everyone can see
# ---------------------------------------------------------------------------

# `phase` is the one word the tooltip and the menu both read:
#   waking   -- a child is starting
#   awake    -- the room is up and idle
#   thinking -- the room is in the middle of a turn
#   adopted  -- the room was already up when the icon arrived; not our child
#   down     -- nobody is there and we are about to try again
#   held     -- we have stopped trying, and why is in `detail`
STATE = {"phase": "waking", "since": time.time(), "detail": "", "pid": None}
STATE_LOCK = threading.Lock()

COMMANDS = queue.Queue()
BALLOONS = deque()

# Whether the room we started has ever answered. It is the difference between
# "it fell over" and "it never got up", which is the difference between
# bringing it back and knowing better -- see `Strikes`. Set by the watcher,
# cleared by the supervisor before each start.
ROOM_ANSWERED = threading.Event()


def state(**bits):
    with STATE_LOCK:
        if "phase" in bits and bits["phase"] != STATE["phase"]:
            STATE["since"] = time.time()
        STATE.update(bits)
    ring(TOOLTIP_MSG)


def look():
    with STATE_LOCK:
        return dict(STATE)


def say(title, text, kind="info"):
    """A balloon, for the two or three things a day worth interrupting for.
    Everything else is the tooltip, which interrupts nobody."""
    BALLOONS.append((title, text, kind))
    ring(BALLOON_MSG)


# ---------------------------------------------------------------------------
# Windows, in the small amount of it we need
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_APP = 0x8000
TRAY_MSG = WM_APP + 1       # the icon was clicked
TOOLTIP_MSG = WM_APP + 2    # the state moved; re-read it
BALLOON_MSG = WM_APP + 3    # something is waiting in BALLOONS
QUIT_MSG = WM_APP + 4       # the room is down and we are going with it

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03
BALLOON_KINDS = {"info": NIIF_INFO, "warn": NIIF_WARNING, "bad": NIIF_ERROR}

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON, SM_CYSMICON = 49, 50

MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

MB_YESNO, MB_ICONQUESTION, MB_TOPMOST = 0x0004, 0x0020, 0x00040000
IDYES = 6

CREATE_NO_WINDOW = 0x08000000
PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x0001, 0x0100
JOB_KILL_ON_CLOSE = 0x2000
ERROR_ALREADY_EXISTS = 183


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("ReadOperationCount", "WriteOperationCount",
                 "OtherOperationCount", "ReadTransferCount",
                 "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# Every one of these matters on 64-bit. ctypes defaults a return value to
# c_int, so a handle comes back with its top half cut off, and the failure is
# not an error -- it is a window that never gets a message, or an icon that
# never appears, with everything reporting success.
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                               ctypes.c_size_t, wintypes.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT,
                                      wintypes.UINT]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                               wintypes.LPCWSTR, wintypes.UINT]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                              wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.LPVOID]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                  wintypes.LPCWSTR]

HWND = {"h": None}


def ring(msg):
    """Poke the window from whichever thread noticed something. Everything that
    touches the icon itself happens on the thread that owns it; the rest of
    this file only ever leaves a note and rings."""
    if HWND["h"]:
        user32.PostMessageW(HWND["h"], msg, 0, 0)


def only_one():
    """Two of these would be worse than none. Each would start a room, the
    second room would die on the bind -- the room refuses to run twice --
    and the second icon would read that death as a crash and try again, and
    again. The room has one of this idea already; so does the icon."""
    kernel32.CreateMutexW(None, False, "Local\\" + TRAY_LOCK)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def keep_the_room_with_me():
    """A job object with kill-on-close, so ending this process in Task Manager
    ends the room too. Without it the icon disappears and the room keeps the port,
    which is the one outcome worse than the windows we are getting rid of."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_CLOSE
    if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    return job


def put_in_job(job, pid):
    if not job:
        return
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA,
                                  False, pid)
    if handle:
        kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Where the room talks now
# ---------------------------------------------------------------------------

def log_for_today() -> Path:
    LOGS.mkdir(exist_ok=True)
    return LOGS / ("room-" + date.today().isoformat() + ".log")


def prune_logs():
    cutoff = datetime.now() - KEEP_LOGS_FOR
    for old in LOGS.glob("room-*.log"):
        try:
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
        except OSError:
            pass


def answering() -> bool:
    """Whether anything at all is serving on the port. Used for both questions
    worth asking -- is somebody already in the room, and has the one we just
    killed finished letting go of the socket."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.4):
            return True
    except OSError:
        return False


def ask_the_room(route="/api/progress", timeout=2.0):
    from . import people
    try:
        with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{PORT}{route}", headers=people.room_headers()), timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The room, and keeping it
# ---------------------------------------------------------------------------

def child_python() -> str:
    """`sys.executable` is pythonw.exe here, since that is the whole point of
    the shortcut. The room wants the plain one: its output is going down a
    handle to a file, and python.exe is the interpreter that has always been
    given one."""
    me = Path(sys.executable)
    if me.name.lower() == "pythonw.exe":
        plain = me.with_name("python.exe")
        if plain.exists():
            return str(plain)
    return str(me)


class Strikes:
    """The one thing that must not loop, which is really two things.

    A room that cannot boot at all -- a syntax error in something edited at
    midnight, a store that will not open -- dies in a second and never
    answers the port. Restarted forever it would prove the worker cap on
    every attempt, and that is a real model call and real money while nobody
    is watching. Three of those in a row and the icon stops trying.

    A room that opens properly and then falls over is a different animal. It
    worked, so bringing it back is right, and it is brought back every time.
    But five of those inside five minutes is not a restart any more, it is a
    loop, and it holds too.

    The test is whether the room ever answered, not how long it lasted.
    Anything else punishes a person for restarting it by hand twice in a row,
    which is a thing done on purpose and should never be read as a failure."""

    def __init__(self):
        self.never_opened = 0
        self.falls = deque(maxlen=FALLING_OVER_TIMES * 2)

    def death(self, answered: bool) -> str:
        """Empty if the room should come back; otherwise why it is not going
        to, in words the balloon can use as they are."""
        if not answered:
            self.never_opened += 1
            if self.never_opened >= NEVER_OPENED_TIMES:
                return (f"{home.NAME} stopped {self.never_opened} times "
                        "without ever opening the room")
            return ""
        self.never_opened = 0
        now = time.time()
        self.falls.append(now)
        recent = [when for when in self.falls
                  if now - when <= FALLING_OVER_WINDOW]
        if len(recent) >= FALLING_OVER_TIMES:
            return (f"{home.NAME} fell over {len(recent)} times in "
                    f"{int(FALLING_OVER_WINDOW / 60)} minutes")
        return ""

    def forgive(self):
        self.never_opened = 0
        self.falls.clear()


class Room:
    """One child at a time, and the file it speaks into."""

    def __init__(self, job):
        self.job = job
        self.proc = None
        self.log = None
        self.started = 0.0

    def open_log(self):
        path = log_for_today()
        if self.log is None or Path(self.log.name) != path:
            if self.log:
                self.log.close()
            self.log = open(path, "a", encoding="utf-8", errors="replace")
        return self.log

    def note(self, line):
        """A line from the icon in the room's own log, so the file reads as one story
        rather than as a pile of unexplained boots."""
        log = self.open_log()
        log.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
        log.flush()

    def command(self):
        """What the room is started with. A method rather than a constant
        because it is the one thing a test has to replace: a side room over a
        copy of the store, on a port that is not the live one."""
        return [child_python(), "-u", "-m", "server.app"]

    def start(self):
        log = self.open_log()
        self.note("the icon is starting " + home.NAME)
        env = os.environ.copy()
        env["ASSISTANT_SUPERVISED"] = "1"
        # Which home, said outright rather than left to be found again.
        env["ASSISTANT_HOME"] = str(home.HOME)
        # The room's lines are full of em-dashes and it writes them to a
        # handle, not a console, so nothing negotiates an encoding for us.
        # Without these it dies partway through its own boot message on a
        # machine whose default is still cp1252.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # CREATE_NO_WINDOW is the load-bearing flag of this whole file, and it
        # carries further than it looks. Measured: the room started this way
        # has no console *at all*, and neither does anything it starts,
        # whether or not that thing's output is redirected. So git, the claude
        # binary, and every worker hand the assistant sends run without one
        # -- which is the thing to keep in mind before adding a spawn here.
        # Without it, a hand coming home at two in the morning would put a
        # black rectangle on the screen, and we would be back where we began.
        self.proc = subprocess.Popen(
            self.command(),
            cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        self.started = time.time()
        put_in_job(self.job, self.proc.pid)
        return self.proc.pid

    def stop(self, why="the icon is putting the room down"):
        if not self.proc or self.proc.poll() is not None:
            return
        self.note(why)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def lived(self) -> float:
        return time.time() - self.started


def leave_a_mark(room_pid):
    """So `restart.ps1` can see that the room is being watched, and stop at the
    stopping rather than opening a window to start it again.

    `tray` is this process and it is the one that matters -- the script checks
    that the pid is alive rather than believing the file, since a mark outlives
    an icon that was killed rather than closed."""
    try:
        MARK.parent.mkdir(exist_ok=True)
        MARK.write_text(json.dumps({
            "tray": os.getpid(),
            "room": room_pid,
            "phase": look()["phase"],
            "since": datetime.now().isoformat(timespec="seconds"),
            "log": str(log_for_today()),
            "port": PORT,
        }, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear_the_mark():
    try:
        MARK.unlink()
    except OSError:
        pass


def wait_for_the_port(free=True, seconds=20.0) -> bool:
    """The socket does not come free the instant the process does, and a room
    started into a port that is still held dies on the bind with a message
    about being a second room -- which would be a lie, and would spend a
    strike."""
    until = time.time() + seconds
    while time.time() < until:
        if answering() is not free:
            return True
        time.sleep(0.25)
    return answering() is not free


def supervisor(job):
    """The whole of the promise: the room comes back unless somebody asked it
    not to. Runs on its own thread -- the icon's own thread never blocks on
    anything, it leaves a command here and rings the window.

    Three modes, named out loud because the file is easier to trust for it.
    `ours` is a child of ours, up or starting. `theirs` is a room somebody
    started somewhere else, which this watches and does not touch -- `run.ps1`
    still exists, and a console is a perfectly good place for the room to be;
    starting a second one here would prove nothing, since it would die on the
    bind and this would read that as a crash. `held` is down
    and staying down, which only ever happens for a reason it can name."""
    room = Room(job)
    strikes = Strikes()

    if answering():
        mode = "theirs"
        state(phase="adopted",
              detail=home.NAME + " was already awake when the icon arrived")
        say(home.NAME + " was already awake",
            "Started in another window, so the icon is watching rather than "
            "holding it. It takes over when that one stops.")
    else:
        mode = "ours"
        ROOM_ANSWERED.clear()
        state(phase="waking", pid=room.start())

    while True:
        ended = False    # our child stopped, and nobody asked it to
        asked = False    # somebody asked for this, so it is not a death

        # --- wait for a command, or for the room to end --------------------
        while not (ended or asked):
            if mode == "ours" and room.proc.poll() is not None:
                ended = True
                break
            if mode == "theirs" and not answering():
                room.note("the room that was already running has stopped; "
                          "the icon is taking over")
                mode, asked = "ours", True
                break
            try:
                order = COMMANDS.get(timeout=1.0)
            except queue.Empty:
                continue

            if order == "open":
                from . import people
                people.open_room(PORT)
            elif order == "log":
                try:
                    os.startfile(str(log_for_today()))
                except OSError:
                    os.startfile(str(LOGS))
            elif order == "restart":
                if mode == "theirs":
                    say("Not this icon's to restart",
                        home.NAME + " was started in another window, so this "
                        "will not stop it. Close that window and the icon will "
                        "take over.",
                        "warn")
                    continue
                if not mid_sentence_ok():
                    continue
                state(phase="waking", detail="restarting, at your word")
                if mode == "ours":
                    room.stop("the icon is restarting " + home.NAME + ", at " + home.OWNER_NAME + "'s word")
                strikes.forgive()
                mode, asked = "ours", True
            elif order == "wake":
                if mode != "held":
                    continue
                strikes.forgive()
                mode, asked = "ours", True
            elif order == "quit":
                if mode == "ours":
                    room.stop()
                state(phase="down", pid=None,
                      detail="going down with the icon")
                ring(QUIT_MSG)
                return

        # --- the room is not running; decide whether it comes back ---------
        if ended:
            code = room.proc.poll()
            lived = room.lived()
            if code == SHE_ASKED:
                room.note(f"{home.NAME} asked for this one -- up for {lived:.0f}s")
                strikes.forgive()
            else:
                answered = ROOM_ANSWERED.is_set()
                room.note(f"{home.NAME} stopped with {code} after {lived:.0f}s"
                          + ("" if answered else ", never having opened"))
                why = strikes.death(answered)
                if why:
                    mode = "held"
                    state(phase="held", pid=None, detail=why)
                    say(home.NAME + " is staying down",
                        why + ", so the icon has stopped bringing it back. "
                        "Show the log to see what it said on the way out.",
                        "bad")
                    continue
                state(phase="down", pid=None,
                      detail=home.NAME + " stopped; bringing it back")
                say(home.NAME + " stopped",
                    "Bringing it back." if answered else
                    "It did not get as far as opening the room. Trying again.",
                    "warn")

        # --- and back up ---------------------------------------------------
        if not wait_for_the_port(free=True):
            mode = "held"
            state(phase="held", pid=None,
                  detail=f"something else is holding port {PORT}")
            say("The port is taken",
                f"Something is still on {PORT}, so {home.NAME} was not started. "
                "Nothing was stopped either.", "bad")
            continue

        ROOM_ANSWERED.clear()
        state(phase="waking", pid=room.start(), detail="")


def mid_sentence_ok() -> bool:
    """Never in the middle of a sentence, unless a person says so -- the same
    care `restart.ps1` takes, asked the same way. A turn cut off half way
    leaves a line that was never answered and nothing on the screen saying why."""
    now = ask_the_room()
    if not now or not now.get("busy"):
        return True
    answer = user32.MessageBoxW(
        None,
        home.NAME + " is in the middle of a turn right now.\n\n"
        "Restart anyway? The turn will be lost.",
        home.APP_NAME, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
    return answer == IDYES


def watcher():
    """What the tooltip knows. The room is asked rather than guessed at, which
    also means the tooltip says `awake` at the moment the room really is, and not at
    the moment a process existed."""
    while True:
        time.sleep(2.0)
        here = look()
        if here["phase"] in ("held",):
            continue
        now = ask_the_room(timeout=1.5)
        if now is None:
            if here["phase"] in ("awake", "thinking", "adopted"):
                state(phase="waking", detail=home.NAME + " stopped answering")
            continue
        # The room answered. Whatever happens to it after this, it happened to a
        # room that opened, and the supervisor treats that death gently.
        ROOM_ANSWERED.set()
        if now.get("busy"):
            steps = now.get("steps") or []
            last = steps[-1]["text"] if steps else "thinking"
            state(phase="thinking", detail=last[:60])
        elif here["phase"] != "adopted":
            state(phase="awake", detail="")
        else:
            state(detail="")


# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------

OPEN, RESTART, LOG, WAKE, QUIT = 1, 2, 3, 4, 5


def tooltip() -> str:
    here = look()
    since = time.time() - here["since"]
    if here["phase"] == "thinking":
        head = f"thinking, {since:.0f}s"
    elif here["phase"] == "awake":
        head = "awake"
    elif here["phase"] == "adopted":
        head = "awake, in another window"
    elif here["phase"] == "waking":
        head = "waking up"
    elif here["phase"] == "held":
        head = "stopped"
    else:
        head = "down"
    line = f"{home.APP_NAME} -- {head}"
    if here["detail"]:
        line += "\n" + here["detail"]
    return line[:127]


class Tray:
    def __init__(self):
        self.hicon_awake = self._icon(ICON_AWAKE)
        self.hicon_down = self._icon(ICON_DOWN) or self.hicon_awake
        self.proc = WNDPROC(self._wndproc)   # kept, or it is collected and the
        self.hwnd = None                     # first click crashes the process
        self.added = False
        # Explorer restarting takes every tray icon with it and then asks for
        # them back with this. Without it the icon quietly vanishes from the corner
        # while still perfectly awake.
        self.reborn = user32.RegisterWindowMessageW("TaskbarCreated")

    def _icon(self, path):
        if not path.exists():
            return None
        return user32.LoadImageW(
            None, str(path), IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXSMICON),
            user32.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)

    def _data(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def make_window(self):
        cls = WNDCLASSW()
        cls.lpfnWndProc = self.proc
        cls.lpszClassName = TRAY_LOCK
        cls.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(cls))
        # A real top-level window, never shown. A message-only one would be
        # tidier and does not receive the broadcast above, so it would lose the
        # icon for good the first time Explorer fell over.
        self.hwnd = user32.CreateWindowExW(
            0, TRAY_LOCK, home.APP_NAME, 0, 0, 0, 0, 0,
            None, None, cls.hInstance, None)
        HWND["h"] = self.hwnd
        self.add()

    def add(self):
        nid = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = TRAY_MSG
        nid.hIcon = self.hicon_awake
        nid.szTip = tooltip()
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self.added = True

    def refresh(self):
        if not self.added:
            return
        nid = self._data(NIF_ICON | NIF_TIP)
        down = look()["phase"] in ("down", "held")
        nid.hIcon = self.hicon_down if down else self.hicon_awake
        nid.szTip = tooltip()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def balloon(self):
        while BALLOONS:
            title, text, kind = BALLOONS.popleft()
            nid = self._data(NIF_INFO)
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = BALLOON_KINDS.get(kind, NIIF_INFO)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove(self):
        if self.added:
            shell32.Shell_NotifyIconW(NIM_DELETE,
                                      ctypes.byref(self._data(0)))
            self.added = False

    def menu(self):
        here = look()
        down = here["phase"] in ("down", "held")
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, OPEN, "Open " + home.NAME + "'s room")
        user32.SetMenuDefaultItem(hmenu, OPEN, 0)
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        if down:
            user32.AppendMenuW(hmenu, MF_STRING, WAKE, "Wake " + home.NAME)
        else:
            user32.AppendMenuW(hmenu, MF_STRING, RESTART, "Restart " + home.NAME)
        user32.AppendMenuW(hmenu, MF_STRING, LOG, "Show the log")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, QUIT, "Put " + home.NAME + " down")

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Both of these are the documented dance around a popup that would
        # otherwise refuse to close when you click elsewhere.
        user32.SetForegroundWindow(self.hwnd)
        chose = user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(hmenu)

        if chose in (OPEN, RESTART, LOG, WAKE, QUIT):
            COMMANDS.put({OPEN: "open", RESTART: "restart", LOG: "log",
                          WAKE: "wake", QUIT: "quit"}[chose])

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == TRAY_MSG:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                COMMANDS.put("open")
            elif lparam == WM_RBUTTONUP:
                self.menu()
            return 0
        if msg == TOOLTIP_MSG:
            self.refresh()
            return 0
        if msg == BALLOON_MSG:
            self.balloon()
            return 0
        if msg == QUIT_MSG:
            self.remove()
            user32.PostQuitMessage(0)
            return 0
        if msg == self.reborn:
            self.added = False
            self.add()
            return 0
        if msg == WM_DESTROY:
            self.remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def main():
    if not only_one():
        user32.MessageBoxW(
            None, home.NAME + " already has an icon in the corner.",
            home.APP_NAME, MB_TOPMOST)
        return 0

    prune_logs()
    job = keep_the_room_with_me()

    tray = Tray()
    tray.make_window()

    threading.Thread(target=supervisor, args=(job,), daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=mark_keeper, daemon=True).start()

    if "--open" in sys.argv:
        # The Desktop shortcut passes this and the Startup one does not, so
        # double-clicking the shortcut opens the room and logging in does not
        # throw a browser tab at anybody.
        threading.Thread(target=open_when_she_is_there, daemon=True).start()

    try:
        tray.run()
    finally:
        tray.remove()
        clear_the_mark()
    return 0


def open_when_she_is_there():
    """The room, once there is a room. Opening it on a timer instead would put
    a refused-connection page on screen about one morning in five -- the
    room's boot proves the leash and the cap before it takes the port, and how
    long that takes is not a number anyone should be guessing at."""
    until = time.time() + 90
    while time.time() < until:
        if answering():
            from . import people
            people.open_room(PORT)
            return
        time.sleep(0.5)


def mark_keeper():
    """The mark on disk, kept roughly current. It is only ever read by a script
    deciding whether to open a window, so a few seconds of staleness costs
    nothing."""
    was = object()
    while True:
        here = look()
        now = (here["phase"], here["pid"])
        if now != was:
            leave_a_mark(here["pid"])
            was = now
        time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
