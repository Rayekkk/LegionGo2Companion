// Shared harness: run a real src/*.tsx module in a vm with array-indexed fake
// React hooks, a jsx factory producing {type, props} trees, a @decky/ui Proxy
// (unknown names become strings), fake timers and recorded/deferred RPCs.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
// These src/ modules always run for real, even where tests stub other pages; so does every desktop/native/*.mjs.
const REAL_MODULES = new Set(['./shared', './result', './tdp_api', './tdp_watcher', './tdp_cpu', './tdp_state', './tdp_sections']);

// Fake timers leave no native event-loop handle, so a pending top-level promise
// would let Node exit successfully; require explicit completion instead.
function run(main) {
  let completed = false;
  process.once('beforeExit', () => {
    if (!completed && !process.exitCode) {
      console.error('Frontend test did not complete: an awaited operation is still pending.');
      process.exitCode = 1;
    }
  });
  Promise.resolve().then(main).then(() => { completed = true; })
    .catch(error => { console.error(error); process.exitCode = 1; });
}
const settle = async (turns = 30) => { for (let i = 0; i < turns; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject }; };
const transpile = (file, append = '') => ts.transpileModule(fs.readFileSync(path.join(root, 'src', file), 'utf8') + append, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText;

// Tree helpers. Nodes are {type, props}; children and descriptions may nest nodes.
const childrenOf = node => Array.isArray(node) ? node : [node.props?.children, node.props?.description];
function find(node, predicate) {
  if (!node || typeof node !== 'object') return undefined;
  if (!Array.isArray(node) && predicate(node)) return node;
  for (const child of childrenOf(node)) { const found = find(child, predicate); if (found) return found; }
}
function nodes(node, out = []) {
  if (!node || typeof node !== 'object') return out;
  if (!Array.isArray(node)) out.push(node);
  for (const child of childrenOf(node)) nodes(child, out);
  return out;
}
const label = (tree, name) => find(tree, n => n.props?.label === name);
const buttonText = n => Array.isArray(n.props?.children) ? n.props.children.join('') : n.props?.children;
const button = (tree, name) => find(tree, n => n.type === 'ButtonItem' && buttonText(n) === name);
const component = (tree, name) => find(tree, n => typeof n.type === 'function' && n.type.name === name);
const text = node => node == null || node === false ? '' : typeof node !== 'object' ? String(node)
  : Array.isArray(node) ? node.map(text).join(' ')
  : [node.props?.title, node.props?.label, node.props?.description, node.props?.children].map(text).join(' ');

// mount(file, options) → harness. Options:
//   component: export rendered by render(); append: source appended (test-only exports)
//   responses: {rpc: value | fn(...args)}; missing RPCs return `fallback` (default {success:true})
//   strict: reject RPCs without a response; deferAll: every RPC stays pending until respond/reject; api/ui/window/globals: extra context
//   modules: {'./x': value} or (name) => value for stubbed relative imports
//   clock: initial Date.now()/performance.now() value (fake clock), visible: QAM visibility
function mount(file, options = {}) {
  const slots = [], effects = new Map(), pendingEffects = [], timers = new Map(), calls = [], deferCounts = new Map();
  let cursor = 0, nextTimer = 0, writes = 0, changed = false, visible = options.visible !== false;
  const clock = { now: options.clock ?? 0 };
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { writes++; const next = typeof value === 'function' ? value(slots[i]) : value;
        if (!Object.is(next, slots[i])) changed = true; slots[i] = next; }];
    },
    useRef(value) { const i = cursor++; if (!(i in slots)) slots[i] = { current: value }; return slots[i]; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useMemo(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { value: fn(), deps }; return slots[i].value; },
    useEffect(fn, deps) {
      const i = cursor++, previous = effects.get(i);
      if (!previous || !same(previous.deps, deps)) pendingEffects.push(() => {
        previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() }); });
    },
  };
  const responses = options.responses ?? {}; // by reference: tests may swap replies later
  const fallback = 'fallback' in options ? options.fallback : { success: true };
  const callable = name => (...args) => {
    const call = { name, args, settled: false }; calls.push(call);
    const deferredCount = deferCounts.get(name) ?? 0;
    if (options.deferAll || deferredCount > 0) {
      if (deferredCount > 0) deferCounts.set(name, deferredCount - 1);
      return new Promise((resolve, reject) => Object.assign(call, { resolve, reject }));
    }
    call.settled = true;
    if (options.strict && !(name in responses)) return Promise.reject(Error(`Unexpected RPC: ${name}`));
    const response = name in responses ? responses[name] : fallback;
    try { return Promise.resolve(typeof response === 'function' ? response(...args) : structuredClone(response)); }
    catch (error) { return Promise.reject(error); }
  };
  // The backend answers D2 ({ok, state}) around the payload a test gives (a refusal with its payload reads the same
  // through the panel's rpc helper); a rejection stays one. h.callable keeps payloads for modules stubbed by tests.
  const d2 = answer => answer && typeof answer === 'object' && 'ok' in answer ? answer : { ok: true, state: answer };
  const listeners = {};
  const answers = options.api?.callable ?? callable;  // a test's own fake answers with payloads too
  const api = { useQuickAccessVisible: () => visible, definePlugin: fn => fn, toaster: { toast() {} },
    addEventListener: (name, fn) => { listeners[name] = fn; return fn; },
    removeEventListener: (name, fn) => { if (!fn || listeners[name] === fn) delete listeners[name]; }, ...options.api,
    callable: name => (...args) => Promise.resolve(answers(name)(...args)).then(d2) };
  const ui = new Proxy({ ...options.ui }, { get: (target, key) => key in target ? target[key] : key });
  const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: 'Fragment' };
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  class FakeDate extends Date { static now() { return clock.now; } }
  const context = vm.createContext({ console, window: {}, Error, __PLUGIN_VERSION__: require(path.join(root, 'package.json')).version,
    setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
    ...('clock' in options ? { Date: FakeDate, performance: { now: () => clock.now } } : {}),
    ...(options.window ? { window: options.window } : {}), ...options.globals });
  const loaded = new Map();
  const load = (name, source = transpile(name)) => {
    const mod = { exports: {} };
    loaded.set(name, mod);
    vm.runInContext(`(function (module, exports, require) {${source}\n})`, context)(mod, mod.exports, request => {
      if (request === 'react') return hooks;
      if (request === 'react/jsx-runtime') return jsx;
      if (request === '@decky/api') return api;
      if (request === '@decky/ui') return ui;
      if (REAL_MODULES.has(request)) {
        const base = request.slice(2), file = fs.existsSync(path.join(root, 'src', base + '.tsx')) ? base + '.tsx' : base + '.ts';
        return (loaded.get(file) ?? load(file)).exports;
      }
      if (request.startsWith('../desktop/native/')) return (loaded.get(request) ?? load(request)).exports;
      const stub = typeof options.modules === 'function' ? options.modules(request) : options.modules?.[request];
      if (stub !== undefined) return stub;
      throw Error(request);
    });
    return mod;
  };
  const exports = load(file, transpile(file, options.append ? `\n${options.append}` : '')).exports;
  let props = options.props ?? {};
  const pending = name => { const call = calls.find(c => c.name === name && c.resolve && !c.settled);
    assert.ok(call, `pending RPC ${name}`); return call; };
  const h = {
    exports, calls, timers, listeners, responses, clock, callable,
    get writes() { return writes; }, get changed() { return changed; }, set changed(value) { changed = value; },
    render(nextProps = props, fn = exports[options.component]) {
      props = nextProps; cursor = 0; const tree = fn(props);
      options.onRender?.(tree); pendingEffects.splice(0).forEach(run => run()); return tree;
    },
    setVisible(value) { visible = value; return h.render(); },
    hide() { return h.setVisible(false); }, show() { return h.setVisible(true); },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); },
    reset() { h.unmount(); slots.length = 0; pendingEffects.length = 0; },
    remount(nextProps) { slots.length = 0; pendingEffects.length = 0; return h.render(nextProps); },
    count: name => calls.filter(c => c.name === name).length,
    defer(name, count = 1) { deferCounts.set(name, count); },
    pending,
    settle(call, value, rejected = false) { call.settled = true; rejected ? call.reject(value) : call.resolve(value); },
    respond(name, value) { h.settle(pending(name), value); },
    reject(name, error = new Error('temporary RPC failure')) {
      h.settle(pending(name), typeof error === 'string' ? new Error(error) : error, true); },
    timeouts: () => [...timers.values()].filter(t => t.kind === 'timeout'),
    intervals: () => [...timers.values()].filter(t => t.kind === 'interval'),
    // Fire matching timers; fired timeouts are removed like real one-shot timers.
    fire(kind = 'interval', delay) {
      for (const [id, t] of [...timers]) if (t.kind === kind && (delay === undefined || t.delay === delay)) {
        if (kind === 'timeout') timers.delete(id); t.fn(); }
    },
    flushTimeouts() { h.fire('timeout'); },
    advance(ms) { clock.now += ms; },
  };
  return h;
}

module.exports = { assert, run, settle, deferred, mount, find, nodes, label, button, component, text };
