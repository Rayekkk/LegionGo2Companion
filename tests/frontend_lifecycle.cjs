const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
function mount(file, component, responses, initialProps = {}) {
  const slots = [], effects = new Map(), pendingEffects = [];
  const callbacks = {}, router = {MainRunningApp:{appid:111}};
  const timers = new Map(), calls = []; let cursor=0, nextTimer=0, visible=true, props=initialProps;
  const hooks = {
    useState(initial) { const i=cursor++; if (!(i in slots)) slots[i]=initial;
      return [slots[i], value => slots[i]=typeof value==='function' ? value(slots[i]) : value]; },
    useRef(value) { const i=cursor++; return slots[i] ||= {current:value}; },
    useCallback(fn, deps) { const i=cursor++, prev=slots[i];
      if (!prev || !deps || deps.some((x,j)=>x!==prev.deps[j])) slots[i]={fn,deps};
      return slots[i].fn; },
    useEffect(fn, deps) { const i=cursor++, prev=effects.get(i);
      if (!prev || !deps || deps.some((x,j) => x!==prev.deps[j])) pendingEffects.push(() => {
        prev?.cleanup?.(); effects.set(i,{deps,cleanup:fn()}); }); },
  };
  const api = {useQuickAccessVisible:()=>visible,
    addEventListener(name,callback){callbacks[name]=callback;},
    removeEventListener(name,callback){if(callbacks[name]===callback)delete callbacks[name];},
    toaster:{toast(){}}, callable:name=>async(...args)=> {
      calls.push({name,args}); const response=responses[name];
      return typeof response === 'function' ? await response(...args) : structuredClone(response || {success:true});
    }};
  const jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props}),Fragment:'Fragment'};
  const mod={exports:{}};
  const code=ts.transpileModule(fs.readFileSync(path.join(root,'src',file),'utf8')+
    `\nexport {${component} as TestedComponent${file==='tdp.tsx'?', AppWatcher':''}};`,
    {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code,{module:mod,exports:mod.exports,console,window:{},
    setTimeout:(fn,delay)=>{let id=++nextTimer;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
    setInterval:()=>999,clearInterval(){},
    require:name=>{
      if(name==='react')return hooks;
      if(name==='react/jsx-runtime')return jsx;
      if(name==='@decky/api')return api;
      if(name==='@decky/ui')return new Proxy({Router:router,findModuleExport:()=>undefined},
        {get:(o,k)=>k in o?o[k]:k});
      throw Error(name);
    }});
  return {calls,timers,callbacks,
    startWatcher(){mod.exports.AppWatcher.start();},
    setGame(appid){router.MainRunningApp=appid?{appid,display_name:`Game ${appid}`}:null;return mod.exports.AppWatcher.check();},
    render(nextProps){if(nextProps)props=nextProps;cursor=0;const tree=mod.exports.TestedComponent(props);pendingEffects.splice(0).forEach(fn=>fn());return tree;},
    hide(){visible=false;return this.render();},
    show(){visible=true;return this.render();},
    unmount(){for(const e of effects.values())e.cleanup?.();effects.clear();},
  };
}
function find(node,label){
  if(!node||typeof node!=='object')return;
  if(node.props?.label===label)return node;
  for(const child of Array.isArray(node)?node:[node.props?.children]){const match=find(child,label);if(match)return match;}
}
function findComponent(node,name){
  if(!node||typeof node!=='object')return;
  if(typeof node.type==='function'&&node.type.name===name)return node;
  for(const child of Array.isArray(node)?node:[node.props?.children]){const match=findComponent(child,name);if(match)return match;}
}
function findButton(node,text){
  if(!node||typeof node!=='object')return;
  if(node.type==='ButtonItem'&&node.props?.children===text)return node;
  for(const child of Array.isArray(node)?node:[node.props?.children]){const match=findButton(child,text);if(match)return match;}
}
(async()=>{
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
 assert.equal(initial.calls.length,4,'Vibration initialization uses four RPCs.');
 assert.equal(count(initial,'vibe_get_driver_status'),1);
 initial.hide();initial.show();await settle();
 assert.equal(count(initial,'vibe_get_driver_status'),2,'Reopening still refreshes the driver.');
 initial.unmount();
 const base=mount('tdp.tsx','TdpPage',initialTdp);
 await base.setGame(null);base.startWatcher();base.render();await settle();base.render();await settle();
 assert.equal(base.calls.filter(c=>c.name!=='set_active_app').length,5,'No-game TDP initialization uses five RPCs.');
 assert.equal(count(base,'get_power_source'),1);assert.equal(count(base,'get_settings'),1);
 base.hide();base.show();await settle();
 assert.equal(count(base,'get_power_source'),2,'Reopening still refreshes power source.');
 await base.setGame(111);base.render();await settle();base.render();
 assert.equal(count(base,'get_game_profile'),1);assert.equal(count(base,'apply_tdp'),1);
 await base.setGame(null);base.render();await settle();base.render();
 assert.equal(count(base,'get_settings'),2,'Leaving a game still reloads global settings.');
 assert.equal(count(base,'apply_tdp'),2,'Leaving a game still reapplies the global profile.');
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
 let simplePage=simple.render();
 const sections=simplePage.props.children[1].props.children;
 assert.equal(sections[0].props.title,'Game Profile');
 assert.equal(sections[1].type.name,'LivePanel');
 assert.equal(sections[2].props.title,'Preset');
 assert.equal(simplePage.props.children[2].type.name,'CpuPowerControlsSection');
 assert.equal(simplePage.props.children[4].props.title,'Extras');
 assert.equal(find(simplePage,'TDP - 20 W').props.max,35);
 const sliderLabels=(node)=>Array.isArray(node)?node.flatMap(sliderLabels):
   !node||typeof node!=='object'?[]:
   [...(node.type==='SliderField'?[node.props.label]:[]),
     ...sliderLabels(node.props?.children)].flat();
 assert.equal(sliderLabels(simplePage).filter(x=>x.startsWith('SPPT')||x.startsWith('FPPT')).length,0);
 find(simplePage,'TDP - 20 W').props.onChange(35);
 simplePage=simple.render();
 await findButton(simplePage,'Apply TDP').props.onClick();await settle();
 assert.deepEqual(simple.calls.filter(x=>x.name==='apply_tdp').at(-1).args.slice(0,3),
   [35000,37000,45000]);
 find(simple.render(),'Unlock Custom TDP to 50 W').props.onChange(true);await settle();
 simplePage=simple.render();
 assert.equal(find(simplePage,'TDP - 35 W').props.max,50);
 find(simplePage,'TDP - 35 W').props.onChange(50);
 await findButton(simple.render(),'Apply TDP').props.onClick();await settle();
 assert.deepEqual(simple.calls.filter(x=>x.name==='apply_tdp').at(-1).args.slice(0,3),
   [50000,50000,50000]);
 find(simple.render(),'Advanced TDP Control').props.onChange(true);await settle();
 simplePage=simple.render();
 assert.equal(sliderLabels(simplePage).filter(x=>x.startsWith('SPPT')||x.startsWith('FPPT')).length,2);
 await findButton(simplePage,'Performance').props.onClick();await settle();
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
 assert.ok(JSON.stringify(seeded).includes('new-device'),'New device event survives slow init.');driver.unmount();
 // Readiness retries cannot outlive either page, even when a cancelled timer
 // callback was already queued by the browser before cleanup ran.
 for(const [file,component,read] of [
   ['tdp.tsx','TdpPage','is_ready'],['vibration.tsx','LGoVibeControl','vibe_is_ready'],
 ]){
   const h=mount(file,component,{[read]:{ready:false}});h.render();await settle();
   const retry=[...h.timers.values()];assert.equal(retry.length,1);h.unmount();
   assert.equal(h.timers.size,0,`${component} cancels readiness retries.`);
   retry[0].fn();await settle();assert.equal(count(h,read),1,`${component} rejects a queued retry after unmount.`);
 }
 const vibe=mount('vibration.tsx','LGoVibeControl',{
   vibe_is_ready:{ready:true},vibe_get_settings:{settings:values,app_id:'111',profile_id:'111',overwrite:true},
   vibe_get_driver_status:{found:true,paths:['test']},vibe_get_capabilities:{mode:['fps','racing']},
   vibe_set_intensity:{success:true,settings:{...values,level:3}}
 });
 vibe.render();await settle();let tree=vibe.render();
 const labels=[]; function all(n){if(!n||typeof n!=='object')return;if(n.props?.label)labels.push(n.props.label);for(const x of Array.isArray(n)?n:[n.props?.children])all(x);}all(tree);
 const slider=find(tree,'Vibration Intensity')||find(tree,'Intensity');
 assert.ok(slider,JSON.stringify(labels));slider.props.onChange(3);vibe.unmount();await settle();
 const save=vibe.calls.find(c=>c.name==='vibe_set_intensity');assert.ok(save);assert.deepEqual(Array.from(save.args),[3,'111','111']);
 assert.equal(vibe.timers.size,0);
 const compatibilityNote='This kernel supports EPP presets only. Saved EPP 77 uses balance performance.';
 const legacy=mount('tdp.tsx','CpuPowerControlsSection',{get_cpu_power_controls:{success:true,available:true,error:'',
   cpu_boost:{available:true,enabled:false,error:''},
   epp:{available:true,error:'',min:0,max:255,value:'balance_performance',numeric_value:128,numeric_supported:false,
        profiles:['default','performance','balance_performance','balance_power','power'],compatibility_note:compatibilityNote}}});
 legacy.render();await settle();const legacyTree=legacy.render();
 assert.equal(find(legacyTree,'EPP compatibility').props.description,compatibilityNote);
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
 assert.match(find(tree,'EPP - Prefer CPU').props.description,/CPU energy demand/);
 findButton(tree,'Use System default EPP').props.onClick();await settle();tree=presets.render();
 assert.equal(presets.calls.find(c=>c.name==='set_epp').args[0],'default');
 assert.equal(findButton(tree,'> System default EPP').props.disabled,true);
 const defaultSlider=find(tree,'EPP - Balanced · Prefer CPU');assert.equal(defaultSlider.props.value,1);
 // The explicit preset must save even when it matches default's live readback.
 defaultSlider.props.onChange(1);await settle();tree=presets.render();
 assert.equal(presets.calls.filter(c=>c.name==='set_epp').length,2);
 assert.equal(presets.calls.filter(c=>c.name==='set_epp')[1].args[0],'balance_performance');
 assert.equal(findButton(tree,'Use System default EPP').props.disabled,false);
 find(tree,'EPP - Balanced · Prefer CPU').props.onChange(3);await settle();tree=presets.render();
 assert.equal(presets.calls.filter(c=>c.name==='set_epp')[2].args[0],'power');
 assert.ok(find(tree,'EPP - Prefer GPU'));
 presets.unmount();
 for(const leave of ['unmount','hide']){
   const epp=mount('tdp.tsx','CpuPowerControlsSection',{get_cpu_power_controls:{success:true,available:true,error:"",
      cpu_boost:{available:true,enabled:true,error:""},epp:{available:true,error:"",min:0,max:255,value:'128',numeric_value:128,numeric_supported:true,profiles:[]}},
      set_epp:{success:true,available:true,error:"",cpu_boost:{available:true,enabled:true,error:""},epp:{available:true,error:"",min:0,max:255,value:'204',numeric_value:204,numeric_supported:true,profiles:[]}}});
   epp.render();await settle();tree=epp.render();const slider=find(tree,'EPP');assert.ok(slider);slider.props.onChange(80);
   epp[leave]();await settle();assert.equal(epp.calls.filter(c=>c.name==='set_epp').length,1);
   assert.equal(epp.calls.find(c=>c.name==='set_epp').args[0],'204');epp.unmount();
 }
 const cpuState=(profile={app_id:'111',ac_profile:false,active:true,cpu_boost_enabled:true,epp:'128'})=>({
   success:true,available:true,error:'',profile,
   cpu_boost:{available:true,enabled:true,error:''},
   epp:{available:true,error:'',min:0,max:255,value:'128',numeric_value:128,numeric_supported:true,profiles:[]}
 });
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
   assert.equal(find(nextTree,'CPU Boost').props.checked,false);assert.equal(find(nextTree,'EPP').props.value,20);
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
   find(tree,'EPP').props.onChange(80);cpu.hide();await settle();
   assert.deepEqual(Array.from(cpu.calls.find(c=>c.name==='set_epp').args),
     ['204',context.appId,context.acProfile,context.expectedAppId]);
   cpu.unmount();assert.equal(cpu.timers.size,0);
 }
 // The AC editor must display saved AC values while battery remains active.
 const acState=cpuState({app_id:'111',ac_profile:true,active:false,cpu_boost_enabled:false,epp:'204'});
 const acResponses={get_cpu_power_controls:acState,set_epp:acState};
 const acProps={appId:'111',acProfile:true,expectedAppId:'111',scopeLabel:'Example - AC profile',powerSource:false};
 const ac=mount('tdp.tsx','CpuPowerControlsSection',acResponses,acProps);
 ac.render();await settle();tree=ac.render();
 assert.equal(find(tree,'CPU Boost').props.checked,false);
 assert.equal(find(tree,'EPP').props.value,80);
 assert.match(find(tree,acProps.scopeLabel).props.description,/Editing saved/);
 // A charger event rereads capabilities/live state without changing the editor.
 acResponses.get_cpu_power_controls={...acState,profile:{...acState.profile,active:true}};
 ac.render({...acProps,powerSource:true});await settle();tree=ac.render();
 assert.equal(ac.calls.filter(c=>c.name==='get_cpu_power_controls').length,2);
 assert.equal(find(tree,'CPU Boost').props.checked,true);
 assert.equal(find(tree,'EPP').props.value,50);
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
   old.render();await settle();find(old.render(),'EPP').props.onChange(80);old.unmount();
   assert.deepEqual(Array.from(old.calls.find(c=>c.name==='set_epp').args),['204','111',false,'111']);
   assert.equal(old.timers.size,0);
   assert.equal(busy.at(-1).value,true,'A flushed old editor remains busy until its save finishes.');
   const next=mount('tdp.tsx','CpuPowerControlsSection',{
     get_cpu_power_controls:cpuState({app_id:nextContext.appId,ac_profile:nextContext.acProfile,
       active:false,cpu_boost_enabled:false,epp:'51'}),
   },nextContext);
   next.render();await settle();assert.equal(find(next.render(),'EPP').props.value,20);
   finishOld(cpuState({...cpuState().profile,epp:'204'}));await settle();
   assert.equal(busy.at(-1).value,false);
   assert.ok(busy.every(item=>item.owner===busy[0].owner));
   assert.equal(find(next.render(),'EPP').props.value,20);assert.equal(next.calls.length,1);
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
 assert.equal(find(tree,'Enable').props.disabled,true);
 assert.equal(find(tree,'Per Game Profile').props.disabled,true);
 assert.equal(find(tree,'Separate AC Profile').props.disabled,true);
 const beforeBlocked=page.calls.length;
 await find(tree,'Per Game Profile').props.onChange(false);
 await find(tree,'Separate AC Profile').props.onChange(false);
 await find(tree,'Enable').props.onChange(false);
 assert.equal(page.calls.length,beforeBlocked,'Handlers guard pending saves even before a disabled button repaints.');
 reportBusy(secondOwner,false);tree=page.render();
 assert.equal(find(tree,'Per Game Profile').props.disabled,false);
 await find(tree,'Per Game Profile').props.onChange(false);await settle();
 assert.equal(page.calls.filter(c=>c.name==='delete_game_profile').length,1);
 page.unmount();
 console.log('Vibration and EPP save on close; CPU edits preserve global/game/battery/AC scope and ignore stale replies.');
})().catch(e=>{console.error(e);process.exitCode=1;});
