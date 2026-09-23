"""Model-free installed Windows permission tests. Does not enable room tools."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import uuid

from .native_boundary_probe import ProbeServer, command_environment


def assessment(checks):
    success = ("documents_read", "documents_edit", "documents_assert", "grant", "environment", "network")
    denied = ("outside_denied", "secret_denied", "fresh_process_revocation")
    passed = (all(checks.get(name, {}).get("exit") == 0 for name in success)
              and all(checks.get(name, {}).get("exit") == 1
                      and "UnauthorizedAccessException" in checks[name].get("error", "") for name in denied))
    return {"requested_scope_supported": passed,
            "explicit_deny_supported": checks.get("explicit_revocation", {}).get("exit") == 1
                and "UnauthorizedAccessException" in checks["explicit_revocation"].get("error", ""),
            "note": "A fresh client with only the base grant must not inherit access absent from its roots."}


def run():
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="assistant-trust-probe-")))
        documents = Path.home() / "Documents"
        # Windows tempfile.mkdtemp uses a private DACL. Use an ordinary inherited
        # Documents directory here, matching the requested folder-grant workflow.
        fixture = documents / ("assistant-trust-fixture-" + uuid.uuid4().hex)
        fixture.mkdir()
        def remove_fixture():
            if fixture.resolve().parent != documents.resolve():
                raise RuntimeError("Fixture cleanup escaped Documents")
            shutil.rmtree(fixture)
        stack.callback(remove_fixture)
        for name in ("workspace/scratch", "trusted", "outside", "extra"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env = command_environment(root)
        env["CODEX_HOME"] = str(Path.home() / ".codex")
        env["ASSISTANT_TEST_SECRET"] = "synthetic-only"
        secret = fixture / "synthetic.token"
        secret.write_text("synthetic-only")
        target = fixture / "fixture.txt"
        target.write_text("value=1")
        extra = root / "extra/item.txt"
        extra.write_text("extra")
        config = {
            "approval_policy": "never", "windows": {"sandbox": "elevated"},
            "default_permissions": "base", "permissions": {},
            "shell_environment_policy": {"inherit": "none", "set": {
                k: v for k, v in env.items()
                if k not in ("CODEX_HOME", "CODEX_SQLITE_HOME", "ASSISTANT_TEST_SECRET")}},
            "features": {"network_proxy": {"enabled": False}},
        }
        for name in ("base", "extra", "revoked"):
            fs = {":root": "deny", ":minimal": "read", str(documents): "write",
                  ":workspace_roots": {".": "write"},
                  str(secret): "deny", str(Path.home() / ".codex"): "deny",
                  str(root / "workspace/scratch"): "write"}
            if name != "base":
                fs[str(root / "extra")] = "write" if name == "extra" else "deny"
            config["permissions"][name] = {"filesystem": fs, "network": {"enabled": True}}

        def toml(value):
            if isinstance(value, dict):
                return "{" + ", ".join(json.dumps(k) + " = " + toml(v) for k, v in value.items()) + "}"
            return json.dumps(value)

        def arguments(cfg):
            args = [shutil.which("codex"), "app-server", "--stdio"]
            for key, value in cfg.items():
                args += ["-c", key + "=" + toml(value)]
            return args
        args = arguments(config)
        ps = env["SystemRoot"] + "/System32/WindowsPowerShell/v1.0/powershell.exe"

        def literal(path):
            return "'" + str(path).replace("'", "''") + "'"

        def command(server, profile, script):
            reply = server.request("command/exec", {
                "command": [ps, "-NoProfile", "-NonInteractive", "-Command",
                            "$ErrorActionPreference='Stop'; " + script],
                "cwd": str(documents), "permissionProfile": profile, "timeoutMs": 8000})
            # Test commands never print file contents or credential values.
            return {"exit": reply["exitCode"], "output": reply["stdout"].strip(),
                    "error": reply["stderr"].replace(str(root), "<temporary probe>")
                                                  .replace(str(fixture), "<Documents fixture>")}

        checks = {}
        server = ProbeServer(args, env, documents)
        try:
            checks["documents_read"] = command(server, "base", "$null=[IO.File]::ReadAllText(" + literal(target) + "); 'READ_OK'")
            checks["documents_edit"] = command(server, "base", "[IO.File]::WriteAllText(" + literal(target) + ",'value=2'); 'EDIT_OK'")
            checks["documents_assert"] = command(server, "base", "if ([IO.File]::ReadAllText(" + literal(target) + ") -ne 'value=2'){throw 'assertion'}; 'ASSERT_OK'")
            for name, profile in (("outside_denied", "base"), ("grant", "extra"),
                                  ("revocation", "base")):
                checks[name] = command(server, profile, "$null=[IO.File]::ReadAllText(" + literal(extra) + "); 'READ_ALLOWED'")
            checks["secret_denied"] = command(server, "base", "$null=[IO.File]::ReadAllText(" + literal(secret) + "); 'SECRET_ACCESSIBLE'")
            checks["environment"] = command(server, "base", "if ($env:ASSISTANT_TEST_SECRET){throw 'inherited secret'}; 'ENV_OK'")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(1)
                checks["network"] = command(server, "base", "$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1'," +
                    str(listener.getsockname()[1]) + "); $c.Dispose(); 'CONNECTED'")
                connection, _ = listener.accept()
                connection.close()
        finally:
            server.close()
        # This client has never selected or even defined the extra write grant.
        fresh_config = dict(config, permissions={key: value for key, value in config["permissions"].items()
                                               if key != "extra"})
        fresh = ProbeServer(arguments(fresh_config), env, documents)
        try:
            checks["fresh_process_revocation"] = command(fresh, "base", "$null=[IO.File]::ReadAllText(" + literal(extra) + "); 'READ_ALLOWED'")
            checks["explicit_revocation"] = command(fresh, "revoked", "$null=[IO.File]::ReadAllText(" + literal(extra) + "); 'READ_ALLOWED'")
        finally:
            fresh.close()
        return {"ready": False, "model_turns_started": 0,
                "codex_version": subprocess.run([shutil.which("codex"), "--version"],
                    capture_output=True, text=True).stdout.strip(),
                "assessment": assessment(checks), "checks": checks}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ready"] else 1)
