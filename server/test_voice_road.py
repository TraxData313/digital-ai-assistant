"""The two roads, driven end to end through a real room on a scratch port.

The unit checks beside this one ask what the reading says. This one asks what
the door does: the routes that write the choice down, the guard that stops
anything but its own page from reaching them, and the answer the page draws
from. Port 0, so the operating system picks the number and a bench can never
land on its own; the voice file is the scratch's own, so a run here can never flip
the switch in his room.

Nothing is spoken and no model is called.

    python -m server.test_voice_road <scratch outside the repo>
"""
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from . import app, brain, db, limits, notes, people

FAILED = []
PRETEND = {"addr": "127.0.0.1"}


def check(what, ok, detail=None):
    print(("  ok   " if ok else "  FAIL ") + what
          + ("" if ok or detail is None else "  -> " + repr(detail)))
    if not ok:
        FAILED.append(what)


def _pretend_peer(self):
    """The one seam, as in test_pocket: the handler reads an address object,
    and a bench on this machine can only ever come from loopback."""
    import ipaddress
    try:
        return ipaddress.ip_address(PRETEND["addr"])
    except ValueError:
        return None


KEY = {"sam": ""}


def voice(port, path, body=None, origin=True, header=True):
    """One request at the voice door, dressed the way its own page dresses one."""
    url = "http://127.0.0.1:" + str(port) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Cookie", "ada_who=" + KEY["sam"])
    if data:
        req.add_header("Content-Type", "application/json")
        if origin:
            req.add_header("Origin", "http://127.0.0.1:" + str(port))
        if header:
            req.add_header("X-Assistant-Voice", "1")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def main():
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_voice_road <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    people.TOKEN_DIR = scratch / "people"
    KEY["sam"] = people.mint("sam")
    db.DB_PATH = scratch / "store.db"
    notes.NOTES_DIR = scratch / "notes"
    # His file is never opened by this bench. Everything the routes write goes
    # here, so a run cannot turn the voice off in the room he is sitting in.
    brain.VOICE_PATH = scratch / "voice.json"
    # Port 9 is discard, and nothing on this machine listens there. The bench
    # must not depend on whether his real speak server happens to be up: half
    # the answers below are what it is told when the road is down, and they
    # would read as failures on a day the voice is working.
    brain.VOICE_PATH.write_text(json.dumps({"enabled": True, "port": 9,
                                            "voice": "default"}), encoding="utf-8")
    brain._VOICE_SEEN.update(at=0.0, was=None)
    limits._CACHE.update({"at": time.time() + 3600, "limits": {}, "error": None,
                          "asking": False})

    app.Handler._peer = _pretend_peer
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    port = room.server_address[1]
    print("  (the bench room is on " + str(port) + ", chosen by the OS)")
    threading.Thread(target=room.serve_forever, daemon=True).start()

    # The speak server is not running for this bench, and must not be waited
    # on: a refusal is one of the answers being checked.
    served = {"state": None}

    def fake_open(req, timeout=None):
        if served["state"] is None:
            raise OSError("nothing is listening")
        import io
        return io.BytesIO(json.dumps(served["state"]).encode())

    try:
        # The speak server is patched only around the direct readings at the
        # bottom. It cannot be patched around the requests above: those go over
        # a real socket through the same urlopen, and a bench that muzzled its
        # own door would be testing nothing.
        # -- what the page is told ---------------------------------------
        code, said = voice(port, "/api/voice")
        check("the page is told which road is chosen", code == 200
              and said.get("backend") == "local", said)
        check("and the room's own switch, apart from whether it is heard",
              said.get("switch") is True and said.get("local", {}).get("on") is False,
              said.get("local"))
        check("a road that is down says why rather than claiming it was heard",
              bool(said.get("local", {}).get("reason")), said.get("local"))

        # -- the guard ----------------------------------------------------
        for what, kwargs in (("without its page's header", {"header": False}),
                             ("from another origin", {"origin": False})):
            code, _ = voice(port, "/api/voice/backend", {"backend": "openai"}, **kwargs)
            check("the voice door is shut " + what, code == 403, code)
        check("and nothing was written", brain.voice_backend() == "local")

        # -- choosing -----------------------------------------------------
        code, said = voice(port, "/api/voice/backend", {"backend": "openai"})
        check("its page may choose the other road", code == 200
              and said.get("backend") == "openai", said)
        check("it is written down, not merely answered",
              brain.voice_backend() == "openai")
        check("and on it it is told plainly that nobody is listening",
              said["local"]["on"] is False and "OpenAI" in said["local"]["reason"],
              said["local"])

        code, said = voice(port, "/api/voice/backend", {"backend": "kyutai"})
        check("a road that does not exist is refused", code == 400, code)
        check("and the choice stands", brain.voice_backend() == "openai")

        code, said = voice(port, "/api/voice/backend", {"backend": "local"})
        check("and back again", code == 200 and brain.voice_backend() == "local")

        # -- the switch ---------------------------------------------------
        code, said = voice(port, "/api/voice/enabled", {"on": False})
        check("the power button writes the room's own switch", code == 200
              and said.get("switch") is False, said)
        check("it is told it is switched off, in those words",
              "switched off" in said["local"]["reason"], said["local"])
        check("the rest of the file survived being written",
              json.loads(brain.VOICE_PATH.read_text(encoding="utf-8"))["port"] == 9)
        code, said = voice(port, "/api/voice/enabled", {"on": True})
        check("and on again", code == 200 and said.get("switch") is True, said)

        # -- what it may say about how it sounds -------------------------
        served["state"] = {"enabled": True, "ready": True, "voice": "default",
                           "engine": "qwen", "instruction": True}
        with patch("urllib.request.urlopen", side_effect=fake_open):
            reading = brain.voice_status(fresh=True)
        check("a warm Qwen is the one case where it is offered a mood",
              reading["on"] and reading["sound"], reading)
        check("and the field is in the contract exactly then",
              "sound" in brain.response_schema(reading["on"], reading["sound"])["properties"])
        served["state"]["instruction"] = False
        served["state"]["engine"] = "pocket"
        with patch("urllib.request.urlopen", side_effect=fake_open):
            reading = brain.voice_status(fresh=True)
        check("an engine without one is not offered it",
              reading["on"] and not reading["sound"], reading)
        check("and then the field is not in the contract either",
              "sound" not in brain.response_schema(reading["on"], reading["sound"])["properties"])
        check("nor any sounds, with none on the list",
              reading["events"] == [], reading)

        # -- and what it may do inside the words --------------------------
        served["state"] = {"enabled": True, "ready": True, "voice": "default",
                           "engine": "breeze", "instruction": True,
                           "events": ["laugh", "sigh", "cough", "clears throat"]}
        # One fresh reading, then everything after it answers off that one --
        # the contract and the page both, as a turn and its window do.
        with patch("urllib.request.urlopen", side_effect=fake_open):
            brain.voice_status(fresh=True)
            said = brain.contract()
        check("an engine that laughs is offered a mood and its sounds",
              said["voice"]["sound"] and said["voice"]["events"] == [
                  "laugh", "sigh", "cough", "clears throat"], said["voice"])
        check("and its instructions name every one of them, in brackets",
              "(laugh) (sigh) (cough) (clears throat)" in said["harness"])
        check("while the contract grows no field for them: they ride in the words",
              set(said["answers_with"]["properties"]) - {"sound"}
              == set(brain.response_schema(True, False)["properties"]),
              sorted(said["answers_with"]["properties"]))
        code, said = voice(port, "/api/voice")
        check("the page is told the same list the prompt was",
              code == 200 and said["local"].get("events") == [
                  "laugh", "sigh", "cough", "clears throat"], said.get("local"))
    finally:
        room.shutdown()

    print()
    print("both roads hold" if not FAILED
          else str(len(FAILED)) + " failed: " + "; ".join(FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
