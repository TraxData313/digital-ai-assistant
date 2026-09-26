"""The angel inbox hands over the assistant's replies addressed to angel, once.

Wants nothing: a scratch store and inbox file in a temporary folder."""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from . import angel, brain, db, home


class AngelInboxTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.inbox_path = root / 'angel.inbox.json'
        self.stack.enter_context(patch.object(db, 'DB_PATH', root / 'store.db'))
        self.stack.enter_context(patch.object(angel, 'INBOX_PATH', self.inbox_path))
        self.stack.enter_context(patch.object(angel, 'COLLECTED', set()))
        self.stack.enter_context(patch.object(angel, 'SELF_AFTER', {}))
        self.conn = db.connect()
        self.stack.callback(self.conn.close)

    def reply(self, text, to):
        return db.add_row(self.conn, home.SELF, text, meta={'to': to})

    def forget_process(self):
        """As if the room had restarted: only the file remembers."""
        angel.COLLECTED.clear()
        angel.SELF_AFTER.clear()

    def test_a_reply_to_angel_is_handed_over_once(self):
        db.add_row(self.conn, 'angel', 'hello from angel')
        self.reply('for the room', 'room')
        ping = self.reply('Angel, are you there?', 'angel')
        got = angel.inbox(self.conn)
        self.assertEqual([l['row'] for l in got], [ping])
        self.assertEqual(got[0]['text'], 'Angel, are you there?')
        self.assertEqual(angel.inbox(self.conn), [])
        self.forget_process()
        self.assertEqual(angel.inbox(self.conn), [])

    def test_history_before_the_last_angel_line_is_not_new(self):
        old = self.reply('an old line to angel', 'angel')
        db.add_row(self.conn, 'angel', 'angel answered it')
        self.assertEqual(angel.inbox(self.conn), [])
        new = self.reply('a new one', 'angel')
        self.assertEqual([l['row'] for l in angel.inbox(self.conn)], [new])
        self.assertNotIn(old, json.loads(
            self.inbox_path.read_text(encoding='utf-8'))['collected'])

    def test_the_floor_is_kept_across_a_restart(self):
        db.add_row(self.conn, 'angel', 'first angel line')
        angel.inbox(self.conn, peek=True)
        floor = json.loads(self.inbox_path.read_text(encoding='utf-8'))['self_after']
        self.forget_process()
        # A later angel line must not move the floor over an unread ping.
        ping = self.reply('while angel was away', 'angel')
        db.add_row(self.conn, 'angel', 'angel came back and spoke first')
        self.assertEqual([l['row'] for l in angel.inbox(self.conn)], [ping])
        self.assertEqual(json.loads(
            self.inbox_path.read_text(encoding='utf-8'))['self_after'], floor)

    def test_peek_does_not_collect(self):
        ping = self.reply('peek at me', 'angel')
        self.assertEqual([l['row'] for l in angel.inbox(self.conn, peek=True)], [ping])
        self.assertEqual([l['row'] for l in angel.inbox(self.conn)], [ping])

    def test_old_tell_rows_still_count(self):
        told = db.add_row(self.conn, 'tell', 'an old tell', meta={'to': 'angel'})
        self.assertEqual([l['row'] for l in angel.inbox(self.conn)], [told])


class QuietAngelTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, 'DB_PATH', root / 'store.db'))
        self.conn = db.connect()
        self.stack.callback(self.conn.close)

    def test_angel_answering_a_reply_to_angel_is_quiet(self):
        ping = db.add_row(self.conn, home.SELF, 'Angel?', meta={'to': 'angel'})
        db.add_row(self.conn, 'angel', 'here', meta={'reply_to': ping})
        self.assertTrue(brain._only_angel_replies(self.conn))

    def test_angel_answering_a_line_to_the_room_is_heard(self):
        said = db.add_row(self.conn, home.SELF, 'Hello all', meta={'to': 'room'})
        db.add_row(self.conn, 'angel', 'hi', meta={'reply_to': said})
        self.assertFalse(brain._only_angel_replies(self.conn))

    def test_a_person_in_the_mix_is_heard(self):
        ping = db.add_row(self.conn, home.SELF, 'Angel?', meta={'to': 'angel'})
        db.add_row(self.conn, 'angel', 'here', meta={'reply_to': ping})
        db.add_row(self.conn, 'user', 'me too')
        self.assertFalse(brain._only_angel_replies(self.conn))


if __name__ == '__main__':
    unittest.main()
