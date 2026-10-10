// Exercise the real TDP page with delayed responses and game/visibility changes.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const caps = { min: 5, std: { spl: 35, sppt: 37, fppt: 45 }, max: { spl: 50, sppt: 50, fppt: 50 },
  wmi: true, extras: true, tdp_available: true, helper_retry_available: false };
const profile = (spl, ac = false) => ({ exists: true,
  profile: { spl: spl * 1000, sppt: (spl + 3) * 1000, fppt: (spl + 8) * 1000, preset: 'custom' },
  ac_separate: ac, ac_profile: { spl: (spl + 2) * 1000, sppt: (spl + 5) * 1000,
    fppt: (spl + 10) * 1000, ac_preset: 'custom' } });
function mount(options = {}) {
  const slots = [], effects = new Map(), pendingEffects = [], calls = [], timers = new Map(), toasts = [];
  const deferred = new Map(), responses = { is_ready: { ready: true, error: '' },
    get_settings: { enabled: true, spl: 15000, sppt: 18000, fppt: 23000, active_preset: 'custom' },
    get_power_source: { ac: false }, get_extras_unlocked: false, get_caps: caps,
    get_game_profile: id => id === '222' ? profile(22, true) : options.firstProfile ?? profile(11),
    ...options.responses };
  const router = { MainRunningApp: options.game ? { appid: options.game, display_name: `Game ${options.game}` } : null };
  let cursor = 0, nextTimer = 0, visible = options.visible !== false, writes = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => v === b[i]);
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++, previous = effects.get(i); if (!previous || !same(previous.deps, deps)) pendingEffects.push(() => {
      previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
    }); },
  };
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  const mod = { exports: {} }, jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const source = fs.readFileSync(path.join(root, 'src/tdp.tsx'), 'utf8') + '\nexport { AppWatcher };';
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
    module: mod, exports: mod.exports, console: { ...console, error() {}, warn() {} }, window: {},
    setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === '@decky/ui') return new Proxy({ Router: router, findModuleExport() {} }, { get: (o, k) => k in o ? o[k] : k });
      if (name === '@decky/api') return { useQuickAccessVisible: () => visible, toaster: { toast: value => toasts.push(value) },
        addEventListener() {}, removeEventListener() {}, callable: name => (...args) => {
          const call = { name, args }; calls.push(call);
          if (deferred.get(name) > 0) {
            deferred.set(name, deferred.get(name) - 1);
            return new Promise((resolve, reject) => Object.assign(call, { resolve, reject }));
          }
          const response = responses[name] ?? { success: true };
          try { return Promise.resolve(typeof response === 'function' ? response(...args) : structuredClone(response)); }
          catch (error) { return Promise.reject(error); }
        } };
      throw Error(name);
    },
  });
  mod.exports.AppWatcher.start();
  const render = () => { cursor = 0; const tree = mod.exports.TdpPage(); pendingEffects.splice(0).forEach(fn => fn()); return tree; };
  return { calls, timers, toasts, responses, render, get writes() { return writes; },
    defer(name, count = 1) { deferred.set(name, count); },
    pending(name) { const call = calls.find(c => c.name === name && c.resolve && !c.settled); assert.ok(call, `pending ${name}`); return call; },
    resolve(call, value) { call.settled = true; call.resolve(value); },
    reject(call, message = 'old game request failed') { call.settled = true; call.reject(new Error(message)); },
    async game(id) { router.MainRunningApp = id ? { appid: id, display_name: `Game ${id}` } : null;
      await mod.exports.AppWatcher.check(); render(); await settle(); return render(); },
    visible(value) { visible = value; return render(); },
    fire(kind, delay) { for (const [id, t] of [...timers]) if (t.kind === kind && t.delay === delay) {
      if (kind === 'timeout') timers.delete(id); t.fn();
    } },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); mod.exports.AppWatcher.stop(); },
  };
}
function find(node, predicate) {
  if (!node || typeof node !== 'object') return;
  if (predicate(node)) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) { const found = find(child, predicate); if (found) return found; }
}
const field = (tree, label) => find(tree, n => n.props?.label === label);
const button = (tree, text) => find(tree, n => n.type === 'ButtonItem' && n.props?.children === text);
const cpu = tree => find(tree, n => typeof n.type === 'function' && n.type.name === 'CpuPowerControlsSection');
async function ready(h) { h.render(); await settle(); h.render(); await settle(); return h.render(); }
const count = (h, name) => h.calls.filter(c => c.name === name).length;
let tests = 0;
async function test(name, fn) { await fn(); tests++; console.log(`TDP recovery: ${name}`); }
(async () => {
  await test('auto-apply keeps admission closed until hardware finishes', async () => {
    const h=mount({game:111});await ready(h);
    const stale=button(h.render(),'Apply & Save for Game 111');assert.ok(stale);
    h.defer('apply_tdp');await h.game(222);await settle();
    assert.equal(cpu(h.render()),undefined);const before=count(h,'apply_tdp');
    stale.props.onClick();button(h.render(),'Applying...').props.onClick();await settle();
    assert.equal(count(h,'apply_tdp'),before);
    h.resolve(h.pending('apply_tdp'),{success:true});await settle();assert.ok(cpu(h.render()));
    stale.props.onClick();await settle();assert.equal(count(h,'apply_tdp'),before,'old handlers stay stale after the fresh editor is ready');
    h.unmount();
  });
  await test('failed game apply still restores fresh global limits on exit', async () => {
    const h=mount({game:111,responses:{apply_tdp:{success:false,stderr:'readback failed'}}});await ready(h);
    h.responses.apply_tdp={success:true};h.responses.get_settings={enabled:true,spl:31000,sppt:34000,fppt:39000,active_preset:'custom'};
    await h.game(null);await settle(); assert.equal(h.calls.filter(c=>c.name==='apply_tdp').at(-1).args[0],31000);
    assert.equal(cpu(h.render()).props.appId,'');assert.equal(field(h.render(),'Could not apply TDP profile'),undefined);h.unmount();
  });
  await test('capability changes cannot complete an unadopted global read', async () => {
    const h = mount({game:111,responses:{get_caps:{...caps,extras:false,helper_retry_available:true}}}); await ready(h);
    h.defer('get_settings'); await h.game(null); const old = h.pending('get_settings');
    h.defer('get_settings', 2);
    h.responses.get_caps = {...caps,extras:false,wmi:false,tdp_available:false,helper_retry_available:true};
    h.fire('interval',5000); await settle(); h.render(); await settle();
    assert.equal(cpu(h.render()),undefined);
    const unavailable = h.calls.filter(c=>c.name==='get_settings'&&c.resolve&&!c.settled).at(-1);
    h.responses.get_caps = {...caps,extras:false,helper_retry_available:true};
    h.fire('interval',5000); await settle(); h.render(); await settle();
    assert.equal(cpu(h.render()),undefined);
    const fresh = h.calls.filter(c=>c.name==='get_settings'&&c.resolve&&!c.settled).at(-1);
    h.resolve(old,{enabled:true,spl:16000,sppt:19000,fppt:24000,active_preset:'custom'});
    h.resolve(unavailable,{enabled:true,spl:18000,sppt:21000,fppt:26000,active_preset:'custom'});
    await settle(); assert.equal(cpu(h.render()),undefined);
    h.resolve(fresh,{enabled:true,spl:31000,sppt:34000,fppt:39000,active_preset:'custom'});
    await settle(); assert.ok(field(h.render(),'SPL (TDP) - 31 W'));
    assert.ok(cpu(h.render())); assert.equal(h.calls.filter(c=>c.name==='apply_tdp').at(-1).args[0],31000);
    h.unmount();
  });
  await test('failed auto-apply preserves fresh CPU context and helper recovery', async () => {
    const h = mount({game:111,responses:{get_caps:{...caps,extras:false,helper_retry_available:true},
      apply_tdp:{success:false,stderr:'firmware denied; helper missing'}}}); const view = await ready(h);
    assert.equal(field(view,'Could not load profile'),undefined);
    assert.ok(field(view,'Could not apply TDP profile')); assert.equal(cpu(view).props.appId,'111');
    const recovery=button(view,'Retry Extras download'); assert.equal(recovery.props.disabled,false);
    h.responses.get_caps=caps; recovery.props.onClick(); await settle(); assert.equal(count(h,'retry_extras'),1);
    h.responses.apply_tdp={success:true}; button(h.render(),'Retry profile').props.onClick(); await settle(); h.render(); await settle();
    assert.equal(field(h.render(),'Could not apply TDP profile'),undefined); assert.ok(cpu(h.render())); h.unmount();
  });
  await test('helper recovery remains available after profile read failure', async () => {
    const h=mount({game:111,responses:{get_caps:{...caps,extras:false,helper_retry_available:true},
      get_game_profile:()=>{throw Error('profile read offline');}}}); await ready(h);
    assert.ok(field(h.render(),'Could not load profile')); assert.equal(cpu(h.render()),undefined);
    const recovery=button(h.render(),'Retry Extras download'); assert.equal(recovery.props.disabled,false);
    recovery.props.onClick(); await settle(); assert.equal(count(h,'retry_extras'),1); h.unmount();
  });
  await test('first readiness failure is actionable and double retry is coalesced', async () => {
    const h = mount(); h.defer('is_ready'); h.render(); h.reject(h.pending('is_ready'), 'Decky offline'); await settle();
    const view = h.render(); assert.match(field(view, 'Error').props.description, /Decky offline/);
    const retry = button(view, 'Retry status'); assert.ok(retry);
    h.defer('is_ready'); retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(count(h, 'is_ready'), 2);
    h.resolve(h.pending('is_ready'), { ready: true }); await settle();
    assert.ok(field(h.render(), 'Enable')); assert.equal(button(h.render(), 'Retry status'), undefined);
    h.unmount();
  });
  await test('failed initial settings are shown before sibling reads finish', async () => {
    const h = mount(); h.defer('get_settings'); h.defer('get_power_source'); h.render(); await settle();
    h.reject(h.pending('get_settings'), 'settings unreadable'); await settle();
    assert.match(field(h.render(), 'Error').props.description, /settings unreadable/);
    button(h.render(), 'Retry status').props.onClick(); await settle();
    assert.equal(count(h, 'get_power_source'), 1, 'a still-running sibling read is not duplicated');
    h.resolve(h.pending('get_power_source'), { ac: false }); await settle();
    button(h.render(), 'Retry status').props.onClick(); await settle(); assert.ok(field(h.render(), 'Enable')); h.unmount();
  });
  await test('automatic initialization retry is bounded and stops when hidden or unmounted', async () => {
    const h = mount({ responses: { is_ready: () => { throw Error('offline'); } } }); await ready(h);
    for (let i = 0; i < 5; i++) { h.fire('timeout', 1000); await settle(); }
    assert.equal(count(h, 'is_ready'), 3);
    h.visible(false); h.fire('timeout', 1000); await settle(); assert.equal(count(h, 'is_ready'), 3);
    h.visible(true); await settle(); assert.equal(count(h, 'is_ready'), 4);
    const queued = [...h.timers.values()].filter(t => t.kind === 'timeout'); h.unmount();
    queued.forEach(t => t.fn()); await settle(); assert.equal(count(h, 'is_ready'), 4);
    const hidden = mount({ visible: false }); hidden.render(); await settle(); assert.equal(count(hidden, 'is_ready'), 0); hidden.unmount();
  });
  for (const failure of [false, true]) await test(`old per-game toggle ${failure ? 'failure' : 'missing profile'} cannot change the new game`, async () => {
    const h = mount({ game: 111, firstProfile: { exists: false } }); await ready(h);
    h.defer('get_game_profile'); field(h.render(), 'Per Game Profile').props.onChange(true); await settle();
    const old = h.pending('get_game_profile'); await h.game(222);
    const before = JSON.stringify(h.render()), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, { exists: false }); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(h.toasts.length, toasts);
    assert.equal(field(h.render(), 'Per Game Profile').props.checked, true); h.unmount();
  });
  for (const failure of [false, true]) await test(`old profile delete ${failure ? 'failure' : 'success'} cannot change the new game`, async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('delete_game_profile');
    field(h.render(), 'Per Game Profile').props.onChange(false); await settle(); const old = h.pending('delete_game_profile');
    await h.game(222); const before = JSON.stringify(h.render()), settingsReads = count(h, 'get_settings'), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, { success: true }); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(count(h, 'get_settings'), settingsReads);
    assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('global settings read after delete cannot overwrite the next game', async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('get_settings');
    field(h.render(), 'Per Game Profile').props.onChange(false); await settle(); const old = h.pending('get_settings');
    await h.game(222); const before = JSON.stringify(h.render());
    h.resolve(old, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    assert.equal(JSON.stringify(h.render()), before); h.unmount();
  });
  for (const failure of [false, true]) await test(`old preset ${failure ? 'failure' : 'success'} cannot restore old sliders or status`, async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('apply_tdp');
    button(h.render(), 'Performance').props.onClick(); await settle(); const old = h.pending('apply_tdp');
    await h.game(222); const before = JSON.stringify(h.render()), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, { success: true }); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('old advanced-control refresh and AC errors are scoped to the requested game', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.defer('get_settings'); field(h.render(), 'Advanced TDP Control').props.onChange(true); await settle();
    const oldRead = h.pending('get_settings'); await h.game(222); const before = JSON.stringify(h.render());
    h.resolve(oldRead, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    assert.equal(JSON.stringify(h.render()), before);
    h.defer('set_game_ac_profile'); field(h.render(), 'Separate AC Profile').props.onChange(false); await settle();
    const oldAc = h.pending('set_game_ac_profile'); await h.game(111); const next = JSON.stringify(h.render()), toasts = h.toasts.length;
    h.reject(oldAc); await settle(); assert.equal(JSON.stringify(h.render()), next); assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('returning to the same app and unmounting do not revive old responses', async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('apply_tdp'); button(h.render(), 'Performance').props.onClick();
    const old = h.pending('apply_tdp'); await h.game(222); await h.game(111); const before = JSON.stringify(h.render());
    h.resolve(old, { success: true }); await settle(); assert.equal(JSON.stringify(h.render()), before);
    h.defer('apply_tdp'); button(h.render(), 'Performance').props.onClick(); const pending = h.pending('apply_tdp');
    h.unmount(); const writes = h.writes; h.resolve(pending, { success: true }); await settle(); assert.equal(h.writes, writes);
  });
  await test('game notification still clears old status when an earlier response already observed the new app', async () => {
    const h = mount({ game: 111, responses: { get_game_profile: { exists: false } } }); await ready(h);
    field(h.render(), 'Per Game Profile').props.onChange(true); await settle();
    assert.match(JSON.stringify(h.render()), /No saved profile for Game 111/);
    h.defer('delete_game_profile'); field(h.render(), 'Per Game Profile').props.onChange(false); await settle();
    const old = h.pending('delete_game_profile'); h.defer('set_active_app'); const change = h.game(222);
    await settle(); h.reject(old); await settle(); // Router changed before its backend report completed.
    h.resolve(h.pending('set_active_app'), { success: true }); await change;
    assert.doesNotMatch(JSON.stringify(h.render()), /No saved profile for Game 111|old game request failed/); h.unmount();
  });
  await test('old initial snapshot is discarded and the new game initializes from a fresh snapshot', async () => {
    const h = mount({ game: 111 }); h.defer('get_settings'); h.render(); await settle(); const old = h.pending('get_settings');
    await h.game(222); h.resolve(old, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    h.render(); await settle(); const view = h.render();
    assert.ok(field(view, 'SPL (TDP) - 22 W')); assert.equal(count(h, 'is_ready'), 2); h.unmount();
  });
  await test('a power read from before the game transition cannot change the new game context', async () => {
    const h = mount({ game: 111 }); await ready(h); h.visible(false); h.defer('get_power_source'); h.visible(true);
    await settle(); const old = h.pending('get_power_source'); await h.game(222); const before = JSON.stringify(h.render());
    h.resolve(old, { ac: true }); await settle(); assert.equal(JSON.stringify(h.render()), before); h.unmount();
  });
  await test('unavailable TDP disables writes but exposes fresh-profile helper retry and CPU game scope', async () => {
    const unavailable = { ...caps, wmi: false, extras: false, tdp_available: false,
      tdp_error: 'No firmware interface; helper download failed: offline', helper_retry_available: true };
    const h = mount({ game: 111, responses: { get_caps: unavailable } }); await ready(h);
    let view = h.render(); assert.equal(field(view, 'Enable').props.disabled, true);
    assert.equal(button(view, 'Apply & Save for Game 111').props.disabled, true);
    assert.equal(button(view, 'Performance').props.disabled, true);
    assert.match(field(view, 'TDP control unavailable').props.description, /offline/);
    assert.ok(button(view, 'Retry Extras download')); assert.ok(cpu(view));
    const applies = count(h, 'apply_tdp');
    assert.equal(field(view, 'Per Game Profile').props.disabled, false);
    field(view, 'Per Game Profile').props.onChange(true); await settle(); view = h.render();
    assert.equal(cpu(view).props.appId, '111'); assert.equal(count(h, 'apply_tdp'), applies);
    h.defer('retry_extras'); const retry = button(view, 'Retry Extras download'); retry.props.onClick(); retry.props.onClick();
    assert.equal(count(h, 'retry_extras'), 1);
    h.responses.get_caps = caps; h.resolve(h.pending('retry_extras'), { success: true }); await settle();
    assert.equal(field(h.render(), 'Enable').props.disabled, false); h.unmount();
  });
  await test('background helper capability is adopted by one visible read and hidden reads are ignored', async () => {
    const unavailable = { ...caps, wmi: false, extras: false, tdp_available: false, helper_retry_available: true };
    const h = mount({ responses: { get_caps: unavailable } }); await ready(h);
    h.defer('get_caps'); h.fire('interval', 5000); h.fire('interval', 5000); await settle();
    assert.equal(count(h, 'get_caps'), 2); const old = h.pending('get_caps'); h.visible(false);
    h.resolve(old, caps); await settle(); assert.equal(field(h.render(), 'Enable').props.disabled, true);
    const reads = count(h, 'get_caps'); h.fire('interval', 5000); assert.equal(count(h, 'get_caps'), reads);
    h.responses.get_caps = caps; h.visible(true); h.fire('interval', 5000); await settle();
    assert.equal(field(h.render(), 'Enable').props.disabled, false); h.render();
    assert.equal([...h.timers.values()].filter(t => t.kind === 'interval' && t.delay === 5000).length, 0); h.unmount();
  });
  await test('legacy capability replies retain compatible availability', async () => {
    const legacy = mount({ responses: { get_caps: {} } }); await ready(legacy);
    assert.equal(field(legacy.render(), 'Enable').props.disabled, false); legacy.unmount();
    const missing = mount({ responses: { get_caps: { ...caps, tdp_available: undefined, wmi: false, extras: false } } });
    await ready(missing); assert.equal(field(missing.render(), 'Enable').props.disabled, true); missing.unmount();
  });
  await test('pending game profile blocks stale Apply and accepts the fresh saved profile', async () => {
    const h = mount({ game: 111 }); await ready(h);
    const oldApply = button(h.render(), 'Apply & Save for Game 111').props.onClick;
    h.defer('get_game_profile'); await h.game(222);
    const before = count(h, 'apply_tdp'), view = h.render();
    assert.equal(field(view, 'Per Game Profile').props.checked, false);
    const apply = find(view, n => n.type === 'ButtonItem' && /Apply|Save/.test(n.props?.children));
    assert.equal(apply.props.disabled, true);
    apply.props.onClick(); oldApply(); await settle();
    assert.equal(count(h, 'apply_tdp'), before);
    h.resolve(h.pending('get_game_profile'), profile(22, true)); await settle();
    assert.ok(field(h.render(), 'SPL (TDP) - 22 W'));
    assert.equal(button(h.render(), 'Apply & Save for Game 222').props.disabled, false);
    h.unmount();
  });
  await test('missing game profile adopts fresh global limits and never inherits the old game', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.responses.get_game_profile = () => ({ exists: false });
    h.responses.get_settings = { enabled: true, spl: 15000, sppt: 18000, fppt: 23000, active_preset: 'custom' };
    await h.game(222); const view = h.render();
    assert.equal(field(view, 'Per Game Profile').props.checked, false);
    assert.ok(field(view, 'SPL (TDP) - 15 W'));
    button(view, 'Apply TDP').props.onClick(); await settle();
    const last = h.calls.filter(c => c.name === 'apply_tdp').at(-1);
    assert.deepEqual(Array.from(last.args), [15000, 18000, 23000, '', 'custom', '222']);
    h.unmount();
  });
  await test('failed profile read blocks writes and an explicit retry restores the editor', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.responses.get_game_profile = () => { throw Error('profile offline'); };
    await h.game(222); const view = h.render();
    assert.match(field(view, 'Could not load profile').props.description, /profile offline/);
    const before = count(h, 'apply_tdp');
    const apply = find(view, n => n.type === 'ButtonItem' && /Apply/.test(n.props?.children));
    apply.props.onClick(); await settle();
    assert.equal(count(h, 'apply_tdp'), before); assert.equal(cpu(view), undefined);
    h.responses.get_game_profile = () => profile(22, true);
    button(view, 'Retry profile').props.onClick(); button(view, 'Retry profile').props.onClick();
    h.render(); await settle(); h.render(); await settle();
    assert.ok(field(h.render(), 'SPL (TDP) - 22 W')); assert.ok(cpu(h.render())); h.unmount();
  });
  await test('CPU battery and AC profile selection works without a TDP provider', async () => {
    const h = mount({ game: 222, responses: { get_caps: { ...caps, tdp_available: false, wmi: false, extras: false } } });
    let view = await ready(h);
    assert.equal(field(view, 'Per Game Profile').props.checked, true);
    assert.equal(field(view, 'Separate AC Profile').props.checked, true);
    assert.equal(button(view, 'AC profile').props.disabled, false);
    button(view, 'AC profile').props.onClick(); view = h.render();
    assert.equal(cpu(view).props.appId, '222'); assert.equal(cpu(view).props.acProfile, true);
    button(view, 'Battery profile').props.onClick(); view = h.render();
    assert.equal(cpu(view).props.acProfile, false); assert.equal(count(h, 'apply_tdp'), 0);
    assert.equal(count(h, 'set_game_ac_profile'), 0); h.unmount();
  });
  await test('late profile metadata cannot revive an earlier visit to the same app or an unmounted page', async () => {
    const h = mount({ game: 111 }); h.defer('get_game_profile'); await ready(h);
    const old = h.pending('get_game_profile'); await h.game(222); await h.game(111);
    const before = JSON.stringify(h.render()), applies = count(h, 'apply_tdp');
    h.resolve(old, profile(49)); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(count(h, 'apply_tdp'), applies);
    h.defer('get_game_profile'); await h.game(222); const pending = h.pending('get_game_profile');
    h.unmount(); const writes = h.writes, mutations = count(h, 'apply_tdp');
    h.resolve(pending, profile(49)); await settle();
    assert.equal(h.writes, writes); assert.equal(count(h, 'apply_tdp'), mutations);
  });
  await test('missing profile plus failed global read stays blocked until fresh retry', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.responses.get_game_profile = () => ({ exists: false });
    h.responses.get_settings = () => { throw Error('global settings unavailable'); };
    await h.game(222); let view = h.render();
    assert.match(field(view, 'Could not load profile').props.description, /global settings unavailable/);
    assert.equal(cpu(view), undefined);
    h.responses.get_settings = { enabled: true, spl: 15000, sppt: 18000, fppt: 23000, active_preset: 'custom' };
    button(view, 'Retry profile').props.onClick(); h.render(); await settle(); view = h.render();
    assert.equal(field(view, 'Per Game Profile').props.checked, false);
    assert.ok(field(view, 'SPL (TDP) - 15 W')); assert.ok(cpu(view)); h.unmount();
  });
  console.log(`Passed ${tests} TDP recovery regressions.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
