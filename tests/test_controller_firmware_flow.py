# SPDX-License-Identifier: BSD-3-Clause
"""Check / Download / Install state machine, sysfs reader and fwupd engine.

Every subprocess is mocked: nothing here runs fwupd or touches a controller.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # noqa: F401  (Decky stub for main)
import controller_firmware as fw
import controller_firmware_catalog as catalog
import main
import module_runtime
from test_controller_firmware_catalog import result

INSTALLED = {'left': '260310A', 'right': '260310A', 'receiver': '260422A'}


def feed(left='v26.04.01.01'):
    value = result()
    value['candidates']['left'].update(feed_version=left, version=catalog.raw_version(left))
    return value


def image(component, version):
    release = replace(fw.release_for(component), version=version)
    return fw.ValidatedImage(release, component.encode() * 200)


def validated(component, data, release=None):
    return fw.ValidatedImage(replace(release, size=len(data), sha256=hashlib.sha256(data).hexdigest()), data)


def devices(**versions):
    rows = []
    for component, instance in fw.FWUPD_INSTANCES.items():
        rows.append({'Name': component, 'DeviceId': hashlib.sha1(component.encode()).hexdigest(),
            'InstanceIds': [instance], 'Plugin': 'legion_hid', 'Flags': ['internal', 'updatable'],
            'Version': versions.get(component, INSTALLED[component])})
    return {component: {'id': row['DeviceId'], 'version': row['Version']}
        for component, row in zip(fw.FWUPD_INSTANCES, rows)}, rows


class Service:
    """A FirmwarePreparation with a scripted reader and synchronous waits."""
    def __init__(self, readings=None):
        self.readings = readings or []
        self.pads = {'left': 100, 'right': 100, 'connection_left': 'attached', 'connection_right': 'attached'}
        self.model = 'Legion Go 8ASP2'
        self.service = fw.FirmwarePreparation(reader=self.read, model=lambda: self.model, pads=lambda: self.pads,
                                              sleep=lambda _seconds: None)

    def read(self):
        if self.readings:
            value = self.readings.pop(0)
            if isinstance(value, Exception):
                raise value
            return dict(value)
        return dict(INSTALLED)

    def run(self, method, *args):
        status = getattr(self.service, method)(*args)
        if self.service._thread is not None:
            self.service._thread.join(5)
        return status, self.service.status()


class FlowTests(unittest.TestCase):
    def downloaded(self, left='v26.04.01.01'):
        state = Service()
        with patch.object(catalog, 'check_catalog', return_value=feed(left)):
            state.run('check')
        with patch.object(fw, 'download_image', side_effect=lambda r, c: r.component.encode() * 200), \
                patch.object(fw, 'validate_image', side_effect=validated):
            state.run('download')
        return state

    def test_check_reports_versions_and_only_newer_components(self):
        state = Service()
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            _, status = state.run('check')
        self.assertEqual(status['phase'], 'available')
        rows = {row['id']: row for row in status['components']}
        self.assertEqual((rows['left']['installed'], rows['left']['available'], rows['left']['newer']), ('260310A', '260401A', True))
        self.assertFalse(rows['right']['newer'] or rows['receiver']['newer'])
        self.assertEqual(status['token'], '')
        with patch.object(catalog, 'check_catalog', return_value=result()):
            _, status = state.run('check')
        self.assertEqual(status['phase'], 'up_to_date')
        status['components'][0]['installed'] = 'fake'
        self.assertEqual(state.service.status()['components'][0]['installed'], '260310A')

    def test_check_fails_closed_on_partial_feed_or_changed_versions(self):
        partial = result(); partial['candidates']['receiver'] = None
        for readings, value in (([], partial), ([INSTALLED, dict(INSTALLED, left='260311A')], result()),
                                ([fw.FirmwareError('The supported controllers are not connected.')], result())):
            state = Service(list(readings))
            with patch.object(catalog, 'check_catalog', return_value=value):
                _, status = state.run('check')
            self.assertEqual(status['phase'], 'failed')
            self.assertTrue(status['error'])
            self.assertEqual(status['checked_at'], '')

    def test_check_asks_the_feed_for_this_console_and_nothing_off_the_model_gate(self):
        state = Service()
        state.model = 'Legion Go 8AHP2'
        with patch.object(catalog, 'check_catalog', return_value=result()) as check:
            _, status = state.run('check')
        self.assertEqual((status['phase'], check.call_args.args[1]), ('up_to_date', 'Legion Go 8AHP2'))
        state.model = None
        with patch.object(catalog, 'check_catalog') as check:
            _, status = state.run('check')
        check.assert_not_called()
        self.assertEqual(status['phase'], 'failed')

    def test_model_gate_admits_the_two_go_2_machine_types_only(self):
        with tempfile.TemporaryDirectory() as raw:
            dmi = Path(raw) / 'class/dmi/id'
            dmi.mkdir(parents=True)
            for vendor, model, version, expected in (
                    ('LENOVO', '83N0', 'Legion Go 8ASP2', 'Legion Go 8ASP2'),
                    ('LENOVO', '83N1', 'Legion Go 8AHP2', 'Legion Go 8AHP2'),
                    ('LENOVO', '83N1', 'Legion Go 8ASP2', None), ('LENOVO', '83E1', 'Legion Go 8APU1', None),
                    ('OTHER', '83N0', 'Legion Go 8ASP2', None)):
                for name, value in (('sys_vendor', vendor), ('product_name', model), ('product_version', version)):
                    (dmi / name).write_text(value + '\n')
                with self.subTest(model=model, version=version):
                    self.assertEqual(fw.machine_name(Path(raw)), expected)
                    self.assertIs(fw.applies(Path(raw)), expected is not None)
            # DMI that is not UTF-8 (a foreign PC): no console, and no crash at plugin start.
            with patch.object(fw.Path, 'read_text', side_effect=UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')):
                self.assertFalse(fw.applies(Path(raw)))

    def test_download_needs_a_fresh_available_check_and_fetches_all_three_once(self):
        state = Service()
        with self.assertRaises(fw.FirmwareError):
            state.service.download()
        with patch.object(catalog, 'check_catalog', return_value=result()):
            state.run('check')
        with self.assertRaises(fw.FirmwareError):
            state.service.download()  # Up to date: nothing to download.
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            state.run('check')
        with patch.object(fw.time, 'monotonic', return_value=state.service._check['at'] + fw.CHECK_MAX_AGE + 1):
            with self.assertRaises(fw.FirmwareError):
                state.service.download()
        # A stale check ends in `failed`, from which both UIs offer Check, instead of looping on Download.
        self.assertEqual(state.service.status()['phase'], 'failed')
        self.assertIn('out of date', state.service.status()['error'])
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            state.run('check')
        calls = []
        with patch.object(fw, 'download_image', side_effect=lambda r, c: calls.append(r.component) or b'x' * 700), \
                patch.object(fw, 'validate_image', side_effect=validated) as validate:
            _, status = state.run('download')
        self.assertEqual(sorted(calls), sorted(fw.RELEASES))
        self.assertEqual(validate.call_count, 3)
        self.assertEqual(status['phase'], 'downloaded')
        self.assertRegex(status['token'], '^[0-9a-f]{64}$')
        self.assertEqual([row['downloaded'] for row in status['components']], [True, False, False])

    def test_failed_download_discards_images_and_allows_a_new_check(self):
        state = Service()
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            state.run('check')
        with patch.object(fw, 'download_image', return_value=b'x' * 700), \
                patch.object(fw, 'validate_image', side_effect=fw.FirmwareError('The Lenovo firmware signature is not valid.')):
            _, status = state.run('download')
        self.assertEqual((status['phase'], status['error']), ('failed', 'The Lenovo firmware signature is not valid.'))
        self.assertIsNone(state.service._images)
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            _, status = state.run('check')
        self.assertEqual(status['phase'], 'available')

    def test_install_is_enabled(self):
        self.assertTrue(fw.INSTALL_ENABLED)
        self.assertTrue(self.downloaded().service.status()['install_enabled'])

    @patch.object(fw, 'INSTALL_ENABLED', False)
    def test_kill_switch_never_runs_fwupd(self):
        state = self.downloaded()
        status = state.service.status()
        self.assertFalse(status['install_enabled']); self.assertTrue(status['install_reason'])
        done = Mock()
        with patch.object(fw, '_run_tool') as tool:
            with self.assertRaises(fw.FirmwareError):
                state.service.admit_install(status['token'])
            with self.assertRaises(fw.FirmwareError):
                state.service.install(status['token'], done)
        tool.assert_not_called(); done.assert_called_once()
        self.assertEqual(state.service.status()['phase'], 'downloaded')

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_install_needs_the_confirmed_token_of_the_current_download(self):
        state = self.downloaded()
        token = state.service.status()['token']
        stale = 'no longer matches the download'  # the token refuses, not the host's power supply
        for wrong in ('', None, 'x' * 64, token.upper()):
            with self.assertRaisesRegex(fw.FirmwareError, stale):
                state.service.admit_install(wrong)
        # A new check replaces the download; the old confirmation is stale.
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            state.run('check')
        with self.assertRaises(fw.FirmwareError):
            state.service.admit_install(token)
        state = self.downloaded()
        token = state.service.status()['token']
        with patch.object(fw.time, 'monotonic', return_value=state.service._check['at'] + fw.CHECK_MAX_AGE + 1):
            with self.assertRaises(fw.FirmwareError):
                state.service.admit_install(token)
        status = state.service.status()
        self.assertEqual(status['phase'], 'failed')
        self.assertEqual((status['token'], [row['downloaded'] for row in status['components']]), ('', [False] * 3))

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_install_writes_only_newer_devices_then_verifies_with_retries(self):
        state = self.downloaded()
        token = state.service.status()['token']
        found, _ = devices()
        new = dict(INSTALLED, left='260401A')
        # Preflight read, then the controller is still restarting twice before it reports the new version.
        state.readings = [INSTALLED, fw.FirmwareError('restarting'), INSTALLED, new]
        done = Mock()
        containers = []
        def blob(container, device_id, log):
            containers.append(container.read_bytes())
            return 0, 42.0, 'Done'
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                patch.object(fw, 'install_blob', side_effect=blob) as install:
            _, status = state.run('install', token, done)
        self.assertEqual(status['phase'], 'installed', status['error'])
        install.assert_called_once()
        self.assertEqual(install.call_args.args[1], found['left']['id'])
        done.assert_called_once()
        self.assertEqual({row['id']: row['installed'] for row in status['components']}, new)
        self.assertEqual(status['token'], '')
        sizes = struct.unpack_from('<6I', containers[0])
        self.assertEqual(sizes[2:4], (len(b'left' * 200), 0x0260401A))

    def test_one_install_updates_every_newer_component_in_order(self):
        state = Service()
        newer = result()
        for component, version in (('left', 'v26.04.01.01'), ('right', 'v26.04.01.02'), ('receiver', 'v26.05.01.01')):
            newer['candidates'][component].update(feed_version=version, version=catalog.raw_version(version))
        with patch.object(catalog, 'check_catalog', return_value=newer):
            state.run('check')
        with patch.object(fw, 'download_image', side_effect=lambda r, c: r.component.encode() * 200), \
                patch.object(fw, 'validate_image', side_effect=validated):
            state.run('download')
        target = {row['id']: row['available'] for row in state.service.status()['components']}
        current = dict(INSTALLED)
        state.service._reader = lambda: dict(current)
        written = []
        def blob(container, device_id, log):
            component = next(c for c, d in devices()[0].items() if d['id'] == device_id)
            written.append(component); current[component] = target[component]
            return 0, 40.0, 'Done'
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', side_effect=lambda: devices(**current)[0]), \
                patch.object(fw, 'updater_running', return_value=False), patch.object(fw, 'install_blob', side_effect=blob):
            _, status = state.run('install', state.service.status()['token'], Mock())
        self.assertEqual(status['phase'], 'installed', status['error'])
        self.assertEqual(written, ['left', 'right', 'receiver'])
        self.assertEqual({row['id']: row['installed'] for row in status['components']}, target)

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_install_refuses_foreign_updater_mismatch_and_changed_versions_before_writing(self):
        found, _ = devices()
        mismatch, _ = devices(receiver='251208A')
        for running, listed, readings in ((True, found, []), (False, mismatch, []),
                                          (False, found, [dict(INSTALLED, right='260311A')])):
            state = self.downloaded()
            token = state.service.status()['token']
            state.readings = list(readings)
            with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                    patch.object(fw, 'fwupd_devices', return_value=listed), \
                    patch.object(fw, 'updater_running', return_value=running), \
                    patch.object(fw, 'install_blob') as install:
                _, status = state.run('install', token, Mock())
            install.assert_not_called()
            self.assertEqual(status['phase'], 'failed')

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_install_needs_both_pads_attached_and_the_updated_pad_charged(self):
        found, _ = devices()
        attached = {'left': 100, 'right': 100, 'connection_left': 'attached', 'connection_right': 'attached'}
        for pads, error in ((dict(attached, connection_right='detached'), 'Attach both controllers'),  # right is not updated
                            (dict(attached, connection_left='connecting'), 'Attach both controllers'),
                            (None, 'Attach both controllers'),
                            (dict(attached, left=29), 'Left controller: attach'), (dict(attached, left=None), 'Left controller: attach')):
            state = self.downloaded()
            token = state.service.status()['token']
            state.pads = pads
            with self.subTest(pads=pads), patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                    patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                    patch.object(fw, 'install_blob') as install, self.assertRaisesRegex(fw.FirmwareError, error):
                state.service.install(token, Mock())
            install.assert_not_called()
            self.assertEqual(state.service.status()['phase'], 'downloaded')  # The verified download stays for a retry.

    def test_check_and_download_need_both_pads_attached(self):
        detached = {'left': 100, 'right': 100, 'connection_left': 'attached', 'connection_right': 'detached'}
        state = Service()
        state.pads = detached
        with patch.object(catalog, 'check_catalog') as check, \
                self.assertRaisesRegex(fw.FirmwareError, '^Attach both controllers to check or install firmware updates.$'):
            state.service.check()
        check.assert_not_called()
        self.assertEqual(state.service.status()['phase'], 'idle')
        state.pads = dict(detached, connection_right='attached')
        with patch.object(catalog, 'check_catalog', return_value=feed()):
            state.run('check')
        state.pads = detached
        with patch.object(fw, 'download_image') as download, self.assertRaisesRegex(fw.FirmwareError, 'Attach both'):
            state.service.download()
        download.assert_not_called()
        self.assertEqual(state.service.status()['phase'], 'available')  # Download again once both are attached.

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_pad_detached_after_admission_is_refused_before_its_write(self):
        found, _ = devices()
        state = self.downloaded()
        token = state.service.status()['token']
        reads = iter([state.pads, dict(state.pads, connection_left='detached')])
        state.service._pads = lambda: next(reads)
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                patch.object(fw, 'install_blob') as install:
            _, status = state.run('install', token, Mock())
        install.assert_not_called()
        self.assertEqual(status['phase'], 'failed')
        self.assertEqual(status['error'], 'Attach both controllers to check or install firmware updates. Nothing was installed.')

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_pad_detached_after_the_first_pad_says_what_was_installed(self):
        state = Service()
        both = feed(); both['candidates']['right'].update(feed_version='v26.04.01.01', version=catalog.raw_version('v26.04.01.01'))
        with patch.object(catalog, 'check_catalog', return_value=both):
            state.run('check')
        with patch.object(fw, 'download_image', side_effect=lambda r, c: r.component.encode() * 200), \
                patch.object(fw, 'validate_image', side_effect=validated):
            state.run('download')
        token = state.service.status()['token']
        new_left = dict(INSTALLED, left='260401A')
        state.readings = [INSTALLED, new_left, new_left]
        attached = {'left': 100, 'right': 100, 'connection_left': 'attached', 'connection_right': 'attached'}
        reads = iter([attached, attached, dict(attached, connection_right='detached')])
        state.service._pads = lambda: next(reads)
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', side_effect=[devices()[0], devices(left='260401A')[0]]), \
                patch.object(fw, 'updater_running', return_value=False), \
                patch.object(fw, 'install_blob', return_value=(0, 1.0, 'Done')) as install:
            _, status = state.run('install', token, Mock())
        install.assert_called_once()  # Left only; the detached right pad is never written.
        self.assertEqual(status['phase'], 'failed')
        self.assertIn('Attach both controllers', status['error'])
        self.assertIn('keep their new firmware', status['error'])
        self.assertNotIn('Nothing was installed', status['error'])

    def test_console_power_needs_mains_or_thirty_percent_of_the_console_battery(self):
        def power(**supplies):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                for name, values in supplies.items():
                    (root / name).mkdir()
                    for key, value in values.items():
                        (root / name / key).write_text(f'{value}\n')
                with patch.object(fw, 'Path', lambda path: root if path == '/sys/class/power_supply' else Path(path)):
                    return fw.console_power()
        battery = lambda level, **more: dict(type='Battery', capacity=level, **more)
        self.assertEqual(power(BAT0=battery(30)), (False, 30))
        self.assertEqual(power(BAT0=battery(5), ACAD=dict(type='Mains', online=1)), (True, 5))
        self.assertEqual(power(BAT1=battery(80, scope='System')), (False, 80))
        self.assertEqual(power(BAT0=battery(100)), (False, 100))
        for kind in ('USB', 'USB_C', 'USB_PD'):  # a USB-C charger
            self.assertEqual(power(BAT0=battery(5), charger=dict(type=kind, online=1)), (True, 5))
        for supplies in (dict(BAT0=battery(29)), dict(BAT0=battery(29), ACAD=dict(type='Mains', online=0)),
                         dict(BAT0=battery(20), pad=battery(90, scope='Device')),  # a controller's battery
                         dict(BAT0=battery(20), pad=dict(type='USB', online=1, scope='Device')),
                         dict(BAT0=battery(80), BAT1=battery(80)), dict()):
            with self.subTest(supplies=supplies), self.assertRaises(fw.FirmwareError):
                power(**supplies)

    def test_a_release_with_another_components_identity_is_refused(self):
        left, right = fw.RELEASES['left'], fw.RELEASES['right']
        for release in (replace(left, component='right'), replace(left, footer=right.footer),
                        replace(left, label=right.label)):
            with self.subTest(release=release), self.assertRaisesRegex(fw.FirmwareError, 'identity'):
                fw.validate_image('left', b'', release)

    def test_a_feed_candidate_needs_its_own_version_and_the_official_host(self):
        candidate = {'version': '270101A', 'feed_version': 'v27.01.01.01',
                     'url': 'https://s3.service.lenovo.com/legion/cms/package/future-left.bin'}
        self.assertEqual(fw.candidate_release('left', candidate).version, '270101A')
        for change in (dict(feed_version='v27.01.01.02'), dict(url='https://foreign.invalid/future-left.bin')):
            with self.subTest(change=change), self.assertRaises(fw.FirmwareError):
                fw.candidate_release('left', dict(candidate, **change))

    def test_pads_need_thirty_percent(self):
        read = lambda level: lambda: {'left': level, 'right': 5, 'connection_left': 'attached', 'connection_right': 'attached'}
        fw.pad_power(read(30), ('left',))
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.pad_power(read(29), ('left',))

    def test_receiver_only_install_needs_both_pads_attached(self):
        fw.pad_power(lambda: {'connection_left': 'attached', 'connection_right': 'attached'}, ('receiver',))
        for read in (None, lambda: {'connection_left': 'attached', 'connection_right': 'detached'}):
            with self.assertRaisesRegex(fw.FirmwareError, 'Attach both'):
                fw.pad_power(read, ('receiver',))

    def test_check_refuses_while_another_updater_runs(self):
        state = Service()
        with patch.object(fw, 'updater_running', return_value=True), \
                patch.object(catalog, 'check_catalog') as check:
            _, status = state.run('check')
        check.assert_not_called()
        self.assertEqual(status['phase'], 'failed')
        self.assertIn('Another firmware update is running', status['error'])

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_failed_or_unverified_install_reports_error_and_allows_retry(self):
        found, _ = devices()
        for returncode, readings in ((1, []), (None, []), (0, [INSTALLED] + [INSTALLED] * fw.VERIFY_ATTEMPTS)):
            state = self.downloaded()
            token = state.service.status()['token']
            state.readings = list(readings)
            done = Mock()
            with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                    patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                    patch.object(fw, 'install_blob', return_value=(returncode, 1.0, 'error tail')):
                _, status = state.run('install', token, done)
            self.assertEqual(status['phase'], 'failed')
            self.assertIn('Left controller', status['error'])
            done.assert_called_once()
            with patch.object(catalog, 'check_catalog', return_value=feed()):
                _, status = state.run('check')
            self.assertEqual(status['phase'], 'available', 'no permanent lock after a failed install')

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_concurrent_install_is_rejected_while_one_runs(self):
        state = self.downloaded()
        token = state.service.status()['token']
        release, entered = threading.Event(), threading.Event()
        def blob(*_args):
            entered.set(); self.assertTrue(release.wait(5)); return 1, 1.0, ''
        found, _ = devices()
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                patch.object(fw, 'install_blob', side_effect=blob):
            state.service.install(token, Mock())
            self.assertTrue(entered.wait(5))
            self.assertEqual(state.service.status()['phase'], 'installing')
            second = Mock()
            with self.assertRaises(fw.FirmwareError):
                state.service.install(token, second)
            second.assert_called_once()
            self.assertEqual(state.service.check()['phase'], 'installing', 'a check cannot interrupt an install')
            release.set(); state.service._thread.join(5)

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_power_refusal_comes_before_the_install_and_keeps_the_download(self):
        state = self.downloaded()
        token = state.service.status()['token']
        for error in (fw.FirmwareError('Connect power or charge the console to at least 30%.'), OSError('no sysfs')):
            with patch.object(fw, 'console_power', side_effect=error), patch.object(fw, '_run_tool') as tool:
                with self.assertRaises(fw.FirmwareError):
                    state.service.admit_install(token)
            tool.assert_not_called()
            status = state.service.status()
            self.assertEqual((status['phase'], status['token']), ('downloaded', token))
        with patch.object(fw, 'console_power'):
            self.assertEqual(state.service.admit_install(token)[0], state.service._check)

    @patch.object(fw, 'INSTALL_ENABLED', True)
    def test_install_that_succeeds_after_close_still_reports_installed(self):
        state = self.downloaded()
        token = state.service.status()['token']
        new = dict(INSTALLED, left='260401A')
        current = dict(INSTALLED)
        state.service._reader = lambda: dict(current)
        release, entered = threading.Event(), threading.Event()
        def blob(*_args):
            entered.set(); self.assertTrue(release.wait(5)); current.update(new); return 0, 40.0, 'Done'
        found, _ = devices()
        with patch.object(fw.Path, 'is_file', return_value=True), patch.object(fw, 'console_power'), \
                patch.object(fw, 'fwupd_devices', return_value=found), patch.object(fw, 'updater_running', return_value=False), \
                patch.object(fw, 'install_blob', side_effect=blob):
            state.service.install(token, Mock())
            self.assertTrue(entered.wait(5))
            state.service.close()  # The conflict guard closes the updater while fwupd writes.
            release.set(); state.service._thread.join(5)
        status = state.service.status()
        self.assertEqual(status['phase'], 'installed', status['message'])
        self.assertEqual({row['id']: row['installed'] for row in status['components']}, new)
        self.assertEqual(status['token'], '')


class EngineTests(unittest.TestCase):
    def test_container_is_the_fwupd_legion_hid_layout(self):
        images = {'receiver': image('receiver', '260422A'), 'left': image('left', '260310A'), 'right': image('right', '260310A')}
        data = fw.build_container(images)
        header = struct.unpack_from('<6I', data)
        self.assertEqual(header, (len(images['receiver'].original), 0x0260422A, len(images['left'].original), 0x0260310A,
                                  len(images['right'].original), 0x0260310A))
        self.assertEqual(data[24:], images['receiver'].original + images['left'].original + images['right'].original)
        for broken in ({'left': images['left'], 'right': images['right']}, dict(images, left=images['right']),
                       dict(images, receiver=b'raw')):
            with self.assertRaises(fw.FirmwareError):
                fw.build_container(broken)

    def test_fwupd_devices_match_unique_updatable_legion_hid_instances(self):
        found, rows = devices()
        def listing(payload, returncode=0):
            return Mock(returncode=returncode, stdout=json.dumps({'Devices': payload}).encode())
        with patch.object(fw, '_run_tool', return_value=listing(rows)) as tool:
            self.assertEqual(fw.fwupd_devices(), found)
        self.assertEqual(tool.call_args.args[0], [fw.FWUPDMGR, 'get-devices', '--json'])
        for payload, code in ((rows + [dict(rows[1], DeviceId='f' * 40)], 0), ([dict(rows[0], Flags=['internal'])] + rows[1:], 0),
                              ([dict(rows[0], Plugin='other')] + rows[1:], 0), ([dict(rows[0], DeviceId='../x')] + rows[1:], 0),
                              (rows, 1)):
            with patch.object(fw, '_run_tool', return_value=listing(payload, code)), self.assertRaises(fw.FirmwareError):
                fw.fwupd_devices()
        with patch.object(fw, '_run_tool', return_value=Mock(returncode=0, stdout=b'not json')), self.assertRaises(fw.FirmwareError):
            fw.fwupd_devices()

    def test_install_blob_never_stops_a_writing_fwupdtool_and_stops_a_silent_one(self):
        popen = fw.subprocess.Popen
        def tool(script):
            def start(argv, **kwargs):
                self.assertEqual(argv[:2], [fw.FWUPDTOOL, 'install-blob'])
                return popen([sys.executable, '-c', 'import sys, time\n' + script], **kwargs)
            return start
        writing = "for _ in range(12):\n sys.stdout.write('x' * 600); sys.stdout.flush(); time.sleep(.3)\nprint('Done')"
        with tempfile.TemporaryDirectory() as raw:
            log, blob = Path(raw) / 'fwupdtool.log', Path(raw) / 'c.bin'
            with patch.object(fw, 'INSTALL_STALL_SECONDS', 2), patch.object(fw.subprocess, 'Popen', tool(writing)):
                returncode, seconds, tail = fw.install_blob(blob, 'a' * 40, log)
            self.assertEqual(returncode, 0); self.assertGreater(seconds, 2)  # Ran past the stall limit.
            self.assertTrue(tail.rstrip().endswith('Done')); self.assertLessEqual(len(tail), 4096)
            with patch.object(fw, 'INSTALL_STALL_SECONDS', 2), patch.object(fw.subprocess, 'Popen', tool(
                    "print('start', flush=True); time.sleep(60)")):
                returncode, seconds, tail = fw.install_blob(blob, 'a' * 40, log)
            self.assertIsNone(returncode); self.assertLess(seconds, 30); self.assertIn('start', tail)
            with patch.object(fw, 'INSTALL_SECONDS', 1), patch.object(fw.subprocess, 'Popen', tool(
                    "while True:\n print('x', flush=True); time.sleep(.1)")):
                returncode, seconds, _ = fw.install_blob(blob, 'a' * 40, log)
            self.assertIsNone(returncode); self.assertLess(seconds, 30)

    def test_updater_running_detects_fwupd_clients_and_steam_updater(self):
        with tempfile.TemporaryDirectory() as raw:
            proc = Path(raw)
            for pid, name in ((1, 'systemd'), (2, 'fwupd')):
                (proc / str(pid)).mkdir(); (proc / str(pid) / 'comm').write_text(name + '\n')
            self.assertFalse(fw.updater_running(proc))
            for name in ('fwupdtool', 'fwupdmgr', 'steamos-firmwar'):
                (proc / '9').mkdir(exist_ok=True); (proc / '9' / 'comm').write_text(name + '\n')
                self.assertTrue(fw.updater_running(proc), name)

    def test_two_different_reads_are_not_trusted(self):
        with patch.object(fw, '_read_versions_once', side_effect=[INSTALLED, dict(INSTALLED, left='0000000')]), \
                patch.object(fw, '_text', side_effect=lambda p: {'sys_vendor': 'LENOVO', 'product_name': '83N0',
                    'product_version': 'Legion Go 8ASP2', 'idVendor': '17ef', 'idProduct': '61eb'}.get(p.name)), \
                patch.object(fw.Path, 'glob', side_effect=[[Path('usb')], [Path('hid')]]), \
                patch.object(fw.Path, 'is_file', return_value=True), \
                patch.object(fw.os.path, 'realpath', side_effect=lambda p: '/d/usb/hid-lenovo-go' if str(p).endswith('driver') else '/d/usb/hid'), \
                self.assertRaises(fw.FirmwareError):
            fw.read_installed_versions()


@unittest.skipUnless(os.name == 'posix', 'sysfs symlink tree')
class SysfsReaderTests(unittest.TestCase):
    def tree(self, *, product='83N0', driver='hid-lenovo-go', generation='2', version='260310a', extra_root=False):
        raw = tempfile.TemporaryDirectory(); self.addCleanup(raw.cleanup)
        sysfs = Path(raw.name)
        dmi = sysfs / 'class/dmi/id'; dmi.mkdir(parents=True)
        for name, value in (('sys_vendor', 'LENOVO'), ('product_name', product), ('product_version', 'Legion Go 8ASP2')):
            (dmi / name).write_text(value + '\n')
        usb = sysfs / 'devices/pci/usb3/3-1'; usb.mkdir(parents=True)
        (usb / 'idVendor').write_text('17ef\n'); (usb / 'idProduct').write_text('61eb\n')
        (sysfs / 'bus/usb/devices').mkdir(parents=True); (sysfs / 'bus/usb/devices/3-1').symlink_to(usb)
        drivers = sysfs / 'bus/hid/drivers'; (drivers / driver).mkdir(parents=True); (drivers / 'hid-multitouch').mkdir()
        (sysfs / 'bus/hid/devices').mkdir(parents=True)
        for index, owner in ((1, 'hid-multitouch'), (2, driver), (3, driver)):
            node = usb / f'3-1:1.{index}' / f'0003:17EF:61EB.000{index}'; node.mkdir(parents=True)
            (node / 'driver').symlink_to(drivers / owner)
            (sysfs / 'bus/hid/devices' / node.name).symlink_to(node)
            if index == 2 or (index == 3 and extra_root):
                for sub, value in (('', '260422a'), ('left_handle', version), ('right_handle', '260310a'), ('tx_dongle', '260422a')):
                    (node / sub).mkdir(exist_ok=True)
                    (node / sub / 'firmware_version').write_text(value + '\n')
                    (node / sub / 'hardware_generation').write_text(generation + '\n')
        return sysfs

    def test_reads_driver_versions_on_the_supported_console(self):
        self.assertEqual(fw.read_installed_versions(self.tree()), INSTALLED)

    def test_unsupported_model_driver_generation_format_or_duplicate_fail_closed(self):
        for options in ({'product': '83N1'}, {'driver': 'hid-generic'}, {'generation': '1'},
                        {'version': '0'}, {'version': '261399a'}, {'extra_root': True}):
            with self.subTest(**options), self.assertRaises(fw.FirmwareError):
                fw.read_installed_versions(self.tree(**options))


class PluginInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_writes_are_paused_only_while_an_admitted_install_runs(self):
        plugin = support.LegacyPlugin()
        plugin._check_guard = AsyncMock()
        plugin._controller = Mock(_status_cache=('cached', {}))
        finished = asyncio.Event()
        observed = []
        class Firmware:
            def admit_install(self, token):
                if token != 'good':
                    raise fw.FirmwareError('The confirmed update no longer matches the download.')
            def install(self, token, on_done):
                observed.append(module_runtime.is_paused('rgb'))
                threading.Thread(target=lambda: (on_done(), loop.call_soon_threadsafe(finished.set))).start()
                return {'phase': 'installing'}
        loop = asyncio.get_running_loop()
        plugin._firmware = Firmware()
        with self.assertRaises(fw.FirmwareError):
            await plugin.firmware_install('bad')
        self.assertFalse(module_runtime.is_paused('rgb'), 'a refused install never pauses controllers')
        with patch.object(main.vibration_backend, '_forget_device') as forget:
            self.assertEqual((await plugin.firmware_install('good'))['phase'], 'installing')
            await asyncio.wait_for(finished.wait(), 5)
            await asyncio.sleep(0)
        self.assertEqual(observed, [True])
        self.assertFalse(module_runtime.is_paused('rgb'))
        self.assertFalse(module_runtime.is_paused('vibration'))
        self.assertIsNone(plugin._controller._status_cache)
        forget.assert_called_once()

    async def test_paused_controller_rejects_module_writes(self):
        module_runtime.pause_controller()
        try:
            with self.assertRaises(module_runtime.ControllerPaused):
                module_runtime.require_available('rgb')
            module_runtime.require_available('fan')
            with self.assertRaises(module_runtime.ControllerPaused):
                module_runtime.pause_controller()
        finally:
            module_runtime.resume_controller()
        module_runtime.require_available('rgb')


if __name__ == '__main__':
    unittest.main()
