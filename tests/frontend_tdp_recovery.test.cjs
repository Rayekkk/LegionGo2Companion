// Exercise the real TDP page with delayed responses and game/visibility changes.
const { assert, run, settle: wait, mount: load, find, label: field, button, component, nodes } = require('./legion_harness.cjs');
const settle = () => wait(24);
const caps = { min: 5, std: { spl: 35, sppt: 37, fppt: 45 }, max: { spl: 50, sppt: 50, fppt: 50 },
  wmi: true, extras: true, tdp_available: true, helper_retry_available: false };
const profile = (spl, ac = false) => ({ exists: true,
  profile: { spl: spl * 1000, sppt: (spl + 3) * 1000, fppt: (spl + 8) * 1000, preset: 'custom' },
  ac_separate: ac, ac_profile: { spl: (spl + 2) * 1000, sppt: (spl + 5) * 1000,
    fppt: (spl + 10) * 1000, ac_preset: 'custom' } });
function mount(options = {}) {
  const toasts = [], modals = [];
  const router = { MainRunningApp: options.game ? { appid: options.game, display_name: `Game ${options.game}` } : null };
  const h = load('tdp.tsx', { component: 'TdpPage', append: 'export { AppWatcher };', visible: options.visible,
    globals: { console: { ...console, error() {}, warn() {} } },
    ui: { Router: router, findModuleExport() {}, showModal: node => { modals.push(node); } }, api: { toaster: { toast: value => toasts.push(value) } },
    responses: { is_ready: { ready: true, error: '' },
      get_settings: { enabled: true, spl: 15000, sppt: 18000, fppt: 23000, active_preset: 'custom' },
      get_power_source: { ac: false }, get_extras_unlocked: false, get_caps: caps,
      get_game_profile: id => id === '222' ? profile(22, true) : options.firstProfile ?? profile(11),
      ...options.responses } });
  h.exports.AppWatcher.start();
  return { calls: h.calls, timers: h.timers, toasts, modals, responses: h.responses, render: () => h.render(), get writes() { return h.writes; },
    defer: h.defer, pending: h.pending,
    resolve(call, value) { h.settle(call, value); },
    reject(call, message = 'old game request failed') { h.settle(call, new Error(message), true); },
    async game(id) { router.MainRunningApp = id ? { appid: id, display_name: `Game ${id}` } : null;
      await h.exports.AppWatcher.check(); h.render(); await settle(); return h.render(); },
    visible: h.setVisible, fire: h.fire, callbacks: h.listeners,
    unmount() { h.unmount(); h.exports.AppWatcher.stop(); },
  };
}
const cpu = tree => component(tree, 'CpuPowerControlsSection');
const scopeMenu = tree => find(tree, n => n.type === 'DropdownItem' && /^Settings for /.test(n.props?.label));
const chooseScope = (h, value) => scopeMenu(h.render()).props.onChange({ data: value });
const PRESETS = ['silent', 'balanced', 'performance', 'max', 'custom'];
const LABELS = { silent: 'Silent', balanced: 'Balanced', performance: 'Performance', max: 'Maximum', custom: 'Custom' };
// The selected preset's button is marked "> " and disabled.
const presetButton = (view, key) => button(view, LABELS[key]) ?? button(view, `> ${LABELS[key]}`);
const pickPreset = (h, key) => presetButton(h.render(), key).props.onClick();
const enableDescription = h => field(h.render(), 'Enable TDP control').props.description;
async function ready(h) { h.render(); await settle(); h.render(); await settle(); return h.render(); }
const count = (h, name) => h.calls.filter(c => c.name === name).length;
let tests = 0;
async function test(name, fn) { await fn(); tests++; console.log(`TDP recovery: ${name}`); }
run(async () => {
  await test('game changes only show profiles; the backend applies them from set_active_app', async () => {
    const h=mount({game:111});await ready(h);assert.ok(field(h.render(),'Sustained power · 11 W'));
    const stale=button(h.render(),'Apply for Game 111');assert.ok(stale);
    h.responses.get_settings={enabled:true,spl:31000,sppt:34000,fppt:39000,active_preset:'custom'};
    await h.game(222);await settle();assert.ok(field(h.render(),'Sustained power · 22 W'));assert.ok(cpu(h.render()));
    stale.props.onClick();await settle();
    await h.game(null);await settle();assert.ok(field(h.render(),'Sustained power · 31 W'));
    assert.equal(cpu(h.render()).props.appId,'');
    assert.equal(count(h,'apply_tdp'),0,'neither the switch nor the stale old-game handler writes limits');
    assert.deepEqual(h.calls.filter(c=>c.name==='set_active_app').map(c=>c.args[0]),['111','222','']);
    h.unmount();
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
    await settle(); assert.ok(field(h.render(),'Sustained power · 31 W'));
    assert.ok(cpu(h.render())); assert.equal(count(h,'apply_tdp'),0);
    h.unmount();
  });
  await test('game CPU context and helper recovery are available without Extras', async () => {
    const h = mount({game:111,responses:{get_caps:{...caps,extras:false,helper_retry_available:true},get_extras_unlocked:true}});
    const view = await ready(h);
    assert.equal(field(view,'Could not load profile'),undefined); assert.equal(cpu(view).props.appId,'111');
    const recovery=button(view,'Retry helper download'); assert.equal(recovery.props.disabled,false);
    h.responses.get_caps=caps; recovery.props.onClick(); await settle(); assert.equal(count(h,'retry_extras'),1); h.unmount();
  });
  await test('helper recovery remains available after profile read failure', async () => {
    const h=mount({game:111,responses:{get_caps:{...caps,extras:false,helper_retry_available:true},get_extras_unlocked:true,
      get_game_profile:()=>{throw Error('profile read offline');}}}); await ready(h);
    assert.ok(field(h.render(),'Could not load profile')); assert.equal(cpu(h.render()),undefined);
    const recovery=button(h.render(),'Retry helper download'); assert.equal(recovery.props.disabled,false);
    recovery.props.onClick(); await settle(); assert.equal(count(h,'retry_extras'),1); h.unmount();
  });
  await test('first readiness failure is actionable and double retry is coalesced', async () => {
    const h = mount(); h.defer('is_ready'); h.render(); h.reject(h.pending('is_ready'), 'Decky offline'); await settle();
    const view = h.render(); assert.match(field(view, 'Error').props.description, /Decky offline/);
    const retry = button(view, 'Retry status'); assert.ok(retry);
    h.defer('is_ready'); retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(count(h, 'is_ready'), 2);
    h.resolve(h.pending('is_ready'), { ready: true }); await settle();
    assert.ok(field(h.render(), 'Enable TDP control')); assert.equal(button(h.render(), 'Retry status'), undefined);
    h.unmount();
  });
  await test('failed initial settings are shown before sibling reads finish', async () => {
    const h = mount(); h.defer('get_settings'); h.defer('get_power_source'); h.render(); await settle();
    h.reject(h.pending('get_settings'), 'settings unreadable'); await settle();
    assert.match(field(h.render(), 'Error').props.description, /settings unreadable/);
    button(h.render(), 'Retry status').props.onClick(); await settle();
    assert.equal(count(h, 'get_power_source'), 1, 'a still-running sibling read is not duplicated');
    h.resolve(h.pending('get_power_source'), { ac: false }); await settle();
    button(h.render(), 'Retry status').props.onClick(); await settle(); assert.ok(field(h.render(), 'Enable TDP control')); h.unmount();
  });
  await test('automatic initialization retry is bounded and stops when hidden or unmounted', async () => {
    const h = mount({ responses: { is_ready: () => { throw Error('offline'); } } }); await ready(h);
    for (let i = 0; i < 5; i++) { h.fire('timeout', 1000); await settle(); }
    assert.equal(count(h, 'is_ready'), 3);
    h.visible(false); h.fire('timeout', 1000); await settle(); assert.equal(count(h, 'is_ready'), 3);
    h.visible(true); await settle(); assert.equal(count(h, 'is_ready'), 4);
    const queued = [...h.timers.values()].filter(t => t.kind === 'timeout'); h.unmount();
    assert.equal([...h.timers.values()].filter(t => t.kind === 'timeout').length, 0, 'unmount cancels readiness retries');
    queued.forEach(t => t.fn()); await settle(); assert.equal(count(h, 'is_ready'), 4);
    const hidden = mount({ visible: false }); hidden.render(); await settle(); assert.equal(count(hidden, 'is_ready'), 0); hidden.unmount();
  });
  for (const failure of [false, true]) await test(`old "This game" ${failure ? 'failure' : 'missing profile'} cannot change the new game`, async () => {
    const h = mount({ game: 111, firstProfile: { exists: false } }); await ready(h);
    h.defer('get_game_profile'); chooseScope(h, 'game'); await settle();
    const old = h.pending('get_game_profile'); await h.game(222);
    const before = JSON.stringify(h.render()), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, { exists: false }); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(h.toasts.length, toasts);
    assert.equal(scopeMenu(h.render()).props.selectedOption, 'game_ac'); h.unmount();
  });
  for (const failure of [false, true]) await test(`old "Global" ${failure ? 'failure' : 'success'} cannot change the new game`, async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('set_game_profile_active');
    chooseScope(h, 'global'); await settle(); const old = h.pending('set_game_profile_active');
    await h.game(222); const before = JSON.stringify(h.render()), settingsReads = count(h, 'get_settings'), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, {}); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(count(h, 'get_settings'), settingsReads);
    assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('global settings read after choosing Global cannot overwrite the next game', async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('get_settings');
    chooseScope(h, 'global'); await settle(); const old = h.pending('get_settings');
    await h.game(222); const before = JSON.stringify(h.render());
    h.resolve(old, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    assert.equal(JSON.stringify(h.render()), before); h.unmount();
  });
  for (const failure of [false, true]) await test(`old preset ${failure ? 'failure' : 'success'} cannot restore old sliders or status`, async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('apply_tdp');
    pickPreset(h, 'performance'); await settle(); const old = h.pending('apply_tdp');
    await h.game(222); const before = JSON.stringify(h.render()), toasts = h.toasts.length;
    failure ? h.reject(old) : h.resolve(old, {}); await settle();
    assert.equal(JSON.stringify(h.render()), before); assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('old advanced-control refresh and AC errors are scoped to the requested game', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.defer('get_settings'); field(h.render(), 'Separate boost limits').props.onChange(true); await settle();
    const oldRead = h.pending('get_settings'); await h.game(222); const before = JSON.stringify(h.render());
    h.resolve(oldRead, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    assert.equal(JSON.stringify(h.render()), before);
    h.defer('set_game_ac_profile'); chooseScope(h, 'game'); await settle();
    const oldAc = h.pending('set_game_ac_profile'); await h.game(111); const next = JSON.stringify(h.render()), toasts = h.toasts.length;
    h.reject(oldAc); await settle(); assert.equal(JSON.stringify(h.render()), next); assert.equal(h.toasts.length, toasts); h.unmount();
  });
  await test('returning to the same app and unmounting do not revive old responses', async () => {
    const h = mount({ game: 111 }); await ready(h); h.defer('apply_tdp'); pickPreset(h, 'performance');
    const old = h.pending('apply_tdp'); await h.game(222); await h.game(111); const before = JSON.stringify(h.render());
    h.resolve(old, {}); await settle(); assert.equal(JSON.stringify(h.render()), before);
    h.defer('apply_tdp'); pickPreset(h, 'performance'); const pending = h.pending('apply_tdp');
    h.unmount(); const writes = h.writes; h.resolve(pending, {}); await settle(); assert.equal(h.writes, writes);
  });
  await test('an old failure that lands after the Router changed does not reach the new game', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.defer('set_game_profile_active'); chooseScope(h, 'global'); await settle();
    const old = h.pending('set_game_profile_active'); h.defer('set_active_app'); const change = h.game(222);
    await settle(); h.reject(old); await settle(); // Router changed before its backend report completed.
    h.resolve(h.pending('set_active_app'), {}); await change;
    assert.doesNotMatch(JSON.stringify(h.render()), /old game request failed/);
    assert.equal(h.toasts.length, 0); h.unmount();
  });
  await test('old initial snapshot is discarded and the new game initializes from a fresh snapshot', async () => {
    const h = mount({ game: 111 }); h.defer('get_settings'); h.render(); await settle(); const old = h.pending('get_settings');
    await h.game(222); h.resolve(old, { enabled: true, spl: 49000, sppt: 50000, fppt: 50000 }); await settle();
    h.render(); await settle(); const view = h.render();
    assert.ok(field(view, 'Sustained power · 22 W')); assert.equal(count(h, 'is_ready'), 2); h.unmount();
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
    let view = h.render(); assert.equal(field(view, 'Enable TDP control').props.disabled, true);
    assert.equal(button(view, 'Apply for Game 111').props.disabled, true);
    assert.ok(PRESETS.every(key => presetButton(view, key).props.disabled), 'every preset button is disabled');
    assert.match(field(view, 'TDP control unavailable').props.description, /offline/);
    assert.ok(button(view, 'Retry helper download')); assert.ok(cpu(view));
    const applies = count(h, 'apply_tdp');
    assert.equal(scopeMenu(view).props.disabled, false);
    chooseScope(h, 'global'); await settle(); view = h.render();
    assert.equal(cpu(view).props.appId, '', 'without TDP the choice only picks the CPU settings');
    assert.equal(count(h, 'set_game_profile_active'), 0);
    chooseScope(h, 'game'); await settle(); view = h.render();
    assert.equal(cpu(view).props.appId, '111'); assert.equal(count(h, 'apply_tdp'), applies);
    h.defer('retry_extras'); const retry = button(view, 'Retry helper download'); retry.props.onClick(); retry.props.onClick();
    assert.equal(count(h, 'retry_extras'), 1);
    h.responses.get_caps = caps; h.resolve(h.pending('retry_extras'), { ok: true }); await settle();
    assert.equal(field(h.render(), 'Enable TDP control').props.disabled, false); h.unmount();
  });
  await test('background helper capability is adopted by one visible read and hidden reads are ignored', async () => {
    const unavailable = { ...caps, wmi: false, extras: false, tdp_available: false, helper_retry_available: true };
    const h = mount({ responses: { get_caps: unavailable } }); await ready(h);
    h.defer('get_caps'); h.fire('interval', 5000); h.fire('interval', 5000); await settle();
    assert.equal(count(h, 'get_caps'), 2); const old = h.pending('get_caps'); h.visible(false);
    h.resolve(old, caps); await settle(); assert.equal(field(h.render(), 'Enable TDP control').props.disabled, true);
    const reads = count(h, 'get_caps'); h.fire('interval', 5000); assert.equal(count(h, 'get_caps'), reads);
    h.responses.get_caps = caps; h.visible(true); h.fire('interval', 5000); await settle();
    assert.equal(field(h.render(), 'Enable TDP control').props.disabled, false); h.render();
    assert.equal([...h.timers.values()].filter(t => t.kind === 'interval' && t.delay === 5000).length, 0); h.unmount();
  });
  await test('legacy capability replies retain compatible availability', async () => {
    const legacy = mount({ responses: { get_caps: {} } }); await ready(legacy);
    assert.equal(field(legacy.render(), 'Enable TDP control').props.disabled, false); legacy.unmount();
    const missing = mount({ responses: { get_caps: { ...caps, tdp_available: undefined, wmi: false, extras: false } } });
    await ready(missing); assert.equal(field(missing.render(), 'Enable TDP control').props.disabled, true); missing.unmount();
  });
  await test('pending game profile blocks stale Apply and accepts the fresh saved profile', async () => {
    const h = mount({ game: 111 }); await ready(h);
    const oldApply = button(h.render(), 'Apply for Game 111').props.onClick;
    h.defer('get_game_profile'); await h.game(222);
    const before = count(h, 'apply_tdp'), view = h.render();
    assert.equal(scopeMenu(view).props.selectedOption, 'global');
    const apply = find(view, n => n.type === 'ButtonItem' && /Apply|Save/.test(n.props?.children));
    assert.equal(apply.props.disabled, true);
    apply.props.onClick(); oldApply(); await settle();
    assert.equal(count(h, 'apply_tdp'), before);
    h.resolve(h.pending('get_game_profile'), profile(22, true)); await settle();
    assert.ok(field(h.render(), 'Sustained power · 22 W'));
    assert.equal(button(h.render(), 'Apply for Game 222').props.disabled, false);
    h.unmount();
  });
  await test('missing game profile adopts fresh global limits and never inherits the old game', async () => {
    const h = mount({ game: 111 }); await ready(h);
    h.responses.get_game_profile = () => ({ exists: false });
    h.responses.get_settings = { enabled: true, spl: 15000, sppt: 18000, fppt: 23000, active_preset: 'custom' };
    await h.game(222); const view = h.render();
    assert.equal(scopeMenu(view).props.selectedOption, 'global');
    assert.equal(button(view, 'Delete profile for Game 222'), undefined, 'nothing to delete');
    assert.ok(field(view, 'Sustained power · 15 W'));
    button(view, 'Apply').props.onClick(); await settle();
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
    assert.ok(field(h.render(), 'Sustained power · 22 W')); assert.ok(cpu(h.render())); h.unmount();
  });
  await test('CPU battery and AC profile selection works without a TDP provider', async () => {
    const h = mount({ game: 222, responses: { get_caps: { ...caps, tdp_available: false, wmi: false, extras: false } } });
    let view = await ready(h);
    assert.equal(scopeMenu(view).props.selectedOption, 'game_ac');
    const source = () => field(h.render(), 'Edit settings for');
    assert.equal(source().props.disabled, false); assert.equal(source().props.selectedOption, 'battery');
    assert.equal(source().props.description, 'On battery now.');
    source().props.onChange({ data: 'charger' }); view = h.render();
    assert.equal(source().props.description, 'On battery now. Changes apply when you plug in.');
    assert.equal(cpu(view).props.appId, '222'); assert.equal(cpu(view).props.acProfile, true);
    source().props.onChange({ data: 'battery' }); view = h.render();
    assert.equal(cpu(view).props.acProfile, false); assert.equal(count(h, 'apply_tdp'), 0);
    assert.equal(count(h, 'set_game_ac_profile'), 0); h.unmount();
  });
  await test('the power summary never presents draft limits before profile confirmation', async () => {
    const h = mount({ game: 111 }); h.defer('get_game_profile'); await ready(h);
    assert.equal(enableDescription(h), 'Reading profile…');
    h.resolve(h.pending('get_game_profile'), profile(13)); await settle();
    assert.equal(enableDescription(h), 'Game 111: Custom · 13 W');
    h.defer('get_game_profile'); await h.game(222);
    assert.equal(enableDescription(h), 'Reading profile…');
    h.resolve(h.pending('get_game_profile'), profile(22, true)); await settle();
    assert.equal(enableDescription(h), 'Game 222: Custom · 22 W');
    h.callbacks.tdp_info({ values: { package_draw: 9.44 } });
    assert.equal(enableDescription(h), 'Game 222: Custom · 22 W · drawing 9.4 W');
    h.callbacks.power_source({ ac: true });
    assert.equal(enableDescription(h), 'Game 222: Custom · 24 W · drawing 9.4 W', 'on charger the charger values run');
    h.unmount();
    const off = mount({ responses: { get_settings: { enabled: false, spl: 15000, sppt: 18000, fppt: 23000 } } }); await ready(off);
    assert.equal(enableDescription(off), "Off · the console uses Lenovo's power limits."); off.unmount();
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
    assert.equal(scopeMenu(view).props.selectedOption, 'global');
    assert.ok(field(view, 'Sustained power · 15 W')); assert.ok(cpu(view)); h.unmount();
  });
  await test('allowing 50 W asks first; turning it off does not', async () => {
    const h = mount(); await ready(h);
    field(h.render(), 'Allow up to 50 W').props.onChange(true); await settle();
    assert.equal(count(h, 'set_extras_unlocked'), 0, 'nothing is unlocked before OK');
    const asked = h.modals.at(-1).props;
    assert.deepEqual([asked.strTitle, asked.strOKButtonText, asked.strCancelButtonText], ['Allow up to 50 W?', 'Allow 50 W', 'Cancel']);
    assert.equal(asked.strDescription, "This goes beyond Lenovo's power limits. The console runs hotter and louder, and the battery drains faster.");
    asked.onOK(); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'set_extras_unlocked').map(c => c.args[0]), [true]);
    field(h.render(), 'Allow up to 50 W').props.onChange(false); await settle();
    assert.equal(h.modals.length, 1, 'turning 50 W off needs no confirmation');
    assert.deepEqual(h.calls.filter(c => c.name === 'set_extras_unlocked').map(c => c.args[0]), [true, false]);
    const missing = mount({ responses: { get_caps: { ...caps, extras: false, helper_retry_available: true } } }); await ready(missing);
    field(missing.render(), 'Allow up to 50 W').props.onChange(true);
    assert.match(missing.modals.at(-1).props.strDescription, /Companion first downloads the verified RyzenAdj helper from GitHub\.$/);
    h.unmount(); missing.unmount();
  });
  await test('Global keeps the game profile unused and This game turns it back on', async () => {
    const h = mount({ game: 111 }); await ready(h);
    assert.equal(scopeMenu(h.render()).props.selectedOption, 'game');
    assert.equal(scopeMenu(h.render()).props.description, 'Only Game 111 uses these settings.');
    h.responses.get_settings = { enabled: true, spl: 31000, sppt: 34000, fppt: 39000, active_preset: 'custom' };
    chooseScope(h, 'global'); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'set_game_profile_active').map(c => Array.from(c.args)), [['111', false]]);
    assert.equal(count(h, 'delete_game_profile'), 0, 'turning the profile off never deletes it');
    let view = h.render();
    assert.equal(scopeMenu(view).props.description, 'Game 111 uses Global settings, shared by all games.');
    assert.ok(field(view, 'Sustained power · 31 W')); assert.equal(cpu(view).props.appId, '');
    assert.ok(button(view, 'Delete profile for Game 111'), 'the unused profile can still be deleted');
    assert.deepEqual(h.toasts.map(t => [t.title, t.body]), [['TDP', 'Game 111 uses Global settings.']]);
    chooseScope(h, 'game'); await settle(); view = h.render();
    assert.deepEqual(h.calls.filter(c => c.name === 'set_game_profile_active').map(c => Array.from(c.args)), [['111', false], ['111', true]]);
    assert.ok(field(view, 'Sustained power · 11 W')); assert.equal(cpu(view).props.appId, '111');
    assert.equal(count(h, 'apply_tdp'), 0, 'the backend applies the chosen profile itself');
    h.unmount();
    const unused = mount({ game: 111, firstProfile: { ...profile(11), active: false } }); await ready(unused);
    assert.equal(scopeMenu(unused.render()).props.selectedOption, 'global', 'an unused profile is not shown as active');
    assert.ok(field(unused.render(), 'Sustained power · 15 W'));
    assert.ok(button(unused.render(), 'Delete profile for Game 111')); unused.unmount();
  });
  await test('deleting a game profile asks first', async () => {
    const h = mount({ game: 111 }); await ready(h);
    button(h.render(), 'Delete profile for Game 111').props.onClick(); await settle();
    assert.equal(count(h, 'delete_game_profile'), 0, 'nothing is deleted before OK');
    const asked = h.modals.at(-1).props;
    assert.deepEqual([asked.strTitle, asked.strDescription, asked.strOKButtonText], ['Delete profile for Game 111?',
      'Its power, CPU Boost and energy settings are removed. Game 111 will use Global settings.', 'Delete']);
    asked.onOK(); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'delete_game_profile').map(c => c.args[0]), ['111']);
    const view = h.render();
    assert.equal(button(view, 'Delete profile for Game 111'), undefined);
    assert.equal(scopeMenu(view).props.selectedOption, 'global'); h.unmount();
  });
  await test('confirmations pressed while the panel is hidden still run once', async () => {
    // A confirmation takes focus from the Quick Access menu, so OK arrives while the panel is hidden.
    const h = mount({ game: 111 }); await ready(h);
    button(h.render(), 'Delete profile for Game 111').props.onClick(); let ok = h.modals.at(-1).props.onOK;
    h.visible(false); await settle(); ok(); ok(); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'delete_game_profile').map(c => c.args[0]), ['111']);
    h.visible(true); await settle();
    field(h.render(), 'Allow up to 50 W').props.onChange(true); ok = h.modals.at(-1).props.onOK;
    h.visible(false); await settle(); ok(); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'set_extras_unlocked').map(c => c.args[0]), [true]);
    h.unmount();
    const draft = mount({ game: 111 }); await ready(draft);
    field(draft.render(), 'Sustained power · 11 W').props.onChange(14);
    chooseScope(draft, 'global'); ok = draft.modals.at(-1).props.onOK;
    draft.visible(false); await settle(); ok(); await settle();
    assert.equal(count(draft, 'set_game_profile_active'), 1, 'the discarded draft lets the scope change run');
    draft.unmount();
  });
  await test('This game + charger keeps the stored charger values both ways', async () => {
    const h = mount({ game: 111 }); await ready(h);
    chooseScope(h, 'game_ac'); await settle();
    assert.deepEqual(Array.from(h.calls.filter(c => c.name === 'set_game_ac_profile').at(-1).args), ['111', 13000, 16000, 21000, true, '']);
    assert.ok(field(h.render(), 'Edit settings for'));
    h.responses.get_game_profile = () => profile(11, true);
    chooseScope(h, 'game'); await settle();
    assert.deepEqual(Array.from(h.calls.filter(c => c.name === 'set_game_ac_profile').at(-1).args), ['111', 13000, 16000, 21000, false, '']);
    assert.equal(field(h.render(), 'Edit settings for'), undefined); h.unmount();
  });
  await test('an unsaved Custom draft asks before a scope change; a preset drops it silently', async () => {
    const h = mount({ game: 111 }); await ready(h);
    const custom = () => find(h.render(), n => n.type === 'PanelSection' && /^Custom limits/.test(n.props?.title));
    field(h.render(), 'Sustained power · 11 W').props.onChange(14);
    assert.equal(custom().props.title, 'Custom limits · unsaved');
    assert.equal(button(h.render(), 'Apply for Game 111').props.description, 'Unsaved changes');
    chooseScope(h, 'global'); await settle();
    assert.equal(count(h, 'set_game_profile_active'), 0, 'the draft is not dropped before OK');
    const asked = h.modals.at(-1).props;
    assert.deepEqual([asked.strTitle, asked.strDescription, asked.strOKButtonText],
      ['Discard unsaved changes?', "Your changes to these limits haven't been applied.", 'Discard']);
    asked.onOK(); await settle();
    assert.equal(count(h, 'set_game_profile_active'), 1);
    field(h.render(), 'Sustained power · 15 W').props.onChange(18);
    pickPreset(h, 'balanced'); await settle();
    assert.equal(h.modals.length, 1, 'a preset needs no confirmation');
    assert.deepEqual(Array.from(h.calls.filter(c => c.name === 'apply_tdp').at(-1).args), [16000, 25000, 30000, '', 'balanced', '111']);
    assert.equal(custom(), undefined); h.unmount();
  });
  await test('preset buttons apply on click; the selected one is marked and disabled; Custom applies nothing', async () => {
    const h = mount(); await ready(h);
    assert.equal(presetButton(h.render(), 'custom').props.children, '> Custom');
    assert.equal(presetButton(h.render(), 'custom').props.disabled, true);
    assert.equal(presetButton(h.render(), 'max').props.disabled, false);
    pickPreset(h, 'max'); await settle();
    assert.deepEqual(h.calls.filter(c => c.name === 'apply_tdp').map(c => Array.from(c.args).slice(0, 5)), [[35000, 37000, 45000, '', 'max']]);
    assert.equal(h.toasts.at(-1).body, 'Maximum applied.');
    const max = presetButton(h.render(), 'max');
    assert.deepEqual([max.props.children, max.props.disabled, max.props.description], ['> Maximum', true, 'Maximum · 35 W (boost 37/45 W)']);
    assert.equal(presetButton(h.render(), 'custom').props.disabled, false);
    pickPreset(h, 'custom'); await settle();
    assert.equal(count(h, 'apply_tdp'), 1); assert.equal(presetButton(h.render(), 'custom').props.description, 'Set your own limits below.');
    assert.ok(button(h.render(), 'Apply')); h.unmount();
  });
  await test('a refused preset or Enable TDP control shows the backend text and keeps what ran before', async () => {
    const refused = { ok: false, error: 'firmware refused; previous target restored' };
    const h = mount({ responses: { apply_tdp: refused, set_plugin_enabled: refused } }); await ready(h);
    pickPreset(h, 'max'); await settle();
    assert.equal(h.toasts.at(-1).body, refused.error);
    assert.equal(presetButton(h.render(), 'custom').props.children, '> Custom');
    field(h.render(), 'Enable TDP control').props.onChange(false); await settle();
    assert.equal(h.toasts.at(-1).body, refused.error);
    assert.equal(field(h.render(), 'Enable TDP control').props.checked, true); h.unmount();
  });
  await test('the panel names what runs now from get_settings.running, and falls back without it', async () => {
    const saved = { enabled: true, spl: 16000, sppt: 25000, fppt: 30000, active_preset: 'balanced' };
    const old = mount({ responses: { get_settings: saved } }); await ready(old);
    assert.match(enableDescription(old), /^Global: Balanced · 16 W/); old.unmount();
    const running = { spl: 12, sppt: 12, fppt: 20, preset: 'silent', battery: true, game: false };
    const h = mount({ responses: { get_settings: { ...saved, running } } }); await ready(h);
    assert.match(enableDescription(h), /^Global: Silent · 12 W/);
    h.responses.get_settings = { ...saved, running: { ...running, spl: 20, sppt: 32, fppt: 35, preset: 'performance', battery: false } };
    h.visible(false); await settle(); h.visible(true); await settle(); h.render();
    assert.match(enableDescription(h), /^Global: Performance · 20 W/);
    h.unmount();
    const g = mount({ game: 111, responses: { get_settings: { ...saved, running: { ...running, game: true, preset: 'custom', spl: 25 } } } }); await ready(g);
    assert.match(enableDescription(g), /^Game 111: Custom · 25 W/); g.unmount();
  });
  await test('Separate battery presets: a switch where TDP works, and battery values on battery', async () => {
    const batteryCaps = { ...caps, presets: { silent: { spl: 8, sppt: 15, fppt: 20 }, balanced: { spl: 16, sppt: 25, fppt: 30 },
      performance: { spl: 20, sppt: 32, fppt: 35 }, max: { spl: 35, sppt: 37, fppt: 45 } } };
    batteryCaps.battery_presets = { ...batteryCaps.presets, balanced: { spl: 16, sppt: 16, fppt: 30 } };
    const settings = { enabled: true, spl: 16000, sppt: 25000, fppt: 30000, active_preset: 'balanced' };
    for (const [separate, ac, note] of [[false, false, '(boost 25/30 W)'], [true, false, '(boost 16/30 W)'], [true, true, '(boost 25/30 W)']]) {
      const h = mount({ responses: { get_caps: batteryCaps, get_power_source: { ac },
        get_settings: { ...settings, separate_battery_presets: separate } } });
      const view = await ready(h);
      assert.ok(JSON.stringify(view).includes(`Balanced · 16 W ${note}`), `separate=${separate} ac=${ac}`);
      const toggle = field(view, 'Separate battery presets');
      assert.equal(toggle.props.checked, separate);
      assert.equal(toggle.props.description, 'Silent, Balanced and Performance use the lower limits the firmware sets on battery.');
      toggle.props.onChange(!separate); await settle();
      assert.deepEqual(h.calls.filter(c => c.name === 'set_separate_battery_presets').map(c => c.args), [[!separate]]);
      assert.equal(count(h, 'apply_tdp'), 0, 'the backend applies the values itself');
      h.unmount();
    }
    // Switched in the Desktop app while the panel was closed: the next opening shows it.
    const reopened = mount({ responses: { get_caps: batteryCaps, get_settings: settings } });
    assert.equal(field(await ready(reopened), 'Separate battery presets').props.checked, false);
    reopened.visible(false); await settle();
    reopened.responses.get_settings = { ...settings, separate_battery_presets: true };
    reopened.visible(true); await settle();
    const shown = reopened.render();
    assert.equal(field(shown, 'Separate battery presets').props.checked, true);
    assert.ok(JSON.stringify(shown).includes('Balanced · 16 W (boost 16/30 W)'));
    // Switched back in the Desktop app while the panel stays open: the next re-read shows it.
    reopened.responses.get_settings = { ...settings, separate_battery_presets: false };
    reopened.fire('interval', 10000); await settle();
    assert.equal(field(reopened.render(), 'Separate battery presets').props.checked, false);
    reopened.unmount();
    const h = mount({ responses: { get_caps: { ...caps, wmi: false, extras: false, tdp_available: false } } });
    assert.equal(field(await ready(h), 'Separate battery presets'), undefined); h.unmount();
  });
  await test('every page ends with Show details', async () => {
    const h = mount({ game: 111 }); await ready(h);
    const last = () => { const rows = nodes(h.render()).filter(n => ['ButtonItem', 'ToggleField', 'SliderField', 'DropdownItem', 'Field'].includes(n.type)); return rows.at(-1); };
    assert.equal(last().props.children, 'Show details');
    last().props.onClick();
    assert.equal(last().props.label, 'Set by'); h.unmount();
  });
  await test('the rules and texts both UIs share (desktop/native/tdp.mjs)', async () => {
    const T = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/tdp.mjs')).href);
    // No slider sequence breaks FPPT >= SPPT >= SPL or the ceilings.
    let seed = 7, t = T.fromAbsolute(15, 20, 25);
    const next = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
    for (let i = 0; i < 600; i++) {
      const c = i < 300 ? caps.std : caps.max, v = Math.round(next() * 60) - 5, pick = Math.floor(next() * 3);
      t = pick === 0 ? T.withSpl(t, v, c, 5) : pick === 1 ? T.withSppt(t, v, c) : T.withFppt(t, v, c);
      const a = T.absolute(t);
      assert.ok(a.spl >= 5 && a.spl <= a.sppt && a.sppt <= a.fppt, `order ${JSON.stringify(a)}`);
      assert.ok(a.spl <= c.spl && a.sppt <= c.sppt && a.fppt <= c.fppt, `ceilings ${JSON.stringify(a)}`);
    }
    assert.deepEqual(T.absolute(T.simpleTuning(30, caps.std, 5)), { spl: 30, sppt: 37, fppt: 45 });
    // A console whose firmware allows more than the Go 2 keeps its own range in the single-dial mode.
    assert.deepEqual(T.absolute(T.simpleTuning(40, { spl: 40, sppt: 43, fppt: 53 }, 5)), { spl: 40, sppt: 43, fppt: 53 });
    // "Above Lenovo's limit" warns only while 50 W is not allowed.
    const high = { spl: 40, sppt: 45, fppt: 50 };
    assert.equal(T.powerSummary(true, 'Game 1', 'Custom', high, 18.25, false, caps.std), "Game 1: Custom · 40 W · drawing 18.3 W · above Lenovo's limit");
    assert.equal(T.powerSummary(true, 'Global', 'Custom', high, null, true, caps.std), 'Global: Custom · 40 W');
    assert.equal(T.powerSummary(false, 'Global', 'Custom', high, null, false, caps.std), "Off · the console uses Lenovo's power limits.");
    // A game's limits are named by their values; the saved name belongs to Global.
    assert.deepEqual(T.activeLimits({ spl: 16000, sppt: 25000, fppt: 30000, active_preset: 'balanced' }),
      { spl: 16, sppt: 25, fppt: 30, global: true, label: 'Balanced' });
    assert.equal(T.activeLimits({ spl: 15000, sppt: 20000, fppt: 25000, active_spl: 35000, active_sppt: 37000, active_fppt: 45000, active_preset: 'custom' }).label, 'Maximum');
    assert.equal(T.activeLimits({}), null);
    // The backend's `running` is taken as it is; an incomplete one falls back to the saved and active_* values.
    const run = { spl: 12, sppt: 12, fppt: 20, preset: 'silent', battery: true, game: false };
    assert.deepEqual(T.activeLimits({ spl: 16000, sppt: 25000, fppt: 30000, active_preset: 'balanced', running: run }),
      { spl: 12, sppt: 12, fppt: 20, global: true, label: 'Silent' });
    assert.equal(T.activeLimits({ spl: 1000, sppt: 2000, fppt: 3000, running: { ...run, game: true, preset: 'custom' } }).global, false);
    assert.equal(T.activeLimits({ spl: 16000, sppt: 25000, fppt: 30000, active_preset: 'balanced', running: { ...run, preset: 'x' } }).label, 'Balanced');
    assert.equal(T.runningNow({ running: { ...run, sppt: null } }), null);
    assert.equal(T.overviewSummary({ enabled: true, running: run }), 'Silent · 12 W');
    // Separate battery presets: Balanced on battery runs 16/16/30 and is still named (and owned by Global).
    assert.deepEqual(T.activeLimits({ spl: 16000, sppt: 25000, fppt: 30000, active_spl: 16000, active_sppt: 16000,
      active_fppt: 30000, active_preset: 'balanced', battery_preset: 'balanced' }),
      { spl: 16, sppt: 16, fppt: 30, global: true, label: 'Balanced' });
    const battery = { ...T.PRESETS, balanced: { spl: 16, sppt: 16, fppt: 30 } };
    assert.equal(T.shownPresets(T.PRESETS, battery, true, true), battery);
    assert.equal(T.shownPresets(T.PRESETS, battery, false, true), T.PRESETS);
    assert.equal(T.shownPresets(T.PRESETS, battery, true, false), T.PRESETS);
    assert.equal(T.shownPresets(T.PRESETS, undefined, true, true), T.PRESETS);
    // Retry helper download only while TDP or the 50 W unlock is missing the helper.
    const ok = T.capsState({ ...caps, helper_retry_available: true });
    assert.equal(T.helperRetryShown(ok, true), false);
    const paused = T.capsState({ ...caps, extras: false, helper_retry_available: true });
    assert.equal(T.helperRetryShown(paused, false), false);
    assert.equal(T.helperRetryShown(paused, true), true);
    assert.equal(T.tdpNotice(paused, true)[0], '50 W paused');
    assert.deepEqual(T.tdpNotice(T.capsState({ tdp_available: false }), false),
      ['TDP control unavailable', 'Neither the firmware TDP interface nor the verified RyzenAdj helper is available.']);
    // CPU Boost cannot change while its state is unknown.
    assert.equal(T.boostUsable({ available: true, enabled: null, error: '' }), false);
    assert.equal(T.boostNote({ available: true, enabled: null, error: '' }), 'The current state could not be read.');
    // One EPP scale; the panel steps by 10%, the Desktop app by 1%; unknown is 50%, set by the system.
    const epp = { available: true, value: '64', numeric_value: null, numeric_supported: true, profiles: [], min: 0, max: 255, error: '' };
    assert.deepEqual([T.eppPercent(epp, 10), T.eppPercent(epp, 1)], [30, 25]);
    assert.equal(T.eppPercent({ ...epp, value: null }, 1), 50);
    assert.equal(T.eppNote({ ...epp, value: null }), 'Set by the system. Move the slider to choose.');
    assert.equal(T.eppNote({ ...epp, value: 'balance_power' }), T.EPP_PREFERENCE);
    assert.equal(T.eppValue(40), '102');
    assert.deepEqual(['default', 'performance', 'balance_performance', 'balance_power', 'power', 'low-power'].map(T.eppLabel),
      ['System default', 'Prefer CPU', 'Balanced · Prefer CPU', 'Balanced · Prefer GPU', 'Prefer GPU', 'Low power']);
    assert.equal(T.limitsText({ values: { spl_limit: 20, sppt_limit: 25 } }), 'SPL 20 · SPPT 25 · FPPT – W');
    assert.equal(T.limitsText(undefined), '—');  // no answer yet, or a refused read
    assert.deepEqual([T.sourceText({ values: { source: 'ryzenadj' } }), T.sourceText({ values: {} })], ['ryzenadj', '—']);
  });
  console.log(`Passed ${tests} TDP recovery regressions.`);
});
