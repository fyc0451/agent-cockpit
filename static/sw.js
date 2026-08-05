self.addEventListener('push',event=>{
  let data={};
  try{data=event.data?event.data.json():{}}catch(_){data={}}
  if(!data||typeof data!=='object'||Array.isArray(data))data={};
  event.waitUntil(self.registration.showNotification(data.title||'Agent Cockpit 需要你',{
    body:data.body||'',
    tag:data.tag||'agent-cockpit',
    data:{url:data.url||'/'},
  }));
});

self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=new URL(event.notification.data&&event.notification.data.url||'/',self.location.origin);
  if(target.origin!==self.location.origin)return;
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(windows=>{
    for(const client of windows){
      if('navigate' in client)return client.navigate(target.href).then(()=>client.focus());
    }
    return clients.openWindow(target.href);
  }));
});
