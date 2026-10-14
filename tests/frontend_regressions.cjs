const complete = require('./frontend_test_guard.cjs')();
// Execute the actual TSX component with minimal React/Decky test doubles.
// Pending edits must be durably submitted when a view is closed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const state = {
  success: true, settings: { configured: true, control_enabled: true,
    rings_enabled: true, effect: 'monocolor', hue: 255, saturation: 100,
    brightness: 50, speed: 50, power_led_enabled: true },
  rgb: { supported: true, effects: [] }, power_led: { supported: false },
};
const slots = [], effects = new Map(), pendingEffects = [], timers = new Map(), calls = [];
let timerId = 0, cursor = 0;
const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => value === b[i]);
const hooks = {
  useState: initial => {
    const index = cursor++;
    if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
    return [slots[index], next => { slots[index] = typeof next === 'function' ? next(slots[index]) : next; }];
  },
  useRef: value => { const index = cursor++; return slots[index] ??= { current: value }; },
  useCallback: (fn, deps) => {
    const index = cursor++;
    if (!slots[index] || !same(slots[index].deps, deps)) slots[index] = { fn, deps };
    return slots[index].fn;
  },
  useEffect: (fn, deps) => {
    const index = cursor++, previous = effects.get(index);
    if (!previous || !same(previous.deps, deps)) pendingEffects.push(() => {
      previous?.cleanup?.(); effects.set(index, { deps, cleanup: fn() });
    });
  },
};
const api = { useQuickAccessVisible: () => true,
  callable: name => async (...args) => { calls.push({ name, args }); return structuredClone(state); } };
const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: 'Fragment' };
const moduleValue = { exports: {} };
const compiled = ts.transpileModule(fs.readFileSync(path.join(root, 'src/rgb.tsx'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
vm.runInNewContext(compiled, {
  module: moduleValue, exports: moduleValue.exports, console,
  setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
  clearTimeout: id => timers.delete(id), setInterval: () => 10000, clearInterval: () => {},
  require: name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsx;
    if (name === '@decky/api') return api;
    if (name === '@decky/ui') return new Proxy({}, { get: (_, name) => name });
    throw new Error(name);
  },
});
function render() {
  cursor = 0;
  const tree = moduleValue.exports.RgbPage();
  pendingEffects.splice(0).forEach(fn => fn());
  return tree;
}
function find(node, label) {
  if (!node || typeof node !== 'object') return undefined;
  if (node.props?.label === label) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const found = find(child, label); if (found) return found;
  }
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
(async () => {
  render(); await settle();
  const slider = find(render(), 'Brightness');
  assert.ok(slider, 'The real status RPC makes the brightness control available.');
  slider.props.onChange(73);
  assert.equal(timers.size, 1);
  const delay = [...timers.values()][0].delay;
  assert.ok(Number.isFinite(delay) && delay > 0 && delay <= 1000,
    'The pending gesture has a bounded debounce, without prescribing its exact tuning.');
  for (const effect of effects.values()) effect.cleanup?.(); // Navigate away before the gesture fires.
  assert.equal(timers.size, 0);
  await settle();
  assert.equal(calls.filter(c => c.name === 'rgb_set_brightness').length, 1);
  assert.equal(calls.find(c => c.name === 'rgb_set_brightness').args[0], 73);
  console.log('RGB brightness edit survives closing the view before debounce expires.');
})().then(complete).catch(error => { console.error(error); process.exitCode = 1; });
