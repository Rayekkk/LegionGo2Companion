const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const source = ts.transpileModule(fs.readFileSync(path.join(root, 'src/battery.tsx'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const base = (changes = {}) => ({ success: true, supported: true, managed: false,
  enabled: false, requested_enabled: null, capacity: 98, charging_status: 'Not charging',
  backend: 'charge_types', current_mode: 'Fast', baseline: null, options: ['Fast', 'Standard', 'Long_Life'],
  error: '', reason: '', recovery_pending: false, ...changes });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve=yes; reject=no; });
  return { promise, resolve, reject }; };
const settle = async () => { for (let i=0; i<30; i++) await Promise.resolve(); };
function harness(initial=base()) {
  const slots=[], effects=new Map(), scheduled=[], timers=new Map(), calls=[];
  let cursor=0, visible=true, timerId=0, response=initial, tree;
  const methods={battery_get_status:()=>response};
  const same=(a,b)=>a && a.length===b.length && b.every((v,i)=>Object.is(v,a[i]));
  const hooks={
    useState(value) { const i=cursor++; if(!(i in slots))slots[i]=typeof value==='function'?value():value;
      return [slots[i], v=>slots[i]=typeof v==='function'?v(slots[i]):v]; },
    useRef(value) { const i=cursor++; if(!(i in slots))slots[i]={current:value}; return slots[i]; },
    useCallback(fn,deps) {const i=cursor++;if(!slots[i]||!same(slots[i].deps,deps))slots[i]={fn,deps};return slots[i].fn;},
    useEffect(fn,deps) {const i=cursor++,old=effects.get(i);if(!old||!same(old.deps,deps))scheduled.push(()=>{
      old?.cleanup?.();effects.set(i,{deps,cleanup:fn()});});},
  };
  const mod={exports:{}}, jsx={jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
  vm.runInNewContext(source,{module:mod,exports:mod.exports,console,
    setInterval:fn=>{timers.set(++timerId,fn);return timerId;},clearInterval:id=>timers.delete(id),
    require:name=>{
      if(name==='react')return hooks;if(name==='react/jsx-runtime')return jsx;
      if(name==='@decky/ui')return new Proxy({},{get:(_,k)=>k});
      if(name==='@decky/api')return {useQuickAccessVisible:()=>visible,callable:name=>(...args)=>{
        calls.push({name,args});assert.ok(methods[name],`Unexpected battery RPC: ${name}`);
        return Promise.resolve().then(()=>methods[name](...args));}};
      throw Error(name);
    },
  });
  function render(){cursor=0;tree=mod.exports.BatteryPage();scheduled.splice(0).forEach(f=>f());return tree;}
  function unmount(){for(const effect of effects.values())effect.cleanup?.();effects.clear();}
  return {methods,calls,timers,render,unmount,summary:mod.exports.batterySummary,
    setResponse:value=>{response=value;},setVisible:value=>{visible=value;return render();},
    get tree(){return tree;},tick(){for(const fn of [...timers.values()])fn();},
    async ready(){render();await settle();return render();}};
}
function walk(node,out=[]) {if(!node||typeof node!=='object')return out;
  if(Array.isArray(node)){node.forEach(n=>walk(n,out));return out;}
  out.push(node);walk(node.props?.children,out);return out;}
function content(node) {if(node==null||node===false)return '';if(typeof node!=='object')return String(node);
  if(Array.isArray(node))return node.map(content).join(' ');
  return [node.props?.title,node.props?.label,node.props?.description,node.props?.children].map(content).join(' ');}
const toggle=h=>walk(h.tree).find(n=>n.type==='ToggleField');
const button=(h,label)=>walk(h.tree).find(n=>n.type==='ButtonItem'&&content(n).includes(label));
const count=(h,name)=>h.calls.filter(c=>c.name===name).length;

(async()=>{
  // Opening a page only reads the actual firmware state; it never adopts a setting.
  let h=harness();await h.ready();
  assert.equal(toggle(h).props.checked,false);assert.equal(toggle(h).props.disabled,false);
  assert.match(content(h.tree),/Companion control: released/);
  assert.match(content(h.tree),/Battery: 98%/);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].name,'battery_get_status');h.unmount();

  // A commit failure must retain the restored state, without a Saved confirmation.
  h=harness();await h.ready();
  h.methods.battery_set_enabled=enabled=>{assert.equal(enabled,true);return {
    success:false,error:'Settings commit failed; previous settings restored.',status:base(),};};
  toggle(h).props.onChange(true);await settle();h.render();
  assert.equal(toggle(h).props.checked,false);assert.equal(toggle(h).props.disabled,false);
  assert.match(content(h.tree),/Settings commit failed/);
  assert.doesNotMatch(content(h.tree),/Preference confirmed|preference saved/);
  // Retrying remains possible after a recoverable operation error.
  const committed=base({managed:true,enabled:true,requested_enabled:true,current_mode:'Long_Life',baseline:'Fast'});
  h.methods.battery_set_enabled=()=>({success:true,status:committed});
  toggle(h).props.onChange(true);await settle();h.render();
  assert.equal(toggle(h).props.checked,true);assert.match(content(h.tree),/Preference confirmed/);
  assert.equal(count(h,'battery_set_enabled'),2);h.unmount();

  // An interrupted first takeover is recoverable even though managed is still false.
  const interrupted=base({success:false,enabled:true,current_mode:'Long_Life',recovery_pending:true,
    error:'Hardware rollback failed; recovery remains pending.'});
  h=harness(interrupted);await h.ready();
  assert.equal(toggle(h).props.disabled,true);assert.match(content(h.tree),/Recovery pending/);
  assert.match(h.summary(interrupted),/needs recovery/);
  assert.match(content(h.tree),/Current protection: On/);
  h.methods.battery_release_control=()=>({success:true,status:base()});
  button(h,'Recover and Release Control').props.onClick();await settle();h.render();
  assert.equal(count(h,'battery_release_control'),1);assert.equal(toggle(h).props.disabled,false);
  assert.doesNotMatch(content(h.tree),/Recovery pending/);h.unmount();

  // Failed recovery remains actionable, while unknown actual values are never Off.
  h=harness(interrupted);await h.ready();
  h.methods.battery_release_control=()=>({success:false,error:'Firmware is unavailable.',status:interrupted});
  button(h,'Recover and Release Control').props.onClick();await settle();h.render();
  assert.match(content(h.tree),/Firmware is unavailable/);assert.ok(button(h,'Recover and Release Control'));
  h.unmount();h=harness(base({enabled:null,capacity:null,current_mode:null}));await h.ready();
  assert.match(content(h.tree),/Current protection: Unknown/);assert.match(content(h.tree),/Battery: Unknown/);
  assert.doesNotMatch(content(h.tree),/Current protection: Off|Battery: 0%/);h.unmount();

  // Requested and actual state remain separate when firmware drift is detected.
  h=harness(base({managed:true,enabled:false,requested_enabled:true,baseline:'Fast'}));await h.ready();
  assert.equal(toggle(h).props.checked,true);assert.match(content(h.tree),/Current protection: Off/);
  assert.match(content(h.tree),/Saved preference: On/);assert.match(content(h.tree),/does not match/);
  assert.match(h.summary(base({managed:true,requested_enabled:true})),/saved choice pending/);h.unmount();

  // A stale status response cannot replace a mutation result; rapid clicks serialize.
  h=harness();await h.ready();const oldRead=deferred(),mutation=deferred();
  h.methods.battery_get_status=()=>oldRead.promise;h.tick();await settle();
  h.methods.battery_set_enabled=()=>mutation.promise;
  toggle(h).props.onChange(true);toggle(h).props.onChange(false);await settle();
  assert.equal(count(h,'battery_set_enabled'),0,'Earlier read must finish before writing');
  oldRead.resolve(base());await settle();assert.equal(count(h,'battery_set_enabled'),1);
  mutation.resolve({success:true,status:committed});await settle();h.render();
  assert.equal(toggle(h).props.checked,true);assert.match(content(h.tree),/Current protection: On/);h.unmount();

  // Hidden/unmounted pages have no polling, and a new visible lease reads again.
  h=harness();await h.ready();const initialReads=count(h,'battery_get_status');
  h.setVisible(false);assert.equal(h.timers.size,0);h.tick();await settle();
  assert.equal(count(h,'battery_get_status'),initialReads);
  h.setVisible(true);await settle();h.render();assert.equal(count(h,'battery_get_status'),initialReads+1);
  h.unmount();assert.equal(h.timers.size,0);
  console.log('Battery UI keeps actual/saved states distinct, reports rollback/recovery, prevents stale saves, and stops hidden polling.');
})().catch(error=>{console.error(error);process.exitCode=1;});
