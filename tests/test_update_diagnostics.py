# SPDX-License-Identifier: BSD-3-Clause
"""Diagnostic coverage preserves update results and never logs raw payloads."""
import asyncio
import collections
import json
from pathlib import Path
import socket
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import companion_updates as updates
import controller_firmware as fw
import controller_firmware_catalog as catalog
import controller_firmware_worker as worker
import controller_maintenance as maintenance
import diagnostics


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

    async def test_background_installer_failure_correlates_jobs_and_status_polls_stay_quiet(self):
        def installed():return {'versions':{c:'250101A' for c in fw.RELEASES},'source':'fixture','attachment':'same'}
        feed={'checked_at':'fixture','model':catalog.MODEL,'region':'PL','candidates':{
            c:{'version':r.version,'feed_version':'v26.04.22.01' if c=='receiver' else 'v26.03.10.01','url':r.url}
            for c,r in fw.RELEASES.items()}}
        def installer(*args):raise fw.FirmwareError('private-secret-marker /home/private/full-path')
        installer.diagnostic_job_id='b'*32
        service=fw.FirmwarePreparation(installed_reader=installed,installer=installer)
        service._job['id']='a'*32
        with patch.object(catalog,'check_catalog',return_value=feed),patch.object(fw,'download_image',return_value=b'fixture'), \
                patch.object(fw,'validate_image',side_effect=lambda c,d,release=None:fw.ValidatedImage(release,d)):
            service._run_updates()
        self.assertEqual(service.status()['job']['phase'],'uncertain')
        fault=next(value for value in self.records if value['event']=='check_failed')
        self.assertEqual(fault['context']['stage'],'installer')
        self.assertEqual(fault['context']['job_id'],'a'*32);self.assertEqual(fault['context']['worker_id'],'b'*32)
        self.assertEqual(fault['error']['type'],'FirmwareError')
        count=len(self.records)
        for _ in range(20):service.status()
        self.assertEqual(len(self.records),count)
        self.assert_private()

    async def test_worker_fault_and_cleanup_are_separate_safe_events(self):
        transport=Mock();transport.acquire.side_effect=fw.FirmwareError('private-serial-marker')
        inhibitor=Mock();inhibitor.valid.return_value=True
        journal=Mock();journal.close.side_effect=OSError('private-secret-marker')
        session=Mock();session.snapshot.return_value={'phase':'failed_before_write'}
        with patch.object(worker,'TransferSession',return_value=session):
            result=worker.FirmwareWorker('left',b'fixture',SimpleNamespace(verification_timeout=2),transport,inhibitor,journal,lambda:True).run()
        self.assertEqual(result['cleanup_errors'],['journal']);self.assertFalse(result['cleanup_complete'])
        fault=next(value for value in self.records if value['event']=='component_worker_failed')
        self.assertEqual(fault['context']['stage'],'exclusive_acquisition');self.assertEqual(fault['context']['scope'],'left')
        cleanup=next(value for value in self.records if value['event']=='component_cleanup_failed')
        self.assertEqual(cleanup['context']['stage'],'journal');self.assert_private()

    async def test_maintenance_drain_fault_keeps_generic_response_but_records_stage(self):
        peer=Mock();peer.getsockopt.return_value=struct.pack('3i',123,1000,1000)
        writer=Mock();writer.get_extra_info.return_value=peer
        writer.drain=AsyncMock();writer.wait_closed=AsyncMock()
        reader=SimpleNamespace(readline=AsyncMock(return_value=b'{"action":"pause"}\n'))
        gate=maintenance.ControllerUpdaterGate(AsyncMock(side_effect=maintenance.MaintenanceError('private-secret-marker')),AsyncMock(),owner=1000)
        with patch.object(socket,'SO_PEERCRED',17,create=True):await gate._client(reader,writer)
        fault=next(value for value in self.records if value['event']=='coordination_failed')
        self.assertEqual(fault['context']['stage'],'pause_drain');self.assertFalse(fault['context']['pending'])
        self.assertEqual(writer.write.call_args.args[0],b'{"error":"Updater coordination did not complete."}\n')
        self.assert_private()

    async def test_cached_release_error_does_not_repeat_network_or_diagnostics(self):
        updater=updates.CompanionUpdates(str(Path(__file__).resolve().parents[1]),'unused',1000,1000)
        with patch.object(updater,'_metadata',side_effect=OSError('private-secret-marker /home/private/full-path')) as metadata:
            result=updater.check();count=len(self.records)
            for _ in range(20):self.assertEqual(updater.check(),result)
        self.assertFalse(result['success']);self.assertEqual(metadata.call_count,1)
        self.assertEqual(len(self.records),count)
        fault=next(value for value in self.records if value['event']=='release_check_failed')
        self.assertEqual(fault['context']['stage'],'metadata');self.assertEqual(fault['error']['type'],'OSError')
        self.assert_private()


if __name__=='__main__':unittest.main()
