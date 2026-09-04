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
const effects = [], timers = new Map(), calls = [];
let timerId = 0, stateIndex = 0;
const hooks = {
  useState: initial => {
    const index = stateIndex++;
    let value = index === 0 ? structuredClone(state) : initial;
    return [value, next => { value = typeof next === 'function' ? next(value) : next; }];
  },
  useRef: value => ({ current: value }),
  useCallback: fn => fn,
  useEffect: fn => effects.push(fn),
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
const tree = moduleValue.exports.RgbPage();
const cleanups = effects.map(fn => fn()).filter(fn => typeof fn === 'function');
function find(node, label) {
  if (!node || typeof node !== 'object') return undefined;
  if (node.props?.label === label) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const found = find(child, label); if (found) return found;
  }
}
const slider = find(tree, 'Brightness');
assert.ok(slider);
slider.props.onChange(73);
assert.equal(timers.size, 1);
assert.equal([...timers.values()][0].delay, 450);
cleanups.forEach(fn => fn()); // Navigate to All Controls before 450 ms.
assert.equal(timers.size, 0);
Promise.resolve().then(() => {
  assert.equal(calls.filter(c => c.name === 'rgb_set_brightness').length, 1);
  assert.equal(calls.find(c => c.name === 'rgb_set_brightness').args[0], 73);
  console.log('RGB brightness edit survives closing the view before debounce expires.');
});
