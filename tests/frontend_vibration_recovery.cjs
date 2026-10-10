// Execute the actual TSX with controlled RPC completion and browser timers.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const values = { level: 2, mode: 1, touchpadIntensity: 2, touchpadEnabled: true };
const settings = (app = '0', level = 2, overwrite = false) => ({ settings: { ...values, level }, app_id: app, profile_id: overwrite ? app : '0', overwrite });
const base = {
  vibe_is_ready: { ready: true, error: '' }, vibe_get_settings: settings(),
  vibe_get_driver_status: { found: true, paths: ['fixture'] },
  vibe_get_capabilities: { mode: ['fps', 'racing'] },
};
function mount(responses = {}, appid = 0) {
  const slots = [], effects = new Map(), pendingEffects = [], timers = new Map(), calls = [], toasts = [];
  const callbacks = {}, router = { MainRunningApp: appid ? { appid, display_name: `Game ${appid}` } : null };
  let cursor = 0, nextTimer = 0, visible = true, now = 100000, writes = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => value === b[i]);
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++, previous = effects.get(i); if (!previous || !same(previous.deps, deps)) pendingEffects.push(() => {
      previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
    }); },
  };
  const api = {
    useQuickAccessVisible: () => visible, toaster: { toast: item => toasts.push(item) },
    addEventListener: (name, callback) => { callbacks[name] = callback; },
    removeEventListener: (name, callback) => { if (callbacks[name] === callback) delete callbacks[name]; },
    callable: name => async (...args) => {
      calls.push({ name, args }); const response = { ...base, ...responses }[name];
      // Preserve explicit false replies; a falsy fixture is not a successful RPC.
      return typeof response === 'function' ? await response(...args) : structuredClone(response ?? { success: true });
    },
  };
  const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: 'Fragment' };
  const mod = { exports: {} }, source = fs.readFileSync(path.join(root, 'src/vibration.tsx'), 'utf8') + '\nexport { LGoVibeControl as Tested, AppWatcher };';
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    module: mod, exports: mod.exports, console, Error,
    Date: class extends Date { static now() { return now; } },
    window: { SleepManager: { RegisterForNotifyResumeFromSuspend() { return () => {}; } } },
    setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === '@decky/api') return api;
      if (name === '@decky/ui') return new Proxy({ Router: router, findModuleExport() {} }, { get: (o, key) => key in o ? o[key] : key });
      throw Error(name);
    },
  });
  return {
    calls, timers, toasts, responses, getOverviewSettings:()=>mod.exports.getVibrationSettings(),
    get writes() { return writes; },
    render() { cursor = 0; const tree = mod.exports.Tested(); pendingEffects.splice(0).forEach(fn => fn()); return tree; },
    hide() { visible = false; return this.render(); }, show() { visible = true; return this.render(); },
    flush() { for (const [id, timer] of [...timers]) if (timer.kind === 'timeout') { timers.delete(id); timer.fn(); } },
    startWatcher() { mod.exports.AppWatcher.start(); },
    async game(app) { router.MainRunningApp = app ? { appid: app, display_name: `Game ${app}` } : null; await mod.exports.AppWatcher.check(); },
    async heartbeat() { now += 6001; await mod.exports.AppWatcher.check(); },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); mod.exports.AppWatcher.stop(); },
    remount() { slots.length=0; pendingEffects.length=0; return this.render(); },
  };
}
function find(node, label, type) {
  if (!node || typeof node !== 'object') return;
  if (type === 'ButtonItem' ? node.type === type && node.props?.children === label : node.props?.label === label) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) { const match = find(child, label, type); if (match) return match; }
}
const button = (tree, label) => find(tree, label, 'ButtonItem');
const count = (h, name) => h.calls.filter(call => call.name === name).length;
async function ready(h) { h.render(); await settle(); return h.render(); }
(async () => {
  {
    const pending=deferred(),h=mount({vibe_set_intensity:()=>pending.promise});
    find(await ready(h),'Intensity').props.onChange(3);h.unmount();await settle();
    const overview=h.getOverviewSettings();await settle();
    assert.equal(count(h,'vibe_get_settings'),1,'The main-menu vibration getter follows saves from the closed page.');
    h.responses.vibe_get_settings=settings('0',3);pending.resolve({...settings('0',3),success:true});
    const current=await overview;assert.equal(current.settings.level,3);assert.equal(count(h,'vibe_get_settings'),2);
  }

  for (const failed of [false,true]) {
    const first=deferred(),second=deferred();
    const h=mount({vibe_set_intensity:()=>first.promise,vibe_set_rumble_mode:()=>second.promise});
    let tree=await ready(h);find(tree,'Intensity').props.onChange(3);find(tree,'Mode').props.onChange(0);
    h.unmount();await settle();h.remount();await settle();
    assert.equal(count(h,'vibe_get_settings'),1,'A reopened vibration page waits for all accepted slider saves.');
    first.resolve({...settings('0',3),success:true});await settle();
    assert.equal(count(h,'vibe_set_rumble_mode'),1);
    assert.equal(count(h,'vibe_get_settings'),1,'The second queued save still precedes the initial snapshot.');
    h.responses.vibe_get_settings={...settings('0',3),settings:{...values,level:3,mode:failed?1:0}};
    if(failed)second.reject(new Error('mode change interrupted'));
    else second.resolve({...h.responses.vibe_get_settings,success:true});
    await settle();tree=h.render();
    assert.equal(count(h,'vibe_get_settings'),2,'A settled mutation tail immediately admits the new snapshot, including after failure.');
    assert.equal(find(tree,'Intensity').props.value,3);assert.equal(find(tree,'Mode').props.value,failed?1:0);
    h.unmount();
  }

  for (const pendingName of ['vibe_get_driver_status','vibe_get_capabilities']) {
    const delayed=deferred();let failed=true;
    const h=mount({vibe_get_settings:()=>{if(failed)throw Error('temporary settings failure');return settings('0',3);},
      [pendingName]:()=>delayed.promise});
    let tree=await ready(h);
    assert.ok(button(tree,'Retry settings'),'An initial rejection is visible even while another read is pending.');
    failed=false;button(tree,'Retry settings').props.onClick();h.render();await settle();
    assert.equal(count(h,'vibe_get_settings'),1,'Retry waits for the previous initial snapshot to settle.');
    assert.equal(count(h,pendingName),1,'A retry cannot duplicate an unfinished hardware read.');
    h.responses[pendingName]=base[pendingName];delayed.resolve(base[pendingName]);await settle();tree=h.render();
    assert.equal(count(h,'vibe_get_settings'),2);
    assert.equal(find(tree,'Intensity').props.value,3,'The requested retry runs as soon as the preceding attempt finishes.');
    h.unmount();
  }

  for (const metadata of [false,true]) {
    const saved={...values,level:1,mode:0};
    const h=mount({vibe_reset_to_default:{success:false,settings:saved,error:'hardware disconnected',
      ...(metadata?{app_id:'0',profile_id:'0',overwrite:false}:{})}});
    await button(await ready(h),'Reset to defaults').props.onClick();await settle();
    assert.equal(find(h.render(),'Intensity').props.value,1,'Failed hardware apply still displays the settings committed by Reset.');
    assert.equal(find(h.render(),'Mode').props.value,0);
    assert.ok(h.toasts.some(t=>t.title==='Reset failed'),'The hardware failure remains visible.');h.unmount();
  }
  for (const failed of [false,true]) {
    const playing=deferred(),h=mount({vibe_test_vibration:()=>playing.promise});
    const click=button(await ready(h),'Test Vibration (0.5s)').props.onClick;
    click();click();await settle();
    assert.equal(count(h,'vibe_test_vibration'),1,'Repeated taps start one vibration test.');
    h.unmount();const writes=h.writes;
    if(failed)playing.reject(new Error('test disconnected'));else playing.resolve({success:true});
    await settle();assert.equal(h.writes,writes,'A completed vibration test cannot update an unmounted page.');
    click();await settle();assert.equal(count(h,'vibe_test_vibration'),1);
  }
  for (const leave of [false,true]) {
    const writing=deferred(),h=mount({vibe_set_intensity:()=>writing.promise});
    await ready(h);find(h.render(),'Intensity').props.onChange(3);
    button(h.render(),'Test Vibration (0.5s)').props.onClick();await settle();
    assert.equal(count(h,'vibe_set_intensity'),1,'Testing flushes the latest slider gesture.');
    assert.equal(count(h,'vibe_test_vibration'),0,'The test waits for the current settings to finish saving.');
    if(leave)h.unmount();
    writing.resolve({...settings('0',3),success:true});await settle();
    assert.equal(count(h,'vibe_test_vibration'),leave?0:1,'A queued test is cancelled when its page closes.');
    h.unmount();
  }
  {
    const h=mount();const tree=await ready(h);
    const gestures=[()=>find(tree,'Intensity').props.onChange(3),
      ()=>find(tree,'Per Game Profile').props.onChange(true),()=>button(tree,'Reset to defaults').props.onClick()];
    h.unmount();const writes=h.writes,calls=h.calls.length;
    gestures.forEach(run=>run());h.flush();await settle();
    assert.equal(h.writes,writes,'Closed vibration controls cannot create new state changes.');
    assert.equal(h.calls.length,calls,'Closed vibration controls cannot start new writes.');
  }
  {
    const writing=deferred(),heartbeat=deferred();let stored=settings('111',1);
    const h=mount({vibe_get_settings:()=>structuredClone(stored),
      vibe_set_active_app:()=>({...stored,success:true,changed:false}),vibe_set_intensity:()=>writing.promise},111);
    await ready(h);h.startWatcher();await settle();
    find(h.render(),'Intensity').props.onChange(3);h.flush();await settle();
    h.responses.vibe_set_active_app=()=>heartbeat.promise;const checking=h.heartbeat();await settle();
    stored=settings('111',3);writing.resolve({...stored,success:true});await settle();
    heartbeat.resolve({...settings('111',1),success:true,changed:false});await checking;await settle();
    assert.equal(find(h.render(),'Intensity').props.value,3,'heartbeat started during a write cannot undo its completion');h.unmount();
  }
  {
    const old=deferred();const h=mount({vibe_get_settings:settings('111'),
      vibe_set_active_app:id=>({...settings(id,2,id!=='0'),success:true,changed:true})},111);
    await ready(h);h.startWatcher();await settle();
    h.responses.vibe_set_active_app=id=>id==='111'?old.promise:({...settings(id,3,true),success:true,changed:true});
    const checking=h.heartbeat();await settle();await h.game(222);
    old.resolve({...settings('111'),success:true,changed:false});await checking;await settle();
    assert.equal(find(h.render(),'Intensity').props.value,3,'new game is delivered immediately after a busy old heartbeat');
    assert.equal(find(h.render(),'Per Game Profile').props.checked,true);
    find(h.render(),'Intensity').props.onChange(1);h.flush();await settle();
    assert.equal(h.calls.find(c=>c.name==='vibe_set_intensity').args[1],'222');h.unmount();
  }
  for (const action of ['toggle', 'slider']) {
    const oldHeartbeat = deferred(); let stored = settings('111', 1, false);
    const h = mount({
      vibe_get_settings: () => structuredClone(stored),
      vibe_set_active_app: () => ({success:true,changed:false,...structuredClone(stored)}),
      vibe_set_profile_overwrite: (_app, enabled) => { stored = settings('111', 2, enabled); return {success:true,...structuredClone(stored)}; },
      vibe_set_intensity: level => { stored = {...stored,settings:{...stored.settings,level}}; return {success:true,...structuredClone(stored)}; },
    }, 111);
    await ready(h); h.startWatcher(); await settle();
    const captured = structuredClone(stored); h.responses.vibe_set_active_app = () => oldHeartbeat.promise;
    const heartbeat = h.heartbeat(); await settle();
    if (action === 'toggle') await find(h.render(), 'Per Game Profile').props.onChange(true);
    else { find(h.render(), 'Intensity').props.onChange(3); h.flush(); }
    await settle(); oldHeartbeat.resolve({success:true,changed:false,...captured}); await heartbeat; await settle();
    assert.equal(find(h.render(), 'Per Game Profile').props.checked, action === 'toggle', 'delayed heartbeat cannot undo committed profile scope');
    assert.equal(find(h.render(), 'Intensity').props.value, action === 'toggle' ? 2 : 3, 'delayed heartbeat cannot undo a committed slider');
    find(h.render(), 'Intensity').props.onChange(0); h.flush(); await settle();
    assert.equal(h.calls.filter(c=>c.name==='vibe_set_intensity').at(-1).args[2], action === 'toggle' ? '111' : '0');
    h.unmount();
  }
  // A reset is ordered after every deferred gesture, so no old timer can undo it.
  let stored = { ...values };
  const reset = mount({
    vibe_set_intensity: level => { stored.level = level; return { success: true, settings: { ...stored } }; },
    vibe_set_rumble_mode: mode => { stored.mode = mode; return { success: true, settings: { ...stored } }; },
    vibe_reset_to_default: () => { stored = { ...values }; return { success: true, settings: { ...stored } }; },
  });
  let tree = await ready(reset);
  find(tree, 'Intensity').props.onChange(3); find(tree, 'Mode').props.onChange(0); tree = reset.render();
  const alreadyQueued = [...reset.timers.values()].filter(t => t.kind === 'timeout').map(t => t.fn);
  await button(tree, 'Reset to defaults').props.onClick(); await settle(); tree = reset.render();
  assert.equal([...reset.timers.values()].filter(t => t.kind === 'timeout').length, 0);
  assert.deepEqual(reset.calls.filter(call => /^vibe_(set_intensity|set_rumble_mode|reset_to_default)$/.test(call.name)).map(call => call.name),
    ['vibe_set_intensity', 'vibe_set_rumble_mode', 'vibe_reset_to_default']);
  assert.deepEqual(Array.from(reset.calls.find(call => call.name === 'vibe_reset_to_default').args), ['0', '0']);
  assert.equal(stored.level, 2); assert.equal(stored.mode, 1); assert.equal(find(tree, 'Intensity').props.value, 2);
  reset.flush(); alreadyQueued.forEach(fn => fn()); await settle(); assert.equal(stored.level, 2);
  assert.equal(count(reset, 'vibe_set_intensity'), 1, 'A cleared callback cannot replay a gesture after Reset'); reset.unmount();

  const coalesced = mount({ vibe_set_intensity: level => ({ success: true, settings: { ...values, level } }) });
  tree = await ready(coalesced); find(tree, 'Intensity').props.onChange(1);
  const superseded = [...coalesced.timers.values()].find(t => t.kind === 'timeout').fn;
  tree = coalesced.render(); find(tree, 'Intensity').props.onChange(3); superseded(); coalesced.flush(); await settle();
  assert.equal(count(coalesced, 'vibe_set_intensity'), 1); assert.equal(coalesced.calls.find(call => call.name === 'vibe_set_intensity').args[0], 3); coalesced.unmount();

  // A reset also waits for a write already in flight, and duplicate activation coalesces.
  const writing = deferred(), order = [];
  const serial = mount({ vibe_set_intensity: () => { order.push('write'); return writing.promise; },
    vibe_reset_to_default: () => { order.push('reset'); return { success: true, settings: values }; } });
  tree = await ready(serial); const resetCallback = button(tree, 'Reset to defaults').props.onClick;
  find(tree, 'Intensity').props.onChange(3); serial.flush(); await settle();
  const one = resetCallback(), two = resetCallback(); await settle(); assert.deepEqual(order, ['write']);
  writing.resolve({ success: true, settings: { ...values, level: 3 } }); await one; await two; await settle();
  assert.deepEqual(order, ['write', 'reset']); assert.equal(find(serial.render(), 'Intensity').props.value, 2); serial.unmount();

  // Success, rejection payloads and thrown errors from old games cannot change the new page.
  for (const action of ['toggle', 'reset']) for (const result of ['success', 'failure', 'throw']) {
    const delayed = deferred();
    const h = mount({ vibe_get_settings: settings('111', 1, false),
      vibe_set_active_app: app => ({ success: true, changed: true, ...settings(app, app === '222' ? 0 : 1, app === '222') }),
      vibe_set_profile_overwrite: () => delayed.promise, vibe_reset_to_default: () => delayed.promise,
    }, 111);
    tree = await ready(h); h.startWatcher(); await settle(); tree = h.render();
    const pending = action === 'toggle' ? find(tree, 'Per Game Profile').props.onChange(true) : button(tree, 'Reset to defaults').props.onClick();
    await settle(); await h.game(222); tree = h.render();
    assert.equal(find(tree, 'Intensity').props.value, 0); assert.equal(find(tree, 'Per Game Profile').props.checked, true);
    if (result === 'throw') delayed.reject(Error('old game failed'));
    else delayed.resolve({ success: result === 'success', error: 'old game failed', settings: { ...values, level: 3 }, overwrite: true });
    await pending; await settle(); tree = h.render();
    assert.equal(find(tree, 'Intensity').props.value, 0, `${action}/${result} keeps the new game's values`);
    assert.equal(find(tree, 'Per Game Profile').props.checked, true, `${action}/${result} keeps the new game's profile enabled`);
    assert.equal(h.toasts.length, 0, `${action}/${result} does not report an old result on the new game`);
    if (action === 'reset') assert.deepEqual(Array.from(h.calls.find(call => call.name === 'vibe_reset_to_default').args), ['111', '0']);
    h.unmount();
  }

  // A first RPC failure hides unconfirmed defaults and supports an explicit retry.
  for (const failed of ['vibe_is_ready', 'vibe_get_settings', 'vibe_get_driver_status']) {
    let reject = true;
    const replies = { vibe_get_settings: settings('0', 0), [failed]: () => {
      if (reject) throw Error(`${failed} disconnected`);
      return failed === 'vibe_get_settings' ? settings('0', 0) : base[failed];
    } };
    const h = mount(replies); tree = await ready(h);
    assert.ok(find(tree, 'Could not load settings')); assert.ok(button(tree, 'Retry settings'));
    assert.equal(find(tree, 'Intensity'), undefined, 'An unread profile never exposes editable defaults');
    assert.ok(JSON.stringify(tree).includes(`${failed} disconnected`));
    h.hide(); h.show(); await settle(); assert.equal(count(h, failed), 1, 'Reopening does not start a duplicate failed read');
    reject = false; const retry = button(tree, 'Retry settings').props.onClick; retry(); retry();
    h.render(); await settle(); tree = h.render();
    assert.equal(count(h, failed), 2, 'Repeated retry activation creates one new batch');
    assert.equal(find(tree, 'Intensity').props.value, 0); assert.equal(button(tree, 'Retry settings'), undefined); h.unmount();
  }

  // Slow initialization and heartbeat snapshots cannot roll back a newer game or draft.
  const caps = deferred();
  const initial = mount({ vibe_get_settings: settings('111', 1), vibe_get_capabilities: () => caps.promise,
    vibe_set_active_app: app => ({ success: true, changed: true, ...settings(app, app === '222' ? 0 : 1, true) }),
  }, 111);
  initial.render(); await settle(); initial.startWatcher(); await settle(); await initial.game(222);
  caps.resolve({ mode: ['fps', 'racing'] }); await settle(); tree = initial.render();
  assert.equal(find(tree, 'Intensity').props.value, 0); initial.unmount();
  const field = deferred();
  const heartbeat = mount({ vibe_get_settings: settings('111', 1, true),
    vibe_set_active_app: app => ({ success: true, changed: false, ...settings(app, 1, true) }), vibe_set_intensity: () => field.promise }, 111);
  tree = await ready(heartbeat); heartbeat.startWatcher(); await settle(); tree = heartbeat.render();
  find(tree, 'Intensity').props.onChange(3); heartbeat.flush(); await settle(); await heartbeat.heartbeat();
  assert.equal(find(heartbeat.render(), 'Intensity').props.value, 3);
  field.resolve({ success: true, settings: { ...values, level: 3 } }); await settle();
  assert.equal(find(heartbeat.render(), 'Intensity').props.value, 3); heartbeat.unmount();

  // Pending gestures survive both temporary hiding and closing the page.
  for (const leave of ['hide', 'unmount']) {
    const h = mount({ vibe_get_settings: settings('111', 1, true), vibe_set_intensity: { success: true, settings: { ...values, level: 3 } } }, 111);
    tree = await ready(h); find(tree, 'Intensity').props.onChange(3); h[leave](); if (leave === 'hide') h.flush(); await settle();
    assert.equal(count(h, 'vibe_set_intensity'), 1); assert.deepEqual(Array.from(h.calls.find(call => call.name === 'vibe_set_intensity').args), [3, '111', '111']);
    if (leave === 'hide') h.unmount();
  }
  // A queued browser gesture cannot obscure a same-game toggle failure.
  const switching = deferred();
  const failedSwitch = mount({ vibe_get_settings: settings('111', 1, false),
    vibe_set_profile_overwrite: () => switching.promise }, 111);
  tree = await ready(failedSwitch);
  const gesture = find(tree, 'Intensity').props.onChange;
  const toggle = find(tree, 'Per Game Profile').props.onChange(true);
  await settle(); tree = failedSwitch.render();
  for (const label of ['Intensity', 'Mode', 'Touchpad vibration', 'Touchpad intensity'])
    assert.equal(find(tree, label).props.disabled, true);
  gesture(3); failedSwitch.flush(); await settle();
  assert.equal(count(failedSwitch, 'vibe_set_intensity'), 0);
  switching.reject(Error('commit failed')); await toggle; await settle(); tree = failedSwitch.render();
  assert.equal(find(tree, 'Per Game Profile').props.checked, false);
  assert.equal(find(tree, 'Intensity').props.value, 1);
  assert.equal(find(tree, 'Intensity').props.disabled, false);
  find(tree, 'Intensity').props.onChange(3); failedSwitch.flush(); await settle();
  assert.deepEqual(Array.from(failedSwitch.calls.find(c => c.name === 'vibe_set_intensity').args), [3, '111', '0']);
  failedSwitch.unmount();
  // A hardware failure after a successful commit must adopt the committed scope.
  const savedSwitch = mount({ vibe_get_settings: settings('111', 1, false),
    vibe_set_profile_overwrite: { success: false, error: 'device unplugged', ...settings('111', 2, true) } }, 111);
  tree = await ready(savedSwitch); await find(tree, 'Per Game Profile').props.onChange(true); await settle(); tree = savedSwitch.render();
  assert.equal(find(tree, 'Per Game Profile').props.checked, true);
  find(tree, 'Intensity').props.onChange(3); savedSwitch.flush(); await settle();
  assert.deepEqual(Array.from(savedSwitch.calls.find(c => c.name === 'vibe_set_intensity').args), [3, '111', '111']);
  savedSwitch.unmount();
  const rejectedField = mount({ vibe_get_settings: settings('111', 1, true),
    vibe_set_intensity: { success: false, error: 'profile changed', ...settings('111', 2, false) } }, 111);
  tree = await ready(rejectedField); find(tree, 'Intensity').props.onChange(3); rejectedField.flush(); await settle();
  tree = rejectedField.render(); assert.equal(find(tree, 'Per Game Profile').props.checked, false);
  find(tree, 'Intensity').props.onChange(1); rejectedField.flush(); await settle();
  assert.deepEqual(Array.from(rejectedField.calls.filter(c => c.name === 'vibe_set_intensity').at(-1).args), [1, '111', '0']);
  rejectedField.unmount();
  // An ambiguous transport failure cannot leave an editor with an unconfirmed scope.
  const unknownSwitch = mount({ vibe_get_settings: settings('111', 1, false),
    vibe_set_profile_overwrite: () => { throw Error('connection lost after commit'); } }, 111);
  tree = await ready(unknownSwitch);
  unknownSwitch.responses.vibe_get_settings = () => { throw Error('resync offline'); };
  await find(tree, 'Per Game Profile').props.onChange(true); await settle(); tree = unknownSwitch.render();
  assert.equal(find(tree, 'Intensity'), undefined); assert.ok(button(tree, 'Retry settings'));
  unknownSwitch.responses.vibe_get_settings = settings('111', 2, true);
  button(tree, 'Retry settings').props.onClick(); unknownSwitch.render(); await settle(); tree = unknownSwitch.render();
  assert.equal(find(tree, 'Per Game Profile').props.checked, true); unknownSwitch.unmount();
  // Presentation must retain native stepped controls and real driver details
  // without issuing hardware mutations when a diagnostic disclosure is opened.
  const endpointPaths = [
    '/sys/devices/pci0000:00/0000:00:08.3/0000:c4:00.0/usb3/3-1/3-1:1.2/0003:17EF:61EB.0002',
    '/sys/devices/pci0000:00/0000:00:08.3/0000:c4:00.0/usb3/3-2/3-2:1.2/0003:17EF:61EB.0003',
  ];
  const presentation = mount({ vibe_get_driver_status: { found: true,
    paths: endpointPaths, method: 'pyudev', ids: '17ef:61eb' } });
  tree = await ready(presentation);
  for (const label of ['Intensity', 'Mode', 'Touchpad intensity']) {
    const slider = find(tree, label);
    assert.equal(slider.type, 'SliderField'); assert.equal(slider.props.step, 1);
    assert.ok(slider.props.description, `${label} retains its current value label`);
  }
  assert.equal(find(tree, 'Controller endpoints'), undefined);
  const beforeDetails = presentation.calls.length;
  button(tree, 'Show driver details').props.onClick(); tree = presentation.render();
  const endpoints = find(tree, 'Controller endpoints');
  assert.equal(endpoints.type, 'Field', 'Endpoint details keep native focus and scrolling');
  const endpointText = endpoints.props.description;
  assert.equal(typeof endpointText === 'string' ? endpointText : endpointText.props?.children,
    endpointPaths.join(' · '), 'Both long endpoint paths remain complete');
  assert.equal(endpointText.props?.style?.overflowWrap, 'anywhere',
    'Unbroken sysfs segments must wrap within the native description width');
  assert.equal(endpointText.props.style.display, 'block');
  assert.equal(endpointText.props.style.maxWidth, '100%');
  assert.equal(presentation.calls.length, beforeDetails, 'Presentation disclosure never touches hardware');
  presentation.hide(); presentation.show(); await settle(); tree = presentation.render();
  assert.ok(find(tree, 'Controller endpoints'), 'QAM visibility does not reset disclosure/navigation');
  button(tree, 'Hide driver details').props.onClick(); tree = presentation.render();
  assert.equal(find(tree, 'Controller endpoints'), undefined);
  assert.ok(find(tree, 'Intensity')); assert.ok(button(tree, 'Test Vibration (0.5s)'));
  presentation.unmount();
  console.log('Vibration recovery: serialized mutations, scoped late replies, retries, stable drafts, save-on-close and failed profile transitions pass.');
})().catch(error => { console.error(error); process.exitCode = 1; });
