// About: info first, the device report, Updates last (DeckyKit about.tsx, whose own guarantees are tested in DeckyKit);
// here the plugin's wiring: the update RPCs and their D2 answers, ~/ paths, one updater per plugin load.
const { assert, run, settle: wait, mount, find, nodes, button: findButton } = require('./legion_harness.cjs');
const settle = () => wait(12);
function button(tree, text) { const result = findButton(tree, text); assert.ok(result, `Button: ${text}`); return result; }
const field = (tree, name) => nodes(tree).find(node => node.type === 'Field' && node.props.label === name);
const text = node => node == null || typeof node !== 'object' ? String(node ?? '')
  : Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
const check = (patch = {}) => ({ current_version: '0.6.2', latest_version: '0.6.3', update_available: true, download_available: true, size: 1048576, ...patch });

run(async () => {
  const h = mount('index.tsx', { component: 'C', append: 'export { Controls as C };', ui: { staticClasses: {} }, deferAll: true,
    modules: name => name.startsWith('./') ? new Proxy({}, { get: (_, key) => /^(start|stop)/.test(key) ? () => {} : key.endsWith('Summary') ? () => ''
      : key.startsWith('get') ? () => new Promise(() => {}) : key }) : undefined });
  const plugin = h.exports.default();
  const props = { modules: {}, device: { console: 'go2', model: '83N0' }, home: '/home/deck' };
  find(h.render(props), n => n.props?.title === 'About' && n.props?.onOpen).props.onOpen();
  const about = find(h.render(props), n => n.type?.name === 'AboutPage');
  assert.ok(about, 'About is the shared AboutPage');
  assert.equal(about.props.license, 'BSD-3-Clause');
  assert.equal(about.props.home, '/home/deck');
  const page = about.type(about.props).props.children.filter(Boolean);
  assert.deepEqual([...page].map(n => n.type?.name ?? n.type), ['PanelSection', 'DeviceReportSection', 'UpdateSection'], 'info, the device report, Updates last');

  // The updater asks the plugin's own RPCs and reads their D2 answers.
  const view = mount('about.tsx', { component: 'UpdateSection' });
  const section = () => view.render({ controller: about.props.updates, version: '0.6.2', home: '/home/deck' });
  button(section(), 'Check for updates').props.onClick();
  h.respond('updates_check', { ok: true, state: check() }); await settle();
  assert.ok(field(section(), 'Update available'));
  button(section(), 'Download 0.6.3').props.onClick();
  assert.deepEqual([...h.pending('updates_download').args], ['0.6.3'], 'the download names the version the button showed');
  h.respond('updates_download', { ok: true, state: { version: '0.6.3', path: '/home/deck/Downloads/LegionGo2Companion-0.6.3.zip' } }); await settle();
  assert.match(text(field(section(), '0.6.3 downloaded').props.description), /^Saved to ~\/Downloads\/LegionGo2Companion-0\.6\.3\.zip\./);
  button(section(), 'Check for updates').props.onClick();
  h.respond('updates_check', { ok: false, error: 'GitHub is unavailable.' }); await settle();
  assert.equal(text(field(section(), 'Could not complete').props.description), 'GitHub is unavailable.');
  assert.ok(field(section(), '0.6.3 downloaded'), 'a failed re-check keeps the saved ZIP on screen');

  // A plugin unload ends its updater: an old About page starts nothing.
  const calls = h.calls.length;
  plugin.onDismount();
  button(section(), 'Check for updates').props.onClick(); await settle();
  assert.equal(h.calls.length, calls, 'an unloaded plugin starts no update request');
  view.unmount(); h.unmount();

  // Device report (About): one RPC per click, the saved path (inside the home folder as ~/…) or the error, nothing before a click.
  const report = mount('report.tsx', { component: 'DeviceReportSection', deferAll: true, props: { home: '/home/deck' } });
  let tree = report.render();
  assert.match(field(tree, 'Save device report').props.description, /No serial numbers, network names or user names\.$/);
  assert.equal(report.calls.length, 0, 'Opening About saves nothing');
  button(tree, 'Save device report').props.onClick(); button(tree, 'Save device report').props.onClick();
  assert.deepEqual(report.calls.map(call => call.name), ['save_device_report'], 'A double click saves one report');
  assert.equal(button(report.render(), 'Saving report…').props.disabled, true);
  report.respond('save_device_report', { ok: true, state: { path: '/home/deck/Desktop/LegionGoCompanion-device-report-20261021-120000.txt' } }); await settle();
  assert.equal(field(report.render(), 'Device report').props.description.props.children, 'Saved to ~/Desktop/LegionGoCompanion-device-report-20261021-120000.txt');
  button(report.render(), 'Save device report').props.onClick();
  report.respond('save_device_report', { ok: false, error: 'The Desktop directory does not belong to the console user.' }); await settle();
  assert.equal(field(report.render(), 'Device report').props.description.props.children, 'The Desktop directory does not belong to the console user.');
  button(report.render(), 'Save device report').props.onClick();
  report.reject('save_device_report', new Error('Backend unavailable')); await settle();
  assert.match(field(report.render(), 'Device report').props.description.props.children, /Backend unavailable/);
  console.log('Frontend About: shared page order, update RPCs with D2 answers, ~/ paths, unload and the device report passed.');
});
