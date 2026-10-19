// The Desktop app's TDP slider model (desktop/native/Tdp.js) is a copy of the one in
// src/tdp.tsx. Run both on the same inputs so the two UIs can never send different limits.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const { assert, run, mount } = require('./frontend_harness.cjs');

run(async () => {
  const decky = mount('tdp.tsx', { append:
    'exports.__model = { offsetMax, normalise, absolute, fromAbsolute, simpleCaps, simpleTuning, makeTuningHandlers };' })
    .exports.__model;
  const source = fs.readFileSync(path.join(__dirname, '..', 'desktop/native/Tdp.js'), 'utf8').replace(/^\.pragma library$/m, '');
  const desktop = vm.runInNewContext(`${source}\n({offsetMax, normalise, absolute, fromAbsolute, simpleCaps, simpleTuning,
    withSpl, withSppt, withFppt})`);
  const plain = value => JSON.parse(JSON.stringify(value));
  const same = (what, a, b) => assert.deepEqual(plain(a), plain(b), what);

  const capsList = [{ spl: 35, sppt: 37, fppt: 45 }, { spl: 50, sppt: 50, fppt: 50 }, { spl: 30, sppt: 32, fppt: 40 }];
  let cases = 0;
  for (const caps of capsList) for (const minW of [5, 8]) {
    for (const unlocked of [false, true]) same('simpleCaps', decky.simpleCaps(caps, unlocked), desktop.simpleCaps(caps, unlocked));
    for (let spl = 0; spl <= 55; spl += 1) {
      same('offsetMax', decky.offsetMax(spl, caps), desktop.offsetMax(spl, caps));
      same('simpleTuning', decky.simpleTuning(spl, caps, minW), desktop.simpleTuning(spl, caps, minW));
      for (const spptOff of [-2, 0, 3, 10, 15, 20]) for (const fpptOff of [-1, 0, 2, 10, 15, 25]) {
        const t = { spl, spptOff, fpptOff };
        same('normalise', decky.normalise(t, caps, minW), desktop.normalise(t, caps, minW));
        same('absolute', decky.absolute(t), desktop.absolute(t));
        same('fromAbsolute', decky.fromAbsolute(spl, spl + spptOff, spl + fpptOff),
             desktop.fromAbsolute(spl, spl + spptOff, spl + fpptOff));
        for (const v of [-5, 0, 4, 12, 30, 60]) {
          let seen; const h = decky.makeTuningHandlers(t, next => { seen = next; }, caps, minW);
          h.onSpl(v); same('onSpl/withSpl', seen, desktop.withSpl(t, v, caps, minW));
          h.onSppt(v); same('onSppt/withSppt', seen, desktop.withSppt(t, v, caps));
          h.onFppt(v); same('onFppt/withFppt', seen, desktop.withFppt(t, v, caps));
          cases += 3;
        }
      }
    }
  }
  console.log(`Desktop TDP slider model matches Decky on ${cases} slider moves.`);

  // desktop/native/Fan.js copies the Fan Control status of src/fan.tsx (UX spec 3.2).
  const fanStatus = mount('fan.tsx', { append: 'exports.__status = fanStatus;' }).exports.__status;
  const fanSource = fs.readFileSync(path.join(__dirname, '..', 'desktop/native/Fan.js'), 'utf8').replace(/^\.pragma library$/m, '');
  const desktopStatus = vm.runInNewContext(`${fanSource}\nstatus`);
  let states = 0;
  const choices = [[true, false], [true, false], ['', 'EC read failed.'], [undefined, 'on', 'off', 'auto', 'manual'],
    [false, true], ['global', 'performance'], [undefined, true, false], [false, true]];
  const combos = choices.reduce((all, options) => all.flatMap(prefix => options.map(option => [...prefix, option])), [[]]);
  for (const [supported, enabled, error, telemetryKind, blocked, activeCurve, readingAutomatic, assignments] of combos) {
    const telemetry = telemetryKind === undefined ? undefined : { active: telemetryKind === 'on' || telemetryKind === 'manual',
      automatic: telemetryKind === 'auto' || telemetryKind === 'off' };
    const state = { supported, error, telemetry, active_curve: activeCurve,
      settings: { enabled, assign_tdp_presets: assignments },
      ownership: { blocked, state: blocked ? 'other' : 'companion', reason: blocked ? 'fancontrol holds /dev/port.' : '' },
      readings: readingAutomatic === undefined ? { valid: false } : { valid: true, automatic: readingAutomatic } };
    const decky = fanStatus(state, 0), desktop = desktopStatus(state);
    assert.equal(desktop.headline, decky.headline, JSON.stringify(state));
    // Deliberate differences: Decky shows the backend's reason on an unsupported console,
    // and the Desktop app adds the 30 s "still starting" hint itself (`starting`).
    if (supported) assert.equal(desktop.note || '', decky.detail || '', JSON.stringify(state));
    states++;
  }
  console.log(`Desktop Fan Control status matches Decky in ${states} backend states.`);
});
