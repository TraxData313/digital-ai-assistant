"""Offline acceptance of compact voice controls, audio routing and lifecycle."""
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent
INDEX=(ROOT/'index.html').read_text(encoding='utf-8')
HTML=(re.search(r'<footer>[\s\S]*?</footer>',INDEX).group()
      +'<select id=person><option value=sam>Sam</option></select>')


def run():
    result={'liveCalls':0,'realMicrophoneOpened':False}
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(viewport={'width':1100,'height':800});calls=[];errors=[];held=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        behavior={'holdStart':False,'catalogError':False,'wrongVoice':False,'ended':False,'nativeText':False}
        # The room's own answer about which voice says the assistant's lines,
        # and what the speak server is doing. Kept here rather than in the page
        # because that is where it really lives: a reload must come back to the
        # same road. The backend ids are the room's: "local" and "openai".
        room={'backend':'local','switch':True,
              'local':{'on':True,'backend':'local','voice':'default','engine':'qwen','sound':True}}
        selection={'session':'test-session','sdp':'fake','voice':'sol','maxSeconds':1800,
                   'voiceSelection':{'voice':'sol','catalogVerified':True,'nativeStartAccepted':True}}
        def route(request):
            action=request.request.url.rsplit('/',1)[-1]
            if request.request.url.rstrip('/').endswith('/api/voice'):
                request.fulfill(json=dict(room,active=False,voice='sol',maxSeconds=1800));return
            if '/api/voice/' not in request.request.url:
                request.fulfill(body=HTML,content_type='text/html');return
            if action=='voices':
                if behavior['catalogError']:request.fulfill(status=503,json={'error':'Could not load voices.'})
                else:request.fulfill(json={'voices':[{'id':v,'name':v.title()} for v in ('sol','breeze','juniper')],'defaultVoice':'sol'})
                return
            body=request.request.post_data_json;calls.append((action,body))
            if action=='backend':
                room['backend']=body['backend']
                request.fulfill(json=dict(room));return
            if action=='enabled':
                room['switch']=bool(body['on'])
                request.fulfill(json=dict(room));return
            if action=='start' and behavior['holdStart']:
                held.append(request);return
            if action=='start':
                voice='sol' if behavior['wrongVoice'] else body['voice']
                request.fulfill(json=dict(selection,voice=voice,voiceSelection=dict(selection['voiceSelection'],voice=voice)))
            elif action=='stop' and behavior['ended']:
                request.fulfill(status=400,json={'error':'This voice conversation has ended or belongs to another window.'})
            elif action=='sync':
                late=any(event.get('item',{}).get('id')=='late-ack' for event in body.get('events',[]))
                rows=([{'id':91,'kind':'assistant','text':'Let me check that real quick.'}] if late else [])
                request.fulfill(json={'saved':([91] if late else []),'rows':rows,'active':True})
            elif action=='text':
                if behavior['nativeText']:
                    request.fulfill(json={'saved':[123],'rows':[],'row':123,'queued':False,'voiceInput':{
                        'type':'session.context.append','channel':'developer','event_id':'typed-123',
                        'content':[{'type':'input_text','text':'Reply to the typed greeting.'}]}})
                else:request.fulfill(json={'saved':[1,2],'rows':[], 'queued':True})
            else:request.fulfill(json={'ended':True,'queued':True,'rows':[]})
        page.route('**/*',route)
        fixture="""()=>{
          window.ASSISTANT={name:'Ada'};
          window.whoNow=()=> 'sam';window.lateAckKept=false;window.voiceRefreshes=[];
          window.assistantVoiceRows=rows=>{window.lastRows=rows;if(rows.some(row=>row.text?.includes('Let me check')))window.lateAckKept=true;};
          window.assistantVoiceRefresh=async()=>voiceRefreshes.push({lateAckKept});
          window.busyWatches=0;window.watchIfBusy=()=>busyWatches++;
          window.blockAudio=false;window.delayedTrack=false;
          document.querySelector('footer').style.cssText='position:absolute;bottom:0;width:100%;box-sizing:border-box';
          const nativePlay=HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play=function(){
            if(window.blockAudio)return Promise.reject(new DOMException('Playback blocked','NotAllowedError'));
            return nativePlay.call(this);
          };
          window.fixtureContexts=[];navigator.mediaDevices={getUserMedia:async()=>{const c=new AudioContext();fixtureContexts.push(c);const d=c.createMediaStreamDestination();window.track=d.stream.getAudioTracks()[0];return d.stream}};
          window.RTCPeerConnection=class{
           constructor(){window.peer=this;this.connectionState='connected';this.iceGatheringState='complete';this.localDescription={sdp:'fake-offer'}}
           addTrack(){} async createOffer(){return {type:'offer',sdp:'fake-offer'}} async setLocalDescription(){}
           createDataChannel(){window.channel={readyState:'open',sent:[],send(value){this.sent.push(JSON.parse(value));},close(){},addEventListener(){}};return channel}
           async setRemoteDescription(){window.inputInitiallyDisabled=!track.enabled;
             window.deliverTrack=()=>this.ontrack({streams:[new MediaStream([track])],track});
             if(!delayedTrack)deliverTrack();
             window.outputInitiallyMuted=document.getElementById('voice-audio').muted;
             channel.onmessage({data:JSON.stringify({type:'session.started',session:{id:'test'}})})}
           close(){}
          };
        }"""
        def load():
            page.goto('http://assistant.test/')
            page.add_style_tag(path=str(ROOT/'style.css'))
            page.add_script_tag(path=str(ROOT/'voice_selection.js'))
            page.evaluate(fixture)
            page.add_script_tag(path=str(ROOT/'voice.js'))
            page.wait_for_function("document.getElementById('voice-backend').value==="
                                   + repr(room['backend']))
            if room['backend']=='openai':
                page.wait_for_function("!document.getElementById('voice-start').disabled || !document.getElementById('voice-retry').hidden || document.getElementById('voice-choice-hint').textContent.includes('unavailable')")
        def pick(road):
            if room['backend']==road:return
            with page.expect_response('**/api/voice/backend'):
                page.locator('#voice-backend').select_option(road)
            page.wait_for_function("document.getElementById('voice-tools').dataset.backend==="
                                   + repr(road))
        def start():
            page.locator('#voice-start').click()
            page.wait_for_function("document.getElementById('voice-tools').dataset.state==='on'")
        def stop():
            with page.expect_response('**/api/voice/stop'):page.locator('#voice-start').click()
            page.wait_for_function("!document.getElementById('voice-start').disabled")
        def choose(voice):
            page.locator('#voice-picker').click()
            page.locator(f'#voice-options button[data-voice={voice}]').click()
        def volume(value):
            page.locator('#voice-volume').evaluate('(input,value)=>{input.value=value;input.dispatchEvent(new Event("input",{bubbles:true}))}',str(value))
        load()
        # The local voice app is the road the room comes up on, and on it the
        # bar offers one switch: the app owns the microphone, the volume and
        # the picker, so none of the three is drawn.
        assert page.locator('#voice-tools').get_attribute('data-backend')=='local'
        for hidden in ('#voice-mute','#voice-picker','#voice-volume-toggle'):
            assert page.locator(hidden).is_hidden(),hidden
        assert page.locator('#voice-start').is_enabled()
        assert page.locator('#voice-start').get_attribute('aria-pressed')=='true'
        app=page.locator('#voice-app')
        assert 'Ada’s voice is on' in app.inner_text() and 'qwen' in app.inner_text()
        assert 'how it sounds' in app.inner_text()
        # The power button on this road is the room's own switch, not a call.
        with page.expect_response('**/api/voice/enabled'):page.locator('#voice-start').click()
        page.wait_for_function("document.getElementById('voice-app').textContent==='Ada’s voice is off.'")
        assert page.locator('#voice-start').get_attribute('aria-pressed')=='false'
        assert not any(a=='start' for a,_ in calls)
        with page.expect_response('**/api/voice/enabled'):page.locator('#voice-start').click()
        page.wait_for_function("document.getElementById('voice-start').getAttribute('aria-pressed')==='true'")
        # An engine with no moods is not advertised as having them, and a road
        # that is down says why rather than claiming the assistant was heard.
        room['local']={'on':True,'backend':'local','voice':'default','engine':'pocket','sound':False}
        load();assert 'how it sounds' not in page.locator('#voice-app').inner_text()
        room['local']={'on':False,'backend':'local','reason':'speak server not answering (URLError)'}
        load();assert 'not answering' in page.locator('#voice-app').inner_text()
        assert 'warn' in page.locator('#voice-app').get_attribute('class')
        room['local']={'on':True,'backend':'local','voice':'default','engine':'qwen','sound':True}
        # And the other road brings all three controls back.
        pick('openai')
        for shown in ('#voice-mute','#voice-picker','#voice-volume-toggle'):
            assert page.locator(shown).is_visible(),shown
        assert page.locator('#voice-app').is_hidden()
        page.wait_for_function("!document.getElementById('voice-start').disabled")
        result['ownRoadHidesWhatTheAppOwns']='passed'
        assert page.locator('#voice-picker').inner_text()=='Sol'
        assert page.locator('#voice-mute').is_disabled()
        start()
        assert page.evaluate('inputInitiallyDisabled && outputInitiallyMuted && !track.enabled')
        assert page.locator('#voice-mute').get_attribute('aria-pressed')=='true'
        page.wait_for_function("!document.getElementById('voice-audio').paused")
        assert page.evaluate("document.getElementById('voice-audio').muted")
        assert page.locator('#voice-audio').is_hidden()
        assert page.locator('#voice-audio').get_attribute('controls') is None
        page.locator('#voice-picker').click()
        assert page.locator('#voice-options button[data-voice=breeze]').is_disabled()
        assert 'End voice' in page.locator('#voice-choice-hint').inner_text()
        page.keyboard.press('Escape')
        page.locator('#voice-mute').click();assert page.evaluate('track.enabled')
        page.locator('#voice-mute').click();assert not page.evaluate('track.enabled')
        page.locator('#voice-volume-toggle').click()
        volume(37)
        assert page.evaluate("document.getElementById('voice-audio').volume===.37 && document.getElementById('voice-audio').muted")
        volume(0)
        assert page.evaluate("document.getElementById('voice-audio').muted && !track.enabled")
        volume(37);page.keyboard.press('Escape')
        page.evaluate("""()=>{
          for(const x of [
           {type:'input_transcript.added',item:{id:'input-one',text:'Hi Ada'},start_ms:0,end_ms:100},
           {type:'output_transcript.added',item:{id:'output-one',text:'Hi Sammy'},start_ms:100,end_ms:200},
           {type:'session.delegation.created',delegation:{id:'work-one',target:'client'}}
          ])channel.onmessage({data:JSON.stringify(x)});
          setTimeout(()=>channel.onmessage({data:JSON.stringify(
            {type:'output_transcript.added',item:{id:'late-ack',text:'Let me check that real quick.'},start_ms:200,end_ms:350}
          )}),75);
        }""")
        page.wait_for_function("document.getElementById('voice-status').textContent.includes('working')")
        assert any(a=='delegate' for a,_ in calls)
        batch=next(b['events'] for a,b in calls if a=='sync' and b.get('events'))
        assert [e['type'] for e in batch]==['input_transcript.added','output_transcript.added']
        page.wait_for_function('voiceRefreshes.length>0')
        assert page.evaluate('voiceRefreshes[0].lateAckKept')
        assert any(a=='sync' and any(e.get('item',{}).get('id')=='late-ack' for e in b.get('events',[])) for a,b in calls)
        watches_before=page.evaluate('busyWatches')
        packets_before=page.evaluate('channel.sent.length')
        page.evaluate("AssistantVoice.sendText('A typed question')")
        assert any(a=='text' and b['text']=='A typed question' and b['inputMode']=='native-routed-v1' for a,b in calls)
        assert page.evaluate('channel.sent.length')==packets_before
        assert page.evaluate("document.getElementById('voice-status').textContent.includes('working')")
        assert page.evaluate('busyWatches')==watches_before+1
        # Chat reaches native voice without creating a backend turn. A rejected
        # packet falls back once, even if its error is delivered twice.
        behavior['nativeText']=True
        page.evaluate("AssistantVoice.sendText('Hi Ada')")
        assert page.evaluate('channel.sent.length')==packets_before+1
        assert page.evaluate('channel.sent.at(-1).channel')=='developer'
        assert page.evaluate('busyWatches')==watches_before+1
        assert not any(a=='text-fallback' for a,_ in calls)
        page.evaluate("""()=>{
          const error={type:'error',error:{event_id:'typed-123',message:'Packet rejected'}};
          channel.onmessage({data:JSON.stringify(error)});
          channel.onmessage({data:JSON.stringify(error)});
        }""")
        page.wait_for_function('busyWatches==='+str(watches_before+2))
        assert len([a for a,_ in calls if a=='text-fallback'])==1
        assert next(b for a,b in calls if a=='text-fallback')['row']==123
        behavior['nativeText']=False
        sent_before=len([a for a,b in calls if a=='text'])
        error=page.evaluate("async()=>{channel.readyState='closed';try{await AssistantVoice.sendText('Must not send')}catch(e){return e.message}finally{channel.readyState='open'}}")
        assert 'ended' in error
        assert len([a for a,b in calls if a=='text'])==sent_before
        page.evaluate("channel.onmessage({data:JSON.stringify({type:'error',error:{message:'Context rejected'}})})")
        assert page.locator('#voice-status').inner_text()=='Context rejected'
        page.evaluate('window.previousChannel=channel')
        stop()
        assert page.evaluate("track.readyState==='ended' && !AssistantVoice.active()")
        assert page.locator('#voice-status').get_attribute('class')=='voice-quiet'
        result['mutedStartupTypingAndVolume']='passed'
        # A late track and stale closed-session event must not spoil a new call.
        choose('breeze');assert page.evaluate("localStorage.getItem('assistant.nativeVoice')")=='breeze'
        page.evaluate('delayedTrack=true');start()
        assert [body for action,body in calls if action=='start'][-1]['voice']=='breeze'
        page.evaluate("previousChannel.onmessage({data:JSON.stringify({type:'session.closed'})});deliverTrack()")
        page.wait_for_function("!document.getElementById('voice-audio').paused")
        assert page.evaluate("AssistantVoice.active() && !track.enabled && document.getElementById('voice-audio').muted && document.getElementById('voice-audio').volume===.37")
        stop();result['lateAudioAndOldSessionEvents']='passed'
        # Browser-blocked and paused output is recoverable from the speaker icon.
        page.evaluate('delayedTrack=false;blockAudio=true');start()
        page.wait_for_function("document.getElementById('voice-volume-toggle').classList.contains('needs-sound')")
        page.evaluate('blockAudio=false');page.locator('#voice-volume-toggle').click()
        page.wait_for_function("!document.getElementById('voice-volume-toggle').classList.contains('needs-sound')")
        assert page.evaluate("!document.getElementById('voice-audio').paused && document.getElementById('voice-audio').muted")
        page.keyboard.press('Escape')
        page.evaluate("document.getElementById('voice-audio').pause()")
        page.wait_for_function("document.getElementById('voice-volume-toggle').classList.contains('needs-sound')")
        page.locator('#voice-volume-toggle').click()
        page.wait_for_function("!document.getElementById('voice-volume-toggle').classList.contains('needs-sound')")
        page.keyboard.press('Escape');stop();result['playbackRecovery']='passed'
        # A call that vanished after a restart is not a failed save when empty.
        behavior['ended']=True;start();stop()
        assert page.locator('#voice-status').inner_text()=='Voice off.'
        assert page.locator('#voice-status').get_attribute('class')=='voice-quiet'
        # Real pending words still receive an honest, concise warning.
        start();page.evaluate("""()=>{
          channel.onmessage({data:JSON.stringify({type:'input_transcript.added',item:{id:'last-in',text:'One more thing'},start_ms:0,end_ms:50})});
          channel.onmessage({data:JSON.stringify({type:'output_transcript.added',item:{id:'last',text:'Last words'},start_ms:50,end_ms:100})});
        }""")
        stop();assert 'may not be saved' in page.locator('#voice-status').inner_text()
        behavior['ended']=False;result['endedCallCleanup']='passed'
        # One power button can cancel a call while startup is still pending.
        behavior['holdStart']=True
        with page.expect_request('**/api/voice/start'):page.locator('#voice-start').click()
        page.locator('#voice-start').click()
        assert page.evaluate("track.readyState==='ended' && !AssistantVoice.active()")
        with page.expect_response('**/api/voice/stop'):held.pop().fulfill(json=selection)
        behavior['holdStart']=False;result['cancelDuringStartup']='passed'
        load()
        assert page.locator('#voice-picker').inner_text()=='Bre'
        assert page.locator('#voice-volume').input_value()=='37'
        behavior['wrongVoice']=True
        with page.expect_response('**/api/voice/stop'):page.locator('#voice-start').click()
        assert 'selected voice' in page.locator('#voice-status').inner_text()
        behavior['wrongVoice']=False
        page.evaluate("localStorage.setItem('assistant.nativeVoice','jupiter')");load()
        assert page.locator('#voice-start').is_disabled()
        assert page.locator('#voice-options button[data-voice=jupiter]').count()==0
        choose('juniper');assert page.locator('#voice-start').is_enabled()
        result['rememberedChoiceAndVolume']='passed'
        behavior['catalogError']=True;load()
        assert page.locator('#voice-start').is_disabled()
        page.locator('#voice-picker').click();assert page.locator('#voice-retry').is_visible()
        behavior['catalogError']=False;page.locator('#voice-retry').click()
        page.wait_for_function("!document.getElementById('voice-start').disabled")
        assert page.locator('#voice-picker').inner_text()=='Jun'
        page.keyboard.press('Escape')
        # Keyboard opening, option navigation, choosing, and light dismissal.
        page.locator('#voice-picker').focus();page.keyboard.press('Enter')
        page.keyboard.press('ArrowLeft');page.keyboard.press('Enter')
        assert page.locator('#voice-picker').inner_text()=='Bre'
        page.locator('#voice-picker').click();page.locator('#input').click()
        assert not page.locator('#voice-menu').is_visible()
        result['pickerKeyboardAndDiscoveryRetry']='passed'
        # The road is chosen for the next conversation, not swapped under one
        # already talking -- locked while a call is up, exactly as the voice
        # picker is, and saying why.
        pick('openai');start()
        assert page.locator('#voice-backend').is_disabled()
        assert page.locator('#voice-backend').get_attribute('title')=='End voice to choose another.'
        stop()
        pick('local')
        assert page.evaluate('!AssistantVoice.active()')
        assert page.locator('#voice-mute').is_hidden()
        assert page.locator('#voice-app').is_visible()
        result['theRoadIsChosenBetweenCalls']='passed'
        result.update(status='passed',pageErrors=errors)
        assert not errors,errors
        browser.close()
    print(json.dumps(result,indent=2))
    return result

if __name__=='__main__':run()
