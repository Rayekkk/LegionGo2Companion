# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import support
import main
import fan_backend as fan
import fan_guardian as guard
import module_control
from fan_policy import defaults, DEFAULT_CURVE, stop_timed_out
from safe_settings import SettingsManager


class FakeClient:
    timeout=None
    def __init__(self):
        self.socket=object();self.state=dict(kind='ready',active=False,automatic=True);self.calls=[];self.withdrawn=False
    def start(self): pass
    def configure(self,enabled,curve):
        if self.socket is None:raise RuntimeError('Closed fan client')
        self.calls.append((enabled,copy.deepcopy(curve)))
        self.state.update(kind='configured',active=enabled,automatic=not enabled)
        return self.snapshot()
    def snapshot(self): return copy.deepcopy(self.state)
    def close(self): self.socket=None;self.state.update(kind='closed',active=False,automatic=True)
    def check(self):
        if self.state.get('kind') in ('closed','disconnected'):raise RuntimeError(self.state.get('error') or 'Fan watchdog stopped.')
        return self.snapshot()
    def pump(self): return self.check()
    def withdraw(self): self.withdrawn=True


class EndingClient(FakeClient):
    """A guardian session that ends on its first telemetry poll with a classified outcome."""
    def __init__(self,outcome,reason):
        super().__init__();self.outcome,self.reason=outcome,reason
    def pump(self):
        self.state.update(kind='closed',active=False,automatic=True,outcome=self.outcome,error=self.reason)
        return self.check()


class DeadClient(FakeClient):
    """Owner and backup were both killed: no terminal packet, no receipt, the override is still set."""
    LOST=fan.Client.LOST
    def pump(self):
        self.state.update(kind='disconnected',active=False,automatic=False,error=self.LOST)
        return self.check()
    def close(self):
        self.socket=None;raise RuntimeError(self.state.get('error') or 'Firmware automatic readback is pending.')


class RefusedClient(FakeClient):
    def start(self):raise fan.Retry('Another Fan Control instance still owns EC recovery.')


class FanBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.storage=patch.object(fan,'settings',SettingsManager('fan_settings',self.tmp.name))
        import tdp_backend as tdp
        self.tdp_running=patch.object(tdp.Plugin,'_ready',True);self.tdp_running.start();self.addCleanup(self.tdp_running.stop)
        self.storage.start();self.owner_read=patch.object(fan,'read_automatic',return_value=dict(valid=True,rpm=1500,apu_temperature=45,age_seconds=0,automatic=True,error='',ownership=dict(blocked=False,state='firmware_auto',reason='')));self.owner_read.start();self.p=fan.Plugin();self.p._supported=True;self.p._client=FakeClient();self.supervisors=[]
    async def asyncTearDown(self):
        # Fan workers drain module-wide: stop every supervising plugin first.
        for plugin in self.supervisors:
            plugin._withdraw_lease()
            if plugin._task is not None:plugin._task.cancel()
        for plugin in self.supervisors:await plugin._unload()
        await self.p._unload();self.owner_read.stop();self.storage.stop();self.tmp.cleanup()

    async def test_first_install_custom_off_and_persisted_curves_readback(self):
        self.p._load();self.assertFalse(self.p._state['enabled'])
        c=copy.deepcopy(DEFAULT_CURVE);c[6][0]=90
        result=await self.p.save_curve('global',c,0)
        self.assertEqual(result['settings']['global_curve'],c)
        fresh=fan.Plugin();fresh._load();self.assertEqual(fresh._state,result['settings'])
        with patch.object(fan,'platform_check',return_value={}), \
                patch.object(fresh,'_start_client',new=AsyncMock()) as start:
            await fresh._main()
        start.assert_not_awaited()
        self.assertIsNone(fresh._task);self.assertIsNone(fresh._client)

    async def test_tdp_assignment_reparses_tdp_settings_only_after_they_change(self):
        import tdp_backend as tdp
        with patch.object(tdp,'settings',SettingsManager('tdp_settings',self.tmp.name)),patch.object(tdp,'_current_game_id',None):
            tdp._write_keys({tdp.SETTINGS_KEY_SETTINGS:dict(enabled=True,active_preset='silent')})
            real=tdp._read_valid_settings;reads=[]
            with patch.object(tdp,'_read_valid_settings',side_effect=lambda:(reads.append(1),real())[1]):
                cache={}
                self.assertEqual([fan.companion_preset(cache) for _ in range(3)],['silent']*3)
                self.assertEqual(len(reads),2,'settings and profiles parsed once, then served from the cache')
                tdp._write_keys({tdp.SETTINGS_KEY_SETTINGS:dict(enabled=True,active_preset='max')})
                self.assertEqual(fan.companion_preset(cache),'max')

    async def test_assignment_mode_is_cas_persisted_global_off_and_never_arms(self):
        self.p._load();self.p._state['presets']={'silent':[[t,4800] for t,_ in DEFAULT_CURVE]}
        self.p._save(self.p._state);before=copy.deepcopy(self.p._state)
        with patch.object(fan,'companion_preset',side_effect=AssertionError('Off must not query TDP')):
            self.assertEqual(self.p._selected(),(before['global_curve'],None))
        saved=await self.p.save_assignment_mode(True,before['revision'])
        self.assertFalse(saved['settings']['enabled']);self.assertEqual(saved['settings']['presets'],before['presets'])
        self.assertEqual(self.p._client.calls,[])
        with patch.object(fan,'companion_preset',return_value='silent'):
            self.assertEqual(self.p._selected(),(before['presets']['silent'],'silent'))
        with patch.object(fan,'companion_preset',return_value='max'):
            self.assertEqual(self.p._selected(),(before['global_curve'],None))
        for bad in (None,1,'false'):
            with self.assertRaises(ValueError):await self.p.save_assignment_mode(bad,saved['settings']['revision'])
        with self.assertRaises(ValueError):await self.p.save_assignment_mode(False,before['revision'])
        saved=await self.p.save_assignment_mode(False,saved['settings']['revision'])
        fresh=fan.Plugin();fresh._load();self.assertEqual(fresh._state,saved['settings'])
        self.assertEqual(fresh._state['presets'],before['presets'])
        with patch.object(fan,'companion_preset',side_effect=AssertionError('Off must not query TDP')):
            self.assertEqual(fresh._selected(),(before['global_curve'],None))

    async def test_live_assignment_toggle_applies_global_and_fault_disables_and_releases(self):
        self.p._load();self.p._state.update(enabled=True,assign_tdp_presets=True,presets={'silent':[[t,4800] for t,_ in DEFAULT_CURVE]})
        self.p._task=asyncio.create_task(asyncio.sleep(100))
        saved=await self.p.save_assignment_mode(False,0)
        self.assertTrue(saved['settings']['enabled']);self.assertEqual(self.p._client.calls[-1],(True,DEFAULT_CURVE))
        with patch.object(self.p._client,'configure',side_effect=RuntimeError('Switch failed')),patch.object(fan,'companion_preset',return_value='silent'):
            with self.assertRaisesRegex(RuntimeError,'Switch failed'):await self.p.save_assignment_mode(True,saved['settings']['revision'])
        self.assertFalse(self.p._state['enabled']);self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIsNone(self.p._task);self.assertIsNone(self.p._client.socket);self.assertTrue(self.p._client.snapshot()['automatic'])

    async def test_template_catalog_is_detached_and_saves_as_an_ordinary_curve(self):
        state=await self.p.get_state();catalog=copy.deepcopy(state['templates'])
        state['templates']['silent'][0][1]=4800
        self.assertEqual((await self.p.get_state())['templates'],catalog)
        curve=copy.deepcopy(catalog['silent']);curve[2][1]=1501
        saved=await self.p.save_curve('global',curve,self.p._state['revision'])
        self.assertEqual(saved['settings']['global_curve'],curve);self.assertFalse(saved['settings']['enabled'])
        self.assertFalse(any('template' in key for key in saved['settings']))
        fresh=fan.Plugin();fresh._load();self.assertEqual(fresh._state,saved['settings'])

    async def supervised(self,clients,outcome=None,backoff=(0,)):
        """Fresh plugin whose saved ON intent re-arms through scripted guardian sessions."""
        fan.settings.setSetting('state',dict(defaults(),enabled=True,revision=8));fan.settings.commit()
        fresh=fan.Plugin();fresh.BACKOFF_S=backoff;fresh.POLL_S=0
        self.supervisors.append(fresh)
        receipt=None if outcome is None else dict(outcome=outcome,reason='Fan did not reach a stable running speed.')
        made=[]
        def client():
            made.append(clients.pop(0) if clients else FakeClient());return made[-1]
        for item in (patch.object(fan,'platform_check',return_value={}),
                     patch.object(fan,'last_session',return_value=receipt or {}),
                     patch.object(fan,'Client',side_effect=client)):
            item.start();self.addCleanup(item.stop)
        await fresh._main()
        return fresh,made

    async def until(self,condition,timeout=3):
        deadline=time.monotonic()+timeout
        while not condition():
            if time.monotonic()>deadline:self.fail('Condition not reached')
            await asyncio.sleep(.01)

    async def idle(self,plugin):
        # With POLL_S=0 the monitor retakes the lock at once; a polled locked() can miss the gap.
        # asyncio.Lock is FIFO, so waiting for it is guaranteed to get in at the next release.
        async with plugin._mutation:pass

    async def test_saved_enable_rearms_at_startup_unless_this_boot_ended_in_a_fault(self):
        for outcome in (None,'pause','retry'):
            with self.subTest(outcome=outcome):
                fresh,made=await self.supervised([],outcome)
                self.assertIn('Resuming',(await fresh.get_state())['error'],'no control is claimed before readiness')
                await self.until(lambda:made and made[-1].calls);await self.idle(fresh)
                self.assertEqual(made[-1].calls,[(True,DEFAULT_CURVE)])
                self.assertEqual(fresh._error,'')
                self.assertEqual(fan.settings.getSetting('state')['revision'],8,'re-arm never rewrites the saved intent')
                await fresh._unload()
                self.assertTrue(made[-1].withdrawn);self.assertIsNone(made[-1].socket)
                self.assertEqual(fan.settings.getSetting('state')['revision'],8,'unload never writes enabled')
        fresh,made=await self.supervised([],'fault')
        self.assertEqual(made,[]);self.assertIsNone(fresh._task)
        self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIn('stable running speed',fresh._error)

    async def test_pause_rearms_without_limit_and_keeps_saved_intent(self):
        clients=[EndingClient('pause','Suspend requested; firmware automatic restored before sleep.') for _ in range(6)]
        fresh,made=await self.supervised(clients)
        await self.until(lambda:len(made)==7 and made[-1].calls)
        self.assertTrue(all(c.socket is None for c in made[:-1]),'each ended session was closed first')
        self.assertTrue(fresh._state['enabled']);self.assertEqual(fan.settings.getSetting('state')['revision'],8)

    async def test_retry_rearms_three_times_in_ten_minutes_then_latches_off_with_reason(self):
        clients=[EndingClient('retry','Stale/slow APU temperature acquisition.') for _ in range(4)]
        fresh,made=await self.supervised(clients)
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertEqual(len(made),4)
        stored=fan.settings.getSetting('state');self.assertFalse(stored['enabled']);self.assertEqual(stored['revision'],9)
        self.assertIn('kept failing',fresh._error);self.assertIn('Stale/slow',fresh._error)

    async def test_retries_outside_the_window_do_not_accumulate(self):
        fresh,made=await self.supervised([EndingClient('retry','EC owner died') for _ in range(5)])
        fresh.RETRY_WINDOW_S=0  # Every earlier retry is already outside the window.
        await self.until(lambda:len(made)==6 and made[-1].calls)
        self.assertTrue(fan.settings.getSetting('state')['enabled'])

    async def test_fault_latches_off_immediately_and_a_local_timeout_is_a_retry(self):
        fresh,made=await self.supervised([EndingClient('fault','Fan override changed outside its owner.')])
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertEqual(len(made),1);self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIn('outside its owner',fresh._error)
        slow=FakeClient();slow.configure=lambda *_:(_ for _ in ()).throw(TimeoutError('Fan configuration readback timed out; control withdrawn.'))
        fresh,made=await self.supervised([slow])
        await self.until(lambda:len(made)==2 and made[-1].calls)
        self.assertTrue(fan.settings.getSetting('state')['enabled'])

    async def test_user_save_readback_timeout_counts_as_retry_not_pause(self):
        class TimingOut(FakeClient):
            def configure(self,enabled,curve):
                if not self.calls:return super().configure(enabled,curve)
                # Client.configure on timeout: close the channel, then raise.
                self.timeout=TimeoutError('Fan configuration readback timed out; control withdrawn.')
                self.socket=None;self.state.update(kind='disconnected',active=False,automatic=False,error=fan.Client.LOST)
                raise self.timeout
            def close(self):
                # The guardian saw EOF: its receipt says pause.
                self.socket=None;self.state.update(kind='closed',active=False,automatic=True,outcome='pause',error='Controller disconnected')
        fresh,made=await self.supervised([TimingOut()])
        await self.until(lambda:made and made[-1].calls and fresh._live());await self.idle(fresh)
        curve=copy.deepcopy(fresh._state['global_curve']);curve[1][1]=1600
        with patch.object(fan,'diagnostic_event') as event:
            with self.assertRaises(TimeoutError):await fresh.save_curve('global',curve,fresh._state['revision'])
            await self.until(lambda:len(made)==2 and made[-1].calls)
        ended=[c.kwargs for c in event.call_args_list if c.args[1:]==('session_ended',)]
        self.assertEqual([(e['outcome'],e['count']) for e in ended],[('retry',1)])
        self.assertIn('timed out',ended[0]['reason'])

    async def test_failed_start_is_classified_by_the_guardian(self):
        class Failing(FakeClient):
            def __init__(self,error):super().__init__();self.error=error
            def start(self):raise self.error
        from fan_sleep import Pause,Retry
        fresh,made=await self.supervised([Failing(Pause('Sleep is preparing; fan control remains off.')),
                                          Failing(Retry('Another Fan Control instance still owns EC recovery.'))])
        await self.until(lambda:len(made)==3 and made[-1].calls)
        self.assertTrue(fan.settings.getSetting('state')['enabled'])
        fresh,made=await self.supervised([Failing(RuntimeError('This hardware/BIOS/EC variant has not been validated for Fan Control.'))])
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertFalse(fan.settings.getSetting('state')['enabled']);self.assertIn('validated',fresh._error)

    async def test_unconfirmed_release_runs_one_release_only_session_before_latching_off(self):
        fresh,made=await self.supervised([DeadClient()])
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertEqual(len(made),2,'exactly one release-only session, no re-arm')
        self.assertEqual(made[1].calls,[]);self.assertIsNone(made[1].socket,'started, never configured, closed')
        self.assertIsNone(fresh._client)
        self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIn('restored by a release-only session',fresh._error)
        # The release cannot run either: today's fault latch, with the reason surfaced.
        fresh,made=await self.supervised([DeadClient(),RefusedClient()])
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertEqual(len(made),2);self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIn('release failed',fresh._error);self.assertIn('still owns EC recovery',fresh._error)
        fresh._client=None  # Its release stays unconfirmed; unload would report that again.

    async def test_startup_with_fan_off_releases_a_leftover_override_once(self):
        fan.settings.setSetting('state',dict(defaults(),revision=8));fan.settings.commit()
        made=[]
        def client():
            made.append(clients.pop(0) if clients else FakeClient());return made[-1]
        orphan=dict(valid=True,rpm=0,automatic=False,error='',ownership=dict(blocked=False,state='external_override',override=256,reason=''))
        with patch.object(fan,'platform_check',return_value={}),patch.object(fan,'Client',side_effect=client):
            for observed,clients,count,error in ((orphan,[],1,''),(fan.read_automatic.return_value,[],0,''),
                                                  (orphan,[RefusedClient()],1,'release failed')):
                with self.subTest(state=observed['ownership']['state'],error=error),patch.object(fan,'read_automatic',return_value=observed):
                    made.clear();fresh=fan.Plugin();self.supervisors.append(fresh)
                    await fresh._main()
                    self.assertEqual(len(made),count);self.assertTrue(all(c.calls==[] for c in made))
                    self.assertIsNone(fresh._task);self.assertIsNone(fresh._client)
                    self.assertIn(error,fresh._error)
                    self.assertEqual(fan.settings.getSetting('state'),dict(defaults(),revision=8),'intent stays OFF, unwritten')

    async def test_user_disable_during_backoff_stops_rearm_and_unload_during_backoff_writes_nothing(self):
        fresh,made=await self.supervised([],backoff=(30,))
        await asyncio.sleep(.05)
        state=await fresh.set_enabled(False)
        self.assertFalse(state['settings']['enabled']);self.assertIsNone(fresh._task);self.assertEqual(made,[])
        self.assertEqual(state['error'],'','OFF never claims that Fan Control is resuming')
        class Sleeping(FakeClient):
            def start(self):raise fan.Pause('Sleep is preparing; fan control remains off.')
        fresh,made=await self.supervised([Sleeping()],backoff=(0,30))  # Paused, next re-arm in 30 s.
        await self.until(lambda:'paused' in fresh._error and not fresh._mutation.locked())
        self.assertEqual((await fresh.set_enabled(False))['error'],'')
        fresh,made=await self.supervised([EndingClient('fault','Fan override changed outside its owner.')])
        await self.until(lambda:fresh._task is None and not fresh._mutation.locked())
        self.assertIn('outside its owner',(await fresh.set_enabled(False))['error'],'a fault reason stays until re-enable')
        fresh,made=await self.supervised([],backoff=(30,))
        await fresh._unload()
        self.assertEqual(made,[]);self.assertEqual(fan.settings.getSetting('state')['revision'],8)

    async def test_explicit_enable_during_pending_rearm_starts_fresh_instead_of_disabling(self):
        fresh,made=await self.supervised([],backoff=(30,))
        state=await fresh.set_enabled(True)
        self.assertTrue(state['settings']['enabled']);self.assertEqual(len(made),1)
        self.assertEqual(made[0].calls,[(True,DEFAULT_CURVE)]);self.assertTrue(fresh._live())

    async def test_explicit_preset_mapping_missing_mapping_and_game_ac_observation(self):
        c=[[t,4800] for t,_ in DEFAULT_CURVE]
        self.p._state['presets']={'silent':c};self.p._state['assign_tdp_presets']=True
        with patch.object(fan,'companion_preset',return_value='silent'):
            await self.p._apply()
        self.assertEqual(self.p._client.calls[-1][1],c)
        with patch.object(fan,'companion_preset',return_value='balanced'):
            await self.p._apply()
        self.assertEqual(self.p._client.calls[-1][1],DEFAULT_CURVE)
        self.assertIsNone(self.p._preset)
        import tdp_backend as tdp
        with patch.object(tdp,'_load_settings',return_value={'enabled':True,'active_preset':'max'}), \
                patch.object(tdp,'_current_game_id','42'), \
                patch.object(tdp,'_load_profiles',return_value={'42':{'preset':'silent','ac_preset':'balanced','ac_separate':True}}), \
                patch.object(tdp,'_get_ac_online',return_value=True):
            self.assertEqual(fan.companion_preset(),'balanced')

    async def test_stale_concurrent_save_rejected_instead_of_overwriting(self):
        results=await asyncio.gather(self.p.save_curve('silent',DEFAULT_CURVE,0),
                                     self.p.save_curve('balanced',DEFAULT_CURVE,0),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
        self.assertEqual(len(self.p._state['presets']),1)
        revision=self.p._state['revision']
        scope=next(iter(self.p._state['presets']))
        await self.p.save_curve(scope,None,revision)
        self.assertEqual(self.p._state['presets'],{})

    async def test_custom_curve_is_independent_durable_and_global_fallback_is_preserved(self):
        self.p._load();self.p._state['assign_tdp_presets']=True
        curve=copy.deepcopy(DEFAULT_CURVE);curve[2][1]=1801
        maximum=[[t,4800] for t,_ in DEFAULT_CURVE]
        first=await self.p.save_curve('max',maximum,0)
        saved=await self.p.save_curve('custom',curve,first['settings']['revision'])
        self.assertIn('custom',saved['presets'])
        fresh=fan.Plugin();fresh._load();self.assertEqual(fresh._state,saved['settings'])
        with patch.object(fan,'companion_preset',return_value='custom'):
            await self.p._apply()
        self.assertEqual(self.p._preset,'custom');self.assertEqual(self.p._client.calls[-1][1],curve)
        removed=await self.p.save_curve('custom',None,saved['settings']['revision'])
        self.assertEqual(removed['settings']['presets'],{'max':maximum})
        with patch.object(fan,'companion_preset',return_value='custom'):
            await self.p._apply()
        self.assertIsNone(self.p._preset);self.assertEqual(self.p._client.calls[-1][1],DEFAULT_CURVE)

    def test_custom_companion_intent_global_game_and_separate_ac_are_observed_without_writes(self):
        import tdp_backend as tdp
        with patch.object(tdp,'_load_settings',return_value={'enabled':True,'active_preset':'custom'}), \
                patch.object(tdp,'_current_game_id',None),patch.object(tdp,'_load_profiles',return_value={}):
            self.assertEqual(fan.companion_preset(),'custom')
        for battery,ac in [('custom','max'),('max','custom')]:
          for online,expected in [(False,battery),(True,ac)]:
            with self.subTest(battery=battery,ac=ac,online=online), \
                    patch.object(tdp,'_load_settings',return_value={'enabled':True,'active_preset':'balanced'}), \
                    patch.object(tdp,'_current_game_id','42'), \
                    patch.object(tdp,'_load_profiles',return_value={'42':{'preset':battery,'ac_preset':ac,'ac_separate':True}}), \
                    patch.object(tdp,'_get_ac_online',return_value=online):
                self.assertEqual(fan.companion_preset(),expected)
        with patch.object(tdp,'_load_settings',return_value={'enabled':False,'active_preset':'custom'}):
            self.assertIsNone(fan.companion_preset())

    def test_tdp_module_turned_off_in_manage_modules_means_the_global_curve(self):
        import tdp_backend as tdp
        # Withdrawal keeps TDP intent and the last game id; no Companion preset is active.
        with patch.object(tdp.Plugin,'_ready',False), \
                patch.object(tdp,'_load_settings',return_value={'enabled':True,'active_preset':'max'}), \
                patch.object(tdp,'_current_game_id','42'), \
                patch.object(tdp,'_load_profiles',return_value={'42':{'preset':'silent'}}):
            self.assertIsNone(fan.companion_preset())

    async def test_disable_disk_failure_never_rearms_previous_enabled_intent(self):
        self.p._state['enabled']=True
        with patch.object(self.p,'_save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): await self.p.set_enabled(False)
        self.assertFalse(self.p._state['enabled'])
        self.assertIsNone(self.p._client.socket)
        self.assertTrue(all(enabled is False for enabled,_ in self.p._client.calls))

    async def test_repeated_disable_and_module_off_after_custom_disable_are_idempotent(self):
        await self.p.set_enabled(False)
        self.assertIsNone(self.p._client.socket)
        await self.p.set_enabled(False)
        self.assertFalse(self.p._state['enabled'])
        self.assertTrue(self.p._client.snapshot()['automatic'])
        self.assertEqual(len(self.p._client.calls),1)

    async def test_disable_after_lost_command_still_confirms_auto_and_persists_off(self):
        self.p._load();self.p._state['enabled']=True
        self.p._client.state.update(active=True,automatic=False)
        with patch.object(self.p._client,'configure',side_effect=RuntimeError('Channel lost')):
            result=await self.p.set_enabled(False)
        self.assertFalse(result['settings']['enabled'])
        self.assertTrue(result['telemetry']['automatic'])
        self.assertIsNone(self.p._client.socket)
        self.assertFalse(fan.settings.getSetting('state')['enabled'])
        self.assertIn('Channel lost',result['error'])

    async def test_disable_persists_off_even_when_the_leftover_release_fails(self):
        fan.settings.setSetting('state',dict(defaults(),enabled=True));fan.settings.commit()
        self.p._load();self.assertTrue(self.p._state['enabled'])
        self.p._client=DeadClient();self.p._client.state.update(active=True,automatic=False)
        busy=fan.Retry('Another Fan Control instance still owns EC recovery.')
        try:
            with patch.object(self.p._client,'configure',side_effect=RuntimeError('Channel lost')), \
                    patch.object(fan,'release_orphan',side_effect=busy):
                with self.assertRaisesRegex(RuntimeError,'still owns EC recovery'):
                    await self.p.set_enabled(False)
        finally:
            self.p._client=None  # DeadClient.close would fail again in teardown
        self.assertIn('still owns EC recovery',self.p._error)
        # Otherwise the next Decky start resumes Fan Control the user turned off.
        self.assertFalse(fan.settings.getSetting('state')['enabled'])

    async def test_corrupt_or_newer_schema_is_not_silently_enabled(self):
        fan.settings.setSetting('state',dict(defaults(),schema=99,enabled=True));fan.settings.commit()
        self.p._load();self.assertFalse(self.p._state['enabled']);self.assertIn('schema',self.p._error)
        with patch.object(fan,'platform_check',return_value={}), \
                patch.object(self.p,'_start_client',new=AsyncMock()) as start:
            await self.p._main()
        start.assert_not_awaited()
        self.assertEqual(fan.settings.getSetting('state')['schema'],99)

    async def test_invalid_schema_disable_preserves_original_and_enable_needs_explicit_curve_save(self):
        raw=dict(defaults(),schema=99,enabled=True)
        fan.settings.setSetting('state',raw);fan.settings.commit();self.p._load()
        with self.assertRaisesRegex(RuntimeError,'save a valid curve'):await self.p.set_enabled(True)
        await self.p.set_enabled(False)
        self.assertEqual(fan.settings.getSetting('state'),raw)
        self.assertTrue(self.p._client.snapshot()['automatic'])
        repaired=await self.p.save_curve('global',DEFAULT_CURVE,self.p._state['revision'])
        self.assertEqual(repaired['error'],'')
        self.assertFalse(repaired['needs_curve_review'])
        self.assertEqual(fan.settings.getSetting('state')['schema'],4)
        self.assertFalse(fan.settings.getSetting('state')['enabled'])

    async def test_module_disable_releases_fan_even_when_module_settings_cannot_save(self):
        p=main.Plugin();p._fan.set_enabled=AsyncMock()
        with patch.object(main,'_installed_standalone_plugins',return_value=[]), \
                patch.object(p,'_save_modules',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): await p.modules_set_enabled('fan',False)
        p._fan.set_enabled.assert_awaited_once_with(False)

    async def test_fan_rpcs_are_namespaced_and_module_guarded(self):
        p=main.Plugin();p._module_state['fan']={'enabled':False}
        with patch.object(main,'_installed_standalone_plugins',return_value=[]):
            for name,args in [('fan_get_state',()),('fan_set_enabled',(True,)),('fan_save_curve',('global',DEFAULT_CURVE,0)),('fan_save_unit',('percent',0)),('fan_save_assignment_mode',(True,0))]:
                with self.assertRaisesRegex(RuntimeError,'disabled'): await getattr(p,name)(*args)

    async def test_real_manage_modules_withdrawal_and_reenable_keep_curves_custom_off(self):
        curve=copy.deepcopy(DEFAULT_CURVE);curve[6][0]=90
        await self.p.save_curve('global',curve,0)
        p=main.Plugin();p._fan=self.p
        with patch.object(main,'_installed_standalone_plugins',return_value=[]), \
                patch.object(fan,'platform_check',return_value={}), \
                patch.object(self.p,'_start_client',new=AsyncMock()) as start:
            disabled=await p.modules_set_enabled('fan',False)
            self.assertFalse(disabled['fan']['enabled'])
            self.assertFalse(disabled['fan']['pending'])
            self.assertEqual(disabled['fan']['error'],'')
            enabled=await p.modules_set_enabled('fan',True)
            self.assertTrue(enabled['fan']['ready'])
            start.assert_not_awaited()
        self.assertEqual(self.p._state['global_curve'],curve)
        self.assertFalse(self.p._state['enabled'])
        await p._unload()

    async def test_pending_fan_withdrawal_before_main_preserves_original_saved_curves(self):
        curve=copy.deepcopy(DEFAULT_CURVE);curve[6][0]=90
        self.p._save(dict(defaults(),enabled=True,global_curve=curve,presets={'silent':curve}))
        fresh=fan.Plugin()
        with patch.object(fresh,'_start_client',new=AsyncMock()) as start:
            self.assertEqual(await module_control.withdraw('fan',fresh),'')
            start.assert_not_awaited()
        self.assertEqual(fresh._state['global_curve'],curve)
        self.assertEqual(fresh._state['presets']['silent'],curve)
        self.assertFalse(fresh._state['enabled'])
        self.assertIsNone(fresh._client)

    async def test_early_disable_before_main_preserves_saved_curves_and_future_schema(self):
        curve=copy.deepcopy(DEFAULT_CURVE);curve[6][0]=90
        for schema in (4,99):
            raw=dict(defaults(),enabled=True,revision=7,global_curve=curve,schema=schema)
            fan.settings.setSetting('state',raw);fan.settings.commit()
            fresh=fan.Plugin()
            await fresh.set_enabled(False)
            stored=fan.settings.getSetting('state')
            self.assertEqual(stored['global_curve'],curve)
            self.assertEqual(stored['schema'],schema)
            if schema==4:
                self.assertFalse(stored['enabled']);self.assertEqual(stored['revision'],8)
            else:
                self.assertEqual(stored,raw)
            self.assertIsNone(fresh._client)

    async def test_early_curve_save_uses_durable_revision_instead_of_constructor_defaults(self):
        raw=dict(defaults(),revision=7)
        fan.settings.setSetting('state',raw);fan.settings.commit()
        fresh=fan.Plugin()
        with self.assertRaisesRegex(ValueError,'changed elsewhere'):
            await fresh.save_curve('global',DEFAULT_CURVE,0)
        self.assertEqual(fan.settings.getSetting('state'),raw)
        state=await fresh.get_state();self.assertEqual(state['settings']['revision'],7)


    async def test_cancelled_settings_write_holds_revision_lock_until_commit_finishes(self):
        self.p._load();entered=threading.Event();release=threading.Event()
        original=self.p._save
        def delayed(value):
            entered.set();release.wait(2);original(value)
        with patch.object(self.p,'_save',side_effect=delayed):
            request=asyncio.create_task(self.p.save_unit('percent',0))
            while not entered.is_set():await asyncio.sleep(.005)
            request.cancel();await asyncio.sleep(.01)
            self.assertTrue(self.p._mutation.locked())
            later=asyncio.create_task(self.p.save_unit('rpm',0))
            try:
                await asyncio.sleep(.01);self.assertFalse(later.done())
            finally:release.set()
            with self.assertRaises(asyncio.CancelledError):await request
            with self.assertRaisesRegex(ValueError,'changed elsewhere'):await later
        self.assertEqual(self.p._state['display_unit'],'percent')
        self.assertEqual(self.p._state['revision'],1)


class ClientRecoveryTests(unittest.TestCase):
    def test_snapshot_remains_responsive_while_an_io_thread_waits_for_recovery(self):
        client=fan.Client()
        client.state=dict(active=False,automatic=False,error='Recovery pending')
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        states=[];errors=[]
        def recovering():
            with client.lock:
                entered.set();release.wait()
        def read_snapshot():
            try:states.append(client.snapshot())
            except BaseException as exc:errors.append(exc)
            finally:finished.set()
        worker=threading.Thread(target=recovering);worker.start()
        reader=threading.Thread(target=read_snapshot)
        try:
            self.assertTrue(entered.wait(5))
            reader.start()
            # Prove independence from the I/O lock, not a 100 ms scheduling SLA.
            # Recovery cannot finish until cleanup explicitly releases it.
            self.assertTrue(finished.wait(5),'A status read must not wait for EC recovery I/O')
            self.assertEqual(errors,[])
            self.assertEqual(states[0]['error'],'Recovery pending')
            self.assertFalse(release.is_set())
        finally:
            release.set();worker.join(5)
            if reader.ident is not None:reader.join(5)
        self.assertFalse(worker.is_alive());self.assertFalse(reader.is_alive())

    def test_backup_automatic_readback_clears_old_custom_stop_verification(self):
        client=fan.Client()
        client.state=dict(session='session-own',active=True,automatic=False,target=0,stop_verified=True)
        client.last_intent_at=10
        outcome=dict(session='session-own',monotonic=11,active=False,automatic=True,
                     state=dict(override=0,automatic_reads=3,rpm=2500,effective=2600))
        with patch.object(fan.pathlib.Path,'read_text',return_value=json.dumps(outcome)), \
                patch.object(fan.time,'monotonic',return_value=11):
            client.close()
        self.assertTrue(client.state['automatic'])
        self.assertFalse(client.state['active'])
        self.assertFalse(client.state['stop_verified'])
        self.assertEqual(client.state['rpm'],2500)


class PassiveTelemetryTests(unittest.TestCase):
    def test_foreign_raw_port_check_runs_before_any_competing_port_open(self):
        with patch.object(fan,'platform_check'),patch.object(fan,'secure_root',return_value=71), \
                patch.object(fan,'check_owners',side_effect=RuntimeError('Another app is using the fan controller.')), \
                patch.object(fan,'Ports') as ports,patch.object(fan.os,'close') as close:
            reading=fan.read_automatic()
        self.assertFalse(reading['valid']);self.assertTrue(reading['ownership']['blocked'])
        self.assertNotIn('override',reading['ownership'],'Unknown ownership is not an invented automatic readback')
        ports.assert_not_called();close.assert_called_once_with(71)

    def test_passive_reader_closes_transport_before_releasing_owner_and_never_sets_target(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        events=[];sensor=Mock();port=Mock();ec=Mock()
        sensor.read.return_value={'k10temp':43.5,'amdgpu':45}
        sensor.close.side_effect=lambda:events.append('sensor closed')
        port.close.side_effect=lambda:events.append('port closed')
        ec.transaction.return_value=nullcontext();ec.status.return_value=dict(rpm=1733,effective=1800,override=0)
        with patch.object(fan,'platform_check'),patch.object(fan,'secure_root',return_value=71), \
                patch.object(fan,'check_owners'),patch.object(fan,'Sensors',return_value=sensor), \
                patch.object(fan,'Ports',return_value=port),patch.object(fan,'EC',return_value=ec), \
                patch.object(fan.os,'close',side_effect=lambda fd:events.append('owner closed')):
            reading=fan.read_automatic()
        self.assertTrue(reading['valid']);self.assertEqual(reading['rpm'],1733)
        self.assertEqual(reading['apu_temperature'],45);self.assertTrue(reading['automatic'])
        self.assertCountEqual(events,['port closed','sensor closed','owner closed'])
        self.assertLess(events.index('port closed'),events.index('owner closed'),
                        'Raw EC access must close before another owner can acquire it')
        ec.set_target.assert_not_called();ec.release.assert_not_called()

    def test_orphaned_override_with_clear_ownership_does_not_block_enable(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        sensor=Mock();sensor.read.return_value={'k10temp':45,'amdgpu':44};ec=Mock()
        ec.transaction.return_value=nullcontext();ec.status.return_value=dict(rpm=1800,effective=1800,override=1800)
        with patch.object(fan,'platform_check'),patch.object(fan,'secure_root',return_value=71), \
                patch.object(fan,'check_owners'),patch.object(fan,'Sensors',return_value=sensor), \
                patch.object(fan,'Ports'),patch.object(fan,'EC',return_value=ec),patch.object(fan.os,'close'):
            reading=fan.read_automatic()
        self.assertFalse(reading['automatic']);self.assertEqual(reading['ownership']['state'],'external_override')
        self.assertFalse(reading['ownership']['blocked'],'session start releases an orphan after the same checks')
        ec.set_target.assert_not_called();ec.release.assert_not_called()

    def test_sensor_failure_and_foreign_owner_fail_closed_without_cached_numbers(self):
        from unittest.mock import Mock
        sensor=Mock();sensor.read.side_effect=OSError('sensor lost')
        with patch.object(fan,'platform_check'),patch.object(fan,'secure_root',return_value=71), \
                patch.object(fan,'check_owners'),patch.object(fan,'Sensors',return_value=sensor), \
                patch.object(fan,'Ports') as ports,patch.object(fan.os,'close'):
            result=fan.read_automatic()
        self.assertFalse(result['valid']);self.assertNotIn('rpm',result);ports.assert_not_called();sensor.close.assert_called_once()
        with patch.object(fan,'platform_check'),patch.object(fan,'secure_root',side_effect=RuntimeError('Recovery owns EC')), \
                patch.object(fan,'Ports') as ports:
            result=fan.read_automatic()
        self.assertFalse(result['valid']);self.assertIn('Recovery',result['error']);ports.assert_not_called()

    def test_stale_future_invalid_samples_do_not_present_target_or_old_tach(self):
        sample=dict(sampled_at=10,rpm=1555,target=4800,override=4800,temperatures={'k10temp':45,'amdgpu':43})
        with patch.object(fan.time,'monotonic',return_value=11):
            reading=fan.measured_readings(sample,'controller')
        self.assertEqual(reading['rpm'],1555);self.assertEqual(reading['apu_temperature'],45)
        for now in (9,13):
            with patch.object(fan.time,'monotonic',return_value=now):
                reading=fan.measured_readings(sample,'controller')
            self.assertFalse(reading['valid']);self.assertNotIn('rpm',reading)
        with patch.object(fan.time,'monotonic',return_value=11):
            for change in ({'rpm':True},{'temperatures':{'k10temp':45}}, {'temperatures':{'k10temp':float('nan'),'amdgpu':45}}):
                self.assertFalse(fan.measured_readings(dict(sample,**change),'controller')['valid'])


class TelemetryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.storage=patch.object(fan,'settings',SettingsManager('fan_settings',self.tmp.name));self.storage.start()
        self.p=fan.Plugin();self.p._supported=True;self.p._load()

    async def asyncTearDown(self):
        await self.p._unload();self.storage.stop();self.tmp.cleanup()

    async def test_foreign_or_unknown_ownership_blocks_enable_before_worker_and_durable_intent(self):
        for owner in ('foreign_port','external_override','unavailable'):
            old=copy.deepcopy(self.p._state)
            with patch.object(fan,'read_automatic',return_value=dict(valid=False,ownership=dict(blocked=True,state=owner,reason='Occupied'))), \
                    patch.object(self.p,'_start_client',new=AsyncMock()) as start:
                with self.assertRaisesRegex(RuntimeError,'Occupied'):await self.p.set_enabled(True)
            self.assertEqual(self.p._state,old);start.assert_not_awaited();self.assertIsNone(self.p._client)

    async def test_units_are_durable_cas_metadata_with_no_controller_start_or_curve_mutation(self):
        before=copy.deepcopy(self.p._state)
        with patch.object(fan,'read_automatic',return_value=dict(valid=False,error='foreign')), \
                patch.object(self.p,'_start_client',new=AsyncMock()) as start:
            value=await self.p.save_unit('percent',before['revision'])
            self.assertEqual(value['settings']['display_unit'],'percent');start.assert_not_awaited()
            self.assertEqual(value['settings']['global_curve'],before['global_curve'])
            self.assertFalse(value['settings']['enabled'])
            with self.assertRaises(ValueError):await self.p.save_unit('rpm',before['revision'])
            with self.assertRaises(ValueError):await self.p.save_unit('invalid',value['settings']['revision'])
        fresh=fan.Plugin();fresh._load();self.assertEqual(fresh._state['display_unit'],'percent')

    async def test_pre_release_schema_fails_closed_without_rewriting_or_starting(self):
        old=dict(defaults(),schema=3,enabled=True,revision=5)
        fan.settings.setSetting('state',old);fan.settings.commit()
        fresh=fan.Plugin();fresh._load()
        self.assertFalse(fresh._settings_valid);self.assertFalse(fresh._state['enabled'])
        self.assertIsNone(fresh._client);self.assertEqual(fan.settings.getSetting('state'),old)

    async def test_stale_active_snapshot_does_not_claim_verified_ec_ownership(self):
        self.p._load();self.p._state['enabled']=True;self.p._client=FakeClient()
        self.p._client.state.update(active=True,automatic=False,rpm=3000,override=3000,
            sampled_at=time.monotonic()-3,temperatures={'k10temp':45,'amdgpu':44})
        with patch.object(fan,'read_automatic',side_effect=AssertionError('Must not race the active owner')):
            state=await self.p.get_state()
        self.assertFalse(state['readings']['valid'])
        self.assertTrue(state['ownership']['blocked'])
        self.assertEqual(state['ownership']['state'],'unavailable')
        self.assertNotIn('override',state['ownership'])

    async def test_schema_4_with_dev_archive_keys_loads_and_next_save_drops_them(self):
        current=dict(defaults(),enabled=False,revision=9,display_unit='percent')
        fan.settings.setSetting('state',dict(current,**dict(curve_archive_v2={'global':fan.TEMPLATES['silent'],'presets':{}},
             legacy_curves={'global':[[t,1500] for t in range(50,101,5)][:10],'presets':{}})))
        fan.settings.commit()
        fresh=fan.Plugin();fresh._load()
        self.assertTrue(fresh._settings_valid);self.assertEqual(fresh._state,current)
        with self.assertRaises(ValueError):await fresh.save_curve('global',DEFAULT_CURVE,8)
        saved=await fresh.save_curve('custom',DEFAULT_CURVE,9)
        self.assertEqual(fan.settings.getSetting('state'),saved['settings'])
        self.assertNotIn('curve_archive_v2',saved['settings']);self.assertNotIn('legacy_curves',saved['settings'])
        self.assertIsNone(fresh._client)

    async def test_custom_uses_fresh_worker_but_auto_reads_again_instead_of_closed_snapshot(self):
        self.p._client=FakeClient();self.p._state['enabled']=True
        self.p._client.state=dict(active=True,automatic=False,sampled_at=time.monotonic(),rpm=1544,override=4800,temperatures={'k10temp':44,'amdgpu':46})
        with patch.object(fan,'read_automatic') as passive:
            state=await self.p.get_state();passive.assert_not_called()
        self.assertEqual(state['readings']['rpm'],1544)
        self.p._client.state.update(active=False,automatic=True,kind='closed')
        self.p._state['enabled']=False
        with patch.object(fan,'read_automatic',return_value=dict(valid=True,rpm=1900,apu_temperature=41,age_seconds=0,automatic=True,error='')) as passive:
            state=await self.p.get_state();passive.assert_called_once()
        self.assertEqual(state['readings']['rpm'],1900)
        self.assertIsNone(self.p._task)

    async def test_cancelled_passive_rpc_retains_lock_until_transport_closes_before_enable(self):
        entered=threading.Event();release=threading.Event();closed=threading.Event()
        def read():
            entered.set();release.wait(2);closed.set();return dict(valid=True,rpm=1500,apu_temperature=45,age_seconds=0,automatic=True,error='',ownership=dict(blocked=False,state='firmware_auto',reason=''))
        async def start():
            self.assertTrue(closed.is_set());self.p._client=FakeClient();self.p._client.configure(True,DEFAULT_CURVE)
        with patch.object(fan,'read_automatic',side_effect=read),patch.object(self.p,'_start_client',side_effect=start):
            request=asyncio.create_task(self.p.get_state())
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            request.cancel();await asyncio.sleep(.01)
            changing=await self.p.get_state();self.assertFalse(changing['readings']['valid'])
            enable=asyncio.create_task(self.p.set_enabled(True));await asyncio.sleep(.01)
            self.assertFalse(enable.done());self.assertTrue(self.p._mutation.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):await request
            await enable
        self.assertTrue(closed.is_set())


class SafetyInputTests(unittest.TestCase):
    def test_realistic_maximum_speed_coastdown_is_not_a_stop_fault_but_timeout_is_bounded(self):
        # Device trace: 4687 -> 1541 RPM over 9.8s, despite effective target zero.
        # Continued inertial decay is harmless; a rotor that never stops is not.
        for seconds,rpm in [(0,4687),(9.8,1541),(15,200),(18,0)]:
            self.assertFalse(stop_timed_out(rpm,0,seconds))
        self.assertTrue(stop_timed_out(200,0,21))
        self.assertTrue(stop_timed_out(0,1500,21))
        self.assertFalse(stop_timed_out(0,0,21))

    def test_invalid_stale_and_missing_temperature_data(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            path=__import__('pathlib').Path(d)/'sensor';path.write_text('45000')
            fd=os.open(path,os.O_RDONLY);s=guard.Sensors.__new__(guard.Sensors);s.fds={'k10temp':fd}
            try:
                self.assertEqual(s.read(),{'k10temp':45})
                for raw in ['NaN','-1','105001','', '45000\n45000']:
                    path.write_text(raw)
                    with self.assertRaises((RuntimeError,ValueError)): s.read()
                    self.assertEqual(s.last_sample['code'], 'sensor_range' if raw in ('-1','105001') else 'sensor_value_format')
                    self.assertEqual(s.last_sample['sensor_name'],'k10temp')
                    if raw=='105001':self.assertEqual(s.last_sample['cpu_temperature'],105.001)
                    self.assertIn('sensor_finished_at',s.last_sample)
                path.write_text('45000')
                with patch.object(guard.time,'monotonic',side_effect=[0,1]):
                    with self.assertRaisesRegex(guard.Retry,'Stale'): s.read()
                self.assertEqual(s.last_sample['code'],'sensor_acquisition_timeout')
                self.assertEqual(s.last_sample['acquisition_ms'],1000)
                self.assertEqual(s.last_sample['cpu_temperature'],45)
                self.assertEqual(s.read(),{'k10temp':45})
                self.assertNotIn('code',s.last_sample,'A later healthy acquisition must not inherit an old fault')
            finally:s.close()

    def test_session_endings_are_classified_and_unknown_failures_fail_closed(self):
        from fan_sleep import Pause,Retry
        for exc,outcome in [(Pause('Suspend requested'),'pause'),(BrokenPipeError(),'pause'),(ConnectionResetError(),'pause'),
                            (Retry('Stale/slow APU temperature acquisition.'),'retry'),(TimeoutError('EC transaction ownership timeout.'),'retry'),
                            (RuntimeError('Fan override changed outside its owner.'),'fault'),(OSError('Short EC port read.'),'fault'),
                            (ValueError('bad'),'fault')]:
            self.assertEqual(guard.outcome_of(exc),outcome,exc)

    def test_suspend_gap_pauses_but_normal_clock_jitter_does_not(self):
        from fan_sleep import Pause
        with patch.object(guard.time,'CLOCK_BOOTTIME',7,create=True), \
                patch.object(guard.time,'clock_gettime',return_value=100,create=True), \
                patch.object(guard.time,'monotonic',return_value=95):
            self.assertEqual(guard.boot_gap(5.1),5)
            with self.assertRaisesRegex(Pause,'Suspend'): guard.boot_gap(3)

    def test_conflicting_kernel_modules_are_matched_by_exact_module_name(self):
        from pathlib import Path
        for modules,blocked in [('legion_laptop 4096 0 - Live 0x0',True),
                                ('gpd_fan 4096 0 - Live 0x0',True),
                                ('unrelated_legion_laptop_monitor 4096 0 - Live 0x0',False),
                                ('gpd_fan_telemetry 4096 0 - Live 0x0',False)]:
            with self.subTest(modules=modules), \
                    patch.object(Path,'read_text',lambda p:modules if p.name=='modules' else ''), \
                    patch.object(guard.os,'listdir',return_value=[]):
                if blocked:
                    with self.assertRaisesRegex(RuntimeError,'Conflicting fan driver'):guard.check_owners()
                else:guard.check_owners()

    def test_raw_port_scan_finds_dev_port_holder_and_skips_vanished_entries(self):
        from pathlib import Path
        me=str(guard.os.getpid())
        listing={'/proc':['1','self','7','9',me],'/proc/1/fd/':['0','3'],'/proc/9/fd/':['0','4']}
        links={'/proc/1/fd/0':'/dev/null','/proc/9/fd/0':'/dev/null','/proc/9/fd/4':'/dev/port'}
        def listdir(path):
            if path==f'/proc/{me}/fd/':self.fail('the guardian must not scan its own fds')
            if path not in listing:raise FileNotFoundError(path)  # process 7 exited mid-scan
            return listing[path]
        def readlink(path):
            if path not in links:raise FileNotFoundError(path)  # fd 3 closed mid-scan
            return links[path]
        with patch.object(Path,'read_text',return_value=''), \
                patch.object(guard.os,'listdir',side_effect=listdir), \
                patch.object(guard.os,'readlink',side_effect=readlink):
            with self.assertRaisesRegex(RuntimeError,'Another app is using the fan controller'):guard.check_owners()
            links['/proc/9/fd/4']='/dev/tty'
            guard.check_owners()
            with patch.object(guard.time,'monotonic',side_effect=[0,0.6]):
                with self.assertRaisesRegex(guard.Retry,'time budget'):guard.check_owners()

    def test_pci_aperture_is_not_claimant_but_real_ec_port_driver_blocks(self):
        from pathlib import Path
        for text,blocked in [('0000-0cf7 : PCI Bus 0000:00\n  0062-0062 : EC data',False),
                             ('0000-0cf7 : PCI Bus 0000:00\n  004e-004f : unknown ec driver',True)]:
            with patch.object(Path,'read_text',lambda p:text if p.name=='ioports' else ''), \
                    patch.object(guard.os,'listdir',return_value=[]):
                if blocked:
                    with self.assertRaisesRegex(RuntimeError,'Another driver is using the fan controller'): guard.check_owners()
                else:guard.check_owners()
