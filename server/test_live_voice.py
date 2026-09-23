"""Room voice integration checks: no hosted models, microphones, or live memory."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import urllib.request
import urllib.error
from . import db, live_voice, voice_native, app


def event(ident, role, text, start=0, end=100):
    return {'type': ('input' if role == 'user' else 'output') + '_transcript.added',
            'item': {'id': ident, 'text': text}, 'start_ms': start, 'end_ms': end}


class VoiceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', Path(self.temp.name)/'store.db'); self.patch.start()
        conn = db.connect(); conn.close()
        self.manager = live_voice.VoiceManager(Path(self.temp.name), Mock(return_value=100))
        self.client = Mock()
        self.manager.current = {'id':'test-session', 'client':self.client, 'thread':'own-thread',
          'owner':'sam', 'who':'sam', 'voice':'sol', 'voices':['sol','juniper'], 'started':0, 'seen':0,
          'rows':{}, 'delegations':set(), 'task':None, 'timer':Mock(), 'has_input':False,
          'ignored_startup_outputs':set()}

    def tearDown(self):
        self.manager.shutdown(); self.patch.stop(); self.temp.cleanup()

    def rows(self):
        conn=db.connect()
        try:return db.all_rows(conn)
        finally:conn.close()

    def test_direction_not_turn_role_sets_speaker_and_retry_is_idempotent(self):
        events=[event('one','user','Hi Ada'),event('two','assistant','Hi Sammy'),event('three','user',', how are you?',100,200)]
        self.manager.sync('test-session','sam',events)
        self.manager.sync('test-session','sam',events)
        rows=self.rows()
        self.assertEqual([(r['kind'],r['text']) for r in rows],[('user','Hi Ada, how are you?'),('assistant','Hi Sammy')])
        self.assertTrue(all(r['meta']['voice_handled'] for r in rows))
        conn=db.connect()
        try:self.assertIsNone(app.unanswered(conn))
        finally:conn.close()

    def test_voice_reply_cannot_swallow_a_pending_text_request(self):
        conn=db.connect()
        try:rid=db.add_row(conn,'user','An ordinary task')
        finally:conn.close()
        self.manager.sync('test-session','sam',[event('a','user','Hello'),event('b','assistant','Hello too')])
        conn=db.connect()
        try:self.assertEqual(app.unanswered(conn),rid)
        finally:conn.close()

    def test_invalid_batch_does_not_partially_write(self):
        with self.assertRaises(live_voice.ProbeError):
            self.manager.sync('test-session','sam',[event('ok','user','hello'),{'type':'turn.done'}])
        self.assertEqual(self.rows(),[])

    def test_speaking_again_after_a_pause_creates_a_new_row(self):
        self.manager.sync('test-session','sam',[event('one','user','First'),event('two','user','Second',3000,3500)])
        self.assertEqual(len(self.rows()),2)

    def test_household_speaker_is_retained(self):
        self.manager.current['who']='lee'
        self.manager.sync('test-session','sam',[event('one','user','Hello')])
        self.assertEqual(self.rows()[0]['meta']['who'],'lee')

    def test_other_owner_cannot_control_session(self):
        with self.assertRaises(live_voice.ProbeError):self.manager.end('test-session','lee')
        self.client.close.assert_not_called()

    def test_stop_saves_last_words_and_closes_only_own_session(self):
        self.manager.end('test-session','sam',[event('one','user','Last words')])
        self.assertEqual(self.rows()[0]['text'],'Last words')
        self.assertIsNone(self.manager.current)
        self.client.request.assert_called_with('thread/realtime/stop',{'threadId':'own-thread'},timeout=5)
        self.manager.end('test-session','sam',[])
        self.client.close.assert_called_once()

    def test_missing_browser_lease_stops_session(self):
        with patch.object(live_voice.time,'monotonic',return_value=46):self.manager._watchdog()
        self.assertIsNone(self.manager.current)

    def test_handoff_queues_once_and_result_returns_to_voice(self):
        self.manager.sync('test-session','sam',[event('one','user','Please remember this preference.')])
        handoff={'type':'delegation.created','item':{'id':'handoff-one','target':'client'}}
        self.manager.delegate('test-session','sam',handoff,[])
        self.manager.delegate('test-session','sam',handoff,[])
        self.manager.queue_task.assert_called_once()
        self.assertIn('Please remember this preference.',self.manager.queue_task.call_args.args[1])
        self.assertIn('"selected_voice": "sol"',self.manager.queue_task.call_args.args[1])
        conn=db.connect()
        try:backend_id=db.add_row(conn,'assistant','Saved the preference.',meta={'room':'sam'})
        finally:conn.close()
        self.assertTrue(self.manager.backend_reply(100,'Saved the preference.',answer_row=backend_id))
        method,body=self.client.request.call_args.args
        self.assertEqual(method,'thread/realtime/appendText')
        self.assertEqual(body['role'],'developer')
        self.assertIn('Saved the preference.',body['text'])
        self.assertIsNone(self.manager.current['task'])
        self.assertEqual(self.manager.current['backend_row'],backend_id)
        result=self.manager.sync('test-session','sam',[
            event('spoken-result','assistant','I saved the preference.',200,400)])
        rows=self.rows();backend=next(row for row in rows if row['id']==backend_id)
        spoken=next(row for row in rows if row['text']=='I saved the preference.')
        self.assertTrue(backend['meta']['voice_backend']['delivered'])
        self.assertEqual(backend['meta']['voice_backend']['session'],'test-session')
        self.assertIn(backend_id,result['saved'])
        self.assertIn(spoken['id'],result['saved'])
        self.assertIsNone(self.manager.current['backend_row'])
        self.assertTrue(db.delivered_voice_backend(backend))
        self.assertFalse(db.delivered_voice_backend(spoken))

    def test_startup_speech_is_neither_saved_nor_resurrected_after_input(self):
        startup=event('startup','assistant','An old answer replayed at connection time.')
        self.assertEqual(self.manager.sync('test-session','sam',[startup])['saved'],[])
        self.assertEqual(self.rows(),[])
        self.manager.sync('test-session','sam',[
            startup,event('fresh-input','user','Hello now.'),
            event('fresh-answer','assistant','Hello, Sammy.',200,300)])
        self.assertEqual([(row['kind'],row['text']) for row in self.rows()],
                         [('user','Hello now.'),('assistant','Hello, Sammy.')])

    def test_official_client_delegation_shape_queues_work(self):
        self.manager.sync('test-session','sam',[event('one','user','Please check this.')])
        result=self.manager.delegate('test-session','sam',{
            'type':'session.delegation.created',
            'delegation':{'id':'official-handoff','target':'client'}},[])
        self.assertTrue(result['queued'])
        self.manager.queue_task.assert_called_once()
        self.assertIn('Please check this.',self.manager.queue_task.call_args.args[1])

    def test_backend_answer_stays_full_when_voice_delivery_fails(self):
        self.manager.text('test-session','sam','Do a task')
        conn=db.connect()
        try:backend_id=db.add_row(conn,'assistant','The durable fallback answer.',meta={'room':'sam'})
        finally:conn.close()
        self.client.request.side_effect=RuntimeError('voice disconnected')
        try:
            self.assertFalse(self.manager.backend_reply(100,'The durable fallback answer.',answer_row=backend_id))
        finally:
            self.client.request.side_effect=None
        backend=next(row for row in self.rows() if row['id']==backend_id)
        self.assertNotIn('voice_backend',backend['meta'])
        self.assertNotIn('backend_row',self.manager.current)

    def test_typed_message_queues_backend_without_interim_voice_turn(self):
        result=self.manager.text('test-session','sam','A typed question')
        self.assertEqual(self.rows()[0]['text'],'A typed question')
        self.client.request.assert_not_called()
        self.manager.queue_task.assert_called_once()
        who,request,session=self.manager.queue_task.call_args.args
        self.assertEqual((who,session),('sam','test-session'))
        self.assertIn('"message": "A typed question"',request)
        self.assertIn('Codex-task',request)
        self.assertEqual(self.manager.current['task'],100)
        self.assertEqual(result['saved'],[1,100])
        self.assertTrue(result['queued'])
        self.assertNotIn('voiceInput',result)
        self.assertIn('waiting silently',request)
        self.assertEqual(self.rows()[0]['kind'],'user')
        self.assertEqual(self.rows()[0]['meta']['voice']['capture'],'user_text')

    def test_routed_chat_replies_natively_without_room_turn(self):
        with patch.object(live_voice, 'route_text', return_value='chat'):
            result=self.manager.text('test-session','sam','How are you?',native=True)
        self.manager.queue_task.assert_not_called()
        self.assertFalse(result['queued'])
        self.assertEqual(result['voiceInput']['channel'],'developer')
        self.assertEqual(result['voiceInput']['event_id'],'typed-1')
        self.assertEqual([r['text'] for r in self.rows()],['How are you?'])
        self.assertTrue(self.manager.current['has_input'])
        self.assertIsNone(self.manager.current['task'])

    def test_routed_work_is_queued_without_waiting_for_native_delegation(self):
        with patch.object(live_voice, 'route_text', return_value='backend'):
            result=self.manager.text('test-session','sam','Remember my favorite color.',native=True)
        self.manager.queue_task.assert_called_once()
        self.assertTrue(result['queued'])
        self.assertNotIn('voiceInput',result)

    def test_conversation_length_does_not_force_backend(self):
        for message in ['I enjoyed talking with you today. '*10,
                        'Long input. '*100, 'Радвам се, че си говорим. '*30]:
            with self.subTest(message=message[:25]), patch.object(live_voice, 'route_text',return_value='chat') as route:
                result=self.manager.text('test-session','sam',message,native=True)
            route.assert_called_once()
            self.assertFalse(result['queued'])
            content=result['voiceInput']['content'][0]['text']
            self.assertEqual(json.loads(content.split('User message (JSON string): ',1)[1]),message.strip())
        self.manager.queue_task.assert_not_called()

    def test_rejected_native_packet_falls_back_once_without_duplicate_user_row(self):
        with patch.object(live_voice, 'route_text', return_value='chat'):
            result=self.manager.text('test-session','sam','Hello Ada',native=True)
        first=self.manager.text_fallback('test-session','sam',result['row'])
        self.manager.backend_reply(100,'Hello!')
        self.assertEqual(self.manager.text_fallback('test-session','sam',result['row']),first)
        self.manager.queue_task.assert_called_once()
        self.assertEqual([r['text'] for r in self.rows()],['Hello Ada'])
        with self.assertRaises(live_voice.ProbeError):
            self.manager.text_fallback('test-session','lee',result['row'])
        with self.assertRaises(live_voice.ProbeError):
            self.manager.text_fallback('test-session','sam',9999)

    def test_native_packet_quotes_and_unicode_remain_complete_user_data(self):
        message='Say "hello".\nHow are you?'
        packet=live_voice.typed_voice_input(message,1)
        content=packet['content'][0]['text']
        self.assertEqual(json.loads(content.split('User message (JSON string): ',1)[1]),message)
        content=live_voice.typed_voice_input('я'*500,1)['content'][0]['text']
        self.assertEqual(json.loads(content.split('User message (JSON string): ',1)[1]),'я'*500)

    def test_second_typed_request_waits_for_current_backend_task(self):
        self.manager.text('test-session','sam','First task')
        with self.assertRaisesRegex(live_voice.ProbeError,'current voice task'):
            self.manager.text('test-session','sam','Second task')
        self.manager.queue_task.assert_called_once()
        self.assertEqual([row['text'] for row in self.rows()],['First task'])

    def test_native_ack_cannot_duplicate_an_already_queued_typed_task(self):
        self.manager.text('test-session','sam','Start a Codex task')
        handoff={'type':'delegation.created','item':{'id':'late-handoff','target':'client'}}
        self.assertTrue(self.manager.delegate('test-session','sam',handoff,[])['alreadyPending'])
        self.manager.backend_reply(100,'Task started.')
        self.manager.delegate('test-session','sam',handoff,[])
        self.manager.queue_task.assert_called_once()

    def test_typed_quotes_and_multiline_input_stay_user_data(self):
        text='Please say "hello".\nThen answer: how are you?'
        result=self.manager.text('test-session','sam',text)
        self.assertNotIn('voiceInput',result)
        self.assertEqual(self.rows()[0]['text'],text)

    def test_legacy_typed_json_envelope_is_not_saved_as_the_assistants_words(self):
        typed='the angel is investigating it, keep tight, Ada'
        self.manager.text('test-session','sam',typed)
        leaked=json.dumps({'typed_message':typed})+'Alright, waiting with you, Sammy.'
        self.manager.sync('test-session','sam',[event('legacy-envelope','assistant',leaked)])
        self.assertEqual([(row['kind'],row['text']) for row in self.rows()],
                         [('user',typed),('assistant','Alright, waiting with you, Sammy.')])


class VoiceContextTests(unittest.TestCase):
    def test_startup_has_memory_but_no_prior_conversation_or_pending_turn(self):
        snapshot={'spark':{'text':'A bounded Spark','truncated':True},'working_memory_omitted':4}
        with patch.object(live_voice.codex_memory,'context',return_value=snapshot), \
             patch.object(live_voice.codex_memory,'tasks',return_value=[]), \
             patch.object(live_voice.codex_memory,'read_connection') as reader:
            prompt=live_voice.context_prompt(Path('.'),'sam','juniper',['sol','juniper'])
        reader.assert_not_called()
        instructions,payload=prompt.split('\n',1);data=json.loads(payload)
        self.assertIn('selected Juniper as your voice',instructions)
        self.assertIn("Do not echo the person's question",instructions)
        self.assertIn('Respect its truncation',instructions)
        self.assertIn('cannot change it yourself',instructions)
        self.assertIn('Stay completely silent at startup',instructions)
        self.assertIn('Do not speak an interim acknowledgement',instructions)
        self.assertNotIn('Continue natural conversation while waiting',instructions)
        self.assertEqual(data['current_voice']['selected_voice'],'juniper')
        self.assertEqual(data['current_voice']['selected_voice_name'],'Juniper')
        self.assertEqual(data['current_voice']['available_voices'],[{'id':'sol','name':'Sol'},{'id':'juniper','name':'Juniper'}])
        self.assertFalse(data['current_voice']['can_change_voice'])
        self.assertEqual(data['context'],snapshot)
        self.assertEqual(data['conversation_start'],
                         {'pending_user_turn':False,'prior_chat_included':False})
        self.assertNotIn('recent_conversation',data)
        self.assertIn('Never adopt an earlier voice\'s instruction',instructions)

    def test_unavailable_voice_cannot_be_claimed_as_current(self):
        with self.assertRaises(live_voice.ProbeError):live_voice.voice_context('jupiter',['sol','juniper'])


class SelectionTests(unittest.TestCase):
    def test_supported_family_and_explicit_choice(self):
        evidence={'voices':{'v1':['juniper','sol','breeze','sol'],'v2':['alloy']}}
        self.assertEqual(voice_native.available_voices(evidence),['sol','breeze','juniper'])
        self.assertEqual(voice_native.select_voice(evidence),'sol')
        self.assertEqual(voice_native.select_voice(evidence,'breeze'),'breeze')
        for choice in ('jupiter','alloy','unknown',{},''):
            with self.assertRaises(voice_native.ProbeError):voice_native.select_voice(evidence,choice)
        with self.assertRaises(voice_native.ProbeError):voice_native.select_voice({'voices':{'v1':['juniper','cove']}})
        self.assertEqual(voice_native.select_voice({'voices':{'v1':['jupiter']}},'jupiter'),'jupiter')

    def test_discovery_is_cached_and_does_not_touch_active_call(self):
        manager=live_voice.VoiceManager(Path('.'))
        active=Mock();manager.current={'client':active}
        client=Mock();client.request.return_value={'voices':{'v1':['breeze','sol'],'v2':['alloy']}}
        with patch.object(live_voice,'NativeClient',return_value=client):
            first=manager.voices();self.assertEqual(manager.voices(),first)
        self.assertEqual(first,{'voices':[{'id':'sol','name':'Sol'},{'id':'breeze','name':'Breeze'}],'defaultVoice':'sol'})
        client.request.assert_called_once_with('thread/realtime/listVoices',{})
        client.close.assert_called_once();active.request.assert_not_called()

    def test_empty_or_failed_catalog_never_becomes_a_default(self):
        for evidence in ({},{'voices':{'v2':['alloy']}},{'voices':{'v1':[]}}):
            with self.assertRaises(voice_native.ProbeError):voice_native.available_voices(evidence)
        manager=live_voice.VoiceManager(Path('.'));client=Mock()
        client.request.side_effect=voice_native.ProbeError('discovery failed')
        with patch.object(live_voice,'NativeClient',return_value=client):
            with self.assertRaises(voice_native.ProbeError):manager.voices()
        client.close.assert_called_once();self.assertIsNone(manager.catalog_cache)

    def test_start_uses_fresh_catalog_and_preserves_requested_voice(self):
        manager=live_voice.VoiceManager(Path('.'));client=Mock()
        def connect(_client,_sdp,report,_prompt,**kwargs):
            self.assertEqual(kwargs,{'voice':'breeze','voices':['sol','breeze']})
            report['startRequestAcknowledged']=True
            return 'thread','answer'
        with patch.object(live_voice,'NativeClient',return_value=client), \
             patch.object(live_voice,'preflight',return_value={'voices':{'v1':['sol','breeze']}}), \
             patch.object(live_voice,'context_prompt',return_value='context') as context, \
             patch.object(live_voice,'negotiate',side_effect=connect) as negotiate, \
             patch.object(manager,'_arm'):
            # A stale or invented selection must fail before context is sent.
            with self.assertRaises(voice_native.ProbeError):manager.start('offer','sam','sam',voice='jupiter')
            negotiate.assert_not_called()
            result=manager.start('offer','sam','sam',voice='breeze')
        context.assert_called_once_with(Path('.'),'sam','breeze',['sol','breeze'])
        self.assertEqual(manager.current['voices'],['sol','breeze'])
        self.assertEqual(result['voice'],'breeze')
        self.assertEqual(manager.current['voice'],'breeze')
        self.assertTrue(result['voiceSelection']['catalogVerified'])

    def test_native_request_contains_explicit_sol(self):
        client=Mock();client.events=[{'method':'thread/realtime/sdp','params':{'threadId':'own','sdp':'answer'}}]
        client.request.side_effect=[{'thread':{'id':'own'}},{}];client.thread_config={}
        sdp='v=0\nm=audio 9 UDP/TLS/RTP/SAVPF 111\na=sendrecv\na=ice-ufrag:test\na=ice-pwd:test\na=fingerprint:sha-256 test\na=rtpmap:111 opus/48000/2\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\n'
        voice_native.negotiate(client,sdp,{'events':[]},voice='sol',voices=['sol'])
        self.assertEqual(client.request.call_args.args[1]['voice'],'sol')


class VoiceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(app.Handler):
            def _door(self):return 'sam',''
            def log_message(self,*args):pass
        cls.server=app.OneRoom(('127.0.0.1',0),Handler)
        cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.worker.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.worker.join()
    def request(self,origin,body,action='sync'):
        return urllib.request.urlopen(urllib.request.Request(self.base+'/api/voice/'+action,data=json.dumps(body).encode(),headers={'Origin':origin,'Content-Type':'application/json','X-Assistant-Voice':'1'}),timeout=3)
    def test_discovery_endpoint_returns_catalog(self):
        catalog={'voices':[{'id':'sol','name':'Sol'}],'defaultVoice':'sol'}
        with patch.object(live_voice.manager,'voices',return_value=catalog):
            with urllib.request.urlopen(self.base+'/api/voice/voices') as response:
                self.assertEqual(json.load(response),catalog)

    def test_old_page_cannot_use_the_echoing_text_route(self):
        with patch.object(live_voice.manager,'text') as send:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(self.base,{'session':'own','text':'Hello','inputMode':'context'},'text')
            self.assertEqual(error.exception.code,400)
            self.assertIn('Refresh Ada',json.load(error.exception)['error'])
            send.assert_not_called()

    def test_text_endpoint_queues_backend_without_voice_input(self):
        result={'saved':[],'queued':True,'row':42}
        with patch.object(live_voice.manager,'text',return_value=result) as send, patch.object(db,'connect'):
            with self.request(self.base,{'session':'own','text':'Hello','inputMode':'backend'},'text') as response:
                self.assertEqual(json.load(response),dict(result,rows=[]))
            send.assert_called_once_with('own','sam','Hello')

    def test_native_routed_endpoint_and_rejected_packet_fallback(self):
        result={'saved':[],'queued':False,'row':42}
        with patch.object(live_voice.manager,'text',return_value=result) as send, patch.object(db,'connect'):
            with self.request(self.base,{'session':'own','text':'Hello','inputMode':'native-routed-v1'},'text') as response:
                self.assertEqual(json.load(response)['row'],42)
            send.assert_called_once_with('own','sam','Hello',native=True)
        with patch.object(live_voice.manager,'text_fallback',return_value={'saved':[],'queued':True}) as fallback, patch.object(db,'connect'):
            with self.request(self.base,{'session':'own','row':42},'text-fallback') as response:
                self.assertTrue(json.load(response)['queued'])
            fallback.assert_called_once_with('own','sam',42)

    def test_cross_origin_cannot_mutate(self):
        with patch.object(live_voice.manager,'sync') as sync:
            with self.assertRaises(urllib.error.HTTPError) as error:self.request('https://example.org',{})
            self.assertEqual(error.exception.code,403);sync.assert_not_called()
    def test_same_origin_sync_keeps_authenticated_owner(self):
        with patch.object(live_voice.manager,'sync',return_value={'saved':[],'active':True}) as sync, patch.object(db,'connect') as connect:
            with self.request(self.base,{'session':'own','events':[]}) as response:self.assertTrue(json.load(response)['active'])
            sync.assert_called_once_with('own','sam',[])


if __name__=='__main__':unittest.main()
