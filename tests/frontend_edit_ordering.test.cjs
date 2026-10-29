// Edits accepted by one page instance are ordered against reads and mutations of the next.
const { assert, run, settle, find, harness, rgb, remap, display } = require('./legion_page_harness.cjs');
run(async () => {
  const leasePanel=harness('tdp_state.tsx','useTdpInfo');
  leasePanel.render();
  leasePanel.reject('set_panel_active');leasePanel.respond('get_tdp_info',{success:true});await settle();
  leasePanel.fire();leasePanel.reject('set_panel_active');await settle();
  leasePanel.unmount();leasePanel.reject('set_panel_active');await settle();
  assert.equal(leasePanel.calls.filter(call=>call.name==='set_panel_active').length,3,
    'mount, renewal and cleanup tolerate a lost Decky connection');
  {
    const h=harness('battery.tsx','BatteryPage'),initial={success:true,supported:true,enabled:false,managed:false},
      final={success:true,supported:true,enabled:true,managed:true};
    h.render();h.respond('battery_get_status',initial);await settle();
    find(h.render(),'Battery protection').props.onChange(true);await settle();h.unmount();
    const overview=h.moduleExports.getBatteryStatus();h.respond('battery_set_enabled',{success:true});await settle();
    assert.equal(h.calls.filter(c=>c.name==='battery_get_status').length,2,'A result without a snapshot still starts its verification read inside the transaction.');
    h.respond('battery_get_status',final);await settle();
    assert.equal(h.calls.filter(c=>c.name==='battery_get_status').length,3,'The public getter waits for verification without making the transaction wait for itself.');
    h.respond('battery_get_status',final);assert.equal(await overview,final);
  }

  for (const spec of [
    {file:'battery.tsx',component:'BatteryPage',read:'battery_get_status',getter:'getBatteryStatus',props:{},
      initial:{success:true,supported:true,enabled:false,managed:false},
      final:{success:true,supported:true,enabled:true,managed:true},method:'battery_set_enabled',label:'Battery protection',selection:true},
    {file:'controller.tsx',component:'ControllerPage',read:'controller_get_status',getter:'getControllerStatus',props:{visible:true},
      initial:{available:true,gyro_source:'system',controlled:false},final:{available:true,gyro_source:'left',controlled:true},
      method:'controller_set_gyro_source',label:'Gyro source',selection:{data:'left'}},
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',getter:'getRemapStatus',props:{},initial:remap,
      final:{...remap,desktop_action:'f3'},method:'remap_set_action',label:'Desktop button',selection:{data:'f3'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',getter:'getRgbStatus',props:{},initial:rgb,
      final:{...rgb,settings:{...rgb.settings,effect:'breathe'}},method:'rgb_set_effect',label:'Effect',selection:{data:'breathe'}},
  ]) {
    const h=harness(spec.file,spec.component);h.render(spec.props);h.respond(spec.read,spec.initial);await settle();
    find(h.render(),spec.label).props.onChange(spec.selection);await settle();h.unmount();
    const overview=h.moduleExports[spec.getter]();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'The main-menu getter must wait for edits accepted by a closed page.');
    h.respond(spec.method,spec.component==='ControllerPage'?spec.final:{success:true,status:spec.final});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2);
    h.respond(spec.read,spec.final);assert.equal(await overview,spec.final);
  }

  for (const failed of [false, true]) for (const spec of [
    {file:'battery.tsx',component:'BatteryPage',read:'battery_get_status',props:{},
      initial:{success:true,supported:true,enabled:false,managed:false},
      final:{success:true,supported:true,enabled:true,managed:true,requested_enabled:true},
      method:'battery_set_enabled',label:'Battery protection',selection:true,key:'checked',expected:true},
    {file:'controller.tsx',component:'ControllerPage',read:'controller_get_status',props:{visible:true},
      initial:{available:true,gyro_source:'system',controlled:false},
      final:{available:true,gyro_source:'left',applied_source:'left',controlled:true},
      method:'controller_set_gyro_source',label:'Gyro source',selection:{data:'left'},key:'selectedOption',expected:'left'},
  ]) {
    const h=harness(spec.file,spec.component);h.render(spec.props);h.respond(spec.read,spec.initial);
    await settle();h.fire();await settle();
    find(h.render(),spec.label).props.onChange(spec.selection);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,0,'An accepted change waits for its earlier status read.');
    h.unmount();h.remount();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A reopened page cannot overtake an accepted change still waiting for its read.');
    h.respond(spec.read,spec.initial);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,1);
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'The new status read also waits for the actual mutation.');
    if(failed) {
      if(spec.component==='ControllerPage')h.render({visible:false});else h.visible(false);
      h.reject(spec.method);
    }
    else h.respond(spec.method,spec.component==='BatteryPage'?{success:true,status:spec.final}:spec.final);
    await settle();
    if(failed) {
      assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A hidden reopened page must not read after the old mutation ends.');
      if(spec.component==='ControllerPage')h.render({visible:true});else h.visible(true);
      await settle();
    }
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,3,'Completion or rejection immediately releases the reopened status read.');
    h.respond(spec.read,failed?spec.initial:spec.final);await settle();
    assert.equal(find(h.render(),spec.label).props[spec.key],failed?(spec.key==='checked'?false:'system'):spec.expected);
    h.unmount();
  }

  // A reopened page must observe all edits accepted by its previous instance.
  // The old instance may still have an RPC running and another one queued.
  for (const spec of [
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',initial:remap,
      method:'remap_set_action',label:'Desktop button',values:[{data:'f3'},{data:'f1'},{data:'f3'}],
      first:{...remap,desktop_action:'f3'},final:{...remap,desktop_action:'f1'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',initial:rgb,
      method:'rgb_set_effect',label:'Effect',values:[{data:'breathe'},{data:'rainbow'},{data:'monocolor'}],
      first:{...rgb,settings:{...rgb.settings,effect:'breathe'}},
      final:{...rgb,settings:{...rgb.settings,effect:'rainbow'}}},
  ]) {
    const h=harness(spec.file,spec.component);h.render();h.respond(spec.read,spec.initial);await settle();
    const select=find(h.render(),spec.label).props.onChange;
    select(spec.values[0]);select(spec.values[1]);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,1);
    h.unmount();h.remount();await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'Reopening waits for the accepted mutation tail before reading status.');
    h.respond(spec.method,{success:true,status:spec.first});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'The second queued edit still precedes the new snapshot.');
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,2);
    h.respond(spec.method,{success:true,status:spec.final});await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2);
    h.respond(spec.read,spec.final);await settle();
    assert.equal(find(h.render(),spec.label).props.selectedOption,
      spec.file==='rgb.tsx'?'rainbow':'f1','The reopened page displays the last accepted edit.');
    find(h.render(),spec.label).props.onChange(spec.values[2]);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.method).length,3);
    h.respond(spec.method,{success:true,status:spec.initial});await settle();h.unmount();
  }
  for (const spec of [
    {file:'remap.tsx',component:'RemapPage',read:'remap_get_status',initial:remap,method:'remap_set_action',label:'Desktop button',value:{data:'f3'}},
    {file:'rgb.tsx',component:'RgbPage',read:'rgb_get_status',initial:rgb,method:'rgb_set_effect',label:'Effect',value:{data:'breathe'}},
  ]) {
    const h=harness(spec.file,spec.component);h.render();h.respond(spec.read,spec.initial);await settle();
    find(h.render(),spec.label).props.onChange(spec.value);await settle();h.unmount();h.remount();await settle();
    h.visible(false);h.reject(spec.method);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,1,'A hidden page does not start a read when the older failed mutation finishes.');
    h.visible(true);await settle();
    assert.equal(h.calls.filter(c=>c.name===spec.read).length,2,'A failed mutation cannot leave the new page waiting forever.');
    h.respond(spec.read,spec.initial);await settle();assert.ok(find(h.render(),spec.label));h.unmount();
  }
  console.log('Edit ordering: accepted edits stay ordered across reopened pages, reads and mutations.');
});
