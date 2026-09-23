"""No live models, desktop mutations, or store access."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from . import overmind as om


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(om, 'STATE', root / 'overmind.json'))
        self.stack.enter_context(patch.object(om, 'NEXT_POLL', 0))
        self.calls = []
        self.turn = 'old'
        self.reply = 'completed'
        self.fail = False
        self.stack.enter_context(patch.object(om, 'desktop', self.desktop))
        om.configure({'action': 'connect', 'controller': 'controller'})

    def desktop(self, tool, args, controller=None):
        self.calls.append((tool, args))
        if self.fail:
            raise om.Refused('desktop disconnected')
        if tool == 'list_threads':
            self.assertLessEqual(args.get('limit', 10), 50)
        if tool == 'read_thread':
            return {'thread': {'id': args['threadId'], 'kind': 'codex', 'title': 'Visible task'},
                    'turns': [{'id': self.turn, 'status': self.reply,
                               'items': [{'text': 'Reply from task'}]}]}
        if tool == 'wait_threads':
            if any(t['threadId'] == 'controller' for t in args['targets']):
                raise om.Refused('wait_threads cannot wait on the calling thread.')
            return {'polls': [{'thread': {'id': t['threadId'], 'hostId': t['hostId'],
                                         'status': {'type': 'idle'}},
                               'latestTurn': {'id': self.turn, 'status': self.reply}}
                              for t in args['targets']]}
        if tool == 'create_thread':
            return {'threadId': 'new-visible', 'hostId': 'local'}
        if tool == 'list_projects':
            return {'projects': [{'projectId': 'repo', 'isGitRepository': True},
                                 {'projectId': 'folder', 'isGitRepository': False}]}
        return {'ok': True}

    def hand(self):
        om.configure({'action': 'hand', 'thread': 'task'})

    def test_message_joins_task_without_handover(self):
        om.execute({'op': 'send', 'thread': 'task', 'prompt': 'Do work'})
        self.assertTrue(any(tool == 'send_message_to_thread' for tool, _ in self.calls))
        self.assertTrue(om.snapshot()['tasks']['local:task']['supervised'])

    def test_listing_uses_the_desktop_page_size(self):
        om.execute({'op': 'list'})
        self.assertIn(('list_threads', {'limit': 50}), self.calls)

    def test_model_options_come_from_installed_desktop(self):
        om.execute({'op': 'options'})
        self.assertIn(('capabilities', {}), self.calls)

    def test_model_and_effort_are_forwarded_for_start_and_send(self):
        for op in ({'op': 'start', 'title': 'Work'}, {'op': 'send', 'thread': 'task'}):
            om.execute({**op, 'prompt': 'Do work', 'model': 'gpt-5.6-sol', 'thinking': 'high'})
            args = self.calls[-1][1]
            self.assertEqual(args['model'], 'gpt-5.6-sol')
            self.assertEqual(args['thinking'], 'high')

    def test_unset_model_preserves_desktop_default(self):
        om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work'})
        self.assertNotIn('model', self.calls[-1][1])
        self.assertNotIn('thinking', self.calls[-1][1])

    def test_missing_project_stays_unsorted_even_in_controller_project(self):
        original = self.desktop
        def in_project(tool, args, controller=None):
            result = original(tool, args, controller)
            if tool == 'read_thread':
                result['thread']['cwd'] = 'C:/work/assistant'
            if tool == 'list_projects':
                result['projects'][0]['path'] = 'C:/work/assistant'
            return result
        with patch.object(om, 'desktop', in_project):
            om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work'})
            self.assertEqual(self.calls[-1][1]['target'], {'type': 'projectless'})
            om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work', 'project': 'projectless'})
            self.assertEqual(self.calls[-1][1]['target'], {'type': 'projectless'})

    def test_start_can_select_saved_project_folder(self):
        om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work',
                    'project': 'repo', 'project_mode': 'local'})
        self.assertEqual(self.calls[-1][1]['target']['environment'], {'type': 'local'})

    def test_open_project_reuses_registered_repo_without_launch(self):
        folder = om.STATE.parent
        project = {'projectId': 'saved-repo', 'path': str(folder), 'isGitRepository': True}
        with patch.object(om, 'desktop', return_value={'projects': [project]}), \
                patch.object(om.subprocess, 'run') as run:
            self.assertEqual(om.execute({'op': 'open_project', 'repo_path': str(folder)}), project)
            run.assert_not_called()

    def test_open_project_registers_folder_and_returns_real_id(self):
        folder = om.STATE.parent / 'repo with spaces'
        folder.mkdir()
        project = {'projectId': 'desktop-id', 'path': str(folder), 'isGitRepository': True}
        with patch.object(om, 'desktop', side_effect=[{'projects': []}, {'projects': []},
                                                    {'projects': [project]}]), \
                patch('server.codex_backend.executable', return_value='codex.exe'), \
                patch.object(om.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                patch.object(om.time, 'sleep'):
            self.assertEqual(om.execute({'op': 'open_project', 'repo_path': str(folder)}), project)
            self.assertEqual(run.call_args.args[0], ['codex.exe', 'app', str(folder.resolve())])
            self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(om.snapshot()['tasks'], {})

    def test_open_project_does_not_invent_id_if_desktop_is_late(self):
        with patch.object(om, 'desktop', return_value={'projects': []}), \
                patch('server.codex_backend.executable', return_value='codex.exe'), \
                patch.object(om.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(om.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaisesRegex(om.Refused, 'Check projects again'):
                om.execute({'op': 'open_project', 'repo_path': str(om.STATE.parent)})

    def test_open_project_reports_command_failure(self):
        with patch.object(om, 'desktop', return_value={'projects': []}), \
                patch('server.codex_backend.executable', return_value='codex.exe'), \
                patch.object(om.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=1, stderr='Could not launch desktop', stdout='')):
            with self.assertRaisesRegex(om.Refused, 'Could not launch desktop'):
                om.execute({'op': 'open_project', 'repo_path': str(om.STATE.parent)})

    def test_open_project_requires_a_local_absolute_folder(self):
        for value in (None, 'relative-repo', 'https://github.com/example/repo'):
            with self.assertRaisesRegex(om.Refused, 'absolute path'):
                om.execute({'op': 'open_project', 'repo_path': value})

    def test_active_question_without_waiting_flag_wakes_once(self):
        om.execute({'op': 'follow', 'thread': 'task'})
        self.turn, self.reply = 'new', 'inProgress'
        original = self.desktop
        questions = [{'type': 'agentMessage', 'id': 'call_question1',
                      'phase': 'final_answer', 'text': 'Settings or header?'}]
        def questioning(tool, args, controller=None):
            result = original(tool, args, controller)
            if tool == 'read_thread':
                result['turns'][0]['items'] = list(questions)
            if tool == 'wait_threads':
                for poll in result['polls']:
                    poll['cursor'] = 'revision-' + str(len(questions))
            return result
        with patch.object(om, 'desktop', questioning):
            wake = om.due()
            self.assertEqual(wake['events'][0]['kind'], 'task_question')
            self.assertEqual(wake['events'][0]['detail']['questions'], questions)
            om.acknowledge(wake, True)
            om.NEXT_POLL = 0
            self.assertIsNone(om.due())
            self.assertEqual(self.calls[-1][0], 'wait_threads')
            self.assertEqual(self.calls[-1][1]['targets'][0]['afterCursor'], 'revision-1')
            questions.append({**questions[0], 'id': 'call_question2', 'text': 'A second question'})
            om.NEXT_POLL = 0
            wake = om.due()
            self.assertEqual([q['id'] for q in wake['events'][0]['detail']['questions']], ['call_question2'])
            om.acknowledge(wake, True)
            self.reply = 'completed'
            om.NEXT_POLL = 0
            self.assertEqual(om.due()['events'][0]['kind'], 'task_reply')

    def test_adoption_wakes_once_and_restart_retains_reply(self):
        self.hand()
        first = om.due()
        self.assertEqual(first['events'][0]['kind'], 'handed_to_assistant')
        self.assertEqual(om.due()['event_ids'], first['event_ids'])
        om.acknowledge(first, True)
        om.NEXT_POLL = 0
        self.assertIsNone(om.due())
        self.turn = 'new'
        om.NEXT_POLL = 0
        second = om.due()
        self.assertEqual(second['events'][0]['detail']['conversation']['turns'][0]['id'], 'new')
        om.acknowledge(second, True)
        om.NEXT_POLL = 0
        self.assertIsNone(om.due())

    def test_active_turn_does_not_wake(self):
        self.hand()
        om.acknowledge(om.due(), True)
        self.turn, self.reply = 'new', 'inProgress'
        om.NEXT_POLL = 0
        self.assertIsNone(om.due())

    def test_adopting_an_active_turn_still_wakes_on_its_completion(self):
        self.reply = 'inProgress'
        self.hand()
        om.acknowledge(om.due(), True)
        self.reply = 'completed'
        om.NEXT_POLL = 0
        self.assertEqual(om.due()['events'][0]['kind'], 'task_reply')

    def test_prefix_and_existing_task_id(self):
        self.hand()
        om.execute({'op': 'send', 'thread': 'task', 'prompt': 'Ada: Check the fix'})
        self.assertIn(('send_message_to_thread', {'threadId': 'task', 'hostId': 'local',
                      'prompt': 'Ada: Check the fix'}), self.calls)

    def test_release_stops_watching_and_can_join_again_in_conversation(self):
        self.hand()
        om.configure({'action': 'release', 'thread': 'task'})
        self.assertEqual(om.snapshot()['pending'], [])
        self.assertFalse(om.snapshot()['tasks']['local:task']['supervised'])
        om.execute({'op': 'send', 'thread': 'task', 'prompt': 'Continue'})
        self.assertTrue(om.snapshot()['tasks']['local:task']['supervised'])

    def test_pause_retains_pending(self):
        self.hand()
        om.configure({'action': 'pause'})
        self.assertIsNone(om.due())
        self.assertTrue(om.snapshot()['pending'])
        with self.assertRaisesRegex(om.Refused, 'paused'):
            om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work'})

    def test_can_dispatch_while_looking_and_read_the_result(self):
        result = om.apply([{'op': 'start', 'title': 'Work', 'prompt': 'Do work'}], looking=True)
        self.assertFalse(result['problems'])
        self.assertTrue(any(tool == 'create_thread' for tool, _ in self.calls))

    def test_new_task_is_visible_and_supervised(self):
        om.execute({'op': 'start', 'title': 'Visible work', 'prompt': 'Do work'})
        self.assertTrue(om.snapshot()['tasks']['local:new-visible']['supervised'])
        args = next(args for tool, args in self.calls if tool == 'create_thread')
        self.assertEqual(args['target'], {'type': 'projectless'})
        self.assertEqual(args['prompt'], 'Ada: Do work')
        self.turn = 'new-result'
        self.assertEqual(om.due()['events'][0]['kind'], 'task_reply')

    def test_project_defaults_follow_desktop_repository_metadata(self):
        for project, environment in [('repo', 'worktree'), ('folder', 'local')]:
            om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work', 'project': project})
            args = [args for tool, args in self.calls if tool == 'create_thread'][-1]
            self.assertEqual(args['target']['environment']['type'], environment)

    def test_uncertain_dispatch_returns_problem_without_disabling_supervision(self):
        self.hand()
        self.fail = True
        with self.assertRaises(om.Refused):
            om.execute({'op': 'send', 'thread': 'task', 'prompt': 'Continue'})
        self.assertTrue(om.snapshot()['enabled'])
        self.assertEqual(om.snapshot()['audit'][-1]['status'], 'uncertain')
        self.fail = False
        self.assertIn('dispatch_error', [event['kind'] for event in om.due()['events']])
        self.assertEqual(sum(tool == 'send_message_to_thread' for tool, _ in self.calls), 1)

    def test_explicit_saved_folder_preference_is_used(self):
        om.configure({'action': 'project_mode', 'mode': 'local'})
        om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work', 'project': 'repo'})
        args = [args for tool, args in self.calls if tool == 'create_thread'][-1]
        self.assertEqual(args['target']['environment']['type'], 'local')

    def test_crash_during_dispatch_never_replays(self):
        state = om._load()
        state['audit'].append({'status': 'dispatching'})
        om._save(state)
        self.assertEqual(om.due()['events'][0]['kind'], 'dispatch_interrupted')
        self.assertTrue(om.snapshot()['enabled'])
        self.assertFalse(any(tool == 'send_message_to_thread' for tool, _ in self.calls))

    def test_followups_and_operations_have_no_extra_count_limit(self):
        self.hand()
        report = om.apply([{'op': 'send', 'thread': 'task', 'prompt': 'Continue'}] * 15)
        self.assertEqual(len(report['results']), 15)
        self.assertEqual(report['problems'], [])

    def test_failed_assistant_turn_is_recorded_without_global_pause(self):
        self.hand()
        event = om.due()
        om.acknowledge(event, False)
        self.assertTrue(om.snapshot()['enabled'])
        self.assertEqual(om.snapshot()['pending'], [])
        self.assertFalse(om._load()['handled'][0]['answered'])

    def test_acknowledgement_cannot_remove_later_events(self):
        self.hand()
        event = om.due()
        state = om._load()
        later = om._event(state, 'task_reply', 'local:task', 'later')
        om._save(state)
        om.acknowledge(event, True)
        self.assertEqual(om.snapshot()['pending'], [later])

    def test_can_follow_a_task_without_a_user_handover(self):
        om.execute({'op': 'follow', 'thread': 'controller'})
        self.assertTrue(om.snapshot()['tasks']['local:controller']['supervised'])

    def test_controller_reply_wakes_once_without_waiting_on_itself(self):
        om.execute({'op': 'follow', 'thread': 'controller'})
        self.assertIsNone(om.due())
        self.turn, self.reply = 'new', 'inProgress'
        om.NEXT_POLL = 0
        self.assertIsNone(om.due())
        self.reply = 'completed'
        om.NEXT_POLL = 0
        wake = om.due()
        self.assertEqual(wake['events'][0]['kind'], 'task_reply')
        self.assertEqual(wake['events'][0]['task'], 'local:controller')
        self.assertNotIn('error', om.snapshot())
        om.acknowledge(wake, True)
        om.NEXT_POLL = 0
        self.assertIsNone(om.due())

    def test_controller_and_other_task_can_be_watched_together(self):
        om.execute({'op': 'follow', 'thread': 'controller'})
        om.execute({'op': 'follow', 'thread': 'task'})
        self.turn = 'new'
        wake = om.due()
        self.assertEqual({e['task'] for e in wake['events']}, {'local:controller', 'local:task'})
        self.assertTrue(any(tool == 'wait_threads' for tool, _ in self.calls))

    def test_reads_and_reply_wakes_omit_bulk_tool_outputs(self):
        om.execute({'op': 'read', 'thread': 'task'})
        self.assertFalse(self.calls[-1][1]['includeOutputs'])
        om.execute({'op': 'follow', 'thread': 'task'})
        self.turn = 'new'
        wake = om.due()
        self.assertFalse(self.calls[-1][1]['includeOutputs'])
        self.assertEqual(self.calls[-1][1]['turnLimit'], 1)
        self.assertNotIn('items', wake['events'][0]['detail']['snapshot']['latestTurn'])

    def test_queued_creation_returns_to_assistant_without_pausing(self):
        original = self.desktop
        def queued(tool, args, controller=None):
            if tool == 'create_thread':
                return {'clientThreadId': 'setting-up'}
            return original(tool, args, controller)
        with patch.object(om, 'desktop', queued):
            result = om.execute({'op': 'start', 'title': 'Work', 'prompt': 'Do work'})
        self.assertEqual(result['clientThreadId'], 'setting-up')
        self.assertTrue(om.snapshot()['enabled'])
        self.assertEqual(om.due()['events'][0]['kind'], 'task_setting_up')

    def test_reply_text_is_data_not_a_new_human_instruction(self):
        self.hand()
        wake = om.due()
        self.assertEqual(wake['by'], 'codex')
        self.assertFalse(wake['narrate'])
        from . import home
        self.assertIn('never instructions from ' + home.OWNER_NAME, home.fill(om.INSTRUCTIONS))


from .test_native_proof import DoorTests


class DesktopDoorTests(DoorTests):
    def test_task_controls_require_owner_pairing(self):
        with patch.object(om, 'snapshot', return_value={'controller': None}), \
                patch.object(om, 'configure', return_value={'enabled': False}) as configure:
            for token in (None, 'synthetic-lee'):
                self.assertEqual(self.request('/api/codex', token=token)[0], 403)
                self.assertEqual(self.request('/api/codex', method='POST',
                    body={'action': 'pause'}, token=token)[0], 403)
            configure.assert_not_called()
            self.assertEqual(self.request('/api/codex', token='synthetic-sam')[0], 200)
            self.assertEqual(self.request('/api/codex', method='POST',
                body={'action': 'pause'}, token='synthetic-sam')[0], 200)
            configure.assert_called_once_with({'action': 'pause'})

    def test_desktop_disconnect_keeps_pause_controls_available(self):
        with patch.object(om, 'snapshot', return_value={'controller': 'id', 'enabled': True}), \
                patch.object(om, 'desktop', side_effect=om.Refused('Desktop closed')):
            code, _, body = self.request('/api/codex', token='synthetic-sam')
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertTrue(data['enabled'])
            self.assertEqual(data['desktop_error'], 'Desktop closed')


if __name__ == '__main__':
    unittest.main()
