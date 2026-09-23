"""Protocol regression checks; no model calls, credentials, or live store."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import codex_backend as backend, providers


SCHEMA = {"type": "object", "properties": {
    "reply": {"type": "string"}, "drop": {"type": "array", "items": {"type": "integer"}}},
    "required": ["reply"]}


class FakeClient:
    account = {"type": "chatgpt"}
    events = []
    instances = []

    def __init__(self, *args):
        self.counter = 0
        self.requests, self.sent = [], []
        self.pending = copy.deepcopy(self.events)
        self.closed = False
        self.instances.append(self)

    def initialize(self):
        pass

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method == "config/read":
            return {"config": {"mcp_servers": {"test.server": {"command": "unused"}}}}
        if method == "account/rateLimits/read":
            return {"rateLimitsByLimitId": {"codex": {
                "primary": {"usedPercent": 58, "windowDurationMins": 300,
                            "resetsAt": 1789231750},
                "secondary": {"usedPercent": 9, "windowDurationMins": 10080,
                              "resetsAt": 1789818550},
                "credits": {"hasCredits": True, "unlimited": False,
                            "balance": "217.8642000000"}}}}
        if method == "thread/start":
            return {"thread": {"id": "test"}}
        raise AssertionError(method)

    def send(self, message):
        self.sent.append(message)

    def next(self):
        if not self.pending:
            raise providers.TurnBroke("stream closed")
        return self.pending.pop(0)

    def close(self):
        self.closed = True


def event(method, **params):
    return {"method": method, "params": dict(threadId="test", **params)}


class CodexTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.account = {"type": "chatgpt"}
        FakeClient.events = [
            event("item/agentMessage/delta", delta='{"reply":"hello"'),
            event("item/completed", item={"type": "agentMessage", "text": '{"reply":"hello","drop":null}'}),
            event("thread/tokenUsage/updated", tokenUsage={"total": {
                "inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 12,
                "reasoningOutputTokens": 2}, "modelContextWindow": 1000}),
            event("turn/completed", turn={"status": "completed"}),
        ]
        self.patcher = patch.object(backend, "Client", FakeClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def call(self, **kwargs):
        return providers.call(providers.resolve("codex/gpt-5.6-sol"),
                              "Ada's instructions", '{"spark":"its words"}', SCHEMA, **kwargs)

    def test_subscription_dispatch_and_metadata(self):
        with patch.object(providers, "key_of", side_effect=AssertionError("API key lookup")):
            answer = self.call()
        self.assertEqual(answer["reply"], "hello")
        self.assertEqual(answer["drop"], [])
        self.assertIsNone(answer["_meta"]["cost_usd"])
        self.assertEqual(answer["_meta"]["fresh_input_tokens"], 20)
        self.assertEqual(answer["_meta"]["model_key"], "codex/gpt-5.6-sol")
        self.assertEqual(answer["_meta"]["limits"], [])
        client = FakeClient.instances[0]
        self.assertTrue(client.closed)
        params = dict(client.requests)["thread/start"]
        self.assertEqual(params["baseInstructions"], "Ada's instructions")
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["dynamicTools"], [])
        self.assertTrue(params["ephemeral"])
        self.assertEqual(client.sent[0]["params"]["outputSchema"], providers.strict_schema(SCHEMA))

    def test_api_key_or_missing_login_never_starts_a_turn(self):
        for account in ({"type": "apiKey"}, None):
            FakeClient.account = account
            with self.assertRaisesRegex(providers.TurnBroke, "codex login"):
                self.call()
            client = FakeClient.instances[-1]
            self.assertTrue(client.closed)
            self.assertEqual([m for m, _ in client.requests], ["account/read"])
            self.assertEqual(client.sent, [])

    def test_subscription_windows_are_shaped_for_the_existing_gauges(self):
        rows, credits, error = backend._fetch_limits()
        self.assertIsNone(error)
        self.assertEqual([row["label"] for row in rows], ["5-hour limit", "Weekly"])
        self.assertEqual([row["used_fraction"] for row in rows], [.58, .09])
        self.assertTrue(all(row["resets_at"].endswith("+00:00") for row in rows))
        self.assertEqual(credits, {"unlimited": False, "balance": "217.8642000000"})
        self.assertTrue(FakeClient.instances[-1].closed)

    def test_credit_balance_rejects_missing_or_invalid_values(self):
        self.assertIsNone(backend._tidy_credits({}))
        self.assertIsNone(backend._tidy_credits({"credits": {"balance": "unknown"}}))
        self.assertEqual(backend._tidy_credits({"credits": {"unlimited": True}}),
                         {"unlimited": True, "balance": None})

    def test_limit_label_comes_from_reported_duration(self):
        weekly = backend._tidy_limit({
            "usedPercent": 12, "windowDurationMins": 10080,
            "resetsAt": 1789818550})
        self.assertEqual(weekly["window"], "seven_day")
        self.assertEqual(weekly["label"], "Weekly")

    def test_prompt_measurement_is_not_cumulative_tool_consumption(self):
        def sample(total, last):
            return event("thread/tokenUsage/updated", tokenUsage={
                "total": {"inputTokens": total, "cachedInputTokens": 170000},
                "last": {"inputTokens": last}, "modelContextWindow": 258400})
        FakeClient.events = [sample(0, 0), sample(50000, 50000), sample(105000, 55000),
            sample(223318, 58000), *FakeClient.events[:2], FakeClient.events[-1]]
        meta = self.call()["_meta"]
        self.assertEqual(meta["prompt_input_tokens"], 50000)
        self.assertEqual(meta["last_model_input_tokens"], 58000)
        self.assertEqual(meta["input_tokens"], 223318)
        self.assertEqual(meta["input_tokens_scope"], "cumulative_thread")
        self.assertEqual(meta["fresh_input_tokens"], 53318)

    def test_missing_initial_usage_is_unknown_not_a_total(self):
        meta = self.call()["_meta"]  # old mock has total only
        self.assertIsNone(meta["prompt_input_tokens"])
        self.assertEqual(meta["input_tokens"], 100)
        FakeClient.events[-2]["params"]["tokenUsage"]["last"] = {"inputTokens": 50}
        meta = self.call()["_meta"]
        self.assertIsNone(meta["prompt_input_tokens"], "first observed sample was already cumulative")
        self.assertEqual(meta["last_model_input_tokens"], 50)

    def test_interrupted_stream_preserves_partial_reply(self):
        FakeClient.events = [event("item/agentMessage/delta", delta='{"reply":"half')]
        with self.assertRaises(providers.TurnBroke) as failed:
            self.call()
        self.assertEqual(failed.exception.reply, "half")
        self.assertTrue(FakeClient.instances[0].closed)

    def test_failed_turn_keeps_its_reason(self):
        FakeClient.events = [event("turn/completed", turn={
            "status": "failed", "error": {"message": "usage limit reached"}})]
        with self.assertRaisesRegex(providers.TurnBroke, "usage limit reached"):
            self.call()

    def test_malformed_or_wrong_shape_is_not_accepted(self):
        for raw in ('broken json', '{"other":1}', '[]'):
            FakeClient.events = [
                event("item/completed", item={"type": "agentMessage", "text": raw}),
                event("turn/completed", turn={"status": "completed"})]
            with self.assertRaises(providers.TurnBroke):
                self.call()

    def test_images_are_carried_in_order(self):
        self.call(images=[{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}])
        inputs = FakeClient.instances[0].sent[0]["params"]["input"]
        self.assertEqual(inputs[1], {"type": "image", "url": "data:image/png;base64,aGVsbG8="})

    def test_choice_preserves_independent_night_and_credit_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(providers, "CHOICE_PATH", Path(folder) / "provider.json"):
                providers._save_choice(dream_model="openai/gpt-5.6-sol", credits={"openai": {"usd": 7}})
                providers.choose("codex/gpt-5.6-sol")
                self.assertEqual(providers.dream_model(), "openai/gpt-5.6-sol")
                self.assertEqual(providers.credits_note("openai")["usd"], 7)
                self.assertEqual(providers.chosen(), "codex/gpt-5.6-sol")
        self.assertEqual(providers.resolve("codex/custom-model")["service"], "codex")
        astra = providers.resolve("codex/gpt-6-astra")
        self.assertEqual(astra["id"], "gpt-6-astra")
        self.assertEqual(providers.price_of(astra)["source"], "subscription")
        self.assertEqual(providers.price_of(providers.resolve("codex/gpt-5.6-sol"))["source"], "subscription")


if __name__ == "__main__":
    unittest.main()
