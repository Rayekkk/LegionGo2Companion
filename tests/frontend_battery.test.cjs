const { assert, run, settle: wait, mount, deferred, nodes: walk, text: content } = require('./legion_harness.cjs');
const base = (changes = {}) => ({ supported: true, managed: false,
  enabled: false, requested_enabled: null, capacity: 98, charging_status: 'Not charging',
  backend: 'charge_types', current_mode: 'Fast', baseline: null, options: ['Fast', 'Standard', 'Long_Life'],
  error: '', reason: '', recovery_pending: false, ...changes });
const controllerBase = (changes = {}) => ({available: true, left: 87, right: 64,
  connection_left: 'attached', connection_right: 'attached', reason: '', ...changes});
const settle = () => wait(30);
function harness(initial=base(), initialControllers=controllerBase()) {
  let response=initial, controllerResponse=initialControllers, tree;const toasts=[];
  const h=mount('battery.tsx',{component:'BatteryPage',strict:true,api:{toaster:{toast:t=>toasts.push(t)}},responses:{
    battery_get_status:()=>response,battery_get_controller_levels:()=>controllerResponse}});
  const render=()=>tree=h.render();
  return {methods:h.responses,calls:h.calls,timers:h.timers,render,unmount:h.unmount,summary:h.exports.batterySummary,
    setResponse:value=>{response=value;},setVisible:value=>{h.setVisible(value);return render();},
    get tree(){return tree;},tick(){h.fire('interval');},toasts,
    details(){button({tree},'Show details').props.onClick();return render();},
    get writes(){return h.writes;},
    async ready(){render();await settle();return render();}};
}
const toggle=h=>walk(h.tree).find(n=>n.type==='ToggleField');
const button=(h,label)=>walk(h.tree).find(n=>n.type==='ButtonItem'&&content(n).includes(label));
const count=(h,name)=>h.calls.filter(c=>c.name===name).length;

run(async()=>{
  {
    const h=harness(),old=deferred();h.methods.battery_get_controller_levels=()=>old.promise;
    await h.ready();h.setVisible(false);h.setResponse(base({capacity:55}));h.setVisible(true);
    await settle();h.render();assert.match(content(h.tree),/Battery 55%/);
    assert.equal(count(h,'battery_get_controller_levels'),1,'Reopening must not queue another controller read before the old one finishes.');
    h.methods.battery_get_controller_levels=()=>controllerBase({left:88});
    old.resolve(controllerBase({left:42}));await settle();h.render();
    assert.equal(count(h,'battery_get_controller_levels'),2);
    assert.match(content(h.tree),/Controllers: left 88%/);
    assert.doesNotMatch(content(h.tree),/left 42%/);
    h.unmount();
  }
  {
    const h=harness(),levels=deferred();h.methods.battery_get_controller_levels=()=>levels.promise;
    await h.ready();h.unmount();const writes=h.writes;levels.resolve(controllerBase());await settle();
    assert.equal(h.writes,writes,'A late controller reply cannot update an unmounted battery page.');
  }
  {
    const h=harness();h.methods.battery_get_controller_levels=()=>{throw Error('HID unavailable');};
    await h.ready();assert.ok(toggle(h));assert.equal(toggle(h).props.disabled,false);
    assert.match(content(h.tree),/Controllers: left — · right —/);h.unmount();
  }
  {
    const h=harness(),levels=deferred();
    h.methods.battery_get_controller_levels=()=>levels.promise;
    await h.ready();
    assert.ok(toggle(h),'Console controls must be visible while controller battery sampling is pending.');
    const committed=base({managed:true,requested_enabled:true,enabled:true,current_mode:'Long_Life',baseline:'Fast'});
    h.methods.battery_set_enabled=()=>{h.setResponse(committed);return {ok:true,state:committed};};
    toggle(h).props.onChange(true);await settle();h.render();
    assert.equal(count(h,'battery_set_enabled'),1,'Console protection must not wait for unrelated controller sampling.');
    assert.equal(toggle(h).props.checked,true);
    h.tick();await settle();
    assert.equal(count(h,'battery_get_controller_levels'),1,'Slow controller sampling remains bounded during independent status polling.');
    levels.resolve(controllerBase({left:42}));await settle();h.render();
    assert.match(content(h.tree),/Controllers: left 42%/);
    assert.equal(toggle(h).props.checked,true,'A controller reply cannot replace the console mutation result.');
    h.unmount();
  }
  {
    const h=harness(),levels=deferred();let fail=true;
    h.methods.battery_get_controller_levels=()=>levels.promise;
    h.methods.battery_get_status=()=>{if(fail)throw Error('battery unavailable');return base();};
    await h.ready();assert.match(content(h.tree),/Status unavailable/);
    fail=false;button(h,'Try again').props.onClick();await settle();h.render();
    assert.ok(toggle(h),'Retrying console status must not wait for the old controller request.');
    assert.equal(count(h,'battery_get_controller_levels'),1,'A console read error must not duplicate an in-flight controller read.');
    levels.resolve(controllerBase());await settle();h.unmount();
  }
  // Opening a page only reads the actual firmware state; it never adopts a setting.
  let h=harness();await h.ready();
  assert.equal(toggle(h).props.checked,false);assert.equal(toggle(h).props.disabled,false);
  assert.match(content(h.tree),/Battery 98% · Not charging/);
  assert.match(content(h.tree),/Controllers: left 87% · right 64%/);
  assert.equal(toggle(h).props.description,'Stops charging at about 80% to protect the battery.');
  assert.doesNotMatch(content(h.tree),/Saved choice/,'technical facts stay folded');
  h.details();assert.match(content(h.tree),/Charging mode reported Fast/);
  assert.match(content(h.tree),/Saved choice None · Companion is not managing charging/);
  assert.equal(button(h,'Restore previous charging mode'),undefined,'nothing to restore while unmanaged');
  assert.equal(count(h,'battery_get_status'),1);assert.equal(count(h,'battery_get_controller_levels'),1);h.unmount();

  // A valid controller-reported level remains visible after physical detachment.
  h=harness(base(),controllerBase({left:55,right:0,connection_right:'detached'}));await h.ready();
  assert.match(content(h.tree),/Controllers: left 55% · right 0% \(detached\)/);h.unmount();
  h=harness(base(),controllerBase({left:null,connection_left:'detached'}));await h.ready();
  assert.match(content(h.tree),/Controllers: left — · right 64%/);h.unmount();
  h=harness(base(),controllerBase({available:false,left:null,right:null,
    connection_left:null,connection_right:null,reason:'No controller battery report was received.'}));await h.ready();
  assert.match(content(h.tree),/Controllers: left — · right —/);
  h.details();assert.match(content(h.tree),/No controller battery report was received/);h.unmount();

  // A commit failure must retain the restored state, without a Saved confirmation.
  h=harness();await h.ready();
  h.methods.battery_set_enabled=enabled=>{assert.equal(enabled,true);return {
    ok:false,error:'Settings commit failed; previous settings restored.',state:base(),};};
  toggle(h).props.onChange(true);await settle();h.render();
  assert.equal(toggle(h).props.checked,false);assert.equal(toggle(h).props.disabled,false);
  assert.match(toggle(h).props.description,/Settings commit failed/,'the failure stays on the toggle');
  assert.deepEqual(h.toasts.map(t=>t.title),['Battery']);assert.match(h.toasts[0].body,/Settings commit failed/);
  // Retrying remains possible after a recoverable operation error.
  const committed=base({managed:true,enabled:true,requested_enabled:true,current_mode:'Long_Life',baseline:'Fast'});
  h.methods.battery_set_enabled=()=>({ok:true,state:committed});
  toggle(h).props.onChange(true);await settle();h.render();
  assert.equal(toggle(h).props.checked,true);assert.equal(h.toasts.at(-1).body,'Battery protection on.');
  assert.doesNotMatch(toggle(h).props.description,/commit failed/,'success clears the failure');
  h.details();assert.ok(button(h,'Restore previous charging mode'),'managed charging can be handed back from Details');
  assert.equal(count(h,'battery_set_enabled'),2);h.unmount();

  // D2 answers: a refusal without status (refused before any change) shows the generic text, as a rejected call did;
  // a read that the backend refuses still shows its status and its error.
  h=harness();await h.ready();
  h.methods.battery_set_enabled=()=>({ok:false,error:'This module is disabled. Enable it in Manage modules.'});
  toggle(h).props.onChange(true);await settle();h.render();
  assert.equal(h.toasts.at(-1).body,'The change could not be confirmed. Check the battery state before trying again.');
  h.unmount();h=harness();
  h.methods.battery_get_status=()=>({ok:false,error:'Battery status could not be read.',
    state:base({error:'Battery status could not be read.'})});
  await h.ready();assert.match(content(h.tree),/Battery status could not be read\./);h.unmount();

  // An interrupted first takeover is recoverable even though managed is still false.
  const interrupted=base({enabled:true,current_mode:'Long_Life',recovery_pending:true,
    error:'Hardware rollback failed; recovery remains pending.'});
  h=harness(interrupted);await h.ready();
  assert.equal(toggle(h).props.disabled,true);
  assert.match(content(h.tree),/A charging change didn't finish Restore the previous charging mode before changing it again\./);
  assert.match(h.summary(interrupted),/needs recovery/);
  assert.equal(toggle(h).props.checked,true);
  h.methods.battery_release_control=()=>({ok:true,state:base()});
  button(h,'Restore previous charging mode').props.onClick();await settle();h.render();
  assert.equal(count(h,'battery_release_control'),1);assert.equal(toggle(h).props.disabled,false);
  assert.equal(h.toasts.at(-1).body,'Previous charging mode restored.');
  assert.doesNotMatch(content(h.tree),/didn't finish/);h.unmount();

  // Failed recovery remains actionable, while unknown actual values are never Off.
  h=harness(interrupted);await h.ready();
  h.methods.battery_release_control=()=>({ok:false,error:'Firmware is unavailable.',state:interrupted});
  button(h,'Restore previous charging mode').props.onClick();await settle();h.render();
  assert.match(button(h,'Restore previous charging mode').props.description,/Firmware is unavailable/);
  h.unmount();h=harness(base({enabled:null,capacity:null,current_mode:null}));await h.ready();
  assert.match(toggle(h).props.description,/Current state unknown\.$/);assert.equal(toggle(h).props.disabled,true);
  assert.match(content(h.tree),/Battery — · Not charging/);
  assert.doesNotMatch(content(h.tree),/Battery 0%/);h.unmount();

  // Requested and actual state remain separate when firmware drift is detected.
  h=harness(base({managed:true,enabled:false,requested_enabled:true,baseline:'Fast'}));await h.ready();
  assert.equal(toggle(h).props.checked,true);assert.match(toggle(h).props.description,/Waiting for the battery to confirm…$/);
  h.details();assert.match(content(h.tree),/Saved choice On/);
  assert.match(h.summary(base({managed:true,requested_enabled:true})),/saved choice pending/);h.unmount();

  // A stale status response cannot replace a mutation result; rapid clicks serialize.
  h=harness();await h.ready();const oldRead=deferred(),mutation=deferred();
  h.methods.battery_get_status=()=>oldRead.promise;h.tick();await settle();
  h.methods.battery_set_enabled=()=>mutation.promise;
  toggle(h).props.onChange(true);toggle(h).props.onChange(false);await settle();
  assert.equal(count(h,'battery_set_enabled'),0,'Earlier read must finish before writing');
  oldRead.resolve(base());await settle();assert.equal(count(h,'battery_set_enabled'),1);
  mutation.resolve({ok:true,state:committed});await settle();h.render();
  assert.equal(toggle(h).props.checked,true);assert.doesNotMatch(toggle(h).props.description,/Waiting/);h.unmount();

  // Hidden/unmounted pages have no polling, and a new visible lease reads again.
  h=harness();await h.ready();const initialReads=count(h,'battery_get_status');
  h.setVisible(false);assert.equal(h.timers.size,0);h.tick();await settle();
  assert.equal(count(h,'battery_get_status'),initialReads);
  h.setVisible(true);await settle();h.render();assert.equal(count(h,'battery_get_status'),initialReads+1);
  h.unmount();assert.equal(h.timers.size,0);

  // An unreadable mode (fast charging and protection both on) offers a reset, only then.
  assert.equal(button(h,'Reset charging mode to Standard'),undefined);
  const conflict=base({supported:false,enabled:null,current_mode:null,mode_conflict:true,
    reason:'The firmware reports fast charging and battery protection as both on, so the charging mode cannot be read.'});
  h=harness(conflict);await h.ready();
  assert.match(content(h.tree),/so the charging mode cannot be read/);
  h.methods.battery_reset_charging_mode=()=>({ok:true,state:base({current_mode:'Standard'})});
  button(h,'Reset charging mode to Standard').props.onClick();await settle();h.render();
  assert.equal(count(h,'battery_reset_charging_mode'),1);assert.equal(h.toasts.at(-1).body,'Charging mode reset to Standard.');
  assert.equal(button(h,'Reset charging mode to Standard'),undefined,'gone once the mode can be read');h.unmount();

  // The rules both UIs share (src/battery.tsx and the Desktop ModulePage.qml import the same module).
  const B=await import(require('node:url').pathToFileURL(require('node:path').join(__dirname,'../desktop/native/battery.mjs')).href);
  assert.equal(B.protectionChecked(base({managed:true,enabled:false,requested_enabled:true})),true,'the saved choice while Companion manages charging');
  assert.equal(B.protectionChecked(base({enabled:true})),true,'the actual state otherwise');
  assert.equal(B.protectionLocked(base({enabled:null})),true,'an unknown actual state locks the switch');
  assert.equal(B.protectionLocked(base({recovery_pending:true})),true);
  assert.equal(B.protectionLocked(base()),false);
  assert.match(B.protectionNote(base({enabled:null})),/Current state unknown\.$/);
  assert.equal(B.controllersText({left:80.4,right:250,connection_left:'detached'}),'left 80% (detached) · right —','impossible levels are not shown');
  assert.equal(B.resetShown(base({mode_conflict:true})),true);assert.equal(B.resetShown(base()),false,'the reset is offered only for an unreadable mode');
  assert.equal(B.levelText(-1),'—');assert.equal(B.levelText(null),'—');
  console.log('Battery UI keeps actual/saved states distinct, reports rollback/recovery, prevents stale saves, and stops hidden polling.');
});
