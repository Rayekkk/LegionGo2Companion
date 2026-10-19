# SPDX-License-Identifier: BSD-3-Clause
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import support
import remap_backend as remap
import wifi_backend as wifi
import tdp_backend as tdp
import display_backend as display
from system_process import system_env


class SystemProcessTests(unittest.TestCase):
    def test_frozen_runtime_is_kept_in_parent_but_not_external_programs(self):
        env = {name: '/tmp/_MEI/bundled' for name in (
            'LD_LIBRARY_PATH', 'LD_LIBRARY_PATH_ORIG', 'LD_PRELOAD', 'LD_AUDIT')}
        env.update(PATH='/usr/bin', DISPLAY=':1', XDG_RUNTIME_DIR='/run/user/1000')
        child = subprocess.CompletedProcess([], 0, 'ok', '')
        with patch.dict(os.environ, env, clear=True), \
             patch.object(subprocess, 'run', return_value=child) as run, \
             patch.object(os.path, 'isfile', return_value=True), \
             patch.object(remap, '_version_cache', None):
            self.assertEqual(remap._run_busctl(['--version']), 'ok')
            remap._inputplumber_version()
            self.assertTrue(wifi.Plugin()._run_cmd(['nmcli', '--version'])['success'])
            environments = [call.kwargs['env'] for call in run.call_args_list]
            proc = Mock(returncode=0)
            proc.communicate.return_value = (b'ok', b'')
            with patch.object(subprocess, 'Popen', return_value=proc) as popen, \
                 patch.object(tdp, '_ryzenadj_available', True):
                self.assertEqual(tdp._run_ryzenadj(['-i'])[0], 0)
                environments.append(popen.call_args.kwargs['env'])
            environments.append(display._system_env(LC_ALL='C'))
            for actual in environments:
                self.assertEqual(actual, {'PATH': '/usr/bin', 'DISPLAY': ':1',
                                         'XDG_RUNTIME_DIR': '/run/user/1000', 'LC_ALL': 'C'})
            self.assertEqual(dict(os.environ), env)

    @unittest.skipUnless(os.name == 'posix', 'SteamOS interpreter')
    def test_helpers_run_on_the_system_python_without_windows_flags(self):
        import system_process
        self.assertEqual((system_process.PYTHON, system_process.NO_WINDOW), ('/usr/bin/python3', {}))

    def test_clean_runtime_and_fallback_path(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(system_env(), {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})

    def test_fan_system_python_and_fwupd_do_not_inherit_frozen_loader_libraries(self):
        import fan_backend as fan
        import controller_firmware as firmware
        blocked = {'LD_LIBRARY_PATH': '/tmp/_MEI/bundled', 'LD_PRELOAD': '/tmp/unrelated.so',
                   'LD_AUDIT': '/tmp/audit.so', 'LD_LIBRARY_PATH_ORIG': '/tmp/old',
                   'PYTHONPATH': '/tmp/unrelated', 'PATH': '/usr/bin'}
        parent, child = Mock(), Mock()
        client = fan.Client()
        def ready(*_args):
            client.state = {'kind': 'ready'}
        with patch.dict(os.environ, blocked, clear=True), \
                patch.object(fan.socket, 'AF_UNIX', 1, create=True), \
                patch.object(fan.socket, 'SOCK_SEQPACKET', 5, create=True), \
                patch.object(fan.socket, 'socketpair', return_value=(parent, child)), \
                patch.object(fan.subprocess, 'Popen', return_value=Mock()) as launch, \
                patch.object(client, '_receive', side_effect=ready):
            client.start()
            environment = launch.call_args.kwargs['env']
            for name in blocked.keys()-{'PATH'}:
                self.assertNotIn(name, environment)
            with patch.object(firmware.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
                firmware._run_tool([firmware.FWUPDMGR, '--version'], 5)
                for name in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT', 'LD_LIBRARY_PATH_ORIG'):
                    self.assertNotIn(name, run.call_args.kwargs['env'])
            self.assertEqual(dict(os.environ), blocked)
