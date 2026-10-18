const { assert, run, settle, mount, text } = require('./frontend_harness.cjs');
const watchers = [];
let response = { version: 'test', blocked: true, latched: true, standalone_plugins: ['LeGoTDP'] };
const h = mount('index.tsx', {
  component: 'TestContent', append: 'export { Content as TestContent, refreshGuard as testRefreshGuard };',
  ui: { staticClasses: {} },
  responses: { get_version: () => typeof response === 'function' ? response() : response },
  strict: true,
  modules: name => name === './updates' ? { startUpdates() {}, stopUpdates() {}, UpdateSection: 'UpdateSection' }
    : name.startsWith('./') ? new Proxy({}, { get: (_, k) => /^(start|stop)/.test(k) ? () => watchers.push(k) : k }) : undefined,
});
const guard = value => h.listeners.companion_guard(value);
const render = () => h.render();
run(async () => {
  let plugin = h.exports.default(); await settle(); let tree = render(); await settle(); tree = render();
  assert.match(text(tree), /Companion paused/); assert.match(text(tree), /LeGoTDP/);
  assert.deepEqual(watchers, []);
  const outcome = tree.props.children.find(child => child?.type === 'FirmwarePanel');
  assert.ok(outcome, 'the paused screen still exposes the independent firmware outcome');
  assert.equal(outcome.props.visible, true); assert.equal(outcome.props.allowUpdates, false);
  response = { version: 'test', blocked: true, latched: true, standalone_plugins: [], restart_required: true };
  guard(response); tree = render(); assert.match(text(tree), /Ready for a restart/);
  assert.doesNotMatch(text(tree), /are gone/, 'a failed startup scan latches without any conflict ever having existed');
  guard({ version: 'old reply', blocked: false }); assert.match(text(render()), /Ready for a restart/);
  plugin.onDismount(); h.reset(); watchers.length = 0;
  response = { version: 'test', blocked: false, standalone_plugins: [] }; plugin = h.exports.default(); await settle();
  tree = render(); await settle(); tree = render(); assert.equal(tree.type.name, 'Controls');
  assert.deepEqual(watchers, ['startTdpWatcher', 'startVibrationWatcher']);
  guard({ version: 'test', blocked: false, modules: { tdp: { enabled: false }, vibration: { enabled: false } } });
  assert.deepEqual(watchers.slice(-2), ['stopTdpWatcher', 'stopVibrationWatcher']);
  guard({ version: 'test', blocked: false, modules: { tdp: { enabled: true }, vibration: { enabled: false } } });
  assert.equal(watchers.at(-1), 'startTdpWatcher');
  guard({ version: 'test', blocked: false, modules: { tdp: { enabled: true }, vibration: { enabled: true } } });
  assert.equal(watchers.at(-1), 'startVibrationWatcher');
  guard({ version: 'test', blocked: false, modules: { tdp: { enabled: true, ready: true }, vibration: { enabled: true, ready: false, maintenance: true } } });
  assert.equal(watchers.at(-1), 'stopVibrationWatcher', 'maintenance pauses automatic profile writes without changing saved enabled intent');
  tree = render(); assert.equal(tree.props.modules.vibration.enabled, true); assert.equal(tree.props.modules.vibration.maintenance, true);
  guard({ version: 'test', blocked: false, modules: { tdp: { enabled: true }, vibration: { enabled: true } } });
  assert.equal(watchers.at(-1), 'startVibrationWatcher');
  // One failed scan pauses the panel; the next clean scan (event or poll) must resume it.
  guard({ version: 'test', blocked: true, latched: false, guard_error: 'denied', standalone_plugins: [] });
  assert.match(text(render()), /Companion paused/); assert.deepEqual(watchers.slice(-2), ['stopTdpWatcher', 'stopVibrationWatcher']);
  guard({ version: 'test', blocked: false, latched: false, modules: { tdp: { enabled: true }, vibration: { enabled: true } } });
  assert.equal(render().type.name, 'Controls', 'a transient scan error must not latch the panel');
  assert.deepEqual(watchers.slice(-2), ['startTdpWatcher', 'startVibrationWatcher']);
  guard({ version: 'test', blocked: true, latched: false, guard_error: 'denied', standalone_plugins: [] });
  response = { version: 'test', blocked: false, latched: false, modules: { tdp: { enabled: true }, vibration: { enabled: true } } };
  await h.exports.testRefreshGuard(); await settle(); assert.equal(render().type.name, 'Controls', 'a healthy poll resumes after a transient error');
  guard({ version: 'test', blocked: true, latched: true, standalone_plugins: ['LeGo Vibe Control'] });
  tree = render(); assert.match(text(tree), /LeGo Vibe Control/); assert.notEqual(tree.type.name, 'Controls');
  assert.deepEqual(watchers.slice(-2), ['stopTdpWatcher', 'stopVibrationWatcher']);
  plugin.onDismount(); h.reset(); assert.equal(Object.keys(h.listeners).length, 0);
  let finishOldRead;
  response = () => new Promise(resolve => { finishOldRead = resolve; });
  watchers.length = 0; plugin = h.exports.default(); render();
  guard({ version: 'new', blocked: false, modules: { tdp: { enabled: false }, vibration: { enabled: false } } });
  finishOldRead({ version: 'stale', blocked: false, modules: { tdp: { enabled: true }, vibration: { enabled: true } } });
  await settle(); tree = render(); assert.equal(tree.props.modules.tdp.enabled, false);
  assert.deepEqual(watchers, [], 'an earlier poll must not re-enable modules disabled by an event');
  plugin.onDismount(); h.reset();
  for (const invalid of [null, undefined, false, [], { blocked: 'false' }]) {
    response = { version: 'test', blocked: false }; watchers.length = 0;
    plugin = h.exports.default(); await settle(); render(); await settle();
    assert.equal(render().type.name, 'Controls');
    response = invalid; await h.exports.testRefreshGuard(); await settle();
    assert.notEqual(render().type.name, 'Controls', 'A malformed guard read must pause stale hardware controls');
    assert.deepEqual(watchers.slice(-2), ['stopTdpWatcher', 'stopVibrationWatcher']);
    response = { version: 'test', blocked: false }; await h.exports.testRefreshGuard(); await settle();
    assert.equal(render().type.name, 'Controls', 'A malformed response must not latch a real plugin conflict');
    assert.doesNotThrow(() => guard(invalid), 'Malformed events must not escape into Decky');
    assert.notEqual(render().type.name, 'Controls');
    guard(response); assert.equal(render().type.name, 'Controls');
    plugin.onDismount(); h.reset();
  }
  console.log('Guard hides every hardware page, stops reports, explains conflicts and requires a fresh start.');
});
