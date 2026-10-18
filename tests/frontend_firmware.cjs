const { assert, run, mount, nodes, settle: wait } = require('./frontend_harness.cjs');
const settle = () => wait(20);
const components=[{id:'left',label:'Left controller',installed:'260310A',available:'260401A',newer:true,downloaded:false},
  {id:'right',label:'Right controller',installed:'260310A',available:'260310A',newer:false,downloaded:false},
  {id:'receiver',label:'Receiver',installed:'260422A',available:'260422A',newer:false,downloaded:false}];
const status={phase:'idle',message:'',error:'',component:'',install_enabled:true,install_reason:'',checked_at:'',token:'',components:[]};
const phase=(name,extra={})=>({...status,phase:name,components,...extra});
function harness(allowUpdates=true){
  const logs=[],modals=[];let visible=true;
  const h=mount('firmware.tsx',{component:'FirmwarePanel',deferAll:true,clock:0,
    globals:{console:{warn:(...args)=>logs.push(['warn',...args]),info:(...args)=>logs.push(['info',...args])}},
    ui:{showModal:node=>{modals.push(node);}}});
  const render=()=>h.render({visible,allowUpdates});
  const respond=(name,value,fail=false)=>fail?h.reject(name,value):h.respond(name,value);
  const timer=match=>{const [id,e]=[...h.timers].find(([,e])=>match(e.delay));h.timers.delete(id);e.fn();};
  return {calls:h.calls,timers:h.timers,logs,modals,render,respond,nodes,get writes(){return h.writes;},
    button(){return nodes(render()).find(n=>n.type==='ButtonItem');},
    text(){return JSON.stringify(nodes(render()).map(n=>[n.props?.label,n.props?.description]));},
    hide(){visible=false;render();},show(){visible=true;render();},unmount:h.unmount,
    advance:h.advance,expire(){h.advance(8000);timer(ms=>ms===8000);},fire(){timer(ms=>ms<8000);},count:h.count};
}
run(async()=>{
  const recovered=harness();recovered.render();recovered.respond('firmware_get_status',new Error('Unavailable'),true);await settle();
  recovered.fire();recovered.advance(9000);recovered.respond('firmware_get_status',status);await settle();
  assert.equal(recovered.button().props.disabled,true);
  assert.equal(recovered.logs.filter(row=>row[0]==='info').length,0,'A stale response cannot announce status recovery.');
  recovered.fire();recovered.respond('firmware_get_status',status);await settle();
  assert.equal(recovered.logs.filter(row=>row[0]==='info').length,1,'Only an accepted fresh response announces recovery.');recovered.unmount();
  let blocked=harness(false);blocked.render();blocked.respond('firmware_get_status',status);await settle();
  assert.equal(blocked.button().props.disabled,true);blocked.button().props.onClick();await settle();
  assert.equal(blocked.count('firmware_check'),0,'paused controls can observe but cannot update');blocked.unmount();

  // Explicit steps: Check, then Download, then Install behind a confirmation.
  let h=harness();h.render();assert.equal(h.calls.length,1);h.respond('firmware_get_status',status);await settle();
  assert.equal(h.button().props.children,'Check for updates');
  const button=h.button();button.props.onClick();button.props.onClick();await settle();
  assert.equal(h.count('firmware_check'),1,'double activation coalesced');
  h.respond('firmware_check',phase('checking'));await settle();
  assert.equal(h.button().props.disabled,true);assert.equal(h.button().props.children,'Checking…');
  h.fire();h.respond('firmware_get_status',phase('available'));await settle();
  assert.equal(h.button().props.children,'Download');assert.ok(h.text().includes('Installed 260310A · Available 260401A · newer'));
  h.button().props.onClick();h.button().props.onClick();await settle();
  assert.equal(h.count('firmware_download'),1);assert.equal(h.count('firmware_check'),1,'Download never re-runs Check');
  h.respond('firmware_download',phase('downloaded',{token:'t1'}));await settle();
  assert.equal(h.button().props.children,'Install');
  h.button().props.onClick();await settle();
  assert.equal(h.count('firmware_install'),0,'Install only opens a confirmation');
  assert.equal(h.modals.length,1);const modal=h.modals[0];
  assert.ok(JSON.stringify(h.nodes(modal.props.strDescription)).includes('Left controller: 260310A → 260401A'));
  assert.ok(JSON.stringify(h.nodes(modal.props.strDescription)).includes('AC power'));
  modal.props.onOK();modal.props.onOK();await settle();
  assert.equal(h.count('firmware_install'),1,'confirmation double activation coalesced');
  assert.deepEqual(h.calls.find(c=>c.name==='firmware_install').args,['t1']);
  h.respond('firmware_install',phase('installing',{component:'left'}));await settle();
  assert.equal(h.button().props.disabled,true);h.hide();assert.equal(h.timers.size,0,'hidden panel has no polling');
  assert.equal(h.calls.filter(c=>/cancel|stop|flash|start_update|check_updates/.test(c.name)).length,0,'no cancel or implicit flash path');h.unmount();

  // A confirmation is bound to the download it showed.
  h=harness();h.render();h.respond('firmware_get_status',phase('downloaded',{token:'old'}));await settle();
  h.button().props.onClick();const stale=h.modals[0];h.fire();
  h.respond('firmware_get_status',phase('downloaded',{token:'new'}));await settle();
  stale.props.onOK();await settle();assert.equal(h.count('firmware_install'),0,'a replaced download cannot be installed by an old confirmation');
  h.fire();h.respond('firmware_get_status',phase('available'));await settle();
  stale.props.onOK();await settle();assert.equal(h.count('firmware_install'),0);h.unmount();

  // The confirmation takes focus from the Quick Access menu: OK still installs the shown download.
  h=harness();h.render();h.respond('firmware_get_status',phase('downloaded',{token:'shown'}));await settle();
  h.button().props.onClick();const hiddenConfirm=h.modals[0];h.hide();await settle();
  hiddenConfirm.props.onOK();await settle();
  assert.deepEqual(h.calls.filter(c=>c.name==='firmware_install').map(c=>c.args),[['shown']],'a hidden panel still installs after OK');h.unmount();

  // Disabled installation is visible and cannot be confirmed.
  h=harness();h.render();h.respond('firmware_get_status',phase('downloaded',{token:'t',install_enabled:false,install_reason:'Not enabled in this build.'}));await settle();
  assert.equal(h.button().props.children,'Check for updates');assert.equal(h.button().props.disabled,false);
  assert.ok(h.text().includes('Not enabled in this build.'));assert.ok(h.text().includes('Installed 260310A · Available 260401A · newer'));
  h.button().props.onClick();await settle();assert.equal(h.modals.length,0);assert.equal(h.count('firmware_install'),0);
  assert.equal(h.count('firmware_check'),1,'a downloaded firmware with install disabled can be checked again');h.unmount();

  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  const pending=h.button().props.onClick();await settle();assert.equal(h.count('firmware_check'),0);
  h.respond('firmware_get_status',{...status,message:'old reply'});await settle();assert.equal(h.count('firmware_check'),1);
  h.respond('firmware_check',phase('up_to_date'));await settle();await pending;
  assert.equal(h.button().props.children,'Check for updates');
  h.unmount();const before=h.writes;await settle();assert.equal(h.writes,before);

  h=harness();h.render();h.hide();const writes=h.writes;h.respond('firmware_get_status',status);await settle();
  assert.equal(h.writes,writes,'late hidden read ignored');assert.equal(h.timers.size,0);h.unmount();

  h=harness();h.render();assert.equal(h.button().props.disabled,true,'no admission before status');
  h.respond('firmware_get_status',status);await settle();
  const check=h.button();check.props.onClick();check.props.onClick();h.button().props.onClick();await settle();
  assert.equal(h.count('firmware_check'),1,'check double activation coalesced');
  h.respond('firmware_check',phase('checking'));await settle();
  assert.equal(h.button().props.disabled,true);h.hide();assert.equal(h.timers.size,0);h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  h.respond('firmware_get_status',new Error('Backend connection lost'),true);await settle();
  assert.equal(h.button().props.disabled,true,'failed status read must invalidate old idle state');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();
  const staleHandler=h.button().props.onClick;h.fire();h.expire();await settle();
  assert.equal(h.button().props.disabled,true,'status expires even during a stalled read');staleHandler();await settle();
  assert.equal(h.count('firmware_check'),0);h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  h.button().props.onClick();h.respond('firmware_get_status',new Error('Read failed'),true);await settle();
  assert.equal(h.count('firmware_check'),0,'failed pending read cannot admit a write');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  h.button().props.onClick();h.respond('firmware_get_status',phase('installing'));await settle();
  assert.equal(h.count('firmware_check'),0,'adopt the running step instead of sending another check');h.unmount();
  h=harness();h.render();h.advance(9000);h.respond('firmware_get_status',status);await settle();
  assert.equal(h.button().props.disabled,true,'response latency counts toward status freshness');h.unmount();
  for(const invalid of [{phase:'unrecognised'},{phase:'toString'},{...status,message:{malformed:true}},null]){
    h=harness();h.render();h.respond('firmware_get_status',invalid);await settle();
    assert.equal(h.button().props.disabled,true,'unknown or malformed status cannot enable a step');h.unmount();
  }
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.button().props.onClick();await settle();
  h.respond('firmware_check',new Error('Lost result'),true);await settle();
  assert.equal(h.button().props.disabled,true,'lost write result requires fresh status, no replay');
  assert.equal(h.count('firmware_check'),1);h.unmount();
  console.log('Firmware frontend: explicit Check/Download/Install steps, confirmation token binding, double-click exclusion, read/write order and hidden polling passed.');
});
