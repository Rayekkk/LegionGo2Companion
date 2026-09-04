const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const slots = [], effects = new Map(), pending = [], events = new Map(), watchers = [];
let cursor = 0, response = { version: 'test', blocked: true, standalone_plugins: ['LeGoTDP'] };
const hooks = {
  useState(initial) { const i=cursor++; if (!(i in slots)) slots[i]=initial;
    return [slots[i], v=>slots[i]=typeof v==='function'?v(slots[i]):v]; },
  useCallback(fn) { cursor++; return fn; },
  useEffect(fn,deps) { const i=cursor++,old=effects.get(i);
    if(!old||deps.some((v,j)=>v!==old.deps[j]))pending.push(()=>{old?.cleanup?.();effects.set(i,{deps,cleanup:fn()});}); },
};
const mod={exports:{}},jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
const source=fs.readFileSync(path.join(root,'src/index.tsx'),'utf8')+'\nexport {Content as TestContent};';
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,
  target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
  module:mod,exports:mod.exports,console,setInterval:()=>1,clearInterval(){},
  require:name=>{
    if(name==='react')return hooks;if(name==='react/jsx-runtime')return jsx;
    if(name==='@decky/api')return {definePlugin:fn=>fn,useQuickAccessVisible:()=>true,
      callable:name=>async()=>{assert.equal(name,'get_version','blocked view only calls the guard');return response;},
      addEventListener:(name,fn)=>{events.set(name,fn);return fn;},removeEventListener:name=>events.delete(name)};
    if(name==='@decky/ui')return new Proxy({staticClasses:{}},{get:(o,k)=>o[k]||k});
    if(name.startsWith('./'))return new Proxy({},{get:(_,k)=>/^(start|stop)/.test(k)?()=>watchers.push(k):k});
    throw Error(name);
  }
});
const settle=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
function render(){cursor=0;const t=mod.exports.TestContent();pending.splice(0).forEach(f=>f());return t;}
function text(node){if(!node)return '';if(typeof node!=='object')return String(node);
 return [node.props?.title,node.props?.label,node.props?.description,...(Array.isArray(node)?node:[node.props?.children])].map(text).join(' ');}
function reset(){for(const e of effects.values())e.cleanup?.();effects.clear();slots.length=0;pending.length=0;}
(async()=>{
 let plugin=mod.exports.default();await settle();let tree=render();await settle();tree=render();
 assert.match(text(tree),/Companion paused/);assert.match(text(tree),/LeGoTDP/);
 assert.match(text(tree),/uninstall/);assert.match(text(tree),/restart Decky/);assert.deepEqual(watchers,[]);
 response={version:'test',blocked:true,standalone_plugins:[],restart_required:true};
 events.get('companion_guard')(response);tree=render();assert.match(text(tree),/Ready for a restart/);
 events.get('companion_guard')({version:'old reply',blocked:false});assert.match(text(render()),/Ready for a restart/);
 plugin.onDismount();reset();watchers.length=0;
 response={version:'test',blocked:false,standalone_plugins:[]};plugin=mod.exports.default();await settle();
 tree=render();await settle();tree=render();assert.equal(tree.type.name,'Controls');
 assert.deepEqual(watchers,['startTdpWatcher','startVibrationWatcher']);
 events.get('companion_guard')({version:'test',blocked:true,standalone_plugins:['LeGo Vibe Control']});
 tree=render();assert.match(text(tree),/LeGo Vibe Control/);assert.notEqual(tree.type.name,'Controls');
 assert.deepEqual(watchers.slice(-2),['stopTdpWatcher','stopVibrationWatcher']);
 plugin.onDismount();reset();assert.equal(events.size,0);
 console.log('Guard hides every hardware page, stops reports, explains conflicts and requires a fresh start.');
})().catch(e=>{console.error(e);process.exitCode=1;});
