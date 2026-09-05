// Exercise the actual update UI with deferred RPCs and page/plugin lifecycles.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function harness() {
  const slots = [], effects = new Map(), pending = [], calls = [];
  let cursor = 0, writes = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => value === b[i]);
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useEffect(fn, deps) { const i = cursor++, previous = effects.get(i);
      if (!previous || !same(previous.deps, deps)) pending.push(() => { previous?.cleanup?.(); effects.set(i, { deps, cleanup: fn() }); }); },
  };
  const mod = { exports: {} };
  const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const forbiddenTimer = () => { throw Error('Update UI must not create background timers.'); };
  const source = ts.transpileModule(fs.readFileSync(path.join(root, 'src/updates.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  vm.runInNewContext(source, {
    module: mod, exports: mod.exports, console, Error,
    setTimeout: forbiddenTimer, setInterval: forbiddenTimer,
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === '@decky/ui') return new Proxy({}, { get: (_, key) => key });
      if (name === '@decky/api') return { callable: name => (...args) => new Promise((resolve, reject) => calls.push({ name, args, resolve, reject, settled: false })) };
      throw Error(name);
    },
  });
  const render = () => { cursor = 0; const result = mod.exports.UpdateSection({ currentVersion: '0.6.2' }); pending.splice(0).forEach(fn => fn()); return result; };
  const next = name => { const call = calls.find(call => call.name === name && !call.settled); assert.ok(call, `Pending ${name}`); call.settled = true; return call; };
  mod.exports.startUpdates();
  return {
    calls, render, get writes() { return writes; },
    respond(name, value) { next(name).resolve(value); },
    reject(name) { next(name).reject(new Error('Network unavailable')); },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); },
    remount() { slots.length = 0; pending.length = 0; return render(); },
    start: mod.exports.startUpdates, stop: mod.exports.stopUpdates,
  };
}

function nodes(node, result = []) {
  if (Array.isArray(node)) { for (const child of node) nodes(child, result); return result; }
  if (!node || typeof node !== 'object') return result;
  result.push(node); nodes(node.props?.children, result); return result;
}
function button(tree, label) {
  const result = nodes(tree).find(node => node.type === 'ButtonItem' && node.props.children === label);
  assert.ok(result, `Button: ${label}`); return result;
}
function field(tree, label) { return nodes(tree).find(node => node.type === 'Field' && node.props.label === label); }
const check = (patch = {}) => ({ success: true, current_version: '0.6.2', latest_version: '0.6.3', update_available: true, download_available: true, size: 1048576, ...patch });
const downloaded = { success: true, version: '0.6.3', path: '/home/deck/Downloads/LegionGo2Companion-0.6.3.zip', sha256: 'a'.repeat(64) };

(async () => {
  const h = harness();
  let tree = h.render();
  assert.ok(field(tree, 'GitHub releases'));
  h.unmount(); tree = h.remount(); await settle();
  assert.equal(h.calls.length, 0, 'Mounting and remounting never check automatically');
  const initial = button(tree, 'Check for Updates');
  initial.props.onClick(); initial.props.onClick();
  assert.equal(h.calls.length, 1, 'Synchronous repeated clicks create one RPC');
  assert.equal(button(h.render(), 'Checking GitHub…').props.disabled, true);
  h.unmount(); const writes = h.writes;
  h.respond('updates_check', check()); await settle();
  assert.equal(h.writes, writes, 'Unmounted page has no subscribed state writes');
  tree = h.remount();
  assert.ok(field(tree, 'Update available'), 'Delayed reply is preserved across a page remount');
  assert.equal(h.calls.length, 1, 'Remount does not restart the request');
  const download = button(tree, 'Download 0.6.3');
  download.props.onClick(); download.props.onClick(); button(tree, 'Check for Updates').props.onClick();
  assert.equal(h.calls.length, 2, 'Neither stale button closure bypasses the shared busy guard');
  assert.deepEqual(Array.from(h.calls[1].args), ['0.6.3'], 'Downloads the version shown to the user');
  tree = h.render();
  assert.equal(button(tree, 'Check for Updates').props.disabled, true);
  assert.equal(button(tree, 'Downloading ZIP…').props.disabled, true);
  h.unmount(); tree = h.remount();
  button(tree, 'Check for Updates').props.onClick();
  assert.equal(h.calls.length, 2, 'Remount preserves the busy operation');
  h.respond('updates_download', downloaded); await settle();
  assert.ok(field(h.render(), 'Version 0.6.3 downloaded'));
  h.unmount(); assert.ok(field(h.remount(), 'Version 0.6.3 downloaded'), 'Download path survives reopening About');
  assert.equal(h.calls.length, 2);

  const changed = harness(); button(changed.render(), 'Check for Updates').props.onClick(); changed.respond('updates_check', check()); await settle();
  const previousButton = button(changed.render(), 'Download 0.6.3');
  button(changed.render(), 'Check for Updates').props.onClick(); changed.respond('updates_check', check({ latest_version: '0.6.4' })); await settle();
  previousButton.props.onClick();
  assert.equal(changed.calls.length, 2, 'A stale button cannot silently download a newly discovered version');
  button(changed.render(), 'Download 0.6.4').props.onClick();
  assert.deepEqual(Array.from(changed.calls[2].args), ['0.6.4']);
  changed.respond('updates_download', { ...downloaded, version: '0.6.4', path: '/home/deck/Downloads/new.zip' }); await settle();

  const failures = harness();
  button(failures.render(), 'Check for Updates').props.onClick();
  failures.reject('updates_check'); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /Network unavailable/);
  button(failures.render(), 'Check for Updates').props.onClick();
  failures.respond('updates_check', { success: false, error: 'GitHub rate limit reached' }); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /rate limit/);
  button(failures.render(), 'Check for Updates').props.onClick();
  failures.respond('updates_check', check()); await settle();
  assert.equal(field(failures.render(), 'Could not complete'), undefined, 'Successful retry clears the error');
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.respond('updates_download', { success: false, error: 'Checksum mismatch' }); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /Checksum mismatch/);
  assert.equal(field(failures.render(), 'Version 0.6.3 downloaded'), undefined);
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.reject('updates_download'); await settle();
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.respond('updates_download', downloaded); await settle();
  assert.ok(field(failures.render(), 'Version 0.6.3 downloaded'), 'Download can retry after backend and transport failures');

  for (const [response, label] of [
    [check({ no_release: true, latest_version: undefined, update_available: false, download_available: false }), 'No public release yet'],
    [check({ download_available: false }), 'New release found'],
    [check({ latest_version: '0.6.2', update_available: false }), 'Up to date'],
    [check({ latest_version: '0.6.1', update_available: false }), 'No newer release'],
  ]) {
    const view = harness(); button(view.render(), 'Check for Updates').props.onClick(); view.respond('updates_check', response); await settle();
    assert.ok(field(view.render(), label), label);
    assert.equal(nodes(view.render()).filter(node => node.type === 'ButtonItem').length, 1, 'No unusable download action');
  }

  const assetError = harness(); button(assetError.render(), 'Check for Updates').props.onClick();
  assetError.respond('updates_check', check({ download_available: false, error: 'Missing SHA256 checksum for the release ZIP.' })); await settle();
  assert.ok(field(assetError.render(), 'New release found'), 'An unusable asset still identifies the newer release');
  assert.match(field(assetError.render(), 'Could not complete').props.description, /Missing SHA256/, 'Shows the specific asset problem from successful metadata lookup');
  button(assetError.render(), 'Check for Updates').props.onClick(); assetError.respond('updates_check', check()); await settle();
  assert.equal(field(assetError.render(), 'Could not complete'), undefined, 'Valid metadata clears the prior asset problem');

  for (const operation of ['updates_check', 'updates_download']) {
    const lifecycle = harness(); button(lifecycle.render(), 'Check for Updates').props.onClick();
    if (operation === 'updates_download') { lifecycle.respond('updates_check', check()); await settle(); button(lifecycle.render(), 'Download 0.6.3').props.onClick(); }
    lifecycle.stop(); lifecycle.unmount(); lifecycle.start(); lifecycle.remount();
    button(lifecycle.render(), 'Check for Updates').props.onClick();
    lifecycle.respond(operation, operation === 'updates_check' ? check() : downloaded); await settle();
    assert.ok(field(lifecycle.render(), 'GitHub releases'), 'A previous plugin session cannot publish its result');
    assert.equal(button(lifecycle.render(), 'Checking GitHub…').props.disabled, true, 'Stale finally cannot clear the new operation');
    lifecycle.respond('updates_check', check({ no_release: true, latest_version: undefined, update_available: false })); await settle();
    assert.ok(field(lifecycle.render(), 'No public release yet'));
  }

  const invalid = harness(); button(invalid.render(), 'Check for Updates').props.onClick(); invalid.respond('updates_check', check()); await settle();
  button(invalid.render(), 'Download 0.6.3').props.onClick(); invalid.respond('updates_download', { ...downloaded, version: '0.6.4' }); await settle();
  assert.ok(field(invalid.render(), 'Could not complete'), 'Does not claim that a different version was downloaded');
  assert.equal(field(invalid.render(), 'Version 0.6.4 downloaded'), undefined);
  invalid.stop(); button(invalid.render(), 'Check for Updates').props.onClick();
  assert.equal(invalid.calls.length, 2, 'Stopped plugin cannot start another request');
  console.log('Frontend updates: manual requests, retries, concurrent clicks, cached remounts and lifecycle isolation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
