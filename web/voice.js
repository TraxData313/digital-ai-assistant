(() => {
  const N = (window.ASSISTANT && window.ASSISTANT.name) || 'the assistant';
  const el = id => document.getElementById(id);
  const controls = Object.fromEntries(['start','mute','picker','menu','options','retry','audio','volume','volume-toggle','volume-panel','volume-value','sound','tools','backend','app'].map(name=>[name,el('voice-'+name)]));
  const preferenceKey='assistant.nativeVoice', volumeKey='assistant.nativeVolume';
  const delegationTranscriptGraceMs=1500;
  // How often to ask the room what the local voice app is doing. The room keeps
  // its reading for twenty seconds, so this costs one real probe per two asks
  // and shows a warming engine without anybody reloading the page.
  const appPollMs=10000;
  let current=null, closing=false, serial=Promise.resolve(), lastSync=0;
  let voices=[], selected='sol', loadingVoices=null, volume=100;
  // The local road. `backend` is the room's answer, not this browser's: one
  // room, one voice, whether the reader is at the desk or on a phone. `app` is the last
  // reading of the speak server, `appSwitch` the room's own on/off for it.
  let backend='local', app=null, appSwitch=true, appBusy=false, appTimer=null;
  try { const saved=localStorage.getItem(volumeKey); if(saved!==null&&Number.isFinite(Number(saved)))volume=Math.max(0,Math.min(100,Number(saved))); } catch {}
  const enqueue = work => { const next=serial.then(work,work); serial=next.catch(()=>{}); return next; };
  const selectedName = () => voices.find(voice=>voice.id===selected)?.name || 'Choose a voice';

  function message(text,error=false,quiet=false) {
    const status=el('voice-status');status.textContent=text;
    status.classList.toggle('error',error);status.classList.toggle('voice-quiet',quiet);
  }
  function readyMessage(s) {
    if(current===s)message(s.muted?'Voice on. Microphone muted; you can type.':'Voice on. I’m listening.',false,true);
  }
  function choiceState() {
    controls.tools.dataset.backend=backend;
    controls.backend.value=backend;
    // Locked while a call is up, the same way the voice picker is, and for the
    // same reason: the road is chosen for the next conversation, not swapped
    // under one already talking. The title says so rather than leaving a grey
    // control that gives no reason.
    controls.backend.disabled=appBusy||closing||!!current;
    controls.backend.title=current?'End voice to choose another.':backend==='local'
      ? N+'’s own voice, through your voice app · which voice says '+N+'’s lines'
      : 'OpenAI’s voice, as a live call in this browser · which voice says '+N+'’s lines';
    if(backend==='local')return localState();
    controls.app.classList.add('hidden');
    controls.start.disabled=closing||(!current&&!voices.some(voice=>voice.id===selected));
    controls.start.setAttribute('aria-pressed',String(!!current));
    const title=current?(current.ready?'Turn voice off':'Cancel voice connection'):'Turn voice on';
    controls.start.title=title;controls.start.setAttribute('aria-label',title);
    controls.tools.dataset.state=current?(current.ready?'on':'connecting'):'off';
    controls.picker.textContent=voices.some(voice=>voice.id===selected)?selectedName().slice(0,3):'…';
    controls.picker.setAttribute('aria-label','Choose voice: '+selectedName());
    controls.picker.title=selectedName()+' · choose voice';
    el('voice-choice-hint').textContent=current?'End voice to choose another.':
      !voices.length?'Loading voices…':!voices.some(voice=>voice.id===selected)?'Your saved voice is unavailable. Choose another.':'Used for your next voice conversation.';
    for(const button of controls.options.querySelectorAll('button')) {
      button.disabled=!!current||closing;
      button.setAttribute('aria-pressed',String(button.dataset.voice===selected));
    }
  }
  // The local road has one switch and one sentence. The switch is the room's,
  // not the voice app's -- the app's own is shared with everything else on this
  // machine, and quieting the assistant here must not silence anything else.
  function localState() {
    controls.start.disabled=appBusy;
    controls.start.setAttribute('aria-pressed',String(appSwitch));
    const title=appSwitch?'Turn '+N+'’s voice off':'Turn '+N+'’s voice on';
    controls.start.title=title;controls.start.setAttribute('aria-label',title);
    controls.tools.dataset.state=appSwitch&&app?.on?'on':'off';
    controls.app.classList.remove('hidden');
    let text, warn=false;
    if(!appSwitch)text=N+'’s voice is off.';
    else if(!app)text='Asking your voice app…';
    else if(app.on){
      text=N+'’s voice is on'+(app.engine?', through '+app.engine:'')+'.';
      // Only worth saying when it is true, and it is the whole reason the
      // engine is named at all: on Qwen the assistant can say how a line
      // should land.
      if(app.sound)text+=' '+N+' can say how it sounds.';
    } else {text='Not heard: '+(app.reason||'the voice app is not answering.');warn=true;}
    controls.app.textContent=text;
    controls.app.classList.toggle('warn',warn);
  }
  function micState() {
    const muted=!current||current.muted;
    controls.mute.disabled=!current?.ready;
    controls.mute.setAttribute('aria-pressed',String(muted));
    controls.mute.setAttribute('aria-label',muted?'Unmute microphone':'Mute microphone');
    controls.mute.title=muted?'Microphone muted · click to speak':'Mute microphone';
  }
  function volumeState() {
    controls.volume.value=volume;controls['volume-value'].value=volume+'%';
    controls.audio.volume=volume/100;
    // A few realtime builds speak in response to their startup prompt. Keep
    // that transport audio inaudible until this call has received a genuinely
    // new user input; the prompt is context, not a conversation turn.
    controls.audio.muted=!current?.ready||!current?.hasInput||volume===0;
    controls['volume-toggle'].classList.toggle('is-muted',volume===0);
    const blocked=!controls.sound.hidden;
    controls['volume-toggle'].classList.toggle('needs-sound',blocked);
    const label=blocked?'Enable sound':('Voice volume: '+volume+'%');
    controls['volume-toggle'].title=label;controls['volume-toggle'].setAttribute('aria-label',label);
    controls.volume.setAttribute('aria-valuetext',volume===0?'Muted':volume+'%');
  }
  function positionPopover(popover,anchor) {
    const rect=anchor.getBoundingClientRect(), box=popover.getBoundingClientRect();
    popover.style.left=Math.max(10,Math.min(innerWidth-box.width-10,rect.right-box.width))+'px';
    const top=rect.top-box.height-10;
    popover.style.top=Math.max(10,Math.min(innerHeight-box.height-10,top<10?rect.bottom+10:top))+'px';
  }
  function togglePopover(popover,anchor) {
    if(popover.matches(':popover-open')){popover.hidePopover();return;}
    for(const other of [controls.menu,controls['volume-panel']])if(other!==popover&&other.matches(':popover-open'))other.hidePopover();
    popover.showPopover();positionPopover(popover,anchor);
    if(popover===controls.menu)(controls.options.querySelector('[aria-pressed="true"]:not(:disabled)')||controls.options.querySelector('button:not(:disabled)')||(!controls.retry.hidden?controls.retry:controls.menu)).focus();
    else controls.volume.focus();
  }
  for(const [popover,anchor] of [[controls.menu,controls.picker],[controls['volume-panel'],controls['volume-toggle']]]) {
    popover.addEventListener('toggle',()=>anchor.setAttribute('aria-expanded',String(popover.matches(':popover-open'))));
    addEventListener('resize',()=>{if(popover.matches(':popover-open'))positionPopover(popover,anchor);});
  }
  async function loadVoices() {
    if(loadingVoices)return loadingVoices;
    controls.retry.hidden=true;
    loadingVoices=(async()=>{
      try {
        const response=await fetch('/api/voice/voices'), result=await response.json();
        if(!response.ok)throw Error(result.error||'Could not load voices.');
        if(!Array.isArray(result.voices)||!result.voices.length)throw Error('No supported voices are available.');
        voices=result.voices;selected=result.defaultVoice;
        try{selected=localStorage.getItem(preferenceKey)||selected;}catch{}
        controls.options.replaceChildren(...voices.map(voice=>{
          const button=document.createElement('button');button.type='button';button.dataset.voice=voice.id;
          const swatch=document.createElement('span');swatch.className='voice-swatch';swatch.textContent=voice.name.slice(0,3);swatch.setAttribute('aria-hidden','true');
          const name=document.createElement('span');name.textContent=voice.name;button.append(swatch,name);
          button.onclick=()=>{
            if(current||closing)return;selected=voice.id;
            try{localStorage.setItem(preferenceKey,selected);}catch{}
            choiceState();controls.menu.hidePopover();controls.picker.focus();
          };
          return button;
        }));
        choiceState();
      } catch(error) {
        voices=[];controls.options.replaceChildren();choiceState();
        el('voice-choice-hint').textContent='Voices couldn’t load. Try again.';controls.retry.hidden=false;
      }
      if(controls.menu.matches(':popover-open'))positionPopover(controls.menu,controls.picker);
    })();
    try{await loadingVoices;}finally{loadingVoices=null;}
  }
  controls.options.onkeydown=event=>{
    const buttons=[...controls.options.querySelectorAll('button:not(:disabled)')],index=buttons.indexOf(document.activeElement);
    const steps={ArrowRight:1,ArrowLeft:-1,ArrowDown:3,ArrowUp:-3};
    if(index<0||!(event.key in steps))return;
    event.preventDefault();buttons[(index+steps[event.key]+buttons.length)%buttons.length].focus();
  };
  function soundBlocked(s) {
    if(current!==s||!s.ready)return;
    controls.sound.hidden=false;volumeState();
    message('Tap the speaker to enable sound.');
  }
  async function playAudio(s) {
    if(current!==s||!s.ready||!controls.audio.srcObject)return;
    volumeState();
    try {
      await controls.audio.play();
      if(current===s){controls.sound.hidden=true;volumeState();readyMessage(s);}
    } catch {soundBlocked(s);}
  }
  function absorbVoice(result) {
    if(result.backend)backend=result.backend;
    if(result.local)app=result.local;
    if(typeof result.switch==='boolean')appSwitch=result.switch;
    choiceState();
  }
  async function loadApp() {
    try {absorbVoice(await (await fetch('/api/voice')).json());}
    catch {/* A reading we could not take is not a reading we invent. */}
  }
  function pollApp() {
    clearInterval(appTimer);
    appTimer=backend==='local'?setInterval(loadApp,appPollMs):null;
  }
  async function api(action,body,keepalive=false) {
    const response=await fetch('/api/voice/'+action,{method:'POST',keepalive,
      headers:{'Content-Type':'application/json','X-Assistant-Voice':'1'},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){const error=Error(result.error||'The voice connection failed.');error.status=response.status;throw error;}
    if(result.rows)window.assistantVoiceRows?.(result.rows);
    return result;
  }
  async function flush(s) {
    if(!s.id)return;
    const batch=s.pending.slice(0,160),result=await api('sync',{session:s.id,events:batch});
    s.pending.splice(0,batch.length);lastSync=Date.now();return result;
  }
  async function flushDelegationTranscriptTail(s) {
    // Native voice can finish its spoken handoff acknowledgement just after
    // the delegation event. Save that short tail before rebuilding the chat.
    await new Promise(resolve=>setTimeout(resolve,delegationTranscriptGraceMs));
    if(current!==s||!s.id)return;
    while(s.pending.length)await flush(s);
  }
  async function rejectedTypedInput(s, pending) {
    if(current!==s||pending.recovering)return;
    pending.recovering=true;
    try {
      const result=await api('text-fallback',{session:s.id,row:pending.row});
      if(current!==s)return;
      s.typedInputs.delete(pending.eventId);
      if(result.queued){message(N + ' is working on your request.',false,true);window.watchIfBusy?.();}
    } catch(error) {
      if(current===s)message('Your message is saved, but voice could not answer it. '+error.message,true);
    }
  }
  function eventMessage(s,event) {
    if(current!==s)return;
    let data;try{data=JSON.parse(event.data);}catch{return;}
    s.events.push({type:data.type,voiceEvidence:nativeVoiceEvidence(data)});
    if(s.events.length>300)s.events.splice(1,100);
    if(data.type==='input_transcript.added'){
      s.hasInput=true;s.pending.push(data);volumeState();playAudio(s);return;
    }
    if(data.type==='output_transcript.added'){
      if(s.hasInput)s.pending.push(data);
      return;
    }
    if(['delegation.created','session.delegation.created','conversation.handoff.requested'].includes(data.type)) {
      enqueue(async()=>{
        if(current!==s||!s.id)return;
        await flush(s);await api('delegate',{session:s.id,event:data,events:[]});
        if(current===s&&controls.sound.hidden)message(N + ' is working on your request.',false,true);
        window.watchIfBusy?.();
        await flushDelegationTranscriptTail(s);
        if(current===s)window.assistantVoiceRefresh?.().catch(()=>{});
      }).catch(error=>{if(current===s)message(error.message,true);});
    }
    if(data.type==='error'){
      const rejected=s.typedInputs.get(data.error?.event_id);
      if(rejected){enqueue(()=>rejectedTypedInput(s,rejected));return;}
      message(data.error?.message||'Voice reported an error.',true);
    }
    if(data.type==='session.closed')end();
  }
  function closeDevices(s) {
    clearInterval(s.heartbeat);clearTimeout(s.deadline);
    s.mic?.getTracks().forEach(track=>track.stop());s.dc?.close();s.pc?.close();
    if(!current||current===s){controls.audio.pause();controls.audio.srcObject=null;controls.audio.muted=true;}
  }
  async function start() {
    if(backend!=='local'&&(current||closing||!voices.some(voice=>voice.id===selected)))return;
    if(backend==='local')return;
    const s={id:null,pc:null,dc:null,mic:null,pending:[],events:[],typedInputs:new Map(),muted:true,ready:false,hasInput:false,voice:selected};
    current=s;choiceState();micState();controls.sound.hidden=true;volumeState();
    for(const popover of [controls.menu,controls['volume-panel']])if(popover.matches(':popover-open'))popover.hidePopover();
    el('person').disabled=true;message('Connecting…');
    try {
      if(!navigator.mediaDevices?.getUserMedia)throw Error('Open ' + N + ' at localhost or through a secure connection to use voice.');
      s.mic=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
      // Never send microphone audio until the person explicitly unmutes it.
      s.mic.getAudioTracks().forEach(track=>track.enabled=false);
      if(current!==s){closeDevices(s);return;}
      s.pc=new RTCPeerConnection({iceServers:[]});s.mic.getTracks().forEach(track=>s.pc.addTrack(track,s.mic));
      s.pc.ontrack=event=>{if(current!==s)return;controls.audio.srcObject=event.streams[0]||new MediaStream([event.track]);playAudio(s);};
      s.pc.onconnectionstatechange=()=>{if(current===s&&['failed','disconnected'].includes(s.pc.connectionState))end('Voice disconnected. Tap the power button to reconnect.');};
      s.dc=s.pc.createDataChannel('oai-events');s.dc.onmessage=event=>eventMessage(s,event);
      await s.pc.setLocalDescription(await s.pc.createOffer());
      await new Promise(resolve=>{if(s.pc.iceGatheringState==='complete')return resolve();const timer=setTimeout(resolve,3000);s.pc.addEventListener('icegatheringstatechange',()=>{if(s.pc.iceGatheringState==='complete'){clearTimeout(timer);resolve();}});});
      if(current!==s)return;
      const result=await api('start',{sdp:s.pc.localDescription.sdp,who:typeof whoNow==='function'?whoNow():((window.ASSISTANT||{}).owner||'owner'),voice:s.voice});
      s.id=result.session;
      if(current!==s){await api('stop',{session:s.id,events:[]});return;}
      if(result.voice!==s.voice)throw Error('The voice connection did not use your selected voice.');
      await s.pc.setRemoteDescription({type:'answer',sdp:result.sdp});
      await new Promise((resolve,reject)=>{if(s.dc.readyState==='open')return resolve();const timer=setTimeout(()=>reject(Error('Voice did not connect.')),8000);s.dc.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});});
      await confirmNativeVoice(s.dc,s.events,s.voice,result.voiceSelection);
      if(current!==s)return;
      s.ready=true;choiceState();micState();readyMessage(s);lastSync=Date.now();playAudio(s);
      s.heartbeat=setInterval(()=>{
        if(current!==s||s.syncing||(!s.pending.length&&Date.now()-lastSync<10000))return;
        s.syncing=true;enqueue(()=>flush(s)).catch(error=>{
          if(current!==s)return;
          if(error.status===400||Date.now()-lastSync>30000)end();
          else message('The latest words haven’t saved yet. Reconnecting…');
        }).finally(()=>s.syncing=false);
      },1000);
      s.deadline=setTimeout(()=>end('Voice reached its 30-minute limit. Tap the power button to reconnect.'),result.maxSeconds*1000);
    } catch(error) {
      if(current===s)await end(error.message,true);else closeDevices(s);
    }
  }
  async function end(text='Voice off.',error=false) {
    const s=current;if(!s)return;current=null;closing=true;closeDevices(s);
    controls.sound.hidden=true;choiceState();micState();volumeState();el('person').disabled=false;
    message(text,error,text==='Voice off.'&&!error);
    try {
      if(s.id)await enqueue(async()=>{
        while(s.pending.length>160)await flush(s);
        await api('stop',{session:s.id,events:s.pending});s.pending=[];
      });
      await window.assistantVoiceRefresh?.();
    } catch(failure) {
      if(!current) {
        // Already-ended calls with nothing left to save are normal cleanup.
        if(s.pending.length)message('Voice ended. The last few words may not be saved.',true);
        else if(failure.status!==400)message('Voice is off. Couldn’t reach ' + N + '; try again.');
      }
    } finally {closing=false;choiceState();}
  }
  controls.backend.onchange=async()=>{
    const want=controls.backend.value;
    if(want===backend||appBusy)return;
    appBusy=true;choiceState();
    try {
      // The picker is locked while a call is up, so this is belt and braces:
      // however this was reached, no call is left talking over the road that
      // was just chosen.
      if(current)await end();
      absorbVoice(await api('backend',{backend:want}));
      message('Voice off.',false,true);
      pollApp();
    } catch(error) {
      controls.backend.value=backend;message(error.message,true);
    } finally {appBusy=false;choiceState();}
  };
  async function toggleApp() {
    if(appBusy)return;
    appBusy=true;choiceState();
    try {absorbVoice(await api('enabled',{on:!appSwitch}));}
    catch(error) {message(error.message,true);}
    finally {appBusy=false;choiceState();}
  }
  controls.start.onclick=()=>backend==='local'?toggleApp():(current?end():start());
  controls.mute.onclick=()=>{
    const s=current;if(!s?.ready)return;s.muted=!s.muted;
    s.mic.getAudioTracks().forEach(track=>track.enabled=!s.muted);micState();
    if(controls.sound.hidden)readyMessage(s);
  };
  controls.picker.onclick=()=>togglePopover(controls.menu,controls.picker);
  controls['volume-toggle'].onclick=()=>{
    if(!controls.sound.hidden&&current)playAudio(current);
    togglePopover(controls['volume-panel'],controls['volume-toggle']);
  };
  controls.volume.oninput=()=>{
    volume=Number(controls.volume.value);try{localStorage.setItem(volumeKey,String(volume));}catch{}
    volumeState();if(current?.ready)playAudio(current);
  };
  controls.sound.onclick=()=>{if(current)playAudio(current);};
  controls.audio.onpause=()=>{if(current&&controls.audio.srcObject)soundBlocked(current);};
  controls.retry.onclick=loadVoices;
  choiceState();micState();volumeState();loadVoices();
  loadApp().then(pollApp);
  window.AssistantVoice={active:()=>!!current,message,sendText:async text=>{
    const s=current;if(!s?.ready)throw Error('Wait for the voice connection.');
    await enqueue(async()=>{
      await flush(s);
      if(current!==s||s.dc?.readyState!=='open')throw Error('Voice has ended. Send your message again.');
      const result=await api('text',{session:s.id,text,inputMode:'native-routed-v1'});
      if(current!==s||s.dc.readyState!=='open')throw Error('Your message is saved in chat, but voice disconnected before it could receive it.');
      s.hasInput=true;volumeState();await playAudio(s);
      if(result.voiceInput){
        const packet=result.voiceInput;
        if(packet.type!=='session.context.append'||packet.channel!=='developer')throw Error('Refresh ' + N + ' to update voice.');
        const pending={row:result.row,eventId:packet.event_id,recovering:false};
        s.typedInputs.set(packet.event_id,pending);
        try{s.dc.send(JSON.stringify(packet));}
        catch{await rejectedTypedInput(s,pending);}
      }
      if(result.queued){
        if(controls.sound.hidden)message(N + ' is working on your request.',false,true);
        window.watchIfBusy?.();
      }
    });
  }};
  addEventListener('pagehide',()=>{
    const s=current;if(!s)return;current=null;closeDevices(s);
    if(s.id)fetch('/api/voice/stop',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-Assistant-Voice':'1'},body:JSON.stringify({session:s.id,events:s.pending.slice(0,100)})}).catch(()=>{});
  });
})();
