const { assert, run, settle, find, findButton, harness, display } = require('./legion_page_harness.cjs');
run(async () => {
  {
    const h=harness('display.tsx','DisplayPage');h.render();h.respond('display_get_state',display);await settle();
    const change=find(h.render(),'Fix HDR brightness in games').props.onChange;
    h.unmount();const before=h.writes;change(false);await settle();
    assert.equal(h.calls.filter(c=>c.name==='display_set_edid_fix').length,0,'Unmounted display callbacks cannot start writes.');
    assert.equal(h.writes,before);
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(h.render(), 'Fix HDR brightness in games').props.onChange;
    h.fire(); h.reject('display_get_state'); await settle();
    const failed = h.render();
    assert.ok(find(failed, 'Display status unavailable'), 'A failed poll cannot present a saved display snapshot as current.');
    retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'Retained controls cannot write after a failed read.');
    findButton(failed, 'Try again').props.onClick();
    h.respond('display_get_state', display); await settle();
    assert.ok(find(h.render(), 'Fix HDR brightness in games'), 'A fresh read restores controls.');
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(h.render(), 'Fix HDR brightness in games').props.onChange;
    h.visible(false); retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'A hidden display page does not accept a stale toggle.');
    h.visible(true); retained(false); await settle();
    assert.equal(h.calls.some(c => c.name === 'display_set_edid_fix'), false, 'Reopening must confirm current display ownership first.');
    h.respond('display_get_state', display); await settle();
    retained(false); await settle();
    assert.equal(h.calls.filter(c => c.name === 'display_set_edid_fix').length, 1);
    h.respond('display_set_edid_fix', { ...display, edid_fix: false }); await settle(); h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const retained = find(h.render(), 'Fix HDR brightness in games').props.onChange;
    retained(false); await settle(); h.visible(false); h.visible(true);
    h.respond('display_set_edid_fix', { ...display, edid_fix: false }); await settle();
    assert.equal(find(h.render(), 'Fix HDR brightness in games'), undefined, 'A previous visit write result cannot establish current ownership after reopening.');
    retained(true); await settle();
    assert.equal(h.calls.filter(c => c.name === 'display_set_edid_fix').length, 1);
    h.respond('display_get_state', { ...display, edid_fix: false }); await settle();
    assert.ok(find(h.render(), 'Fix HDR brightness in games')); h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, steamos_update_needed: true }); await settle();
    let tree = h.render();
    assert.equal(find(tree, 'SteamOS update needed').props.description,
      'Update SteamOS to 3.9 or newer, which supports this screen natively. The HDR brightness fix below still works.');
    assert.equal(find(tree, 'Fix HDR brightness in games').props.description,
      "Games see the screen's real HDR brightness. Restart the game after changing. On · games see 1107 nits.");
    h.fire(); h.respond('display_get_state', { ...display, settings_error: 'Settings unreadable.' }); await settle();
    tree = h.render();
    assert.equal(find(tree, 'SteamOS update needed'), undefined, 'SteamOS 3.9 needs no notice');
    assert.equal(find(tree, 'Display settings unavailable').props.description, 'Settings unreadable.');
    assert.equal(find(tree, 'Fix HDR brightness in games').props.disabled, true, 'unreadable settings pause the switch');
    assert.equal(findButton(tree, 'Restart Gaming Mode'), undefined);
    h.unmount();
  }
  // The texts both UIs share (src/display.tsx and the Desktop ModulePage.qml import the same module).
  const D = await import(require('node:url').pathToFileURL(require('node:path').join(__dirname, '../desktop/native/display.mjs')).href);
  assert.equal(D.steamosNotice({ steamos_update_needed: false }), '');
  assert.deepEqual([D.edidStatus({ edid_fix: false, edid_patched: true }), D.edidStatus({ edid_fix: true }),
    D.edidStatus({ edid_fix: true, edid_patched: true, edid_game_nits: 0 })], ['Off', 'Not active yet', 'On']);
  console.log('Display page verified.');
});
