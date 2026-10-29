const { assert, run, settle, find, harness, findSection, rgb, remap, display, wifi } = require('./legion_page_harness.cjs');
run(async () => {
  const overviewFailures = [];
  const modules = Object.fromEntries(['tdp', 'fan', 'vibration', 'display', 'wifi', 'rgb', 'remap', 'battery', 'controller']
    .map(name => [name, { enabled: true }]));
  const overviewValues = { get_version: { version: 'test', blocked: false, modules },
    fan_get_state: { supported: true, error: '', receivedAt: 100000, active_curve: 'global', settings: { enabled: false },
      readings: { valid: true, rpm: 1555, apu_temperature: 45, age_seconds: 0, automatic: true, error: '' } },
    get_settings: { spl: 23000, sppt: 30000, fppt: 35000 },
    vibe_get_settings: { settings: { level: 2, mode: 2, touchpadEnabled: false } },
    vibe_get_driver_status: { found: true }, display_get_state: display, wifi_get_status: wifi,
    rgb_get_status: rgb, remap_get_status: remap,
    battery_get_status: { success: true, supported: true, managed: true, enabled: true, requested_enabled: true },
    battery_get_controller_levels: { available: true, left: 75, right: 82, connection_left: 'attached', connection_right: 'attached' },
    controller_get_status: {} };
  const answerOverview = (h, except = []) => {
    for (const request of [...h.calls]) if (!request.settled && !except.includes(request.name)) {
      assert.ok(request.name in overviewValues, `known overview read ${request.name}`);
      request.settled = true; request.resolve(overviewValues[request.name]);
    }
  };
  const newOverview = async () => {
    const h = harness('index.tsx', 'Controls'), plugin = h.initialize();
    answerOverview(h); await settle(); h.calls.length = 0;
    h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.render({ modules: structuredClone(modules) });
    await settle();
    return h;
  };
  const testOverview = async (name, check) => {
    const h = await newOverview();
    try { await check(h); } catch (error) { overviewFailures.push(new Error(`${name}: ${error.message}`)); }
    finally { h.dispose(); }
  };
  const assertPowerSummary = h => assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 23 W');
  await testOverview('overview labels include confirmed controller levels and touchpad state', async h => {
    h.respond('get_settings', { spl: 20000, sppt: 32000, fppt: 35000, enabled: true, active_preset: 'performance' });
    answerOverview(h); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Performance · 20 W');
    assert.equal(findSection(h.render(), 'Vibration').props.description, 'Medium · Standard · Touchpad Off');
    assert.equal(findSection(h.render(), 'Battery').props.description,
      'Protection on · L 75% · R 82%');
  });
  await testOverview('equivalent module objects preserve the pending overview read', async h => {
    h.render({ modules: structuredClone(modules) });
    answerOverview(h); await settle();
    assertPowerSummary(h);
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'equivalent props do not duplicate reads');
  });
  await testOverview('fast summaries appear while WiFi is still pending', async h => {
    answerOverview(h, ['wifi_get_status']); await settle();
    assert.ok(h.calls.some(c => c.name === 'wifi_get_status' && !c.settled));
    assertPowerSummary(h);
  });
  await testOverview('hide and reopen during a read retains its eventual summary', async h => {
    h.visible(false); h.visible(true);
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'reopening does not overlap the pending read');
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  await testOverview('remounted overview immediately shows the last confirmed summary', async h => {
    answerOverview(h); await settle(); assertPowerSummary(h);
    h.unmount();
    const remounted = h.remount();
    assert.equal(findSection(remounted, 'TDP').props.description, 'Custom · 23 W');
  });
  await testOverview('pending overview reads survive actual unmount/remount without duplication', async h => {
    h.unmount(); h.remount();
    await settle();
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1);
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  await testOverview('a read completed while hidden is cached for immediate reopening', async h => {
    h.visible(false); answerOverview(h); await settle();
    const reopened = h.visible(true);
    assert.equal(findSection(reopened, 'TDP').props.description, 'Custom · 23 W');
  });
  await testOverview('repeated and queued ticks never overlap pending reads or restart hidden polling', async h => {
    const queuedTicks = [...h.timers.values()].filter(t => t.kind === 'interval').map(t => t.fn);
    assert.ok(queuedTicks.length, 'visible overview schedules polling');
    const reads = h.calls.length;
    h.fire(); h.fire();
    assert.equal(h.calls.length, reads, 'pending overview reads remain single-flight');
    h.visible(false); queuedTicks.forEach(tick => tick());
    assert.equal(h.calls.length, reads, 'a queued tick cannot start hidden reads');
    answerOverview(h); await settle();
    h.fire(); queuedTicks.forEach(tick => tick()); await settle();
    assert.equal(h.calls.length, reads, 'completed hidden reads do not restart polling or retry');
  });
  await testOverview('a failed refresh preserves the previously confirmed TDP summary', async h => {
    answerOverview(h); await settle(); assertPowerSummary(h);
    h.fire();
    const refresh = h.calls.find(c => c.name === 'get_settings' && !c.settled);
    assert.ok(refresh, 'the next poll refreshes TDP');
    refresh.settled = true; refresh.reject(new Error('temporary RPC failure'));
    answerOverview(h); await settle();
    assertPowerSummary(h);
  });
  const section = (h, title = 'TDP') => findSection(h.render(), title).props;
  const failPower = async h => { h.reject('get_settings'); answerOverview(h); await settle(); };
  await testOverview('an initial failure has a retry hint and recovers without leaving stale metadata', async h => {
    await failPower(h);
    assert.equal(section(h).description, 'Status unavailable');
    assert.match(section(h).statusNote, /Retrying while this menu is open/);
    assert.equal(section(h, 'Vibration').statusNote, undefined);
    h.advance(10000); h.fire(); answerOverview(h); await settle();
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('only repeated failures mark confirmed data older and the existing tick updates its age', async h => {
    answerOverview(h); await settle();
    h.advance(10000); h.fire(); await failPower(h);
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined, 'one transient failure is quiet');
    h.advance(10000); h.fire(); await failPower(h);
    assertPowerSummary(h); assert.match(section(h).statusNote, /Older data.*Last read 20s ago/);
    assert.equal(section(h, 'Vibration').statusNote, undefined, 'failure metadata stays with its own RPC');
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.match(section(h).statusNote, /Last read 30s ago/);
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined, 'a confirmed refresh clears the warning');
    assert.equal([...h.timers.values()].filter(timer => timer.delay === 1000).length, 1, 'only live fan telemetry has a fast timer');
    assert.ok([...h.timers.values()].every(timer => timer.delay === 1000 || timer.delay >= 10000));
  });
  await testOverview('fan uses real measured numbers, isolated live polling, stale/error clearing and hidden cleanup', async h => {
    answerOverview(h); await settle();
    assert.match(section(h,'Fan Control').description,/APU 45.0°C · 1555 RPM · Automatic/);
    const timer=[...h.timers.values()].find(t=>t.delay===1000);assert.ok(timer);
    const count=h.calls.length;h.advance(1000);timer.fn();await settle();
    assert.deepEqual(h.calls.slice(count).map(c=>c.name),['fan_get_state'],'1s tick reads only Fan');
    h.advance(2000);timer.fn();await settle();
    assert.equal(h.calls.length,count+1,'hung fan read never overlaps');
    assert.match(section(h,'Fan Control').description,/APU — · — RPM/,'cached numbers expire while RPC hangs');
    h.reject('fan_get_state');await settle();assert.match(section(h,'Fan Control').description,/APU — · — RPM/);
    const before=h.calls.length;h.visible(false);assert.equal([...h.timers.values()].filter(t=>t.delay===1000).length,0);
    h.fire();await settle();assert.equal(h.calls.length,before,'hidden overview does no fan polling');
    h.visible(true);await settle();assert.equal(h.calls.filter(c=>c.name==='fan_get_state'&&!c.settled).length,1,'reopen requests a fresh fan read');
  });
  await testOverview('switching away from Decky tab stops polling even while QAM itself remains visible', async h => {
    answerOverview(h); await settle();const count=h.calls.length;
    h.panelVisible(false);assert.equal([...h.timers.values()].filter(t=>t.delay===1000).length,0,'hidden root removes live fan timer');
    h.fire();await settle();assert.equal(h.calls.length,count,'no new read behind the other QAM tab');
    h.panelVisible(true);await settle();assert.equal(h.calls.filter(c=>c.name==='fan_get_state'&&!c.settled).length,1,'actual root visibility restarts one fresh request');
    findSection(h.render(),'Fan Control').props.onClick();h.render();
    h.panelVisible(false);
    assert.ok(findSection(h.render(),'Fan Control','onBack'),
      'The current page, not a previously captured tree, remains mounted while polling pauses');
  });
  await testOverview('a hung cached read is marked delayed without starting overlapping RPCs', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    answerOverview(h, ['get_settings']); await settle();
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.doesNotMatch(section(h).statusNote || '', /Older data|delayed/);
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assertPowerSummary(h); assert.match(section(h).statusNote, /Older data.*Last read 40s ago.*Update delayed/);
    assert.equal(h.calls.filter(call => call.name === 'get_settings').length, 2);
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('a hung initial read becomes unavailable only after thirty visible seconds', async h => {
    answerOverview(h, ['get_settings']); await settle();
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.notEqual(section(h).description, 'Status unavailable');
    h.advance(10000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.equal(section(h).description, 'Status unavailable');
    assert.match(section(h).statusNote, /Still waiting/);
    assert.equal(section(h, 'Vibration').statusNote, undefined);
    assert.equal(h.calls.filter(call => call.name === 'get_settings').length, 1);
  });
  await testOverview('hidden time never turns a pending read into a failure', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    answerOverview(h, ['get_settings']); await settle(); h.advance(10000);
    const queuedTicks = [...h.timers.values()].filter(timer => timer.kind === 'interval').map(timer => timer.fn);
    h.visible(false); const before = h.calls.length;
    h.advance(3600000); queuedTicks.forEach(tick => tick());
    assert.equal(h.calls.length, before, 'hidden age checks never start work');
    h.visible(true);
    assert.match(section(h).statusNote, /^Updating….*Last read 1h ago/);
    assert.doesNotMatch(section(h).statusNote, /Older data|delayed/);
    h.advance(20000); h.fire(); answerOverview(h, ['get_settings']); await settle();
    assert.match(section(h).statusNote, /Older data.*Update delayed/, 'only accumulated visible waiting counts');
    h.respond('get_settings', overviewValues.get_settings); await settle();
    assert.equal(section(h).statusNote, undefined);
  });
  await testOverview('hidden rejections do not count toward a visible failure streak', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    h.visible(false); await failPower(h); h.advance(3600000); h.visible(true);
    await failPower(h);
    assert.equal(section(h).statusNote, undefined, 'the first visible error still tolerates a transient failure');
    h.advance(10000); h.fire(); await failPower(h);
    assert.match(section(h).statusNote, /Older data/);
  });
  await testOverview('fresh backend warnings replace old healthy data and survive later transport failures', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    h.respond('battery_get_status', { supported: false, reason: 'Battery firmware unavailable' });
    h.respond('controller_get_status', { available: true, recovery_pending: true, reason: 'Restore the interrupted controller change' });
    h.respond('wifi_get_status', { success: false, error: 'unexpected', message: 'Wireless settings could not be verified' });
    answerOverview(h); await settle();
    assert.equal(section(h, 'Battery').description, 'Battery firmware unavailable');
    assert.equal(section(h, 'Gyro & touchpad').description, 'Restore the interrupted controller change');
    assert.equal(section(h, 'Wi-Fi').description, 'Wireless settings could not be verified');
    for (let i = 0; i < 2; i++) {
      h.advance(10000); h.fire(); h.reject('battery_get_status'); answerOverview(h); await settle();
    }
    assert.equal(section(h, 'Battery').description, 'Battery firmware unavailable');
    assert.match(section(h, 'Battery').statusNote, /Older data/);
  });
  await testOverview('driver warnings are not masked by missing vibration settings', async h => {
    h.respond('vibe_get_driver_status', { found: false }); h.reject('vibe_get_settings');
    answerOverview(h); await settle();
    assert.equal(section(h, 'Vibration').description, 'Controller driver not detected');
    assert.match(section(h, 'Vibration').statusNote, /Retrying/);
  });
  await testOverview('display settings_error replaces its earlier healthy summary', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    h.respond('display_get_state', { ...display, settings_error: 'Display controls need attention' });
    answerOverview(h); await settle();
    assert.equal(section(h, 'Display').description, 'Display controls need attention');
    h.advance(10000); h.fire(); answerOverview(h); await settle();
    assert.notEqual(section(h, 'Display').description, 'Display controls need attention');
  });
  for (const [state, expected] of [
    [display, 'HDR game fix on'],
    [{ ...display, edid_patched: false }, 'HDR game fix not active yet'],
    [{ ...display, edid_fix: false, edid_patched: false }, 'HDR game fix off'],
    [{ ...display, steamos_update_needed: true }, 'Update SteamOS to 3.9 · HDR game fix on'],
  ]) {
    await testOverview(`display summary: ${expected}`, async h => {
      h.respond('display_get_state', state); answerOverview(h); await settle();
      assert.equal(section(h, 'Display').description, expected);
    });
  }
  await testOverview('stale rejected page reads cannot seed the returned overview metadata', async h => {
    answerOverview(h); await settle(); h.advance(10000); h.fire();
    findSection(h.render(), 'TDP').props.onClick(); h.render();
    h.reject('get_settings'); await settle();
    findSection(h.render(), 'TDP', 'onBack').props.onBack(); h.render();
    await failPower(h);
    assertPowerSummary(h); assert.equal(section(h).statusNote, undefined, 'only the post-page error counts');
  });
  for (const gate of [{ enabled: false }, { enabled: true, pending: true }]) {
    await testOverview(`module gate ${JSON.stringify(gate)} clears freshness and ignores rejected prior reads`, async h => {
      answerOverview(h); await settle(); h.advance(10000); h.fire(); await failPower(h);
      h.advance(10000); h.fire(); await failPower(h);
      assert.match(section(h).statusNote, /Older data/);
      h.fire();
      const gated = { ...structuredClone(modules), tdp: gate };
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules: gated }); h.render({ modules: gated });
      h.reject('get_settings'); await settle();
      h.callbacks.companion_guard(overviewValues.get_version); h.render({ modules });
      assert.equal(section(h).statusNote, undefined);
      assert.notEqual(section(h).description, 'Custom · 23 W');
      answerOverview(h); await settle(); assertPowerSummary(h);
    });
  }
  await testOverview('an old plugin rejection cannot add freshness warnings to its replacement', async h => {
    const oldRead = h.calls.find(call => call.name === 'get_settings' && !call.settled);
    h.dispose(); h.advance(3600000);
    const plugin = h.initialize(); h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.respond('get_version', overviewValues.get_version); await settle(); h.remount();
    oldRead.settled = true; oldRead.reject(new Error('old plugin disconnected')); await settle();
    assert.equal(section(h).statusNote, undefined);
    assert.notEqual(section(h).description, 'Status unavailable');
    answerOverview(h); await settle(); assertPowerSummary(h);
  });
  await testOverview('late replies from a disposed plugin cannot populate or unlock the new instance', async h => {
    const oldRead = h.calls.find(c => c.name === 'get_settings' && !c.settled);
    h.dispose();
    const plugin = h.initialize();
    h.dispose = () => { h.unmount(); plugin.onDismount(); };
    h.respond('get_version', overviewValues.get_version); await settle();
    h.remount(); await settle();
    const currentRead = h.calls.find(c => c.name === 'get_settings' && !c.settled && c !== oldRead);
    assert.ok(currentRead, 'the new plugin starts its own read');
    oldRead.settled = true; oldRead.resolve(overviewValues.get_settings); await settle();
    assert.notEqual(findSection(h.render(), 'TDP').props.description, 'Custom · 23 W', 'the disposed plugin cannot seed the new cache');
    h.fire();
    assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 2, 'the old reply cannot clear the new single-flight guard');
    currentRead.settled = true; currentRead.resolve({ spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 W');
  });
  for (const state of [{ enabled: false }, { enabled: true, pending: true }]) {
    await testOverview(`real module gate ${JSON.stringify(state)} rejects stale status and stops its reads`, async h => {
      const gated = { ...structuredClone(modules), tdp: state };
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules: gated });
      assert.equal(findSection(h.render({ modules: gated }), 'TDP'), undefined);
      answerOverview(h); await settle(); h.fire();
      assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'gated modules are not polled');
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules });
      const reopened = h.render({ modules });
      assert.notEqual(findSection(reopened, 'TDP').props.description, 'Custom · 23 W', 'old gated reply never enters the cache');
      await settle();
      h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
      assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 W');
    });
  }
  for (const [state, text] of [[{ enabled: true, ready: false, starting: true, error: '' }, 'Still starting. Its controls appear when it has finished.'],
    [{ enabled: false, ready: false, starting: true, error: '' }, 'Turns off when it has finished starting.']]) {
    await testOverview(`a module still starting ${JSON.stringify(state)} keeps a row without controls`, async h => {
      const starting = { ...structuredClone(modules), tdp: state };
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules: starting });
      const tree = h.render({ modules: starting });
      assert.equal(findSection(tree, 'TDP'), undefined, 'no link to controls that are not ready');
      const row = find(tree, 'TDP');
      assert.equal(row?.props.description, text);
      assert.equal(row.props.onActivate, undefined, 'the row has no action');
      answerOverview(h); await settle(); h.fire();
      assert.equal(h.calls.filter(c => c.name === 'get_settings').length, 1, 'a starting module is not polled');
      h.callbacks.companion_guard({ ...overviewValues.get_version, modules });
      const started = h.render({ modules });
      assert.ok(findSection(started, 'TDP'), 'its section returns when the start has finished');
    });
  }
  await testOverview('entering a module invalidates old overview values without blocking fresh reads on return', async h => {
    findSection(h.render(), 'TDP').props.onClick(); h.render();
    h.respond('get_settings', overviewValues.get_settings); await settle();
    findSection(h.render(), 'TDP', 'onBack').props.onBack();
    const returned = h.render();
    assert.notEqual(findSection(returned, 'TDP').props.description, 'Custom · 23 W', 'a pre-edit read cannot become the returned page summary');
    await settle();
    h.respond('get_settings', { spl: 28000, sppt: 31000, fppt: 38000 }); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 W');
    answerOverview(h); await settle();
    assert.equal(findSection(h.render(), 'TDP').props.description, 'Custom · 28 W', 'the remainder of the older overview cannot overwrite newer power state');
  });
  if (overviewFailures.length) throw new AggregateError(overviewFailures, 'Overview loading regressions');
  console.log('OLED/RGB/remap/WiFi polling is bounded; stale reads and stopped watchers cannot overwrite current state; hidden dropdown choices still save.');
  console.log('Overview survives equivalent props, slow WiFi, visibility changes and remounts without losing confirmed summaries.');
});
