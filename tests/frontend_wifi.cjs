const { assert, run, settle, find, findButton, harness, wifi } = require('./frontend_page_harness.cjs');
const band = tree => find(tree, 'Band');
const options = tree => Array.from(band(tree).props.rgOptions, option => option.label);
const choose = (tree, id) => band(tree).props.onChange({ data: id });
const policyCalls = h => h.calls.filter(c => c.name === 'wifi_set_band_policy');
run(async () => {
  for (const failed of [false,true]) {
    const h=harness('wifi.tsx','WifiPage');h.render();h.respond('wifi_get_status',wifi);await settle();
    const tree=h.render();choose(tree,'off');h.unmount();
    const writes=h.writes;
    if(failed)h.reject('wifi_set_band_policy');
    else h.respond('wifi_set_band_policy',{success:true,message:'Saved'});
    await settle();assert.equal(h.writes,writes,'A late Wi-Fi mutation result cannot update an unmounted page.');
    choose(tree,'off');await settle();
    assert.equal(h.writes,writes,'A queued Wi-Fi callback cannot update an unmounted page.');
    assert.equal(policyCalls(h).length,1,'A queued callback cannot start another Wi-Fi operation after unmount.');
  }
  {
    const h=harness('wifi.tsx','WifiPage');h.render();h.respond('wifi_get_status',wifi);await settle();
    assert.equal(band(h.render()).props.selectedOption,'five_six_no_24');
    choose(h.render(),'off');
    h.respond('wifi_set_band_policy',{success:true,reconnected:true,message:'Verified'});await settle();
    assert.equal(h.calls.filter(c=>c.name==='wifi_get_status').length,2,
      'Verified reconnect refreshes immediately instead of holding the page for three seconds.');
    assert.equal([...h.timers.values()].filter(t=>t.kind==='timeout').length,0);
    h.respond('wifi_get_status',{...wifi,settings:{...wifi.settings,band_policy:'off'}});await settle();
    assert.deepEqual(h.toasts.map(t=>[t.title,t.body]),[['Wi-Fi','Verified']],'success is a toast');
    assert.deepEqual(options(h.render()),['Automatic','Prefer 5/6 GHz','Only 5/6 GHz']);
    assert.equal(band(h.render()).props.description,'SteamOS picks the band.');h.unmount();
  }
  const network = harness('wifi.tsx', 'WifiPage'); network.render(); network.respond('wifi_get_status', wifi); await settle();
  network.fire(); choose(network.render(), 'off'); await settle();
  assert.deepEqual(network.calls.find(c => c.name === 'wifi_set_band_policy').args, ['off']);
  assert.equal(band(network.render()).props.description, 'Applying…');
  network.respond('wifi_set_band_policy', { success: true, message: 'Saved' }); await settle();
  const beforeOldRead = network.writes;
  network.respond('wifi_get_status', wifi); await settle();
  assert.equal(network.writes, beforeOldRead, 'old Wi-Fi status cannot overwrite the mutation');
  assert.equal(network.calls.filter(c => c.name === 'wifi_get_status').length, 3, 'Wi-Fi confirms status after the earlier read finishes');
  network.respond('wifi_get_status', { ...wifi, settings: { ...wifi.settings, band_policy: 'off', band_preference_enabled: false } }); await settle();
  assert.equal(band(network.render()).props.selectedOption, 'off'); network.unmount();

  const manualWpa = harness('wifi.tsx', 'WifiPage'); manualWpa.render();
  const automaticWpa = { ...wifi, connected: true, settings: { ...wifi.settings, band_policy: 'off', band_preference_enabled: false },
    live: { wifi_backend: 'wpa_supplicant', frequency: '5260', six_ghz_only_supported: true } };
  manualWpa.respond('wifi_get_status', automaticWpa); await settle();
  assert.deepEqual(options(manualWpa.render()), ['Automatic', 'Only 6 GHz'], 'WPA offers no iwd band choices');
  assert.equal(band(manualWpa.render()).props.description, 'SteamOS picks the band and roams automatically.');
  assert.equal(findButton(manualWpa.render(), 'Reconnect to 5/6 GHz').props.disabled, false);
  findButton(manualWpa.render(), 'Connect to 6 GHz once').props.onClick(); await settle();
  assert.deepEqual(manualWpa.calls.find(c => c.name === 'wifi_rescan_and_reconnect').args, ['six']);
  assert.equal(findButton(manualWpa.render(), 'Connect to 6 GHz once').props.disabled, true);
  manualWpa.respond('wifi_rescan_and_reconnect', { success: true, frequency: 6295, message: 'Connected; temporary selection cleared' }); await settle();
  manualWpa.respond('wifi_get_status', { ...automaticWpa, live: { ...automaticWpa.live, frequency: '6295' } }); await settle();
  assert.equal(band(manualWpa.render()).props.selectedOption, 'off', 'one-time connection keeps the automatic policy');
  assert.equal(manualWpa.toasts.at(-1).body, 'Connected; temporary selection cleared');
  manualWpa.unmount();

  const sixOnly = harness('wifi.tsx', 'WifiPage'); sixOnly.render();
  sixOnly.respond('wifi_get_status', automaticWpa); await settle();
  choose(sixOnly.render(), 'six_ghz_only'); await settle();
  assert.equal(policyCalls(sixOnly).length, 0, 'Only 6 GHz waits for its confirmation');
  const sixAsked = sixOnly.confirm(); await settle();
  assert.deepEqual([sixAsked.strTitle, sixAsked.strOKButtonText], ['Use only 6 GHz?', 'Use only 6 GHz']);
  assert.match(sixAsked.strDescription, /no Wi-Fi until you choose Automatic/);
  assert.deepEqual(sixOnly.calls.find(c => c.name === 'wifi_set_band_policy').args, ['six_ghz_only', false]);
  sixOnly.respond('wifi_set_band_policy', { success: true, message: 'Saved' }); await settle();
  sixOnly.respond('wifi_get_status', { ...automaticWpa, live: { ...automaticWpa.live, frequency: '6295' },
    settings: { ...automaticWpa.settings, band_policy: 'six_ghz_only' } }); await settle();
  assert.equal(sixOnly.toasts.at(-1).body, 'Saved', 'enabling 6 GHz only is verified');
  assert.equal(band(sixOnly.render()).props.selectedOption, 'six_ghz_only');
  assert.equal(band(sixOnly.render()).props.description, 'Connects only to 6 GHz networks.');
  sixOnly.unmount();
  const oldNm = harness('wifi.tsx', 'WifiPage'); oldNm.render();
  oldNm.respond('wifi_get_status', { ...automaticWpa, live: { ...automaticWpa.live, six_ghz_only_supported: false } }); await settle();
  assert.deepEqual(options(oldNm.render()), ['Automatic'], 'NetworkManager without 6 GHz support has no Only 6 GHz choice');
  assert.ok(findButton(oldNm.render(), 'Connect to 6 GHz once'), 'one-time 6 GHz connection stays available');
  oldNm.unmount();
  const offlineWpa = harness('wifi.tsx', 'WifiPage'); offlineWpa.render();
  offlineWpa.respond('wifi_get_status', { ...automaticWpa, connected: false }); await settle();
  assert.deepEqual(options(offlineWpa.render()), ['Automatic'], 'Only 6 GHz needs a live connection');
  choose(offlineWpa.render(), 'six_ghz_only'); await settle();
  assert.equal(offlineWpa.modals.length + policyCalls(offlineWpa).length, 0);
  offlineWpa.unmount();

  const strictWifi = harness('wifi.tsx', 'WifiPage'); strictWifi.render();
  strictWifi.respond('wifi_get_status', wifi); await settle();
  choose(strictWifi.render(), 'five_six_only'); await settle();
  assert.equal(policyCalls(strictWifi).length, 0, 'Only 5/6 GHz waits for its confirmation');
  const strictAsked = strictWifi.confirm(); await settle();
  assert.deepEqual([strictAsked.strTitle, strictAsked.strOKButtonText, strictAsked.strCancelButtonText],
    ['Use only 5/6 GHz?', 'Use only 5/6 GHz', 'Cancel']);
  assert.match(strictAsked.strDescription, /your previous setting comes back automatically/);
  assert.deepEqual(strictWifi.calls.find(c => c.name === 'wifi_set_band_policy').args, ['five_six_only', true]);
  strictWifi.respond('wifi_set_band_policy', { success: true, message: 'Saved' }); await settle();
  strictWifi.respond('wifi_get_status', { ...wifi, connected: false,
    settings: { ...wifi.settings, band_policy: 'five_six_only', band_preference_enabled: false } }); await settle();
  assert.equal(band(strictWifi.render()).props.description, 'No 5/6 GHz network. Choose Automatic or Prefer 5/6 GHz.');
  assert.deepEqual(options(strictWifi.render()), ['Automatic', 'Prefer 5/6 GHz', 'Only 5/6 GHz'], 'strict mode can be disabled when disconnected');
  strictWifi.unmount();

  const oldPolicy = harness('wifi.tsx', 'WifiPage'); oldPolicy.render();
  const six = { ...wifi, connected: true, settings: { ...wifi.settings, band_policy: 'six_ghz_only', band_preference_enabled: false },
    live: { ...wifi.live, wifi_backend: 'wpa_supplicant', frequency: 5975 } };
  oldPolicy.respond('wifi_get_status', six); await settle();
  assert.equal(band(oldPolicy.render()).props.selectedOption, 'six_ghz_only');
  assert.match(oldPolicy.moduleExports.wifiSummary(six), /^Only 6 GHz · 6 GHz$/);
  assert.ok(options(oldPolicy.render()).includes('Automatic'));
  oldPolicy.unmount();
  const inactiveSix=harness('wifi.tsx','WifiPage');inactiveSix.render();
  const sixOnIwd={...six,live:{...six.live,wifi_backend:'iwd'}};
  inactiveSix.respond('wifi_get_status',sixOnIwd);await settle();
  assert.match(inactiveSix.moduleExports.wifiSummary(sixOnIwd),/^Automatic · saved setting unused/);
  assert.match(band(inactiveSix.render()).props.description,/isn't used with the current Wi-Fi backend/);
  assert.ok(options(inactiveSix.render()).includes('Only 6 GHz'), 'a saved but unoffered policy stays visible');
  inactiveSix.unmount();

  const preflightWifi = harness('wifi.tsx', 'WifiPage'); preflightWifi.render();
  preflightWifi.respond('wifi_get_status', wifi); await settle();
  choose(preflightWifi.render(), 'five_six_only'); preflightWifi.confirm(); await settle();
  preflightWifi.respond('wifi_set_band_policy', { success: false, error: 'preflight_failed',
    message: 'No fresh 5 or 6 GHz scan result was verified.' }); await settle();
  preflightWifi.respond('wifi_get_status', wifi); await settle();
  assert.equal(band(preflightWifi.render()).props.description, 'No fresh 5 or 6 GHz scan result was verified.', 'the failure stays on the control');
  assert.equal(preflightWifi.toasts.at(-1).body, 'No fresh 5 or 6 GHz scan result was verified.');
  assert.equal(band(preflightWifi.render()).props.selectedOption, 'five_six_no_24');
  preflightWifi.unmount();

  // Native SteamOS switches and failed polls invalidate cached action callbacks.
  const switched = harness('wifi.tsx', 'WifiPage'); switched.render();
  const automaticIwd = {...wifi,settings:{...wifi.settings,band_policy:'off',band_preference_enabled:false}};
  switched.respond('wifi_get_status', automaticIwd); await settle();
  const staleTree = switched.render();
  switched.fire(); switched.respond('wifi_get_status', automaticWpa); await settle();
  choose(staleTree, 'five_six_no_24'); choose(staleTree, 'five_six_only'); await settle();
  assert.equal(policyCalls(switched).length,0);
  assert.equal(switched.modals.length,0,'a stale strict choice does not ask');
  assert.deepEqual(options(switched.render()),['Automatic','Only 6 GHz']);
  const retainedReconnect = findButton(switched.render(),'Connect to 6 GHz once');
  switched.fire(); switched.reject('wifi_get_status'); await settle();
  retainedReconnect.props.onClick(); await settle();
  assert.equal(switched.calls.filter(c=>c.name==='wifi_rescan_and_reconnect').length,0);
  assert.ok(find(switched.render(),'Wi-Fi status unavailable'));
  switched.unmount();

  const reopen=harness('wifi.tsx','WifiPage');reopen.render();
  reopen.respond('wifi_get_status',automaticIwd);await settle();
  const beforeHide=reopen.render();
  reopen.visible(false);reopen.visible(true);
  assert.equal(band(reopen.render()),undefined,'reopened QAM waits for a fresh backend');
  reopen.respond('wifi_get_status',automaticWpa);await settle();
  choose(beforeHide,'five_six_no_24');await settle();
  assert.equal(policyCalls(reopen).length,0,'a fresh read on another backend refuses the old choice');
  assert.ok(findButton(reopen.render(),'Connect to 6 GHz once'));
  reopen.unmount();

  // The band menu and its confirmations take focus from the Quick Access menu: the choice or OK
  // arrives while the panel is hidden and its status cleared.
  const hiddenStrict=harness('wifi.tsx','WifiPage');hiddenStrict.render();
  hiddenStrict.respond('wifi_get_status',wifi);await settle();
  choose(hiddenStrict.render(),'five_six_only');const strictOk=hiddenStrict.modals.at(-1).props.onOK;
  hiddenStrict.visible(false);await settle();strictOk();strictOk();await settle();
  assert.deepEqual(policyCalls(hiddenStrict).map(c=>Array.from(c.args)),[['five_six_only',true]],'Only 5/6 GHz confirmed while hidden runs once');
  hiddenStrict.respond('wifi_set_band_policy',{success:false,message:'No 5/6 GHz network connected.'});await settle();
  assert.equal(hiddenStrict.toasts.at(-1).body,'No 5/6 GHz network connected.','a hidden failure is a toast');
  hiddenStrict.unmount();

  const hiddenSix=harness('wifi.tsx','WifiPage');hiddenSix.render();
  hiddenSix.respond('wifi_get_status',automaticWpa);await settle();
  choose(hiddenSix.render(),'six_ghz_only');const sixOk=hiddenSix.modals.at(-1).props.onOK;
  hiddenSix.visible(false);await settle();sixOk();await settle();
  assert.deepEqual(policyCalls(hiddenSix).map(c=>Array.from(c.args)),[['six_ghz_only',false]],'Only 6 GHz confirmed while hidden runs once');
  hiddenSix.unmount();

  const hiddenMenu=harness('wifi.tsx','WifiPage');hiddenMenu.render();
  hiddenMenu.respond('wifi_get_status',wifi);await settle();
  const menu=band(hiddenMenu.render());hiddenMenu.visible(false);await settle();
  menu.props.onChange({data:'off'});menu.props.onChange({data:'off'});await settle();
  assert.deepEqual(policyCalls(hiddenMenu).map(c=>c.args[0]),['off'],'Automatic chosen while the menu hid the panel runs once');
  hiddenMenu.unmount();

  const changed=harness('wifi.tsx','WifiPage');changed.render();
  changed.respond('wifi_get_status',wifi);await settle();
  choose(changed.render(),'five_six_only');const changedOk=changed.modals.at(-1).props.onOK;
  changed.fire();changed.respond('wifi_get_status',automaticWpa);await settle();changedOk();await settle();
  assert.equal(policyCalls(changed).length,0,'a newer status that contradicts the dialog refuses its OK');
  assert.equal(changed.toasts.at(-1).body,'Wi-Fi changed. Choose again.');
  changed.unmount();

  const hiddenSuccess=harness('wifi.tsx','WifiPage');hiddenSuccess.render();
  hiddenSuccess.respond('wifi_get_status',wifi);await settle();
  choose(hiddenSuccess.render(),'off');await settle();
  hiddenSuccess.visible(false);hiddenSuccess.respond('wifi_set_band_policy',{success:true,message:'Saved while hidden'});await settle();
  hiddenSuccess.visible(true);hiddenSuccess.respond('wifi_get_status',automaticWpa);await settle();
  assert.equal(hiddenSuccess.toasts.length,0,'a hidden operation neither invents a stale failure nor reports an unverified success');
  assert.equal(band(hiddenSuccess.render()).props.description,'SteamOS picks the band and roams automatically.');
  hiddenSuccess.unmount();

  for (const state of ['unknown','connecting','disconnected']) {
    const h=harness('wifi.tsx','WifiPage');h.render();
    const next={...automaticWpa,connected:false,live:{...automaticWpa.live,connection_state:state}};
    h.respond('wifi_get_status',next);await settle();
    assert.equal(findButton(h.render(),'Connect to 6 GHz once').props.disabled,true);
    findButton(h.render(),'Connect to 6 GHz once').props.onClick();await settle();
    assert.equal(h.calls.filter(c=>c.name!=='wifi_get_status').length,0);
    h.unmount();
  }
  const unknownBackend=harness('wifi.tsx','WifiPage');unknownBackend.render();
  unknownBackend.respond('wifi_get_status',{...wifi,live:{...wifi.live,wifi_backend:''}});await settle();
  assert.ok(find(unknownBackend.render(),'Wi-Fi backend unavailable'));
  assert.equal(band(unknownBackend.render()),undefined);
  assert.match(unknownBackend.moduleExports.wifiSummary(undefined),/unavailable/);
  unknownBackend.unmount();
  const unsupported=harness('wifi.tsx','WifiPage');unsupported.render();
  unsupported.respond('wifi_get_status',{...wifi,settings:{...wifi.settings,driver:'iwlwifi'}});await settle();
  assert.equal(find(unsupported.render(),'Wi-Fi tweaks unavailable').props.description,'Only for the Legion Go 2 Wi-Fi card (MT7922).');
  assert.deepEqual(options(unsupported.render()),['Prefer 5/6 GHz']);
  unsupported.unmount();

  const cleanup=harness('wifi.tsx','WifiPage');cleanup.render();
  const staleWpa={...wifi,live:{...wifi.live,wifi_backend:'wpa_supplicant',recovery_required:true,band_policy_restore_available:true}};
  cleanup.respond('wifi_get_status',staleWpa);await settle();
  assert.ok(find(cleanup.render(),'Wi-Fi settings are locked'));
  assert.match(cleanup.moduleExports.wifiSummary(staleWpa),/^Automatic · saved setting unused/);
  assert.deepEqual(options(cleanup.render()),['Automatic','Prefer 5/6 GHz'],'the saved iwd policy can still be cleared');
  const tree=cleanup.render();choose(tree,'off');choose(tree,'off');await settle();
  assert.equal(policyCalls(cleanup).length,1);
  assert.equal(find(cleanup.render(),'Target').props.description,'Checking the connection…');
  cleanup.respond('wifi_set_band_policy',{success:true,message:'Cleared'});await settle();
  assert.equal(cleanup.toasts.length,0,'success waits for the actual final state');
  cleanup.respond('wifi_get_status',{...automaticWpa,live:{...wifi.live,wifi_backend:'wpa_supplicant',ssid:' spaced network '}});await settle();
  assert.ok(find(cleanup.render(),' spaced network '));
  assert.equal(cleanup.toasts.at(-1).body,'Cleared');
  cleanup.unmount();

  const mismatched=harness('wifi.tsx','WifiPage');mismatched.render();
  mismatched.respond('wifi_get_status',wifi);await settle();
  choose(mismatched.render(),'off');await settle();
  mismatched.respond('wifi_set_band_policy',{success:true,message:'Saved'});await settle();
  mismatched.respond('wifi_get_status',wifi);await settle();
  assert.match(band(mismatched.render()).props.description,/could not be verified/);
  assert.match(mismatched.toasts.at(-1).body,/could not be verified/);
  mismatched.unmount();

  const strayed=harness('wifi.tsx','WifiPage');strayed.render();
  strayed.respond('wifi_get_status',{...wifi,settings:{...wifi.settings,band_policy:'five_six_only'},live:{...wifi.live,frequency:'2437'}});await settle();
  assert.equal(band(strayed.render()).props.description,'Saved: Only 5/6 GHz, but connected on 2.4 GHz.');
  strayed.unmount();
  console.log('Wi-Fi page verified.');
});
