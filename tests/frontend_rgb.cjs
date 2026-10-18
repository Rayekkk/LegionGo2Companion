const { assert, run, settle, find, findButton, harness, rgb } = require('./frontend_page_harness.cjs');
run(async () => {
  // Immediate actions must follow already accepted slider edits, including
  // restore/disable actions that would otherwise overtake their debounce.
  for (const action of ['disable', 'restore', 'effect']) {
    const h=harness('rgb.tsx','RgbPage');h.render();h.respond('rgb_get_status',rgb);await settle();
    find(h.render(),'Brightness').props.onChange(90);
    if(action==='disable')find(h.render(),'Enable lighting control').props.onChange(false);
    else if(action==='restore')findButton(h.render(),'Restore original lighting').props.onClick();
    else find(h.render(),'Effect').props.onChange({data:'breathe'});
    await settle();
    const writes=h.calls.filter(c=>c.name!=='rgb_get_status');
    assert.equal(writes[0]?.name,'rgb_set_brightness',`The earlier slider save precedes ${action}.`);
    assert.equal(writes.length,1,'The later operation remains queued until the slider finishes.');
    h.respond('rgb_set_brightness',{success:true,status:{...rgb,settings:{...rgb.settings,brightness:90}}});await settle();
    const method=action==='disable'?'rgb_set_control_enabled':action==='restore'?'rgb_restore_original':'rgb_set_effect';
    assert.equal(h.calls.at(-1).name,method);
    h.respond(method,{success:true,status:rgb});await settle();
    for(const [id,t]of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();}
    await settle();assert.equal(h.calls.filter(c=>c.name==='rgb_set_brightness').length,1);
    h.unmount();
  }
  {
    // A brightness edit survives closing the view before its bounded debounce fires.
    const h=harness('rgb.tsx','RgbPage');h.render();h.respond('rgb_get_status',rgb);await settle();
    find(h.render(),'Brightness').props.onChange(73);
    const debounce=[...h.timers.values()].filter(t=>t.kind==='timeout');
    assert.equal(debounce.length,1);
    assert.ok(Number.isFinite(debounce[0].delay)&&debounce[0].delay>0&&debounce[0].delay<=1000,
      'The pending gesture has a bounded debounce, without prescribing its exact tuning.');
    h.unmount();assert.equal([...h.timers.values()].filter(t=>t.kind==='timeout').length,0);await settle();
    assert.deepEqual(h.calls.filter(c=>c.name==='rgb_set_brightness').map(c=>c.args[0]),[73]);
  }
  for(const leave of ['timer','unmount']) {
    const h=harness('rgb.tsx','RgbPage');h.render();
    const breathing={...rgb,settings:{...rgb.settings,effect:'breathe'}};
    h.respond('rgb_get_status',breathing);await settle();
    find(h.render(),'Speed').props.onChange(0);await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_set_speed').length,0,'Speed edits debounce before hardware I/O.');
    assert.equal(find(h.render(),'Speed').props.disabled,false,'A speed gesture stays responsive while its timer is pending.');
    for(const t of h.timers.values())if(t.kind==='interval')t.fn();
    await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_get_status').length,1,'Polling cannot replace an unsaved speed draft.');
    const obsolete=[...h.timers.values()].find(t=>t.kind==='timeout').fn;
    find(h.render(),'Speed').props.onChange(100);obsolete();await settle();
    assert.equal(h.calls.filter(c=>c.name==='rgb_set_speed').length,0,'Superseded speed timers cannot queue a save.');
    if(leave==='unmount')h.unmount();
    else for(const[id,t]of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();t.fn();}
    await settle();
    const writes=h.calls.filter(c=>c.name==='rgb_set_speed');
    assert.equal(writes.length,1,'Only the final speed is saved, once, including on close.');
    assert.equal(writes[0].args[0],100);
    h.respond('rgb_set_speed',{success:true,status:{...breathing,settings:{...breathing.settings,speed:100}}});await settle();
    if(leave==='timer')h.unmount();
  }
  {
    const h=harness('rgb.tsx','RgbPage');h.render();h.respond('rgb_get_status',rgb);await settle();
    const tree=h.render(),gestures=[()=>find(tree,'Hue').props.onChange(120),
      ()=>find(tree,'Brightness').props.onChange(80),()=>find(tree,'Enable lighting control').props.onChange(false)];
    h.unmount();const writes=h.writes,calls=h.calls.length;
    gestures.forEach(run=>run());
    for(const [id,t] of [...h.timers])if(t.kind==='timeout'){h.timers.delete(id);t.fn();}
    await settle();assert.equal(h.writes,writes,'Closed RGB controls cannot create new drafts.');
    assert.equal(h.calls.length,calls,'Closed RGB controls cannot start new writes.');
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    for (const [id,t] of [...h.timers]) if (t.kind==='timeout') { h.timers.delete(id); t.fn(); }
    await settle(); h.reject('rgb_set_color'); await settle();
    find(h.render(), 'Brightness').props.onChange(90);
    for (const [id,t] of [...h.timers]) if (t.kind==='timeout') { h.timers.delete(id); t.fn(); }
    await settle(); h.respond('rgb_set_brightness',{success:true,status:{...rgb,settings:{...rgb.settings,brightness:90}}});
    await settle(); assert.equal(find(h.render(),'Brightness').props.value,90);
    assert.equal(find(h.render(),'Hue').props.value,rgb.settings.hue,'a failed old draft cannot be resurrected by the next verified result');
    h.unmount();
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    const oldTimer = [...h.timers.values()].find(t => t.kind === 'timeout').fn;
    find(h.render(), 'Hue').props.onChange(240);
    oldTimer();
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    assert.equal(h.calls.filter(c => c.name === 'rgb_set_color').length, 1, 'superseded RGB callback cannot enqueue obsolete work');
    assert.equal(h.calls.find(c => c.name === 'rgb_set_color').args[0], 240);
    h.respond('rgb_set_color', {success:true,status:{...rgb,settings:{...rgb.settings,hue:240}}}); await settle(); h.unmount();
  }
  {
    const h = harness('rgb.tsx', 'RgbPage'); h.render(); h.respond('rgb_get_status', rgb); await settle();
    find(h.render(), 'Hue').props.onChange(120);
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    find(h.render(), 'Brightness').props.onChange(90);
    h.respond('rgb_set_color', {success:true,status:{...rgb,settings:{...rgb.settings,hue:120}}}); await settle();
    assert.equal(find(h.render(), 'Brightness').props.value, 90, 'old RGB reply preserves a newer unsaved slider draft');
    for (const [id, timer] of [...h.timers]) if (timer.kind === 'timeout') { h.timers.delete(id); timer.fn(); }
    await settle();
    h.respond('rgb_set_brightness', {success:true,status:{...rgb,settings:{...rgb.settings,hue:120,brightness:90}}});
    await settle(); assert.equal(find(h.render(), 'Hue').props.value, 120); h.unmount();
  }
  console.log('RGB page verified.');
});
