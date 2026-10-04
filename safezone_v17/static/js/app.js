// SafeZone v20: remove legacy map/PWA caches and unregister old service workers.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', async () => {
    try {
      const regs = await navigator.serviceWorker.getRegistrations();
      for (const reg of regs) await reg.unregister();
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    } catch (e) {}
  });
}
async function getLocation(){
  return new Promise(resolve=>{
    if(!navigator.geolocation) return resolve({});
    navigator.geolocation.getCurrentPosition(p=>resolve({latitude:p.coords.latitude,longitude:p.coords.longitude}),()=>resolve({}),{enableHighAccuracy:true,timeout:7000,maximumAge:30000});
  });
}
async function checkIn(){
  const loc=await getLocation();
  const r=await fetch('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify(loc)});
  const d=await r.json(); alert((d.ok?'🟢 ':'⚠️ ')+d.message+(d.checked_at?'\n\nLast updated: '+d.checked_at:''));
}
async function activateEmergency(){
  const status=document.getElementById('emergency-status');
  const actions=document.getElementById('contact-actions');
  status.innerHTML='<div class="notice">Getting your location and activating emergency mode…</div>';
  actions.innerHTML='';
  const loc=await getLocation();
  const r=await fetch('/api/emergency/start',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify(loc)});
  const d=await r.json();
  if(!d.ok){ status.innerHTML='<div class="notice">'+d.message+' <a href="/login">Log in</a></div>'; return; }
  sessionStorage.setItem('safezone_event',d.event_id);
  status.innerHTML='<div class="emergency-active"><b>🚨 EMERGENCY MODE ACTIVE</b><span>Your emergency event has been recorded. Call emergency services now.</span><button onclick="resolveEmergency()">I AM SAFE — END EMERGENCY</button></div>'+(d.location?'<div class="notice">📍 Your location was captured with your permission.</div>':'<div class="notice">Location was not available. You can still call emergency services.</div>');
  if(d.contacts && d.contacts.length){
    actions.innerHTML='<div class="contact-actions"><h2>Alert your Trusted Circle</h2><p class="muted">SafeZone has prepared messages. Tap a button to open your phone’s SMS or WhatsApp composer. Nothing is sent automatically.</p>'+d.contacts.map(c=>'<div class="contact-alert"><b>'+c.name+'</b><span>'+c.phone+'</span><div><a class="secondary mini" href="'+c.sms+'">📱 SMS</a><a class="secondary mini" target="_blank" rel="noopener" href="'+c.whatsapp+'">💬 WhatsApp</a></div></div>').join('')+'</div>';
  }
}
async function resolveEmergency(){
  const id=sessionStorage.getItem('safezone_event');
  await fetch('/api/emergency/resolve',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':window.SAFEZONE_CSRF||''},body:JSON.stringify({event_id:id})});
  sessionStorage.removeItem('safezone_event'); location.reload();
}
if('serviceWorker' in navigator){navigator.serviceWorker.register('/static/sw.js').catch(()=>{});}
