"""The one-shot installer and the Codex memory plug, on made-up homes.

Wants nothing: every folder is a temporary one, Codex's included (CODEX_HOME),
and the identity is the test household's. Run: python -m server.test_install
"""
import json
import os
import string
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from . import codex_mcp, home, install

LEGACY_TOML = """model = "gpt"

[mcp_servers.other]
command = "x"

[mcp_servers.ada_memory]
command = 'C:\\old\\python.exe'
args = ["-B", 'C:\\old\\codex_memory_mcp.py', "--root", '{root}']

[mcp_servers.ada_memory.env]
A = "1"

[desktop]
theme = "dark"
"""


def make_home(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "data").mkdir(exist_ok=True)
    (folder / "identity.json").write_text(json.dumps(home.TEST_IDENTITY), encoding="utf-8")
    return folder


class CodexRegistration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = make_home(base / "ada")
        self.codex = base / "codex"
        self.codex.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.codex)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def legacy(self):
        (self.codex / "config.toml").write_text(LEGACY_TOML.format(root=self.home), encoding="utf-8")
        old = f"C:\\old\\python.exe -B C:\\old\\codex_memory_mcp.py --root {self.home} --hook --allow-task task-123"
        other = {"matcher": "startup", "hooks": [{"type": "command", "command": "somebody else"}]}
        mine = {"matcher": "startup", "hooks": [{"type": "command", "command": old}]}
        (self.codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [other, mine]}}),
                                               encoding="utf-8")

    def test_replaces_the_hand_made_install_and_keeps_the_rest(self):
        self.legacy()
        self.assertTrue(install.register_codex(self.home))
        toml = (self.codex / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(toml.count("[mcp_servers.ada_memory]"), 1)
        self.assertNotIn("codex_memory_mcp.py", toml)
        self.assertNotIn("[mcp_servers.ada_memory.env]", toml)
        for kept in ("[mcp_servers.other]", "[desktop]", 'model = "gpt"', 'theme = "dark"'):
            self.assertIn(kept, toml)
        self.assertIn(json.dumps(str(install.PLUG)), toml)
        hooks = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        cmds = [h["command"] for e in hooks for h in e["hooks"]]
        self.assertIn("somebody else", cmds)
        self.assertEqual(sum("codex_mcp.py" in c for c in cmds), 1)
        self.assertFalse(any("codex_memory_mcp.py" in c for c in cmds))
        allow = json.loads((self.home / "data" / "codex.json").read_text(encoding="utf-8"))
        self.assertEqual(allow["allow_tasks"], ["task-123"])
        self.assertTrue((self.codex / "config.toml.before-install").exists())

    def test_a_second_run_changes_nothing(self):
        self.legacy()
        install.register_codex(self.home)
        first = ((self.codex / "config.toml").read_text(encoding="utf-8"),
                 (self.codex / "hooks.json").read_text(encoding="utf-8"))
        install.register_codex(self.home)
        second = ((self.codex / "config.toml").read_text(encoding="utf-8"),
                  (self.codex / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(first, second)

    def test_fresh_codex_gets_one_server_and_one_hook(self):
        self.assertTrue(install.register_codex(self.home))
        toml = (self.codex / "config.toml").read_text(encoding="utf-8")
        self.assertTrue(toml.startswith("[mcp_servers.ada_memory]"))
        hooks = json.loads((self.codex / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(hooks["hooks"]["SessionStart"]), 1)
        self.assertIn("Ada", hooks["hooks"]["SessionStart"][0]["hooks"][0]["statusMessage"])

    def test_no_codex_folder_is_reported_not_made(self):
        self.env.stop()
        with patch.dict(os.environ, {"CODEX_HOME": str(Path(self.tmp.name) / "none")}):
            self.assertFalse(install.register_codex(self.home))
        self.env.start()
        self.assertFalse((Path(self.tmp.name) / "none").exists())


class Memories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name) / "ada")

    def tearDown(self):
        self.tmp.cleanup()

    def zip_with(self, names):
        z = Path(self.tmp.name) / "b.zip"
        with zipfile.ZipFile(z, "w") as w:
            for n, body in names.items():
                w.writestr(n, body)
        return z

    def test_unpack_adds_and_never_replaces(self):
        (self.home / "data" / "spark.md").write_text("tracked", encoding="utf-8")
        z = self.zip_with({"data/spark.md": "older", "data/store.db": "db", "data/pictures/a.png": "p",
                           "what-is-in-here.json": "{}"})
        added, kept = install._unpack(z, self.home)
        self.assertEqual((added, kept), (2, 1))
        self.assertEqual((self.home / "data" / "spark.md").read_text(encoding="utf-8"), "tracked")
        self.assertFalse((self.home / "what-is-in-here.json").exists())

    def test_unpack_refuses_a_path_outside_the_home(self):
        z = self.zip_with({"../outside.txt": "x"})
        with self.assertRaises(RuntimeError):
            install._unpack(z, self.home)
        self.assertFalse((Path(self.tmp.name) / "outside.txt").exists())

    def test_a_missing_drive_lets_the_models_folder_go(self):
        free = next(f"{c}:\\" for c in reversed(string.ascii_uppercase) if not Path(f"{c}:\\").exists())
        path = self.home / "data" / "models.json"
        path.write_text(json.dumps({"folder": free + "Models", "note": "n"}), encoding="utf-8")
        install.fix_models_folder(self.home)
        got = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(got["folder"])
        self.assertEqual(got["note"], "n")

    def test_a_present_drive_keeps_the_models_folder(self):
        path = self.home / "data" / "models.json"
        here = str(Path(self.tmp.name) / "Models")
        path.write_text(json.dumps({"folder": here}), encoding="utf-8")
        install.fix_models_folder(self.home)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["folder"], here)


class RestoreKit(unittest.TestCase):
    def test_puts_back_only_what_is_missing_and_skips_the_old_plug(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            h = make_home(t / "ada")
            kit = h / "restore"
            for rel, body in {"dot-claude/CLAUDE.md": "kit", "dot-claude/new.md": "new",
                              "dot-codex/hooks.json": "{}", "dot-codex/ada-memory/x.py": "old",
                              "dot-codex/AGENTS.md": "a"}.items():
                (kit / rel).parent.mkdir(parents=True, exist_ok=True)
                (kit / rel).write_text(body, encoding="utf-8")
            claude, codex = t / "claude", t / "codex"
            claude.mkdir()
            (claude / "CLAUDE.md").write_text("mine", encoding="utf-8")
            places = {"dot-claude": lambda: claude, "dot-codex": lambda: codex}
            with patch.dict(install.KIT_PLACES, places, clear=True):
                install.put_back_kit(h)
            self.assertEqual((claude / "CLAUDE.md").read_text(encoding="utf-8"), "mine")
            self.assertEqual((claude / "new.md").read_text(encoding="utf-8"), "new")
            self.assertTrue((codex / "AGENTS.md").exists())
            self.assertFalse((codex / "hooks.json").exists())
            self.assertFalse((codex / "ada-memory").exists())


class ProjectFolders(unittest.TestCase):
    """Claude's per-project folders follow the Windows user name."""

    NEW = Path("C:/Users/Asus ROG")

    def test_the_name_claude_gives(self):
        self.assertEqual(install.claude_name(r"C:\Users\Asus ROG\Documents"), "C--Users-Asus-ROG-Documents")

    def test_alias_follows_the_current_user(self):
        with patch.object(install, "home_dir", lambda: self.NEW):
            self.assertEqual(install.project_alias("C--Users-Old-Documents-GitHub-x"),
                             "C--Users-Asus-ROG-Documents-GitHub-x")
            self.assertEqual(install.project_alias("C--Users-Old-Documents"), "C--Users-Asus-ROG-Documents")
            self.assertEqual(install.project_alias("C--Users-Asus-ROG-Documents-x"), "")
            self.assertEqual(install.project_alias("some-other-folder"), "")
            # a recorded prefix settles a user name that holds dashes
            self.assertEqual(install.project_alias("C--Users-A-Documents-B-Documents-x", "C--Users-A-Documents-B"),
                             "C--Users-Asus-ROG-Documents-x")

    def test_kit_copies_projects_under_this_user_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            h = make_home(t / "ada")
            kit = h / "restore" / "dot-claude" / "projects" / "C--Users-Old-Documents-GitHub-x" / "memory"
            kit.mkdir(parents=True)
            (kit / "MEMORY.md").write_text("kit", encoding="utf-8")
            (kit / "b.md").write_text("b", encoding="utf-8")
            claude = t / "claude"
            twin = claude / "projects" / "C--Users-Asus-ROG-Documents-GitHub-x" / "memory"
            twin.mkdir(parents=True)
            (twin / "MEMORY.md").write_text("mine", encoding="utf-8")
            with patch.dict(install.KIT_PLACES, {"dot-claude": lambda: claude}, clear=True), \
                    patch.object(install, "home_dir", lambda: self.NEW):
                install.put_back_kit(h)
                install.put_back_kit(h)    # and again: nothing changes
            self.assertEqual((twin / "MEMORY.md").read_text(encoding="utf-8"), "mine")
            self.assertEqual((twin / "b.md").read_text(encoding="utf-8"), "b")
            old = claude / "projects" / "C--Users-Old-Documents-GitHub-x" / "memory"
            self.assertEqual((old / "MEMORY.md").read_text(encoding="utf-8"), "kit")


class Plug(unittest.TestCase):
    def test_names_come_from_the_home(self):
        names = [t["name"] for t in codex_mcp.tools()]
        self.assertEqual(names, ["ada_context", "ada_search", "ada_fetch", "ada_tasks",
                                 "ada_record", "ada_remember"])
        self.assertIn(home.NAME, codex_mcp.instructions())
        speakers = codex_mcp.tools()[4]["inputSchema"]["properties"]["attribution"]["properties"]["speaker"]["enum"]
        self.assertEqual(speakers, list(home.HOUSEHOLD) + [home.SELF, "unknown"])

    def test_initialize_and_unknown_tool(self):
        got = codex_mcp.rpc(home.CODE, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(got["result"]["serverInfo"]["name"], "ada-memory")
        bad = codex_mcp.rpc(home.CODE, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "nope", "arguments": {}}})
        self.assertTrue(bad["result"]["isError"])

    def test_hook_stays_out_of_other_projects(self):
        with tempfile.TemporaryDirectory() as t:
            got = codex_mcp.hook(t, {"hook_event_name": "SessionStart", "cwd": str(Path.home()),
                                     "session_id": "s"})
            self.assertEqual(got, {})

    def test_hook_lets_in_a_task_the_home_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            h = make_home(Path(t) / "ada")
            (h / "data" / "codex.json").write_text(json.dumps({"allow_tasks": ["task-9"]}), encoding="utf-8")
            got = codex_mcp.hook(h, {"hook_event_name": "SessionStart", "cwd": str(Path.home()),
                                     "session_id": "task-9"})
            self.assertNotEqual(got, {})

    def test_run_as_a_file_it_serves_the_home_it_was_given(self):
        with tempfile.TemporaryDirectory() as t:
            ident = dict(home.TEST_IDENTITY, name="Bea", slug="bea")
            h = Path(t) / "bea"
            (h / "data").mkdir(parents=True)
            (h / "identity.json").write_text(json.dumps(ident), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "ASSISTANT_HOME"}
            p = subprocess.run([sys.executable, "-B", str(install.PLUG), "--root", str(h)],
                               input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                               capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
            tools = json.loads(p.stdout.splitlines()[0])["result"]["tools"]
            self.assertEqual(tools[0]["name"], "bea_context")


if __name__ == "__main__":
    unittest.main()
