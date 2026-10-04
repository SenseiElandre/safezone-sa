self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data={};
  try{data=event.data ? event.data.json() : {};}catch(e){data={};}
  const title=data.title || 'SafeZone SA';
  const options={
    body:data.body || 'SafeZone emergency alert.',
    icon:'/static/icons/icon-192.png',
    badge:'/static/icons/icon-192.png',
    tag:data.type==='emergency' ? 'safezone-emergency-'+(data.event_id||'alert') : 'safezone-alert',
    renotify:true,
    silent:false,
    requireInteraction:data.type==='emergency',
    vibrate:data.type==='emergency' ? [300,150,300,150,600,200,600] : [200,100,200],
    data:{url:data.url || '/'}
  };
  event.waitUntil(self.registration.showNotification(title,options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target=event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    for(const client of list){if('focus' in client){client.navigate(target);return client.focus();}}
    if(clients.openWindow) return clients.openWindow(target);
  }));
});
