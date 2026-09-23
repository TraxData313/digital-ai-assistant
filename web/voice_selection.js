// Inspect service-reported session metadata, never a prompt or transcript.
function nativeVoiceEvidence(event){
 if(!event.type?.startsWith('session.'))return [];
 const result=[];
 function visit(value,path){
  if(!value||typeof value!=='object')return;
  for(const [key,item] of Object.entries(value)){
   const next=path?path+'.'+key:key;
   if(['voice','voice_id','voiceId'].includes(key)){
    const id=typeof item==='string'?item:item?.id??item?.name;
    if(typeof id==='string')result.push({path:next,voice:id.toLowerCase()});
   }else if(typeof item==='object')visit(item,next);
  }
 }
 visit(event,'');return result;
}
function verifiedNativeVoice(events,expected){
 const evidence=events.flatMap(x=>x.voiceEvidence||[]);
 if(evidence.some(x=>x.voice!==expected))throw Error('Native voice does not match '+expected+'. No default voice will be used.');
 return evidence.length>0;
}

async function confirmNativeVoice(channel,events,expected,selection){
 if(typeof expected!=='string'||!expected||selection?.voice!==expected||
    selection?.catalogVerified!==true||selection?.nativeStartAccepted!==true)
  throw Error('The selected native voice was not accepted.');
 const until=Date.now()+6000;
 while(Date.now()<until){
  const readback=verifiedNativeVoice(events,expected);
  if(events.some(x=>x.type==='session.started'))return {
   voice:expected,catalogVerified:true,nativeStartAccepted:true,
   effectiveVoiceReadback:readback?expected:null,
   basis:readback?'service voice metadata':'native voice parameter accepted; service omits voice readback'
  };
  await new Promise(resolve=>setTimeout(resolve,50));
 }
 throw Error('The native voice session did not start.');
}
