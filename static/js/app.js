// SafeZone v32: persistent PWA service worker + emergency push alarms.
async function getLocation(){
  return new Promise(resolve=>{
    if(!navigator.geolocation) return resolve({});
    navigator.geolocation.getCurrentPosition(p=>resolve({latitude:p.coords.latitude,longitude:p.coords.longitude}),()=>resolve({}),{enableHighAccuracy:true,timeout:7000,maximumAge:30000});
  });
}
async function checkIn(){
  const loc=await getLocation();
  const r=await fetch('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify(loc)});
  let d; try{d=await r.json()}catch(e){alert('SafeZone could not complete the check-in right now. Please try again.');return;}
  alert((d.ok?'🟢 ':'⚠️ ')+d.message+(d.checked_at?'\n\nLast updated: '+d.checked_at:''));
}

async function registerEmergencyPush(){
  if(!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)){
    alert('This device/browser does not support emergency push alarms.'); return false;
  }
  try{
    const permission=await Notification.requestPermission();
    if(permission!=='granted'){alert('Emergency alarm permission was not granted. You can enable notifications for SafeZone in your browser settings.');return false;}
    const keyResponse=await fetch('/api/push/public-key');
    const keyData=await keyResponse.json();
    if(!keyResponse.ok || !keyData.public_key){alert(keyData.message||'Emergency alarms are not configured yet.');return false;}
    const registration=await navigator.serviceWorker.ready;
    let subscription=await registration.pushManager.getSubscription();
    if(!subscription){
      subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:urlBase64ToUint8Array(keyData.public_key)});
    }
    const response=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify({subscription})});
    const data=await response.json();
    if(!response.ok || !data.ok){alert(data.message||'Could not enable emergency alarms.');return false;}
    alert('🔔 Emergency alarms are enabled on this phone.');
    return true;
  }catch(e){console.error(e);alert('Could not enable emergency alarms. Please make sure notifications are allowed for SafeZone.');return false;}
}
function urlBase64ToUint8Array(base64String){
  const padding='='.repeat((4-base64String.length%4)%4);
  const base64=(base64String+padding).replace(/-/g,'+').replace(/_/g,'/');
  const raw=atob(base64); return Uint8Array.from([...raw].map(c=>c.charCodeAt(0)));
}

async function activateEmergency(){
  const status=document.getElementById('emergency-status');
  const actions=document.getElementById('contact-actions');
  status.innerHTML='<div class="notice">Getting your location and activating emergency mode…</div>';
  actions.innerHTML='';
  const selected=[...document.querySelectorAll('input[name="circle_member_ids"]:checked')].map(x=>Number(x.value));
  const loc=await getLocation();
  try{
    const r=await fetch('/api/emergency/start',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify({...loc,circle_member_ids:selected}),cache:'no-store'});
    let d={}; try{d=await r.json()}catch(e){}
    if(!r.ok || !d.ok){ status.innerHTML='<div class="notice">⚠️ '+(d.message||('SafeZone could not activate emergency mode. Server response: '+r.status+'. Please refresh and try again.'))+'</div>'; return; }
    sessionStorage.setItem('safezone_event',d.event_id);
    status.innerHTML='<div class="emergency-active"><b>🚨 EMERGENCY MODE ACTIVE</b><span>Your emergency event has been recorded. Call emergency services now.</span><button onclick="resolveEmergency()">I AM SAFE — END EMERGENCY</button></div>'+(d.location?'<div class="notice">📍 Your location was captured with your permission.</div>':'<div class="notice">Location was not available. You can still call emergency services.</div>')+(d.push_count?'<div class="notice">🔔 Alarm sent to '+d.push_count+' selected SafeZone circle device(s).</div>':'<div class="notice">⚠️ No selected circle device was reachable by push. Use SMS/WhatsApp below as an additional backup.</div>');
    if(d.contacts && d.contacts.length){
      actions.innerHTML='<div class="contact-actions"><h2>Alert your Trusted Circle</h2><p class="muted">SafeZone has prepared messages. Tap a button to open your phone’s SMS or WhatsApp composer. Nothing is sent automatically.</p>'+d.contacts.map(c=>'<div class="contact-alert"><b>'+escapeEmergencyHtml(c.name)+'</b><span>'+escapeEmergencyHtml(c.phone)+'</span><div><a class="secondary mini" href="'+c.sms+'">📱 SMS</a><a class="secondary mini" target="_blank" rel="noopener" href="'+c.whatsapp+'">💬 WhatsApp</a></div></div>').join('')+'</div>';
    }
  }catch(e){ console.error('SafeZone emergency activation failed',e); status.innerHTML='<div class="notice">⚠️ SafeZone could not activate emergency mode. Please check your connection and try again.</div>'; }
}
function escapeEmergencyHtml(v){return String(v??'').replace(/[&<>'"]/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[s]));}
async function resolveEmergency(){
  const id=sessionStorage.getItem('safezone_event');
  await fetch('/api/emergency/resolve',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify({event_id:id})});
  sessionStorage.removeItem('safezone_event'); location.reload();
}
if('serviceWorker' in navigator){navigator.serviceWorker.register('/static/sw.js?v=33',{updateViaCache:'none'}).catch(()=>{});}
