const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
function mount(file, component, responses) {
  const slots = [], effects = new Map(), pendingEffects = [];
  const timers = new Map(), calls = []; let cursor=0, nextTimer=0, visible=true;
  const hooks = {
    useState(initial) { const i=cursor++; if (!(i in slots)) slots[i]=initial;
      return [slots[i], value => slots[i]=typeof value==='function' ? value(slots[i]) : value]; },
    useRef(value) { const i=cursor++; return slots[i] ||= {current:value}; },
    useCallback(fn) { cursor++; return fn; },
    useEffect(fn, deps) { const i=cursor++, prev=effects.get(i);
      if (!prev || !deps || deps.some((x,j) => x!==prev.deps[j])) pendingEffects.push(() => {
        prev?.cleanup?.(); effects.set(i,{deps,cleanup:fn()}); }); },
  };
  const api = {useQuickAccessVisible:()=>visible, addEventListener(){}, removeEventListener(){},
    toaster:{toast(){}}, callable:name=>async(...args)=> {
      calls.push({name,args}); return structuredClone(responses[name] || {success:true});
    }};
  const jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props}),Fragment:'Fragment'};
  const mod={exports:{}};
  const code=ts.transpileModule(fs.readFileSync(path.join(root,'src',file),'utf8')+`\nexport {${component} as TestedComponent};`,
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
    render(){cursor=0;const tree=mod.exports.TestedComponent();pendingEffects.splice(0).forEach(fn=>fn());return tree;},
    hide(){visible=false;return this.render();},
    unmount(){for(const e of effects.values())e.cleanup?.();effects.clear();},
  };
}
function find(node,label){
  if(!node||typeof node!=='object')return;
  if(node.props?.label===label)return node;
  for(const child of Array.isArray(node)?node:[node.props?.children]){const match=find(child,label);if(match)return match;}
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
 console.log('Vibration saves on close with profile context; EPP saves on close and hide.');
})().catch(e=>{console.error(e);process.exitCode=1;});
