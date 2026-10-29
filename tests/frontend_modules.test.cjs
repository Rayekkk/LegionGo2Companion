const { assert, run, settle, find, findButton, harness, rgb, display } = require('./legion_page_harness.cjs');
run(async () => {
  {
    const h=harness('index.tsx','ModulesPage');const tree=h.render({modules:{display:{enabled:false},rgb:{enabled:true,ready:true}}});
    const toggle=find(tree,'Lighting').props.onChange;
    assert.equal(findButton(tree,'Restart Gaming Mode'),undefined,'turning Display off needs no Gaming Mode restart');
    h.unmount();const writes=h.writes;toggle(false);await settle();
    assert.equal(h.calls.length,0,'Discarded module controls cannot start restoration.');
    assert.equal(h.writes,writes);
  }
  {
    const h = harness('index.tsx', 'ModulesPage');
    const modules = { rgb: { enabled: true, ready: false, error: 'Startup failed', pending: false } };
    const view = h.render({ modules });
    const retry = findButton(view, 'Retry Lighting start');
    assert.ok(retry, 'failed startup offers a distinct retry without cleanup first');
    const lighting = find(view, 'Lighting');
    assert.equal(lighting.props.checked, true, 'the switch shows the saved choice, so a failed module can be turned off');
    assert.equal(lighting.props.description, 'Startup failed', 'failed readiness is visible');
    retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === 'modules_set_enabled').length, 1);
    h.respond('modules_set_enabled', { rgb: { enabled: true, ready: true } }); await settle();
    assert.equal(findButton(h.render({ modules: { rgb: { enabled: true, ready: true } } }),
      'Retry Lighting start'), undefined);
    h.unmount();
  }
  {
    const h = harness('index.tsx', 'ModulesPage');
    const view = h.render({ modules: { rgb: { enabled: true, ready: false, error: 'Startup failed', pending: false } } });
    find(view, 'Lighting').props.onChange(false); await settle();
    assert.deepEqual(h.calls.find(c => c.name === 'modules_set_enabled')?.args, ['rgb', false], 'a module that failed to start can be turned off');
    h.unmount();
  }
  {
    const h = harness('index.tsx', 'ModulesPage');
    const starting = { rgb: { enabled: true, ready: false, starting: true, error: '' } };
    const lighting = find(h.render({ modules: starting }), 'Lighting');
    assert.equal(lighting.props.description, 'Still starting. Its controls appear when it has finished.');
    assert.equal(lighting.props.checked, true);
    assert.equal(lighting.props.disabled, false, 'a module can be turned off while it is still starting');
    assert.equal(findButton(h.render(), 'Retry Lighting start'), undefined);
    lighting.props.onChange(false); await settle();
    assert.deepEqual(h.calls.find(c => c.name === 'modules_set_enabled')?.args, ['rgb', false]);
    const after = { rgb: { enabled: false, ready: false, starting: true, error: '' } };
    h.respond('modules_set_enabled', after); await settle();
    const off = find(h.render({ modules: after }), 'Lighting');
    assert.equal(off.props.checked, false);
    assert.equal(off.props.description, 'Turns off when it has finished starting.');
    h.unmount();
  }
  // The rules both UIs share (src/index.tsx and the Desktop ModulePage.qml import the same module).
  const M = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/modules.mjs')).href);
  assert.equal(M.moduleChecked({ enabled: true, ready: false }), true);
  assert.equal(M.moduleChecked({ enabled: false, pending: true }), false);
  assert.equal(M.moduleLocked({ enabled: false, pending: true }), true);
  assert.equal(M.moduleLocked({ enabled: true, ready: false, maintenance: true }), true);
  assert.equal(M.moduleNote('battery', { enabled: false }), 'Charge limit of about 80%.', 'an Off module still says what it does');
  assert.equal(M.moduleNote('battery', { enabled: false, pending: true, error: 'Restore failed' }), 'Restore failed');
  assert.equal(M.startRetryShown({ enabled: true, ready: false, maintenance: true }), false);
  assert.equal(M.startRetryShown({ enabled: true, ready: false, starting: true }), false, 'no retry while a slow start still runs');
  assert.equal(M.moduleStartingText({ enabled: false, starting: true, pending: true }), 'Turns off when it has finished starting.', 'off during a start reads as such although withdrawal is pending');
  assert.equal(M.cleanupRetryShown({ enabled: false, starting: true, pending: true }), false, 'no cleanup retry before the start has finished');
  assert.equal(M.moduleLocked({ enabled: true, ready: false, starting: true }), false, 'Off is accepted during a start');
  assert.equal(M.moduleStartingText({ enabled: true, ready: false, starting: true, maintenance: true }), '', 'maintenance reads first');
  assert.equal(M.moduleStartingText({ enabled: true, ready: false, error: 'Startup failed' }), '');
  assert.equal(M.cleanupRetryShown({ enabled: false, pending: true }), true);
  console.log('Modules page verified.');
});
