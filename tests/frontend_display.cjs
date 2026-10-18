const { assert, run, settle, find, findButton, harness, findModeChoice, display, nativeDisplay } = require('./frontend_page_harness.cjs');
const { nodes } = require('./frontend_harness.cjs');
const lastRow = tree => nodes(tree).filter(n => ['ButtonItem', 'ToggleField', 'Field'].includes(n.type)).at(-1);
run(async () => {
  {
    const h=harness('display.tsx','DisplayPage');h.render();h.respond('display_get_state',display);await settle();
    const change=find(h.render(),'Brightness slider in HDR').props.onChange;
    h.unmount();const before=h.writes;change(false);await settle();
    assert.equal(h.calls.filter(c=>c.name==='display_set_enabled').length,0,'Unmounted display callbacks cannot start writes.');
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
    findButton(failed, 'Retry status').props.onClick();
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
    h.respond('display_get_state', display); await settle();
    findButton(h.render(), 'Reset display fix').props.onClick();
    h.fire(); // Keep an older status read unresolved across the reset.
    assert.equal(h.calls.some(c => c.name === 'display_reset_settings'), false, 'Reset waits for its confirmation.');
    const asked = h.confirm(); h.confirm(); await settle();
    assert.deepEqual([asked.strTitle, asked.strDescription, asked.strOKButtonText], ['Reset display fix?',
      'Companion removes its display changes and restores your previous display script, if there was one. Gaming Mode may need a restart.', 'Reset']);
    assert.equal(h.calls.filter(c => c.name === 'display_reset_settings').length, 1);
    const initial = { ...display, panel_mode: null, active_mode: null, setup_done: false,
      reset_in_progress: false, reset_restart_pending: true, reset_error: '' };
    h.respond('display_reset_settings', initial); await settle();
    assert.ok(findButton(h.render(), 'Restart Gaming Mode'));
    assert.equal(findButton(h.render(), 'Other options').props.disabled, true);
    h.respond('display_get_state', display); await settle();
    assert.ok(findButton(h.render(), 'Restart Gaming Mode'), 'stale read cannot undo the reset');
    h.fire(); h.respond('display_get_state', { ...initial, reset_restart_pending: false }); await settle();
    assert.equal(findButton(h.render(), 'Other options').props.disabled, false);
    assert.equal(findButton(h.render(), 'Reset display fix'), undefined, 'reset returns to the initial chooser');
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, reset_in_progress: true, reset_error: 'Restore failed' }); await settle();
    assert.equal(find(h.render(), 'Display controls are paused').props.description, 'Restore failed');
    assert.equal(find(h.render(), 'Fix HDR brightness in games'), undefined);
    findButton(h.render(), 'Retry display reset').props.onClick(); await settle();
    h.respond('display_reset_settings', { ...display, panel_mode: null, setup_done: false,
      reset_in_progress: false, reset_restart_pending: true, reset_error: '' }); await settle();
    assert.ok(findButton(h.render(), 'Restart Gaming Mode')); h.unmount();
  }
  for (const gate of [{ setup_done: false }, { restart_pending: true }]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, ...gate }); await settle();
    assert.ok(findButton(h.render(), 'Reset display fix'), 'reset remains accessible behind setup/restart gates');
    h.unmount();
  }
  for (const state of [
    { ...display, panel_mode: null, setup_done: false },
    { ...display, setup_done: false },
    { ...display, restart_pending: true },
    { ...display, panel_mode: null, setup_done: false, reset_restart_pending: true },
    nativeDisplay,
    { ...nativeDisplay, native_cleanup_pending: true },
    { ...nativeDisplay, native_cleanup_pending: true, native_cleanup_error: 'Previous script could not be restored.' },
    { ...nativeDisplay, native_restart_pending: true },
    { ...nativeDisplay, native_support_detected: false, native_cleanup_pending: true },
  ]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', state); await settle();
    let tree = h.render();
    const edid = find(tree, 'Fix HDR brightness in games');
    assert.ok(edid, 'EDID is visible independently of mode, setup and cleanup.');
    assert.equal(edid.props.disabled, false);
    if (state.native_support_detected || state.native_cleanup_pending) {
      assert.equal(find(tree, 'Brightness slider in HDR'), undefined);
      assert.equal(findModeChoice(tree), undefined);
      assert.equal(findButton(tree, 'Install display fix'), undefined);
      assert.equal(findButton(tree, 'Change display mode'), undefined);
      assert.equal(findButton(tree, 'Reset display fix'), undefined);
      assert.ok(find(tree, state.native_support_detected ? 'SteamOS supports this screen' : 'Display fix cleanup pending'));
    }
    if (state.native_support_detected) {
      assert.equal(find(tree, 'SteamOS supports this screen').props.description, "Companion's display fix is off. The HDR game fix below still works.");
      findButton(tree, 'Show details').props.onClick(); tree = h.render();
      assert.equal(find(tree, 'SteamOS support').props.description, state.native_support_reason);
    }
    if (state.native_cleanup_error) {
      assert.match(find(tree, 'Display fix cleanup needs attention').props.description, /Previous script.*retry automatically/);
    }
    edid.props.onChange(false); await settle();
    assert.deepEqual(h.calls.map(call => call.name), ['display_get_state', 'display_set_edid_fix']);
    assert.deepEqual(Array.from(h.calls[1].args), [false]);
    h.respond('display_set_edid_fix', { ...state, edid_fix: false }); await settle();
    tree = h.render();
    assert.equal(find(tree, 'Fix HDR brightness in games').props.checked, false);
    if (state.native_restart_pending) {
      assert.equal(h.calls.some(call => call.name === 'display_restart_session'), false, 'Native handover never restarts automatically.');
      findButton(tree, 'Restart Gaming Mode').props.onClick(); await settle();
      assert.equal(h.calls.some(call => call.name === 'display_restart_session'), false, 'Restart waits for its confirmation.');
      assert.equal(h.confirm().strTitle, 'Restart Gaming Mode?'); await settle();
      assert.equal(h.calls.filter(call => call.name === 'display_restart_session').length, 1);
      h.respond('display_restart_session', { ...state, native_restart_pending: false }); await settle();
    }
    h.unmount();
  }
  // A retained callback from an older render cannot re-enable the fix after a
  // newer native-support snapshot has arrived, even before React renders it.
  for (const state of [nativeDisplay, { ...nativeDisplay, native_support_detected: false, native_cleanup_pending: true }]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    const brightness = find(h.render(), 'Brightness slider in HDR');
    findButton(h.render(), 'Change display mode').props.onClick();
    const mode = findModeChoice(h.render()); assert.ok(mode);
    const reset = findButton(h.render(), 'Reset display fix'); assert.ok(reset);
    h.fire(); h.respond('display_get_state', state); await settle();
    brightness.props.onChange(true); mode.props.onPick('hybrid'); reset.props.onClick(); h.confirm(); await settle();
    assert.equal(h.calls.some(call => ['display_set_enabled', 'display_set_panel_mode', 'display_reset_settings'].includes(call.name)), false);
    h.unmount();
    const chooser = harness('display.tsx', 'DisplayPage'); chooser.render();
    chooser.respond('display_get_state', { ...display, panel_mode: null, setup_done: false }); await settle();
    const setup = findModeChoice(chooser.render()); assert.ok(setup);
    chooser.fire(); chooser.respond('display_get_state', state); await settle();
    setup.props.onPick('hybrid'); chooser.confirm(); await settle();
    assert.equal(chooser.calls.some(call => call.name === 'display_run_setup'), false); chooser.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', display); await settle();
    h.fire(); // This old polling response predates the native handover.
    find(h.render(), 'Fix HDR brightness in games').props.onChange(false); await settle();
    h.respond('display_set_edid_fix', { ...nativeDisplay, edid_fix: false }); await settle();
    h.respond('display_get_state', display); await settle();
    assert.ok(find(h.render(), 'SteamOS supports this screen'), 'Stale polling cannot undo the native gate.');
    assert.equal(find(h.render(), 'Fix HDR brightness in games').props.checked, false); h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, panel_mode: null, setup_done: false }); await settle();
    findModeChoice(h.render()).props.onPick('hybrid'); await settle();
    assert.equal(h.calls.some(call => call.name === 'display_run_setup'), false, 'Setup waits for its confirmation.');
    const asked = h.confirm(); await settle();
    assert.deepEqual([asked.strTitle, asked.strOKButtonText], ['Install display fix for Hybrid?', 'Install']);
    assert.match(asked.strDescription, /kept as a \.backup file\. Gaming Mode needs a restart afterwards\./);
    assert.deepEqual(h.calls.filter(call => call.name === 'display_run_setup').map(call => Array.from(call.args)), [['hybrid']]);
    h.respond('display_run_setup', { ...display, restart_pending: true }); await settle(); h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render();
    h.respond('display_get_state', { ...display, panel_mode: 'hybrid', gamescope_reachable: true, hdr_now: false,
      active: true, nits: 210.4, edid_patched: true, edid_game_nits: 1100 }); await settle();
    let tree = h.render();
    assert.equal(findButton(tree, 'Change display mode').props.description, 'Now: Hybrid (screen in Gamma 2.2)');
    assert.equal(find(tree, 'Brightness slider in HDR').props.description,
      'Lets the Steam brightness slider dim the screen in PQ. Working now (210 nits).');
    assert.equal(find(tree, 'Fix HDR brightness in games').props.description,
      "Games see the screen's real HDR brightness. Restart the game after changing. Games see 1100 nits.");
    assert.equal(lastRow(tree).props.children, 'Show details', 'the page ends with Show details');
    findButton(tree, 'Change display mode').props.onClick(); tree = h.render();
    const pq = nodes(tree).find(n => typeof n.type === 'function' && n.props?.label === 'Use PQ');
    const pqButton = nodes(pq.type(pq.props)).find(n => n.type === 'ButtonItem');
    assert.ok(pqButton.props.description, 'the pros and cons are the button description, not a row');
    pqButton.props.onClick(); await settle();
    assert.equal(h.modals.length, 0, 'switching mode after setup needs no confirmation');
    assert.deepEqual(h.calls.filter(c => c.name === 'display_set_panel_mode').map(c => Array.from(c.args)), [['pq']]);
    h.respond('display_set_panel_mode', { ...display, restart_pending: true, restart_error: 'session busy' }); await settle();
    tree = h.render();
    assert.equal(find(tree, 'Restart Gaming Mode to finish').props.description,
      'session busy. Restart from the Steam menu (Power → Switch to Desktop and back) or reboot.');
    assert.equal(lastRow(tree).props.children, 'Show details');
    findButton(tree, 'Show details').props.onClick(); tree = h.render();
    assert.equal(lastRow(tree).props.label, 'HDR info'); h.unmount();
  }
  // A confirmation takes focus from the Quick Access menu: OK arrives while the panel is hidden.
  for (const [label, state, open, rpc] of [
    ['setup', { ...display, panel_mode: null, setup_done: false }, tree => findModeChoice(tree).props.onPick('hybrid'), 'display_run_setup'],
    ['reset', display, tree => findButton(tree, 'Reset display fix').props.onClick(), 'display_reset_settings'],
    ['restart', { ...display, restart_pending: true }, tree => findButton(tree, 'Restart Gaming Mode').props.onClick(), 'display_restart_session'],
  ]) {
    const h = harness('display.tsx', 'DisplayPage'); h.render(); h.respond('display_get_state', state); await settle();
    open(h.render()); const ok = h.modals.at(-1).props.onOK;
    h.visible(false); await settle(); ok(); ok(); await settle();
    assert.equal(h.calls.filter(c => c.name === rpc).length, 1, `${label} confirmed while the panel is hidden runs once`);
    h.unmount();
  }
  {
    const h = harness('display.tsx', 'DisplayPage'); h.render(); h.respond('display_get_state', display); await settle();
    findButton(h.render(), 'Reset display fix').props.onClick(); const ok = h.modals.at(-1).props.onOK;
    h.visible(false); await settle(); ok(); await settle();
    h.reject('display_reset_settings'); await settle();
    assert.equal(h.toasts.at(-1).body, 'temporary RPC failure', 'a hidden failure is a toast');
    h.unmount();
  }
  console.log('Display page verified.');
});
