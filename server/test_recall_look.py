"""How often a room asks LM Studio about the automatic memory's model:
python -m server.test_recall_look

Needs nothing: LM Studio is stood in front of, keys are minted in a
temporary folder, and the room is a side room on a spare port. What is
checked: the state a status answer carries never asks LM Studio; a loaded
model is asked about once a minute, one that is coming or going every ten
seconds, and one LM Studio has let go shows as on disk; the manager's
heartbeat -- /api/progress with no `look` -- never makes the room ask, a
page that is open does, and the page says so on every poll.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from . import app, db, home, people, recall

KEY = recall.MODELS[0]["key"]


def lm_studio(calls, loaded):
    """LM Studio's model list, with the chosen model in memory while
    `loaded["now"]` says so. Anything else asked of it fails the bench."""
    def ask(path, body=None, timeout=2.0):
        calls.append(path)
        if path == "/api/v1/models":
            return {"models": [{"key": KEY, "size_bytes": 1,
                                "loaded_instances": [{"id": KEY}] if loaded["now"] else []}]}
        raise AssertionError("the room asked LM Studio for " + path)
    return ask


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.temp / "store.db"))
        self.stack.enter_context(patch.object(recall, "SETTINGS_PATH", self.temp / "recall.json"))
        self.calls, self.loaded = [], {"now": True}
        self.stack.enter_context(patch.object(recall, "_lm", lm_studio(self.calls, self.loaded)))
        saved = dict(recall.STATE)
        self.stack.callback(lambda: (recall.STATE.clear(), recall.STATE.update(saved)))

    def test_the_state_it_shows_never_asks(self):
        recall._set(model=KEY, phase="loaded", checked=0.0)
        for _ in range(5):
            got = recall.status()
        self.assertEqual(self.calls, [])
        self.assertEqual(got["phase"], "loaded")

    def test_a_loaded_model_is_asked_about_once_a_minute(self):
        recall._set(model=KEY, phase="loaded", checked=time.time() - 30)
        recall.refresh()
        self.assertEqual(self.calls, [], "half a minute on, not yet")
        recall._set(checked=time.time() - recall.RECHECK_LOADED_S - 1)
        recall.refresh()
        self.assertEqual(self.calls, ["/api/v1/models"])
        recall.refresh()
        self.assertEqual(len(self.calls), 1, "and not again straight after")

    def test_a_model_let_go_is_seen_and_then_asked_about_every_ten_seconds(self):
        self.loaded["now"] = False
        recall._set(model=KEY, phase="loaded", checked=0.0)
        recall.refresh()
        self.assertEqual(recall.STATE["phase"], "on disk", "LM Studio letting it go is noticed")
        recall._set(checked=time.time() - 5)
        recall.refresh()
        self.assertEqual(len(self.calls), 1, "five seconds on, not yet")
        recall._set(checked=time.time() - recall.RECHECK_S - 1)
        recall.refresh()
        self.assertEqual(len(self.calls), 2, "ten seconds on, again")

    def test_only_a_page_that_is_open_makes_the_room_ask(self):
        self.stack.enter_context(patch.object(people, "TOKEN_DIR", self.temp / "people"))
        # The rest of the heartbeat's answer, stood in front of: none of it
        # is what this bench is about, and the plan's gauge would go out to
        # the network.
        self.stack.enter_context(patch.object(app.limits, "now", lambda: None))
        self.stack.enter_context(patch.object(
            app.limits, "status", lambda: {"limits": None, "error": None, "read_at": None}))
        self.stack.enter_context(patch.object(app.providers, "now", lambda: {}))
        self.stack.enter_context(patch.object(app.models_dir, "move_status", lambda: None))
        key = people.mint(home.OWNER)
        room = app.OneRoom(("127.0.0.1", 0), app.Handler)
        self.stack.callback(room.server_close)
        self.stack.callback(room.shutdown)
        threading.Thread(target=room.serve_forever, daemon=True).start()

        def ask(path):
            req = urllib.request.Request(
                "http://127.0.0.1:" + str(room.server_address[1]) + path,
                headers={"Cookie": home.COOKIE + "=" + key})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())

        recall._set(model=KEY, phase="loaded", checked=0.0)
        for _ in range(3):
            code, got = ask("/api/progress")
            self.assertEqual((code, got["recall"]["phase"]), (200, "loaded"))
        self.assertEqual(self.calls, [], "the manager's heartbeat never reaches LM Studio")
        ask("/api/progress?look=1")
        self.assertEqual(self.calls, ["/api/v1/models"], "a page that is open does")
        ask("/api/progress?look=1")
        self.assertEqual(len(self.calls), 1, "and polling every two seconds, once a minute")
        recall._set(checked=0.0)
        ask("/api/recall")
        self.assertEqual(len(self.calls), 2, "asking for the memory's state outright checks too")

    def test_the_page_says_it_is_looking_on_every_poll(self):
        page = (home.CODE / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('getJson("api/progress?look=1")', page)
        self.assertNotIn('getJson("api/progress")', page)


if __name__ == "__main__":
    unittest.main()
