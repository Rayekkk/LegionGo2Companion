const { assert, run, settle, find, findButton, harness, rgb, remap, display, cases } = require('./frontend_page_harness.cjs');
run(async () => {
  for (const [file,component,read,value] of [...cases,
    ['battery.tsx','BatteryPage','battery_get_status',{success:true,supported:false}],
    ['controller.tsx','ControllerPage','controller_get_status',{supported:false}],
  ]) {
    // Controller visibility is a prop; the other pages follow QAM visibility.
    const controller=file==='controller.tsx',h=harness(file,component,controller);
    const show=value=>controller?h.render({visible:value}):h.visible(value);
    h.render(controller?{visible:false}:{});assert.equal(h.calls.length,0,`${component} does no work while hidden`);
    show(true);h.fire();h.fire();
    assert.equal(h.calls.filter(c=>c.name===read).length,1,`${component} has at most one unresolved read`);
    for(let i=0;i<3;i++){show(false);show(true);}
    assert.equal(h.calls.filter(c=>c.name===read).length,1,`${component}: rapid reopening never overlaps reads.`);
    const before=h.writes;h.respond(read,value);
    if(file==='battery.tsx')h.respond('battery_get_controller_levels',{available:false});
    await settle();assert.equal(h.writes,before,'A reply from before hiding stays discarded.');
    assert.equal(h.calls.filter(c=>c.name===read).length,2,
      `${component} immediately starts one fresh read after reopening instead of waiting for polling.`);
    h.unmount();const after=h.writes;h.respond(read,value);
    if(file==='battery.tsx')h.respond('battery_get_controller_levels',{available:false});
    await settle();assert.equal(h.writes,after,`${component} ignores unmounted reads`);
  }
  for (const [file, component, read, value] of cases) {
    const h = harness(file, component);
    h.render(); h.reject(read); await settle();
    let view = h.render();
    assert.match(JSON.stringify(view), /status unavailable/, `${component} shows the first read failure`);
    const retry = findButton(view, 'Retry status');
    assert.ok(retry, `${component} allows an explicit retry`);
    retry.props.onClick(); retry.props.onClick(); await settle();
    assert.equal(h.calls.filter(c => c.name === read).length, 2, 'retry remains bounded');
    h.reject(read); await settle();
    assert.ok(findButton(h.render(), 'Retry status'), 'repeated errors remain actionable');
    h.visible(false); const before = h.calls.length; h.fire();
    assert.equal(h.calls.length, before, 'failed hidden pages do not poll');
    h.visible(true); h.respond(read, value); await settle();
    assert.equal(findButton(h.render(), 'Retry status'), undefined, 'successful recovery clears initial error');
    h.unmount();
  }
  for (const spec of [
    { file: 'display.tsx', component: 'DisplayPage', read: 'display_get_state', initial: display, next: { ...display, enabled: false },
      label: 'Brightness slider in HDR', method: 'display_set_enabled', value: false },
    { file: 'rgb.tsx', component: 'RgbPage', read: 'rgb_get_status', initial: rgb, next: { ...rgb, settings: { ...rgb.settings, control_enabled: false } },
      label: 'Enable lighting control', method: 'rgb_set_control_enabled', value: false },
    { file: 'remap.tsx', component: 'RemapPage', read: 'remap_get_status', initial: remap, next: { ...remap, desktop_action: 'f3' },
      label: 'Desktop button', method: 'remap_set_action', value: { data: 'f3' } },
  ]) {
    const h = harness(spec.file, spec.component); h.render(); h.respond(spec.read, spec.initial); await settle();
    let tree = h.render(); h.fire();
    const control = find(tree, spec.label); assert.ok(control, spec.label); control.props.onChange(spec.value); await settle();
    h.respond(spec.method, spec.file === 'display.tsx' ? spec.next : { success: true, status: spec.next }); await settle();
    tree = h.render(); const saved = find(tree, spec.label).props;
    h.respond(spec.read, spec.initial); await settle(); tree = h.render();
    assert.equal(find(tree, spec.label).props.checked, saved.checked, `${spec.component} keeps the saved toggle`);
    assert.equal(find(tree, spec.label).props.selectedOption, saved.selectedOption, `${spec.component} keeps the saved dropdown`);
    h.unmount();
  }
  for (const [file, component, read, initial] of [...cases, ['battery.tsx', 'BatteryPage', 'battery_get_status', { success: true, supported: false }]]) {
    const tab = harness(file, component); tab.render({ visible: false }); await settle();
    tab.fire(); assert.equal(tab.calls.filter(c => c.name === read).length, 0, `${component} does not read while its tab is hidden`);
    tab.render({ visible: true }); await settle();
    assert.equal(tab.calls.filter(c => c.name === read).length, 1, `${component} reads once its tab is shown`);
    tab.respond(read, initial); await settle();
    if (file === 'battery.tsx') { tab.respond('battery_get_controller_levels', { available: false }); await settle(); }
    const polls = () => [...tab.timers.values()].filter(t => t.kind === 'interval').length;
    assert.ok(polls() > 0, `${component} polls while shown`);
    tab.advance(60000); tab.render({ visible: false }); tab.fire();
    assert.equal(tab.calls.filter(c => c.name === read).length, 1, `${component} stops polling when its tab is hidden under an open QAM`);
    assert.equal(polls(), 0, `${component} clears its poll interval when hidden`);
    tab.render({ visible: true }); assert.ok(polls() > 0);
    tab.unmount(); assert.equal(polls(), 0, `${component} clears its poll interval on unmount`);
  }
  {
    // The Current TDP panel holds a backend lease that drives the RAPL loop: no lease for a hidden Companion tab.
    const panel = harness('tdp.tsx', 'useTdpInfo'), leases = () => panel.calls.filter(c => c.name === 'set_panel_active').map(c => c.args[0]);
    panel.render({ visible: false }); await settle(); panel.fire();
    assert.deepEqual(leases(), [], 'LivePanel takes no lease while its tab is hidden');
    panel.render({ visible: true }); assert.deepEqual(leases(), [true], 'LivePanel leases once its tab is shown');
    panel.render({ visible: false }); assert.deepEqual(leases(), [true, false], 'LivePanel releases the lease when its tab is hidden');
    panel.unmount();
  }
  for (const file of ['tdp.tsx', 'vibration.tsx']) {
    const h = harness(file, 'AppWatcher'), watcher = h.exported;
    watcher.start(); const request = h.calls[0];
    h.callbacks.lifetime(); const late = [...h.timers.values()].find(t => t.kind === 'timeout').fn;
    const resume = h.callbacks.resume; watcher.stop(); assert.equal(h.timers.size, 0);
    late(); resume(); assert.equal(h.calls.length, 1, `${file} callbacks cannot revive a stopped watcher`);
    watcher.start(); let received = 0; watcher.listen(() => received++);
    request.settled = true; request.resolve({ settings: {}, app_id: '111' }); await settle();
    assert.equal(received, 0, `${file} ignores a response from the previous lifecycle`);
    assert.equal(watcher.busy, true, `${file} old request cannot clear the new in-flight guard`);
    h.respond(file === 'tdp.tsx' ? 'set_active_app' : 'vibe_set_active_app', { settings: {}, app_id: '111' }); await settle();
    watcher.stop();
  }
  console.log('Page lifecycle: hidden, retry, reopen and stopped-watcher behaviour verified.');
});
