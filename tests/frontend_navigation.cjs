const { assert, run, mount, find } = require('./frontend_harness.cjs');
const rpcCalls = [];
let visible = true, modules = {}, refNode = null;
const h = mount('index.tsx', {
  component: 'TestedContent', ui: { staticClasses: {} },
  append: 'export { Controls as TestedContent, SectionLink as TestedLink, SectionHeader as TestedHeader };',
  api: { useQuickAccessVisible: () => visible,
    callable: name => async () => { rpcCalls.push(name); return { version: 'test', blocked: false }; } },
  modules: name => name.startsWith('./') ? new Proxy({}, { get: (_, k) =>
    k.startsWith('get') ? async () => { rpcCalls.push(k); return {}; } : /^(start|stop)/.test(k) ? () => {} : k.endsWith('Summary') ? () => '' : k }) : undefined,
  onRender: tree => { if (refNode) find(tree, n => n.props?.ref).props.ref.current = refNode; },
});
const timers = { get size() { return h.intervals().length; } };
let plugin;
function render() {
  // Decky 3.2.8 PluginView unmounts content on hide unless alwaysRender is set.
  // Merely changing useQuickAccessVisible in a mounted component misses this.
  if (!visible && !plugin.alwaysRender) { h.reset(); return null; }
  let tree, passes = 0;
  do {
    assert.ok(++passes < 10, 'render loop'); h.changed = false;
    tree = h.render({ modules });
  } while (h.changed);
  return tree;
}
run(async () => {
plugin = h.exports.default();
// Controls consume the compatibility snapshot already read by the parent.
for (let i = 0; i < 16; i++) await Promise.resolve();
for (const title of ['TDP', 'Fan Control', 'Vibration', 'Lighting', 'Button mapping', 'Gyro & touchpad', 'Battery', 'Display', 'Wi-Fi', 'About', 'Manage Modules']) {
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
  const returned = find(render(), n => n.props?.title === title && n.props?.onClick);
  assert.ok(returned, 'explicit back works');
  assert.equal(returned.props.restoreFocus, true, 'back restores the selected native module row');
  assert.ok(timers.size > 0, 'returning to the overview resumes polling');
}
find(render(), n => n.props?.title === 'Battery' && n.props?.onClick).props.onClick();
// Complete the earlier overview read before testing the next visibility cycle.
for (let i = 0; i < 12; i++) await Promise.resolve();
modules = Object.fromEntries(['tdp','fan','vibration','rgb','remap','controller','battery','display','wifi'].map(k=>[k,{enabled:false}]));
rpcCalls.length = 0;
const disabled = render();
assert.deepEqual(rpcCalls, [], 'disabled modules are not polled and Controls does not duplicate the guard read');
assert.ok(!find(disabled, n => n.props?.onBack), 'disabling an open module returns to the overview');
for (const title of ['TDP','Fan Control','Vibration','Lighting','Button mapping','Gyro & touchpad','Battery','Display','Wi-Fi']) {
  assert.ok(!find(disabled, n => n.props?.title === title && n.props?.onClick), `${title} hidden when disabled`);
}
for (const title of ['About','Manage Modules']) assert.ok(find(disabled, n => n.props?.title === title && n.props?.onClick));
assert.equal(find(disabled, n => n.props?.title === 'Manage Modules' && n.props?.onClick).props.restoreFocus,
  true, 'a module withdrawn while open restores focus to module management');
const firmwareLink=find(disabled,n=>n.props?.title==='Controller firmware' && n.props?.onClick);
assert.ok(firmwareLink,'firmware remains accessible when the gyro module is disabled');
firmwareLink.props.onClick();
let firmwareOnly=render();
assert.ok(!find(firmwareOnly,n=>n.type==='ControllerPage'),'disabled gyro does not mount hardware polling');
assert.equal(find(firmwareOnly,n=>n.type==='FirmwarePanel').props.allowUpdates,true);
modules=Object.assign({},modules,{controller:{enabled:true,ready:true}});
assert.ok(find(render(),n=>n.type==='ControllerPage'));
modules=Object.assign({},modules,{controller:{enabled:true,ready:false,maintenance:true}});
firmwareOnly=render();
assert.ok(find(firmwareOnly,n=>n.props?.title==='Controller firmware' && n.props?.onBack),
  'firmware maintenance preserves the open progress route');
assert.ok(!find(firmwareOnly,n=>n.type==='ControllerPage'),'maintenance retires gyro polling');
assert.equal(find(firmwareOnly,n=>n.type==='FirmwarePanel').props.allowUpdates,false,
  'maintenance only admits read-only firmware status polling');
find(firmwareOnly,n=>n.props?.onBack).props.onBack();
const maintenanceLink=find(render(),n=>n.props?.title==='Controller firmware' && n.props?.onClick);
assert.ok(maintenanceLink,'an uncertain maintenance outcome can be revisited');
maintenanceLink.props.onClick();
assert.equal(find(render(),n=>n.type==='FirmwarePanel').props.allowUpdates,false);
find(render(),n=>n.props?.onBack).props.onBack();
visible = false; render(); assert.equal(timers.size, 0, 'hidden overview stops polling');
// Exercise native ref focus handoffs separately from the Controls hook instance.
h.reset();
visible = true;
let focusCalls = 0, activations = 0;
const nativeRender = (component, props, node) => { refNode = node; const tree = h.render(props, component); refNode = null; return tree; };
const linkProps = {section:'battery',title:'Battery',description:'Protection off · L 87% · R 64%',
  restoreFocus:true,onClick:()=>activations++};
const nativeRow = nativeRender(h.exports.TestedLink, linkProps, {focus:()=>focusCalls++});
assert.equal(focusCalls, 1);
const nativeField = find(nativeRow, n => n.type === 'Field');
nativeField.props.onActivate(); assert.equal(activations, 1, 'native activation keeps the module action');
nativeRender(h.exports.TestedLink, {...linkProps,description:'Protection off · L 42% · R 37%'}, {focus:()=>focusCalls++});
visible = false; nativeRender(h.exports.TestedLink,linkProps,{focus:()=>focusCalls++});
visible = true; nativeRender(h.exports.TestedLink,linkProps,{focus:()=>focusCalls++});
assert.equal(focusCalls, 1, 'polls and dropdown visibility do not steal native focus');
h.reset();
const backHost = {querySelector:()=>({focus:()=>focusCalls++})};
nativeRender(h.exports.TestedHeader,{title:'Battery',onBack(){}},backHost);
assert.equal(focusCalls, 2, 'page entry focuses its native Back control');
nativeRender(h.exports.TestedHeader,{title:'Battery',onBack(){}},backHost);
visible = false; nativeRender(h.exports.TestedHeader,{title:'Battery',onBack(){}},backHost);
visible = true; nativeRender(h.exports.TestedHeader,{title:'Battery',onBack(){}},backHost);
assert.equal(focusCalls, 2, 'reopening QAM does not overwrite a page or dropdown selection');
// Decky tab hidden (IntersectionObserver reports off-screen) while Quick Access stays open:
// every polling sub-page must be told it is not visible.
{
  let report; class Observer { constructor(fn) { report = fn; } observe() {} disconnect() {} }
  let shown = true;
  const panel = { getClientRects: () => shown ? [{}] : [], ownerDocument: { defaultView: { IntersectionObserver: Observer } } };
  const tab = mount('index.tsx', { component: 'C', append: 'export { Controls as C };', ui: { staticClasses: {} }, deferAll: true,
    modules: name => name.startsWith('./') ? new Proxy({}, { get: (_, k) => /^(start|stop)/.test(k) ? () => {} : k.endsWith('Summary') ? () => '' : k.startsWith('get') ? () => new Promise(() => {}) : k }) : undefined,
    onRender: t => { if (t?.props?.rootRef) t.props.rootRef.current = panel; } });
  tab.exports.default();
  const open = (state, title, type) => {
    const states = { modules: state };
    shown = true; tab.reset(); report?.([{ isIntersecting: true }]);
    find(tab.render(states), n => n.props?.title === title && n.props?.onClick).props.onClick();
    const visibleWhen = onScreen => { shown = onScreen; report([{ isIntersecting: onScreen }]); return find(tab.render(states), n => n.type === type).props.visible; };
    return [visibleWhen(true), visibleWhen(false)];
  };
  const on = { controller: { enabled: true }, rgb: { enabled: true }, remap: { enabled: true }, tdp: { enabled: true }, display: { enabled: true }, wifi: { enabled: true }, battery: { enabled: true } };
  assert.deepEqual(open(on, 'Gyro & touchpad', 'ControllerPage'), [true, false], 'gyro page stops polling when its tab is hidden');
  assert.deepEqual(open({ controller: { enabled: false } }, 'Controller firmware', 'FirmwarePanel'), [true, false], 'firmware panel stops polling when its tab is hidden');
  assert.deepEqual(open(on, 'Lighting', 'RgbPage'), [true, false], 'RGB page stops polling when its tab is hidden');
  assert.deepEqual(open(on, 'Button mapping', 'RemapPage'), [true, false], 'remap page stops polling when its tab is hidden');
  for (const [title, type] of [['TDP', 'TdpPage'], ['Display', 'DisplayPage'], ['Wi-Fi', 'WifiPage'], ['Battery', 'BatteryPage']])
    assert.deepEqual(open(on, title, type), [true, false], `${type} stops polling when its tab is hidden`);
}
console.log('Ten sections retain navigation; disabled modules are hidden; explicit Back restores native focus.');
});
