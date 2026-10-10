"""A failed search must say why, and must not read like an empty one.

Wants nothing (unittest). The claude tool is faked; no network, no cost.
"""
import json
import subprocess
import unittest
from unittest import mock

from . import brain, web


def _run_returning(envelope, stderr=""):
    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), stderr)
    return fake


class SearchFailureIsExplained(unittest.TestCase):
    def search(self, envelope, stderr=""):
        with mock.patch.object(brain, "find_claude", return_value="claude"), \
                mock.patch.object(web.subprocess, "run",
                                  _run_returning(envelope, stderr)):
            return web.run({"op": "search", "query": "boiling point of water"})

    def test_error_flagged_success_carries_the_tools_reason(self):
        r = self.search({"subtype": "success", "is_error": True,
                         "total_cost_usd": 0, "result": "Not logged in"})
        self.assertIn("Not logged in", r["refused"])
        self.assertIn("flagged an error", r["refused"])

    def test_no_reason_is_said_plainly(self):
        r = self.search({"subtype": "success", "is_error": True, "total_cost_usd": 0})
        self.assertIn("no reason", r["refused"])

    def test_good_answer_still_comes_through(self):
        body = json.dumps({"answer": "100 degrees Celsius.", "results": []})
        r = self.search({"subtype": "success", "is_error": False,
                         "total_cost_usd": 0.04, "result": body})
        self.assertNotIn("refused", r)
        self.assertIn("100 degrees", r["quoted_answer"])


if __name__ == "__main__":
    unittest.main()
