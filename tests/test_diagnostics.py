# SPDX-License-Identifier: BSD-3-Clause
"""Diagnostic failures must neither leak request data nor affect controls."""
import asyncio
import errno
import io
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import diagnostics as diag


class EventTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.sink = patch.object(diag, '_sink', lambda text, level: self.records.append(json.loads(text)))
        self.sink.start(); self.addCleanup(self.sink.stop)
        with diag._lock:
            diag._seen.clear()

    def test_error_retains_errno_frames_cause_without_arguments_or_private_text(self):
        try:
            try:
                raise ConnectionResetError(errno.ECONNRESET, 'token=secret /home/private/user https://user:password@secret.invalid')
            except OSError as error:
                raise RuntimeError('SSID=private-wifi') from error
        except RuntimeError as error:
            diag.event('fan', 'transport_failed', error=error, level='warning', args=['secret'], settings={'password':'secret'})
        record = self.records[0]
        self.assertEqual(record['error']['type'], 'RuntimeError')
        self.assertEqual(record['error']['causes'], [{'type':'ConnectionResetError','errno':errno.ECONNRESET}])
        self.assertTrue(any(frame['function'].startswith('test_error_') for frame in record['error']['frames']))
        self.assertNotIn('secret', json.dumps(record)); self.assertNotIn('private-wifi', json.dumps(record))
        self.assertNotIn('args', record.get('context', {})); self.assertNotIn('settings', record.get('context', {}))

    def test_repeat_coalesces_per_store_and_reports_suppressed_count(self):
        with patch.object(diag.time, 'monotonic', return_value=100):
            for _ in range(5): diag.event('settings', 'backup_failed', level='warning', store='battery', error=OSError(errno.ENOSPC, 'full'), repeat=60)
            diag.event('settings', 'backup_failed', level='warning', store='rgb', error=OSError(errno.ENOSPC, 'full'), repeat=60)
        self.assertEqual(len(self.records), 2)
        with patch.object(diag.time, 'monotonic', return_value=161):
            diag.event('settings', 'backup_failed', level='warning', store='battery', error=OSError(errno.ENOSPC, 'full'), repeat=60)
        self.assertEqual(self.records[-1]['suppressed'], 4)

    def test_failure_and_one_recovery_are_not_poll_spam(self):
        for _ in range(15): diag.transition('fan','health','failed',level='warning',error=OSError(errno.EIO,'bad'))
        for _ in range(15): diag.transition('fan','health','ready')
        self.assertEqual([r['context']['status'] for r in self.records], ['failed','ready'])
        self.assertEqual([r['level'] for r in self.records], ['warning','info'])

    def test_persistent_fault_reappears_after_sink_failure_without_suppressing_a_new_fault_cycle(self):
        with patch.object(diag.time,'monotonic',return_value=100),patch.object(diag,'_sink',side_effect=OSError('full')):
            diag.transition('fan','health','failed',level='warning',error=OSError(errno.EIO,'bad'))
        with patch.object(diag.time,'monotonic',return_value=161):
            diag.transition('fan','health','failed',level='warning',error=OSError(errno.EIO,'bad'))
            diag.transition('fan','health','ready')
            diag.transition('fan','health','failed',level='warning',error=OSError(errno.EIO,'bad'))
        self.assertEqual([r['context']['status'] for r in self.records],['failed','ready','failed'])
        self.assertEqual(self.records[0]['context']['repeated'],1)

    def test_nonfinite_and_nested_context_is_not_serialized(self):
        diag.event('plugin','test',duration_ms=float('nan'),count=1,reason={'secret':'value'},args=['secret'])
        self.assertEqual(self.records[0]['context']['count'],1)
        for key in ('duration_ms','reason','args'): self.assertNotIn(key,self.records[0]['context'])

    def test_unicode_and_control_text_stays_bounded_valid_single_line_json(self):
        values={key:'\u2603\n'*5000 for key in diag._FIELDS}
        diag.event('plugin','large',**values)
        self.assertEqual(len(self.records),1)
        encoded=diag._encode(self.records[0])
        self.assertLessEqual(len(encoded.encode()),diag.MAX_RECORD)
        self.assertNotIn('\n',encoded)
        self.assertIsInstance(json.loads(encoded),dict)

    def test_faulty_sink_and_faulty_error_format_never_escape(self):
        with patch.object(diag,'_sink',side_effect=OSError('disk full')):
            self.assertIsNone(diag.event('plugin','failed',error=RuntimeError('bad')))
            self.assertIsNone(diag.transition('plugin','health','bad'))
        class Broken:
            def __str__(self): raise RuntimeError('bad formatting')
        self.assertIsNone(diag.event('plugin','bad',error=Broken()))

    def test_unconfigured_sink_never_calls_a_potentially_blocking_legacy_handler(self):
        logger=Mock()
        logger.warning.side_effect=AssertionError('Producer reached a synchronous logger')
        with patch.object(diag,'_sink',None),patch.object(diag,'_legacy',logger):
            diag.event('fan','release_failed',level='warning',error=OSError(errno.EIO,'bad'))
        logger.warning.assert_not_called()

    def test_bounded_dedup_memory_and_component_context(self):
        for index in range(600): diag.event('settings','failed',repeat=60,store=str(index),component='battery')
        self.assertLessEqual(len(diag._seen),256)
        self.assertEqual(self.records[-1]['context']['component'],'battery')

    def test_legacy_filter_removes_credentials_paths_addresses_and_raw_tracebacks(self):
        try: raise ValueError('password=secret')
        except ValueError as exc:
            record=logging.LogRecord('test',40,'',0,'Bearer abc https://user:password@secret.invalid/path /home/name/private 192.168.1.5 token=secret',(),(type(exc),exc,exc.__traceback__))
        self.assertTrue(diag._LegacyFilter().filter(record))
        text=record.getMessage()
        for private in ('secret','abc','/home/name','192.168.1.5'):self.assertNotIn(private,text)
        self.assertIn('ValueError',text);self.assertIsNone(record.exc_info)

    def test_asyncio_error_keeps_metadata_without_formatting_task_or_context(self):
        for with_exception in (True,False):
            with self.subTest(with_exception=with_exception):
                try:raise OSError(errno.EIO,'unlabelled_private_exception')
                except OSError as exc:info=(type(exc),exc,exc.__traceback__) if with_exception else None
                record=logging.LogRecord('asyncio',40,'',0,'private task %r',('private_context',),info)
                record.getMessage=Mock(side_effect=AssertionError('Raw context must not be formatted'))
                record.stack_info='private_stack'
                self.assertTrue(diag._AsyncioFilter().filter(record))
                self.assertNotIn('private',record.msg)
                self.assertEqual(record.args,())
                self.assertIsNone(record.exc_info);self.assertIsNone(record.stack_info)
        self.assertEqual(self.records[0]['event'],'asyncio_exception')
        self.assertEqual(self.records[0]['error']['errno'],errno.EIO)
        self.assertNotIn('private',json.dumps(self.records))


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_rpc_and_thread_work_share_one_operation_but_requests_do_not(self):
        records=[]
        with patch.object(diag,'_sink',lambda text,level:records.append(json.loads(text))):
            with diag.source_context('desktop'),diag.operation(name='fan_save_curve') as first:
                diag.event('fan','started')
                with diag.operation(name='nested'):
                    await asyncio.to_thread(diag.event,'fan','persisted')
            with diag.operation(name='fan_save_curve') as second:
                diag.event('fan','started')
        self.assertNotEqual(first,second)
        self.assertEqual([r['operation_id'] for r in records],[first,first,second])
        self.assertEqual([r['context']['source'] for r in records],['desktop','desktop','decky'])
        self.assertEqual(records[1]['context']['operation'],'fan_save_curve')

    async def test_background_failure_keeps_its_exception_and_gets_one_safe_event(self):
        import module_runtime
        records=[]
        async def fail():raise OSError(errno.EIO,'private path or payload')
        with patch.object(diag,'_sink',lambda text,level:records.append(json.loads(text))):
            task=module_runtime.background_task(fail())
            with self.assertRaises(OSError):await task
            await asyncio.sleep(0)
        self.assertTrue(any(r['event']=='background_task_failed' and r['error']['errno']==errno.EIO for r in records))
        self.assertNotIn('private path',json.dumps(records))

    async def test_late_thread_failure_after_waiter_cancel_is_recorded_and_still_drained(self):
        import module_runtime
        entered,release=threading.Event(),threading.Event();records=[]
        logger=logging.getLogger('asyncio');output=io.StringIO()
        privacy_filter=diag._AsyncioFilter();logger.addFilter(privacy_filter)
        self.addCleanup(logger.removeFilter,privacy_filter)
        def fail():entered.set();release.wait(5);raise OSError(errno.EIO,'private')
        # This sink-only fixture bypasses configure(); retain the production
        # privacy filter if CPython emits its known eager shield-error record.
        with patch.object(diag,'_sink',lambda text,level:records.append(json.loads(text))), \
                patch.object(logger,'handlers',[logging.StreamHandler(output)]),patch.object(logger,'propagate',False):
            task=asyncio.create_task(module_runtime.offload('logging_fixture',fail))
            self.assertTrue(await asyncio.to_thread(entered.wait,3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(module_runtime._workers['logging_fixture'])
            worker,=module_runtime._workers['logging_fixture']
            release.set();await module_runtime.drain('logging_fixture');await asyncio.sleep(0)
        self.assertFalse(module_runtime._workers['logging_fixture'])
        self.assertFalse(worker._log_traceback)
        self.assertTrue(any(r['event']=='worker_failed' and r['error']['errno']==errno.EIO for r in records))
        self.assertNotIn('private',json.dumps(records));self.assertNotIn('private',output.getvalue())


class WriterTests(unittest.TestCase):
    def tearDown(self):
        diag.close()

    def test_actual_private_rotation_is_bounded_and_parseable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'logs'
            with patch.object(diag,'MAX_BYTES',512):
                self.assertTrue(diag.configure(directory,Mock()))
                for count in range(40):diag.event('plugin','write',count=count)
                diag.close()
            files=list(directory.iterdir());self.assertLessEqual(len(files),3)
            self.assertIn('events.jsonl',{p.name for p in files})
            for path in files:
                self.assertLessEqual(path.stat().st_size,512)
                for line in path.read_text().splitlines():self.assertEqual(json.loads(line)['event'],'write')
                if os.name=='posix':self.assertEqual(path.stat().st_mode&0o777,0o600)
            if os.name=='posix':self.assertEqual(directory.stat().st_mode&0o777,0o700)

    def test_asyncio_filter_lifetime_preserves_existing_filters(self):
        logger=logging.getLogger('asyncio');existing=Mock()
        logger.addFilter(existing)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self.assertTrue(diag.configure(Path(temporary)/'logs',Mock()))
                installed=diag._asyncio_filter
                self.assertIn(installed,logger.filters)
                self.assertTrue(diag.configure(Path(temporary)/'logs',Mock()))
                self.assertEqual(logger.filters.count(installed),1)
                diag.close()
                self.assertNotIn(installed,logger.filters)
                self.assertIn(existing,logger.filters)
        finally:logger.removeFilter(existing)

    def test_blocked_writer_and_full_queue_do_not_wait_in_producer(self):
        entered,release=threading.Event(),threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(diag,'QUEUE_SIZE',2):self.assertTrue(diag.configure(Path(temporary)/'logs',Mock()))
            handler=diag._writer.handler;real=handler.emit
            def blocked(record):entered.set();release.wait(5);real(record)
            try:
                with patch.object(handler,'emit',side_effect=blocked):
                    diag.event('plugin','first');self.assertTrue(entered.wait(3))
                    producer=threading.Thread(target=lambda:[diag.event('plugin','burst',count=n) for n in range(30)])
                    producer.start();producer.join(2)
                    self.assertFalse(producer.is_alive(),'diagnostic producer waited behind filesystem logging')
                    self.assertGreater(diag._writer.dropped,0)
                    release.set();diag.close()
                    records=[json.loads(line) for line in (Path(temporary)/'logs/events.jsonl').read_text().splitlines()]
                    dropped=[row for row in records if row['event']=='diagnostic_events_dropped']
                    self.assertTrue(dropped)
                    from diagnostics_reader import normalize_event
                    visible=normalize_event(dropped[0],'backend')
                    self.assertEqual(visible['level'],'warning')
                    self.assertGreater(visible['context']['count'],0)
            finally:release.set();diag.close()

    def test_full_disk_is_advisory_and_later_records_are_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'logs';logger=Mock()
            self.assertTrue(diag.configure(directory,logger))
            writer=diag._writer
            with patch.object(writer.handler,'emit',side_effect=OSError(errno.ENOSPC,'full')):
                diag.event('plugin','lost');writer.queue.join()
            diag.event('plugin','recovered');writer.queue.join();diag.close()
            self.assertEqual(json.loads((directory/'events.jsonl').read_text())['event'],'recovered')
            logger.warning.assert_called_once();logger.info.assert_called_once()

    @unittest.skipUnless(os.name=='posix','POSIX symlink/regular-file protection')
    def test_symlink_log_directory_and_history_never_touch_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'keep';target.write_text('untouched')
            folder=root/'logs';folder.mkdir(mode=0o700);(folder/'events.jsonl').symlink_to(target)
            self.assertFalse(diag.configure(folder,Mock()));self.assertEqual(target.read_text(),'untouched')
            link=root/'link';link.symlink_to(folder,target_is_directory=True)
            self.assertFalse(diag.configure(link,Mock()));self.assertEqual(target.read_text(),'untouched')

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix native journal transport')
    def test_journal_worker_datagram_is_nonblocking_structured_and_does_not_log_protocol_payload(self):
        stream=Mock();stream.__enter__=Mock(return_value=stream);stream.__exit__=Mock(return_value=False)
        with patch.object(diag,'_sink',None),patch.object(diag.socket,'socket',return_value=stream):
            with diag.source_context('test'):
                diag.configure_worker('firmware')
                diag.event('firmware','restore_failed',level='error',error=OSError(errno.EIO,'private'),stage='restore')
        stream.setblocking.assert_called_once_with(False)
        data,path=stream.sendto.call_args.args
        self.assertEqual(path,'/run/systemd/journal/socket')
        self.assertIn(b'SYSLOG_IDENTIFIER=legiongo2companion-firmware\n',data)
        self.assertIn(b'PRIORITY=3\n',data)
        record=json.loads(data.split(b'MESSAGE=',1)[1]);self.assertEqual(record['error']['errno'],errno.EIO)
        self.assertNotIn(b'private',data)
        self.assertLess(len(data),5000)


class RpcLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import collections
        import test_integration
        import main
        from unittest.mock import AsyncMock
        self.main=main
        self.records=[]
        self.seen=patch.object(diag,'_seen',collections.OrderedDict());self.seen.start();self.addCleanup(self.seen.stop)
        self.sink=patch.object(diag,'_sink',lambda text,level:self.records.append(json.loads(text)))
        self.sink.start();self.addCleanup(self.sink.stop)
        self.plugin=main.Plugin();self.plugin._check_guard=AsyncMock()
        self.plugin._module_state={name:{'enabled':True} for name in main.MODULE_NAMES}
        self.plugin._module_health={name:{'ready':True} for name in main.MODULE_NAMES}

    async def test_both_frontends_failures_and_success_keep_result_and_operation_context(self):
        from unittest.mock import AsyncMock
        from desktop_bridge import DesktopBridge
        self.plugin._tdp=Mock(apply_tdp=AsyncMock(return_value={'success':False,'stderr':'token=SECRET'}))
        failure=await self.plugin.apply_tdp(15000,18000,20000)
        self.assertFalse(failure['success'])
        self.plugin._tdp.apply_tdp.return_value={'success':True,'private':'not logged'}
        bridge=DesktopBridge(self.plugin,Path('.'),['apply_tdp'])
        success=await bridge.dispatch('apply_tdp',[15000,18000,20000])
        self.assertTrue(success['success'])
        terminals=[r for r in self.records if r['event'] in ('rpc_failed','rpc_completed')]
        self.assertEqual([r['event'] for r in terminals],['rpc_failed','rpc_completed'])
        self.assertEqual([r['context']['source'] for r in terminals],['decky','desktop'])
        self.assertNotEqual(terminals[0]['operation_id'],terminals[1]['operation_id'])
        self.assertNotIn('SECRET',json.dumps(self.records));self.assertNotIn('not logged',json.dumps(self.records))
        self.assertEqual(self.plugin._tdp.apply_tdp.await_count,2)

    async def test_repeated_read_failure_and_recovery_are_coalesced_without_disconnect(self):
        from unittest.mock import AsyncMock
        self.plugin._fan=Mock(get_state=AsyncMock(side_effect=ConnectionResetError(errno.ECONNRESET,'peer reset')))
        for _ in range(6):
            with self.assertRaises(ConnectionResetError):await self.plugin.fan_get_state()
        self.plugin._fan.get_state.side_effect=None
        self.plugin._fan.get_state.return_value={'success':True,'settings':{'enabled':False}}
        value=await self.plugin.fan_get_state();self.assertFalse(value['settings']['enabled'])
        health=[r for r in self.records if r['event']=='rpc_health']
        self.assertEqual([r['context']['status'] for r in health],['failed','ok'])
        self.assertEqual(health[0]['error']['errno'],errno.ECONNRESET)
        self.assertEqual(self.plugin._fan.get_state.await_count,7)

    async def test_failed_logging_cannot_change_success_failure_or_guard_admission(self):
        from unittest.mock import AsyncMock
        self.plugin._tdp=Mock(apply_tdp=AsyncMock(return_value={'success':True}))
        with patch.object(diag,'_sink',side_effect=OSError(errno.ENOSPC,'full')):
            self.assertTrue((await self.plugin.apply_tdp(15000,18000,20000))['success'])
            self.plugin._tdp.apply_tdp.side_effect=OSError(errno.EIO,'bad')
            with self.assertRaises(OSError):await self.plugin.apply_tdp(15000,18000,20000)
            self.plugin._module_state['tdp']={'enabled':False}
            with self.assertRaisesRegex(RuntimeError,'disabled'):await self.plugin.apply_tdp(15000,18000,20000)
        self.assertEqual(self.plugin._tdp.apply_tdp.await_count,2)

    async def test_real_validation_failure_does_not_copy_arbitrary_rpc_argument_to_logs(self):
        import vibration_backend
        private='PRIVATE_UNLABELLED_RPC_ARGUMENT_731'
        self.plugin._vibration=vibration_backend.Plugin()
        value=await self.plugin.vibe_test_vibration(private)
        self.assertFalse(value['success'])
        self.assertIn(private,value['error'])  # Existing caller response unchanged.
        self.assertNotIn(private,json.dumps(self.records))
        self.assertTrue(any(r['event']=='rpc_failed' for r in self.records))

    async def test_uninstall_keeps_logging_after_inner_unload_until_cleanup_finishes(self):
        await self.plugin._uninstall()
        events=[r['event'] for r in self.records]
        for name in ('uninstall_started','shutdown_started','shutdown_completed','uninstall_completed'):
            self.assertIn(name,events)
        self.assertLess(events.index('shutdown_completed'),events.index('uninstall_completed'))
        self.assertFalse(self.plugin._uninstalling)

    async def test_failed_shutdown_records_origin_without_hiding_original_exception(self):
        from unittest.mock import AsyncMock
        self.plugin._modules_started=True
        self.plugin._stop_modules=AsyncMock(side_effect=OSError(errno.EIO,'private cleanup detail'))
        with self.assertRaises(OSError):await self.plugin._uninstall()
        self.assertTrue(any(r['event']=='shutdown_failed' and r['error']['errno']==errno.EIO for r in self.records))
        self.assertTrue(any(r['event']=='uninstall_not_confirmed' for r in self.records))
        self.assertNotIn('private cleanup detail',json.dumps(self.records))
        self.assertFalse(self.plugin._uninstalling)
