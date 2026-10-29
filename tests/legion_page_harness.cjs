// Shared page harness for the per-page frontend tests: real src/*.tsx pages with deferred RPCs, a fake clock and QAM visibility.
const { assert, run, settle: wait, mount, label: find, button: findButton } = require('./legion_harness.cjs');
const settle = () => wait(16);
const batterySummary = mount('battery.tsx').exports.batterySummary;
function harness(file, component, initiallyVisible = true) {
  let panelShown = true, intersectionCallback;
  const modals = [], toasts = [];
  class PanelObserver { constructor(fn) { intersectionCallback = fn; } observe() {} disconnect() { intersectionCallback = null; } }
  const panelElement = { getClientRects: () => panelShown ? [{}] : [], ownerDocument: { defaultView: { IntersectionObserver: PanelObserver } } };
  const fan = mount('fan.tsx', { clock: 100000 });
  const relativeReads = { getFanState: 'fan_get_state', getVibrationSettings: 'vibe_get_settings', getWifiStatus: 'wifi_get_status', getRgbStatus: 'rgb_get_status',
    getRemapStatus: 'remap_get_status', getBatteryStatus: 'battery_get_status',
    getControllerBatteryLevels: 'battery_get_controller_levels', getControllerStatus: 'controller_get_status' };
  const h = mount(file, { component: 'Tested', append: `export { ${component} as Tested };`, deferAll: true, clock: 100000,
    visible: initiallyVisible,
    window: { SteamClient: { GameSessions: { RegisterForAppLifetimeNotifications(fn) { h.listeners.lifetime = fn; return { unregister() {} }; } } },
      SleepManager: { RegisterForNotifyResumeFromSuspend(fn) { h.listeners.resume = fn; return () => {}; } } },
    ui: { Router: { MainRunningApp: { appid: 111, display_name: 'Game' } }, findModuleExport() {}, showModal: node => { modals.push(node); } },
    api: { removeEventListener() {}, toaster: { toast: item => { toasts.push(item); } } },
    modules: name => name.startsWith('./') ? new Proxy({}, { get: (_object, key) => {
      if (key in relativeReads) return (...args) => h.callable(relativeReads[key])(...args);
      if (key === 'batterySummary') return batterySummary;
      if (key === 'fanSummary') return fan.exports.fanSummary;
      if (/^(start|stop)/.test(key)) return () => {};
      if (key.endsWith('Summary')) return () => '';
      return key;
    } }) : undefined,
    onRender: result => { if (result?.props?.rootRef) result.props.rootRef.current = panelElement; },
  });
  return { calls: h.calls, timers: h.timers, callbacks: h.listeners, modals, toasts,
    // Press OK on the newest confirmation; returns its props for text checks.
    confirm() { const modal = modals.at(-1); assert.ok(modal, 'a confirmation is shown'); modal.props.onOK(); return modal.props; }, render: nextProps => h.render(nextProps), exported: h.exports.Tested, moduleExports: h.exports,
    initialize: () => h.exports.default(),
    get writes() { return h.writes; },
    advance(milliseconds) { h.advance(milliseconds); fan.advance(milliseconds); },
    visible: h.setVisible,
    panelVisible(value) { panelShown = value; intersectionCallback?.([{ isIntersecting: value }]); return h.render(); },
    fire: () => h.fire('interval'), respond: h.respond, reject: name => h.reject(name),
    unmount: h.unmount, remount: () => h.remount(),
  };
}
function findSection(node, title, action = 'onOpen') {
  if (!node || typeof node !== 'object') return;
  if (node.props?.title === title && node.props?.[action]) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const found = findSection(child, title, action); if (found) return found;
  }
}
const rgb = { settings: { configured: true, control_enabled: true, rings_enabled: true, effect: 'monocolor',
  hue: 255, saturation: 100, brightness: 50, speed: 50 }, rgb: { supported: true, effects: [] }, power_led: { supported: false } };
const remap = { supported: true, enabled: true, desktop_action: 'f1', page_action: 'f2', actions: [{ id: 'f1', label: 'F1' }, { id: 'f3', label: 'F3' }] };
const display = { edid_fix: true, edid_patched: true, edid_game_nits: 1107.4, edid_reason: 'applied, games read the real values' };
const wifi = { connected: true, settings: { device_family: 'legion_go_2', driver: 'mt7921e', band_policy: 'five_six_no_24' }, live: {wifi_backend:'iwd', connection_state:'connected', connection_uuid:'fixture-profile', ssid:'Target', frequency:'5260'} };
const cases = [
  ['display.tsx', 'DisplayPage', 'display_get_state', display],
  ['rgb.tsx', 'RgbPage', 'rgb_get_status', rgb],
  ['remap.tsx', 'RemapPage', 'remap_get_status', remap],
  ['wifi.tsx', 'WifiPage', 'wifi_get_status', wifi],
];
module.exports = { assert, run, settle, find, findButton, harness, findSection, rgb, remap, display, wifi, cases };
