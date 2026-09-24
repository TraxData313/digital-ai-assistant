"""Claude Code sessions, leaned on with a pretend `claude`: nothing is started
and no model is called. Wants nothing:

    python -m server.test_claude_sessions
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import brain, claude_sessions as cs, overmind

SID = "11111111-2222-3333-4444-555555555555"


class Pretend:
    """Answers the way Claude Code does, and remembers what it was asked."""

    def __init__(self, folder):
        self.folder = folder
        self.calls = []
        self.rows = []

    def __call__(self, args, cwd=None, timeout=60):
        self.calls.append((list(args), cwd))
        out = ""
        if args[:1] == ["agents"]:
            out = json.dumps(self.rows)
        elif args[:1] == ["--bg"]:
            out = "backgrounded · abc12345 · a title\n"
            self.rows = [{"id": "abc12345", "pid": 7, "sessionId": SID, "cwd": str(cwd),
                          "kind": "background", "name": "a title", "status": "busy"}]
        elif args[:1] == ["stop"]:
            out = "stopped " + args[1]
            for row in self.rows:
                row.pop("pid", None)
        elif args[:2] == ["auth", "status"]:
            out = json.dumps({"loggedIn": True})
        return subprocess.CompletedProcess(args, 0, out, "")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.projects = root / "projects"
        (self.projects / "C--work").mkdir(parents=True)
        self.work = root / "work"
        self.work.mkdir()
        self.fake = Pretend(self.work)
        for p in (patch.object(cs, "STATE", root / "claude_sessions.json"),
                  patch.object(cs, "PROJECTS", self.projects),
                  patch.object(cs, "_run", self.fake),
                  patch.object(cs, "NEXT_POLL", 0.0),
                  patch.object(cs.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.temp.cleanup()

    def transcript(self, *entries):
        with open(self.projects / "C--work" / (SID + ".jsonl"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def op(self, **kw):
        base = {k: None for k in cs.OP["required"]}
        base.update(kw)
        return base

    def test_start_passes_everything_through_and_follows(self):
        got = cs.execute(self.op(op="start", title="a title", prompt="do the thing",
                                 folder=str(self.work), model="opus", effort="max"))
        args, cwd = self.fake.calls[0]
        self.assertEqual(args[:1], ["--bg"])
        for flag, value in (("-n", "a title"), ("--model", "opus"), ("--effort", "max"),
                            ("--permission-mode", "auto"), ("--remote-control", "a title")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertTrue(args[-1].endswith(": do the thing"))
        self.assertEqual(cwd, str(self.work))
        self.assertEqual(got["session"], SID)
        self.assertTrue(cs._load()["sessions"][SID]["following"])

    def test_a_turn_ending_wakes_it_once(self):
        cs.execute(self.op(op="start", title="a title", prompt="go", folder=str(self.work)))
        self.transcript({"type": "user", "uuid": "u1", "message": {"content": "go"}},
                        {"type": "assistant", "uuid": "a1", "message": {"content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
                        {"type": "assistant", "uuid": "a2", "message": {"content": [
                            {"type": "text", "text": "done, all good"}]}})
        self.assertIsNone(cs.due(), "still working")
        self.fake.rows[0]["status"] = "idle"
        cs.NEXT_POLL = 0.0
        woke = cs.due()
        self.assertEqual(woke["by"], "claude")
        self.assertEqual(woke["events"][0]["detail"]["reply"], "done, all good")
        cs.acknowledge(woke, True)
        cs.NEXT_POLL = 0.0
        self.assertIsNone(cs.due(), "the same reply does not wake it twice")

    def test_a_send_mid_turn_waits_for_the_turn_to_end(self):
        cs.execute(self.op(op="start", title="a title", prompt="go", folder=str(self.work)))
        got = cs.execute(self.op(op="send", session=SID, prompt="and then this"))
        self.assertIn("queued", got)
        self.fake.rows[0]["status"] = "idle"
        cs.NEXT_POLL = 0.0
        cs.due()
        sent = [a for a, _ in self.fake.calls if a[:2] == ["--bg", "--resume"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], SID)
        self.assertTrue(sent[0][-1].endswith("and then this"))
        self.assertIn(["stop", "abc12345"], [a for a, _ in self.fake.calls])

    def test_messages_read_like_a_conversation(self):
        self.transcript({"type": "user", "uuid": "u1", "message": {"content": "hello"}},
                        {"type": "user", "uuid": "u2", "message": {"content": [
                            {"type": "tool_result", "content": "noise"}]}},
                        {"type": "assistant", "uuid": "a1", "message": {"content": [
                            {"type": "thinking", "thinking": "hm"},
                            {"type": "text", "text": "hi"}]}})
        msgs = cs.messages(SID)
        self.assertEqual([(m["role"], m.get("text")) for m in msgs],
                         [("you", "hello"), ("claude", "hi")])

    def test_each_switch_takes_its_whole_side_away(self):
        with patch.object(overmind, "offered", return_value=False):
            cs.configure({"action": "pause"})
            self.assertNotIn("claude", brain.response_schema()["properties"])
            self.assertNotIn("## Claude Code sessions", brain.SESSION_INSTRUCTIONS())
            self.assertNotIn("## Codex tasks", brain.SESSION_INSTRUCTIONS())
            self.assertIsNone(cs.due())
            cs.configure({"action": "resume"})
            self.assertIn("claude", brain.response_schema()["properties"])
            self.assertIn("## Claude Code sessions", brain.SESSION_INSTRUCTIONS())
        with patch.object(overmind, "offered", return_value=True):
            self.assertIn("codex", brain.response_schema()["properties"])


if __name__ == "__main__":
    unittest.main()
