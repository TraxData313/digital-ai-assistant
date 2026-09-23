"""Memory bridge regressions on a temporary store, without model calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from . import brain, codex_memory as memory, db


class MemoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', Path(self.temp.name)/'store.db')
        self.patch.start()
        self.conn = db.connect()
        self.attr = dict(task_id='test-task', task_title='Memory test', session_id='test-session',
                         source_ref='test message', model=None, speaker='sam', capture='user_text')
        self.record = dict(op='record', operation_id='record-one', text='I prefer the example voice.', attribution=self.attr)

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.temp.cleanup()

    def count(self):
        return self.conn.execute('SELECT COUNT(*) FROM rows').fetchone()[0]

    def addition(self):
        source = memory.apply_write(self.conn, self.record)['row_id']
        return dict(op='remember', operation_id='memory-one', title='Example voice preference',
                    text='The person prefers the example voice.', source_ids=[source],
                    attribution=dict(self.attr, speaker='assistant', capture='assistant_text', model='test-model'))

    def test_record_replay_and_conflicting_retry(self):
        saved = memory.apply_write(self.conn, self.record)
        again = memory.apply_write(self.conn, self.record)
        self.assertEqual(saved['row_id'], again['row_id'])
        self.assertTrue(again['replayed'])
        self.assertFalse(saved['wakes_room'])
        self.assertFalse(db.get_row(self.conn, saved['row_id'])['loaded'])
        self.assertIsNone(db.last_reply_row(self.conn))
        with self.assertRaises(memory.Conflict):
            memory.apply_write(self.conn, dict(self.record, text='Changed retry'))
        self.assertEqual(self.count(), 1)

    def test_memory_keeps_evidence_and_loaded_state_across_edit(self):
        request = self.addition()
        with patch.object(brain, '_vector') as vector:
            saved = memory.apply_write(self.conn, request)
            old = db.get_row(self.conn, saved['row_id'])
            self.assertFalse(old['loaded'])
            self.assertEqual(old['replaces'], request['source_ids'])
            self.assertEqual(old['meta']['codex_memory']['speaker'], 'assistant')
            self.conn.execute('UPDATE rows SET loaded=1 WHERE id=?', (old['id'],))
            self.conn.commit()
            edit = dict(request, operation_id='memory-edit', target_id=old['id'],
                        expected_revision=memory.revision(db.get_row(self.conn, old['id'])), text='An updated preference.')
            updated = memory.apply_write(self.conn, edit)
            self.assertTrue(db.get_row(self.conn, updated['row_id'])['loaded'])
            self.assertFalse(db.get_row(self.conn, old['id'])['loaded'])
            self.assertEqual(db.get_row(self.conn, old['id'])['text'], request['text'])
            count = self.count()
            with self.assertRaises(memory.Conflict):
                memory.apply_write(self.conn, dict(edit, operation_id='stale-edit'))
            self.assertEqual(self.count(), count)
            vector.assert_not_called()

    def test_partial_failure_rolls_back_audit_and_allows_retry(self):
        request = self.addition()
        before = self.count()
        with patch.object(brain, '_apply_essences', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                memory.apply_write(self.conn, request)
        self.assertEqual(self.count(), before)
        saved = memory.apply_write(self.conn, request)
        self.assertFalse(saved['replayed'])

    def test_attribution_and_existing_evidence_are_required(self):
        with self.assertRaises(ValueError):
            memory.apply_write(self.conn, dict(self.record, attribution={}))
        request = self.addition()
        before = self.count()
        for invalid in (dict(request, source_ids=[99999]), dict(request, attribution=self.attr)):
            with self.assertRaises(ValueError):
                memory.apply_write(self.conn, invalid)
        self.assertEqual(self.count(), before)


if __name__ == '__main__': unittest.main()
