"""Intermediate replies survive later look rounds, failures and reloading."""
from contextlib import ExitStack, closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import brain, db


class InterimReplyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, 'DB_PATH', root / 'store.db'))
        self.conn = db.connect()
        self.stack.callback(self.conn.close)

    def test_reply_is_searchable_after_reopening_without_completing_request(self):
        previous = db.add_row(self.conn, 'assistant', 'Previous final answer')
        db.add_row(self.conn, 'user', 'Please look', meta={'who': 'lee'})
        writes = []
        row_id = brain._keep_interim_reply(self.conn,
            {'reply': 'I am checking the actual memories now.'}, 'lee',
            'codex/gpt-5.6-sol', lambda *args: writes.append(args))
        self.assertEqual(db.last_reply_row(self.conn), previous)
        with closing(db.connect()) as reopened:
            row = db.search(reopened, 'actual memories')[0]
            self.assertEqual(row['id'], row_id)
            self.assertEqual(row['by_model'], 'codex/gpt-5.6-sol')
            self.assertEqual(db.rooms_of(row['kind'], row['meta']), ['lee'])
        self.assertEqual(writes[0][1], 'interim')
        self.assertEqual(writes[0][2]['row']['id'], row_id)

    def test_blank_reply_is_not_a_message_and_recipient_is_preserved(self):
        self.assertIsNone(brain._keep_interim_reply(self.conn,
            {'reply': '  '}, 'sam', None, lambda *args: None))
        row_id = brain._keep_interim_reply(self.conn,
            {'reply': 'Checking.', 'to': 'angel', 'room': 'both'},
            'sam', None, lambda *args: None)
        row = db.get_row(self.conn, row_id)
        self.assertEqual(row['meta']['to'], 'angel')
        self.assertEqual(set(db.rooms_of(row['kind'], row['meta'])), {'sam', 'lee'})

    def test_second_call_failure_keeps_first_reply_and_clears_preview(self):
        db.add_row(self.conn, 'user', 'Please search')
        for name, value in (
            ('voice_status', {'on': False}), ('room_recap', None),
            ('system_prompt', 'test'),
            ('build_prompt', {'messages': [], 'essences': [],
                              'self': {'prompt_tokens_est': 0}}),
            ('_look_round', {'round': 1}),
        ):
            self.stack.enter_context(patch.object(brain, name, return_value=value))
        self.stack.enter_context(patch.object(brain.recall, 'before_she_speaks', return_value=None))
        self.stack.enter_context(patch.object(brain, 'call_claude', side_effect=[
            {'reply': 'Checking the balcony memories.', 'look_first': True,
             '_meta': {'model_key': 'codex/gpt-5.6-sol'}},
            brain.TurnBroke('second call failed'),
        ]))
        writes = []
        with self.assertRaisesRegex(brain.TurnBroke, 'second call failed'):
            brain.run_turn(self.conn, model='codex/gpt-5.6-sol', on_write=writes.append)
        self.assertEqual(writes, [None])
        self.assertEqual(len(db.search(self.conn, 'balcony memories')), 1)
        self.assertIsNone(db.last_reply_row(self.conn))


if __name__ == '__main__':
    unittest.main()
