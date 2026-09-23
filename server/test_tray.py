# The icon in the corner, leaned on. Nothing here goes near 8787 or the real
# store: the supervisor is pointed at a spare port and handed a fake room to
# start, so every branch that decides whether it comes back can be walked
# through in ninety seconds instead of waited for over a week.
#
#   python -m server.test_tray
#
# The window and the icon are real -- an icon appears in the corner while this
# runs and goes away at the end -- because "does Shell_NotifyIcon actually take
# it" is one of the two things worth proving here. The other is that a restart
# somebody asked for is never counted as its failing.
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from server import tray

PORT = 8791
FAILED = []

# A room-shaped thing, written out beside the test and started instead of it.
#   serve <port>                 -- up until it is killed
#   serve <port> <secs> <code>   -- up, then leaves with that code
#   die <code>                   -- never opens at all
FAKE = '''
import json, sys, threading, time, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"busy": False, "steps": [], "out": [],
                           "recall": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
if sys.argv[1] == "die":
    print("never opened, on purpose", flush=True)
    raise SystemExit(int(sys.argv[2]))
room = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H)
print("fake room open on " + sys.argv[2], flush=True)
if len(sys.argv) > 4:
    def leave():
        time.sleep(float(sys.argv[3]))
        print("leaving with " + sys.argv[4], flush=True)
        os._exit(int(sys.argv[4]))
    threading.Thread(target=leave, daemon=True).start()
room.serve_forever()
'''


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}"
          + ("" if ok else f"  (wanted {want!r})"))
    if not ok:
        FAILED.append(name)


def wait_for(what, seconds=30.0, **wanted):
    started = time.time()
    while time.time() - started < seconds:
        here = tray.look()
        if all(here.get(k) == v for k, v in wanted.items()):
            print(f"  ok   {what} -- {time.time() - started:.1f}s")
            return True
        time.sleep(0.2)
    print(f"  FAIL {what} -- stuck at {tray.look()}")
    FAILED.append(what)
    return False


def wait_for_new_pid(what, was, seconds=60.0):
    """A different pid, up and answering. The only honest way to say it came
    back, rather than that it never left."""
    started = time.time()
    while time.time() - started < seconds:
        here = tray.look()
        if here["phase"] == "awake" and here["pid"] not in (was, None):
            print(f"  ok   {what} -- {time.time() - started:.1f}s, "
                  f"pid {was} -> {here['pid']}")
            return here["pid"]
        time.sleep(0.2)
    print(f"  FAIL {what} -- stuck at {tray.look()}")
    FAILED.append(what)
    return tray.look()["pid"]


def main():
    scratch = Path(tempfile.mkdtemp(prefix="assistant-tray-"))
    fake = scratch / "fake_room.py"
    fake.write_text(FAKE, encoding="utf-8")

    tray.PORT = PORT
    tray.LOGS = scratch / "logs"
    tray.MARK = scratch / "tray.json"
    tray.FALLING_OVER_WINDOW = 60.0

    serve = [sys.executable, "-u", str(fake), "serve", str(PORT)]
    what = {"cmd": list(serve)}
    tray.Room.command = lambda self: what["cmd"]

    if tray.answering():
        print(f"something is already on {PORT}; nothing was run.")
        return 1

    print("\n--- the window and the icon -----------------------------------")
    icon = tray.Tray()
    icon.make_window()
    check("window made", bool(icon.hwnd), True)
    check("icon taken by the shell", icon.added, True)
    check("its face loaded", bool(icon.hicon_awake), True)
    check("the greyed face is its own", icon.hicon_down != icon.hicon_awake,
          True)

    job = tray.keep_the_room_with_me()
    check("job object made", bool(job), True)

    threading.Thread(target=tray.watcher, daemon=True).start()
    threading.Thread(target=tray.mark_keeper, daemon=True).start()
    threading.Thread(target=tray.supervisor, args=(job,), daemon=True).start()

    print("\n--- it starts, and opens -------------------------------------")
    wait_for("it is awake", 30, phase="awake")
    held = tray.look()["pid"]
    wait_for("the mark is written", 8, phase="awake")
    check("a mark on disk", tray.MARK.exists(), True)
    if tray.MARK.exists():
        check("the mark names this icon",
              json.loads(tray.MARK.read_text())["tray"], os.getpid())

    print("\n--- it asks for a restart itself (code 7) -------------------")
    what["cmd"] = serve + ["2", "7"]
    tray.COMMANDS.put("restart")
    held = wait_for_new_pid("back from the asked restart", held, 40)
    what["cmd"] = list(serve)
    held = wait_for_new_pid("back again from its own code 7", held, 40)

    print("\n--- it falls over, having opened properly --------------------")
    what["cmd"] = serve + ["3", "5"]
    tray.COMMANDS.put("restart")
    held = wait_for_new_pid("up as the one that will fall over", held, 40)
    what["cmd"] = list(serve)
    held = wait_for_new_pid("it fell over and was brought back", held, 60)
    check("brought back, not held", tray.look()["phase"], "awake")

    print("\n--- restarts he asked for must never hold it -----------------")
    for n in range(3):
        tray.COMMANDS.put("restart")
        held = wait_for_new_pid(f"back from asked restart {n + 1}", held, 40)
    check("still not held", tray.look()["phase"], "awake")

    print("\n--- it cannot open at all ------------------------------------")
    what["cmd"] = [sys.executable, "-u", str(fake), "die", "1"]
    tray.COMMANDS.put("restart")
    wait_for("held after three that never opened", 90, phase="held")
    print("       reason: " + tray.look()["detail"])
    check("nothing left running", tray.look()["pid"], None)
    time.sleep(6)
    check("and it stays held", tray.look()["phase"], "held")

    print("\n--- he wakes it by hand --------------------------------------")
    what["cmd"] = list(serve)
    tray.COMMANDS.put("wake")
    wait_for("awake again on his word", 40, phase="awake")

    print("\n--- what it would have said out loud --------------------------")
    for title, text, kind in list(tray.BALLOONS):
        print(f"  [{kind:4}] {title} -- {text[:64]}")

    print("\n--- putting it down ------------------------------------------")
    tray.COMMANDS.put("quit")
    wait_for("down", 30, phase="down")
    time.sleep(2.0)
    check("it let the port go", tray.answering(), False)
    icon.remove()
    check("icon given back", icon.added, False)

    print()
    if FAILED:
        print(f"{len(FAILED)} failed: " + ", ".join(FAILED))
        return 1
    print("every rule held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
