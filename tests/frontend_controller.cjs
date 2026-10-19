const { assert, run, settle: wait, mount, find, text } = require('./frontend_harness.cjs');
const settle = () => wait(16);
const status = {
  available: true, reason: '', gyro_source: 'system', applied_source: null,
  controlled: false, conflict: false, error: '', physical: { path: '/dev/test-physical', pid: 1, driver: 'test' },
  virtual: { path: '/dev/test-virtual', pid: 2 }, diagnostics_active: false,
};
const emptyStream = () => ({ received: false, reports: 0, rate_hz: null, age_ms: null, sample: null });
const snapshot = (token = 'test-token', overrides = {}) => ({
  token, active: true, reason: '', elapsed_s: 0, remaining_s: 30,
  physical: emptyStream(), virtual: emptyStream(), ...overrides,
});

function harness(initiallyVisible = true) {
  let visible = initiallyVisible;
  const h = mount('controller.tsx', { component: 'ControllerPage', deferAll: true,
    modules: { './firmware': { FirmwarePanel: 'FirmwarePanel' } } });
  const render = () => h.render({ visible });
  return {
    calls: h.calls, timers: h.timers, render, exports: h.exports,
    respond: (name, value, rejected = false) => rejected ? h.reject(name, value) : h.respond(name, value),
    get writes() { return h.writes; }, count: h.count,
    visible(value) { visible = value; return render(); },
    fire(kind) {
      const found = [...h.timers].find(([, value]) => value.kind === kind);
      if (!found) return false;
      const [id, entry] = found;
      if (kind === 'timeout') h.timers.delete(id);
      entry.fn();
      return true;
    },
    unmount: h.unmount,
  };
}
const startButton = h => find(h.render(), node => node.type === 'ButtonItem' && node.props.children === 'Start controller test');
async function ready(h) {
  h.render(); h.respond('controller_get_status', status); await settle(); h.render();
}
async function start(h, token = 'test-token') {
  startButton(h).props.onClick(); await settle();
  h.respond('controller_start_diagnostics', snapshot(token)); await settle(); h.render();
}

run(async () => {
  // A mounted but hidden page performs no work. Reads and lease renewals do not overlap.
  const h = harness(false);
  h.render(); assert.equal(h.calls.length, 0); assert.equal(h.timers.size, 0);
  h.visible(true); h.respond('controller_get_status', status); await settle();
  await start(h);
  assert.equal(h.fire('timeout'), true);
  assert.equal(h.fire('timeout'), false, 'one diagnostic RPC at a time');
  assert.equal(h.count('controller_get_diagnostics'), 1);
  h.visible(false);
  assert.equal(h.timers.size, 0, 'all timers cleared on hide');
  assert.equal(h.count('controller_stop_diagnostics'), 1);
  h.respond('controller_get_diagnostics', snapshot());
  h.respond('controller_stop_diagnostics', snapshot('test-token', { active: false, reason: 'Stopped' }));
  await settle();
  assert.equal(h.timers.size, 0, 'late poll does not restart a hidden lease');
  assert.equal(h.count('controller_get_diagnostics'), 1);
  h.unmount();

  // A start reply arriving after unmount must close its newly created token.
  const late = harness(); await ready(late);
  startButton(late).props.onClick(); await settle();
  late.unmount(); const writesAfterUnmount = late.writes;
  late.respond('controller_start_diagnostics', snapshot('late-token')); await settle();
  assert.equal(late.count('controller_stop_diagnostics'), 1);
  assert.equal(late.calls.find(call => call.name === 'controller_stop_diagnostics').args[0], 'late-token');
  late.respond('controller_stop_diagnostics', snapshot('late-token', { active: false })); await settle();
  assert.equal(late.timers.size, 0);
  assert.equal(late.writes, writesAfterUnmount, 'late start does not update an unmounted page');

  // Backend expiration ends polling; unknown samples stay distinct from legitimate zeros.
  const expired = harness(); await ready(expired); await start(expired, 'bounded-token');
  assert.match(text(expired.render()), /Left gyro — · No reports yet\./, 'an empty stream is not shown as zero');
  assert.match(text(expired.render()), /Stop test · 30 s left/);
  expired.fire('timeout');
  expired.respond('controller_get_diagnostics', snapshot('bounded-token', {
    active: false, elapsed_s: 30, remaining_s: 0, reason: '30-second limit reached',
    physical: { received: true, reports: 100, rate_hz: 20, age_ms: 10, sample: {
      gyro_left: { x: 0, y: 0, z: 0 }, gyro_right: null, touchpad: { x: 0, y: 0, is_touching: true },
    } },
  })); await settle();
  assert.equal([...expired.timers.values()].filter(timer => timer.kind === 'timeout').length, 0);
  assert.match(text(expired.render()), /Left gyro X 0 · Y 0 · Z 0 · 100 reports · 20\.0 Hz/);
  assert.match(text(expired.render()), /Right gyro —/);
  assert.match(text(expired.render()), /Touchpad Physical: touch at 0\.00,0\.00 · Steam: —/);
  assert.equal(startButton(expired).props.description, '30-second limit reached', 'a finished test says why and can start again');
  expired.unmount();

  // An older status response must not overwrite a user's source selection.
  const source = harness(); await ready(source); source.fire('interval');
  const dropdown = find(source.render(), node => node.type === 'DropdownItem');
  dropdown.props.onChange({ data: 'left' });
  await settle(); assert.equal(source.count('controller_set_gyro_source'), 0, 'write waits for the earlier read');
  source.respond('controller_get_status', { ...status, gyro_source: 'combined' }); await settle();
  assert.equal(source.count('controller_set_gyro_source'), 1, 'the write starts once the earlier read has finished');
  assert.equal(source.calls.find(call => call.name === 'controller_set_gyro_source').args[0], 'left');
  assert.equal(find(source.render(), node => node.type === 'DropdownItem').props.selectedOption, 'system', 'stale read ignored');
  source.respond('controller_set_gyro_source', { ...status, controlled: true, gyro_source: 'left', applied_source: 'left' });
  await settle(); assert.equal(find(source.render(), node => node.type === 'DropdownItem').props.selectedOption, 'left');
  assert.equal(source.count('controller_start_diagnostics'), 0, 'saving does not start diagnostics');
  source.unmount();
  // Native Steam dropdowns hide QAM before delivering the selected option.
  const overlay = harness(); await ready(overlay);
  const nativeDropdown = find(overlay.render(), node => node.type === 'DropdownItem');
  overlay.visible(false);
  nativeDropdown.props.onChange({ data: 'right' }); await settle();
  assert.equal(overlay.count('controller_set_gyro_source'), 1, 'native dropdown selection must save while QAM is hidden');
  overlay.respond('controller_set_gyro_source', { ...status, controlled: true, gyro_source: 'right', applied_source: 'right' });
  await settle(); overlay.unmount();
  // The rules both UIs share (src/controller.tsx and the Desktop ModulePage.qml import the same module).
  const G = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/controller.mjs')).href);
  assert.match(G.sourceNote({ gyro_source: 'left', applied_source: 'left', controlled: false }), /Not confirmed yet…$/,
    'matching filters alone are not confirmed routing');
  assert.doesNotMatch(G.sourceNote({ gyro_source: 'left', controlled: true }), /Not confirmed/);
  assert.doesNotMatch(G.sourceNote({ gyro_source: 'system', controlled: false }), /Not confirmed/);
  assert.equal(G.sourceLocked({ available: true }, true), true, 'no gyro change during the controller test');
  assert.equal(G.sourceLocked({ available: true, conflict: false }, false), false);
  assert.equal(G.streamText({ received: true, reports: 120, rate_hz: 125.44 }), '120 reports · 125.4 Hz');
  assert.equal(G.reportingText({ imu: { actual: null } }), 'left Unknown · right Unknown');
  console.log('Controller UI: bounded leases, hidden/unmount cleanup, late replies, unknown readings and source-write races verified.');
});
