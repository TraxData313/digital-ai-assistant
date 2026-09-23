"""Assess model-free probe evidence without model calls or live room changes."""
import unittest
from .native_trust_probe import assessment


class TrustProbeTests(unittest.TestCase):
    def test_missing_or_inherited_access_cannot_be_called_supported(self):
        self.assertFalse(assessment({})["requested_scope_supported"])
        checks = {name: {"exit": 0} for name in (
            "documents_read", "documents_edit", "documents_assert", "grant", "environment", "network")}
        denied = {"exit": 1, "error": "UnauthorizedAccessException"}
        checks.update({name: dict(denied) for name in (
            "outside_denied", "secret_denied", "fresh_process_revocation", "explicit_revocation")})
        self.assertTrue(assessment(checks)["requested_scope_supported"])
        checks["fresh_process_revocation"] = {"exit": 0, "output": "READ_ALLOWED"}
        result = assessment(checks)
        self.assertFalse(result["requested_scope_supported"])
        self.assertTrue(result["explicit_deny_supported"])

    def test_arbitrary_command_failure_is_not_proof_of_access_denial(self):
        result = assessment({"explicit_revocation": {"exit": 1, "error": "unknown profile"}})
        self.assertFalse(result["explicit_deny_supported"])


if __name__ == "__main__":
    unittest.main()
