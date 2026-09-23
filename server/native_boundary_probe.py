"""Opt-in, model-free Windows boundary probe. Never imported by the room.

Only synthetic data is used. This does not install/configure the Windows sandbox,
read real authentication, or grant permission to run the assistant's native tools.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading


class ProbeServer:
    """Model-free app-server command probe; no threads or account requests."""
    def __init__(self, command, env, cwd):
        self.proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8",
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.events = queue.Queue()
        self.counter = 0
        def reader():
            for line in self.proc.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
            self.events.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {"name": "assistant_boundary_probe", "version": "0.1"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params})
        while True:
            message = self.events.get(timeout=45)
            if message is None:
                raise RuntimeError("Probe app-server closed")
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601,
                           "message": "Probe never grants requests"}})
            elif message.get("id") == self.counter:
                if message.get("error"):
                    raise RuntimeError("Probe RPC refused: " + method)
                return message.get("result") or {}

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.proc.wait(timeout=10)


def command_environment(root):
    """Trusted launcher environment; no inherited keys, tokens or shell profiles."""
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    scratch = root / "workspace" / "scratch"
    return {
        "SystemRoot": str(windows), "WINDIR": str(windows),
        "COMSPEC": str(windows / "System32" / "cmd.exe"),
        "PATH": str(windows / "System32") + os.pathsep +
                str(windows / "System32/WindowsPowerShell/v1.0"),
        "TEMP": str(scratch), "TMP": str(scratch),
        "USERPROFILE": str(root / "outside"), "HOME": str(root / "outside"),
        "CODEX_HOME": str(root / "trusted"),
        "CODEX_SQLITE_HOME": str(root / "trusted"),
    }


def policy(mode, env):
    # A fresh custom profile avoids inheriting broad reads or global temp writes.
    # Native tool commands must not inherit the trusted process's CODEX_HOME.
    child = {k: v for k, v in env.items() if k not in ("CODEX_HOME", "CODEX_SQLITE_HOME")}
    set_env = "\n".join(json.dumps(k) + " = " + json.dumps(v) for k, v in child.items())
    return '''approval_policy = "never"
default_permissions = "assistant-fixture"
[windows]
sandbox = "''' + mode + '''"
[permissions.assistant-fixture.filesystem]
":root" = "deny"
":minimal" = "read"
[permissions.assistant-fixture.filesystem.":workspace_roots"]
"." = "write"
[permissions.assistant-fixture.network]
enabled = false
[shell_environment_policy]
inherit = "none"
ignore_default_excludes = false
[shell_environment_policy.set]
''' + set_env + "\n"


def accepted(result, marker):
    return result.returncode == 0 and marker in result.stdout.splitlines()


def room_read_probe():
    """Exercise the existing route on synthetic files; do not change its policy."""
    from . import files
    with tempfile.TemporaryDirectory(prefix="assistant-room-boundary-", dir=files.ROOT) as folder:
        root = Path(folder)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "fixture.txt").write_text("fixture")
        outside = root / "outside.txt"
        outside.write_text("synthetic outside")
        secret = workspace / ".env"
        secret.write_text("synthetic secret")
        def readable(path):
            try:
                files.read_file({"path": str(path)})
                return True
            except files.Refused:
                return False
        return {"fixture_readable": readable(workspace / "fixture.txt"),
                "synthetic_env_denied": not readable(secret),
                "outside_fixture_denied": not readable(outside),
                "note": "Current room reach is broader than the proposed fixture; unchanged."}


def run_probe(mode="unelevated", installed_sandbox_home=False, transport="cli", network_proxy=False):
    if os.name != "nt":
        return {"ready": False, "reason": "Windows installation required", "checks": []}
    codex = shutil.which("codex")
    if not codex:
        return {"ready": False, "reason": "Installed codex executable unavailable", "checks": []}
    # This directory is outside the room/repository. It contains no real secrets.
    with tempfile.TemporaryDirectory(prefix="assistant-native-boundary-") as folder, ExitStack() as cleanup:
        root = Path(folder)
        for name in ("workspace/scratch", "trusted", "outside"):
            (root / name).mkdir(parents=True, exist_ok=True)
        canary = "SYNTHETIC_" + secrets.token_hex(16)
        credential = root / "trusted/auth.json"
        credential.write_text(json.dumps({"synthetic_test_secret": canary}))
        outside = root / "outside/secret.txt"
        outside.write_text(canary)
        fixture = root / "workspace/fixture.txt"
        fixture.write_text("value=1")
        env = command_environment(root)
        policy_text = policy(mode, env)
        if network_proxy:
            policy_text += "\n[features.network_proxy]\nenabled = true\nallow_local_binding = false\n"
        overrides = []
        if installed_sandbox_home:
            # Reuse installed sandbox setup via its supported configuration path.
            # Never read/copy auth or write config in this home. All overrides are
            # command-local; all test secrets remain disposable synthetic files.
            import tomllib
            installed = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            env["CODEX_HOME"] = str(installed)
            env["CODEX_SQLITE_HOME"] = str(root / "trusted")
            def toml_value(value):
                if isinstance(value, dict):
                    return "{" + ", ".join(json.dumps(k) + " = " + toml_value(v)
                                             for k, v in value.items()) + "}"
                return json.dumps(value)
            for key, value in tomllib.loads(policy_text).items():
                overrides.extend(["-c", key + "=" + toml_value(value)])
        else:
            (root / "trusted/config.toml").write_text(policy_text, encoding="utf-8")
        windows = Path(env["SystemRoot"])
        powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
        base = [codex, "sandbox"] + overrides + ["--permission-profile", "assistant-fixture",
                "--include-managed-config", "--cd", str(root / "workspace"), "--"]
        server = None
        if transport == "app-server":
            # Deliberately seed the trusted adapter environment with a synthetic
            # secret absent from shell_environment_policy.set. The child must
            # not inherit it; its value is never emitted.
            env["ASSISTANT_BOUNDARY_SECRET"] = canary
            server = ProbeServer([codex, "app-server", "--stdio"] + overrides,
                                 env, root / "workspace")
            cleanup.callback(server.close)
        effective = None
        if server:
            config = server.request("config/read", {"includeLayers": False}).get("config", {})
            effective = {"windows": config.get("windows"),
                         "profile_network": config.get("permissions", {}).get("assistant-fixture", {}).get("network"),
                         "approval_policy": config.get("approval_policy"),
                         "readiness": server.request("windowsSandbox/readiness", None)}
        checks = []

        def invoke(name, script, marker):
            try:
                command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                           "-Command", "$ErrorActionPreference='Stop'; " + script]
                if server:
                    reply = server.request("command/exec", {"command": command,
                        "cwd": str(root / "workspace"), "permissionProfile": "assistant-fixture",
                        "timeoutMs": 15000})
                    result = subprocess.CompletedProcess(command, reply["exitCode"],
                                                         reply["stdout"], reply["stderr"])
                else:
                    result = subprocess.run(base + command,
                        env=env, cwd=root / "workspace", stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                # Even synthetic credential contents are excluded from retained output.
                out = result.stdout.replace(canary, "[synthetic secret redacted]")
                err = result.stderr.replace(canary, "[synthetic secret redacted]")
                checks.append({"name": name, "passed": accepted(result, marker),
                               "exit_code": result.returncode, "stdout": out, "stderr": err})
                return checks[-1]["passed"]
            except (OSError, subprocess.TimeoutExpired, RuntimeError, queue.Empty) as exc:
                checks.append({"name": name, "passed": False, "error": type(exc).__name__})
                return False

        def literal(path):
            return "'" + str(path).replace("'", "''") + "'"

        trusted_read = canary in credential.read_text()
        if not invoke("sandbox_process_start", "Write-Output 'STARTED'", "STARTED"):
            return {"ready": False, "mode": mode, "codex": codex,
                    "trusted_synthetic_auth_read": trusted_read,
                    "reason": "Sandbox did not start the probe command; no boundary proven.",
                    "checks": checks,
                    "not_exercised": ["credential denial", "outside reads/writes", "environment",
                                      "command network", "fixture edit/assertion", "room file route"]}
        invoke("fixture_edit_assertion",
               "if ([IO.File]::ReadAllText('fixture.txt') -ne 'value=1') {throw 'read'}; "
               "[IO.File]::WriteAllText('fixture.txt','value=2'); "
               "if ([IO.File]::ReadAllText('fixture.txt') -ne 'value=2') {throw 'edit'}; "
               "[IO.File]::WriteAllText('scratch/probe.txt','scratch'); Write-Output 'FIXTURE_OK'",
               "FIXTURE_OK")
        # A deliberately false assertion must yield its actual failure/exit code.
        failure_script = ("try {if ([IO.File]::ReadAllText('fixture.txt') -ne 'value=3') "
                          "{throw 'EXPECTED_ASSERTION_FAILURE'}} catch {"
                          "Write-Output $_.Exception.Message; exit 7}; exit 0")
        invoke("failing_assertion", failure_script, "EXPECTED_ASSERTION_FAILURE")
        failed_assertion = checks[-1]
        failed_assertion["passed"] = (failed_assertion.get("exit_code") == 7 and
            "EXPECTED_ASSERTION_FAILURE" in failed_assertion.get("stdout", "").splitlines())
        for name, path in (("credential_read_denied", credential), ("outside_read_denied", outside)):
            invoke(name, "$denied=$false; try {$null=[IO.File]::ReadAllText(" + literal(path) +
                   ")} catch [UnauthorizedAccessException] {$denied=$true}; "
                   "if (!$denied) {throw 'read not denied'}; Write-Output 'DENIED'", "DENIED")
        invoke("outside_write_denied", "$denied=$false; try {[IO.File]::WriteAllText(" +
               literal(outside) + ",'changed')} catch [UnauthorizedAccessException] {$denied=$true}; "
               "if (!$denied) {throw 'write not denied'}; Write-Output 'DENIED'", "DENIED")
        invoke("sanitized_environment", "if ($env:OPENAI_API_KEY -or $env:CODEX_API_KEY -or "
               "$env:ANTHROPIC_API_KEY -or $env:ASSISTANT_BOUNDARY_SECRET) {throw 'inherited key'}; "
               "Write-Output 'ENV_OK'", "ENV_OK")
        # Listen only on loopback. No external request or model traffic is possible.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            port = listener.getsockname()[1]
            invoke("network_denied", "$c=New-Object Net.Sockets.TcpClient; $blocked=$false; "
                   "try {$t=$c.ConnectAsync('127.0.0.1'," + str(port) + "); "
                   "if (!$t.Wait(2000)) {$blocked=$true} else {$blocked=(!$c.Connected)}} "
                   "catch {$blocked=$true} finally {$c.Dispose()}; "
                   "if (!$blocked) {throw 'network connected'}; Write-Output 'NETWORK_DENIED'",
                   "NETWORK_DENIED")
            try:
                connection, _ = listener.accept()
                connection.close()
                checks[-1]["listener_accepted_connection"] = True
                checks[-1]["passed"] = False
            except TimeoutError:
                checks[-1]["listener_accepted_connection"] = False
        intact = canary in credential.read_text() and outside.read_text() == canary
        # CLI probes are necessary, not sufficient: app-server and legacy room reads
        # still require integration tests. A successful probe never enables the room.
        return {"ready": False, "mode": mode, "codex": codex,
                "transport": transport, "network_proxy_requested": network_proxy,
                "effective": effective,
                "local_checks_passed": intact and all(c["passed"] for c in checks),
                "trusted_synthetic_auth_read": trusted_read, "synthetic_secrets_intact": intact,
                "room_file_route": room_read_probe(),
                "reason": "Boundary is not approved; this diagnostic never enables native tools.",
                "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["unelevated", "elevated"], default="unelevated")
    parser.add_argument("--installed-sandbox-home", action="store_true",
                        help="Reuse existing Windows sandbox setup without editing its config/auth")
    parser.add_argument("--transport", choices=["cli", "app-server"], default="cli")
    parser.add_argument("--network-proxy", action="store_true")
    args = parser.parse_args()
    report = run_probe(args.mode, args.installed_sandbox_home, args.transport, args.network_proxy)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report.get("local_checks_passed") else 1)
