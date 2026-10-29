# SPDX-License-Identifier: BSD-3-Clause
"""Diagnostic coverage preserves update results and never logs raw payloads."""
import asyncio
import collections
import json
import unittest
from unittest.mock import patch

import support  # noqa: F401 - installs the fake decky module
import controller_firmware as fw
import diagnostics
import dk_net
import dk_update
import main


class UpdateDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.records=[]
        self.sink=patch.object(diagnostics,'_sink',side_effect=lambda text,level:self.records.append(json.loads(text)))
        self.seen=patch.object(diagnostics,'_seen',collections.OrderedDict())
        self.sink.start();self.seen.start()
        self.addCleanup(self.sink.stop);self.addCleanup(self.seen.stop)

    def assert_private(self):
        text=json.dumps(self.records)
        for forbidden in ('private-secret-marker','private-serial-marker','/home/private/full-path'):
            self.assertNotIn(forbidden,text)

    async def test_failed_install_records_stage_and_status_polls_stay_quiet(self):
        service=fw.FirmwarePreparation(reader=lambda:{c:'250101A' for c in fw.RELEASES},sleep=lambda _:None)
        check={'id':'a'*32,'newer':('left',),'installed':{c:'250101A' for c in fw.RELEASES}}
        with patch.object(fw,'console_power',side_effect=fw.FirmwareError('token=private-secret-marker /home/private/full-path')), \
                patch.object(fw.Path,'is_file',return_value=True):
            service._run_install(check,{},lambda:None)
        self.assertEqual(service.status()['phase'],'failed')
        fault=next(value for value in self.records if value['event']=='step_failed')
        self.assertEqual(fault['context']['stage'],'install')
        self.assertEqual(fault['error']['type'],'FirmwareError')
        count=len(self.records)
        for _ in range(20):service.status()
        self.assertEqual(len(self.records),count)
        self.assert_private()

    async def test_cached_release_error_does_not_repeat_network_or_diagnostics(self):
        updater=dk_update.Updates(repo='Rayekkk/LegionGo2Companion',folder='LegionGo2Companion',
            plugin_name='Legion Go 2 Companion',current='2.4.0',home='/home/private/full-path',
            on_event=main._update_event)
        failure=dk_net.NetError('private-secret-marker /home/private/full-path')
        with patch.object(dk_net,'get',side_effect=failure) as metadata:
            result=main._legacy_update(updater.check());count=len(self.records)
            for _ in range(20):self.assertEqual(main._legacy_update(updater.check()),result)
        self.assertFalse(result['success']);self.assertEqual(metadata.call_count,1)
        self.assertEqual(len(self.records),count)
        fault=next(value for value in self.records if value['event']=='release_check_failed')
        self.assertEqual((fault['level'],fault['context']['outcome']),('warning','failed'))
        self.assert_private()


if __name__=='__main__':unittest.main()
