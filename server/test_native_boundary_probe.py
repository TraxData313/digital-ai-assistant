"""Model-free diagnostic tests; none enables native tools or touches live data."""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from . import native_boundary_probe as probe


class BoundaryProbeTests(unittest.TestCase):
    def test_exit_zero_without_actual_marker_is_not_evidence(self):
        self.assertFalse(probe.accepted(subprocess.CompletedProcess([], 0, "", ""), "OK"))
        self.assertFalse(probe.accepted(subprocess.CompletedProcess([], 1, "OK\n", ""), "OK"))
        self.assertFalse(probe.accepted(subprocess.CompletedProcess([], 0, "NOT_OK\n", ""), "OK"))
        self.assertTrue(probe.accepted(subprocess.CompletedProcess([], 0, "OK\r\n", ""), "OK"))

    def test_launcher_environment_does_not_inherit_arbitrary_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "ANTHROPIC_API_KEY": "synthetic",
                                     "UNRELATED_PRIVATE_VALUE": "synthetic", "PYTHONPATH": "untrusted"}):
            env = probe.command_environment(Path("C:/probe"))
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "UNRELATED_PRIVATE_VALUE", "PYTHONPATH"):
            self.assertNotIn(key, env)
        self.assertEqual(env["TEMP"], str(Path("C:/probe/workspace/scratch")))
        self.assertEqual(env["CODEX_HOME"], str(Path("C:/probe/trusted")))

    def test_policy_has_no_broad_reads_or_network_and_no_approval_escalation(self):
        config = tomllib.loads(probe.policy("elevated", probe.command_environment(Path("C:/probe"))))
        self.assertEqual(config["approval_policy"], "never")
        profile = config["permissions"]["assistant-fixture"]
        self.assertEqual(profile["filesystem"][":root"], "deny")
        self.assertEqual(profile["filesystem"][":workspace_roots"], {".": "write"})
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(config["shell_environment_policy"]["inherit"], "none")
        self.assertNotIn("CODEX_HOME", config["shell_environment_policy"]["set"])

    def test_current_room_route_retains_secret_refusal_but_is_not_fixture_scoped(self):
        from . import files
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(files, "ROOT", Path(folder)), patch.object(files, "DEFAULT_ROOTS", []), \
                 patch.object(files, "REACH_PATH", Path(folder) / "absent.json"):
                result = probe.room_read_probe()
        self.assertTrue(result["fixture_readable"])
        self.assertTrue(result["synthetic_env_denied"])
        # This is a blocker assertion about today's unchanged route, not an
        # acceptance claim that the requested future boundary is implemented.
        self.assertFalse(result["outside_fixture_denied"])


if __name__ == "__main__":
    unittest.main()
