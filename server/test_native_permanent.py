"""Permanent ordinary-chat regressions. Model/command transports are mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import codex_backend, native_tools as native, native_proof, providers
from . import test_prompt_reference as reference
from .test_native_proof import NativeClient
from .test_codex_backend import event, SCHEMA


class NativeScripted(reference.ScriptedClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.pending[0]['params']['item'].update(id='answer', phase='final_answer')

    def request(self, method, params):
        if method == 'windowsSandbox/readiness': return {'status': 'ready'}
        if method == 'config/read': return {'config': {}}
        if method == 'command/exec': return {'exitCode': 0, 'stdout': '', 'stderr': ''}
        return super().request(method, params)


class NativeMemoryTests(reference.ReferenceTests):
    """Run the actual look/answer, recall and project tests with native mode on."""
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(codex_backend, 'Client', NativeScripted))
        self.stack.enter_context(patch.object(native, 'SCRATCH_PARENT', self.temp))
        self.stack.enter_context(native.activated())


class PermanentTests(unittest.TestCase):
    def test_home_profile_preserves_authentication_and_environment_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {
                'ASSISTANT_NATIVE_PROOF': '0', 'OPENAI_API_KEY': 'synthetic-api',
                'ANTHROPIC_API_KEY': 'synthetic-anthropic', 'ROOM_PAIRING_SECRET': 'synthetic-pair',
                'UNRELATED_SECRET': 'synthetic-unrelated'}):
            cfg = native.configuration(Path(tmp))
            _, env = native.launch_options(Path(tmp))
        fs = cfg['permissions']['assistant-chat']['filesystem']
        self.assertEqual(cfg['windows']['sandbox'], 'elevated')
        self.assertEqual(fs[':root'], 'read')
        self.assertEqual(fs[str(native.ROOT)], 'read')
        self.assertEqual({p for p, mode in fs.items() if mode == 'write'},
                         {str(Path.home()), tmp})
        self.assertEqual(fs[':minimal'], 'read')
        self.assertEqual(fs[str(Path.home())], 'write')
        self.assertNotIn(str(Path.home() / 'Documents'), fs)
        for path in native.protected(): self.assertEqual(fs[str(path)], 'deny')
        self.assertEqual(cfg['approval_policy'], 'never')
        self.assertTrue(cfg['permissions']['assistant-chat']['network']['enabled'])
        self.assertEqual(cfg['shell_environment_policy']['inherit'], 'none')
        self.assertNotIn('CODEX_HOME', cfg['shell_environment_policy']['set'])
        self.assertIn('CODEX_HOME', env)
        for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ROOM_PAIRING_SECRET', 'UNRELATED_SECRET'):
            self.assertNotIn(name, env)
            self.assertNotIn(name, cfg['shell_environment_policy']['set'])

    def test_startup_failure_never_spends_a_model_turn(self):
        class RefusedClient(NativeClient):
            def request(self, method, params):
                if method == 'command/exec':
                    raise providers.TurnBroke('Windows setup refresh denied C:\\')
                return super().request(method, params)
        RefusedClient.instances = []
        steps = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(native, 'SCRATCH_PARENT', tmp), patch.object(codex_backend, 'Client', RefusedClient), native.activated():
            with self.assertRaisesRegex(providers.TurnBroke, 'setup refresh denied'):
                codex_backend.call(providers.resolve('codex/gpt-5.6-sol'), 'Spark', '{}', SCHEMA,
                    on_step=lambda *x: steps.append(x))
        self.assertFalse(any(x.get('method') == 'turn/start' for x in RefusedClient.instances[-1].sent))
        self.assertIn('cmd.exe /d /c exit 0', json.dumps(steps))
        self.assertIn('did not produce a valid answer', json.dumps(steps))

    def test_ordinary_native_command_edit_failure_and_malformed_final_are_retained(self):
        NativeClient.instances = []
        NativeClient.events = [
            event('item/completed', item={'type': 'commandExecution', 'id': 'read', 'command': 'Get-Content sample.txt', 'cwd': 'C:/Users/Room', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'value=1'}),
            event('item/completed', item={'type': 'fileChange', 'id': 'edit', 'status': 'completed', 'changes': [{'path': 'sample.txt', 'diff': '-value=1\n+value=2'}]}),
            event('item/completed', item={'type': 'commandExecution', 'id': 'verify', 'command': 'assert value=2', 'cwd': 'C:/Users/Room', 'status': 'completed', 'exitCode': 0, 'stdout': 'VERIFY_OK', 'stderr': ''}),
            event('item/completed', item={'type': 'commandExecution', 'id': 'fail', 'command': 'cmd /c exit 7', 'cwd': 'C:/Users/Room', 'status': 'completed', 'exitCode': 7, 'stdout': '', 'stderr': 'EXPECTED_FAILURE'}),
            event('item/completed', item={'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': 'malformed'}),
            event('turn/completed', turn={'status': 'completed'})]
        with tempfile.TemporaryDirectory() as tmp, patch.object(native, 'SCRATCH_PARENT', tmp), patch.object(native, 'LOGS', Path(tmp)/'logs'), patch.object(codex_backend, 'Client', NativeClient), patch.object(native_proof, 'fixture_text', side_effect=AssertionError('diagnostic fixture')), native.activated():
            def record(text, kind='plain', detail=None):
                if kind == 'native': native.log_activity(42, text, detail)
            with self.assertRaises(providers.TurnBroke):
                codex_backend.call(providers.resolve('codex/gpt-5.6-sol'), 'Spark', '{}', SCHEMA, on_step=record)
            log=(native.LOGS/'turn-42.jsonl').read_text(encoding='utf-8')
        for value in ('sample.txt', 'value=2', 'VERIFY_OK', 'EXPECTED_FAILURE', '"exitCode": 7', 'did not produce a valid answer'):
            self.assertIn(value, log)


if __name__ == '__main__': unittest.main()
