"""Ordinary native chat and audit persistence; no live room or model calls."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import app, db, native_tools as native, native_proof, providers, codex_backend
from .test_native_proof import DoorTests, NativeClient
from .test_codex_backend import event, SCHEMA


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for obj, name, value in ((db,"DB_PATH",self.root/'store.db'),
                                  (providers,"CHOICE_PATH",self.root/'provider.json'),
                                  (native,"LOGS",self.root/'logs'),
                                  (native,"SCRATCH_PARENT",self.root),
                                  (app,"MODEL",{'name':'codex/gpt-5.6-sol'}),
                                  (app,"LAST_TURN",{})):
            self.stack.enter_context(patch.object(obj,name,value))
        providers._save_choice(model='codex/gpt-5.6-sol',codex_only=True,native_tools=True,
                               dream_model='claude_code/opus',credits={'keep':3})
        native.CANCEL.clear()
        self.conn=db.connect()
        self.stack.callback(self.conn.close)

    def test_setting_preserves_other_fields_and_enables_by_default(self):
        providers.set_native_tools(False)
        self.assertFalse(providers.native_tools_enabled())
        providers.set_native_tools(True)
        state=json.loads(providers.CHOICE_PATH.read_text())
        self.assertEqual(state['credits'],{'keep':3})
        self.assertEqual(state['dream_model'],'claude_code/opus')
        providers.CHOICE_PATH.write_text('{}')
        self.assertTrue(providers.native_tools_enabled())
        providers.CHOICE_PATH.unlink()
        self.assertTrue(providers.native_tools_enabled())
        providers.CHOICE_PATH.write_text('broken')
        self.assertFalse(providers.native_tools_enabled())

    def test_only_human_codex_chat_can_have_tools(self):
        user={'kind':'user'}
        self.assertTrue(native.chat_allowed(user,'codex/gpt-5.6-sol'))
        self.assertFalse(native.chat_allowed({'kind':'angel'},'codex/gpt-5.6-sol'))
        self.assertFalse(native.chat_allowed(None,'codex/gpt-5.6-sol'))
        self.assertFalse(native.chat_allowed(user,'codex/gpt-5.6-sol',{'kind':'clock'}))
        self.assertFalse(native.chat_allowed(user,'lmstudio/local'))
        providers.set_native_tools(False)
        self.assertFalse(native.chat_allowed(user,'codex/gpt-5.6-sol'))

    def test_mode_and_provider_prerequisites(self):
        providers._save_choice(codex_only=False)
        providers.set_native_tools(True)
        self.assertTrue(native.chat_allowed({'kind':'user'},'codex/gpt-5.6-sol'))
        providers._save_choice(codex_only=True,model='openai/gpt-5.6-sol')
        with self.assertRaises(providers.Refused): providers.set_native_tools(True)
        with self.assertRaises(providers.Refused): providers.set_native_tools('yes')

    def test_failure_is_durable_in_database_and_jsonl(self):
        row=db.add_row(self.conn,'user','Read a fixture',meta={'who':'sam'})
        def broken(conn,**kw):
            self.assertTrue(native.ACTIVE.get())
            self.assertFalse(native.PROOF.get())
            kw['on_step']('command failed','native',{'command':'assert','cwd':'Documents','exitCode':7,'aggregatedOutput':'FAIL'})
            raise providers.TurnBroke('bad final JSON')
        with patch.object(app.brain,'run_turn',side_effect=broken):
            self.assertFalse(app.one_turn(self.conn,None))
        events=db.events_for_rows(self.conn,[row])
        self.assertTrue(any(e['detail'].get('exitCode')==7 for e in events))
        log=[json.loads(line) for line in (native.LOGS/f'turn-{row}.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['detail'].get('exitCode')==7 for e in log))
        self.assertIn('bad final JSON',log[-1]['summary'])
        self.assertFalse(native.RUNNING.is_set())
        self.assertFalse(native.ACTIVE.get())

    def test_cannot_start_tools_when_log_cannot_be_written(self):
        row=db.add_row(self.conn,'user','Read a fixture')
        with patch.object(native,'log_activity',side_effect=OSError('disk full')), patch.object(app.brain,'run_turn') as run:
            self.assertFalse(app.one_turn(self.conn,None))
            run.assert_not_called()
        self.assertIn('log unavailable',app.LAST_TURN['error'])
        self.assertTrue(db.events_for_rows(self.conn,[row]))

    def test_log_redacts_credentials(self):
        native.log_activity(1,'GET /?k=synthetic-secret',{'authorization':'synthetic-secret','text':'Cookie: ada_who=synthetic-secret'})
        self.assertNotIn('synthetic-secret',(native.LOGS/'turn-1.jsonl').read_text())

    def test_ordinary_chat_uses_native_protocol_without_fixture_restrictions(self):
        NativeClient.instances=[]
        NativeClient.account={'type':'chatgpt'}
        NativeClient.readiness='ready'
        NativeClient.events=[event('item/completed',item={'type':'agentMessage','id':'answer','phase':'final_answer','text':'{"reply":"hello","drop":[9]}'}),event('turn/completed',turn={'status':'completed'})]
        with patch.object(codex_backend,'Client',NativeClient), patch.object(native_proof,'fixture_text',side_effect=AssertionError('fixture must not be read')), native.activated(proof=False):
            answer=codex_backend.call(providers.resolve('codex/gpt-5.6-sol'),'Spark unchanged','{}',SCHEMA)
        self.assertEqual(answer['drop'],[9])
        params=dict(NativeClient.instances[-1].requests)['thread/start']
        self.assertNotIn('environments',params)
        self.assertIn('human conversation',params['baseInstructions'])
        self.assertEqual(params['permissions'], 'assistant-chat')
        self.assertEqual(params['cwd'], str(Path.home()))
        self.assertNotIn('ASSISTANT_NATIVE_PROOF', params['baseInstructions'])

    def test_auxiliary_model_call_does_not_inherit_chat_tools(self):
        NativeClient.instances=[]
        NativeClient.account={'type':'chatgpt'}
        NativeClient.events=[event('item/completed',item={'type':'agentMessage','text':'{"matches":[]}'}),event('turn/completed',turn={'status':'completed'})]
        with patch.object(codex_backend,'Client',NativeClient), native.activated(proof=False):
            codex_backend.call(providers.resolve('codex/gpt-5.6-sol'),'recall','{}',{'type':'object','properties':{'matches':{'type':'array','items':{'type':'integer'}}}},expect='matches')
        params=dict(NativeClient.instances[-1].requests)['thread/start']
        self.assertEqual(params['environments'],[])
        self.assertFalse(params['config']['features.shell_tool'])


class ChatDoorTests(DoorTests):
    def test_only_owner_can_change_setting_or_cancel(self):
        for route in ('/api/providers/native-tools','/api/native-tools/cancel'):
            self.assertEqual(self.request(route,'POST',{'enabled':True},'synthetic-lee')[0],403)
        with patch.object(providers,'set_native_tools') as setter, patch.object(providers,'catalogue',return_value={'native_tools':True}):
            self.assertEqual(self.request('/api/providers/native-tools','POST',{'enabled':True},'synthetic-sam')[0],200)
            setter.assert_called_once_with(True)
        with patch.object(native.RUNNING,'is_set',return_value=True):
            self.assertEqual(self.request('/api/native-tools/cancel','POST',{},'synthetic-sam')[0],200)
        self.assertTrue(native.CANCEL.is_set())
        native.CANCEL.clear()

    def test_export_uses_saved_events_and_requires_auth(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'store.db'):
            c=db.connect()
            row=db.add_row(c,'user','test')
            db.add_event(c,row,'native','ran assertion',{'exitCode':0,'aggregatedOutput':'OK'})
            c.close()
            self.assertEqual(self.request(f'/api/native-log?row={row}')[0],403)
            code,_,body=self.request(f'/api/native-log?row={row}',token='synthetic-sam')
            self.assertEqual(code,200)
            self.assertEqual(json.loads(body)['events'][0]['detail']['aggregatedOutput'],'OK')


if __name__=='__main__': unittest.main()
