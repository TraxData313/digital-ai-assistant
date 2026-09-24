"""Prompt references through the real look/answer loop; all model calls mocked."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from . import brain, codex_backend, db, files, jobs, projects, prompt_reference as refs, providers
from .test_codex_backend import FakeClient, event


def answer(**changes):
    out = {}
    for name, spec in brain.response_schema()["properties"].items():
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        out[name] = (None if "null" in types else [] if "array" in types
                     else False if "boolean" in types else "")
    out.update(reply="hello", to="room")
    out.update(changes)
    return out


class ScriptedClient(FakeClient):
    answers = []
    def __init__(self, *args):
        super().__init__(*args)
        text = json.dumps(self.answers.pop(0))
        self.pending = [event("item/completed", item={"type": "agentMessage", "text": text}),
                        event("turn/completed", turn={"status": "completed"})]


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.temp / "store.db"))
        self.stack.enter_context(patch.object(brain.overmind, 'STATE', self.temp / 'overmind.json'))
        self.stack.enter_context(patch.object(jobs, "JOBS_PATH", self.temp / "jobs.json"))
        self.stack.enter_context(patch.object(providers, "codex_only", return_value=True))
        self.stack.enter_context(patch.object(providers, "chosen", return_value="codex/gpt-5.6-sol"))
        self.stack.enter_context(patch.object(codex_backend, "Client", ScriptedClient))
        for target in ("subprocess.Popen", "subprocess.run", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("external transport")))
        for module, name, value in (
            (brain, "read_spark", "Its Spark — unchanged"), (brain, "spark_version", 30),
            (brain, "spark_budget", 10000), (brain, "voice_status", {"on": False, "reason": "test"}),
            (brain.notes, "for_prompt", "Their attributed notes — unchanged"),
            (brain.clock, "for_prompt", {}), (brain.clock, "apply_her_word", []),
            (brain.watch, "for_prompt", {}), (brain.watch, "looks", {}),
            (brain.watch, "senses", {}), (brain, "_purse", None),
            (codex_backend, "limits_now", {"limits": [], "error": None}),
        ):
            self.stack.enter_context(patch.object(module, name, return_value=value))
        self.recall = self.stack.enter_context(patch.object(brain.recall, "before_she_speaks", return_value=None))
        self.conn = db.connect()
        self.stack.callback(self.conn.close)
        ScriptedClient.instances = []
        ScriptedClient.answers = []
        ScriptedClient.account = {"type": "chatgpt"}

    def read(self, path, start=1, lines=400):
        return brain._apply_files(self.conn, [{"op": "read", "path": path,
                                             "from_line": start, "lines": lines}])["ran"][0]

    def run_turn(self, *replies):
        ScriptedClient.answers = list(replies)
        return brain.run_turn(self.conn, model="codex/gpt-5.6-sol")

    def test_all_indexed_instructions_load_through_existing_files_protocol(self):
        schema = copy.deepcopy(brain.response_schema())
        routine = brain.harness_text()
        for topic in refs.TOPICS:
            handle = "assistant:help/" + topic
            self.assertIn(handle, routine)
            result = self.read(handle)
            self.assertNotIn("refused", result)
            self.assertIn("Room operation reference: " + topic, result["text"])
            self.assertNotIn("{{", result["text"])
        self.assertEqual(brain.response_schema(), schema)
        self.assertNotIn('"op": "task_add"', routine)
        self.assertIn('"op": "task_add"', self.read("assistant:help/projects")["text"])
        self.assertLess(len(routine), len(brain.operation_harness()) * .25)
        self.assertTrue(brain.system_prompt().startswith("Its Spark — unchanged\n\n---\n\nTheir attributed notes — unchanged"))

    def test_reference_is_read_only_bounded_and_cannot_escape(self):
        before = self.conn.total_changes
        for path in ("assistant:help/../passwords.py", "assistant:task/0", "assistant:task/999",
                     "assistant:https://localhost/", "assistant:help/worker"):
            self.assertIn("refused", self.read(path))
        self.assertIn("refused", files.run({"op": "read", "path": "data/angel.token"}))
        self.assertIn("refused", brain._apply_files(self.conn, [{"op": "write", "path": "assistant:task/1"}])["ran"][0])
        first = self.read("assistant:help/memory", lines=5)
        second = self.read("assistant:help/memory", start=6, lines=5)
        whole = refs.read(self.conn, "assistant:help/memory").splitlines()
        self.assertEqual(first["text"], "\n".join(whole[:5]))
        self.assertEqual(second["text"], "\n".join(whole[5:10]))
        self.assertEqual(self.conn.total_changes, before)
        self.assertTrue(first["notes"])
        with patch.object(providers, "codex_only", return_value=False):
            self.assertIn("refused", self.read("assistant:help/memory"))
            self.assertEqual(brain.harness_text(), brain.operation_harness())

    def test_dormant_summaries_keep_full_records_and_all_pending_notices(self):
        project = projects.add_project(self.conn, "Garden", "lee")["project"]
        projects.put_on_desk(self.conn, "Garden")
        task = projects.add_task(self.conn, project["id"], "lee", "Water", wants="Detailed original task " * 60)["task"]
        job = jobs.open_job("Garden job", "Original goal " * 70, 8, 8, "Decision " * 90)["job"]
        before_job = jobs.JOBS_PATH.read_bytes()
        before_task = projects.task(self.conn, task["id"])
        summary = brain._projects_block(self.conn)["on_my_desk"][0]["open_tasks"][0]
        self.assertEqual(summary["asked_by"], "lee")
        self.assertNotIn("wants", summary)
        self.assertNotIn("wants_preview", summary)
        self.assertIn(before_task["wants"], self.read(summary["read"])["text"].replace("\n│ ", ""))
        jobs_view = brain._jobs_block()
        self.assertNotIn("worker_execution", jobs_view[0])
        self.assertLess(len(jobs_view[0]["decided_preview"]), 130)
        self.assertIn("Original goal", self.read(jobs_view[0]["read"])["text"])
        for i in range(61):
            projects.add_notice(self.conn, task["id"], "Pending " + str(i), "lee", {"who": "lee"})
        summary = brain._projects_block(self.conn)["on_my_desk"][0]["open_tasks"][0]
        self.assertEqual(summary["notices"], 61)
        body = refs.read(self.conn, summary["read"])
        self.assertIn("Pending 0", body)
        self.assertIn("Pending 60", body)
        self.assertIn('"who": "lee"', body)
        self.assertIn("│", body)
        self.assertEqual(projects.task(self.conn, task["id"]), before_task)
        self.assertEqual(jobs.JOBS_PATH.read_bytes(), before_job)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_notices WHERE done = 0").fetchone()[0], 61)
        self.assertIn("assistant:job/" + str(job["id"]), self.read("assistant:jobs")["text"])

    def test_long_record_pages_and_nonrunning_schedule_stay_explicit(self):
        project = projects.add_project(self.conn, "Garden", "both")["project"]
        task = projects.add_task(self.conn, project["id"], "lee", "Check",
                                 wants="A" * 35000 + " END OF ORIGINAL", repeats=True,
                                 schedule="whenever the moon feels right")["task"]
        summary = refs.task_summary(self.conn, task)
        self.assertIn("nothing fires", summary["schedule_paused"])
        first = self.read(summary["read"])
        self.assertLessEqual(db.est_tokens(first["text"]), files.MAX_READ_TOKENS)
        self.assertTrue(first["notes"])
        cursor = first["from_line"] + first["lines_shown"]
        next_page = self.read(summary["read"], start=cursor)
        self.assertIn("END OF ORIGINAL", next_page["text"])
        self.assertEqual(projects.task(self.conn, task["id"])["state"], "open")

    def test_ordinary_conversation_needs_no_instruction_read(self):
        incoming = db.add_row(self.conn, "user", "Hello Ada", meta={"who": "lee"})
        with patch.object(refs, "read", side_effect=AssertionError("unnecessary reference lookup")):
            turn = self.run_turn(answer())
        self.assertEqual(len(ScriptedClient.instances), 1)
        row = db.get_row(self.conn, turn["applied"]["reply_row"])
        self.assertEqual(row["text"], "hello")
        self.assertEqual(row["meta"]["room"], "lee")
        self.assertEqual(db.get_row(self.conn, incoming)["text"], "Hello Ada")
        self.recall.assert_called_once()

    def test_memory_meter_uses_initial_input_and_reports_budget(self):
        db.add_row(self.conn, "user", "context " * 18000, meta={"who": "lee"})
        meta = {"model_key": "codex/gpt-5.6-sol", "service": "codex",
                "input_tokens": 223318, "turn_input_tokens": 223318,
                "prompt_input_tokens": 24000, "last_model_input_tokens": 58000,
                "prompt_est_raw": 20000}
        with patch.object(brain, "last_body", return_value=meta), patch.object(brain, "spark_budget", return_value=30000):
            info = brain.build_prompt(self.conn)["self"]
        self.assertEqual(info["measured_input_tokens_last_turn"], 24000)
        self.assertEqual(info["prompt_tokens_est"], round(info["prompt_tokens_est_raw"] * 1.2))
        self.assertEqual(info["total_input_tokens_last_turn"], 223318)
        self.assertEqual(info["budget_status"], "over_target")
        self.assertEqual(info["budget_over_tokens_est"], info["prompt_tokens_est"] - 30000)
        del meta["prompt_input_tokens"]
        with patch.object(brain, "last_body", return_value=meta):
            legacy = brain.build_prompt(self.conn)["self"]
        self.assertIsNone(legacy["measured_input_tokens_last_turn"])
        self.assertEqual(legacy["prompt_tokens_est"], legacy["prompt_tokens_est_raw"])
        self.assertEqual(brain.measured_prompt_input({"service": "claude_code", "input_tokens": 24000}), 24000)

    def test_routine_folding_keeps_sources_attribution_and_spark(self):
        original = db.add_row(self.conn, "user", "Leona saw the river. " * 2000, meta={"who": "lee"})
        db.add_row(self.conn, "user", "Fold settled history", meta={"who": "sam"})
        before = brain.build_prompt(self.conn)["self"]["prompt_tokens_est"]
        routine = brain.harness_text()
        for useful in ("self.budget_target_tokens", "drop: [ids]", "after my final answer", "consumption, NOT the size", "assistant:help/memory"):
            self.assertIn(useful, routine)
        self.assertIn("budget_target_tokens", self.read("assistant:help/memory")["text"])
        with patch.object(brain, "write_spark", side_effect=AssertionError("Spark must stay unchanged")):
            self.run_turn(answer(essences=[{"op": "add", "title": "The river", "text": "Leona saw the river.",
                "replaces": [original], "room": "lee"}], drop=[original]))
        self.assertEqual(db.get_row(self.conn, original)["meta"]["who"], "lee")
        self.assertFalse(db.get_row(self.conn, original)["loaded"])
        essence = self.conn.execute("SELECT id FROM rows WHERE kind='essence' ORDER BY id DESC LIMIT 1").fetchone()[0]
        trail = db.trail(db.trail_index(self.conn), essence)
        self.assertIn(original, trail["rows"])
        after = brain.build_prompt(self.conn)
        self.assertLess(after["self"]["prompt_tokens_est"], before)
        self.assertEqual(after["essences"][0]["room"], "lee")

    def test_recall_loads_manual_searches_fetches_and_preserves_source_trail(self):
        original = db.add_row(self.conn, "user", "Leona saw the river turn blue", meta={"who": "lee"})
        essence = db.add_row(self.conn, "essence", "Leona said the river turned blue", replaces=[original], title="The river")
        self.conn.execute("UPDATE rows SET loaded = 0 WHERE id IN (?, ?)", (original, essence))
        self.conn.commit()
        db.add_row(self.conn, "user", "What did Leona say about the river?", meta={"who": "sam"})
        trail = db.trail(db.trail_index(self.conn), essence)
        turn = self.run_turn(
            answer(look_first=True, looking="I will look for the river memory.",
                   files=[{"op": "read", "path": "assistant:help/memory"}],
                   search=[{"keywords": ["river"]}]),
            answer(look_first=True, looking="I will check its original words.",
                   fetch=[{"op": "essence", "id": essence}, {"op": "sources", "id": essence}]),
            answer(reply="Leona said the river turned blue."))
        self.assertEqual(len(ScriptedClient.instances), 3)
        rounds = turn["sent"]["found"]["rounds"]
        self.assertIn(essence, [h["id"] for h in rounds[0]["search"]["searches"][0]["hits"]])
        self.assertIn("Room operation reference: memory", rounds[0]["files"]["ran"][0]["text"])
        loaded = {r["id"]: r for r in turn["sent"]["messages"]}
        self.assertEqual(loaded[original]["who"], "lee")
        after = db.trail(db.trail_index(self.conn), essence)
        for key in ("direct", "rows", "through", "missing", "covers", "truncated"):
            self.assertEqual(after[key], trail[key])
        self.assertIn(original, after["loaded"])
        self.assertEqual(db.get_row(self.conn, original)["text"], "Leona saw the river turn blue")
        self.recall.assert_called_once()

    def test_project_operation_after_loading_manual_and_record(self):
        p = projects.add_project(self.conn, "Garden", "lee")["project"]
        t = projects.add_task(self.conn, p["id"], "lee", "Water", wants="Use the rain barrel")["task"]
        projects.add_note(self.conn, p["id"], "lee", "The barrel is by the shed")
        before_notes = projects.notes_of(self.conn, p["id"])
        db.add_row(self.conn, "user", "Pick up the watering task", meta={"who": "lee"})
        turn = self.run_turn(
            answer(look_first=True, looking="I will read the project operation and task.",
                   files=[{"op": "read", "path": "assistant:help/projects", "lines": 400},
                          {"op": "read", "path": "assistant:task/" + str(t["id"])}]),
            answer(reply="I have picked up the watering task.",
                   project=[{"op": "task_edit", "title": "Garden", "task": "Water", "state": "doing"}]))
        self.assertEqual(projects.task(self.conn, t["id"])["state"], "doing")
        self.assertEqual(projects.task(self.conn, t["id"])["who"], "lee")
        self.assertEqual(projects.notes_of(self.conn, p["id"]), before_notes)
        self.assertIn('"op": "task_edit"', turn["sent"]["found"]["rounds"][0]["files"]["ran"][0]["text"])
        self.assertEqual(db.get_row(self.conn, turn["applied"]["reply_row"])["meta"]["room"], "lee")


if __name__ == "__main__":
    unittest.main()
