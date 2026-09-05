const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const slots = [], effects = new Map(), pending = [], timers = new Set();
const rpcCalls = [];
let cursor = 0, visible = true, dirty = false, nextTimer = 0, modules = {};
const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => v === b[i]);
const hooks = {
  useState(initial) {
    const i = cursor++;
    if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
    return [slots[i], value => {
      const next = typeof value === 'function' ? value(slots[i]) : value;
      if (next !== slots[i]) { slots[i] = next; dirty = true; }
    }];
  },
  useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
  useCallback(fn, deps) {
    const i = cursor++;
    if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps };
    return slots[i].fn;
  },
  useMemo(fn, deps) {
    const i = cursor++;
    if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { value: fn(), deps };
    return slots[i].value;
  },
  useEffect(fn, deps) {
    const i = cursor++, previous = effects.get(i);
    if (!previous || !same(previous.deps, deps)) pending.push(() => {
      previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
    });
  },
};
const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
const mod = { exports: {} };
const source = fs.readFileSync(path.join(root, 'src/index.tsx'), 'utf8') + '\nexport { Controls as TestedContent };';
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
vm.runInNewContext(code, {
  module: mod, exports: mod.exports, console,
  setInterval: () => { const id = ++nextTimer; timers.add(id); return id; },
  clearInterval: id => timers.delete(id),
  require: name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return jsx;
    if (name === '@decky/api') return {
      useQuickAccessVisible: () => visible, definePlugin: fn => fn, addEventListener: () => {}, removeEventListener: () => {},
      callable: name => async () => { rpcCalls.push(name); return { version: 'test', blocked: false }; },
    };
    if (name === '@decky/ui') return new Proxy({ staticClasses: {} }, { get: (o, k) => o[k] || k });
    if (name.startsWith('./')) return new Proxy({}, { get: (_, k) =>
      k.startsWith('get') ? async () => { rpcCalls.push(k); return {}; } : /^(start|stop)/.test(k) ? () => {} : k.endsWith('Summary') ? () => '' : k });
    throw Error(name);
  },
});
let plugin;
function render() {
  // Decky 3.2.8 PluginView unmounts content on hide unless alwaysRender is set.
  // Merely changing useQuickAccessVisible in a mounted component misses this.
  if (!visible && !plugin.alwaysRender) {
    for (const effect of effects.values()) effect.cleanup?.();
    effects.clear(); slots.length = 0; pending.length = 0;
    return null;
  }
  let tree, passes = 0;
  do {
    assert.ok(++passes < 10, 'render loop'); dirty = false; cursor = 0;
    tree = mod.exports.TestedContent({modules}); pending.splice(0).forEach(fn => fn());
  } while (dirty);
  return tree;
}
function find(node, predicate) {
  if (!node || typeof node !== 'object') return;
  if (predicate(node)) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = find(child, predicate); if (result) return result;
  }
}
(async () => {
plugin = mod.exports.default();
// Controls consume the compatibility snapshot already read by the parent.
for (let i = 0; i < 16; i++) await Promise.resolve();
for (const title of ['TDP', 'Vibration', 'RGB Lighting', 'Button Remapper', 'Gyro & Touchpad', 'Battery', 'OLED Display', 'WiFi', 'About', 'Manage Modules']) {
  const link = find(render(), n => n.props?.title === title && n.props?.onClick);
  assert.ok(link, `section link: ${title}`); link.props.onClick();
  for (let cycle = 0; cycle < 3; cycle++) {
    visible = false; render(); visible = true;
    const page = render();
    assert.ok(find(page, n => n.props?.title === title && n.props?.onBack),
      `${title} must stay open after overlay dismissal or QAM reopening`);
    assert.equal(timers.size, 0, 'overview must not poll while a section is open');
  }
  find(render(), n => n.props?.onBack).props.onBack();
  assert.ok(find(render(), n => n.props?.title === title && n.props?.onClick), 'explicit back works');
  assert.equal(timers.size, 1, 'one overview poller after returning');
}
find(render(), n => n.props?.title === 'About' && n.props?.onClick).props.onClick();
assert.match(find(render(), n => n.props?.label === 'Author').props.description, /^Rayek/);
assert.doesNotMatch(find(render(), n => n.props?.label === 'Included modules').props.description, /\d+\.\d+/);
find(render(), n => n.props?.onBack).props.onBack();
find(render(), n => n.props?.title === 'Battery' && n.props?.onClick).props.onClick();
// Complete the earlier overview read before testing the next visibility cycle.
for (let i = 0; i < 12; i++) await Promise.resolve();
modules = Object.fromEntries(['tdp','vibration','rgb','remap','controller','battery','display','wifi'].map(k=>[k,{enabled:false}]));
rpcCalls.length = 0;
const disabled = render();
assert.deepEqual(rpcCalls, [], 'disabled modules are not polled and Controls does not duplicate the guard read');
assert.ok(!find(disabled, n => n.props?.onBack), 'disabling an open module returns to the overview');
for (const title of ['TDP','Vibration','RGB Lighting','Button Remapper','Gyro & Touchpad','Battery','OLED Display','WiFi']) {
  assert.ok(!find(disabled, n => n.props?.title === title && n.props?.onClick), `${title} hidden when disabled`);
}
for (const title of ['About','Manage Modules']) assert.ok(find(disabled, n => n.props?.title === title && n.props?.onClick));
visible = false; render(); assert.equal(timers.size, 0, 'hidden overview stops polling');
console.log('Ten sections retain navigation; disabled modules are hidden; About has author and unversioned modules.');
})().catch(error => { console.error(error); process.exitCode = 1; });
