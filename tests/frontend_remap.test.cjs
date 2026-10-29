const { assert, run, settle, find, harness, remap } = require('./legion_page_harness.cjs');
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
    if(queued){h.respond('remap_set_action',{ok:true,state:remap});await settle();}
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
  {
    // D2 answers: a refusal with the current status shows its own text at once; one without status (refused before
    // any change) shows the generic text and re-reads, as a rejected call does.
    const h = harness('remap.tsx', 'RemapPage'); h.render(); h.respond('remap_get_status', remap); await settle();
    const reads = () => h.calls.filter(c => c.name === 'remap_get_status').length;
    find(h.render(), 'Desktop button').props.onChange({ data: 'f3' }); await settle();
    h.respond('remap_set_action', { ok: false, error: 'D-Bus unavailable.', state: remap }); await settle();
    assert.equal(find(h.render(), 'Desktop button').props.description, 'D-Bus unavailable.');
    assert.equal(find(h.render(), 'Desktop button').props.selectedOption, 'f1', 'the refusal status replaces the choice');
    assert.equal(reads(), 1, 'a refusal with its status needs no re-read');
    find(h.render(), 'Desktop button').props.onChange({ data: 'f3' }); await settle();
    h.respond('remap_set_action', { ok: false, error: 'This module is disabled. Enable it in Manage modules.' }); await settle();
    assert.match(find(h.render(), 'Desktop button').props.description, /could not be confirmed/);
    assert.equal(reads(), 2, 'a refusal without status re-reads it');
    h.respond('remap_get_status', remap); await settle();
    find(h.render(), 'Desktop button').props.onChange({ data: 'f3' }); await settle();
    h.respond('remap_set_action', { ok: true, state: { ...remap, desktop_action: 'f3' } }); await settle();
    assert.equal(h.toasts.at(-1).body, 'Desktop button: F3.');
    h.unmount();
  }
  const hidden = harness('remap.tsx', 'RemapPage'); hidden.render(); hidden.respond('remap_get_status', remap); await settle();
  const menu = find(hidden.render(), 'Desktop button'); hidden.visible(false); menu.props.onChange({ data: 'f3' }); await settle();
  assert.equal(hidden.calls.filter(c => c.name === 'remap_set_action').length, 1, 'a dropdown choice still saves while QAM is hidden');
  hidden.respond('remap_set_action', { ok: true, state: { ...remap, desktop_action: 'f3' } }); await settle(); hidden.unmount();
  // The rules both UIs share (src/remap.tsx and the Desktop ModulePage.qml import the same module).
  const M = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/remap.mjs')).href);
  assert.equal(M.mappingEditable({ supported: true, enabled: false }), false, 'button choices wait for mapping');
  assert.equal(M.mappingEditable({ supported: false, enabled: true }), false);
  assert.match(M.enableNote({ enabled: true, active: false }), /Waiting to reapply…$/);
  console.log('Remap page verified.');
});
