// SPDX-License-Identifier: BSD-3-Clause
// Each Legion Go shows only what applies to it (get_version modules[].applicable and device); a Go 2 83N0 keeps everything.
const { assert, run, settle, mount, find, nodes, text, label, button } = require('./frontend_harness.cjs');

const NAMES = ['tdp', 'fan', 'vibration', 'rgb', 'remap', 'controller', 'battery', 'display', 'wifi'];
const TITLES = { tdp: 'TDP', fan: 'Fan Control', vibration: 'Vibration', rgb: 'Lighting', remap: 'Button mapping',
  controller: 'Gyro & touchpad', battery: 'Battery', display: 'Display', wifi: 'Wi-Fi' };
const status = hidden => Object.fromEntries(NAMES.map(name => [name, { enabled: true, ready: true, applicable: !hidden.includes(name) }]));
const GO2 = { modules: status([]), device: { console: 'go2', model: '83N0', firmware: true, power_led: true } };
const GO_S = { modules: status(['fan', 'vibration', 'remap', 'controller', 'display']),
  device: { console: 'gos', model: '83L3', firmware: false, power_led: false } };

run(async () => {
  {
    const reads = [];
    const h = mount('index.tsx', { component: 'C', append: 'export { Controls as C, ModulesPage as P };', ui: { staticClasses: {} },
      api: { useQuickAccessVisible: () => true, callable: name => async () => { reads.push(name); return {}; } },
      modules: name => name.startsWith('./') ? new Proxy({}, { get: (_, key) => key.startsWith('get') ? async () => { reads.push(key); return {}; }
        : /^(start|stop)/.test(key) ? () => {} : key === 'rgbSummary' ? (_status, power) => power ? 'with power light' : 'rings only'
        : key.endsWith('Summary') ? () => '' : key }) : undefined });
    const links = tree => nodes(tree).filter(n => n.props?.onClick && typeof n.props?.title === 'string').map(n => n.props.title);
    const go2 = h.render(GO2);
    assert.deepEqual(links(go2), [...NAMES.map(n => TITLES[n]), 'Manage Modules', 'About'], 'a Go 2 83N0 keeps every module');
    assert.equal(find(go2, n => n.props?.title === 'Lighting').props.description, 'with power light');
    await settle(); reads.length = 0;
    const goS = h.render(GO_S); await settle();
    assert.deepEqual(links(goS), ['TDP', 'Lighting', 'Battery', 'Wi-Fi', 'Manage Modules', 'About'],
      'modules that do not apply and the controller firmware link are hidden');
    assert.equal(find(goS, n => n.props?.title === 'Lighting').props.description, 'rings only');
    assert.ok(!reads.some(name => /Fan|Vibe|Remap|Display|ControllerStatus|ControllerBattery|Driver/.test(name)),
      `a hidden module is never read: ${reads}`);

    // About offers the device report on every console, also an unknown one where no module applies.
    for (const props of [GO2, GO_S, { modules: status(NAMES), device: {} }]) {
      find(h.render(props), n => n.props?.title === 'About' && n.props?.onClick).props.onClick();
      const about = h.render(props);
      assert.ok(find(about, n => n.type === 'DeviceReportSection'), `device report on ${props.device.console ?? 'an unknown console'}`);
      find(about, n => n.props?.onBack).props.onBack();
    }

    // The Beta row (title and text from modules.mjs) opens the home page of a Go 1 and a Go S, never a Go 2.
    const notice = props => find(h.render(props), n => /^Beta on /.test(n.props?.label ?? ''));
    const BETA = "Built from Lenovo's firmware data, not yet tested on this console. Report problems on GitHub.";
    const OLD_FIRMWARE = 'Update the controller firmware in Legion Space to unlock vibration, lighting and gyro.';
    const go1 = driver => ({ modules: status(driver ? [] : ['vibration', 'rgb', 'controller']),
      device: { console: 'go1', model: '83E1', firmware: false, power_led: false, controller_driver: driver } });
    assert.equal(notice(GO2), undefined, 'a Go 2 shows no Beta row');
    assert.equal(notice({ modules: status([]), device: { console: 'go2', model: '83N1', controller_driver: false } }), undefined);
    assert.equal(notice({ modules: status([]), device: {} }), undefined, 'an unknown console shows none');
    assert.equal(notice(GO_S).props.label, 'Beta on Legion Go S');
    assert.equal(notice(GO_S).props.description, BETA);
    assert.equal(notice(go1(true)).props.label, 'Beta on Legion Go');
    assert.equal(notice(go1(true)).props.description, BETA, 'newer controller firmware: no firmware hint');
    assert.equal(notice(go1(false)).props.description, `${BETA} ${OLD_FIRMWARE}`, 'older controller firmware adds the hint');
    assert.equal(notice({ modules: GO_S.modules, device: { ...GO_S.device, controller_driver: false } }).props.description, BETA,
      'the firmware hint is for the Go 1 only');
    const page = props => h.render(props, h.exports.P);
    const rows = props => nodes(page(props)).filter(n => n.type === 'ToggleField').map(n => n.props.label);
    assert.deepEqual(rows(GO2), NAMES.map(n => TITLES[n]));
    assert.equal(label(page(GO2), 'Lighting').props.description, 'Stick lights and power button light.');
    assert.deepEqual(rows(GO_S), ['TDP', 'Lighting', 'Battery', 'Wi-Fi'], 'Manage Modules lists only what applies');
    assert.equal(label(page(GO_S), 'Lighting').props.description, 'Stick lights.');
  }
  {
    const rgb = { success: true, settings: { configured: true, control_enabled: true, rings_enabled: true, effect: 'monocolor' },
      rgb: { supported: true, effects: [] }, power_led: { supported: false } };
    for (const powerLight of [true, false]) {
      const h = mount('rgb.tsx', { component: 'RgbPage', responses: { rgb_get_status: () => rgb } });
      h.render({ powerLight }); await settle(); const tree = h.render({ powerLight });
      assert.equal(!!label(tree, 'Power button light'), powerLight, 'the power button light only where it applies');
      assert.ok(button(tree, 'Restore original lighting'), 'restoring the lights stays');
      h.unmount();
    }
  }
  {
    const h = mount('battery.tsx', { component: 'BatteryPage', responses: {
      battery_get_status: () => ({ success: true, supported: true, managed: false, enabled: false, capacity: 80, options: ['Standard', 'Long_Life'] }),
      battery_get_controller_levels: () => ({ available: true, left: 50, right: 60 }) } });
    h.render({ controllers: false }); await settle(); const tree = h.render({ controllers: false });
    assert.doesNotMatch(text(tree), /Controllers/, 'integrated controllers report no separate batteries');
    assert.ok(!h.calls.some(call => call.name === 'battery_get_controller_levels'));
    h.unmount();
  }
  {
    for (const firmware of [true, false]) {
      const h = mount('controller.tsx', { component: 'ControllerPage', modules: name => name === './firmware' ? { FirmwarePanel: 'FirmwarePanel' } : undefined });
      const tree = h.render({ visible: false, firmware });
      assert.equal(!!find(tree, n => n.type === 'FirmwarePanel'), firmware, 'controller firmware only where it applies');
      h.unmount();
    }
  }
  const M = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/modules.mjs')).href);
  assert.equal(M.moduleShown({ applicable: false }), false);
  assert.equal(M.moduleShown({ enabled: false }), true, 'turned off is not the same as not applicable');
  assert.equal(M.moduleShown(undefined), true, 'an older backend without the field is shown');
  assert.equal(M.moduleShown({ applicable: null }), false, 'not detected yet: not shown');
  assert.equal(M.consoleDetecting({ tdp: { applicable: null }, fan: { applicable: null } }), true);
  assert.equal(M.consoleDetecting({ tdp: { applicable: true }, fan: { applicable: false } }), false);
  assert.equal(M.consoleDetecting({ tdp: { enabled: true } }), false, 'no field: an older backend');
  assert.equal(M.consoleDetecting(undefined), false);
  console.log('Console visibility verified.');
});
