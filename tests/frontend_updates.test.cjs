// Exercise the actual update UI with deferred RPCs and page/plugin lifecycles.
const { assert, run, settle: wait, mount, nodes, button: findButton } = require('./legion_harness.cjs');
const settle = () => wait(12);
function harness() {
  const h = mount('updates.tsx', { component: 'UpdateSection', deferAll: true, props: { currentVersion: '0.6.2' } });
  h.exports.startUpdates();
  return {
    calls: h.calls, render: () => h.render(), get writes() { return h.writes; },
    fireTimers() { h.fire('timeout'); h.fire('interval'); },
    respond: h.respond, reject: name => h.reject(name, 'Network unavailable'),
    unmount: h.unmount, remount: () => h.remount(),
    start: h.exports.startUpdates, stop: h.exports.stopUpdates,
  };
}
function button(tree, text) { const result = findButton(tree, text); assert.ok(result, `Button: ${text}`); return result; }
const field = (tree, name) => nodes(tree).find(node => node.type === 'Field' && node.props.label === name);
const check = (patch = {}) => ({ success: true, current_version: '0.6.2', latest_version: '0.6.3', update_available: true, download_available: true, size: 1048576, ...patch });
const downloaded = { success: true, version: '0.6.3', path: '/home/deck/Downloads/LegionGo2Companion-0.6.3.zip', sha256: 'a'.repeat(64) };

run(async () => {
  for (const operation of ['updates_check', 'updates_download']) {
    const closed=harness();let tree=closed.render();
    if(operation==='updates_download') {
      button(tree,'Check for updates').props.onClick();closed.respond('updates_check',check());await settle();tree=closed.render();
    }
    const callback=button(tree,operation==='updates_check'?'Check for updates':'Download 0.6.3').props.onClick;
    closed.unmount();const calls=closed.calls.length,writes=closed.writes;
    callback();await settle();
    assert.equal(closed.calls.length,calls,'A callback from a closed About page cannot initiate another request.');
    assert.equal(closed.writes,writes);
  }
  const h = harness();
  let tree = h.render();
  assert.ok(field(tree, 'GitHub releases'));
  h.unmount(); tree = h.remount(); await settle();
  h.fireTimers();await settle();
  assert.equal(h.calls.length, 0, 'Mounting, remounting and scheduled callbacks never check automatically');
  const initial = button(tree, 'Check for updates');
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
  download.props.onClick(); download.props.onClick(); button(tree, 'Check for updates').props.onClick();
  assert.equal(h.calls.length, 2, 'Neither stale button closure bypasses the shared busy guard');
  assert.deepEqual(Array.from(h.calls[1].args), ['0.6.3'], 'Downloads the version shown to the user');
  tree = h.render();
  assert.equal(button(tree, 'Check for updates').props.disabled, true);
  assert.equal(button(tree, 'Downloading ZIP…').props.disabled, true);
  h.unmount(); tree = h.remount();
  button(tree, 'Check for updates').props.onClick();
  assert.equal(h.calls.length, 2, 'Remount preserves the busy operation');
  h.respond('updates_download', downloaded); await settle();
  assert.ok(field(h.render(), '0.6.3 downloaded'));
  assert.equal(field(h.render(), '0.6.3 downloaded').props.description.props.children,
    `Saved to ${downloaded.path}. Install it from Decky settings → Developer → Install plugin from ZIP.`);
  h.unmount(); assert.ok(field(h.remount(), '0.6.3 downloaded'), 'Download path survives reopening About');
  assert.equal(h.calls.length, 2);

  const changed = harness(); button(changed.render(), 'Check for updates').props.onClick(); changed.respond('updates_check', check()); await settle();
  const previousButton = button(changed.render(), 'Download 0.6.3');
  button(changed.render(), 'Check for updates').props.onClick(); changed.respond('updates_check', check({ latest_version: '0.6.4' })); await settle();
  previousButton.props.onClick();
  assert.equal(changed.calls.length, 2, 'A stale button cannot silently download a newly discovered version');
  button(changed.render(), 'Download 0.6.4').props.onClick();
  assert.deepEqual(Array.from(changed.calls[2].args), ['0.6.4']);
  changed.respond('updates_download', { ...downloaded, version: '0.6.4', path: '/home/deck/Downloads/new.zip' }); await settle();

  const failures = harness();
  button(failures.render(), 'Check for updates').props.onClick();
  failures.reject('updates_check'); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /Network unavailable/);
  button(failures.render(), 'Check for updates').props.onClick();
  failures.respond('updates_check', { success: false, error: 'GitHub rate limit reached' }); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /rate limit/);
  button(failures.render(), 'Check for updates').props.onClick();
  failures.respond('updates_check', check()); await settle();
  assert.equal(field(failures.render(), 'Could not complete'), undefined, 'Successful retry clears the error');
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.respond('updates_download', { success: false, error: 'Checksum mismatch' }); await settle();
  assert.match(field(failures.render(), 'Could not complete').props.description, /Checksum mismatch/);
  assert.equal(field(failures.render(), '0.6.3 downloaded'), undefined);
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.reject('updates_download'); await settle();
  button(failures.render(), 'Download 0.6.3').props.onClick();
  failures.respond('updates_download', downloaded); await settle();
  assert.ok(field(failures.render(), '0.6.3 downloaded'), 'Download can retry after backend and transport failures');

  for (const [response, label] of [
    [check({ no_release: true, latest_version: undefined, update_available: false, download_available: false }), 'No public release yet'],
    [check({ download_available: false }), 'New release found'],
    [check({ latest_version: '0.6.2', update_available: false }), 'Up to date'],
    [check({ latest_version: '0.6.1', update_available: false }), 'No newer release'],
  ]) {
    const view = harness(); button(view.render(), 'Check for updates').props.onClick(); view.respond('updates_check', response); await settle();
    assert.ok(field(view.render(), label), label);
    assert.equal(nodes(view.render()).filter(node => node.type === 'ButtonItem').length, 1, 'No unusable download action');
  }

  const assetError = harness(); button(assetError.render(), 'Check for updates').props.onClick();
  assetError.respond('updates_check', check({ download_available: false, error: 'Missing SHA256 checksum for the release ZIP.' })); await settle();
  assert.ok(field(assetError.render(), 'New release found'), 'An unusable asset still identifies the newer release');
  assert.match(field(assetError.render(), 'Could not complete').props.description, /Missing SHA256/, 'Shows the specific asset problem from successful metadata lookup');
  button(assetError.render(), 'Check for updates').props.onClick(); assetError.respond('updates_check', check()); await settle();
  assert.equal(field(assetError.render(), 'Could not complete'), undefined, 'Valid metadata clears the prior asset problem');

  for (const operation of ['updates_check', 'updates_download']) {
    const lifecycle = harness(); button(lifecycle.render(), 'Check for updates').props.onClick();
    if (operation === 'updates_download') { lifecycle.respond('updates_check', check()); await settle(); button(lifecycle.render(), 'Download 0.6.3').props.onClick(); }
    lifecycle.stop(); lifecycle.unmount(); lifecycle.start(); lifecycle.remount();
    button(lifecycle.render(), 'Check for updates').props.onClick();
    lifecycle.respond(operation, operation === 'updates_check' ? check() : downloaded); await settle();
    assert.ok(field(lifecycle.render(), 'GitHub releases'), 'A previous plugin session cannot publish its result');
    assert.equal(button(lifecycle.render(), 'Checking GitHub…').props.disabled, true, 'Stale finally cannot clear the new operation');
    lifecycle.respond('updates_check', check({ no_release: true, latest_version: undefined, update_available: false })); await settle();
    assert.ok(field(lifecycle.render(), 'No public release yet'));
  }

  const invalid = harness(); button(invalid.render(), 'Check for updates').props.onClick(); invalid.respond('updates_check', check()); await settle();
  button(invalid.render(), 'Download 0.6.3').props.onClick(); invalid.respond('updates_download', { ...downloaded, version: '0.6.4' }); await settle();
  assert.ok(field(invalid.render(), 'Could not complete'), 'Does not claim that a different version was downloaded');
  assert.equal(field(invalid.render(), '0.6.4 downloaded'), undefined);
  invalid.stop(); button(invalid.render(), 'Check for updates').props.onClick();
  assert.equal(invalid.calls.length, 2, 'Stopped plugin cannot start another request');

  // Device report (About): one RPC per click, the saved path or the error, nothing before a click.
  const report = mount('updates.tsx', { component: 'DeviceReportSection', deferAll: true });
  tree = report.render();
  assert.match(field(tree, 'Save device report').props.description, /No serial numbers, network names or user names\.$/);
  assert.equal(report.calls.length, 0, 'Opening About saves nothing');
  button(tree, 'Save device report').props.onClick(); button(tree, 'Save device report').props.onClick();
  assert.deepEqual(report.calls.map(call => call.name), ['save_device_report'], 'A double click saves one report');
  assert.equal(button(report.render(), 'Saving report…').props.disabled, true);
  const savedPath = '/home/deck/Desktop/LegionGoCompanion-device-report-20261021-120000.txt';
  report.respond('save_device_report', { success: true, path: savedPath }); await settle();
  assert.equal(field(report.render(), 'Device report').props.description.props.children, `Saved to ${savedPath}`);
  button(report.render(), 'Save device report').props.onClick();
  report.respond('save_device_report', { success: false, error: 'The Desktop directory does not belong to the console user.' }); await settle();
  assert.equal(field(report.render(), 'Device report').props.description.props.children, 'The Desktop directory does not belong to the console user.');
  button(report.render(), 'Save device report').props.onClick();
  report.reject('save_device_report', new Error('Backend unavailable')); await settle();
  assert.match(field(report.render(), 'Device report').props.description.props.children, /Backend unavailable/);
  console.log('Frontend updates: manual requests, retries, concurrent clicks, cached remounts and lifecycle isolation passed.');
});
