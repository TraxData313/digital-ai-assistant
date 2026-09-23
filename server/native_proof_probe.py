"""Model-free acceptance using the proof's production native configuration."""
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

from . import app, codex_backend, home, native_proof, people, files
from .native_boundary_probe import ProbeServer


def thread_handshake():
    """Exercise production subscription/thread setup; intercept BEFORE turn/start."""
    result = {"subscription_available": False, "native_thread_accepted": False, "model_turns": 0}
    class HeldClient(codex_backend.Client):
        def request(self, method, params):
            got = super().request(method, params)
            if method == "account/read":
                result["subscription_available"] = (got.get("account") or {}).get("type") == "chatgpt"
            if method == "thread/start":
                result["native_thread_accepted"] = True
            return got
        def send(self, message):
            if message.get("method") == "turn/start":
                raise RuntimeError("Model start held for the owner's live acceptance")
            return super().send(message)
    with patch.object(codex_backend, "Client", HeldClient), patch.object(native_proof, "fixture_text", return_value=""), native_proof.activated():
        try:
            from . import providers
            codex_backend.call(providers.resolve("codex/gpt-5.6-sol"), "Local protocol acceptance", "{}",
                {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]})
        except Exception as exc:
            result["held"] = "Model start held" in str(exc)
            if not result["held"]:
                result["error"] = native_proof.redact(str(exc))
    return result


def run():
    with ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="assistant-proof-check-")))
        folder = native_proof.DOCUMENTS / ("assistant-proof-check-" + uuid.uuid4().hex)
        folder.mkdir()
        def cleanup():
            if folder.resolve().parent != native_proof.DOCUMENTS.resolve():
                raise RuntimeError("Refusing fixture cleanup outside Documents")
            shutil.rmtree(folder)
        stack.callback(cleanup)
        fixture = folder / "fixture.txt"
        fixture.write_text("value=1", encoding="utf-8")
        secret = folder / "synthetic.token"
        secret.write_text("synthetic-only-not-a-real-secret")
        outside = scratch / "denied"
        outside.mkdir()
        (outside / "fixture.txt").write_text("synthetic-outside")
        # Explicit known deny, not a claim that all unlisted paths are denied.
        original = native_proof.protected()
        stack.enter_context(patch.object(native_proof, "protected", return_value=original + [outside]))
        tokens = scratch / "tokens"
        tokens.mkdir()
        (tokens / (home.OWNER + ".token")).write_text("synthetic-pair-key")
        stack.enter_context(patch.object(people, "TOKEN_DIR", tokens))
        class Handler(app.Handler):
            def log_message(self, *args):
                pass
            def _state(self, *args):
                return {"who": self._who}
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        stack.callback(http.server_close)
        stack.callback(http.shutdown)
        args, env = native_proof.launch_options(scratch)
        # Present only in the trusted process; never print values.
        env["ASSISTANT_TEST_SECRET"] = "synthetic-env-secret"
        env["OPENAI_API_KEY"] = "synthetic-not-a-key"
        server = ProbeServer([codex_backend.executable(), "app-server", "--stdio", *args], env, native_proof.DOCUMENTS)
        stack.callback(server.close)
        readiness = server.request("windowsSandbox/readiness", {})
        if readiness.get("status") != "ready":
            return {"ready_for_live_acceptance": False, "readiness": readiness, "model_turns": 0}
        ps = env["SystemRoot"] + "/System32/WindowsPowerShell/v1.0/powershell.exe"
        def literal(path):
            return "'" + str(path).replace("'", "''") + "'"
        checks = {}
        def command(name, script):
            reply = server.request("command/exec", {"command": [ps, "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; " + script], "cwd": str(native_proof.DOCUMENTS),
                "permissionProfile": "assistant-proof", "timeoutMs": 10000})
            checks[name] = {"exit": reply["exitCode"], "stdout": reply["stdout"].strip(),
                            "stderr": reply["stderr"].replace(str(scratch), "<scratch>").replace(str(folder), "<fixture>")}
        command("read", "$v=Get-Content -Raw -LiteralPath " + literal(fixture) + "; if($v -ne 'value=1'){exit 2}; 'READ_OK'")
        command("edit", "Set-Content -NoNewline -LiteralPath " + literal(fixture) + " -Value 'value=2'; 'EDIT_OK'")
        command("assertion", "if((Get-Content -Raw -LiteralPath " + literal(fixture) + ") -ne 'value=2'){exit 3}; 'FIXTURE_OK'")
        command("failing_assertion", "if((Get-Content -Raw -LiteralPath " + literal(fixture) + ") -ne 'value=3'){'EXPECTED_FAILURE'; exit 7}; exit 0")
        command("credential_denied", "$null=Get-Content -Raw -LiteralPath " + literal(secret) + "; 'UNSAFE'")
        command("explicit_outside_denied", "$null=Get-Content -Raw -LiteralPath " + literal(outside / 'fixture.txt') + "; 'UNSAFE'")
        command("environment", "if($env:ASSISTANT_TEST_SECRET -or $env:OPENAI_API_KEY -or $env:CODEX_API_KEY -or $env:CODEX_HOME -or $env:CODEX_SQLITE_HOME -or $env:ANTHROPIC_API_KEY){throw 'Unexpected trusted environment'}; 'ENV_OK'")
        port = str(http.server_address[1])
        command("room_denied", "try { $null=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:" + port + "/api/state'; throw 'Unauthenticated room access' } catch {if([int]$_.Exception.Response.StatusCode -ne 403){throw}; 'HTTP_403'}")
        command("pair_denied", "try { $null=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:" + port + "/api/pair'; throw 'Unauthenticated pairing access' } catch {if([int]$_.Exception.Response.StatusCode -ne 403){throw}; 'HTTP_403'}")
        command("network_http", "curl.exe --fail --silent --show-error --max-time 8 --output NUL http://example.com; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}; 'HTTP_OK'")
        command("network_https", "curl.exe --fail --silent --show-error --max-time 8 --output NUL https://example.com; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}; 'HTTPS_OK'")
        with native_proof.activated():
            for name, path in (("room_credential_route", secret), ("room_private_route", outside / "fixture.txt")):
                try:
                    files.read_file({"path": str(path)})
                    checks[name] = {"denied": False}
                except files.Refused:
                    checks[name] = {"denied": True}
        required = ("read", "edit", "assertion", "environment", "room_denied", "pair_denied", "network_http")
        good = all(checks[x]["exit"] == 0 for x in required)
        good &= checks["failing_assertion"]["exit"] == 7
        good &= all(checks[x]["exit"] != 0 and "UnauthorizedAccessError" in checks[x]["stderr"] for x in ("credential_denied", "explicit_outside_denied"))
        good &= all(checks[x]["denied"] for x in ("room_credential_route", "room_private_route"))
        handshake = thread_handshake()
        good &= handshake.get("held", False) and handshake["subscription_available"] and handshake["native_thread_accepted"]
        return {"ready_for_live_acceptance": good, "model_turns": 0, "handshake": handshake,
                "codex_version": subprocess.run([codex_backend.executable(), "--version"], capture_output=True, text=True).stdout.strip(),
                "exclusive_documents_boundary": False, "checks": checks}


if __name__ == "__main__":
    report = run()
    Path("NATIVE_EXPLICIT_TRUST_EVIDENCE.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ready_for_live_acceptance"] else 1)
