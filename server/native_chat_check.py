"""Explicit local acceptance of the permanent profile, without a model turn.

Run manually as the trusted room host. Never imported by room startup. Exercises
native commands against synthetic data; does not install or reconfigure Codex.
"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import uuid
from unittest.mock import patch

from . import app, codex_backend, home, native_tools as native, people, providers
from .native_boundary_probe import ProbeServer


class CheckServer(ProbeServer):
    def request(self, method, params):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params})
        while True:
            message = self.events.get(timeout=45)
            if message is None:
                raise RuntimeError("Local acceptance app-server closed")
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "No escalation"}})
            elif message.get("id") == self.counter:
                if message.get("error"):
                    raise RuntimeError(native.redact(str(message["error"].get("message", "RPC refused"))))
                return message.get("result") or {}


def handshake():
    result = {"model_turns": 0}
    class HeldClient(codex_backend.Client):
        def request(self, method, params):
            got = super().request(method, params)
            if method == "account/read":
                result["subscription_available"] = (got.get("account") or {}).get("type") == "chatgpt"
            if method == "thread/start":
                result["native_thread_accepted"] = True
                result["profile"] = params.get("permissions")
            return got

        def send(self, message):
            if message.get("method") == "turn/start":
                raise RuntimeError("Held before model start")
            return super().send(message)

    with patch.object(codex_backend, "Client", HeldClient), native.activated():
        try:
            codex_backend.call(providers.resolve("codex/gpt-5.6-sol"), "Local acceptance", "{}",
                {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]})
        except Exception as exc:
            result["held"] = "Held before model start" in str(exc)
            if not result["held"]:
                result["error"] = native.redact(str(exc))
    return result


def run():
    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="assistant-chat-check-")))
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="assistant-native-check-", dir=Path.home())))
        # Intentionally outside Documents: this checks the granted account home.
        folder = Path.home() / ("Assistant Native Acceptance-" + uuid.uuid4().hex)
        folder.mkdir()
        def cleanup():
            if folder.resolve().parent != Path.home().resolve():
                raise RuntimeError("Acceptance cleanup escaped its named folder")
            shutil.rmtree(folder)
        stack.callback(cleanup)
        fixture = folder / "sample.txt"
        fixture.write_text("value=1", encoding="utf-8")
        private = folder / "synthetic-auth"
        private.mkdir()
        secret = private / "secret.txt"
        secret.write_text("synthetic-file-secret", encoding="utf-8")
        (temp / "trusted-auth.txt").write_text("synthetic-temporary-auth", encoding="utf-8")
        (private / (home.OWNER + ".token")).write_text("synthetic-pair-key", encoding="utf-8")
        stack.enter_context(patch.object(people, "TOKEN_DIR", private))
        stack.enter_context(patch.object(native, "protected", return_value=native.protected() + [private]))
        class Handler(app.Handler):
            def log_message(self, *args): pass
            def _state(self, *args): return {"who": self._who}
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        stack.callback(http.server_close)
        stack.callback(http.shutdown)
        args, env = native.launch_options(scratch)
        env.update(ASSISTANT_TEST_SECRET="synthetic-env-secret", OPENAI_API_KEY="synthetic-not-an-api-key")
        server = CheckServer([codex_backend.executable(), "app-server", "--stdio", *args], env, folder)
        stack.callback(server.close)
        ready = server.request("windowsSandbox/readiness", {})
        if ready.get("status") != "ready":
            return {"ready_for_live_acceptance": False, "readiness": ready, "model_turns": 0}
        ps = env["SystemRoot"] + "/System32/WindowsPowerShell/v1.0/powershell.exe"
        checks = {}
        def literal(path): return "'" + str(path).replace("'", "''") + "'"
        def command(name, script, cmd=False):
            command = [env["COMSPEC"], "/d", "/c", script] if cmd else [ps, "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script]
            reply = server.request("command/exec", {"command": command, "cwd": str(folder),
                "permissionProfile": native.PROFILE, "timeoutMs": 10000})
            checks[name] = {"command": command, "cwd": str(folder), "exitCode": reply["exitCode"],
                            "stdout": reply["stdout"], "stderr": reply["stderr"]}
        command("read", "if((Get-Content -Raw -LiteralPath " + literal(fixture) + ") -ne 'value=1'){exit 2}; 'READ_OK'")
        command("edit", "Set-Content -NoNewline -LiteralPath " + literal(fixture) + " -Value 'value=2'; 'EDIT_OK'")
        command("verify", "if((Get-Content -Raw -LiteralPath " + literal(fixture) + ") -ne 'value=2'){exit 3}; 'VERIFY_OK'")
        named_roots = {name: native.ACCOUNT_HOME / name for name in
                       ("Desktop", "Downloads", "Documents", "Pictures")}
        for name, root in named_roots.items():
            check_dir = root / ("Assistant Access Check-" + uuid.uuid4().hex)
            check_dir.mkdir()
            def remove_check(path=check_dir, parent=root):
                if path.resolve().parent != parent.resolve():
                    raise RuntimeError("Named-root cleanup escaped " + str(parent))
                shutil.rmtree(path)
            stack.callback(remove_check)
            target = check_dir / "access.txt"
            target.write_text("value=1", encoding="utf-8")
            command(name.lower(), "Set-Content -NoNewline -LiteralPath " + literal(target) +
                    " -Value 'value=2'; if((Get-Content -Raw -LiteralPath " + literal(target) +
                    ") -ne 'value=2'){exit 4}; '" + name.upper() + "_OK'")
        command("failing", "echo EXPECTED_STDERR 1>&2 & exit /b 7", cmd=True)
        command("cmd", "echo CMD_OK", cmd=True)
        command("credential_denied", "$null=Get-Content -Raw -LiteralPath " + literal(secret) + "; 'UNSAFE'")
        command("trusted_temp_denied", "$null=Get-Content -Raw -LiteralPath " + literal(temp / "trusted-auth.txt") + "; 'UNSAFE'")
        command("environment", "if($env:ASSISTANT_TEST_SECRET -or $env:OPENAI_API_KEY -or $env:CODEX_API_KEY -or $env:CODEX_HOME -or $env:CODEX_SQLITE_HOME -or $env:ANTHROPIC_API_KEY){throw 'Trusted environment leaked'}; 'ENV_OK'")
        for name, path in (("room_denied", "/api/state"), ("pair_denied", "/api/pair"), ("log_denied", "/api/native-log?row=1")):
            command(name, "try {$null=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:" + str(http.server_address[1]) + path + "'; throw 'Unauthenticated access'} catch {if([int]$_.Exception.Response.StatusCode -ne 403){throw}; 'HTTP_403'}")
        for name, url in (("network_http", "http://example.com"), ("network_https", "https://example.com")):
            command(name, "curl.exe --fail --silent --show-error --max-time 8 --output NUL " + url + "; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}; 'NETWORK_OK'")
        native_thread = handshake()
        required = ("read", "edit", "verify", "desktop", "downloads", "documents",
                    "pictures", "cmd", "environment", "room_denied", "pair_denied",
                    "log_denied", "network_http")
        passed = all(checks[n]["exitCode"] == 0 for n in required)
        passed &= checks["failing"]["exitCode"] == 7
        passed &= all(checks[n]["exitCode"] != 0 and "UnauthorizedAccess" in checks[n]["stderr"] for n in ("credential_denied", "trusted_temp_denied"))
        passed &= all(native_thread.get(k) for k in ("held", "subscription_available", "native_thread_accepted"))
        return {"ready_for_live_acceptance": bool(passed), "model_turns": 0, "profile": native.PROFILE,
                "home_folder_granted": str(native.ACCOUNT_HOME), "outside_documents": True,
                "checks": checks, "handshake": native_thread,
                "codex_version": subprocess.run([codex_backend.executable(), "--version"], capture_output=True, text=True).stdout.strip()}


if __name__ == "__main__":
    try:
        report = run()
    except Exception as exc:
        report = {"ready_for_live_acceptance": False, "model_turns": 0,
                  "blocked": native.redact(str(exc))}
    Path("NATIVE_PERMANENT_WINDOWS.json").write_text(json.dumps(native.clean(report), indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ready": report["ready_for_live_acceptance"], "model_turns": report["model_turns"],
        "checks": {k: {f: v[f] for f in ("exitCode", "stdout", "stderr")} for k, v in report.get("checks", {}).items()},
        "handshake": report.get("handshake"), "blocked": report.get("blocked")}, indent=2))
    raise SystemExit(0 if report["ready_for_live_acceptance"] else 1)
