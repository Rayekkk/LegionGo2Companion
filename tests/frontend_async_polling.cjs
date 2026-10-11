// Run the actual components with controllable RPC completion order.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
function realExport(file, name, context = {}) {
  const mod = { exports: {} };
  const source = fs.readFileSync(path.join(root, 'src', file), 'utf8');
  const script = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(script, { module: mod, exports: mod.exports, console,
    require: request => request === '@decky/api' ? { callable: () => () => {} } : {}, ...context });
  return mod.exports[name];
}
const tdpModeLabel = realExport('tdp.tsx', 'tdpModeLabel');
const batterySummary = realExport('battery.tsx', 'batterySummary');
function harness(file, component, initiallyVisible = true) {
  const slots = [], effects = new Map(), pending = [], timers = new Map(), calls = [], callbacks = {};
  let cursor = 0, visible = initiallyVisible, nextTimer = 0, writes = 0, props = {}, now = 100000;
  let panelShown = true, intersectionCallback;
  class PanelObserver { constructor(fn) { intersectionCallback = fn; } observe() {} disconnect() { intersectionCallback = null; } }
  const panelElement = { getClientRects: () => panelShown ? [{}] : [], ownerDocument: { defaultView: { IntersectionObserver: PanelObserver } } };
  const fanSummary = realExport('fan.tsx', 'fanSummary', { Date: class extends Date { static now() { return now; } } });
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => v === b[i]);
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useMemo(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { value: fn(), deps }; return slots[i].value; },
    useEffect(fn, deps) { const i = cursor++, previous = effects.get(i); if (!previous || !same(previous.deps, deps)) pending.push(() => {
      previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
    }); },
  };
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  const mod = { exports: {} }, jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const callable = name => (...args) => new Promise((resolve, reject) => calls.push({ name, args, resolve, reject, settled: false }));
  const relativeReads = { getFanState: 'fan_get_state', getVibrationSettings: 'vibe_get_settings', getWifiStatus: 'wifi_get_status', getRgbStatus: 'rgb_get_status',
    getRemapStatus: 'remap_get_status', getBatteryStatus: 'battery_get_status',
    getControllerBatteryLevels: 'battery_get_controller_levels', getControllerStatus: 'controller_get_status' };
  const source = fs.readFileSync(path.join(root, 'src', file), 'utf8') + `\nexport { ${component} as Tested };`;
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    module: mod, exports: mod.exports, console, Date: class extends Date { static now() { return now; } },
    window: { SteamClient: { GameSessions: { RegisterForAppLifetimeNotifications(fn) { callbacks.lifetime = fn; return { unregister() {} }; } } },
      SleepManager: { RegisterForNotifyResumeFromSuspend(fn) { callbacks.resume = fn; return () => {}; } } },
    setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === '@decky/ui') return new Proxy({ Router: { MainRunningApp: { appid: 111, display_name: 'Game' } }, findModuleExport() {} },
        { get: (o, k) => k in o ? o[k] : k });
      if (name === '@decky/api') return { useQuickAccessVisible: () => visible, toaster: { toast() {} },
        definePlugin: fn => fn, addEventListener: (name, fn) => { callbacks[name] = fn; return fn; }, removeEventListener() {}, callable };
      if (name.startsWith('./')) return new Proxy({}, { get: (_object, key) => {
        if (key in relativeReads) return callable(relativeReads[key]);
        if (key === 'tdpModeLabel') return tdpModeLabel;
        if (key === 'batterySummary') return batterySummary;
        if (key === 'fanSummary') return fanSummary;
        if (/^(start|stop)/.test(key)) return () => {};
        if (key.endsWith('Summary')) return () => '';
        return key;
      } });
      throw Error(name);
    },
  });
  const render = (nextProps = props) => { props = nextProps; cursor = 0; const result = mod.exports.Tested(props);
    if (result?.props?.rootRef) result.props.rootRef.current = panelElement;
    pending.splice(0).forEach(fn => fn()); return result; };
  return { calls, timers, callbacks, render, exported: mod.exports.Tested, moduleExports: mod.exports,
    initialize: () => mod.exports.default(),
    get writes() { return writes; },
    advance(milliseconds) { now += milliseconds; },
    visible(value) { visible = value; return render(); },
    panelVisible(value) { panelShown = value; intersectionCallback?.([{ isIntersecting: value }]); return render(); },
    fire() { [...timers.values()].filter(t => t.kind === 'interval').forEach(t => t.fn()); },
    respond(name, value) { const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`);
      call.settled = true; call.resolve(value); },
    reject(name) { const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`);
      call.settled = true; call.reject(new Error('temporary RPC failure')); },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); },
    remount() { slots.length = 0; pending.length = 0; return render(); },
  };
}
function find(node, label) {
  if (!node || typeof node !== 'object') return;
  if (node.props?.label === label) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) { const result = find(child, label); if (result) return result; }
}
function findButton(node, text) {
  if (!node || typeof node !== 'object') return;
  const children = node.props?.children;
  const label = Array.isArray(children) ? children.join('') : children;
  if (node.type === 'ButtonItem' && label === text) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = findButton(child, text); if (result) return result;
  }
}
function findSection(node, title, action = 'onClick') {
  if (!node || typeof node !== 'object') return;
  if (node.props?.title === title && node.props?.[action]) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const found = findSection(child, title, action); if (found) return found;
  }
}
function findPanel(node, title) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'PanelSection' && node.props?.title === title) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = findPanel(child, title); if (result) return result;
  }
}
function findModeChoice(node) {
  if (!node || typeof node !== 'object') return;
  if (typeof node.type === 'function' && node.type.name === 'ModeChoice') return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = findModeChoice(child); if (result) return result;
  }
}
const rgb = { success: true, settings: { configured: true, control_enabled: true, rings_enabled: true, effect: 'monocolor',
  hue: 255, saturation: 100, brightness: 50, speed: 50 }, rgb: { supported: true, effects: [] }, power_led: { supported: false } };
const remap = { supported: true, enabled: true, desktop_action: 'f1', page_action: 'f2', actions: [{ id: 'f1', label: 'F1' }, { id: 'f3', label: 'F3' }] };
const display = { panel_mode: 'pq', active_mode: 'pq', setup_done: true, panel_ok: true, enabled: true, edid_fix: true };
const wifi = { success: true, connected: true, settings: { device_family: 'legion_go_2', driver: 'mt7921e', band_policy: 'five_six_no_24', band_preference_enabled: true }, live: {wifi_backend:'iwd', connection_state:'connected', connection_uuid:'fixture-profile', ssid:'Target', frequency:'5260'} };
const cases = [
  ['display.tsx', 'DisplayPage', 'display_get_state', display],
  ['rgb.tsx', 'RgbPage', 'rgb_get_status', rgb],
  ['remap.tsx', 'RemapPage', 'remap_get_status', remap],
  ['wifi.tsx', 'WifiPage', 'wifi_get_status', wifi],
];
(async () => {
  {
    const h=harness('battery.tsx','BatteryPage'),initial={success:true,supported:true,enabled:false,managed:false},
      final={success:true,supported:true,enabled:true,managed:true};
    h.render();h.respond('battery_get_status',initial);await settle();
    find(h.render(),'Battery protection').props.onChange(true);await settle();h.unmount();
    const overview=h.moduleExports.getBatteryStatus();h.respond('battery_set_enabled',{success:true});await settle();
    assert.equal(h.calls.filter(c=>c.name==='battery_get_status').length,2,'A result without a snapshot still starts its verification read inside the transaction.');
    h.respond('battery_get_status',final);await settle();
    assert.equal(h.calls.filter(c=>c.name==='battery_get_status').length,3,'The public getter waits for verification without making the transaction wait for itself.');
    h.respond('battery_get_status',final);assert.equal(await overview,final);
  }

  for (const spec of [
    {file:'battery.tsx',component:'BatteryPage',read:'battery_get_status',getter:'getBatteryStatus',props:{},
      initial:{success:true,supported:true,enabled:false,managed:false},
      final:{success:true,supported:true,enabled:true,managed:true},method:'battery_set_enabled',label:'Battery protection',selection:true},
    {file:'controller.tsx',component:'ControllerPage',read:'controller_get_status',getter:'getControllerStatus',props:{visible:true},
      initial:{available:true,gyro_source:'system',controlled:false},final:{available:true,gyro_source:'left',controlled:true},
      method:'controller_set_gyro_source',label:'Controller motion source',selection:{data:'left'}},
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',getter:'getRemapStatus',props:{},initial:remap,
      final:{...remap,desktop_action:'f3'},method:'remap_set_action',label:'Desktop button',selection:{data:'f3'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',getter:'getRgbStatus',props:{},initial:rgb,
      final:{...rgb,settings:{...rgb.settings,effect:'breathe'}},method:'rgb_set_effect',label:'Lighting effect',selection:{data:'breathe'}},
  ]) {
    const h=harness(spec.file,spec.component);h.render(spec.props);h.respond(spec.read,spec.initial);await settle();
    find(h.render(),spec.label).props.onChange(spec.selection);await settle();h.unmount();
    const overview=h.moduleExports[spec.getter]();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'The main-menu getter must wait for edits accepted by a closed page.');
    h.respond(spec.method,spec.component==='ControllerPage'?spec.final:{success:true,status:spec.final});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2);
    h.respond(spec.read,spec.final);assert.equal(await overview,spec.final);
  }

  for (const failed of [false, true]) for (const spec of [
    {file:'battery.tsx',component:'BatteryPage',read:'battery_get_status',props:{},
      initial:{success:true,supported:true,enabled:false,managed:false},
      final:{success:true,supported:true,enabled:true,managed:true,requested_enabled:true},
      method:'battery_set_enabled',label:'Battery protection',selection:true,key:'checked',expected:true},
    {file:'controller.tsx',component:'ControllerPage',read:'controller_get_status',props:{visible:true},
      initial:{available:true,gyro_source:'system',controlled:false},
      final:{available:true,gyro_source:'left',applied_source:'left',controlled:true},
      method:'controller_set_gyro_source',label:'Controller motion source',selection:{data:'left'},key:'selectedOption',expected:'left'},
  ]) {
    const h=harness(spec.file,spec.component);h.render(spec.props);h.respond(spec.read,spec.initial);
    await settle();h.fire();await settle();
    find(h.render(),spec.label).props.onChange(spec.selection);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,0,'An accepted change waits for its earlier status read.');
    h.unmount();h.remount();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A reopened page cannot overtake an accepted change still waiting for its read.');
    h.respond(spec.read,spec.initial);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,1);
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'The new status read also waits for the actual mutation.');
    if(failed) {
      if(spec.component==='ControllerPage')h.render({visible:false});else h.visible(false);
      h.reject(spec.method);
    }
    else h.respond(spec.method,spec.component==='BatteryPage'?{success:true,status:spec.final}:spec.final);
    await settle();
    if(failed) {
      assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A hidden reopened page must not read after the old mutation ends.');
      if(spec.component==='ControllerPage')h.render({visible:true});else h.visible(true);
      await settle();
    }
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,3,'Completion or rejection immediately releases the reopened status read.');
    h.respond(spec.read,failed?spec.initial:spec.final);await settle();
    assert.equal(find(h.render(),spec.label).props[spec.key],failed?(spec.key==='checked'?false:'system'):spec.expected);
    h.unmount();
  }

  // A reopened page must observe all edits accepted by its previous instance.
  // The old instance may still have an RPC running and another one queued.
  for (const spec of [
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',initial:remap,
      method:'remap_set_action',label:'Desktop button',values:[{data:'f3'},{data:'f1'},{data:'f3'}],
      first:{...remap,desktop_action:'f3'},final:{...remap,desktop_action:'f1'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',initial:rgb,
      method:'rgb_set_effect',label:'Lighting effect',values:[{data:'breathe'},{data:'rainbow'},{data:'monocolor'}],
      first:{...rgb,settings:{...rgb.settings,effect:'breathe'}},
      final:{...rgb,settings:{...rgb.settings,effect:'rainbow'}}},
  ]) {
    const h=harness(spec.file,spec.component);h.render();h.respond(spec.read,spec.initial);await settle();
    const select=find(h.render(),spec.label).props.onChange;
    select(spec.values[0]);select(spec.values[1]);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,1);
    h.unmount();h.remount();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'Reopening waits for the accepted mutation tail before reading status.');
    h.respond(spec.method,{success:true,status:spec.first});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'The second queued edit still precedes the new snapshot.');
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,2);
    h.respond(spec.method,{success:true,status:spec.final});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2);
    h.respond(spec.read,spec.final);await settle();
    assert.equal(find(h.render(),spec.label).props.selectedOption,
      spec.file==='rgb.tsx'?'rainbow':'f1','The reopened page displays the last accepted edit.');
    find(h.render(),spec.label).props.onChange(spec.values[2]);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,3);
    h.respond(spec.method,{success:true,status:spec.initial});await settle();h.unmount();
  }
  for (const spec of [
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',initial:remap,method:'remap_set_action',label:'Desktop button',value:{data:'f3'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',initial:rgb,method:'rgb_set_effect',label:'Lighting effect',value:{data:'breathe'}},
  ]) {
    const h=harness(spec.file,spec.component);h.render();h.respond(spec.read,spec.initial);await settle();
    find(h.render(),spec.label).props.onChange(spec.value);await settle();h.unmount();h.remount();await settle();
    h.visible(false);h.reject(spec.method);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'A hidden page does not start a read when the older failed mutation finishes.');
    h.visible(true);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A failed mutation cannot leave the new page waiting forever.');
    h.respond(spec.read,spec.initial);await settle();assert.ok(find(h.render(),spec.label));h.unmount();
  }

  const moduleState={display:{enabled:false,note:'Restart required'},rgb:{enabled:true,ready:true}};
  {
    const h=harness('index.tsx','ModulesPage');const tree=h.render({modules:moduleState});
    const toggle=find(tree,'RGB Lighting').props.onChange;
    const restart=findButton(tree,'Restart Gaming Mode (closes games)').props.onClick;
    h.unmount();const writes=h.writes;toggle(false);restart();await settle();
    assert.equal(h.calls.length,0,'Discarded module controls cannot start restoration or a session restart.');
    assert.equal(h.writes,writes);
  }
  {
    const h=harness('index.tsx','ModulesPage');let tree=h.render({modules:moduleState});
    const restart=findButton(tree,'Restart Gaming Mode (closes games)').props.onClick;
    restart();restart();find(tree,'RGB Lighting').props.onChange(false);await settle();
    assert.equal(h.calls.length,1,'A session restart coalesces repeated clicks and excludes module changes.');
    assert.equal(h.calls[0].name,'modules_restart_session');
    assert.equal(findButton(h.render(),'Restart Gaming Mode (closes games)').props.disabled,true);
    h.respond('modules_restart_session',{success:false,error:'session unavailable'});await settle();tree=h.render();
    assert.equal(findButton(tree,'Restart Gaming Mode (closes games)').props.disabled,false);
    assert.match(find(tree,'Could not confirm').props.description,/session unavailable/);
    restart();await settle();assert.equal(h.calls.length,2,'A failed restart can be retried.');
    h.unmount();const writes=h.writes;h.respond('modules_restart_session',{success:false,error:'late error'});await settle();
    assert.equal(h.writes,writes,'Restart completion cannot update a discarded page.');
  }
  // Immediate actions must follow already accepted slider edits, including
  // restore/disable actions that would otherwise overtake their debounce.
  for (const action of ['disable', 'restore', 'effect']) {
    const h=harness('rgb.tsx','RgbPage');h.render();h.respond('rgb_get_status',rgb);await settle();
    find(h.render(),'Brightness').props.onChange(90);
    if(action==='disable')find(h.render(),'Enable RGB control').props.onChange(false);
    else if(action==='restore')findButton(h.render(),'Restore lighting state from before Companion').props.onClick();
    else find(h.render(),'Lighting effect').props.onChange({data:'breathe'});
    await settle();
    const writes=h.calls.filter(c=>c.name!=='rgb_get_status');
    assert.equal(writes[0]?.name,'rgb_set_brightness',`The earlier slider save precedes ${action}.`);
    assert.equal(writes.length,1,'The later operation remains queued until the slider finishes.');
    h.respond('rgb_set_brightness',{success:true,status:{...rgb,settings:{...rgb.settings,brightness:90}}});await settle();
    const method=action==='disable'?'rgb_set_control_enabled':action==='restore'?'rgb_restore_original':'rgb_set_effect';
    assert.equal(h.calls.at(-1).name,method);
    h.respond(method,{success:true,status:rgb});await settle();
    for(const [id,t]of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();}
    await settle();assert.equal(h.calls.filter(c=>c.name==='rgb_set_brightness').length,1);
    h.unmount();
  }
  for(const leave of ['timer','unmount']) {
    const h=harness('rgb.tsx','RgbPage');h.render();
    const breathing={...rgb,settings:{...rgb.settings,effect:'breathe'}};
    h.respond('rgb_get_status',breathing);await settle();
    find(h.render(),'Speed').props.onChange(0);await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_set_speed').length,0,'Speed edits debounce before hardware I/O.');
    assert.equal(find(h.render(),'Speed').props.disabled,false,'A speed gesture stays responsive while its timer is pending.');
    for(const t of h.timers.values())if(t.kind==='interval')t.fn();
    await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_get_status').length,1,'Polling cannot replace an unsaved speed draft.');
    const obsolete=[...h.timers.values()].find(t=>t.kind==='timeout').fn;
    find(h.render(),'Speed').props.onChange(100);obsolete();await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_set_speed').length,0,'Superseded speed timers cannot queue a save.');
    if(leave==='unmount')h.unmount();
    else for(const[id,t]of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();t.fn();}
    await settle();
    const writes=h.calls.filter(c=>c.name==='rgb_set_speed');
    assert.equal(writes.length,1,'Only the final speed is saved, once, including on close.');
    assert.equal(writes[0].args[0],100);
    h.respond('rgb_set_speed',{success:true,status:{...breathing,settings:{...breathing.settings,speed:100}}});await settle();
    if(leave==='timer')h.unmount();
  }
  {
    const h=harness('rgb.tsx','RgbPage');h.render();h.respond('rgb_get_status',rgb);await settle();
    const tree=h.render(),gestures=[()=>find(tree,'Hue').props.onChange(120),
      ()=>find(tree,'Brightness').props.onChange(80),()=>find(tree,'Enable RGB control').props.onChange(false)];
    h.unmount();const writes=h.writes,calls=h.calls.length;
    gestures.forEach(run=>run());
    for(const [id,t] of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();}
    await settle();assert.equal(h.writes,writes,'Closed RGB controls cannot create new drafts.');
    assert.equal(h.calls.length,calls,'Closed RGB controls cannot start new writes.');
  }
  for (const [file,component,read,value] of [...cases,
    ['battery.tsx','BatteryPage','battery_get_status',{success:true,supported:false}],
    ['controller.tsx','ControllerPage','controller_get_status',{supported:false}],
  ]) {
    const h=harness(file,component);h.render({visible:true});
    for(let i=0;i<3;i++){
      if(file==='controller.tsx'){h.render({visible:false});h.render({visible:true});}
      else {h.visible(false);h.visible(true);}
    }
    assert.equal(h.calls.filter(c=>c.name===read).length,1,`${component}: rapid reopening never overlaps reads.`);
    const before=h.writes;h.respond(read,value);
    if(file==='battery.tsx')h.respond('battery_get_controller_levels',{available:false});
    await settle();assert.equal(h.writes,before,'A reply from before hiding stays discarded.');
    assert.equal(h.calls.filter(c=>c.name===read).length,2,
      `${component} immediately starts one fresh read after reopening instead of waiting for polling.`);
    h.respond(read,value);
    if(file==='battery.tsx')h.respond('battery_get_controller_levels',{available:false});
    await settle();h.unmount();
  }
  for (const queued of [false,true]) {
    const h=harness('remap.tsx','RemapPage');h.render();h.respond('remap_get_status',remap);await settle();
    const change=find(h.render(),'Desktop button').props.onChange;
    if(queued)change({data:'f3'});
    h.unmount();const before=h.writes;
    if(!queued)change({data:'f3'});
    await settle();assert.equal(h.writes,before,'Unmounted remap callbacks never update React state.');
    assert.equal(h.calls.filter(c=>c.name==='remap_set_action').length,queued?1:0,
      'A previously accepted mapping still saves; a new callback after unmount is ignored.');
    if(queued){h.respond('remap_set_action',{success:true,status:remap});await settle();}
  }
  {
    const h=harness('display.tsx','DisplayPage');h.render();h.respond('display_get_state',display);await settle();
    const change=find(findPanel(h.render(),'Brightness slider'),'Enabled').props.onChange;
    h.unmount();const before=h.writes;change(false);await settle();
    assert.equal(h.calls.filter(c=>c.name==='display_set_enabled').length,0,'Unmounted display callbacks cannot start writes.');
    assert.equal(h.writes,before);
  }
  for (const failed of [false,true]) {
    const h=harness('wifi.tsx','WifiPage');h.render();h.respond('wifi_get_status',wifi);await settle();
    const click=findButton(h.render(),'Use Automatic WiFi').props.onClick;click();h.unmount();
    const writes=h.writes;
    if(failed)h.reject('wifi_set_band_policy');
    else h.respond('wifi_set_band_policy',{success:true,message:'Saved'});
    await settle();assert.equal(h.writes,writes,'A late WiFi mutation result cannot update an unmounted page.');
    click();await settle();
    assert.equal(h.writes,writes,'A queued WiFi callback cannot update an unmounted page.');
    assert.equal(h.calls.filter(c=>c.name==='wifi_set_band_policy').length,1,
      'A queued callback cannot start another WiFi operation after unmount.');
  }
  {
    const h=harness('wifi.tsx','WifiPage');h.render();h.respond('wifi_get_status',wifi);await settle();
    findButton(h.render(),'Use Automatic WiFi').props.onClick();
    h.respond('wifi_set_band_policy',{success:true,reconnected:true,message:'Verified'});await settle();
    assert.equal(h.calls.filter(c=>c.name==='wifi_get_status').length,2,
      'Verified reconnect refreshes immediately instead of holding the page for three seconds.');
    assert.equal([...h.timers.values()].filter(t=>t.kind==='timeout').length,0);
    h.respond('wifi_get_status',{...wifi,settings:{...wifi.settings,band_policy:'off'}});await settle();
    assert.equal(findButton(h.render(),'Prefer 5/6 GHz (2.4 GHz fallback)').props.disabled,false);h.unmount();
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    for (const [id,t] of [...h.timers]) if (t.kind==='timeout') { h.timers.delete(id); t.fn(); }
    await settle(); find(h.render(), 'Brightness').props.onChange(90);
    h.reject('rgb_set_color'); await settle();
    for (const [id,t] of [...h.timers]) if (t.kind==='timeout') { h.timers.delete(id); t.fn(); }
    await settle(); h.respond('rgb_set_brightness',{success:true,status:{...rgb,settings:{...rgb.settings,brightness:90}}});
    await settle(); assert.equal(find(h.render(),'Brightness').props.value,90);
    assert.equal(find(h.render(),'Hue').props.value,rgb.settings.hue,'a failed old draft cannot be resurrected by the next verified result');
    h.unmount();
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    const oldTimer = [...h.timers.values()].find(t => t.kind === 'timeout').fn;
    find(h.render(), 'Hue').props.onChange(240);
    oldTimer();
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    assert.equal(h.calls.filter(c => c.name === 'rgb_set_color').length, 1, 'superseded RGB callback cannot enqueue obsolete work');
    assert.equal(h.calls.find(c => c.name === 'rgb_set_color').args[0], 240);
    h.respond('rgb_set_color', {success:true,status:{...rgb,settings:{...rgb.settings,hue:240}}}); await settle(); h.unmount();
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    find(h.render(), 'Brightness').props.onChange(90);
    h.respond('rgb_set_color', {success:true,status:{...rgb,settings:{...rgb.settings,hue:120}}}); await settle();
    assert.equal(find(h.render(), 'Brightness').props.value, 90, 'old RGB reply preserves a newer unsaved slider draft');
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    h.respond('rgb_set_brightness', {success:true,status:{...rgb,settings:{...rgb.settings,hue:120,brightness:90}}});
    await settle(); assert.equal(find(h.render(), 'Hue').props.value, 120); h.unmount();
  }
  {
    const h = harness('index.tsx', 'ModulesPage');
    const modules = { rgb: { enabled: true, ready: false, error: 'Startup failed', pending: false } };
    const view = h.render({ modules });
    const retry = findButton(view, 'Retry RGB Lighting Start');
    assert.ok(retry, 'failed startup offers a distinct retry without cleanup first');
    assert.equal(find(view, 'RGB Lighting').props.checked, false, 'failed readiness is visible');
    retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === 'modules_set_enabled').length, 1);
    h.respond('modules_set_enabled', { rgb: { enabled: true, ready: true } }); await settle();
    assert.equal(findButton(h.render({ modules: { rgb: { enabled: true, ready: true } } }),
      'Retry RGB Lighting Start'), undefined);
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(findPanel(h.render(), 'EDID for games'), 'Enabled').props.onChange;
    h.fire(); h.reject('display_get_state'); await settle();
    const failed = h.render();
    assert.ok(find(failed, 'Display status unavailable'), 'A failed poll cannot present a saved display snapshot as current.');
    retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'Retained controls cannot write after a failed read.');
    findButton(failed, 'Retry status').props.onClick();
    h.respond('display_get_state', display); await settle();
    assert.ok(findPanel(h.render(), 'EDID for games'), 'A fresh read restores controls.');
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(findPanel(h.render(), 'EDID for games'), 'Enabled').props.onChange;
    h.visible(false); retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'A hidden display page does not accept a stale toggle.');
    h.visible(true); retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'Reopening must confirm current display ownership first.');
    h.respond('display_get_state', display); await settle();
    retained(false); await settle();
    assert.equal(h.calls.filter(c => c.name === 'display_set_edid_fix').length, 1);
    h.respond('display_set_edid_fix', { ...display, edid_fix: false }); await settle(); h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(findPanel(h.render(), 'EDID for games'), 'Enabled').props.onChange;
    retained(false); await settle(); h.visible(false); h.visible(true);
    h.respond('display_set_edid_fix', { ...display, edid_fix: false }); await settle();
    assert.equal(findPanel(h.render(), 'EDID for games'), undefined, 'A previous visit write result cannot establish current ownership after reopening.');
    retained(true); await settle();
    assert.equal(h.calls.filter(c => c.name === 'display_set_edid_fix').length, 1);
    h.respond('display_get_state', { ...display, edid_fix: false }); await settle();
    assert.ok(findPanel(h.render(), 'EDID for games')); h.unmount();
  }
  for (const [file, component, read, value] of cases) {
    const h = harness(file, component);
    h.render(); h.reject(read); await settle();
    let view = h.render();
    assert.match(JSON.stringify(view), /status unavailable/, `${component} shows the first read failure`);
    const retry = findButton(view, 'Retry status');
    assert.ok(retry, `${component} allows an explicit retry`);
    retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === read).length, 2, 'retry remains bounded');
    h.reject(read); await settle();
    assert.ok(findButton(h.render(), 'Retry status'), 'repeated errors remain actionable');
    h.visible(false); const before = h.calls.length; h.fire();
    assert.equal(h.calls.length, before, 'failed hidden pages do not poll');
    h.visible(true); h.respond(read, value); await settle();
    assert.equal(findButton(h.render(), 'Retry status'), undefined, 'successful recovery clears initial error');
    h.unmount();
  }
  for (const [file, component, read, value] of cases) {
    const h = harness(file, component, false);
    h.render(); assert.equal(h.calls.length, 0, `${component} does no work while hidden`);
    h.visible(true); h.fire(); h.fire();
    assert.equal(h.calls.length, 1, `${component} has at most one unresolved read`);
    h.visible(false); const before = h.writes;
    h.respond(read, value); await settle(); assert.equal(h.writes, before, `${component} ignores hidden reads`);
    h.visible(true); assert.equal(h.calls.length, 2); h.unmount(); const after = h.writes;
    h.respond(read, value); await settle(); assert.equal(h.writes, after, `${component} ignores unmounted reads`);
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    findButton(h.render(), 'Reset Display Fix').props.onClick();
    h.fire(); // Keep an older status read unresolved across the reset.
    const confirm = findButton(h.render(), 'Confirm Display Reset');
    confirm.props.onClick(); confirm.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === 'display_reset_settings').length, 1);
    const initial = { ...display, panel_mode: null, active_mode: null, setup_done: false,
      reset_in_progress: false, reset_restart_pending: true, reset_error: '' };
    h.respond('display_reset_settings', initial); await settle();
    assert.ok(findButton(h.render(), 'Restart Game Mode'));
    assert.equal(findButton(h.render(), 'Other options').props.disabled, true);
    h.respond('display_get_state', display); await settle();
    assert.ok(findButton(h.render(), 'Restart Game Mode'), 'stale read cannot undo the reset');
    h.fire(); h.respond('display_get_state', { ...initial, reset_restart_pending: false }); await settle();
    assert.equal(findButton(h.render(), 'Other options').props.disabled, false);
    assert.equal(findButton(h.render(), 'Reset Display Fix'), undefined, 'reset returns to the initial chooser');
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, reset_in_progress: true, reset_error: 'Restore failed' }); await settle();
    assert.equal(find(h.render(), 'Display controls are paused').props.description, 'Restore failed');
    assert.equal(find(h.render(), 'Enabled'), undefined);
    findButton(h.render(), 'Retry Display Reset').props.onClick(); await settle();
    h.respond('display_reset_settings', { ...display, panel_mode: null, setup_done: false,
      reset_in_progress: false, reset_restart_pending: true, reset_error: '' }); await settle();
    assert.ok(findButton(h.render(), 'Restart Game Mode')); h.unmount();
  }
  for (const gate of [{ setup_done: false }, { restart_pending: true }]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, ...gate }); await settle();
    assert.ok(findButton(h.render(), 'Reset Display Fix'), 'reset remains accessible behind setup/restart gates');
    h.unmount();
  }
  const nativeDisplay = { ...display, panel_mode: null, active_mode: null, setup_done: false,
    native_support_detected: true, native_support_reason: 'Complete upstream display support is present.',
    native_cleanup_pending: false, native_cleanup_error: '', native_restart_pending: false };
  for (const state of [
    { ...display, panel_mode: null, setup_done: false },
    { ...display, setup_done: false },
    { ...display, restart_pending: true },
    { ...display, panel_mode: null, setup_done: false, reset_restart_pending: true },
    nativeDisplay,
    { ...nativeDisplay, native_cleanup_pending: true },
    { ...nativeDisplay, native_cleanup_pending: true, native_cleanup_error: 'Previous script could not be restored.' },
    { ...nativeDisplay, native_restart_pending: true },
    { ...nativeDisplay, native_support_detected: false, native_cleanup_pending: true },
  ]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', state); await settle();
    let tree = h.render();
    const edid = find(findPanel(tree, 'EDID for games'), 'Enabled');
    assert.ok(edid, 'EDID is visible independently of mode, setup and cleanup.');
    assert.equal(edid.props.disabled, false);
    if (state.native_support_detected || state.native_cleanup_pending) {
      assert.equal(findPanel(tree, 'Brightness slider'), undefined);
      assert.equal(findModeChoice(tree), undefined);
      assert.equal(findButton(tree, 'Setup Display Fix'), undefined);
      assert.equal(findButton(tree, 'Switch Display Mode'), undefined);
      assert.equal(findButton(tree, 'Reset Display Fix'), undefined);
      assert.ok(find(tree, 'Companion display fix disabled'));
    }
    if (state.native_support_detected) {
      assert.equal(find(tree, 'Native display support detected').props.description, state.native_support_reason);
    }
    if (state.native_cleanup_error) {
      assert.match(find(tree, 'Display fix cleanup needs attention').props.description, /Previous script.*retry automatically/);
    }
    edid.props.onChange(false); await settle();
    assert.deepEqual(h.calls.map(call => call.name), ['display_get_state', 'display_set_edid_fix']);
    assert.deepEqual(Array.from(h.calls[1].args), [false]);
    h.respond('display_set_edid_fix', { ...state, edid_fix: false }); await settle();
    tree = h.render();
    assert.equal(find(findPanel(tree, 'EDID for games'), 'Enabled').props.checked, false);
    if (state.native_restart_pending) {
      assert.equal(h.calls.some(call => call.name === 'display_restart_session'), false, 'Native handover never restarts automatically.');
      findButton(tree, 'Restart Game Mode').props.onClick(); await settle();
      assert.equal(h.calls.filter(call => call.name === 'display_restart_session').length, 1);
      h.respond('display_restart_session', { ...state, native_restart_pending: false }); await settle();
    }
    h.unmount();
  }
  // A retained callback from an older render cannot re-enable the fix after a
  // newer native-support snapshot has arrived, even before React renders it.
  for (const state of [nativeDisplay, { ...nativeDisplay, native_support_detected: false, native_cleanup_pending: true }]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const brightness = find(findPanel(h.render(), 'Brightness slider'), 'Enabled');
    findButton(h.render(), 'Switch Display Mode').props.onClick();
    const mode = findModeChoice(h.render()); assert.ok(mode);
    findButton(h.render(), 'Reset Display Fix').props.onClick();
    const reset = findButton(h.render(), 'Confirm Display Reset'); assert.ok(reset);
    h.fire(); h.respond('display_get_state', state); await settle();
    brightness.props.onChange(true); mode.props.onPick('hybrid'); reset.props.onClick(); await settle();
    assert.equal(h.calls.some(call => ['display_set_enabled', 'display_set_panel_mode', 'display_reset_settings'].includes(call.name)), false);
    h.unmount();
    const chooser = harness('display.tsx', 'DisplayPage'); chooser.render();
    chooser.respond('display_get_state', { ...display, panel_mode: null, setup_done: false }); await settle();
    const setup = findModeChoice(chooser.render()); assert.ok(setup);
    chooser.fire(); chooser.respond('display_get_state', state); await settle();
    setup.props.onPick('hybrid'); await settle();
    assert.equal(chooser.calls.some(call => call.name === 'display_run_setup'), false); chooser.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    h.fire(); // This old polling response predates the native handover.
    find(findPanel(h.render(), 'EDID for games'), 'Enabled').props.onChange(false); await settle();
    h.respond('display_set_edid_fix', { ...nativeDisplay, edid_fix: false }); await settle();
    h.respond('display_get_state', display); await settle();
    assert.ok(find(h.render(), 'Companion display fix disabled'), 'Stale polling cannot undo the native gate.');
    assert.equal(find(findPanel(h.render(), 'EDID for games'), 'Enabled').props.checked, false); h.unmount();
  }
  for (const spec of [
    { file: 'display.tsx', component: 'DisplayPage', read: 'display_get_state', initial: display, next: { ...display, enabled: false },
      label: 'Enabled', method: 'display_set_enabled', value: false },
    { file: 'rgb.tsx', component: 'RgbPage', read: 'rgb_get_status', initial: rgb, next: { ...rgb, settings: { ...rgb.settings, control_enabled: false } },
      label: 'Enable RGB control', method: 'rgb_set_control_enabled', value: false },
    { file: 'remap.tsx', component: 'RemapPage', read: 'remap_get_status', initial: remap, next: { ...remap, desktop_action: 'f3' },
      label: 'Desktop button', method: 'remap_set_action', value: { data: 'f3' } },
  ]) {
    const h = harness(spec.file, spec.component); h.render(); h.respond(spec.read, spec.initial); await settle();
    let tree = h.render(); h.fire();
    const control = find(tree, spec.label); assert.ok(control, spec.label); control.props.onChange(spec.value); await settle();
    h.respond(spec.method, spec.file === 'display.tsx' ? spec.next : { success: true, status: spec.next }); await settle();
    tree = h.render(); const saved = find(tree, spec.label).props;
    h.respond(spec.read, spec.initial); await settle(); tree = h.render();
    assert.equal(find(tree, spec.label).props.checked, saved.checked, `${spec.component} keeps the saved toggle`);
    assert.equal(find(tree, spec.label).props.selectedOption, saved.selectedOption, `${spec.component} keeps the saved dropdown`);
    h.unmount();
  }
  const hidden = harness('remap.tsx', 'RemapPage'); hidden.render(); hidden.respond('remap_get_status', remap); await settle();
  const menu = find(hidden.render(), 'Desktop button'); hidden.visible(false); menu.props.onChange({ data: 'f3' }); await settle();
  assert.equal(hidden.calls.filter(c => c.name === 'remap_set_action').length, 1, 'a dropdown choice still saves while QAM is hidden');
  hidden.respond('remap_set_action', { success: true, status: { ...remap, desktop_action: 'f3' } }); await settle(); hidden.unmount();

  const network = harness('wifi.tsx', 'WifiPage'); network.render(); network.respond('wifi_get_status', wifi); await settle();
  network.fire(); findButton(network.render(), 'Use Automatic WiFi').props.onClick(); await settle();
  assert.deepEqual(network.calls.find(c => c.name === 'wifi_set_band_policy').args, ['off']);
  network.respond('wifi_set_band_policy', { success: true, message: 'Saved' }); await settle();
  const beforeOldRead = network.writes;
  network.respond('wifi_get_status', wifi); await settle();
  assert.equal(network.writes, beforeOldRead, 'old WiFi status cannot overwrite the mutation');
  assert.equal(network.calls.filter(c => c.name === 'wifi_get_status').length, 3, 'WiFi confirms status after the earlier read finishes');
  network.respond('wifi_get_status', { ...wifi, settings: { ...wifi.settings, band_policy: 'off', band_preference_enabled: false } }); await settle();
  assert.ok(findButton(network.render(), '> Automatic WiFi')); network.unmount();

  const manualWpa = harness('wifi.tsx', 'WifiPage'); manualWpa.render();
  const automaticWpa = { ...wifi, connected: true, settings: { ...wifi.settings, band_policy: 'off', band_preference_enabled: false },
    live: { wifi_backend: 'wpa_supplicant', frequency: '5260' } };
  manualWpa.respond('wifi_get_status', automaticWpa); await settle();
  assert.equal(findButton(manualWpa.render(), 'Prefer 5/6 GHz (2.4 GHz fallback)'), undefined);
  assert.equal(findButton(manualWpa.render(), 'Use Only 5/6 GHz'), undefined);
  assert.equal(findButton(manualWpa.render(), 'Rescan and reconnect to 5/6 GHz').props.disabled, false);
  findButton(manualWpa.render(), 'Connect to 6 GHz once').props.onClick(); await settle();
  assert.deepEqual(manualWpa.calls.find(c => c.name === 'wifi_rescan_and_reconnect').args, ['six']);
  assert.equal(findButton(manualWpa.render(), 'Connect to 6 GHz once').props.disabled, true);
  manualWpa.respond('wifi_rescan_and_reconnect', { success: true, frequency: 6295, message: 'Connected; temporary selection cleared' }); await settle();
  manualWpa.respond('wifi_get_status', { ...automaticWpa, live: { ...automaticWpa.live, frequency: '6295' } }); await settle();
  assert.ok(findButton(manualWpa.render(), '> Automatic WiFi'), 'one-time connection keeps the automatic policy');
  manualWpa.unmount();

  const strictWifi = harness('wifi.tsx', 'WifiPage'); strictWifi.render();
  strictWifi.respond('wifi_get_status', wifi); await settle();
  findButton(strictWifi.render(), 'Use Only 5/6 GHz').props.onClick();
  assert.ok(find(strictWifi.render(), 'Connection attempt'));
  findButton(strictWifi.render(), 'Confirm Only 5/6 GHz').props.onClick(); await settle();
  assert.deepEqual(strictWifi.calls.find(c => c.name === 'wifi_set_band_policy').args, ['five_six_only', true]);
  strictWifi.respond('wifi_set_band_policy', { success: true, message: 'Saved' }); await settle();
  strictWifi.respond('wifi_get_status', { ...wifi, connected: false,
    settings: { ...wifi.settings, band_policy: 'five_six_only', band_preference_enabled: false } }); await settle();
  assert.ok(findButton(strictWifi.render(), 'Use Automatic WiFi'), 'strict mode can be disabled when disconnected');
  strictWifi.unmount();

  const oldPolicy = harness('wifi.tsx', 'WifiPage'); oldPolicy.render();
  const six = { ...wifi, connected: true, settings: { ...wifi.settings, band_policy: 'six_ghz_only', band_preference_enabled: false },
    live: { ...wifi.live, wifi_backend: 'wpa_supplicant', frequency: 5975 } };
  oldPolicy.respond('wifi_get_status', six); await settle();
  assert.ok(find(oldPolicy.render(), 'Existing 6 GHz only policy'));
  assert.match(oldPolicy.moduleExports.wifiSummary(six), /^6 GHz only/);
  assert.ok(findButton(oldPolicy.render(), 'Use Automatic WiFi'));
  oldPolicy.unmount();
  const inactiveSix=harness('wifi.tsx','WifiPage');inactiveSix.render();
  const sixOnIwd={...six,live:{...six.live,wifi_backend:'iwd'}};
  inactiveSix.respond('wifi_get_status',sixOnIwd);await settle();
  assert.match(inactiveSix.moduleExports.wifiSummary(sixOnIwd),/^Automatic WiFi.*6 GHz setting inactive/);
  assert.match(find(inactiveSix.render(),'Existing 6 GHz only policy').props.description,/inactive with iwd/);
  inactiveSix.unmount();

  const legacyWifi = harness('wifi.tsx', 'WifiPage'); legacyWifi.render();
  const legacy = { ...wifi, settings: { ...wifi.settings, band_policy: 'off',
    band_preference_enabled: false, band_policy_legacy_detected: true } };
  legacyWifi.respond('wifi_get_status', legacy); await settle();
  assert.match(legacyWifi.moduleExports.wifiSummary(legacy), /^Legacy 5 GHz setting/);
  assert.equal(findButton(legacyWifi.render(), 'Prefer 5/6 GHz (2.4 GHz fallback)').props.disabled, true);
  findButton(legacyWifi.render(), 'Restore legacy band setting').props.onClick(); await settle();
  assert.deepEqual(legacyWifi.calls.find(c => c.name === 'wifi_set_band_policy').args, ['off']);
  legacyWifi.respond('wifi_set_band_policy', { success: true, message: 'Restored' }); await settle();
  legacyWifi.respond('wifi_get_status', { ...legacy, settings: { ...legacy.settings, band_policy_legacy_detected: false } }); await settle();
  assert.ok(findButton(legacyWifi.render(), '> Automatic WiFi')); legacyWifi.unmount();

  const preflightWifi = harness('wifi.tsx', 'WifiPage'); preflightWifi.render();
  preflightWifi.respond('wifi_get_status', wifi); await settle();
  findButton(preflightWifi.render(), 'Use Only 5/6 GHz').props.onClick();
  findButton(preflightWifi.render(), 'Confirm Only 5/6 GHz').props.onClick(); await settle();
  preflightWifi.respond('wifi_set_band_policy', { success: false, error: 'preflight_failed',
    message: 'No fresh 5 or 6 GHz scan result was verified.' }); await settle();
  preflightWifi.respond('wifi_get_status', wifi); await settle();
  assert.ok(find(preflightWifi.render(), 'Could not complete'));
  assert.ok(findButton(preflightWifi.render(), '> Prefer 5/6 GHz (2.4 GHz fallback)'));
  preflightWifi.unmount();

  // Native SteamOS switches and failed polls invalidate cached action callbacks.
  const switched = harness('wifi.tsx', 'WifiPage'); switched.render();
  const automaticIwd = {...wifi,settings:{...wifi.settings,band_policy:'off',band_preference_enabled:false}};
  switched.respond('wifi_get_status', automaticIwd); await settle();
  const stalePrefer = findButton(switched.render(), 'Prefer 5/6 GHz (2.4 GHz fallback)');
  const staleStrict = findButton(switched.render(), 'Use Only 5/6 GHz');
  switched.fire(); switched.respond('wifi_get_status', automaticWpa); await settle();
  stalePrefer.props.onClick(); staleStrict.props.onClick(); await settle();
  assert.equal(switched.calls.filter(c=>c.name==='wifi_set_band_policy').length,0);
  assert.equal(findButton(switched.render(),'Confirm Only 5/6 GHz'),undefined);
  assert.equal(findButton(switched.render(),'Prefer 5/6 GHz (2.4 GHz fallback)'),undefined);
  const retainedReconnect = findButton(switched.render(),'Connect to 6 GHz once');
  switched.fire(); switched.reject('wifi_get_status'); await settle();
  retainedReconnect.props.onClick(); await settle();
  assert.equal(switched.calls.filter(c=>c.name==='wifi_rescan_and_reconnect').length,0);
  assert.ok(find(switched.render(),'WiFi status unavailable'));
  assert.equal(find(switched.render(),'Result'),undefined);
  switched.unmount();

  const reopen=harness('wifi.tsx','WifiPage');reopen.render();
  reopen.respond('wifi_get_status',automaticIwd);await settle();
  const beforeHide=findButton(reopen.render(),'Prefer 5/6 GHz (2.4 GHz fallback)');
  reopen.visible(false);reopen.visible(true);beforeHide.props.onClick();await settle();
  assert.equal(reopen.calls.filter(c=>c.name==='wifi_set_band_policy').length,0,'reopened QAM waits for a fresh backend');
  assert.equal(findButton(reopen.render(),'Prefer 5/6 GHz (2.4 GHz fallback)'),undefined);
  reopen.respond('wifi_get_status',automaticWpa);await settle();
  assert.ok(findButton(reopen.render(),'Connect to 6 GHz once'));
  reopen.unmount();

  const hiddenSuccess=harness('wifi.tsx','WifiPage');hiddenSuccess.render();
  hiddenSuccess.respond('wifi_get_status',wifi);await settle();
  findButton(hiddenSuccess.render(),'Use Automatic WiFi').props.onClick();await settle();
  hiddenSuccess.visible(false);hiddenSuccess.respond('wifi_set_band_policy',{success:true,message:'Saved while hidden'});await settle();
  hiddenSuccess.visible(true);hiddenSuccess.respond('wifi_get_status',automaticWpa);await settle();
  assert.equal(find(hiddenSuccess.render(),'Could not complete'),undefined,'a hidden operation does not invent a stale verification failure');
  assert.equal(find(hiddenSuccess.render(),'Result'),undefined);
  hiddenSuccess.unmount();

  for (const state of ['unknown','connecting','disconnected']) {
    const h=harness('wifi.tsx','WifiPage');h.render();
    const next={...automaticWpa,connected:false,live:{...automaticWpa.live,connection_state:state}};
    h.respond('wifi_get_status',next);await settle();
    assert.equal(findButton(h.render(),'Connect to 6 GHz once').props.disabled,true);
    findButton(h.render(),'Connect to 6 GHz once').props.onClick();await settle();
    assert.equal(h.calls.filter(c=>c.name!=='wifi_get_status').length,0);
    h.unmount();
  }
  const unknownBackend=harness('wifi.tsx','WifiPage');unknownBackend.render();
  unknownBackend.respond('wifi_get_status',{...wifi,live:{...wifi.live,wifi_backend:''}});await settle();
  assert.ok(find(unknownBackend.render(),'Backend unavailable'));
  assert.equal(findButton(unknownBackend.render(),'Use Automatic WiFi'),undefined);
  assert.equal(findButton(unknownBackend.render(),'Use Only 5/6 GHz'),undefined);
  assert.match(unknownBackend.moduleExports.wifiSummary(undefined),/unavailable/);
  unknownBackend.unmount();

  const cleanup=harness('wifi.tsx','WifiPage');cleanup.render();
  const staleWpa={...wifi,live:{...wifi.live,wifi_backend:'wpa_supplicant',recovery_required:true,band_policy_restore_available:true}};
  cleanup.respond('wifi_get_status',staleWpa);await settle();
  assert.ok(find(cleanup.render(),'Saved iwd setting inactive'));
  assert.match(cleanup.moduleExports.wifiSummary(staleWpa),/^Automatic WiFi.*inactive/);
  assert.equal(findButton(cleanup.render(),'Clear saved iwd preference').props.disabled,false);
  const clear=findButton(cleanup.render(),'Clear saved iwd preference');clear.props.onClick();clear.props.onClick();await settle();
  assert.equal(cleanup.calls.filter(c=>c.name==='wifi_set_band_policy').length,1);
  assert.ok(find(cleanup.render(),'Checking WiFi connection'));
  cleanup.respond('wifi_set_band_policy',{success:true,message:'Cleared'});await settle();
  assert.equal(find(cleanup.render(),'Result'),undefined,'success waits for the actual final state');
  cleanup.respond('wifi_get_status',{...automaticWpa,live:{...wifi.live,wifi_backend:'wpa_supplicant',ssid:' spaced network '}});await settle();
  assert.ok(find(cleanup.render(),' spaced network '));
  assert.ok(find(cleanup.render(),'Result'));
  assert.equal(cleanup.render().props.children[0].props.title,'Current WiFi connection');
  cleanup.fire();cleanup.respond('wifi_get_status',{...automaticWpa,live:{...wifi.live,wifi_backend:'wpa_supplicant',connection_uuid:'new-user-profile'}});await settle();
  assert.equal(find(cleanup.render(),'Result'),undefined,'a changed user profile clears old success');
  cleanup.unmount();

  const mismatched=harness('wifi.tsx','WifiPage');mismatched.render();
  mismatched.respond('wifi_get_status',wifi);await settle();
  findButton(mismatched.render(),'Use Automatic WiFi').props.onClick();await settle();
  mismatched.respond('wifi_set_band_policy',{success:true,message:'Saved'});await settle();
  mismatched.respond('wifi_get_status',wifi);await settle();
  assert.equal(find(mismatched.render(),'Result'),undefined);
  assert.match(find(mismatched.render(),'Could not complete').props.description,/could not be verified/);
  mismatched.unmount();

  const roamed=harness('wifi.tsx','WifiPage');roamed.render();
  roamed.respond('wifi_get_status',automaticWpa);await settle();
  findButton(roamed.render(),'Connect to 6 GHz once').props.onClick();await settle();
  roamed.respond('wifi_rescan_and_reconnect',{success:true,message:'Connected to 6 GHz'});await settle();
  roamed.respond('wifi_get_status',{...automaticWpa,live:{...automaticWpa.live,frequency:'6295'}});await settle();
  assert.ok(find(roamed.render(),'Result'));
  roamed.fire();roamed.respond('wifi_get_status',automaticWpa);await settle();
  assert.equal(find(roamed.render(),'Result'),undefined,'roaming clears an obsolete 6 GHz success');
  roamed.unmount();

  for (const file of ['tdp.tsx', 'vibration.tsx']) {
    const h = harness(file, 'AppWatcher'), watcher = h.exported;
    watcher.start(); const request = h.calls[0];
    h.callbacks.lifetime(); const late = [...h.timers.values()].find(t => t.kind === 'timeout').fn;
    const resume = h.callbacks.resume; watcher.stop(); assert.equal(h.timers.size, 0);
    late(); resume(); assert.equal(h.calls.length, 1, `${file} callbacks cannot revive a stopped watcher`);
    watcher.start(); let received = 0; watcher.listen(() => received++);
    request.settled = true; request.resolve({ settings: {}, app_id: '111' }); await settle();
    assert.equal(received, 0, `${file} ignores a response from the previous lifecycle`);
    assert.equal(watcher.busy, true, `${file} old request cannot clear the new in-flight guard`);
    h.respond(file === 'tdp.tsx' ? 'set_active_app' : 'vibe_set_active_app', { settings: {}, app_id: '111' }); await settle();
    watcher.stop();
  }
  const overviewFailures = [];
  const modules = Object.fromEntries(['tdp', 'fan', 'vibration', 'display', 'wifi', 'rgb', 'remap', 'battery', 'controller']
    .map(name => [name, { enabled: true }]));
  const overviewValues = { get_version: { version: 'test', blocked: false, modules },
    fan_get_state: { supported: true, error: '', receivedAt: 100000, active_curve: 'global', settings: { enabled: false },
      readings: { valid: true, rpm: 1555, apu_temperature: 45, age_seconds: 0, automatic: true, error: '' } },
    get_settings: { spl: 23000, sppt: 30000, fppt: 35000 },
    vibe_get_settings: { settings: { level: 2, mode: 2, touchpadEnabled: false } },
    vibe_get_driver_status: { found: true }, display_get_state: display, wifi_get_status: wifi,
    rgb_get_status: rgb, remap_get_status: remap,
    battery_get_status: { success: true, supported: true, managed: true, enabled: true, requested_enabled: true },
    battery_get_controller_levels: { available: true, left: 75, right: 82, connection_left: 'attached', connection_right: 'attached' },
    controller_get_status: {} };
  const answerOverview = (h, except = []) => {
    for (const request of [...h.calls]) if (!request.settled && !except.includes(request.name)) {
      assert.ok(request.name in overviewValues, `known overview read ${request.name}`);
      request.settled = true; request.resolve(overviewValues[request.name]);
    }
  };
  const newOverview = async () => {
    const h = harness('index.tsx', 'Controls'), plugin = h.initialize();
    answerOverview(h); await settle(); h.calls.length = 0;
    h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.render({ modules: structuredClone(modules) });
    await settle();
    return h;
  };
  const testOverview = async (name, check) => {
    const h = await newOverview();
    try { await check(h); } catch (error) { overviewFailures.push(new Error(`${name}: ${error.message}`)); }
    finally { h.dispose(); }
  };
  const assertPowerSummary = h => assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 23 / 30 / 35 W');
  await testOverview('overview labels include confirmed controller levels and touchpad state', async h => {
    h.respond('get_settings', { spl: 20000, sppt: 32000, fppt: 35000, enabled: true, active_preset: 'performance' });
    answerOverview(h); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Performance · 20 / 32 / 35 W');
    assert.equal(findSection(h.render(), 'Vibration').props.description, 'Medium · Standard · Touchpad Off');
    assert.equal(findSection(h.render(), 'Battery').props.description,
      'Protection on · about 80% limit · L 75% · R 82%');
  });
  await testOverview('equivalent module objects preserve the pending overview read', async h => {
    h.render({ modules: structuredClone(modules) });
    answerOverview(h); await settle();
    assertPowerSummary(h);
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'equivalent props do not duplicate reads');
  });
  await testOverview('fast summaries appear while WiFi is still pending', async h => {
    answerOverview(h, ['wifi_get_status']); await settle();
    assert.ok(h.calls.some(c => c.name === 'wifi_get_status' && !c.settled));
    assertPowerSummary(h);
  });
  await testOverview('hide and reopen during a read retains its eventual summary', async h => {
    h.visible(false); h.visible(true);
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'reopening does not overlap the pending read');
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  await testOverview('remounted overview immediately shows the last confirmed summary', async h => {
    answerOverview(h); await settle(); assertPowerSummary(h);
    h.unmount();
    const remounted = h.remount();
    assert.equal(findSection(remounted, 'TDP').props.description, 'Custom · 23 / 30 / 35 W');
  });
  await testOverview('pending overview reads survive actual unmount/remount without duplication', async h => {
    h.unmount(); h.remount();
    await settle();
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1);
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  await testOverview('a read completed while hidden is cached for immediate reopening', async h => {
    h.visible(false); answerOverview(h); await settle();
    const reopened = h.visible(true);
    assert.equal(findSection(reopened, 'TDP').props.description, 'Custom · 23 / 30 / 35 W');
  });
  await testOverview('repeated and queued ticks never overlap pending reads or restart hidden polling', async h => {
    const queuedTicks = [...h.timers.values()].filter(t => t.kind === 'interval').map(t => t.fn);
    assert.ok(queuedTicks.length, 'visible overview schedules polling');
    const reads = h.calls.length;
    h.fire(); h.fire();
    assert.equal(h.calls.length, reads, 'pending overview reads remain single-flight');
    h.visible(false); queuedTicks.forEach(tick => tick());
    assert.equal(h.calls.length, reads, 'a queued tick cannot start hidden reads');
    answerOverview(h); await settle();
    h.fire(); queuedTicks.forEach(tick => tick()); await settle();
    assert.equal(h.calls.length, reads, 'completed hidden reads do not restart polling or retry');
  });
  await testOverview('a failed refresh preserves the previously confirmed TDP summary', async h => {
    answerOverview(h); await settle(); assertPowerSummary(h);
    h.fire();
    const refresh = h.calls.find(c => c.name === 'get_settings' && !c.settled);
    assert.ok(refresh, 'the next poll refreshes TDP');
    refresh.settled = true; refresh.reject(new Error('temporary RPC failure'));
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  const section = (h, title = 'TDP') => findSection(h.render(), title).props;
  const failPower = async h => { h.reject('get_settings'); answerOverview(h); await settle(); };
  await testOverview('an initial failure has a retry hint and recovers without leaving stale metadata', async h => {
    await failPower(h);
    assert.equal(section(h).description, 'Status unavailable');
    assert.match(section(h).statusNote, /Retrying while this menu is open/);
    assert.equal(section(h, 'Vibration').statusNote, undefined);
    h.advance(10000); h.fire(); answerOverview(h); await settle();
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('only repeated failures mark confirmed data older and the existing tick updates its age', async h => {
    answerOverview(h); await settle();
    h.advance(10000); h.fire(); await failPower(h);
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined, 'one transient failure is quiet');
    h.advance(10000); h.fire(); await failPower(h);
    assertPowerSummary(h); assert.match(section(h).statusNote, /Older data.*Last read 20s ago/);
    assert.equal(section(h, 'Vibration').statusNote, undefined, 'failure metadata stays with its own RPC');
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.match(section(h).statusNote, /Last read 30s ago/);
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined, 'a confirmed refresh clears the warning');
    assert.equal([...h.timers.values()].filter(timer => timer.delay === 1000).length, 1, 'only live fan telemetry has a fast timer');
    assert.ok([...h.timers.values()].every(timer => timer.delay === 1000 || timer.delay >= 10000));
  });
  await testOverview('fan uses real measured numbers, isolated live polling, stale/error clearing and hidden cleanup', async h => {
    answerOverview(h); await settle();
    assert.match(section(h,'Fan Control').description,/APU 45.0°C · 1555 RPM · Firmware automatic/);
    const timer=[...h.timers.values()].find(t=>t.delay===1000);assert.ok(timer);
    const count=h.calls.length;h.advance(1000);timer.fn();await settle();
    assert.deepEqual(h.calls.slice(count).map(c=>c.name),['fan_get_state'],'1s tick reads only Fan');
    h.advance(2000);timer.fn();await settle();
    assert.equal(h.calls.length,count+1,'hung fan read never overlaps');
    assert.match(section(h,'Fan Control').description,/APU — · — RPM/,'cached numbers expire while RPC hangs');
    h.reject('fan_get_state');await settle();assert.match(section(h,'Fan Control').description,/APU — · — RPM/);
    const before=h.calls.length;h.visible(false);assert.equal([...h.timers.values()].filter(t=>t.delay===1000).length,0);
    h.fire();await settle();assert.equal(h.calls.length,before,'hidden overview does no fan polling');
    h.visible(true);await settle();assert.equal(h.calls.filter(c=>c.name==='fan_get_state'&&!c.settled).length,1,'reopen requests a fresh fan read');
  });
  await testOverview('switching away from Decky tab stops polling even while QAM itself remains visible', async h => {
    answerOverview(h); await settle();const count=h.calls.length;
    h.panelVisible(false);assert.equal([...h.timers.values()].filter(t=>t.delay===1000).length,0,'hidden root removes live fan timer');
    h.fire();await settle();assert.equal(h.calls.length,count,'no new read behind the other QAM tab');
    h.panelVisible(true);await settle();assert.equal(h.calls.filter(c=>c.name==='fan_get_state'&&!c.settled).length,1,'actual root visibility restarts one fresh request');
    findSection(h.render(),'Fan Control').props.onClick();const page=h.render();
    h.panelVisible(false);assert.ok(findPanel(h.render(),'Fan Control')||page,'navigation remains mounted while polling pauses');
  });
  await testOverview('a hung cached read is marked delayed without starting overlapping RPCs', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    answerOverview(h, ['get_settings']); await settle();
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.doesNotMatch(section(h).statusNote || '', /Older data|delayed/);
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assertPowerSummary(h); assert.match(section(h).statusNote, /Older data.*Last read 40s ago.*Update delayed/);
    assert.equal(h.calls.filter(call => call.name === 'get_settings').length, 2);
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('a hung initial read becomes unavailable only after thirty visible seconds', async h => {
    answerOverview(h, ['get_settings']); await settle();
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.notEqual(section(h).description, 'Status unavailable');
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.equal(section(h).description, 'Status unavailable');
    assert.match(section(h).statusNote, /Still waiting/);
    assert.equal(section(h, 'Vibration').statusNote, undefined);
    assert.equal(h.calls.filter(call => call.name === 'get_settings').length, 1);
  });
  await testOverview('hidden time never turns a pending read into a failure', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    answerOverview(h, ['get_settings']); await settle(); h.advance(10000);
    const queuedTicks = [...h.timers.values()].filter(timer => timer.kind === 'interval').map(timer => timer.fn);
    h.visible(false); const before = h.calls.length;
    h.advance(3600000); queuedTicks.forEach(tick => tick());
    assert.equal(h.calls.length, before, 'hidden age checks never start work');
    h.visible(true);
    assert.match(section(h).statusNote, /^Updating….*Last read 1h ago/);
    assert.doesNotMatch(section(h).statusNote, /Older data|delayed/);
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.match(section(h).statusNote, /Older data.*Update delayed/, 'only accumulated visible waiting counts');
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('hidden rejections do not count toward a visible failure streak', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    h.visible(false); await failPower(h); h.advance(3600000); h.visible(true);
    await failPower(h);
    assert.equal(section(h).statusNote, undefined, 'the first visible error still tolerates a transient failure');
    h.advance(10000); h.fire(); await failPower(h);
    assert.match(section(h).statusNote, /Older data/);
  });
  await testOverview('fresh backend warnings replace old healthy data and survive later transport failures', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    h.respond('battery_get_status', { supported: false, reason: 'Battery firmware unavailable' });
    h.respond('controller_get_status', { available: true, recovery_pending: true, reason: 'Restore the interrupted controller change' });
    h.respond('wifi_get_status', { success: false, error: 'unexpected', message: 'Wireless settings could not be verified' });
    answerOverview(h); await settle();
    assert.equal(section(h, 'Battery').description, 'Battery firmware unavailable');
    assert.equal(section(h, 'Gyro & Touchpad').description, 'Restore the interrupted controller change');
    assert.equal(section(h, 'WiFi').description, 'Wireless settings could not be verified');
    for (let i = 0; i < 2; i++) {
      h.advance(10000); h.fire(); h.reject('battery_get_status'); answerOverview(h); await settle();
    }
    assert.equal(section(h, 'Battery').description, 'Battery firmware unavailable');
    assert.match(section(h, 'Battery').statusNote, /Older data/);
  });
  await testOverview('driver warnings are not masked by missing vibration settings', async h => {
    h.respond('vibe_get_driver_status', { found: false }); h.reject('vibe_get_settings');
    answerOverview(h); await settle();
    assert.equal(section(h, 'Vibration').description, 'Controller driver not detected');
    assert.match(section(h, 'Vibration').statusNote, /Retrying/);
  });
  for (const field of ['settings_error', 'setup_error']) {
    await testOverview(`OLED ${field} replaces its earlier healthy summary`, async h => {
      answerOverview(h); await settle(); h.advance(10000); h.fire();
      h.respond('display_get_state', { ...display, [field]: 'Display controls need attention' });
      answerOverview(h); await settle();
      assert.equal(section(h, 'OLED Display').description, 'Display controls need attention');
      h.advance(10000); h.fire(); answerOverview(h); await settle();
      assert.notEqual(section(h, 'OLED Display').description, 'Display controls need attention');
    });
  }
  for (const [state, expected] of [
    [nativeDisplay, 'Native display support · EDID standby'],
    [{ ...nativeDisplay, edid_patched: true }, 'Native display support · EDID fixed'],
    [{ ...nativeDisplay, native_cleanup_pending: true }, 'Display fix cleanup pending · EDID standby'],
    [{ ...nativeDisplay, native_restart_pending: true }, 'Restart required · EDID standby'],
    [{ ...nativeDisplay, native_cleanup_pending: true, native_cleanup_error: 'Cleanup could not be verified' }, 'Cleanup could not be verified'],
    [{ ...nativeDisplay, settings_error: 'Display settings unavailable' }, 'Display settings unavailable'],
  ]) {
    await testOverview(`native display summary: ${expected}`, async h => {
      h.respond('display_get_state', { ...state, setup_error: 'Obsolete setup warning' });
      answerOverview(h); await settle();
      assert.equal(section(h, 'OLED Display').description, expected);
    });
  }
  await testOverview('stale rejected page reads cannot seed the returned overview metadata', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    findSection(h.render(), 'TDP').props.onClick(); h.render();
    h.reject('get_settings'); await settle();
    findSection(h.render(), 'TDP', 'onBack').props.onBack(); h.render();
    await failPower(h);
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined, 'only the post-page error counts');
  });
  for (const gate of [{ enabled: false }, { enabled: true, pending: true }]) {
    await testOverview(`module gate ${JSON.stringify(gate)} clears freshness and ignores rejected prior reads`, async h => {
      answerOverview(h); await settle(); h.advance(10000); h.fire(); await failPower(h);
      h.advance(10000); h.fire(); await failPower(h);
      assert.match(section(h).statusNote, /Older data/);
      h.fire();
      const gated = { ...structuredClone(modules), tdp: gate };
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules: gated }); h.render({ modules: gated });
      h.reject('get_settings'); await settle();
      h.callbacks.companion_guard(overviewValues.get_version); h.render({ modules });
      assert.equal(section(h).statusNote, undefined);
      assert.notEqual(section(h).description, 'Custom · 23 / 30 / 35 W');
      answerOverview(h); await settle(); assertPowerSummary(h);
    });
  }
  await testOverview('an old plugin rejection cannot add freshness warnings to its replacement', async h => {
    const oldRead = h.calls.find(call => call.name === 'get_settings' && !call.settled);
    h.dispose(); h.advance(3600000);
    const plugin = h.initialize(); h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.respond('get_version', overviewValues.get_version); await settle(); h.remount();
    oldRead.settled = true; oldRead.reject(new Error('old plugin disconnected')); await settle();
    assert.equal(section(h).statusNote, undefined);
    assert.notEqual(section(h).description, 'Status unavailable');
    answerOverview(h); await settle(); assertPowerSummary(h);
  });
  await testOverview('late replies from a disposed plugin cannot populate or unlock the new instance', async h => {
    const oldRead = h.calls.find(c => c.name === 'get_settings' && !c.settled);
    h.dispose();
    const plugin = h.initialize();
    h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.respond('get_version', overviewValues.get_version); await settle();
    h.remount(); await settle();
    const currentRead = h.calls.find(c => c.name === 'get_settings' && !c.settled && c !== oldRead);
    assert.ok(currentRead, 'the new plugin starts its own read');
    oldRead.settled = true; oldRead.resolve(overviewValues.get_settings); await settle();
    assert.notEqual(findSection(h.render(), 'TDP').props.description, 'Custom · 23 / 30 / 35 W', 'the disposed plugin cannot seed the new cache');
    h.fire();
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 2, 'the old reply cannot clear the new single-flight guard');
    currentRead.settled = true; currentRead.resolve({ spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 / 31 / 38 W');
  });
  for (const state of [{ enabled: false }, { enabled: true, pending: true }]) {
    await testOverview(`real module gate ${JSON.stringify(state)} rejects stale status and stops its reads`, async h => {
      const gated = { ...structuredClone(modules), tdp: state };
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules: gated });
      assert.equal(findSection(h.render({ modules: gated }), 'TDP'), undefined);
      answerOverview(h); await settle(); h.fire();
      assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'gated modules are not polled');
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules });
      const reopened = h.render({ modules });
      assert.notEqual(findSection(reopened, 'TDP').props.description, 'Custom · 23 / 30 / 35 W', 'old gated reply never enters the cache');
      await settle();
      h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
      assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 / 31 / 38 W');
    });
  }
  await testOverview('entering a module invalidates old overview values without blocking fresh reads on return', async h => {
    findSection(h.render(), 'TDP').props.onClick(); h.render();
    h.respond('get_settings', overviewValues.get_settings); await settle();
    findSection(h.render(), 'TDP', 'onBack').props.onBack();
    const returned = h.render();
    assert.notEqual(findSection(returned, 'TDP').props.description, 'Custom · 23 / 30 / 35 W', 'a pre-edit read cannot become the returned page summary');
    await settle();
    h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 / 31 / 38 W');
    answerOverview(h); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 / 31 / 38 W', 'the remainder of the older overview cannot overwrite newer power state');
  });
  if (overviewFailures.length) throw new AggregateError(overviewFailures, 'Overview loading regressions');
  console.log('OLED/RGB/remap/WiFi polling is bounded; stale reads and stopped watchers cannot overwrite current state; hidden dropdown choices still save.');
  console.log('Overview survives equivalent props, slow WiFi, visibility changes and remounts without losing confirmed summaries.');
})().catch(error => { console.error(error); process.exitCode = 1; });
