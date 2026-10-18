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
    assert.equal(find(view, 'Lighting').props.checked, false, 'failed readiness is visible');
    retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === 'modules_set_enabled').length, 1);
    h.respond('modules_set_enabled', { rgb: { enabled: true, ready: true } }); await settle();
    assert.equal(findButton(h.render({ modules: { rgb: { enabled: true, ready: true } } }),
      'Retry Lighting start'), undefined);
    h.unmount();
  }
  console.log('Modules page verified.');
});
