const { assert, run, settle: wait, mount: load, label: find, button: findButton, component: findComponent } = require('./frontend_harness.cjs');
const settle = () => wait(12);
const anyNode = (node, test) => !node || typeof node !== 'object' ? undefined : !Array.isArray(node) && test(node) ? node
  : (Array.isArray(node) ? node : [node.props?.children, node.props?.description]).reduce((hit, child) => hit ?? anyNode(child, test), undefined);
const scopeMenu = tree => anyNode(tree, n => n.type === 'DropdownItem' && /^Settings for /.test(n.props?.label));
function mount(file, component, responses, initialProps = {}) {
  const router = {MainRunningApp:{appid:111}};
  const h = load(file, {component:'TestedComponent', props:initialProps, responses,
    append:`export {${component} as TestedComponent${file==='tdp.tsx'?', AppWatcher':''}};`,
    // Confirmations are answered with OK here; frontend_tdp_recovery checks that nothing runs before it.
    ui:{Router:router,findModuleExport:()=>undefined,showModal:node=>node.props.onOK()}});
  return {calls:h.calls,callbacks:h.listeners,
    // Only one-shot timers matter here; intervals are never fired by these scenarios.
    timers:{get size(){return h.timeouts().length;},values:()=>h.timeouts().values()},
    startWatcher(){h.exports.AppWatcher.start();},
    setGame(appid){router.MainRunningApp=appid?{appid,display_name:`Game ${appid}`}:null;return h.exports.AppWatcher.check();},
    render:nextProps=>nextProps?h.render(nextProps):h.render(),
    hide:h.hide,show:h.show,unmount:h.unmount,fire:h.fire,
  };
}
async function watcherLogging(file){
 const records=[];let fail=true,calls=0;
 const secret='private-secret-marker /home/private/full-path private-serial-marker';
 const logger=Object.fromEntries(['debug','log','info','warn','error'].map(level=>[level,(...args)=>records.push({level,text:args.map(String).join(' ')})]));
 const h=load(file,{append:'export {AppWatcher as TestedWatcher};',clock:100000,globals:{console:logger},
  window:{SteamClient:{GameSessions:{RegisterForAppLifetimeNotifications(){return{unregister(){}};}}},SleepManager:{RegisterForNotifyResumeFromSuspend(){return()=>{};}}},
  api:{callable:()=>async()=>{calls++;if(fail)throw new Error(secret);return{};}},
  ui:{Router:{MainRunningApp:{appid:111}},findModuleExport(){}}});
 const watcher=h.exports.TestedWatcher;watcher.start();await settle();
 for(let i=0;i<9;i++){h.advance(2000);h.fire('interval');await settle();}
 assert.equal(records.length,1,`${file} coalesces repeated foreground reporting failures`);
 assert.equal(records[0].level,'warn');
 fail=false;h.advance(2000);h.fire('interval');await settle();watcher.stop();
 assert.equal(calls,11,'Logging never suppresses the actual report retry.');
 assert.equal(records.length,2);assert.equal(records[1].level,'info');
 assert.ok(records[1].text.endsWith('foreground_reporting_restored'));
 assert.ok(!JSON.stringify(records).includes(secret),'Raw exception objects/paths/tokens stay out of frontend logs.');
}
// TDP and vibration share one poll: one interval and one set of Steam
// subscriptions; a stopped module gets neither ticks nor resume callbacks.
async function sharedWatcher(){
 const h=load('tdp.tsx',{append:'export {AppWatcher as TestedWatcher}; export * as shared from "./shared";',clock:100000,
  window:{SteamClient:{GameSessions:{RegisterForAppLifetimeNotifications(fn){h.listeners.lifetime=fn;return{unregister(){}};}}},
   SleepManager:{RegisterForNotifyResumeFromSuspend(fn){h.listeners.resume=fn;return()=>{};}}},
  ui:{Router:{MainRunningApp:{appid:111}},findModuleExport(){}}});
 const tdp=h.exports.TestedWatcher,seen=[];let resumed=0;
 const other={async check(app){seen.push(app&&String(app.appid));},resume(){resumed++;}};
 tdp.start();const off=h.exports.shared.watchRunningApp(other);await settle();
 assert.equal(h.intervals().length,1,'one poll timer serves both modules');
 h.advance(6000);h.fire('interval');await settle();
 assert.deepEqual(seen,['111','111']);assert.equal(h.calls.filter(c=>c.name==='set_active_app').length,2,'TDP heartbeat still due every 6 s');
 h.listeners.resume();await settle();
 assert.equal(resumed,1);assert.equal(h.calls.filter(c=>c.name==='reapply').length,1,'resume reapplies TDP');
 tdp.stop();h.advance(6000);h.fire('interval');h.listeners.resume();await settle();
 assert.equal(h.calls.filter(c=>c.name==='set_active_app').length,2,'a stopped module receives no ticks');
 assert.equal(h.calls.filter(c=>c.name==='reapply').length,1,'a stopped module receives no resume');
 assert.equal(resumed,2);assert.equal(h.intervals().length,1,'the other module keeps polling');
 off();assert.equal(h.timers.size,0,'the last module out stops the poll');
}
run(async()=>{
 await watcherLogging('tdp.tsx');await watcherLogging('vibration.tsx');await sharedWatcher();
 const values={level:2,mode:1,touchpadIntensity:2,touchpadEnabled:true};
 const initialVibe={
   vibe_is_ready:{ready:true},vibe_get_settings:{settings:values,app_id:'0',profile_id:'0',overwrite:false},
   vibe_get_driver_status:{found:true,paths:['initial']},vibe_get_capabilities:{mode:['fps','racing']},
 };
 const initialTdp={
   is_ready:{ready:true},get_settings:{enabled:true,spl:15000,sppt:18000,fppt:25000},
   get_power_source:{ac:false},get_extras_unlocked:false,get_caps:{},
   get_game_profile:{exists:true,profile:{spl:20000,sppt:23000,fppt:30000},ac_separate:false},
 };
 const count=(h,name)=>h.calls.filter(c=>c.name===name).length;
 // Initialization snapshots also seed the visible page. A later opening still
 // refreshes hardware state; the unchanged page must not duplicate its reads.
 const initial=mount('vibration.tsx','LGoVibeControl',initialVibe);
 initial.render();await settle();initial.render();await settle();
 for(const read of ['vibe_is_ready','vibe_get_settings','vibe_get_driver_status','vibe_get_capabilities'])
   assert.equal(count(initial,read),1,`Vibration initialization reads ${read} once.`);
 initial.hide();initial.show();await settle();
 assert.equal(count(initial,'vibe_get_driver_status'),2,'Reopening still refreshes the driver.');
 initial.unmount();
 const base=mount('tdp.tsx','TdpPage',initialTdp);
 await base.setGame(null);base.startWatcher();base.render();await settle();base.render();await settle();
 for(const read of ['is_ready','get_settings','get_power_source','get_extras_unlocked','get_caps'])
   assert.equal(count(base,read),1,`No-game TDP initialization reads ${read} once.`);
 base.hide();base.show();await settle();
 assert.equal(count(base,'get_power_source'),2,'Reopening still refreshes power source.');
 assert.equal(count(base,'get_settings'),2,'Reopening rereads settings the Desktop app may have changed.');
 await base.setGame(111);base.render();await settle();base.render();
 assert.equal(count(base,'get_game_profile'),1);
 await base.setGame(null);base.render();await settle();base.render();
 assert.equal(count(base,'get_settings'),3,'Leaving a game still reloads global settings.');
 assert.equal(count(base,'apply_tdp'),0,'The backend, not the panel, applies game and global limits.');
 base.unmount();
 const simple=mount('tdp.tsx','TdpPage',{
   ...initialTdp,
   get_extras_unlocked:()=>false,
   get_settings:{enabled:true,spl:20000,sppt:21000,fppt:22000,
     active_preset:'custom',advanced_tdp_control:false},
   get_caps:{min:5,std:{spl:35,sppt:37,fppt:45},max:{spl:50,sppt:50,fppt:50},
     extras:true,presets:{silent:{spl:8,sppt:15,fppt:20},
       balanced:{spl:16,sppt:25,fppt:30},performance:{spl:20,sppt:32,fppt:35},
       max:{spl:35,sppt:37,fppt:45}}},
 });
 await simple.setGame(null);simple.startWatcher();simple.render();await settle();
 simple.render();await settle();
 let simplePage=simple.render();
 const hasSection=(node,title)=>!!node && typeof node==='object' && (node.props?.title===title
   || (Array.isArray(node)?node:[node.props?.children]).some(child=>hasSection(child,title)));
 for(const title of ['Power','More'])assert.ok(hasSection(simplePage,title),`TDP section: ${title}`);
 assert.ok(findComponent(simplePage,'CpuPowerControlsSection'));
 assert.equal(find(simplePage,'Sustained power · 20 W').props.max,35);
 const sliderLabels=(node)=>Array.isArray(node)?node.flatMap(sliderLabels):
   !node||typeof node!=='object'?[]:
   [...(node.type==='SliderField'?[node.props.label]:[]),
     ...sliderLabels(node.props?.children)].flat();
 assert.equal(sliderLabels(simplePage).filter(x=>x.startsWith('Slow boost')||x.startsWith('Fast boost')).length,0);
 find(simplePage,'Sustained power · 20 W').props.onChange(35);
 simplePage=simple.render();
 await findButton(simplePage,'Apply').props.onClick();await settle();
 assert.deepEqual(simple.calls.filter(x=>x.name==='apply_tdp').at(-1).args.slice(0,3),
   [35000,37000,45000]);
 find(simple.render(),'Allow up to 50 W').props.onChange(true);await settle();
 simplePage=simple.render();
 assert.equal(find(simplePage,'Sustained power · 35 W').props.max,50);
 find(simplePage,'Sustained power · 35 W').props.onChange(50);
 await findButton(simple.render(),'Apply').props.onClick();await settle();
 assert.deepEqual(simple.calls.filter(x=>x.name==='apply_tdp').at(-1).args.slice(0,3),
   [50000,50000,50000]);
 find(simple.render(),'Separate boost limits').props.onChange(true);await settle();
 simplePage=simple.render();
 assert.equal(sliderLabels(simplePage).filter(x=>x.startsWith('Slow boost')||x.startsWith('Fast boost')).length,2);
 findButton(simplePage,'Performance').props.onClick();await settle();
 assert.deepEqual(simple.calls.filter(x=>x.name==='apply_tdp').at(-1).args.slice(0,3),
   [20000,32000,35000]);
 simple.unmount();
 // Completing init while hidden must not consume the next visible refresh.
 for(const [file,component,responses,read] of [
   ['tdp.tsx','TdpPage',initialTdp,'get_power_source'],
   ['vibration.tsx','LGoVibeControl',initialVibe,'vibe_get_driver_status'],
 ]){
   const h=mount(file,component,responses);h.render();h.hide();await settle();h.render();h.show();await settle();
   assert.equal(count(h,read),2,`${component} refreshes after hidden initialization.`);h.unmount();
 }
 // New hardware events during a slow init win over its earlier snapshot.
 let finishCaps;
 const power=mount('tdp.tsx','TdpPage',{...initialTdp,get_caps:()=>new Promise(resolve=>{finishCaps=resolve;})});
 power.render();await settle();power.callbacks.power_source({ac:true});finishCaps({});await settle();
 let seeded=power.render();await settle();
 seeded=power.render();
 assert.equal(count(power,'get_power_source'),1);
 assert.equal(findComponent(seeded,'CpuPowerControlsSection').props.powerSource,true,'New charger event survives slow init.');
 power.unmount();
 let finishModes;
 const driver=mount('vibration.tsx','LGoVibeControl',{
   ...initialVibe,vibe_get_capabilities:()=>new Promise(resolve=>{finishModes=resolve;}),
 });
 driver.render();await settle();driver.callbacks.device({found:true,paths:['new-device']});
 finishModes({mode:['fps']});await settle();seeded=driver.render();await settle();
 assert.equal(count(driver,'vibe_get_driver_status'),1);
 findButton(seeded,'Show details').props.onClick();seeded=driver.render();
 assert.ok(JSON.stringify(seeded).includes('new-device'),'New device event survives slow init.');driver.unmount();
 // Readiness retries cannot outlive either page, even when a cancelled timer
 // callback was already queued by the browser before cleanup ran.
 for(const [file,component,read] of [
   ['vibration.tsx','LGoVibeControl','vibe_is_ready'], // TDP: frontend_tdp_recovery
 ]){
   const h=mount(file,component,{[read]:{ready:false}});h.render();await settle();
   const retry=[...h.timers.values()];assert.equal(retry.length,1);h.unmount();
   assert.equal(h.timers.size,0,`${component} cancels readiness retries.`);
   retry[0].fn();await settle();assert.equal(count(h,read),1,`${component} rejects a queued retry after unmount.`);
 }
 let tree;
 const compatibilityNote='This kernel supports EPP presets only. Saved EPP 77 uses balance performance.';
 const notes=[];
 const legacy=mount('tdp.tsx','CpuPowerControlsSection',{get_cpu_power_controls:{success:true,available:true,error:'',
   cpu_boost:{available:true,enabled:false,error:''},
   epp:{available:true,error:'',min:0,max:255,value:'balance_performance',numeric_value:128,numeric_supported:false,
        profiles:['default','performance','balance_performance','balance_power','power'],compatibility_note:compatibilityNote}}},
   {onCompatibilityNote:note=>notes.push(note)});
 legacy.render();await settle();legacy.render();
 assert.equal(notes.at(-1),compatibilityNote,'The compatibility note is handed to the page for Show details.');
 assert.equal(legacy.calls.filter(c=>c.name==='set_epp').length,0);
 legacy.unmount();
 const presetState=(requested='performance')=>({success:true,available:true,error:'',
   profile:{app_id:'',ac_profile:false,active:true,cpu_boost_enabled:null,epp:requested},
   cpu_boost:{available:true,enabled:true,error:''},
   epp:{available:true,error:'',min:0,max:255,value:requested==='default'?'balance_performance':requested,
     numeric_value:requested==='default'?128:null,numeric_supported:false,
     profiles:['default','performance','balance_performance','balance_power','power']}});
 const presets=mount('tdp.tsx','CpuPowerControlsSection',{
   get_cpu_power_controls:presetState(),set_epp:(value)=>presetState(value),
 });
 presets.render();await settle();tree=presets.render();
 assert.equal(find(tree,'CPU energy preference · Prefer CPU').props.description,'Left: faster CPU. Right: more power left for the GPU.');
 findButton(tree,'Use System default').props.onClick();await settle();tree=presets.render();
 assert.equal(presets.calls.find(c=>c.name==='set_epp').args[0],'default');
 assert.equal(findButton(tree,'Use System default').props.disabled,true);
 const defaultSlider=find(tree,'CPU energy preference · Balanced · Prefer CPU');assert.equal(defaultSlider.props.value,1);
 // The explicit preset must save even when it matches default's live readback.
 defaultSlider.props.onChange(1);await settle();tree=presets.render();
 assert.equal(presets.calls.filter(c=>c.name==='set_epp').length,2);
 assert.equal(presets.calls.filter(c=>c.name==='set_epp')[1].args[0],'balance_performance');
 assert.equal(findButton(tree,'Use System default').props.disabled,false);
 find(tree,'CPU energy preference · Balanced · Prefer CPU').props.onChange(3);await settle();tree=presets.render();
 assert.equal(presets.calls.filter(c=>c.name==='set_epp')[2].args[0],'power');
 assert.ok(find(tree,'CPU energy preference · Prefer GPU'));
 presets.unmount();
 for(const leave of ['unmount','hide']){
   const epp=mount('tdp.tsx','CpuPowerControlsSection',{get_cpu_power_controls:{success:true,available:true,error:"",
      cpu_boost:{available:true,enabled:true,error:""},epp:{available:true,error:"",min:0,max:255,value:'128',numeric_value:128,numeric_supported:true,profiles:[]}},
      set_epp:{success:true,available:true,error:"",cpu_boost:{available:true,enabled:true,error:""},epp:{available:true,error:"",min:0,max:255,value:'204',numeric_value:204,numeric_supported:true,profiles:[]}}});
   epp.render();await settle();tree=epp.render();const slider=find(tree,'CPU energy preference');assert.ok(slider);slider.props.onChange(80);
   epp[leave]();await settle();assert.equal(epp.calls.filter(c=>c.name==='set_epp').length,1);
   assert.equal(epp.calls.find(c=>c.name==='set_epp').args[0],'204');epp.unmount();
 }
 const cpuState=(profile={app_id:'111',ac_profile:false,active:true,cpu_boost_enabled:true,epp:'128'})=>({
   success:true,available:true,error:'',profile,
   cpu_boost:{available:true,enabled:true,error:''},
   epp:{available:true,error:'',min:0,max:255,value:'128',numeric_value:128,numeric_supported:true,profiles:[]}
 });
 // A timeout already queued by the browser can fire after clearTimeout.
 // Superseded and repeated callbacks must not overwrite the latest gesture.
 for (const duplicate of [false, true]) {
   const h=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:cpuState(),set_epp:cpuState(),
   },{appId:'111',acProfile:false,expectedAppId:'111'});
   h.render();await settle();
   const slider=find(h.render(),'CPU energy preference');slider.props.onChange(20);
   const old=[...h.timers.values()][0].fn;
   if (duplicate) {
     old();await settle();old();await settle();
     assert.equal(count(h,'set_epp'),1,'A flushed CPU gesture is idempotent.');
   } else {
     slider.props.onChange(80);
     const latest=[...h.timers.values()][0].fn;
     old();await settle();
     assert.equal(count(h,'set_epp'),0,'A superseded CPU timer cannot write or clear the current timer.');
     assert.equal(h.timers.size,1);
     latest();await settle();
     assert.deepEqual(h.calls.filter(c=>c.name==='set_epp').map(c=>c.args[0]),['204']);
   }
   h.unmount();
 }
 // Repeated reopenings share the pending read and request one fresh snapshot
 // afterward. A reply captured before hiding must not become actionable.
 const globalCpu={app_id:'',ac_profile:false,active:true,cpu_boost_enabled:true,epp:'128'};
 const pendingCpu=[];
 const reopenedCpu=mount('tdp.tsx','CpuPowerControlsSection',{
   get_cpu_power_controls:()=>new Promise(resolve=>pendingCpu.push(resolve)),
 });
 reopenedCpu.render();
 for(let i=0;i<3;i++){reopenedCpu.hide();reopenedCpu.show();}
 assert.equal(count(reopenedCpu,'get_cpu_power_controls'),1,'Reopenings never overlap a pending CPU read.');
 pendingCpu.shift()(cpuState(globalCpu));await settle();
 assert.equal(count(reopenedCpu,'get_cpu_power_controls'),2,'A burst of reopenings produces one fresh read.');
 assert.equal(find(reopenedCpu.render(),'CPU Boost'),undefined,'The pre-hide reply remains discarded.');
 pendingCpu.shift()(cpuState(globalCpu));await settle();
 assert.equal(find(reopenedCpu.render(),'CPU Boost').props.disabled,false);reopenedCpu.unmount();
 // The follow-up read uses the latest game/AC context, not the callback that
 // launched the older request. Late context replies cannot seed the new editor.
 for(const next of [
   {appId:'222',acProfile:false,expectedAppId:'222'},
   {appId:'111',acProfile:true,expectedAppId:'111'},
 ]){
   const pending=[];
   const h=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:()=>new Promise(resolve=>pending.push(resolve)),
   },{appId:'111',acProfile:false,expectedAppId:'111'});
   h.render();h.render(next);
   assert.equal(count(h,'get_cpu_power_controls'),1,'Context changes wait for the pending read.');
   pending.shift()(cpuState());await settle();
   assert.equal(count(h,'get_cpu_power_controls'),2);
   assert.deepEqual(Array.from(h.calls.at(-1).args),[next.appId,next.acProfile]);
   assert.equal(find(h.render(),'CPU Boost'),undefined,'The old context is never accepted.');
   pending.shift()(cpuState({app_id:next.appId,ac_profile:next.acProfile,active:false,cpu_boost_enabled:false,epp:'51'}));
   await settle();const nextTree=h.render();
   assert.equal(find(nextTree,'CPU Boost').props.checked,false);assert.equal(find(nextTree,'CPU energy preference').props.value,20);
   h.unmount();
 }
 for(const leave of ['hide','unmount']){
   let finish;
   const h=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:()=>new Promise(resolve=>{finish=resolve;}),
   });
   h.render();h.hide();h.show();h[leave]();finish(cpuState(globalCpu));await settle();
   assert.equal(count(h,'get_cpu_power_controls'),1,`A queued CPU refresh cannot run after ${leave}.`);
   h.unmount();
 }
 for(const context of [
   {appId:'',acProfile:false,expectedAppId:''},
   {appId:'',acProfile:false,expectedAppId:'111'},
   {appId:'111',acProfile:false,expectedAppId:'111'},
   {appId:'111',acProfile:true,expectedAppId:'111'},
 ]) {
   const state=cpuState({app_id:context.appId,ac_profile:context.acProfile,active:true,cpu_boost_enabled:true,epp:'128'});
   const cpu=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:state,set_cpu_boost:state,set_epp:state,
   },context);
   cpu.render();await settle();tree=cpu.render();
   assert.deepEqual(Array.from(cpu.calls[0].args),[context.appId,context.acProfile]);
   find(tree,'CPU Boost').props.onChange(false);await settle();tree=cpu.render();
   assert.deepEqual(Array.from(cpu.calls.find(c=>c.name==='set_cpu_boost').args),
     [false,context.appId,context.acProfile,context.expectedAppId]);
   find(tree,'CPU energy preference').props.onChange(80);cpu.hide();await settle();
   assert.deepEqual(Array.from(cpu.calls.find(c=>c.name==='set_epp').args),
     ['204',context.appId,context.acProfile,context.expectedAppId]);
   cpu.unmount();assert.equal(cpu.timers.size,0);
 }
 // The AC editor must display saved AC values while battery remains active.
 const acState=cpuState({app_id:'111',ac_profile:true,active:false,cpu_boost_enabled:false,epp:'204'});
 const acResponses={get_cpu_power_controls:acState,set_epp:acState};
 const acProps={appId:'111',acProfile:true,expectedAppId:'111',powerSource:false};
 const ac=mount('tdp.tsx','CpuPowerControlsSection',acResponses,acProps);
 ac.render();await settle();tree=ac.render();
 assert.equal(find(tree,'CPU Boost').props.checked,false);
 assert.equal(find(tree,'CPU energy preference').props.value,80);
 // A charger event rereads capabilities/live state without changing the editor.
 acResponses.get_cpu_power_controls={...acState,profile:{...acState.profile,active:true}};
 ac.render({...acProps,powerSource:true});await settle();tree=ac.render();
 assert.equal(ac.calls.filter(c=>c.name==='get_cpu_power_controls').length,2);
 assert.equal(find(tree,'CPU Boost').props.checked,true);
 assert.equal(find(tree,'CPU energy preference').props.value,50);
 assert.ok(ac.calls.filter(c=>c.name==='get_cpu_power_controls').every(c=>c.args[0]==='111'&&c.args[1]===true));
 ac.unmount();
 // A pending slider flushed by a scope remount keeps the original game/AC
 // context. A late reply cannot overwrite the next editor's selected values.
 for(const nextContext of [
   {appId:'111',acProfile:true,expectedAppId:'111'},
   {appId:'222',acProfile:false,expectedAppId:'222'},
 ]) {
   let finishOld;const busy=[];
   const old=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:cpuState(),set_epp:()=>new Promise(resolve=>{finishOld=resolve;}),
   },{appId:'111',acProfile:false,expectedAppId:'111',onBusyChange:(owner,value)=>busy.push({owner,value})});
   old.render();await settle();find(old.render(),'CPU energy preference').props.onChange(80);old.unmount();
   assert.deepEqual(Array.from(old.calls.find(c=>c.name==='set_epp').args),['204','111',false,'111']);
   assert.equal(old.timers.size,0);
   assert.equal(busy.at(-1).value,true,'A flushed old editor remains busy until its save finishes.');
   const next=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:cpuState({app_id:nextContext.appId,ac_profile:nextContext.acProfile,
       active:false,cpu_boost_enabled:false,epp:'51'}),
   },nextContext);
   next.render();await settle();assert.equal(find(next.render(),'CPU energy preference').props.value,20);
   finishOld(cpuState({...cpuState().profile,epp:'204'}));await settle();
   assert.equal(busy.at(-1).value,false);
   assert.ok(busy.every(item=>item.owner===busy[0].owner));
   assert.equal(find(next.render(),'CPU energy preference').props.value,20);assert.equal(next.calls.length,1);
   next.unmount();
 }
 // Mismatched profile replies must never become actionable controls.
 const mismatched=mount('tdp.tsx','CpuPowerControlsSection',{get_cpu_power_controls:cpuState()},
   {appId:'222',acProfile:false,expectedAppId:'222'});
 mismatched.render();await settle();tree=mismatched.render();
 assert.equal(find(tree,'CPU Boost').props.disabled,true);mismatched.unmount();
 // Scope controls cannot delete or change a profile underneath a pending CPU
 // save. Multiple keyed instances keep independent ownership of this guard.
 const page=mount('tdp.tsx','TdpPage',{
   is_ready:{ready:true,error:''},get_settings:{enabled:true,spl:15000,sppt:18000,fppt:25000},
   get_power_source:{ac:false},get_extras_unlocked:false,get_caps:{},
   get_game_profile:{exists:true,profile:{spl:15000,sppt:18000,fppt:25000,preset:'balanced'},
     ac_separate:true,ac_profile:{spl:25000,sppt:28000,fppt:35000,ac_preset:'performance'}},
 });
 page.startWatcher();page.render();await settle();page.render();await settle();tree=page.render();
 const reportBusy=findComponent(tree,'CpuPowerControlsSection').props.onBusyChange;
 const firstOwner={},secondOwner={};
 reportBusy(firstOwner,true);reportBusy(secondOwner,true);reportBusy(firstOwner,false);
 tree=page.render();
 assert.equal(find(tree,'Enable TDP control').props.disabled,true);
 assert.equal(scopeMenu(tree).props.disabled,true);
 assert.equal(find(tree,'Edit settings for').props.disabled,true);
 const beforeBlocked=page.calls.length;
 await scopeMenu(tree).props.onChange({data:'global'});
 find(tree,'Edit settings for').props.onChange({data:'charger'});
 await find(tree,'Enable TDP control').props.onChange(false);
 assert.equal(page.calls.length,beforeBlocked,'Handlers guard pending saves even before a disabled button repaints.');
 assert.equal(find(page.render(),'Edit settings for').props.selectedOption,'battery');
 reportBusy(secondOwner,false);tree=page.render();
 assert.equal(scopeMenu(tree).props.disabled,false);
 await scopeMenu(tree).props.onChange({data:'global'});await settle();
 assert.equal(page.calls.filter(c=>c.name==='set_game_profile_active').length,1);
 page.unmount();
 console.log('EPP saves on close; CPU edits preserve global/game/battery/AC scope and ignore stale replies.');
});
