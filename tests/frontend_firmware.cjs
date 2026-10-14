const complete = require('./frontend_test_guard.cjs')();
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),ts=require(path.join(root,'node_modules/typescript'));
const code=ts.transpileModule(fs.readFileSync(path.join(root,'src/firmware.tsx'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX}
}).outputText;
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const status={reason:'No transfer enabled',catalog_date:'2026-10-12',flash_available:false,
  job:{id:'',phase:'idle',component:'',error:''},components:[{id:'left',label:'Left controller',version:'260310A'}]};
function harness(allowUpdates=true){
  const slots=[],effects=new Map(),pending=[],calls=[],timers=new Map(),logs=[];let cursor=0,visible=true,seq=0,writes=0,clock=0;
  const hooks={
    useRef(v){const i=cursor++;return slots[i] ||= {current:v};},
    useState(v){const i=cursor++;if(!(i in slots))slots[i]=v;return[slots[i],v=>{writes++;slots[i]=typeof v==='function'?v(slots[i]):v;}];},
    useEffect(fn,deps){const i=cursor++,old=effects.get(i);if(!old || deps.some((v,j)=>v!==old.deps[j]))pending.push(()=>{old?.cleanup?.();effects.set(i,{deps,cleanup:fn()});});}
  };
  const module={exports:{}},jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
  vm.runInNewContext(code,{module,exports:module.exports,console:{warn:(...args)=>logs.push(['warn',...args]),info:(...args)=>logs.push(['info',...args])},performance:{now:()=>clock},setTimeout:(fn,ms)=>{const id=++seq;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
    require(name){if(name==='react')return hooks;if(name==='react/jsx-runtime')return jsx;
      if(name==='@decky/ui')return new Proxy({},{get:(_,key)=>key});
      if(name==='@decky/api')return{callable:name=>(...args)=>new Promise((resolve,reject)=>calls.push({name,args,resolve,reject,done:false}))};throw Error(name);}
  });
  function render(){cursor=0;const tree=module.exports.FirmwarePanel({visible,allowUpdates});pending.splice(0).forEach(fn=>fn());return tree;}
  function respond(name,value,fail=false){const c=calls.find(c=>c.name===name&&!c.done);assert.ok(c);c.done=true;fail?c.reject(value):c.resolve(value);}
  function nodes(tree){return !tree || typeof tree!=='object'?[]:[tree,...[tree.props?.children].flat(Infinity).flatMap(nodes)];}
  return {calls,timers,logs,render,respond,get writes(){return writes;},button(label='Check updates'){return nodes(render()).find(n=>n.type==='ButtonItem' && String(n.props.children).includes(label));},
    hide(){visible=false;render();},show(){visible=true;render();},unmount(){effects.forEach(e=>e.cleanup?.());},
    advance(ms){clock+=ms;},expire(){clock+=8000;const [id,e]=[...timers].find(([,e])=>e.ms===8000);timers.delete(id);e.fn();},
    fire(){const [id,e]=[...timers].find(([,e])=>e.ms<8000);timers.delete(id);e.fn();}};
}
(async()=>{
  const recovered=harness();recovered.render();recovered.respond('firmware_get_status',new Error('Unavailable'),true);await settle();
  recovered.fire();recovered.advance(9000);recovered.respond('firmware_get_status',status);await settle();
  assert.equal(recovered.button().props.disabled,true);
  assert.equal(recovered.logs.filter(row=>row[0]==='info').length,0,'A stale response cannot announce status recovery.');
  recovered.fire();recovered.respond('firmware_get_status',status);await settle();
  assert.equal(recovered.logs.filter(row=>row[0]==='info').length,1,'Only an accepted fresh response announces recovery.');recovered.unmount();
  let blocked=harness(false);blocked.render();blocked.respond('firmware_get_status',status);await settle();
  assert.equal(blocked.button().props.disabled,true);await blocked.button().props.onClick();
  assert.equal(blocked.calls.filter(c=>c.name==='firmware_check_updates').length,0,'paused controls can observe but cannot update');blocked.unmount();
  let h=harness();h.render();assert.equal(h.calls.length,1);h.respond('firmware_get_status',status);await settle();
  const button=h.button();const start=button.props.onClick();button.props.onClick();await settle();
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,1,'double activation coalesced');
  h.respond('firmware_check_updates',{...status,job:{id:'one',phase:'downloading',component:'left',error:''}});await settle();await start;
  assert.equal(h.button().props.disabled,true);h.hide();assert.equal(h.timers.size,0,'hidden panel has no polling');
  assert.equal(h.calls.filter(c=>/cancel|stop|flash|start_update/.test(c.name)).length,0,'hiding cannot cancel or flash');h.unmount();

  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  const pending=h.button().props.onClick();await settle();assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,0);
  h.respond('firmware_get_status',{...status,reason:'old reply'});await settle();assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,1);
  h.respond('firmware_check_updates',{...status,job:{id:'two',phase:'up_to_date',component:'left',error:''}});await settle();await pending;
  h.unmount();const before=h.writes;await settle();assert.equal(h.writes,before);

  h=harness();h.render();h.hide();const writes=h.writes;h.respond('firmware_get_status',status);await settle();
  assert.equal(h.writes,writes,'late hidden read ignored');assert.equal(h.timers.size,0);h.unmount();

  h=harness();h.render();assert.equal(h.button().props.disabled,true,'no admission before status');
  h.respond('firmware_get_status',status);await settle();
  const check=h.button();check.props.onClick();check.props.onClick();h.button().props.onClick();await settle();
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,1,'catalog double activation coalesced');

  h.respond('firmware_check_updates',{...status,job:{id:'catalog',phase:'checking',component:'',error:''}});await settle();
  assert.equal(h.button().props.disabled,true);
  h.hide();assert.equal(h.timers.size,0);h.unmount();
  assert.equal(h.calls.filter(c=>/prepare|catalog|cancel|stop/.test(c.name)).length,0,'only unified update action is exposed');
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  h.respond('firmware_get_status',new Error('Backend connection lost'),true);await settle();
  assert.equal(h.button().props.disabled,true,'failed status read must invalidate old idle state');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();
  const staleHandler=h.button().props.onClick;h.fire();h.expire();await settle();
  assert.equal(h.button().props.disabled,true,'status expires even during a stalled read');await staleHandler();
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,0);h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  const failedRead=h.button().props.onClick();h.respond('firmware_get_status',new Error('Read failed'),true);await settle();await failedRead;
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,0,'failed pending read cannot admit a write');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();h.fire();
  const activeRead=h.button().props.onClick();h.respond('firmware_get_status',{...status,job:{phase:'installing'} });await settle();await activeRead;
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,0,'adopt active job instead of sending another check');h.unmount();
  h=harness();h.render();h.advance(9000);h.respond('firmware_get_status',status);await settle();
  assert.equal(h.button().props.disabled,true,'response latency counts toward status freshness');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',{job:{phase:'unrecognised'}});await settle();
  assert.equal(h.button().props.disabled,true,'unknown phase cannot enable update');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',{job:{phase:'idle',message:{malformed:true}}});await settle();
  assert.equal(h.button().props.disabled,true,'malformed status text cannot enable update or break the panel');h.unmount();
  h=harness();h.render();h.respond('firmware_get_status',status);await settle();const unknownResult=h.button().props.onClick();await settle();
  h.respond('firmware_check_updates',new Error('Lost result'),true);await settle();await unknownResult;
  assert.equal(h.button().props.disabled,true,'lost write result requires fresh status, no replay');
  assert.equal(h.calls.filter(c=>c.name==='firmware_check_updates').length,1);h.unmount();
  console.log('Firmware frontend: shared status, double-click exclusion, read/write order, hidden polling and no flash/cancel paths passed.');
})().then(complete).catch(error=>{console.error(error);process.exitCode=1;});
