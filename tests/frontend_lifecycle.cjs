const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
function mount(file, component, responses, initialProps = {}) {
  const slots = [], effects = new Map(), pendingEffects = [];
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
  const api = {useQuickAccessVisible:()=>visible, addEventListener(){}, removeEventListener(){},
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
      if(name==='@decky/ui')return new Proxy({Router:{MainRunningApp:{appid:111}},findModuleExport:()=>undefined},
        {get:(o,k)=>k in o?o[k]:k});
      throw Error(name);
    }});
  return {calls,timers,
    startWatcher(){mod.exports.AppWatcher.start();},
    render(nextProps){if(nextProps)props=nextProps;cursor=0;const tree=mod.exports.TestedComponent(props);pendingEffects.splice(0).forEach(fn=>fn());return tree;},
    hide(){visible=false;return this.render();},
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
(async()=>{
 const values={level:2,mode:1,touchpadIntensity:2,touchpadEnabled:true};
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
