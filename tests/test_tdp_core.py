# SPDX-License-Identifier: BSD-3-Clause
"""TDP core behaviour on a fake firmware-attributes / platform-profile sysfs.

The hardware boundary is files plus a fake ryzenadj process and a fake helper
download; only time-like inputs (foreground game, charger, suspend clock) are
injected directly.
"""
import asyncio
import copy
from contextlib import ExitStack
import hashlib
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import support  # noqa: F401 - installs the fake decky module
import tdp_backend as tdp
from safe_settings import AtomicSettingsManager
import module_control

REAL_RUNNING_APPID = tdp._get_running_appid
REAL_BIN_DIR, REAL_PLUGIN_DIR = tdp.BIN_DIR, tdp.PLUGIN_DIR
# The module's runtime state as imported (before any test ran): plain values, no locks or functions.
PRISTINE_STATE = {name: copy.copy(value) for name, value in vars(tdp).items()
                  if name.startswith('_') and not name.startswith('__')
                  and isinstance(value, (bool, int, float, str, tuple, dict, type(None)))}
HELPER = b'\x7fELF fake ryzenadj'
LIMIT_FLAGS = {'--stapm-limit': 'spl', '--slow-limit': 'sppt', '--fast-limit': 'fppt'}
INFO = """| STAPM LIMIT | {spl:.3f} | stapm-limit |
| PPT LIMIT FAST | {fppt:.3f} | fast-limit |
| PPT LIMIT SLOW | {sppt:.3f} | slow-limit |
"""


class TdpCoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.firmware = self.root / 'firmware'
        for key, attr in tdp.WMI_ATTRS.items():
            entry = self.firmware / attr
            entry.mkdir(parents=True)
            (entry / 'min_value').write_text('5')
            (entry / 'max_value').write_text(str(tdp.FALLBACK_STD_W[key]))
            (entry / 'current_value').write_text('10')
        node = self.root / 'platform-profile/platform-profile-0'
        node.mkdir(parents=True)
        self.profile = node / 'profile'
        self.profile.write_text('balanced')
        self.choices = node / 'choices'
        self.choices.write_text('low-power balanced performance custom')
        self.cpu = self.root / 'cpu'
        self.cpu.mkdir()
        self.bin = self.root / 'bin/ryzenadj'
        self.bin.parent.mkdir()
        self.bin.write_bytes(b'')  # present but not the verified helper
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        self.store = AtomicSettingsManager('tdp_settings', str(self.root / 'settings'))
        self.seed()
        self.app, self.ac, self.machine = '', False, '83N0'
        self.info_failure = None  # ryzenadj's output when it exits 1
        self.ryzenadj = []
        self.smu = {}  # limits written through ryzenadj; otherwise it reads the firmware's
        self.profile_writes = []
        self.profile_sticks = True
        self.downloads, self.archive = [], None
        self.download_started, self.download_gate = threading.Event(), None
        real_write_profile = tdp._write_profile

        def write_profile(path, value):
            self.profile_writes.append(value)
            # A firmware that ignores the write still reports success to the writer.
            return real_write_profile(path, value) if self.profile_sticks else True

        test = self

        class Ryzenadj:  # the helper process; the real availability gate stays in front
            def __init__(self, argv, **_options):
                test.ryzenadj.append(argv[1:])
                for flag, _, value in (arg.partition('=') for arg in argv[1:]):
                    if flag in LIMIT_FLAGS:
                        test.smu[LIMIT_FLAGS[flag]] = int(value) / 1000
                self.returncode = 0 if test.info_failure is None else 1

            def communicate(self, timeout=None):
                if test.info_failure is not None:
                    return test.info_failure, b''
                if test.ryzenadj[-1] == ['--info']:
                    live = {key: test.smu.get(key, 0) if key in test.smu or not test.firmware.exists()
                            else test.ppt(key) for key in LIMIT_FLAGS.values()}
                    return INFO.format(**live).encode(), b''
                return b'', b''

        def download(url, out, **_options):
            self.downloads.append(url)
            self.download_started.set()
            if self.download_gate is not None and not self.download_gate.wait(5):
                raise TimeoutError('test gate was not released')
            if self.archive is None:
                out.write(b'partial archive')
                raise TimeoutError('offline')
            out.write(self.archive)

        for name, value in {
            'settings': self.store, 'WMI_ROOT': str(self.firmware),
            'PLATFORM_PROFILE_GLOB': str(self.root / 'platform-profile/*/profile'),
            'RAPL_GLOB': str(self.root / 'powercap/intel-rapl-*'),  # ':' is not valid on Windows
            'CPU_SYS_ROOT': str(self.cpu), 'BIN_DIR': str(self.bin.parent), 'BIN_PATH': str(self.bin),
        }.items():
            self.stack.enter_context(patch.object(tdp, name, value))
        for name, value in PRISTINE_STATE.items():  # every test starts from a fresh import's state
            self.stack.enter_context(patch.object(tdp, name, copy.copy(value)))
        for name, callback in {
            '_get_running_appid': lambda: self.app, '_get_ac_online': lambda: self.ac,
            '_write_profile': write_profile,
            '_machine_type': lambda: self.machine,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, side_effect=callback))
        self.stack.enter_context(patch.object(tdp.module_runtime, 'suspend_offset', return_value=0.0))
        self.stack.enter_context(patch.object(tdp._resume_detected, 'last', None))
        self.stack.enter_context(patch.object(tdp.subprocess, 'Popen', Ryzenadj))
        self.stack.enter_context(patch.object(tdp.updater, 'download_to', side_effect=download))
        self.stack.enter_context(patch.object(tdp.updater, 'ssl_context'))
        self.stack.enter_context(patch.object(tempfile, 'tempdir', str(self.tmp)))
        self.plugin = tdp.Plugin()
        self.plugin._ready = True

    def seed(self, profiles=None, **settings):
        state = {'enabled': True, 'extras_unlocked': False,
                 'spl': 15000, 'sppt': 20000, 'fppt': 25000, **settings}
        self.store.replace({'settings': state, 'game_profiles': profiles or {},
                            'schema_version': 2})

    def saved(self, key='settings'):
        return AtomicSettingsManager('tdp_settings', str(self.root / 'settings')).getSetting(key)

    def ppt(self, key):
        return int((self.firmware / tdp.WMI_ATTRS[key] / 'current_value').read_text())

    def ppts(self):
        return tuple(self.ppt(key) for key in ('spl', 'sppt', 'fppt'))

    def set_ppts(self, *values):
        for key, value in zip(('spl', 'sppt', 'fppt'), values):
            (self.firmware / tdp.WMI_ATTRS[key] / 'current_value').write_text(str(value))

    def applies(self):
        return [args for args in self.ryzenadj if args != ['--info']]

    def bounces(self):
        """Firmware applies that had to switch the platform profile (two writes each)."""
        return len(self.profile_writes) // 2

    def firmware_ignores_writes(self):
        return patch.object(tdp, '_write_ppt')

    def disk_full(self):
        return patch('safe_settings._atomic_write_bytes',
                     side_effect=OSError(28, 'No space left on device'))

    def publish_helper(self, archive_ok=True, binary_ok=True):
        """Serve a helper archive and pin the digests the backend verifies it against."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            member = tarfile.TarInfo('ryzenadj-linux/ryzenadj')
            member.size = len(HELPER)
            tar.addfile(member, io.BytesIO(HELPER))
        self.archive = buffer.getvalue()
        wrong = '0' * 64
        for name, digest in (('RYZENADJ_SHA256', hashlib.sha256(self.archive).hexdigest()
                              if archive_ok else wrong),
                             ('RYZENADJ_BINARY_SHA256', hashlib.sha256(HELPER).hexdigest()
                              if binary_ok else wrong)):
            self.stack.enter_context(patch.object(tdp, name, digest))

    def helper_ready(self):
        """The verified helper is installed and in use."""
        self.bin.write_bytes(HELPER)
        self.stack.enter_context(patch.object(tdp, 'RYZENADJ_BINARY_SHA256', hashlib.sha256(HELPER).hexdigest()))
        tdp._ryzenadj_available = True

    def start(self):
        """Run the module's startup; the two background loops are not started."""
        plugin = tdp.Plugin()
        self.stack.enter_context(patch.object(tdp.Plugin, '_ready', False))
        self.stack.enter_context(patch.object(tdp.Plugin, '_setup_error', None))

        async def run():
            with patch.object(plugin, '_enforce_loop', new=AsyncMock()), \
                    patch.object(plugin, '_info_loop', new=AsyncMock()):
                await plugin._main()
                await asyncio.gather(*plugin._tasks)
            plugin._tasks = []  # finished, and bound to this event loop
        asyncio.run(run())
        self.addCleanup(lambda: asyncio.run(plugin._unload()))
        return plugin

    def rpc(self, method, *args, **kwargs):
        return asyncio.run(getattr(self.plugin, method)(*args, **kwargs))

    # Firmware path ---------------------------------------------------------

    def test_firmware_apply_bounces_through_low_power_and_reads_back(self):
        result = tdp._apply_limits(18000, 22000, 30000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.assertEqual(self.profile.read_text(), 'custom')
        self.assertEqual(self.ppts(), (18, 22, 30))
        self.assertEqual(self.applies(), [])

    def test_firmware_apply_in_custom_writes_in_place_when_it_latches(self):
        self.profile.write_text('custom')
        self.assertTrue(tdp._apply_limits(18000, 22000, 30000)['success'])
        self.assertEqual(self.profile_writes, [])
        self.assertEqual(self.ppts(), (18, 22, 30))

    def test_dropped_in_place_write_falls_back_to_a_bounce(self):
        self.profile.write_text('custom')
        real_write_ppt, calls = tdp._write_ppt, []

        def firmware_drops_first_write(*values, **options):
            calls.append(values)
            if len(calls) > 1:
                real_write_ppt(*values, **options)

        with patch.object(tdp, '_write_ppt', side_effect=firmware_drops_first_write):
            self.assertTrue(tdp._apply_limits(18000, 22000, 30000)['success'])
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.assertEqual(self.ppts(), (18, 22, 30))

    def test_firmware_readback_mismatch_is_a_failure_naming_the_attribute(self):
        with self.firmware_ignores_writes():
            result = tdp._apply_limits(18000, 22000, 30000)
        self.assertFalse(result['success'])
        self.assertIn('ppt_pl1_spl=10 want 18', result['stderr'])

    def test_stop_during_the_startup_apply_ends_the_start_quietly_with_no_further_firmware_write(self):
        real_write_profile = tdp._write_profile.side_effect
        def stop_after_first_write(path, value):
            result = real_write_profile(path, value)
            tdp.module_runtime.request_stop('tdp')  # Decky's stop lands mid-bounce.
            return result
        tdp._write_profile.side_effect = stop_after_first_write
        self.addCleanup(tdp.module_runtime._stop_requested.discard, 'tdp')
        self.stack.enter_context(patch.object(tdp.Plugin, '_ready', False))
        self.stack.enter_context(patch.object(tdp.Plugin, '_setup_error', None))
        before, plugin = self.ppts(), tdp.Plugin()
        async def start():
            with tdp.module_runtime.starting('tdp'):
                await plugin._main()
        with patch.object(tdp, 'event') as event:
            asyncio.run(start())
        self.assertEqual(self.profile.read_text(), 'low-power')  # The write in flight finished...
        self.assertEqual(self.ppts(), before)  # ...and no other began.
        self.assertEqual(self.applies(), [])
        self.assertFalse(tdp.Plugin._ready)
        self.assertIsNone(tdp.Plugin._setup_error)
        self.assertNotIn('startup_failed', [call.args[1] for call in event.call_args_list])

    # Clamp and path choice -------------------------------------------------

    def test_hardware_clamp_floor_and_ordering(self):
        self.assertTrue(tdp._apply_limits(1000, 900, 800)['success'])
        self.assertEqual(self.ppts(), (5, 5, 5))
        self.assertTrue(tdp._apply_limits(20000, 10000, 15000)['success'])
        self.assertEqual(self.ppts(), (20, 20, 20))

    def test_extras_select_the_firmware_custom_mode_before_ryzenadj(self):
        self.helper_ready()
        self.profile.write_text('performance')  # measured: Extras used to leave a firmware mode on
        result = tdp._apply_limits(60000, 70000, 80000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.profile.read_text(), 'custom')
        self.assertEqual(self.ppts(), (35, 37, 45))  # the nearest limits the firmware accepts
        self.assertEqual(self.applies(), [[
            '--stapm-limit=50000', '--slow-limit=50000', '--fast-limit=50000']])

    def test_extras_never_run_ryzenadj_when_custom_mode_is_not_confirmed(self):
        self.helper_ready()
        with self.firmware_ignores_writes():
            result = tdp._apply_limits(35000, 45000, 50000)
        self.assertFalse(result['success'])
        self.assertEqual(self.applies(), [])

    def test_extras_on_a_go_1_keep_spl_at_its_firmware_ceiling_and_boosts_at_50w(self):
        self.machine = '83E1'
        self.firmware_range((30, 32, 41))
        self.helper_ready()
        self.seed(extras_unlocked=True)
        self.assertEqual(self.rpc('get_caps')['max'], {'spl': 30, 'sppt': 50, 'fppt': 50})
        self.assertTrue(self.rpc('apply_tdp', 45000, 60000, 60000)['success'])
        self.assertEqual(self.ppts(), (30, 32, 41))
        self.assertEqual(self.applies(), [[
            '--stapm-limit=30000', '--slow-limit=50000', '--fast-limit=50000']])

    def test_out_of_range_without_verified_helper_writes_nothing(self):
        result = tdp._apply_limits(40000, 45000, 50000)
        self.assertFalse(result['success'])
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.ppts(), (10, 10, 10))

    def test_firmware_failure_inside_range_never_falls_back_to_ryzenadj(self):
        self.helper_ready()
        with self.firmware_ignores_writes():
            result = tdp._apply_limits(18000, 22000, 30000)
        self.assertFalse(result['success'])
        self.assertEqual(self.applies(), [])

    def test_rpc_without_extras_is_clamped_to_firmware_ceilings(self):
        self.helper_ready()
        result = self.rpc('apply_tdp', 45000, 48000, 50000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.ppts(), (35, 37, 45))
        self.assertEqual(self.applies(), [])

    def test_rpc_with_extras_reaches_ryzenadj(self):
        self.helper_ready()
        self.seed(extras_unlocked=True)
        self.assertTrue(self.rpc('apply_tdp', 45000, 48000, 50000)['success'])
        # The SMU keeps sustained power at the firmware's SPL ceiling; only the boosts go higher.
        spl_max = self.rpc('get_caps')['std']['spl']
        self.assertEqual(self.applies(), [[
            f'--stapm-limit={spl_max * 1000}', '--slow-limit=48000', '--fast-limit=50000']])
        caps = self.rpc('get_caps')
        self.assertEqual(caps['max'], dict(caps['std'], sppt=50, fppt=50))

    def test_extras_low_sustained_limit_is_shown_as_set_and_not_reapplied(self):
        # Legion Go 2 (2026-10-19): ryzenadj 15/40/50 W reads back STAPM 15, slow 40, fast 50.
        self.helper_ready()
        self.seed(extras_unlocked=True)
        self.assertTrue(self.rpc('apply_tdp', 15000, 40000, 50000)['success'])
        tdp._refresh_info_cache()  # one info-loop tick
        values = self.rpc('get_tdp_info')['values']
        self.assertEqual({key: values[key + '_limit'] for key in ('spl', 'sppt', 'fppt')},
                         {'spl': 15, 'sppt': 40, 'fppt': 50})
        for _ in range(3):
            tdp._check_and_enforce()
        self.assertEqual(len(self.applies()), 1)

    # Helper (RyzenAdj) download -------------------------------------------

    def test_enabling_extras_downloads_and_verifies_the_helper_once(self):
        self.bin.unlink()
        result = self.rpc('set_extras_unlocked', True)
        self.assertFalse(result['success'])
        self.assertIn('offline', result['stderr'])
        self.assertFalse(self.saved()['extras_unlocked'])
        self.publish_helper()
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        self.assertEqual(self.bin.read_bytes(), HELPER)
        self.assertTrue(self.saved()['extras_unlocked'])
        self.assertTrue(self.rpc('get_caps')['extras'])
        self.assertTrue(self.rpc('set_extras_unlocked', False)['success'])
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        self.assertEqual(len(self.downloads), 2)

    def test_helper_download_that_fails_verification_installs_nothing(self):
        self.bin.unlink()
        for case, publish in (('interrupted', None),
                              ('archive checksum', {'archive_ok': False}),
                              ('binary checksum', {'binary_ok': False})):
            with self.subTest(case):
                if publish is not None:
                    self.publish_helper(**publish)
                result = self.rpc('set_extras_unlocked', True)
                self.assertFalse(result['success'])
                if publish is not None:
                    self.assertIn('checksum mismatch', result['stderr'])
                self.assertEqual(list(self.bin.parent.iterdir()), [])
                self.assertEqual(list(self.tmp.iterdir()), [])
                self.assertFalse(self.saved()['extras_unlocked'])
                self.assertFalse(self.rpc('get_caps')['extras'])
        self.assertEqual(len(self.downloads), 3)
        self.assertEqual(self.applies(), [])

    def test_failed_helper_download_is_reported_until_a_verified_retry(self):
        shutil.rmtree(self.firmware)
        self.bin.unlink()
        self.assertFalse(self.rpc('retry_extras')['success'])
        caps = self.rpc('get_caps')
        self.assertFalse(caps['tdp_available'])
        self.assertIn('offline', caps['tdp_error'])
        self.publish_helper()
        self.assertTrue(self.rpc('retry_extras')['success'])
        caps = self.rpc('get_caps')
        self.assertTrue(caps['tdp_available'])
        self.assertEqual(caps['tdp_error'], '')
        self.assertTrue(caps['extras'])
        self.assertFalse(caps['helper_retry_available'])

    def test_concurrent_retries_share_one_download_then_the_game_gets_its_extras_target(self):
        self.bin.unlink()
        self.seed({'123': {'spl': 25000, 'sppt': 40000, 'fppt': 50000}}, extras_unlocked=True)
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (25, 37, 45))  # no helper yet: firmware ceilings
        self.publish_helper()
        self.download_gate = threading.Event()

        async def scenario():
            first = asyncio.create_task(self.plugin.retry_extras())
            await asyncio.to_thread(self.download_started.wait, 5)
            second = asyncio.create_task(self.plugin.retry_extras())  # waits for the running one
            await asyncio.sleep(0.05)
            self.download_gate.set()
            return [(await task)['success'] for task in (first, second)]
        self.assertEqual(asyncio.run(scenario()), [True, True])
        self.assertEqual(len(self.downloads), 1)
        tdp._check_and_enforce()
        self.assertEqual(self.applies(), [
            ['--stapm-limit=25000', '--slow-limit=40000', '--fast-limit=50000']])

    def test_extras_taps_during_a_helper_download_apply_in_order(self):
        # Taps queue behind the download instead of failing, and the last one wins:
        # ON (download), ON again, OFF -> all succeed, Extras ends locked.
        self.bin.unlink()
        self.publish_helper()
        self.download_gate = threading.Event()

        async def scenario():
            first = asyncio.create_task(self.plugin.set_extras_unlocked(True))
            await asyncio.to_thread(self.download_started.wait, 5)
            second = asyncio.create_task(self.plugin.set_extras_unlocked(True))
            off = asyncio.create_task(self.plugin.set_extras_unlocked(False))
            await asyncio.sleep(0.05)
            self.assertFalse(off.done(), 'OFF must not commit before the earlier ON')
            self.download_gate.set()
            return [(await task)['success'] for task in (first, second, off)]
        self.assertEqual(asyncio.run(scenario()), [True, True, True])
        self.assertEqual(len(self.downloads), 1)
        self.assertFalse(self.saved()['extras_unlocked'])

    def test_helper_lives_outside_the_plugin_directory_and_survives_an_update(self):
        self.assertTrue(REAL_BIN_DIR.startswith('/var/lib/'))  # root's, not Decky's tree
        self.assertFalse(REAL_BIN_DIR.startswith(REAL_PLUGIN_DIR))
        self.bin.unlink()
        self.publish_helper()
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        # Decky's update: unload, uninstall, a new plugin directory, a fresh start.
        asyncio.run(self.plugin._uninstall())
        tdp._ryzenadj_available = False
        plugin = self.start()
        self.assertTrue(asyncio.run(plugin.get_caps())['extras'])  # no Retry, no new download
        self.assertEqual(len(self.downloads), 1)
        self.assertTrue(self.rpc('apply_tdp', 30000, 45000, 50000)['success'])
        self.assertEqual(self.applies()[-1],
                         ['--stapm-limit=30000', '--slow-limit=45000', '--fast-limit=50000'])

    def test_helper_is_verified_before_every_run(self):
        self.bin.unlink()
        self.publish_helper()
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        self.bin.write_bytes(HELPER + b'tampered')
        self.assertFalse(tdp._apply_limits(30000, 45000, 50000)['success'])
        self.assertEqual(self.applies(), [])
        self.start()
        self.assertFalse(tdp._ryzenadj_available)  # nor does a restart

    def test_helper_that_cannot_read_the_cpu_hides_extras(self):
        self.bin.unlink()
        self.publish_helper()
        self.info_failure = b'Unable to init ryzenadj\n'
        result = self.rpc('set_extras_unlocked', True)
        self.assertFalse(result['success'])
        self.assertIn(tdp.RYZENADJ_UNSUPPORTED_CPU, result['stderr'])
        caps = self.rpc('get_caps')
        self.assertEqual((caps['extras'], caps['helper_retry_available'], caps['tdp_available']),
                         (False, False, True))
        self.assertFalse(self.saved()['extras_unlocked'])

    def test_helper_that_names_the_cpu_but_cannot_read_its_power_table_still_offers_extras(self):
        self.bin.unlink()
        self.publish_helper()
        self.info_failure = (b'CPU Family: Strix Point\n'
                             b'Unable to init power metric table: -1, this does not affect adjustments\n')
        self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        self.assertTrue(self.rpc('get_caps')['extras'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlinks')
    def test_helper_directory_that_is_a_symlink_is_refused(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        shutil.rmtree(self.bin.parent)
        self.bin.parent.symlink_to(elsewhere)
        self.publish_helper()
        result = self.rpc('set_extras_unlocked', True)
        self.assertFalse(result['success'])
        self.assertIn('unsafe RyzenAdj directory', result['stderr'])
        self.assertEqual(list(elsewhere.iterdir()), [])

    # Startup and capabilities ----------------------------------------------

    def test_offline_start_never_downloads_and_keeps_saved_extras_intent(self):
        self.seed({'123': {'spl': 40000, 'sppt': 45000, 'fppt': 50000}},
                  extras_unlocked=True, spl=40000, sppt=45000, fppt=50000)
        plugin = self.start()
        self.assertTrue(asyncio.run(plugin.is_ready())['ready'])
        self.assertEqual(self.downloads, [])
        self.assertFalse(asyncio.run(plugin.get_caps())['extras'])
        self.assertEqual(self.ppts(), (35, 37, 45))  # runs inside the firmware range meanwhile
        saved = self.saved()
        # Extras intent is kept; a sustained limit above the firmware ceiling reads as the 35 W that runs.
        self.assertEqual((saved['extras_unlocked'], saved['spl'], saved['fppt']), (True, 35000, 50000))
        self.assertEqual(self.saved('game_profiles')['123']['spl'], 40000)

    def test_loaded_spl_is_bounded_by_the_last_sane_firmware_ceiling(self):
        self.seed(spl=33000, sppt=40000, fppt=45000)
        spl_max = self.firmware / tdp.WMI_ATTRS['spl'] / 'max_value'
        self.assertEqual(tdp._load_settings()['spl'], 33000)  # 35 W before any firmware read
        spl_max.write_text('30')
        self.rpc('get_caps')
        self.assertEqual(tdp._load_settings()['spl'], 30000)
        for broken in ('500', '0', None):
            with self.subTest(firmware_spl_max=broken):
                tdp._firmware_caps = {}
                if broken is None:
                    spl_max.unlink()
                else:
                    spl_max.write_text(broken)
                self.rpc('get_caps')
                self.assertEqual(tdp._load_settings()['spl'], 33000)

    def test_start_without_firmware_or_helper_is_ready_but_reports_tdp_unavailable(self):
        shutil.rmtree(self.firmware)
        self.bin.unlink()
        plugin = self.start()
        self.assertTrue(asyncio.run(plugin.is_ready())['ready'])
        caps = asyncio.run(plugin.get_caps())
        self.assertFalse(caps['tdp_available'])
        self.assertTrue(caps['helper_retry_available'])
        self.assertFalse(tdp._apply_limits(16000, 25000, 30000)['success'])
        self.assertEqual((self.downloads, self.ryzenadj), ([], []))

    def test_caps_tell_missing_firmware_from_missing_helper(self):
        caps = self.rpc('get_caps')
        self.assertEqual((caps['tdp_available'], caps['extras'], caps['tdp_error']), (True, False, ''))
        self.choices.write_text('low-power balanced performance')  # no tunable profile
        self.assertFalse(self.rpc('get_caps')['tdp_available'])
        self.choices.write_text('low-power balanced performance custom')
        shutil.rmtree(self.firmware)
        caps = self.rpc('get_caps')
        self.assertFalse(caps['tdp_available'])
        self.assertTrue(caps['helper_retry_available'])
        self.assertFalse(caps['extras'])
        self.assertIn('unavailable', caps['tdp_error'])
        self.helper_ready()
        caps = self.rpc('get_caps')
        self.assertEqual((caps['tdp_available'], caps['extras'], caps['helper_retry_available']),
                         (True, True, False))

    def test_unsupported_model_writes_nothing_and_says_so(self):
        boost = self.cpu / 'cpufreq/boost'
        boost.parent.mkdir(parents=True)
        boost.write_text('0')
        self.helper_ready()
        self.seed(cpu_boost_enabled=True)
        self.machine = '83Q2'  # a Lenovo outside the supported Legion Go family
        with patch.object(tdp.Plugin, '_ready', False), \
                patch.object(tdp.Plugin, '_setup_error', None):
            self.assertEqual(tdp._apply_limits(15000, 20000, 25000)['stderr'], tdp.UNSUPPORTED_MODEL)
            self.assertFalse(self.rpc('set_cpu_boost', True)['success'])
            caps = self.rpc('get_caps')
            self.assertFalse(caps['tdp_available'])
            self.assertEqual(caps['tdp_error'], tdp.UNSUPPORTED_MODEL)
            self.assertFalse(self.rpc('retry_extras')['success'])
            with tdp._mutation_lock:
                self.assertTrue(tdp._release_controls_locked()['success'])
            plugin = tdp.Plugin()
            asyncio.run(plugin._main())
            self.assertEqual(asyncio.run(plugin.is_ready()),
                             {'ready': False, 'error': tdp.UNSUPPORTED_MODEL})
            self.assertEqual(plugin._tasks, [])
        self.assertEqual(self.downloads, [])
        self.assertEqual((self.profile.read_text(), boost.read_text()), ('balanced', '0'))
        self.assertEqual((self.ppts(), self.ryzenadj), ((10, 10, 10), []))

    # Legion Go family ------------------------------------------------------

    def firmware_range(self, maxes, mins=(5, 5, 5)):
        for key, low, high in zip(tdp.WMI_ATTRS, mins, maxes):
            (self.firmware / tdp.WMI_ATTRS[key] / 'min_value').write_text(str(low))
            (self.firmware / tdp.WMI_ATTRS[key] / 'max_value').write_text(str(high))

    def test_go_2_keeps_its_measured_presets_and_max(self):
        caps = self.rpc('get_caps')
        self.assertEqual(caps['presets'], {
            'silent': {'spl': 8, 'sppt': 15, 'fppt': 20},
            'balanced': {'spl': 16, 'sppt': 25, 'fppt': 30},
            'performance': {'spl': 20, 'sppt': 32, 'fppt': 35},
            'max': {'spl': 35, 'sppt': 37, 'fppt': 45},
        })
        self.assertFalse(caps['presets_estimated'])
        self.assertEqual((caps['min'], caps['std'], caps['max']),
                         (5, {'spl': 35, 'sppt': 37, 'fppt': 45}, {'spl': 35, 'sppt': 37, 'fppt': 45}))

    def test_go_s_uses_its_bios_presets(self):
        self.machine = '83N6'
        self.firmware_range((40, 43, 53))
        caps = self.rpc('get_caps')
        self.assertFalse(caps['presets_estimated'])
        self.assertEqual(caps['presets'], {
            'silent': {'spl': 10, 'sppt': 15, 'fppt': 20},
            'balanced': {'spl': 15, 'sppt': 25, 'fppt': 30},
            'performance': {'spl': 30, 'sppt': 35, 'fppt': 40},
            'max': {'spl': 40, 'sppt': 43, 'fppt': 53},
        })

    def test_go_s_has_no_extras_and_keeps_its_firmware_boosts_above_50w(self):
        for machine in ('83L3', '83N6'):
            with self.subTest(machine=machine):
                self.machine = machine
                self.firmware_range((40, 43, 53))
                self.bin.unlink(missing_ok=True)
                self.seed(extras_unlocked=True)  # saved before, or by hand: never honoured
                caps = self.rpc('get_caps')
                self.assertEqual((caps['extras'], caps['helper_retry_available'], caps['tdp_available']),
                                 (False, False, True))
                self.assertEqual(caps['max'], {'spl': 40, 'sppt': 43, 'fppt': 53})
                self.assertFalse(self.rpc('get_extras_unlocked'))
                result = self.rpc('set_extras_unlocked', True)
                self.assertFalse(result['success'])
                self.assertIn(tdp.EXTRAS_UNAVAILABLE, result['stderr'])
                self.assertFalse(self.rpc('retry_extras')['success'])
                self.assertEqual(self.downloads, [])
                self.helper_ready()  # even a verified helper left on disk is not used
                self.assertTrue(self.rpc('apply_tdp', 45000, 60000, 60000)['success'])
                self.assertEqual(self.ppts(), (40, 43, 53))
                self.assertEqual(self.applies(), [])

    def test_extras_range_is_50w_boosts_on_the_go_and_go_2(self):
        for machine, std, extras in (('83N0', (35, 37, 45), {'spl': 35, 'sppt': 50, 'fppt': 50}),
                                     ('83N1', (35, 37, 45), {'spl': 35, 'sppt': 50, 'fppt': 50}),
                                     ('83E1', (30, 32, 41), {'spl': 30, 'sppt': 50, 'fppt': 50})):
            with self.subTest(machine=machine):
                self.machine = machine
                self.firmware_range(std)
                self.helper_ready()
                self.seed(extras_unlocked=True)
                self.assertEqual(self.rpc('get_caps')['max'], extras)
                self.assertTrue(self.rpc('get_extras_unlocked'))

    def test_machine_type_without_bios_values_scales_the_presets_to_its_firmware(self):
        self.machine = '83N6'
        self.stack.enter_context(patch.dict(tdp.PRESET_TABLE, clear=True))
        self.firmware_range((30, 32, 41))
        caps = self.rpc('get_caps')
        self.assertTrue(caps['presets_estimated'])
        self.assertEqual(caps['presets'], {
            'silent': {'spl': 7, 'sppt': 13, 'fppt': 18},
            'balanced': {'spl': 14, 'sppt': 22, 'fppt': 27},
            'performance': {'spl': 17, 'sppt': 28, 'fppt': 32},
            'max': {'spl': 30, 'sppt': 32, 'fppt': 41},
        })

    def test_go_1_uses_its_bios_presets_and_max_is_its_ceiling(self):
        self.machine = '83E1'
        self.firmware_range((30, 32, 41))
        caps = self.rpc('get_caps')
        self.assertFalse(caps['presets_estimated'])
        self.assertEqual(caps['presets'], {
            'silent': {'spl': 8, 'sppt': 15, 'fppt': 20},
            'balanced': {'spl': 15, 'sppt': 25, 'fppt': 30},
            'performance': {'spl': 20, 'sppt': 32, 'fppt': 35},
            'max': {'spl': 30, 'sppt': 32, 'fppt': 41},
        })
        self.assertTrue(self.rpc('apply_tdp', 30000, 32000, 41000, preset_name='max')['success'])
        self.assertEqual(self.ppts(), (30, 32, 41))  # the firmware path, not the Go 2's 35/37/45
        self.assertEqual(self.applies(), [])

    def test_single_dial_custom_reaches_a_firmware_ceiling_above_the_go_2s(self):
        self.machine = '83N6'  # Go S: 40/43/53 W
        self.firmware_range((40, 43, 53))
        self.seed(advanced_tdp_control=False)
        self.assertTrue(self.rpc('apply_tdp', 40000, 43000, 53000, preset_name='custom')['success'])
        self.assertEqual(self.ppts(), (40, 43, 53))

    def test_presets_and_limits_stay_inside_the_firmware_minimums(self):
        self.firmware_range((35, 37, 45), mins=(10, 16, 18))
        presets = self.rpc('get_caps')['presets']
        self.assertEqual(presets['silent'], {'spl': 10, 'sppt': 16, 'fppt': 20})
        self.assertEqual(presets['balanced'], {'spl': 16, 'sppt': 25, 'fppt': 30})
        self.helper_ready()
        self.assertTrue(tdp._apply_limits(6000, 7000, 8000)['success'])
        self.assertEqual(self.ppts(), (10, 16, 18))  # raised to the firmware's floor, not ryzenadj
        self.assertEqual(self.applies(), [])

    def test_console_without_firmware_attributes_has_no_tdp_and_never_runs_ryzenadj(self):
        self.machine = '83E1'
        shutil.rmtree(self.firmware)
        self.helper_ready()
        caps = self.rpc('get_caps')
        self.assertEqual((caps['tdp_available'], caps['extras'], caps['helper_retry_available'],
                          caps['tdp_error']), (False, False, False, tdp.NO_FIRMWARE_TDP))
        self.seed(extras_unlocked=True)
        self.assertFalse(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertFalse(tdp._apply_limits(15000, 45000, 50000)['success'])
        with tdp._mutation_lock:
            self.assertTrue(tdp._release_controls_locked()['success'])
        self.assertEqual(self.ryzenadj, [])

    def test_cpu_boost_and_epp_work_on_the_whole_family(self):
        self.machine = '83E1'
        policy = self.cpu / 'cpufreq/policy0'
        policy.mkdir(parents=True)
        (policy / 'boost').write_text('0')
        (policy / 'energy_performance_preference').write_text('balance_performance')
        (policy / 'energy_performance_available_preferences').write_text(
            'default performance balance_performance balance_power power')
        self.assertTrue(self.rpc('set_cpu_boost', True)['success'])
        self.assertTrue(self.rpc('set_epp', 'power')['success'])
        self.assertEqual(((policy / 'boost').read_text(), (policy / 'energy_performance_preference').read_text()),
                         ('1', 'power'))

    def test_firmware_attributes_are_found_whatever_the_instance_index(self):
        instance = self.root / 'firmware-attributes/lenovo-wmi-other-1/attributes'
        instance.parent.mkdir(parents=True)
        shutil.move(str(self.firmware), str(instance))
        self.stack.enter_context(patch.object(
            tdp, 'WMI_ROOT', str(self.root / 'firmware-attributes/lenovo-wmi-other-*/attributes')))
        self.assertTrue(self.rpc('get_caps')['wmi'])
        self.assertTrue(tdp._apply_limits(18000, 22000, 30000)['success'])
        self.assertEqual((instance / tdp.WMI_ATTRS['fppt'] / 'current_value').read_text(), '30')

    def test_locking_extras_clamps_saved_profiles_and_reapplies(self):
        self.helper_ready()
        self.seed({'123': {'spl': 45000, 'sppt': 48000, 'fppt': 50000, 'preset': 'max',
                           'ac_separate': True, 'ac_spl': 30000, 'ac_sppt': 40000,
                           'ac_fppt': 50000, 'ac_preset': 'fast'}},
                  extras_unlocked=True, spl=40000, sppt=45000, fppt=50000,
                  active_preset='mine')
        self.assertTrue(self.rpc('set_extras_unlocked', False)['success'])
        saved = self.saved()
        self.assertEqual((saved['spl'], saved['sppt'], saved['fppt'], saved['active_preset']),
                         (35000, 37000, 45000, 'custom'))
        game = self.saved('game_profiles')['123']
        self.assertEqual((game['spl'], game['sppt'], game['fppt'], game['preset']),
                         (35000, 37000, 45000, 'custom'))
        self.assertEqual((game['ac_spl'], game['ac_sppt'], game['ac_fppt'], game['ac_preset']),
                         (30000, 37000, 45000, 'custom'))
        self.assertEqual(self.ppts(), (35, 37, 45))

    # Rollback when a change cannot be saved --------------------------------

    def test_apply_that_cannot_be_saved_puts_the_previous_limits_back(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 18000, 25000)['success'])
        with self.disk_full(), self.assertRaisesRegex(RuntimeError, 'previous target restored'):
            self.rpc('apply_tdp', 8000, 10000, 15000)
        self.assertEqual(self.ppts(), (15, 18, 25))
        self.assertEqual(self.saved()['spl'], 15000)

    def test_profile_changes_that_cannot_be_saved_put_the_game_limits_back(self):
        self.app, self.ac = '123', True
        for method, args in (('delete_game_profile', ('123',)),
                             ('set_game_ac_profile', ('123', 20000, 23000, 30000, True)),
                             ('set_extras_unlocked', (False,))):
            with self.subTest(method=method):
                self.seed({'123': {'spl': 25000, 'sppt': 28000, 'fppt': 35000}}, extras_unlocked=True)
                tdp._current_game_id = ''
                tdp._check_and_enforce()
                self.assertEqual(self.ppts(), (25, 28, 35))
                with self.disk_full(), self.assertRaisesRegex(RuntimeError, 'previous target restored'):
                    self.rpc(method, *args)
                self.assertEqual(self.ppts(), (25, 28, 35))
                self.assertIn('123', self.saved('game_profiles'))

    # Enforce loop ----------------------------------------------------------

    def test_game_switch_applies_profile_and_exit_restores_global(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (22, 26, 32))
        self.app = ''
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (15, 20, 25))
        saved = self.saved()
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (15000, 20000, 25000))

    def test_failed_game_transition_retries_the_game_target_not_the_stale_global(self):
        self.seed({'123': {'spl': 25000, 'sppt': 28000, 'fppt': 35000}},
                  active_spl=15000, active_sppt=20000, active_fppt=25000)
        self.app = '123'
        with self.firmware_ignores_writes():  # the device is still waking
            for _ in range(2):
                tdp._check_and_enforce()
                tdp._transition_retry_at = 0.0  # the wake backoff has passed
        self.assertEqual(self.ppts(), (10, 10, 10))
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (25, 28, 35))

    # Separate battery presets -----------------------------------------------

    def choose(self, preset, app_id=''):
        """Press a preset as both UIs do: its charger values and its name."""
        values = self.rpc('get_caps')['presets'][preset]
        result = self.rpc('apply_tdp', *(values[key] * 1000 for key in ('spl', 'sppt', 'fppt')),
                          app_id, preset_name=preset)
        self.assertTrue(result['success'], result)

    def test_presets_run_the_charger_values_on_battery_unless_separate_battery_presets_is_on(self):
        for machine, maxes, preset, ac, battery in (
                ('83N0', (35, 37, 45), 'balanced', (16, 25, 30), (16, 16, 30)),
                ('83N1', (35, 37, 45), 'silent', (8, 15, 20), (8, 8, 20)),
                ('83E1', (30, 32, 41), 'balanced', (15, 25, 30), (15, 15, 30)),
                ('83N6', (40, 43, 53), 'performance', (30, 35, 40), (25, 25, 35)),
                ('83L3', (40, 43, 53), 'silent', (10, 15, 20), (10, 10, 20))):
            with self.subTest(machine=machine):
                self.machine, self.ac = machine, False
                self.firmware_range(maxes)
                self.seed()
                tdp._current_ac_online = False
                self.choose(preset)
                self.assertEqual(self.ppts(), ac)  # off (the default): as before, on battery too
                self.assertTrue(self.rpc('set_separate_battery_presets', True)['success'])
                self.assertEqual(self.ppts(), battery)
                self.assertTrue(self.saved()['separate_battery_presets'])
                self.assertEqual(self.rpc('get_settings')['battery_preset'], preset)
                self.ac = True
                events = tdp._check_and_enforce()
                self.assertIn('_resettle_generation', events)  # the charger ladder re-asserts it
                self.assertEqual(self.ppts(), ac)
                self.assertEqual(self.rpc('get_settings')['battery_preset'], '')
                self.ac = False
                tdp._check_and_enforce()
                self.assertEqual(self.ppts(), battery)
                self.assertTrue(self.rpc('set_separate_battery_presets', False)['success'])
                self.assertEqual(self.ppts(), ac)
                # The saved preset keeps its charger values and name either way.
                saved = self.saved()
                self.assertEqual((saved['spl'] // 1000, saved['sppt'] // 1000, saved['fppt'] // 1000,
                                  saved['active_preset']), (*ac, preset))

    def test_pressing_a_preset_on_battery_runs_its_battery_values_at_once(self):
        self.seed(separate_battery_presets=True)
        self.choose('performance')
        self.assertEqual(self.ppts(), (20, 20, 35))
        self.ac = True
        self.choose('silent')
        self.assertEqual(self.ppts(), (8, 15, 20))

    def test_separate_battery_presets_leave_max_and_custom_alone(self):
        self.seed(separate_battery_presets=True)
        self.choose('max')
        self.assertEqual(self.ppts(), (35, 37, 45))
        self.assertTrue(self.rpc('apply_tdp', 16000, 25000, 30000, preset_name='custom')['success'])
        self.assertEqual(self.ppts(), (16, 25, 30))  # Balanced's values, but chosen as Custom
        self.assertTrue(self.rpc('apply_tdp', 12000, 18000, 22000)['success'])
        self.assertEqual(self.ppts(), (12, 18, 22))

    def test_game_profiles_follow_separate_battery_presets(self):
        self.seed({'123': {'spl': 8000, 'sppt': 15000, 'fppt': 20000, 'preset': 'silent'},
                   '456': {'spl': 16000, 'sppt': 25000, 'fppt': 30000, 'preset': 'balanced',
                           'ac_separate': True, 'ac_spl': 20000, 'ac_sppt': 32000, 'ac_fppt': 35000,
                           'ac_preset': 'performance'}},
                  separate_battery_presets=True)
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (8, 8, 20))
        self.app = '456'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (16, 16, 30))
        self.ac = True
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (20, 32, 35))  # its charger preset, with charger values
        self.app, self.ac = '123', False
        tdp._check_and_enforce()
        self.choose('balanced', '123')
        self.assertEqual(self.ppts(), (16, 16, 30))
        self.assertEqual(self.saved('game_profiles')['123']['sppt'], 25000)

    def test_battery_presets_stay_inside_the_firmware_range(self):
        self.firmware_range((35, 37, 45), mins=(10, 12, 18))
        self.assertEqual(self.rpc('get_caps')['battery_presets'], {
            'silent': {'spl': 10, 'sppt': 12, 'fppt': 20},
            'balanced': {'spl': 16, 'sppt': 16, 'fppt': 30},
            'performance': {'spl': 20, 'sppt': 20, 'fppt': 35},
            'max': {'spl': 35, 'sppt': 37, 'fppt': 45},
        })
        self.seed(separate_battery_presets=True)
        self.choose('silent')
        self.assertEqual(self.ppts(), (10, 12, 20))

    def test_game_ac_profile_follows_the_charger(self):
        self.seed({'123': {'spl': 12000, 'sppt': 15000, 'fppt': 20000, 'ac_separate': True,
                           'ac_spl': 30000, 'ac_sppt': 32000, 'ac_fppt': 40000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (12, 15, 20))
        self.ac = True
        events = tdp._check_and_enforce()
        self.assertEqual(events['power_source'], {'ac': True})
        self.assertEqual(self.ppts(), (30, 32, 40))

    def test_charger_edge_survives_a_failed_settings_write(self):
        self.seed({'123': {'spl': 12000, 'sppt': 15000, 'fppt': 20000, 'ac_separate': True,
                           'ac_spl': 30000, 'ac_sppt': 32000, 'ac_fppt': 40000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.ac = True
        with self.disk_full():
            with self.assertRaises(OSError):
                tdp._check_and_enforce()  # limits written, then the save fails
            # Backed off: the next pass reports the charger but makes no second attempt.
            events = tdp._check_and_enforce()
        self.assertEqual(events['power_source'], {'ac': True})
        tdp._transition_retry_at = 0.0  # the backoff has passed
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (30, 32, 40))
        saved = self.saved()
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (30000, 32000, 40000))

    def test_charger_ladder_reasserts_after_firmware_reverts_and_stops_when_settled(self):
        tdp._check_and_enforce()
        self.ac = True
        events = tdp._check_and_enforce()
        generation = events['_resettle_generation']
        self.assertEqual(self.ppts(), (15, 20, 25))
        # The firmware applies its own profile a moment after the transition.
        self.profile.write_text('low-power')
        self.set_ppts(10, 15, 20)
        self.profile_writes.clear()
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (15, 20, 25))
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.profile_writes.clear()
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.profile_writes, [])

    def test_game_switch_right_after_plugging_in_restarts_the_charger_ladder(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        tdp._check_and_enforce()
        self.ac = True
        first = tdp._check_and_enforce()['_resettle_generation']
        self.app = '123'
        events = tdp._check_and_enforce()
        self.assertNotIn('power_source', events)
        generation = events['_resettle_generation']
        self.assertNotEqual(generation, first)
        self.set_ppts(10, 15, 20)  # the firmware's late AC write
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (22, 26, 32))

    def test_stale_charger_ladder_never_reaches_hardware(self):
        tdp._check_and_enforce()
        self.ac = True
        generation = tdp._check_and_enforce()['_resettle_generation']
        self.assertTrue(self.rpc('apply_tdp', 20000, 25000, 30000)['success'])
        self.set_ppts(10, 15, 20)
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (10, 15, 20))

    def test_drift_is_reasserted_then_bounded(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.ppts(), (15, 20, 25))
        self.profile.write_text('balanced')  # something knocked us off custom
        tdp._check_and_enforce()
        self.assertEqual(self.profile.read_text(), 'custom')
        tdp._check_and_enforce()  # the next tick confirms it, refilling the retry budget
        self.profile_writes.clear()
        with self.firmware_ignores_writes():
            self.set_ppts(8, 8, 8)
            for _ in range(tdp.DRIFT_MAX_ATTEMPTS + 3):
                tdp._check_and_enforce()
        self.assertEqual(self.bounces(), tdp.DRIFT_MAX_ATTEMPTS)

    def test_unreachable_target_with_a_persistent_live_mismatch_stays_bounded(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.helper_ready()  # enables the live cross-check
        self.profile_writes.clear()
        clock = [100.0]
        with patch.object(tdp.time, 'monotonic', side_effect=lambda: clock[0]), \
                self.firmware_ignores_writes():
            # 5 s passes; the live cross-check only reads every 30 s (True, None, None, ...).
            for seconds in range(0, 106, 5):
                clock[0] = 100.0 + seconds
                tdp._enforce_target((25000, 30000, 35000))
            self.assertEqual(self.bounces(), tdp.DRIFT_MAX_ATTEMPTS)
            self.set_ppts(15, 22, 27)  # the hardware really moved: a fresh budget
            clock[0] += 30
            tdp._enforce_target((25000, 30000, 35000))
            self.assertEqual(self.bounces(), tdp.DRIFT_MAX_ATTEMPTS + 1)

    def test_disabled_module_writes_no_limits(self):
        self.seed(enabled=False)
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.profile_writes, [])
        self.assertEqual(self.ppts(), (10, 10, 10))

    def test_unchanged_heartbeat_while_disabled_runs_no_pass(self):
        self.seed({'222': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}}, enabled=False)
        self.report_games_from_frontend()
        tdp._current_game_id = '\x00'  # what startup leaves with TDP control off
        with patch.object(tdp, '_check_and_enforce', wraps=tdp._check_and_enforce) as enforce:
            for _ in range(3):
                self.rpc('set_active_app', '123')
        self.assertEqual(enforce.call_count, 1)
        self.assertEqual(self.ppts(), (10, 10, 10))
        self.assertTrue(self.rpc('set_plugin_enabled', True)['success'])
        self.rpc('set_active_app', '222')
        self.assertEqual(self.ppts(), (22, 26, 32))

    def report_games_from_frontend(self):
        """Resolve the game from set_active_app reports, as on the device."""
        for name, value in {'_get_running_appid': REAL_RUNNING_APPID,
                            'scan_proc_for_appid': lambda: '',
                            '_frontend_appid': '', '_frontend_appid_ts': None}.items():
            self.stack.enter_context(patch.object(tdp, name, value))

    def test_reported_game_switch_applies_within_the_call(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')  # no enforce loop runs in this test
        self.assertEqual(self.ppts(), (22, 26, 32))
        self.profile_writes.clear()
        self.rpc('set_active_app', '123')  # the 2 s heartbeat for the same game
        self.assertEqual(self.profile_writes, [])
        self.rpc('set_active_app', '')
        self.assertEqual(self.ppts(), (15, 20, 25))
        saved = self.saved()
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (15000, 20000, 25000))

    def test_reported_game_profile_is_clamped_without_extras(self):
        self.seed({'123': {'spl': 45000, 'sppt': 48000, 'fppt': 50000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')
        self.assertEqual(self.ppts(), (35, 37, 45))

    def test_edit_for_the_previous_game_is_refused_after_a_reported_switch(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000},
                   '222': {'spl': 12000, 'sppt': 15000, 'fppt': 20000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')
        self.rpc('set_active_app', '222')
        self.assertEqual(self.ppts(), (12, 15, 20))
        stale = self.rpc('apply_tdp', 30000, 30000, 30000, '123', '', '123')
        self.assertEqual((stale['success'], stale['stderr']), (False, 'foreground game changed'))
        self.assertEqual(self.ppts(), (12, 15, 20))
        self.assertEqual(self.saved('game_profiles')['123']['spl'], 22000)

    # Panel telemetry -------------------------------------------------------

    def test_panel_shows_only_a_fresh_hardware_sample(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        clock = [1000.0]
        with patch.object(tdp.time, 'monotonic', side_effect=lambda: clock[0]):
            tdp._refresh_info_cache()
            clock[0] += 1
            fresh = self.rpc('get_tdp_info')
            self.assertEqual((fresh['values']['spl_limit'], fresh['sample_age_seconds']), (15, 1))
            clock[0] += 100  # the info loop stopped
            self.assertEqual(self.rpc('get_tdp_info')['values'], {})

    def test_failed_power_and_limit_reads_leave_no_old_sample(self):
        rapl = self.root / 'powercap/intel-rapl-0'
        rapl.mkdir(parents=True)
        (rapl / 'name').write_text('package-0')
        energy = rapl / 'energy_uj'
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        clock = [1000.0]
        with patch.object(tdp.time, 'monotonic', side_effect=lambda: clock[0]):
            for joules in (1, 13):  # 12 J in one second
                energy.write_text(str(joules * 1_000_000))
                tdp._refresh_info_cache()
                clock[0] += 1
            values = self.rpc('get_tdp_info')['values']
            self.assertEqual((values['package_draw'], values['spl_limit']), (12.0, 15))
            energy.unlink()
            for attr in tdp.WMI_ATTRS.values():
                (self.firmware / attr / 'current_value').unlink()
            tdp._refresh_info_cache()
            values = self.rpc('get_tdp_info')['values']
        self.assertNotIn('package_draw', values)
        self.assertNotIn('spl_limit', values)

    # Release ---------------------------------------------------------------

    def test_release_returns_platform_profile_to_balanced(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.profile.read_text(), 'custom')
        self.release()
        self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertEqual(self.applies(), [])

    def test_release_uses_balanced_without_the_helper_when_a_ppt_cap_is_missing(self):
        self.helper_ready()
        (self.firmware / tdp.WMI_ATTRS['fppt'] / 'max_value').unlink()
        for operation in ('uninstall', 'module_disable'):
            with self.subTest(operation=operation):
                self.profile.write_text('custom')
                if operation == 'uninstall':
                    asyncio.run(self.plugin._uninstall())
                else:
                    asyncio.run(module_control.withdraw('tdp', self.plugin))
                self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertEqual(self.ryzenadj, [])

    def test_release_is_not_a_success_until_balanced_reads_back(self):
        self.helper_ready()
        self.profile_sticks = False
        for case in ('firmware attributes present', 'firmware attributes missing'):
            with self.subTest(case):
                self.profile.write_text('custom')
                with self.assertRaisesRegex(RuntimeError, 'Balanced firmware profile was not confirmed'):
                    asyncio.run(self.plugin._uninstall())
                self.assertEqual(self.profile.read_text(), 'custom')
                shutil.rmtree(self.firmware, ignore_errors=True)
        self.assertEqual(self.ryzenadj, [])

    def test_release_restores_managed_cpu_controls(self):
        boost, epp = self.cpu_controls()
        self.seed(cpu_boost_enabled=False, epp='power')
        self.release()
        self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertEqual(boost.read_text(), '1')
        self.assertEqual(epp.read_text(), 'balance_performance')

    # Baseline (what release restores) ---------------------------------------

    def cpu_controls(self, boost='0', epp='power'):
        boost_path = self.cpu / 'cpufreq/boost'
        boost_path.parent.mkdir(parents=True, exist_ok=True)
        boost_path.write_text(boost)
        policy = self.cpu / 'cpufreq/policy0'
        policy.mkdir(exist_ok=True)
        (policy / 'energy_performance_available_preferences').write_text(
            'performance balance_performance balance_power power')
        epp_path = policy / 'energy_performance_preference'
        epp_path.write_text(epp)
        return boost_path, epp_path

    def release(self):
        with tdp._mutation_lock:
            self.assertTrue(tdp._release_controls_locked()['success'])

    def test_fresh_install_restores_what_it_found_before_the_first_write(self):
        boost, epp = self.cpu_controls()
        self.profile.write_text('performance')
        self.store.replace({})
        tdp._migrate()
        self.assertEqual(self.store.getSetting('baseline'), {'pending': True})
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertTrue(self.rpc('set_cpu_boost', True)['success'])
        self.assertTrue(self.rpc('set_epp', 'balance_power')['success'])
        self.assertEqual(self.store.getSetting('baseline'), {
            'platform_profile': 'performance', 'cpu_boost_enabled': False, 'epp': 'power'})
        self.release()
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('performance', '0', 'power'))

    def test_existing_install_captures_only_after_the_next_boot(self):
        boot = self.root / 'boot_id'
        boot.write_text('boot-1')
        self.stack.enter_context(patch.object(tdp, 'BOOT_ID_PATH', str(boot)))
        boost, epp = self.cpu_controls('1', 'balance_power')  # set by the old version
        self.seed(cpu_boost_enabled=True, epp='balance_power')
        tdp._migrate()
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.store.getSetting('baseline'), {'pending': True, 'after_boot': 'boot-1'})
        self.release()  # nothing known yet: today's defaults
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('balanced', '1', 'balance_performance'))
        boot.write_text('boot-2')  # the reboot reset the controls
        self.profile.write_text('custom')
        boost.write_text('0')
        epp.write_text('power')
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.store.getSetting('baseline'), {
            'platform_profile': None, 'cpu_boost_enabled': False, 'epp': 'power'})
        self.release()
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('balanced', '0', 'power'))

    def test_release_before_any_write_restores_the_system_value_it_found(self):
        boot = self.root / 'boot_id'
        boot.write_text('boot-1')
        self.stack.enter_context(patch.object(tdp, 'BOOT_ID_PATH', str(boot)))
        boost, _epp = self.cpu_controls('1', 'balance_power')
        self.seed({'123': {'spl': 15000, 'sppt': 20000, 'fppt': 25000,
                           'cpu_boost_enabled': True}}, enabled=False)
        tdp._migrate()
        boot.write_text('boot-2')  # rebooted: the system default boost is off, nothing written yet
        boost.write_text('0')
        self.release()
        self.assertEqual(boost.read_text(), '0')

    def test_invalid_saved_profile_falls_back_to_balanced(self):
        self.store.replace({'settings': {'enabled': True}, 'schema_version': 2,
                            'baseline': {'platform_profile': 'turbo'}})
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.release()
        self.assertEqual(self.profile.read_text(), 'balanced')


if __name__ == '__main__':
    unittest.main()
