// Run the actual components with controllable RPC completion order.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
function harness(file, component, initiallyVisible = true) {
  const slots = [], effects = new Map(), pending = [], timers = new Map(), calls = [], callbacks = {};
  let cursor = 0, visible = initiallyVisible, nextTimer = 0, writes = 0, props = {}, now = 100000;
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
  const relativeReads = { getWifiStatus: 'wifi_get_status', getRgbStatus: 'rgb_get_status',
    getRemapStatus: 'remap_get_status', getBatteryStatus: 'battery_get_status', getControllerStatus: 'controller_get_status' };
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
        if (/^(start|stop)/.test(key)) return () => {};
        if (key.endsWith('Summary')) return () => '';
        return key;
      } });
      throw Error(name);
    },
  });
  const render = (nextProps = props) => { props = nextProps; cursor = 0; const result = mod.exports.Tested(props); pending.splice(0).forEach(fn => fn()); return result; };
  return { calls, timers, callbacks, render, exported: mod.exports.Tested,
    initialize: () => mod.exports.default(),
    get writes() { return writes; },
    advance(milliseconds) { now += milliseconds; },
    visible(value) { visible = value; return render(); },
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
function findSection(node, title, action = 'onClick') {
  if (!node || typeof node !== 'object') return;
  if (node.props?.title === title && node.props?.[action]) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const found = findSection(child, title, action); if (found) return found;
  }
}
const rgb = { success: true, settings: { configured: true, control_enabled: true, rings_enabled: true, effect: 'monocolor',
  hue: 255, saturation: 100, brightness: 50, speed: 50 }, rgb: { supported: true, effects: [] }, power_led: { supported: false } };
const remap = { supported: true, enabled: true, desktop_action: 'f1', page_action: 'f2', actions: [{ id: 'f1', label: 'F1' }, { id: 'f3', label: 'F3' }] };
const display = { panel_mode: 'pq', active_mode: 'pq', setup_done: true, panel_ok: true, enabled: true, edid_fix: true };
const wifi = { success: true, settings: { device_family: 'legion_go_2', driver: 'mt7921e', band_preference_enabled: true }, live: {} };
const cases = [
  ['display.tsx', 'DisplayPage', 'display_get_state', display],
  ['rgb.tsx', 'RgbPage', 'rgb_get_status', rgb],
  ['remap.tsx', 'RemapPage', 'remap_get_status', remap],
  ['wifi.tsx', 'WifiPage', 'wifi_get_status', wifi],
];
(async () => {
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
  const preference = find(network.render(), 'Prefer 5/6 GHz'); network.fire(); preference.props.onChange(false); await settle();
  network.respond('wifi_set_band_preference', { success: true, message: 'Saved' }); await settle();
  const beforeOldRead = network.writes;
  network.respond('wifi_get_status', wifi); await settle();
  assert.equal(network.writes, beforeOldRead, 'old WiFi status cannot overwrite the mutation');
  assert.equal(network.calls.filter(c => c.name === 'wifi_get_status').length, 3, 'WiFi confirms status after the earlier read finishes');
  network.respond('wifi_get_status', { ...wifi, settings: { ...wifi.settings, band_preference_enabled: false } }); await settle();
  assert.equal(find(network.render(), 'Prefer 5/6 GHz').props.checked, false); network.unmount();

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
  const modules = Object.fromEntries(['tdp', 'vibration', 'display', 'wifi', 'rgb', 'remap', 'battery', 'controller']
    .map(name => [name, { enabled: true }]));
  const overviewValues = { get_version: { version: 'test', blocked: false, modules },
    get_settings: { spl: 23000, sppt: 30000, fppt: 35000 }, vibe_get_settings: { settings: { level: 2, mode: 2 } },
    vibe_get_driver_status: { found: true }, display_get_state: display, wifi_get_status: wifi,
    rgb_get_status: rgb, remap_get_status: remap, battery_get_status: {}, controller_get_status: {} };
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
  const assertPowerSummary = h => assert.equal(findSection(h.render(), 'TDP').props.description, '23 / 30 / 35 W');
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
    assert.equal(findSection(remounted, 'TDP').props.description, '23 / 30 / 35 W');
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
    assert.equal(findSection(reopened, 'TDP').props.description, '23 / 30 / 35 W');
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
    assert.ok([...h.timers.values()].every(timer => timer.delay >= 10000), 'freshness adds no fast timer');
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
      assert.notEqual(section(h).description, '23 / 30 / 35 W');
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
    assert.notEqual(findSection(h.render(), 'TDP').props.description, '23 / 30 / 35 W', 'the disposed plugin cannot seed the new cache');
    h.fire();
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 2, 'the old reply cannot clear the new single-flight guard');
    currentRead.settled = true; currentRead.resolve({ spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, '28 / 31 / 38 W');
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
      assert.notEqual(findSection(reopened, 'TDP').props.description, '23 / 30 / 35 W', 'old gated reply never enters the cache');
      await settle();
      h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
      assert.equal(findSection(h.render(), 'TDP').props.description, '28 / 31 / 38 W');
    });
  }
  await testOverview('entering a module invalidates old overview values without blocking fresh reads on return', async h => {
    findSection(h.render(), 'TDP').props.onClick(); h.render();
    h.respond('get_settings', overviewValues.get_settings); await settle();
    findSection(h.render(), 'TDP', 'onBack').props.onBack();
    const returned = h.render();
    assert.notEqual(findSection(returned, 'TDP').props.description, '23 / 30 / 35 W', 'a pre-edit read cannot become the returned page summary');
    await settle();
    h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, '28 / 31 / 38 W');
    answerOverview(h); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, '28 / 31 / 38 W', 'the remainder of the older overview cannot overwrite newer power state');
  });
  if (overviewFailures.length) throw new AggregateError(overviewFailures, 'Overview loading regressions');
  console.log('OLED/RGB/remap/WiFi polling is bounded; stale reads and stopped watchers cannot overwrite current state; hidden dropdown choices still save.');
  console.log('Overview survives equivalent props, slow WiFi, visibility changes and remounts without losing confirmed summaries.');
})().catch(error => { console.error(error); process.exitCode = 1; });
