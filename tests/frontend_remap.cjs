const { assert, run, settle, find, harness, remap } = require('./frontend_page_harness.cjs');
run(async () => {
  for (const queued of [false,true]) {
    const h=harness('remap.tsx','RemapPage');h.render();h.respond('remap_get_status',remap);await settle();
    const change=find(h.render(),'Desktop button').props.onChange;
    if(queued)change({data:'f3'});
    h.unmount();const before=h.writes;
    if(!queued)change({data:'f3'});
    await settle();assert.equal(h.writes,before,'Unmounted remap callbacks never update React state.');
    assert.equal(h.calls.filter(c=>c.name==='remap_set_action').length,queued?1:0,
      'A previously accepted mapping still saves; a new callback after unmount is ignored.');
    if(queued){h.respond('remap_set_action',{success:true,status:remap});await settle();}
  }
  {
    const h = harness('remap.tsx', 'RemapPage'); h.render(); h.respond('remap_get_status', remap); await settle();
    find(h.render(), 'Desktop button').props.onChange({ data: 'f3' }); await settle();
    h.reject('remap_set_action'); await settle();
    assert.equal(h.calls.filter(c => c.name === 'remap_get_status').length, 2, 'a rejected remap write re-reads the status');
    h.respond('remap_get_status', remap); await settle();
    assert.equal(find(h.render(), 'Desktop button').props.selectedOption, 'f1', 'the unsaved dropdown value is dropped');
    h.unmount();
  }
  {
    // A poll read already in flight when a write is rejected is stale (revision bumped): a fresh read must follow it.
    const h = harness('remap.tsx', 'RemapPage'); h.render(); h.respond('remap_get_status', remap); await settle();
    const reads = () => h.calls.filter(c => c.name === 'remap_get_status').length;
    h.advance(10000); h.fire(); await settle(); assert.equal(reads(), 2, 'the poll tick starts a read');
    find(h.render(), 'Desktop button').props.onChange({ data: 'f3' }); await settle();
    h.reject('remap_set_action'); await settle();
    h.respond('remap_get_status', remap); await settle();
    assert.equal(reads(), 3, 'the stale poll read is followed by a fresh one');
    h.respond('remap_get_status', remap); await settle();
    assert.equal(find(h.render(), 'Desktop button').props.selectedOption, 'f1', 'the rejected dropdown value is dropped');
    h.unmount();
  }
  const hidden = harness('remap.tsx', 'RemapPage'); hidden.render(); hidden.respond('remap_get_status', remap); await settle();
  const menu = find(hidden.render(), 'Desktop button'); hidden.visible(false); menu.props.onChange({ data: 'f3' }); await settle();
  assert.equal(hidden.calls.filter(c => c.name === 'remap_set_action').length, 1, 'a dropdown choice still saves while QAM is hidden');
  hidden.respond('remap_set_action', { success: true, status: { ...remap, desktop_action: 'f3' } }); await settle(); hidden.unmount();
  console.log('Remap page verified.');
});
