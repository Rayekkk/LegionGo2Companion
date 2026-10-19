# SPDX-License-Identifier: BSD-3-Clause
"""Diagnostic events stay truthful, private and quiet on repeated faults.

Each case drives a real situation through the module's RPCs or Decky lifecycle hooks on the
fakes the module tests already use (sysfs trees, a fake ryzenadj, a fake gamescope, real
settings stores in a temp dir) and reads the encoded lines the event file would receive.
"""
import asyncio
from collections import OrderedDict
import copy
import errno
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support
import diagnostics
import battery_backend as battery
import controller_backend as controller
import remap_backend as remap
import rgb_backend as rgb
import main
import safe_settings
import tdp_backend as tdp
from safe_settings import AtomicSettingsManager
import test_display_hybrid
import test_tdp_core
import test_vibration_backend

MARKER = 'ConfidentialValue_Z7'


class Events:
    """Capture each encoded event line exactly as the event file would receive it."""

    def setUp(self):
        super().setUp()
        self.records = []
        for name, value in (('_sink', lambda text, _level: self.records.append(json.loads(text))),
                            ('_seen', OrderedDict())):  # repeat/transition memory starts empty
            patcher = patch.object(diagnostics, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def events(self, component, action):
        return [row for row in self.records if row['component'] == component and row['event'] == action]

    def assertNotLogged(self, text, logger=None):
        self.assertNotIn(text, json.dumps(self.records))
        if logger is not None:
            self.assertNotIn(text, str(logger.mock_calls))


class VibrationEventTests(Events, test_vibration_backend.FakeDevice):
    async def test_uninstall_does_not_claim_a_restore_the_controller_did_not_read_back(self):
        self.ignored.add('rumble_intensity')  # accepted by the driver, never applied
        (self.device / 'rumble_intensity').write_text('low', encoding='ascii')
        await self.plugin._uninstall()
        self.ignored.clear()
        await self.plugin._uninstall()
        rows = self.events('vibration', 'restore')
        self.assertEqual([row['context']['restored'] for row in rows], [False, True])
        self.assertEqual([row['level'] for row in rows], ['warning', 'info'])
        failed, = self.events('vibration', 'attribute_write')
        self.assertEqual(failed['context']['attribute'], 'rumble_intensity')


SNAPSHOT = {'profile': 3, 'mode': 'custom', 'effect': 'monocolor', 'brightness': 20,
            'brightness_max': 100, 'rgb': [0, 100, 100], 'rgb_max': [100, 100, 100],
            'speed': 50, 'enabled': False}


class RgbEventTests(Events, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = AtomicSettingsManager('rgb_settings', str(self.root / 'settings'))
        # No joystick-ring interface on the test host, whatever its DMI says.
        for name, value in (('settings', self.store), ('RGB_LED_PATH', str(self.root / 'no-rings')),
                            ('REAPPLY_WAIT_S', 0)):
            patcher = patch.object(rgb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.plugin = rgb.Plugin()

    def save(self, **state):
        self.store.replace({'state': {**rgb.DEFAULT_STATE, **state}})

    async def test_uninstall_reports_an_interrupted_change_it_could_not_recover(self):
        self.save(pending={'previous': dict(rgb.DEFAULT_STATE), 'before_rgb': SNAPSHOT, 'before_power': None})
        await self.plugin._uninstall()
        row, = self.events('rgb', 'restore')
        self.assertEqual((row['level'], row['context']['restored'], row['context']['outcome']),
                         ('warning', False, 'unconfirmed'))
        recovery, = self.events('rgb', 'transaction_recovery')
        self.assertTrue(recovery['context']['pending'])

    async def test_missing_rings_are_reported_once_and_the_recovery_is_visible(self):
        self.save(configured=True, control_enabled=True, original_rgb=SNAPSHOT)
        self.assertFalse((await self.plugin.reapply())['success'])
        self.assertFalse((await self.plugin.reapply())['success'])
        self.save()  # ring control no longer owned
        self.assertTrue((await self.plugin.reapply())['success'])
        rows = self.events('rgb', 'reconcile')
        self.assertEqual([row['context']['status'] for row in rows], ['ring_unavailable', 'confirmed'])
        self.assertEqual([row['level'] for row in rows], ['warning', 'info'])

    # The attribute writer is the sysfs boundary itself: these drive it on a temp tree.
    async def test_refused_write_keeps_errno_and_attribute_without_raw_path_or_text(self):
        secret = '/home/private/secret-file'
        with patch.object(rgb.os, 'open', side_effect=PermissionError(errno.EACCES, 'private detail', secret)):
            self.assertFalse(rgb._write_attr(secret, 'brightness', '11', force=True))
        row, = self.events('rgb', 'attribute_write')
        self.assertEqual(row['error']['errno'], errno.EACCES)
        self.assertEqual(row['context']['attribute'], 'brightness')
        self.assertNotLogged('/home/private')
        self.assertNotLogged('private detail')

    async def test_short_write_has_its_own_reason(self):
        (self.root / 'brightness').write_text('10\n')
        with patch.object(rgb.os, 'write', return_value=1):
            self.assertFalse(rgb._write_attr(str(self.root), 'brightness', '11', force=True))
        row, = self.events('rgb', 'attribute_write')
        self.assertEqual((row['context']['reason'], row['context']['bytes']), ('short_write', 1))

    @unittest.skipUnless(os.name == 'posix', 'real flock on the power-button LED lock')
    async def test_busy_power_led_lock_names_the_read_and_the_write_stage(self):
        import fcntl
        (self.root / 'DSDT').write_bytes(support.power_led_dsdt())
        (self.root / 'mem').write_bytes(bytes(4096))
        lock = self.root / 'power.lock'
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another holder keeps the LED busy
        identity = {'product_family': rgb.EXPECTED_FAMILY, 'product_name': rgb.EXPECTED_PRODUCT,
                    'board_name': rgb.EXPECTED_BOARD, 'bios_version': 'RRCN16WW'}
        self.save(power_led_managed=True, power_led_enabled=False, power_led_original_enabled=True)
        with patch.object(rgb, '_read_identity', return_value=identity), \
                patch.object(rgb, '_power_capability_cache', None), \
                patch.object(rgb, 'DSDT_PATH', str(self.root / 'DSDT')), \
                patch.object(rgb, 'DEV_MEM_PATH', str(self.root / 'mem')), \
                patch.object(rgb, 'POWER_LOCK_PATH', str(lock)):
            self.assertIsNone((await self.plugin.get_status())['power_led']['current'])
            await self.plugin._uninstall()
        rows = self.events('rgb', 'power_led_access')
        self.assertEqual([row['context']['stage'] for row in rows], ['read', 'write'])
        self.assertTrue(all(row['error']['type'] == 'TimeoutError' for row in rows))
        self.assertFalse(self.events('rgb', 'restore')[0]['context']['restored'])


PHYSICAL = {'path': '/dev/hidraw1', 'identity': '/sys/devices/usb/1-1:1.2',
            'sysfs': '/sys/devices/usb/1-1:1.2/0003:17EF:61EB.0002', 'pid': '61eb', 'devnum': '241:1'}
NO_IMU = {'left': False, 'right': False}
OWNED = {'gyro_source': 'left', 'ownership': {
    'identity': PHYSICAL['identity'], 'pending': False, 'baseline': [], 'applied': [], 'previous': [],
    'imu_baseline': NO_IMU, 'imu_applied': NO_IMU, 'imu_previous': NO_IMU}}


class ControllerEventTests(Events, unittest.IsolatedAsyncioTestCase):
    """HID enumeration is the stubbed boundary; DMI, settings and the descriptor open are real or files."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.dmi = self.root / 'dmi'
        self.dmi.mkdir()
        self.store = AtomicSettingsManager('controller_settings', str(self.root / 'settings'))
        self.inventory = (None, None)

        def inventory():
            if isinstance(self.inventory, BaseException):
                raise self.inventory
            return self.inventory
        for name, value in (('settings', self.store), ('DMI_ROOT', self.dmi),
                            ('_hid_inventory', inventory)):
            patcher = patch.object(controller, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.plugin = controller.Plugin()

    def legion_go_2(self):
        (self.dmi / 'sys_vendor').write_text('LENOVO\n')
        (self.dmi / 'product_name').write_text('83N0\n')

    async def test_startup_failure_retries_and_recovery_are_one_state_history(self):
        self.store.replace({'state': OWNED})
        clock, ticks = [0.0], []

        async def tick(_seconds):
            ticks.append(clock[0])
            clock[0] += 61
            if len(ticks) == 2:
                self.store.replace({'state': {'gyro_source': 'system'}})  # ownership released
            if len(ticks) > 2:
                raise asyncio.CancelledError()
        with patch.object(controller, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(controller.asyncio, 'sleep', side_effect=tick):
            await self.plugin._main()  # controller absent: deferred, the watch keeps retrying
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._watch_task
        rows = self.events('controller', 'managed_profile')
        self.assertEqual([row['context']['status'] for row in rows], ['deferred', 'True'])
        self.assertEqual([row['level'] for row in rows], ['warning', 'info'])
        await self.plugin._unload()

    async def test_motion_test_records_endpoints_and_a_finite_summary_without_its_token(self):
        self.inventory = (PHYSICAL, None)  # identity not verifiable: DMI is not a Go 2
        token = (await self.plugin.start_diagnostics())['token']
        await self.plugin.stop_diagnostics(token)
        streams = self.events('controller', 'diagnostics_stream')
        self.assertEqual({row['context'].get('stage') or row['context']['reason'] for row in streams},
                         {'open', 'endpoint_unavailable'})
        row, = self.events('controller', 'diagnostics_complete')
        self.assertEqual((row['context']['reason'], row['context']['count']), ('unavailable', 0))
        self.assertNotLogged(token)

    @unittest.skipUnless(os.name == 'posix', 'hidraw open flags exist only on POSIX')
    async def test_battery_read_fault_is_private_and_an_absent_controller_stays_quiet(self):
        self.legion_go_2()
        self.inventory = (PHYSICAL, None)
        real_open, opened = os.open, []

        def device_open(path, *args, **kwargs):
            if str(path).startswith('/dev/hidraw'):
                opened.append(path)
                raise PermissionError(errno.EACCES, MARKER)
            return real_open(path, *args, **kwargs)
        with patch.object(controller.os, 'open', side_effect=device_open):
            result = await self.plugin.get_battery_levels()
            self.assertEqual(await self.plugin.get_battery_levels(), result)  # cached, not reopened
            self.inventory = (None, None)
            self.assertFalse((await controller.Plugin().get_battery_levels())['available'])
        self.assertEqual(opened, ['/dev/hidraw1'])
        self.assertFalse(result['available'])
        self.assertIn(MARKER, result['reason'])  # the panel still explains the fault
        row, = self.events('controller', 'battery_read_failed')
        self.assertEqual(row['error']['errno'], errno.EACCES)
        self.assertTrue(row['error']['frames'])
        self.assertNotLogged(MARKER)

    async def test_lifecycle_restore_fault_is_pending_without_raw_dependency_text(self):
        self.store.replace({'state': OWNED})
        self.inventory = OSError(errno.EIO, MARKER)
        logger = Mock()
        with patch.object(controller.decky, 'logger', logger):
            await self.plugin._uninstall()
        rows = self.events('controller', 'restore_failed')
        self.assertEqual([row['context']['stage'] for row in rows], ['unload', 'uninstall'])
        self.assertTrue(all(row['context']['pending'] and row['error']['errno'] == errno.EIO for row in rows))
        self.assertNotLogged(MARKER, logger)


BASELINE_PROFILE = 'version: 1\nkind: DeviceProfile\nname: Default\nmapping: []\n'


class RemapEventTests(Events, unittest.IsolatedAsyncioTestCase):
    """InputPlumber is out of reach: remapping refuses this host as not a Legion Go 2."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = AtomicSettingsManager('remap_settings', folder.name)
        self.store.replace({'state': {**remap.DEFAULT_STATE, 'enabled': True, 'baseline_profile': BASELINE_PROFILE}})
        for target, name, value in ((remap, 'settings', self.store),
                                    (remap.module_runtime, 'is_legion_go_2', lambda *_: False)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_repeated_startup_failure_and_later_recovery_stay_bounded(self):
        plugin = remap.Plugin()
        self.addAsyncCleanup(plugin._unload)
        await plugin._main()
        await plugin._main()
        self.store.replace({'state': dict(remap.DEFAULT_STATE)})  # remapping turned off
        await plugin._main()
        rows = self.events('remap', 'managed_profile')
        self.assertEqual([row['context']['status'] for row in rows], ['False', 'True'])
        self.assertEqual(rows[0]['error']['type'], remap.RemapError.__name__)

    async def test_uninstall_restore_fault_stays_pending(self):
        logger = Mock()
        with patch.object(remap.decky, 'logger', logger):
            await remap.Plugin()._uninstall()
        row, = self.events('remap', 'restore_failed')
        self.assertEqual({key: row['context'][key] for key in ('stage', 'pending', 'restored')},
                         {'stage': 'uninstall', 'pending': True, 'restored': False})
        self.assertEqual(row['error']['type'], remap.RemapError.__name__)


class SettingsEventTests(Events, unittest.TestCase):
    """A directory where a settings file belongs is a real, refused write on every host."""

    def test_backup_failures_identify_each_store_and_recovery_without_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            stores = [AtomicSettingsManager(name, temporary) for name in ('battery_settings', 'rgb_settings')]
            for store in stores:
                os.mkdir(store.backup_path)
                store.replace({'private_setting': 'private-payload-marker'})
                store.replace({'private_setting': 'private-payload-marker'})
            os.rmdir(stores[0].backup_path)
            stores[0].replace({'private_setting': 'private-payload-marker'})
        rows = self.events('settings', 'backup_refresh')
        self.assertEqual([(row['context']['store'], row['context']['status']) for row in rows],
                         [('battery_settings', 'False'), ('rgb_settings', 'False'), ('battery_settings', 'True')])
        self.assertNotLogged('private-payload-marker')

    def test_primary_commit_failure_is_identified_without_changing_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AtomicSettingsManager('controller_settings', temporary)
            store.replace({'enabled': False})
            os.replace(store.path, store.path + '.moved')
            os.mkdir(store.path)
            with self.assertRaises(Exception):
                store.replace({'enabled': True})
            self.assertFalse(store.getSetting('enabled'))
        row, = self.events('settings', 'primary_commit_failed')
        self.assertEqual((row['level'], row['context']['store']), ('error', 'controller_settings'))
        self.assertIn('type', row['error'])


class TdpEventTests(Events, unittest.TestCase):
    """test_tdp_core's fake firmware-attributes tree, platform profile, ryzenadj and helper download."""

    fixture = test_tdp_core.TdpCoreTests
    seed, saved, rpc, publish_helper = fixture.seed, fixture.saved, fixture.rpc, fixture.publish_helper

    def setUp(self):
        self.fixture.setUp(self)
        super().setUp()

    def refuse(self, path):
        """The firmware refuses this attribute: open() for writing fails."""
        path.unlink()
        path.mkdir()

    def enforce(self, passes):
        """Run the enforce loop for `passes` passes; a charger resettle ladder just waits."""
        done = []

        async def tick(seconds):
            if seconds != 5:
                await asyncio.Event().wait()
            if len(done) == passes:
                raise asyncio.CancelledError()
            done.append(seconds)
        with patch.object(tdp.asyncio, 'sleep', side_effect=tick), self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.plugin._enforce_loop())

    def unlock_extras(self):
        self.bin.unlink()
        self.publish_helper()
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])

    def test_refused_firmware_writes_name_each_attribute_and_in_place_refusals_stay_info(self):
        self.profile.write_text('custom')
        for attr in tdp.WMI_ATTRS.values():
            self.refuse(self.firmware / attr / 'current_value')
        self.assertFalse(self.rpc('apply_tdp', 18000, 22000, 30000)['success'])
        self.refuse(self.profile)
        self.assertFalse(self.rpc('apply_tdp', 18000, 22000, 30000)['success'])
        rows = {(row['context']['attribute'], row['level'], row['context']['outcome'])
                for row in self.events('tdp', 'attribute_write')}
        expected = {(attr, 'info', 'retried') for attr in tdp.WMI_ATTRS.values()}  # in place: the bounce writes again
        expected |= {(attr, 'warning', 'failed') for attr in tdp.WMI_ATTRS.values()}
        self.assertEqual(rows, expected | {('platform_profile', 'warning', 'failed')})
        self.assertEqual(self.events('tdp', 'limits_applied'), [])

    def test_rollback_exception_is_reported_not_discarded(self):
        self.unlock_extras()
        self.assertTrue(self.rpc('apply_tdp', 45000, 48000, 50000)['success'])  # beyond firmware: ryzenadj
        with patch.object(tdp.subprocess, 'Popen', side_effect=OSError(errno.EIO, 'exec failed')):
            try:
                result = self.rpc('apply_tdp', 45000, 46000, 50000)
            except RuntimeError as exc:
                result = {'success': False, 'stderr': str(exc)}
        self.assertFalse(result['success'])
        self.assertIn('rollback failed', result['stderr'])
        row, = self.events('tdp', 'rollback')
        self.assertEqual((row['level'], row['error']['errno'], row['context']['restored']), ('error', errno.EIO, False))

    def test_helper_download_failure_and_verified_retry_have_finite_outcomes(self):
        self.bin.unlink()
        self.assertFalse(self.rpc('retry_extras')['success'])  # download offline
        self.publish_helper()
        self.assertTrue(self.rpc('retry_extras')['success'])
        rows = self.events('tdp', 'helper_ready')
        self.assertEqual([row['context']['status'] for row in rows], ['False', 'True'])
        self.assertEqual(rows[0]['level'], 'warning')
        self.assertNotLogged('offline')

    def test_cpu_status_on_unrecoverable_settings_is_generic_and_private(self):
        for path in (self.store.path, self.store.backup_path):
            Path(path).write_text('{"' + MARKER)  # both copies damaged
        logger = Mock()
        with patch.object(tdp.decky, 'logger', logger):
            result = self.rpc('get_cpu_power_controls')
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'CPU power controls unavailable')
        row, = self.events('tdp', 'cpu_status_failed')
        self.assertEqual(row['error']['type'], 'CorruptSettings')
        self.assertNotLogged(MARKER, logger)

    def test_returned_helper_error_stays_out_of_the_log_and_is_not_saved_or_retried(self):
        self.unlock_extras()
        self.assertTrue(self.rpc('apply_tdp', 45000, 48000, 50000)['success'])
        active = self.saved()['active_sppt']
        test, attempts = self, []

        class Rejecting:  # ryzenadj exits 4 and prints private text
            def __init__(self, argv, **_options):
                if argv[1:] != ['--info']:
                    attempts.append(argv[1:])
                self.returncode = 4

            def communicate(self, timeout=None):
                return MARKER.encode(), MARKER.encode()
        self.app = '111'  # a game without a profile starts: Global is reapplied
        logger = Mock()
        with patch.object(tdp.subprocess, 'Popen', Rejecting), patch.object(tdp.decky, 'logger', logger):
            self.enforce(passes=1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.saved()['active_sppt'], active)
        row, = self.events('tdp', 'limits_apply_failed')
        self.assertEqual(row['context']['returncode'], 4)
        self.assertNotLogged(MARKER, logger)
        del test

    def test_enforcement_loop_logs_liveness_and_recovery(self):
        self.ac = True  # charger plugged in: the pass reports it to the panel
        with patch.object(tdp.decky, 'emit', AsyncMock(side_effect=[OSError(errno.EIO, MARKER)])):
            self.enforce(passes=2)
        rows = self.events('tdp', 'enforcement_loop')
        self.assertEqual([row['context']['status'] for row in rows], ['unavailable', 'running'])
        self.assertEqual(rows[0]['error']['errno'], errno.EIO)
        self.assertEqual(rows[-1]['context']['outcome'], 'cycle_complete')
        self.assertNotLogged(MARKER)

    def test_applied_limits_record_watts_source_and_reason(self):
        self.app = '111'
        self.enforce(passes=1)  # game start without a profile: Global, reason 'global'
        self.assertTrue(self.rpc('apply_tdp', 15000, 18000, 20000)['success'])
        transition, rpc = self.events('tdp', 'limits_applied')
        self.assertEqual({key: transition['context'][key] for key in ('spl', 'sppt', 'fppt', 'source', 'reason')},
                         {'spl': 15, 'sppt': 20, 'fppt': 25, 'source': 'wmi', 'reason': 'global'})
        self.assertEqual({key: rpc['context'][key] for key in ('spl', 'sppt', 'fppt')},
                         {'spl': 15, 'sppt': 18, 'fppt': 20})
        self.assertIsNone(rpc['context'].get('reason'))


class BatteryEventTests(Events, unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_uninstall_restore_is_a_fixed_outcome_not_a_log_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'sys'
            identity = root / 'class/dmi/id'
            identity.mkdir(parents=True)
            (identity / 'sys_vendor').write_text('LENOVO', encoding='ascii')
            (identity / 'product_name').write_text('83N0', encoding='ascii')
            (root / 'class/power_supply').mkdir()  # the battery interface is gone
            store = AtomicSettingsManager('battery_settings', str(Path(temporary) / 'settings'))
            store.replace({'state': {**battery.DEFAULT_STATE, 'managed': True, 'requested_enabled': True,
                                     'baseline': 'Fast', 'normal_mode': 'Fast', 'last_applied': 'Long_Life'}})
            with patch.object(battery, 'SYS_ROOT', root), patch.object(battery, 'settings', store):
                self.assertIsNone(await battery.Plugin()._uninstall())
            self.assertTrue(store.getSetting('state')['managed'])  # ownership kept for a later restore
        row, = self.events('battery', 'restore_failed')
        self.assertEqual({key: row['context'][key] for key in ('restored', 'reason', 'pending')},
                         {'restored': False, 'reason': 'operation_unconfirmed', 'pending': True})
        self.assertNotIn('error', row)  # the RPC's returned error text is the panel's, not this event's


class DisplayEventTests(Events, test_display_hybrid.DisplayFixture):
    def test_mode_change_records_both_modes_and_the_pending_restart(self):
        self.start('pq')
        asyncio.run(display_plugin().set_panel_mode('gamma22'))
        row, = self.events('display', 'mode_changed')
        self.assertEqual({key: row['context'][key] for key in ('previous', 'mode', 'pending')},
                         {'previous': 'pq', 'mode': 'gamma22', 'pending': True})


def display_plugin():
    return test_display_hybrid.display.Plugin()


class DiagnosticsFormatTests(Events, unittest.TestCase):
    def test_runtime_event_identifies_device_firmware_and_software_without_ec_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (('product_name', '83N0'), ('bios_version', 'RRCN16WW'), ('ec_firmware_release', '1.16')):
                (root / name).write_text(value + '\n')
            (root / 'os-release').write_text('ID=steamos\nVERSION_ID=3.8.28\nBUILD_ID="20260922.1"\n')
            with patch.object(main.decky, 'DECKY_VERSION', 'v3.2.10', create=True):
                identity = main._runtime_identity(str(root), str(root / 'os-release'))
            self.assertEqual(identity, {'model': '83N0', 'bios': 'RRCN16WW', 'ec': '1.16', 'steamos': '3.8.28',
                                        'steamos_build': '20260922.1', 'decky': 'v3.2.10'})
            missing = main._runtime_identity(str(root / 'absent'), str(root / 'absent'))
        self.assertEqual(set(missing.values()), {None})
        diagnostics.event('plugin', 'runtime', **identity)
        import diagnostics_reader
        self.assertEqual(diagnostics_reader.normalize_event(self.records[-1], 'backend')['context'],
                         {'source': 'decky', 'operation': None, **identity})

    def test_only_this_plugins_exceptions_add_a_bounded_private_message(self):
        diagnostics.event('remap', 'restore_failed', level='warning',
                          error=safe_settings.CorruptSettings('settings at /home/deck/x are unreadable'))
        diagnostics.event('remap', 'restore_failed', level='warning', error=RuntimeError('SSID=Private Net'))
        ours, foreign = self.records
        self.assertEqual(ours['error']['message'], 'settings at [private-path] are unreadable')
        self.assertNotIn('message', foreign['error'])


@unittest.skipUnless(os.name == 'posix', 'dir_fd and O_NOFOLLOW semantics')
class LogRotationTests(unittest.TestCase):
    """Decky chowns the plugin log directory to deck; root pruning must stay inside it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()

    def make_logs(self, folder, count):
        for i in range(count):
            path = folder / f'{i:02}.log'
            path.write_text('x')
            os.utime(path, (1000 + i, 1000 + i))

    def rotate(self, log_dir, current='00.log'):
        with patch.object(main.decky, 'DECKY_PLUGIN_LOG_DIR', str(log_dir), create=True), \
                patch.object(main.decky, 'DECKY_PLUGIN_LOG', str(log_dir / current), create=True):
            main._rotate_logs(keep=10)

    def test_keeps_the_newest_ten_and_the_current_log(self):
        self.make_logs(self.logs, 13)
        (self.logs / 'notes.txt').write_text('keep')
        self.rotate(self.logs)
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()),
                         ['00.log'] + [f'{i:02}.log' for i in range(3, 13)] + ['notes.txt'])

    def test_a_log_that_disappears_during_the_scan_is_skipped_quietly(self):
        self.make_logs(self.logs, 13)
        real_stat = os.stat
        def vanishing(name, *args, **kwargs):
            if name == '05.log':
                raise FileNotFoundError(name)
            return real_stat(name, *args, **kwargs)
        with patch.object(main.os, 'stat', side_effect=vanishing), patch.object(main.diagnostics, 'event') as event:
            self.rotate(self.logs)
        event.assert_not_called()
        self.assertFalse((self.logs / '01.log').exists())   # the rest of the pass still ran

    def test_a_symlinked_log_directory_or_log_entry_is_never_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.make_logs(outside, 13)
        link = self.root / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        with patch.object(main.diagnostics, 'event'):
            self.rotate(link)
        self.assertEqual(len(list(outside.iterdir())), 13)
        self.make_logs(self.logs, 11)
        (self.logs / 'aa.log').symlink_to(outside / '05.log')
        os.utime(self.logs / 'aa.log', (1, 1), follow_symlinks=False)
        self.rotate(self.logs, current='none.log')
        self.assertTrue((outside / '05.log').exists())
        self.assertTrue((self.logs / 'aa.log').is_symlink())
        self.assertFalse((self.logs / '00.log').exists())


if __name__ == '__main__':
    unittest.main()
