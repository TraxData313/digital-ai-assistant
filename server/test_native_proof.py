"""Local HTTP and mocked native protocol checks; never uses the live room."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from . import app, people, native_proof, native_tools
from . import codex_backend as backend, providers, files, db
from .test_codex_backend import FakeClient, event, SCHEMA


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        (folder / "sam.token").write_text("synthetic-sam")
        (folder / "lee.token").write_text("synthetic-lee")
        p = patch.object(people, "TOKEN_DIR", folder)
        p.start()
        self.addCleanup(p.stop)
        class Handler(app.Handler):
            def _state(self, *args):
                return {"who": self._who}
            def log_message(self, *args):
                pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def request(self, path, method="GET", body=None, token=None):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        headers = {"Cookie": app.Handler.COOKIE + "=" + token} if token else {}
        conn.request(method, path, json.dumps(body) if body else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_local_process_cannot_obtain_privilege_or_pairing_link(self):
        import re
        routes = set(re.findall(r'(?:path|self.path) == "([^"]+)"', Path(app.__file__).read_text(encoding="utf-8")))
        for path in routes | {"/", "/app.js", "/api/angel?token=made-up", "/pictures/fixture.png"}:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual((status, body), (403, b""))
                self.assertNotIn("Set-Cookie", headers)
        for path in routes:
            self.assertEqual(self.request(path, "POST", {"who": "sam", "action": "start"})[0], 403)
        self.assertEqual(self.request("/api/pair?k=wrong")[0], 403)

    def test_pairing_requires_authenticated_owner_and_loopback(self):
        self.assertEqual(self.request("/api/pair", token="synthetic-lee")[0], 403)
        with patch.object(people, "my_tailnet_address", return_value="100.64.1.2"):
            status, _, body = self.request("/api/pair", token="synthetic-sam")
        self.assertEqual(status, 200)
        self.assertIn("synthetic-sam", json.loads(body)["url"])
        status, headers, _ = self.request("/?k=synthetic-sam")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(json.loads(self.request("/api/state", token="synthetic-sam")[2]), {"who": "sam"})

    def test_feature_is_disabled_and_attribution_does_not_grant_it(self):
        with patch.object(native_proof, "available", return_value=False):
            self.assertEqual(self.request("/api/native-proof", "POST", {"action": "start"}, "synthetic-sam")[0], 409)
        with patch.object(native_proof, "available", return_value=True):
            self.assertEqual(self.request("/api/native-proof", "POST", {"action": "start", "who": "sam"}, "synthetic-lee")[0], 403)

    def test_query_credentials_are_redacted(self):
        self.assertNotIn("synthetic-sam", native_proof.redact('GET /?k=synthetic-sam HTTP/1.1'))
        self.assertNotIn("synthetic-angel", native_proof.redact('GET /api/angel?token=synthetic-angel HTTP/1.1'))
        self.assertNotIn("synthetic-sam", native_proof.redact('GET /?%6b=synthetic-sam HTTP/1.1'))

    def test_trusted_angel_and_tray_keep_authenticated_connections(self):
        from . import angel, tray
        with patch.object(angel, "PORT", self.http.server_address[1]):
            self.assertEqual(angel._get("/api/state"), {"who": "sam"})
        with patch.object(tray, "PORT", self.http.server_address[1]):
            self.assertEqual(tray.ask_the_room("/api/state"), {"who": "sam"})
        with patch("webbrowser.open") as opened:
            people.open_room(self.http.server_address[1])
        self.assertIn("?k=synthetic-sam", opened.call_args.args[0])

    def test_ipv6_loopback_requires_a_key_and_remote_peer_cannot_pair(self):
        import ipaddress
        handler = app.Handler.__new__(app.Handler)
        handler.path = "/api/state"
        handler.headers = {}
        for peer in ("::1", "127.0.0.1"):
            with patch.object(handler, "_peer", return_value=ipaddress.ip_address(peer)):
                self.assertEqual(handler._door(), ("", ""))
                handler.headers = {"Cookie": app.Handler.COOKIE + "=synthetic-sam"}
                self.assertEqual(handler._door(), ("sam", ""))
                handler.headers = {}
        handler.headers = {"Cookie": app.Handler.COOKIE + "=synthetic-sam"}
        with patch.object(handler, "_peer", return_value=ipaddress.ip_address("8.8.8.8")):
            self.assertEqual(handler._door(), ("", ""))


class NativeClient(FakeClient):
    readiness = "ready"
    def request(self, method, params):
        if method == "command/exec":
            self.requests.append((method, params))
            return {"exitCode": 0, "stdout": "", "stderr": ""}
        if method == "config/read":
            self.requests.append((method, params))
            return {"config": {"mcp_servers": {"test_server": {"command": "unused"}}}}
        if method == "windowsSandbox/readiness":
            self.requests.append((method, params))
            return {"status": self.readiness}
        return super().request(method, params)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        NativeClient.instances = []
        NativeClient.account = {"type": "chatgpt"}
        NativeClient.readiness = "ready"
        native_proof.CANCEL.clear()
        self.addCleanup(native_proof.CANCEL.clear)
        for p in (patch.object(backend, "Client", NativeClient),
                  patch.object(native_proof, "fixture_text", return_value="value=1\n")):
            p.start()
            self.addCleanup(p.stop)
        NativeClient.events = [
            event("turn/started", turn={"id": "turn-test"}),
            event("item/started", item={"type": "agentMessage", "id": "comment", "phase": "commentary"}),
            event("item/agentMessage/delta", itemId="comment", delta="I will test it."),
            event("item/completed", item={"type": "agentMessage", "id": "comment", "phase": "commentary", "text": "I will test it."}),
            event("item/started", item={"type": "commandExecution", "id": "cmd", "command": "powershell -NoProfile fixture assertion", "cwd": "C:/fixture", "status": "inProgress"}),
            event("item/commandExecution/outputDelta", itemId="cmd", delta="FIXTURE_OK\n"),
            event("item/completed", item={"type": "commandExecution", "id": "cmd", "command": "powershell -NoProfile fixture assertion", "cwd": "C:/fixture", "status": "completed", "exitCode": 0, "aggregatedOutput": "FIXTURE_OK\n"}),
            event("item/completed", item={"type": "fileChange", "id": "edit", "status": "completed", "changes": [{"path": "fixture.txt", "diff": "-value=1\n+value=2"}]}),
            event("turn/diff/updated", diff="-value=1\n+value=2"),
            event("item/completed", item={"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": '{"reply":"Test passed","drop":null}'}),
            event("turn/completed", turn={"status": "completed"}),
        ]
        self.steps = []

    def call(self):
        with native_proof.activated(), patch.object(providers, "key_of", side_effect=AssertionError("paid API fallback")):
            return providers.call(providers.resolve("codex/gpt-5.6-sol"), "Spark unchanged", "{}", SCHEMA,
                                  on_step=lambda *args: self.steps.append(args))

    def test_native_execution_and_final_json_are_separate(self):
        result = self.call()
        self.assertEqual(result["reply"], "Test passed")
        self.assertEqual(result["_meta"]["billing"], "subscription")
        params = dict(NativeClient.instances[-1].requests)["thread/start"]
        self.assertNotIn("environments", params)
        self.assertNotIn("sandbox", params)
        self.assertEqual(params["permissions"], "assistant-proof")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertTrue(params["ephemeral"])
        self.assertTrue(params["config"]["features.shell_tool"])
        self.assertFalse(params["config"]["mcp_servers.test_server.enabled"])
        activity = json.dumps(self.steps)
        for required in ("FIXTURE_OK", "exitCode", "C:/fixture", "value=2", "commentary"):
            self.assertIn(required, activity)
        self.assertTrue(NativeClient.instances[-1].closed)

    def test_malformed_final_retains_commands_output_and_failure(self):
        NativeClient.events[-2]["params"]["item"]["text"] = "malformed"
        with self.assertRaises(providers.TurnBroke):
            self.call()
        self.assertIn("FIXTURE_OK", json.dumps(self.steps))
        self.assertIn("did not produce a valid answer", json.dumps(self.steps))
        self.assertTrue(NativeClient.instances[-1].closed)

    def test_commentary_cannot_masquerade_as_final_json(self):
        NativeClient.events[-2]["params"]["item"]["phase"] = "commentary"
        with self.assertRaises(providers.TurnBroke):
            self.call()

    def test_lost_command_completion_has_unknown_exit_not_success(self):
        NativeClient.events = NativeClient.events[:6]
        with self.assertRaises(providers.TurnBroke):
            self.call()
        unknown = [s[2] for s in self.steps if len(s) > 2 and s[2].get("status") == "unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertIsNone(unknown[0]["exitCode"])
        self.assertIn("powershell", unknown[0]["command"])

    def test_failure_and_cancellation_are_retained(self):
        for status in ("failed", "interrupted"):
            self.steps.clear()
            NativeClient.events[-1]["params"]["turn"] = {"status": status}
            native_proof.CANCEL.set()
            with self.assertRaises(providers.TurnBroke):
                self.call()
            self.assertIn("Cancellation requested", json.dumps(self.steps))
            self.assertIn(status, json.dumps(self.steps))
            self.assertIn("turn/interrupt", [m["method"] for m in NativeClient.instances[-1].sent])

    def test_no_model_if_sandbox_unready_or_api_login(self):
        NativeClient.readiness = "notConfigured"
        with self.assertRaisesRegex(providers.TurnBroke, "not ready"):
            self.call()
        self.assertNotIn("thread/start", dict(NativeClient.instances[-1].requests))
        NativeClient.account = {"type": "apiKey"}
        with self.assertRaisesRegex(providers.TurnBroke, "subscription"):
            self.call()
        self.assertEqual(NativeClient.instances[-1].sent, [])

    def test_room_mutation_is_refused_before_answer_applied(self):
        for field in ("spark", "restart", "claude", "clock", "job", "project", "essences"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                native_proof.validate_answer({field: "change"})

    def test_approval_requests_declined_by_real_client_handler(self):
        import queue
        import time
        # setUp patches Client, so use the saved implementation reference.
        client = REAL_CLIENT.__new__(REAL_CLIENT)
        client.native = True
        client.deadline = time.monotonic() + 1
        client.events = queue.Queue()
        sent = []
        client.send = sent.append
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval", "account/chatgptAuthTokens/refresh"):
            client.events.put({"id": 1, "method": method, "params": {}})
            client.next()
        self.assertEqual([x.get("result") for x in sent[:2]], [{"decision": "decline"}] * 2)
        self.assertTrue(all("error" in x for x in sent[2:]))


REAL_CLIENT = backend.Client


class IsolationTests(unittest.TestCase):
    def test_environment_allowlist_and_explicit_native_denies(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(native_proof.os, "walk", return_value=[]), patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic", "UNRELATED_SECRET": "synthetic"}):
            cfg = native_proof.configuration(Path(tmp))
            args, env = native_proof.launch_options(Path(tmp))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertIn("CODEX_HOME", env)  # trusted adapter only
        child = cfg["shell_environment_policy"]
        self.assertEqual(child["inherit"], "none")
        self.assertNotIn("CODEX_HOME", child["set"])
        fs = cfg["permissions"]["assistant-proof"]["filesystem"]
        self.assertEqual(cfg["windows"]["sandbox"], "elevated")
        self.assertEqual(fs[":root"], "read")
        self.assertEqual(fs[str(native_proof.ROOT)], "read")
        self.assertEqual({p for p, mode in fs.items() if mode == "write"},
                         {str(native_proof.DOCUMENTS), tmp})
        self.assertEqual(fs[str(native_proof.DOCUMENTS)], "write")
        self.assertEqual(fs[str(Path.home() / ".codex")], "deny")
        self.assertEqual(fs[str(native_proof.ROOT / "data")], "deny")
        self.assertTrue(cfg["permissions"]["assistant-proof"]["network"]["enabled"])
        self.assertEqual(cfg["approval_policy"], "never")

    def test_room_read_list_and_recursive_search_exclude_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            private = folder / "private"
            private.mkdir()
            (private / "synthetic.txt").write_text("SECRET_CANARY")
            (folder / "fixture.txt").write_text("ordinary fixture")
            with patch.object(native_tools, "protected", return_value=[private]), patch.object(files, "roots", return_value=[folder]):
                self.assertIn("SECRET_CANARY", files.read_file({"path": str(private / "synthetic.txt")})["text"])
                with native_proof.activated():
                    with self.assertRaises(files.Refused):
                        files.read_file({"path": str(private / "synthetic.txt")})
                    self.assertNotIn("private/", json.dumps(files.list_dir({"path": str(folder)})))
                    self.assertNotIn("SECRET_CANARY", json.dumps(files.find({"path": str(folder), "pattern": "CANARY"})))
                    self.assertEqual(files.read_file({"path": str(folder / "fixture.txt")})["text"], "ordinary fixture")

    def test_failed_activity_is_committed_without_a_final_answer_and_not_rearmed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, "DB_PATH", Path(tmp) / "store.db"), patch.object(native_tools, "LOGS", Path(tmp) / "logs"):
            conn = db.connect()
            try:
                ident = db.add_row(conn, "user", "explicit proof", meta={"who": "sam", "native_proof": True})
                def broken(conn, **kwargs):
                    self.assertTrue(native_proof.ACTIVE.get())
                    kwargs["on_step"]("command failed", "native", {"cwd": "fixture", "command": "assert", "exitCode": 7, "aggregatedOutput": "EXPECTED_FAILURE"})
                    raise providers.TurnBroke("Malformed final JSON")
                with patch.object(app.brain, "run_turn", side_effect=broken) as run, patch.object(app, "MODEL", {"name": "codex/gpt-5.6-sol"}), patch.object(app, "LAST_TURN", {}), patch.object(native_proof, "available", return_value=True), patch.object(providers, "codex_only", return_value=True):
                    native_proof.ARMED.add(ident)
                    self.assertFalse(app.one_turn(conn, None))
                    self.assertFalse(app.one_turn(conn, None))
                    self.assertEqual(run.call_count, 1)
                events = db.events_for_rows(conn, [ident])
                self.assertTrue(any(e["detail"].get("exitCode") == 7 for e in events))
                self.assertIn("Malformed", json.dumps(events))
                self.assertEqual(db.get_row(conn, ident)["meta"]["who"], "sam")
                self.assertEqual(app.unanswered(conn), ident)
                self.assertFalse(native_proof.ACTIVE.get())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
