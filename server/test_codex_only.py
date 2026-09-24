"""No paid calls, live store, credentials, sessions, or running room required."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from . import app, brain, codex_backend, db, dream, jobs, limits, providers, watch, web
from . import test_codex_backend as protocol
from .test_codex_backend import FakeClient, SCHEMA


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "provider.json"
        p = patch.object(providers, "CHOICE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        self.original = {"model": "codex/gpt-5.6-sol", "dream_model": "opus",
                         "credits": {"openai": {"usd": 7}}, "unrelated": "keep me"}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        providers.set_codex_only(True)
        # Every test is fenced from external transport, even if a guard regresses.
        for target in ("subprocess.Popen", "subprocess.run", "urllib.request.urlopen"):
            p = patch(target, side_effect=AssertionError("external transport: " + target))
            p.start()
            self.addCleanup(p.stop)

    def test_switch_is_reversible_and_preserves_choices(self):
        self.assertTrue(providers.codex_only())
        providers.set_codex_only(False)
        saved = json.loads(self.path.read_text())
        for key, value in self.original.items():
            self.assertEqual(saved[key], value)
        self.assertFalse(providers.codex_only())
        self.path.unlink()
        self.assertFalse(providers.codex_only())

    def test_unreadable_setting_fails_closed_and_boolean_is_required(self):
        self.path.write_text("{broken")
        self.assertTrue(providers.codex_only())
        with self.assertRaises(providers.Refused):
            providers.set_codex_only("false")

    def test_claude_aliases_router_and_auto_cannot_launch(self):
        for key in (None, "opus", "sonnet", "haiku", "fable", "claude-opus-5",
                    "claude_code/custom", "anthropic/claude-future",
                    "openrouter/anthropic/claude-opus-5", "openrouter/auto",
                    "openrouter/openrouter/auto", "openrouter/openrouter/auto:online",
                    "openrouter/openrouter/free", "openrouter/switchpoint/router",
                    "unknown-bare-fallback"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(brain.TurnBroke, "Codex-only"):
                    brain.call_claude("system", "{}", model=key)
                with self.assertRaisesRegex(providers.TurnBroke, "Codex-only"):
                    providers.call(providers.resolve(key), "system", "{}", SCHEMA)
                with self.assertRaises(providers.Refused):
                    providers.choose(key)
        with self.assertRaises(providers.TurnBroke):
            providers.call({"service": "anthropic", "id": "future"}, "", "{}", SCHEMA)

    def test_disabled_call_paths_remain(self):
        providers.set_codex_only(False)
        with patch.object(brain, "_call_model", return_value={"reply": "legacy"}) as call:
            self.assertEqual(brain.call_claude("s", "{}", "opus")["reply"], "legacy")
            call.assert_called_once()
        with patch.object(providers, "_call", return_value={"reply": "router"}) as call:
            self.assertEqual(providers.call(providers.resolve("anthropic/claude-opus-5"),
                                           "s", "{}", SCHEMA)["reply"], "router")
            call.assert_called_once()
        with patch.object(web, "_search", return_value={"answer": "test"}):
            self.assertEqual(web.search({"query": "test"})["answer"], "test")
        with patch.object(limits, "_fetch_claude", return_value=([{"window": "old"}], None)):
            self.assertEqual(limits._fetch()[0][0]["window"], "old")

    def test_activation_cannot_overlap_a_claude_operation(self):
        providers.set_codex_only(False)
        with providers.model_activity("opus"):
            with self.assertRaisesRegex(providers.Refused, "active Claude"):
                providers.set_codex_only(True)
            self.assertFalse(providers.codex_only())
        providers.set_codex_only(True)
        with self.assertRaises(providers.Refused):
            with providers.model_activity("opus"):
                self.fail("Claude entered after activation")

    def test_the_rooms_own_claude_calls_are_blocked(self):
        with self.assertRaises(providers.Refused):
            brain.find_claude()  # includes standalone paid diagnostic entry points

    def test_web_search_pauses_but_page_reads_and_memory_search_work(self):
        with self.assertRaisesRegex(web.Refused, "Paused"):
            web.search({"query": "test"})
        with patch.object(web, "read_page", return_value={"text": "page"}) as read:
            result = web.run({"op": "read", "url": "https://example.com"})
            self.assertEqual(result["text"], "page")
            read.assert_called_once()
        with patch.object(brain.search, "run", return_value={"hits": [], "problems": [], "notes": [], "summary": "no hits"}) as search:
            brain._apply_search(None, [{"keywords": ["memory"]}])
            search.assert_called_once()

    def test_quota_never_reads_credentials_or_returns_cached_claude_windows(self):
        with patch.object(limits, "_token", side_effect=AssertionError("credentials read")), \
             patch.dict(limits._CACHE, {"limits": [{"window": "seven_day_opus"}]}):
            self.assertIsNone(limits.now())
            self.assertIsNone(limits._fetch()[0])
            self.assertIsNone(limits.status()["limits"])
            self.assertIn("Paused", limits.status()["error"])
            limits._refresh()  # even a queued background refresh is blocked

    def test_paused_senses_keep_watermarks(self):
        state = {"quota_fired": ["keep"], "resets_seen": {"old": "keep"}}
        before = copy.deepcopy(state)
        with patch.object(watch, "_limits_now", side_effect=AssertionError("quota poll")):
            self.assertEqual(watch._quota(state, {}), [])
            self.assertEqual(watch._resets(state, {}), [])
        self.assertEqual(state, before)

    def test_settings_route_and_busy_refusal(self):
        handler = app.Handler.__new__(app.Handler)
        handler.path = "/api/providers/codex-only"
        handler._let_in = lambda: True
        handler._json = lambda body, code=200: (code, body)
        def post(enabled):
            body = json.dumps({"enabled": enabled}).encode()
            handler.headers = {"Content-Length": len(body)}
            handler.rfile = io.BytesIO(body)
            return handler.do_POST()
        with patch.object(providers, "catalogue", return_value={"mock": True}), \
             patch.object(app, "NUDGE", Mock()), \
             patch.object(app, "progress_now", return_value={"busy": False}), \
             patch.dict(app.FAILED, {"row": 99}):
            self.assertEqual(post(False)[0], 200)
            self.assertFalse(providers.codex_only())
            with app.TURN_GATE:
                self.assertEqual(post(True)[0], 400)
                self.assertFalse(providers.codex_only())
            self.assertEqual(post(True)[0], 200)
            self.assertTrue(providers.codex_only())
            self.assertIsNone(app.FAILED["row"])

    def test_paused_chat_leaves_scheduler_and_messages_pending(self):
        nudge = Mock()
        nudge.wait.side_effect = [True, KeyboardInterrupt]
        with patch.object(app, "NUDGE", nudge), \
             patch.dict(app.MODEL, {"name": "opus"}), \
             patch.dict(app.RESTARTING, {"why": None}), \
             patch.object(db, "connect", return_value=Mock()), \
             patch.object(app, "take_woken", side_effect=AssertionError("consumed waking")), \
             patch.object(app, "unanswered", side_effect=AssertionError("consumed message")), \
             patch.object(app.clock, "due", side_effect=AssertionError("consumed clock")), \
             patch.object(app.watch, "due", side_effect=AssertionError("consumed sense")):
            with self.assertRaises(KeyboardInterrupt):
                app.turn_loop()
        self.assertFalse(app.TURN_GATE.locked())

    def test_live_voice_holds_autonomous_wakings_without_consuming_them(self):
        nudge = Mock()
        nudge.wait.side_effect = [True, KeyboardInterrupt]
        conn = Mock()
        with patch.object(app, "NUDGE", nudge), \
             patch.dict(app.RESTARTING, {"why": None}), \
             patch.object(providers, "paused_reason", return_value=None), \
             patch.object(db, "connect", return_value=conn), \
             patch.object(app, "unanswered", return_value=None), \
             patch.object(app.clock, "free_now", return_value=None), \
             patch.object(app.live_voice.manager, "status", return_value={"active": True}), \
             patch.object(app, "take_woken", side_effect=AssertionError("consumed waking")), \
             patch.object(app.clock, "due", side_effect=AssertionError("consumed clock")), \
             patch.object(app.watch, "due", side_effect=AssertionError("consumed sense")), \
             patch.object(app.dream, "due", side_effect=AssertionError("consumed dream")):
            with self.assertRaises(KeyboardInterrupt):
                app.turn_loop()
        self.assertFalse(app.TURN_GATE.locked())

    def test_dream_gate_keeps_requests_and_never_writes_missed_or_done(self):
        with patch.dict(dream.ASKED, {"why": "asked"}), \
             patch.object(dream, "record_missed", side_effect=AssertionError("missed record")), \
             patch.object(db, "open_dream", side_effect=AssertionError("dream record")):
            gate = dream.due(None, record=True)
            self.assertFalse(gate["due"])
            self.assertEqual(gate["status"], "paused")
            self.assertEqual(dream.ASKED["why"], "asked")
            self.assertEqual(dream.dream(None, "2026-09-11", "asked", False)["status"], "paused")
            providers.set_codex_only(False)
            with patch.object(dream, "quiet_for", return_value=40), \
                 patch.object(db, "dreams_for_night", return_value=[]):
                self.assertTrue(dream.due(None)["due"])
            self.assertIsNone(dream.ASKED["why"])

    def test_codex_dream_selection_does_not_follow_chat_or_pause(self):
        providers.choose_dream("codex/gpt-6-astra")
        providers.choose("codex/gpt-5.6-sol")
        self.assertEqual(providers.dream_model(), "codex/gpt-6-astra")
        with patch.dict(dream.ASKED, {"why": "asked"}), \
             patch.object(dream, "quiet_for", return_value=40), \
             patch.object(db, "dreams_for_night", return_value=[]):
            self.assertTrue(dream.due(None)["due"])

    def test_prompt_omits_claude_machinery_and_retains_shared_contract(self):
        spark = "Spark with Claude in its own words — keep exactly"
        with patch.object(brain, "read_spark", return_value=spark), \
             patch.object(brain.notes, "for_prompt", return_value="Their notes"):
            prompt = brain.system_prompt(False)
        self.assertTrue(prompt.startswith(spark + "\n\n---\n\nTheir notes"))
        for absent in ("### `worker`", "## The standing terms", "{{standing_terms}}",
                       "Opus, Fable", '"op": "search", "query"', "--max-budget-usd"):
            self.assertNotIn(absent, prompt)
        schema = brain.response_schema()
        self.assertNotIn("worker", schema["properties"])
        self.assertEqual(schema["properties"]["web"]["items"]["properties"]["op"]["enum"], ["read"])
        for name in ("fetch", "search", "shelf", "files", "project", "clock", "watch", "to", "essences"):
            self.assertIn(name, schema["properties"])
        providers.set_codex_only(False)
        self.assertNotIn("worker", brain.response_schema()["properties"])
        self.assertNotIn("## The standing terms", brain.harness_text())

    def test_catalogue_and_codex_gauges(self):
        reading = {
            "limits": [{"window": "codex_primary", "used_fraction": .58}],
            "credits": {"unlimited": False, "balance": "217.8642000000"},
            "error": None,
        }
        with patch.object(providers, "prices", return_value={"by_id": {}, "read_at": None, "error": None}), \
             patch.object(providers, "key_of", return_value=None), \
             patch.object(providers, "keys_present", return_value={"anthropic": {}, "openai": {}}), \
             patch.object(providers, "money", return_value={}), \
             patch.object(codex_backend, "limits_now", return_value=reading):
            cat = providers.catalogue()
            self.assertTrue(cat["codex_only"])
            self.assertNotIn("claude_code", cat["services"])
            self.assertNotIn("anthropic", cat["keys"])
            self.assertFalse(any(providers.is_claude(m) for m in cat["models"]))
            self.assertTrue(any(m["service"] == "openai" for m in cat["models"]))
            self.assertEqual(providers.now()["limits"], reading["limits"])
            self.assertEqual(providers.now()["credits"], reading["credits"])
            self.assertEqual(brain.plan_block(None)["plan"], "ChatGPT subscription")
            self.assertEqual(brain.plan_block(None)["limits"], reading["limits"])
            self.assertIsNone(brain.plan_block(None, "openai/gpt-5.6-sol")["limits"])


class CodexChatWithModeTests(protocol.CodexTests):
    """Run the existing subscription protocol suite again with the switch ON."""
    def setUp(self):
        super().setUp()
        self.mode = patch.object(providers, "codex_only", return_value=True)
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_room_chat_dispatches_through_subscription(self):
        with patch.object(brain, "find_claude", side_effect=AssertionError("Claude executable")), \
             patch.object(providers, "key_of", side_effect=AssertionError("API key")):
            answer = brain.call_claude("Its Spark", "{}", model="codex/gpt-5.6-sol", schema=SCHEMA)
        self.assertEqual(answer["reply"], "hello")
        self.assertEqual(answer["_meta"]["service"], "codex")
        self.assertEqual(len(FakeClient.instances), 1)

    def test_complete_chat_turn_keeps_messages_and_routes_codex_reply(self):
        with tempfile.TemporaryDirectory() as scratch, ExitStack() as stack:
            stack.enter_context(patch.object(db, "DB_PATH", Path(scratch) / "store.db"))
            stack.enter_context(patch.object(brain.overmind, 'STATE', Path(scratch) / 'overmind.json'))
            for module, name, value in (
                (brain, "read_spark", "Its unchanged Spark"),
                (brain, "spark_version", 30), (brain, "spark_budget", 10000),
                (brain, "voice_status", {"on": False, "reason": "test"}),
                (brain.notes, "for_prompt", ""), (brain.recall, "before_she_speaks", None),
                (brain, "_jobs_block", []), (brain, "_projects_block", []),
                (brain.clock, "for_prompt", {}), (brain.watch, "for_prompt", {}),
                (brain.clock, "apply_her_word", []), (brain, "_purse", None),
                (codex_backend, "limits_now", {"limits": [], "error": None}),
            ):
                stack.enter_context(patch.object(module, name, return_value=value))
            stack.enter_context(patch.object(providers, "chosen", return_value="codex/gpt-5.6-sol"))
            stack.enter_context(patch.object(brain, "find_claude", side_effect=AssertionError("Claude")))
            conn = db.connect()
            try:
                user_id = db.add_row(conn, "user", "Hello Ada", meta={"who": "lee"})
                result = brain.run_turn(conn, model="codex/gpt-5.6-sol")
                reply = db.get_row(conn, result["applied"]["reply_row"])
                self.assertEqual(reply["text"], "hello")
                self.assertEqual(reply["by_model"], "codex/gpt-5.6-sol")
                self.assertEqual(reply["meta"]["room"], "lee")
                self.assertEqual(db.get_row(conn, user_id)["text"], "Hello Ada")
                self.assertNotIn("errands", result["sent"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
