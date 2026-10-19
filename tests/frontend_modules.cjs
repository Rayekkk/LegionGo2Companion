const { assert, run, settle, find, findButton, harness, rgb, display } = require('./frontend_page_harness.cjs');
run(async () => {
  const moduleState={display:{enabled:false,note:'Restart required'},rgb:{enabled:true,ready:true}};
  {
    const h=harness('index.tsx','ModulesPage');const tree=h.render({modules:moduleState});
    const toggle=find(tree,'Lighting').props.onChange;
    findButton(tree,'Restart Gaming Mode').props.onClick();
    assert.equal(h.calls.length,0,'Restarting Gaming Mode waits for its confirmation.');
    const confirmed=h.modals.at(-1).props;
    assert.deepEqual([confirmed.strTitle,confirmed.strDescription,confirmed.strOKButtonText,confirmed.strCancelButtonText],
      ['Restart Gaming Mode?','Steam and any running game will close and reopen. Save your game first.','Restart','Cancel']);
    h.unmount();const writes=h.writes;toggle(false);confirmed.onOK();await settle();
    assert.equal(h.calls.length,0,'Discarded module controls cannot start restoration or a session restart.');
    assert.equal(h.writes,writes);
  }
  {
    const h=harness('index.tsx','ModulesPage');let tree=h.render({modules:moduleState});
    const restart=()=>{findButton(tree,'Restart Gaming Mode').props.onClick();h.confirm();};
    restart();restart();find(tree,'Lighting').props.onChange(false);await settle();
    assert.equal(h.calls.length,1,'A session restart coalesces repeated clicks and excludes module changes.');
    assert.equal(h.calls[0].name,'modules_restart_session');
    assert.ok(findButton(h.render(),'Restarting…'),'the last row stays enabled while it runs');
    h.respond('modules_restart_session',{success:false,error:'session unavailable'});await settle();tree=h.render();
    assert.match(findButton(tree,'Restart Gaming Mode').props.description,/session unavailable/);
    restart();await settle();assert.equal(h.calls.length,2,'A failed restart can be retried.');
    h.unmount();const writes=h.writes;h.respond('modules_restart_session',{success:false,error:'late error'});await settle();
    assert.equal(h.writes,writes,'Restart completion cannot update a discarded page.');
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
  assert.equal(M.cleanupRetryShown({ enabled: false, pending: true }), true);
  console.log('Modules page verified.');
});
