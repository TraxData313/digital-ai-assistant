"""Its notebook, leaned on without a model: python -m server.test_notebook

Needs nothing: a temporary store and a temporary data folder stand in for
the home's, and every call out of the room is stood in front of. What is
checked: notes are numbered 1, 2, 3 and a number is never given out twice;
removing keeps the row on disk and takes it out of what the assistant sees;
votes add one and refuse a note that is gone; turns are counted off its own
answers, up to and including the one that removed a note; the cap takes a
note up to five per cent past it and then locks, and says so in words; the
owner's cap refuses nonsense; the schema and both kinds of instructions carry
it; and a whole turn applies its operations after the answer, reports them
back the next turn, and leaves them alone on a look round.
"""

import copy
import json
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from . import backup, brain, db, home, jobs, notebook, providers


def answer(**changes):
    """A whole answer, every field the schema asks for, as the model sends it."""
    out = {}
    for name, spec in brain.response_schema()["properties"].items():
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        out[name] = (None if "null" in types else [] if "array" in types
                     else False if "boolean" in types else "")
    out.update(reply="hello", to="room")
    out.update(changes)
    return out


def op(kind, note_id=None, text=None):
    return {"op": kind, "id": note_id, "text": text}


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.temp / "data").mkdir()
        self.stack.enter_context(patch.object(db, "DB_PATH", self.temp / "store.db"))
        self.stack.enter_context(patch.object(home, "DATA", self.temp / "data"))
        self.conn = db.connect()
        self.stack.callback(self.conn.close)

    def turn(self, text="a reply"):
        """One answer of its own: what the turn counts are counted off."""
        return db.add_row(self.conn, home.SELF, text)

    def book(self):
        return notebook.for_prompt(self.conn)

    # -- the shape ----------------------------------------------------------

    def test_an_empty_book_says_so(self):
        self.assertEqual(self.book(), {"used": "0 of 5000 tokens (0%)", "notes": []})
        self.assertIsNone(notebook.apply(self.conn, [], self.turn()))
        self.assertIsNone(notebook.apply(self.conn, None, self.turn()))

    def test_numbers_are_the_rooms_and_never_given_twice(self):
        reply = self.turn()
        got = notebook.apply(self.conn, [op("add", 99, "first"), op("add", None, "second")], reply)
        self.assertEqual(got["lines"][0], "added #1 (1 token) — 1 of 5000 tokens (0%)")
        self.assertTrue(got["lines"][1].startswith("added #2 "))
        self.assertEqual(got["problems"], [])
        notebook.apply(self.conn, [op("remove", 2)], self.turn())
        got = notebook.apply(self.conn, [op("add", None, "third")], self.turn())
        self.assertTrue(got["lines"][0].startswith("added #3 "), got)
        self.assertEqual([n["id"] for n in self.book()["notes"]], [1, 3])

    def test_a_note_keeps_its_words_as_written(self):
        notebook.apply(self.conn, [op("add", None, "  line one\r\n  line two  \n")], self.turn())
        note = self.book()["notes"][0]
        self.assertEqual(note["text"], "line one\n  line two")
        self.assertEqual(note["tokens"], db.est_tokens("line one\n  line two"))
        self.assertEqual(list(note), ["id", "up", "down", "turns", "tokens", "text"])

    def test_removed_stays_on_disk_and_leaves_its_view(self):
        notebook.apply(self.conn, [op("add", None, "keep me"), op("add", None, "drop me")],
                       self.turn())
        got = notebook.apply(self.conn, [op("remove", 2)], self.turn())
        self.assertEqual(got["lines"], ["removed #2 — 2 of 5000 tokens (0%)"])
        self.assertEqual([n["text"] for n in self.book()["notes"]], ["keep me"])
        row = self.conn.execute("SELECT * FROM notebook WHERE id = 2").fetchone()
        self.assertEqual(row["text"], "drop me")
        self.assertIsNotNone(row["gone"])
        self.assertIsNotNone(row["gone_dt"])
        table = notebook.status(self.conn)
        self.assertEqual((table["count"], table["removed"]), (1, 1))
        self.assertEqual([(n["id"], n["gone"]) for n in table["notes"]],
                         [(1, False), (2, True)])
        again = notebook.apply(self.conn, [op("remove", 2)], self.turn())
        self.assertEqual(again["lines"], ["refused remove: note #2 was removed"])

    def test_votes_add_one_and_refuse_what_is_not_there(self):
        notebook.apply(self.conn, [op("add", None, "useful")], self.turn())
        got = notebook.apply(self.conn, [op("up", 1), op("up", 1), op("down", 1),
                                         op("up", 7), op("down", None), op("up", True)],
                             self.turn())
        self.assertEqual(got["lines"][:3], ["#1 up — now 1 up, 0 down",
                                            "#1 up — now 2 up, 0 down",
                                            "#1 down — now 2 up, 1 down"])
        self.assertEqual(got["lines"][3], "refused up: there is no note #7")
        self.assertEqual(got["lines"][4], "refused down: it needs the id of a note")
        self.assertEqual(got["lines"][5], "refused up: it needs the id of a note")
        self.assertEqual(len(got["problems"]), 3)
        self.assertEqual(got["summary"], "#1 up · #1 up · #1 down · 3 refused")
        note = self.book()["notes"][0]
        self.assertEqual((note["up"], note["down"]), (2, 1))

    def test_words_that_are_not_a_note_are_refused_in_words(self):
        got = notebook.apply(self.conn, [op("add", None, "   "), op("add", None, None),
                                         op("edit", 1, "new words"), {"op": "add"}, "junk"],
                             self.turn())
        self.assertEqual(got["lines"], [
            "refused add: an empty note is not written down",
            "refused add: an empty note is not written down",
            "refused edit: 'edit' is not something a notebook does -- add, remove, up or down",
            "refused add: an empty note is not written down"])
        self.assertEqual(self.book()["notes"], [])

    # -- turns ---------------------------------------------------------------

    def test_turns_count_its_answers_after_and_through_the_removal(self):
        born = self.turn()
        notebook.apply(self.conn, [op("add", None, "a"), op("add", None, "b")], born)
        self.assertEqual([n["turns"] for n in self.book()["notes"]], [0, 0])
        db.add_row(self.conn, "user", "a person's line is not a turn of its own")
        db.add_row(self.conn, home.SELF_INTERIM, "nor is a line while looking")
        self.turn()
        self.assertEqual([n["turns"] for n in self.book()["notes"]], [1, 1])
        gone = self.turn()
        notebook.apply(self.conn, [op("remove", 2)], gone)
        self.turn()
        self.turn()
        table = {n["id"]: n["turns"] for n in notebook.status(self.conn)["notes"]}
        self.assertEqual(table, {1: 4, 2: 2})
        same = self.turn()
        notebook.apply(self.conn, [op("add", None, "c"), op("remove", 3)], same)
        self.assertEqual(notebook.status(self.conn)["notes"][2]["turns"], 0)

    # -- the cap -------------------------------------------------------------

    def test_a_note_may_go_five_percent_past_and_then_it_locks(self):
        notebook.set_cap(100)
        first = notebook.apply(self.conn, [op("add", None, "x" * 360)], self.turn())
        self.assertEqual(first["lines"], ["added #1 (90 tokens) — 90 of 100 tokens (90%)"])
        past = notebook.apply(self.conn, [op("add", None, "y" * 56)], self.turn())
        self.assertEqual(past["lines"], [
            "added #2 (14 tokens) — 104 of 100 tokens (104%): past the cap now, "
            "so adding waits until notes are removed"])
        self.assertIn("locked", self.book())
        self.assertIn(home.OWNER_NAME, self.book()["locked"])
        self.assertEqual(self.book()["used"], "104 of 100 tokens (104%)")
        shut = notebook.apply(self.conn, [op("add", None, "z")], self.turn())
        self.assertEqual(shut["lines"], [
            "refused add: the notebook is past its cap (104 of 100 tokens (104%)), so "
            "adding waits until I remove notes or " + home.OWNER_NAME + " raises the cap"])
        # A removal in the same answer makes room for the add after it.
        room = notebook.apply(self.conn, [op("remove", 1), op("add", None, "w" * 400)],
                              self.turn())
        self.assertEqual(room["lines"][1],
                         "refused add: that note is 100 tokens and would take the notebook "
                         "to 114 of 100 tokens (114%); a note may go at most 5% past the "
                         "cap, which leaves room for 91 more")
        self.assertNotIn("locked", self.book())
        fits = notebook.apply(self.conn, [op("add", None, "v" * 364)], self.turn())
        self.assertTrue(fits["lines"][0].startswith("added #3 (91 tokens) — 105 of 100"), fits)

    def test_the_percentage_never_reads_full_unless_it_is(self):
        self.assertEqual(notebook.pct(4999, 5000), 99)
        self.assertEqual(notebook.pct(5000, 5000), 100)
        self.assertEqual(notebook.pct(5001, 5000), 101)
        self.assertEqual(notebook.pct(0, 5000), 0)

    def test_the_owners_cap_refuses_nonsense_and_keeps_its_file(self):
        self.assertEqual(notebook.cap(), 5000)
        for bad in (99, 100_001, "abc", None, ""):
            with self.assertRaises(notebook.Refused):
                notebook.set_cap(bad)
        self.assertEqual(notebook.set_cap("7,500"), 7500)
        self.assertEqual(notebook.cap(), 7500)
        kept = json.loads((self.temp / "data" / "notebook.json").read_text(encoding="utf-8"))
        self.assertEqual(kept, {"cap_tokens": 7500})
        (self.temp / "data" / "notebook.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(notebook.cap(), 5000)
        (self.temp / "data" / "notebook.json").write_text('{"cap_tokens": 5}', encoding="utf-8")
        self.assertEqual(notebook.cap(), 5000)

    def test_the_table_carries_everything_the_page_draws(self):
        notebook.apply(self.conn, [op("add", None, "one"), op("up", 1)], self.turn())
        table = notebook.status(self.conn)
        self.assertEqual({k: table[k] for k in ("cap", "grace_pct", "used", "pct", "locked")},
                         {"cap": 5000, "grace_pct": 5, "used": 1, "pct": 0, "locked": False})
        self.assertEqual(set(table["notes"][0]),
                         {"id", "dt", "text", "tokens", "up", "down", "turns", "gone", "gone_dt"})

    def test_the_backup_carries_the_book_and_its_cap(self):
        notebook.set_cap(4321)
        notebook.apply(self.conn, [op("add", None, "rides out in the zip")], self.turn())
        self.stack.enter_context(patch.object(backup, "DATA", self.temp / "data"))
        self.stack.enter_context(patch.object(backup, "TRACKED", self.temp / "tracked.zip"))
        note = backup.take(self.temp / "shelf")
        self.assertEqual(note["counts"]["notebook"], 1)
        self.assertIn("notebook.json", note["also"])
        with zipfile.ZipFile(note["file"]) as z:
            z.extract(backup.STORE_IN_ZIP, self.temp / "out")
        import sqlite3
        copy_ = sqlite3.connect(self.temp / "out" / backup.STORE_IN_ZIP)
        try:
            self.assertEqual(copy_.execute("SELECT text FROM notebook").fetchall(),
                             [("rides out in the zip",)])
        finally:
            copy_.close()

    # -- what it is told, and what it can answer ----------------------------

    def test_the_schema_and_both_kinds_of_instructions_carry_it(self):
        schema = brain.response_schema()
        self.assertEqual(schema["properties"]["notebook"],
                         {"type": "array", "items": notebook.OP})
        self.assertIn("notebook", schema["required"])
        self.assertEqual(set(notebook.OP["required"]), set(notebook.OP["properties"]))
        strict = providers.strict_schema(schema)
        self.assertIn("notebook", strict["required"])
        for codex in (False, True):
            with patch.object(providers, "codex_only", return_value=codex):
                text = brain.harness_text()
                self.assertEqual(text.count("## `notebook`"), 1, codex)
                self.assertIn("5% past its cap", text)
                self.assertIn("ask " + home.OWNER_NAME + " to raise the cap", text)
                self.assertNotIn("{{", text[text.index("## `notebook`"):])
        self.assertLess(len(notebook.INSTRUCTIONS.split()), 200)

    def run_turn(self, *answers, looks=False):
        """A whole turn over the temporary store, the model stood in for."""
        for module, name, value in (
            (brain, "voice_status", {"on": False, "reason": "test"}),
            (brain.recall, "before_she_speaks", None),
            (brain.clock, "for_prompt", {}), (brain.clock, "apply_her_word", []),
            (brain.watch, "for_prompt", {}), (brain.watch, "looks", {}),
            (brain.watch, "senses", {}), (brain, "_purse", None),
            (brain, "plan_block", {}), (providers, "codex_only", False),
        ):
            self.stack.enter_context(patch.object(module, name, return_value=value))
        self.stack.enter_context(patch.object(brain.overmind, "STATE",
                                              self.temp / "overmind.json"))
        self.stack.enter_context(patch.object(brain.claude_sessions, "STATE",
                                              self.temp / "claude_sessions.json"))
        self.stack.enter_context(patch.object(jobs, "JOBS_PATH", self.temp / "jobs.json"))
        sent = []

        def model(system, prompt_json, **kw):
            sent.append(json.loads(prompt_json))
            got = copy.deepcopy(answers[len(sent) - 1])
            got["_meta"] = {"model_key": "claude_code/opus"}
            return got

        self.stack.enter_context(patch.object(brain, "call_claude", side_effect=model))
        db.add_row(self.conn, "user", "hello")
        return brain.run_turn(self.conn, model="opus"), sent

    def test_a_turn_applies_it_after_the_answer_and_reports_it_next_turn(self):
        out, sent = self.run_turn(answer(notebook=[
            op("add", None, "the first thing I keep"), op("up", 1), op("remove", 5)]))
        self.assertEqual(list(sent[0])[:3], ["messages", "essences", "notebook"])
        self.assertEqual(sent[0]["notebook"], {"used": "0 of 5000 tokens (0%)", "notes": []})
        booked = out["applied"]["notebook"]
        self.assertEqual(booked["lines"][0][:9], "added #1 ")
        self.assertEqual(booked["lines"][2], "refused remove: there is no note #5")
        ev = [e for e in db.events_for(self.conn, out["applied"]["reply_row"])
              if e["kind"] == "notebook"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["summary"], "added #1 · #1 up · 1 refused")
        prompt = brain.build_prompt(self.conn)
        self.assertEqual(prompt["notebook"]["notes"],
                         [{"id": 1, "up": 1, "down": 0, "turns": 0,
                           "tokens": db.est_tokens("the first thing I keep"),
                           "text": "the first thing I keep"}])
        self.assertEqual(prompt["report"]["notebook"], booked["lines"])
        self.assertIn("My notebook refused an operation -- refused remove: there is no note #5",
                      prompt["report"]["problems"])

    def test_a_look_round_leaves_the_book_alone_and_says_so(self):
        out, sent = self.run_turn(
            answer(look_first=True, looking="checking", notebook=[op("add", None, "early")],
                   files=[{"op": "list", "path": None, "from_line": None,
                           "lines": None, "pattern": None}]),
            answer(notebook=[]))
        self.assertEqual(len(sent), 2)
        self.assertIn("notebook operations", out["applied"]["looked"][0]["not_done"])
        self.assertEqual(notebook.for_prompt(self.conn)["notes"], [])
        self.assertIsNone(out["applied"]["notebook"])

    def test_a_book_that_will_not_open_costs_the_block_not_the_turn(self):
        with patch.object(notebook, "for_prompt", side_effect=RuntimeError("no table")):
            block = brain._notebook_block(self.conn)
        self.assertIn("could not be read", block["broken"])

    def test_only_the_owner_moves_the_cap_and_anyone_paired_reads_the_table(self):
        import threading
        import urllib.error
        import urllib.request
        from . import app, people
        self.stack.enter_context(patch.object(people, "TOKEN_DIR", self.temp / "people"))
        owner, other = home.OWNER, [p for p in home.HOUSEHOLD if p != home.OWNER][0]
        keys = {who: people.mint(who) for who in (owner, other)}
        room = app.OneRoom(("127.0.0.1", 0), app.Handler)
        self.stack.callback(room.server_close)
        self.stack.callback(room.shutdown)
        threading.Thread(target=room.serve_forever, daemon=True).start()

        def ask(path, who=None, body=None):
            headers = {"Content-Type": "application/json"}
            if who:
                headers["Cookie"] = home.COOKIE + "=" + keys[who]
            req = urllib.request.Request(
                "http://127.0.0.1:" + str(room.server_address[1]) + path,
                data=json.dumps(body).encode("utf-8") if body is not None else None,
                headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                raw = e.read()
                return e.code, json.loads(raw) if raw else None

        self.assertEqual(ask("/api/notebook")[0], 403)
        code, got = ask("/api/notebook", other)
        self.assertEqual((code, got["cap"]), (200, 5000))
        code, got = ask("/api/notebook", other, {"cap": 9000})
        self.assertEqual(code, 403)
        self.assertEqual(got["error"], "the notebook's cap is " + home.OWNER_NAME + "'s to move")
        self.assertEqual(notebook.cap(), 5000)
        code, got = ask("/api/notebook", owner, {"cap": 9000})
        self.assertEqual((code, got["cap"]), (200, 9000))
        code, got = ask("/api/notebook", owner, {"cap": "lots"})
        self.assertEqual((code, got["error"]), (400, "the cap is a whole number of tokens"))
        self.assertEqual(notebook.cap(), 9000)


if __name__ == "__main__":
    unittest.main()
