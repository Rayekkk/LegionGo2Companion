const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const compiled = ts.transpileModule(fs.readFileSync(path.join(root, 'src/controller.tsx'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const status = {
  available: true, reason: '', gyro_source: 'system', applied_source: null,
  controlled: false, conflict: false, error: '', physical: { path: '/dev/test-physical', pid: 1, driver: 'test' },
  virtual: { path: '/dev/test-virtual', pid: 2 }, iio: [], diagnostics_active: false,
};
const emptyStream = () => ({ received: false, reports: 0, rate_hz: null, age_ms: null, sample: null });
const snapshot = (token = 'test-token', overrides = {}) => ({
  token, active: true, reason: '', elapsed_s: 0, remaining_s: 30,
  physical: emptyStream(), virtual: emptyStream(), ...overrides,
});

function harness(initiallyVisible = true) {
  const slots = [], effects = new Map(), pendingEffects = [], timers = new Map(), calls = [];
  let cursor = 0, visible = initiallyVisible, nextTimer = 0, writes = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => value === b[i]);
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = initial;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }];
    },
    useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps };
      return slots[i].fn;
    },
    useEffect(fn, deps) {
      const i = cursor++, previous = effects.get(i);
      if (!previous || !same(previous.deps, deps)) pendingEffects.push(() => {
        previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
      });
    },
  };
  const mod = { exports: {} };
  const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const timer = (kind, fn, ms) => { const id = ++nextTimer; timers.set(id, { kind, fn, ms }); return id; };
  vm.runInNewContext(compiled, {
    module: mod, exports: mod.exports, console,
    setTimeout: (fn, ms) => timer('timeout', fn, ms), clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => timer('interval', fn, ms), clearInterval: id => timers.delete(id),
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === '@decky/ui') return new Proxy({}, { get: (_, key) => key });
      if (name === '@decky/api') return { callable: name => (...args) => new Promise((resolve, reject) => {
        calls.push({ name, args, resolve, reject, settled: false });
      }) };
      throw Error(name);
    },
  });
  const render = () => {
    cursor = 0;
    const tree = mod.exports.ControllerPage({ visible });
    pendingEffects.splice(0).forEach(fn => fn());
    return tree;
  };
  const respond = (name, value, rejected = false) => {
    const call = calls.find(call => call.name === name && !call.settled);
    assert.ok(call, `pending RPC ${name}`);
    call.settled = true;
    if (rejected) call.reject(value); else call.resolve(value);
  };
  return {
    calls, timers, render, respond, exports: mod.exports,
    get writes() { return writes; },
    count: name => calls.filter(call => call.name === name).length,
    visible(value) { visible = value; return render(); },
    fire(kind) {
      const found = [...timers].find(([, value]) => value.kind === kind);
      if (!found) return false;
      const [id, entry] = found;
      if (kind === 'timeout') {
        assert.ok(entry.ms >= 250 && entry.ms <= 500, 'diagnostic cadence fits the 3-second lease');
        timers.delete(id);
      }
      entry.fn();
      return true;
    },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); },
  };
}

function find(node, predicate) {
  if (!node || typeof node !== 'object') return;
  if (predicate(node)) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = find(child, predicate);
    if (result) return result;
  }
}
function text(node) {
  if (!node) return '';
  if (typeof node !== 'object') return String(node);
  return [node.props?.title, node.props?.label, node.props?.description,
    ...(Array.isArray(node) ? node : [node.props?.children])].map(text).join(' ');
}
const startButton = h => find(h.render(), node => node.type === 'ButtonItem' && node.props.children === 'Start 30-Second Test');
async function ready(h) {
  h.render(); h.respond('controller_get_status', status); await settle(); h.render();
}
async function start(h, token = 'test-token') {
  startButton(h).props.onClick(); await settle();
  h.respond('controller_start_diagnostics', snapshot(token)); await settle(); h.render();
}

(async () => {
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
  assert.match(text(expired.render()), /No packets received yet/);
  assert.match(text(expired.render()), /does not establish that the sensor is faulty/);
  assert.match(text(expired.render()), /No reading received/);
  expired.fire('timeout');
  expired.respond('controller_get_diagnostics', snapshot('bounded-token', {
    active: false, elapsed_s: 30, remaining_s: 0, reason: '30-second limit reached',
    physical: { received: true, reports: 100, rate_hz: 20, age_ms: 10, sample: {
      gyro_left: { x: 0, y: 0, z: 0 }, gyro_right: null, touchpad: { x: 0, y: 0, is_touching: true },
    } },
  })); await settle();
  assert.equal([...expired.timers.values()].filter(timer => timer.kind === 'timeout').length, 0);
  assert.match(text(expired.render()), /X 0 · Y 0 · Z 0/);
  assert.match(text(expired.render()), /Touch detected · X 0\.000 · Y 0\.000/);
  assert.match(text(expired.render()), /30-second limit reached/);
  expired.unmount();

  // An older status response must not overwrite a user's source selection.
  const source = harness(); await ready(source); source.fire('interval');
  const dropdown = find(source.render(), node => node.type === 'DropdownItem');
  dropdown.props.onChange({ data: 'left' }); dropdown.props.onChange({ data: 'right' });
  await settle(); assert.equal(source.count('controller_set_gyro_source'), 0, 'write waits for the earlier read');
  source.respond('controller_get_status', { ...status, gyro_source: 'combined' }); await settle();
  assert.equal(source.count('controller_set_gyro_source'), 1, 'duplicate changes do not queue writes');
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
  console.log('Controller UI: bounded leases, hidden/unmount cleanup, late replies, unknown readings and source-write races verified.');
})().catch(error => { console.error(error); process.exitCode = 1; });
