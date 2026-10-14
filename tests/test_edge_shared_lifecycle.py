# SPDX-License-Identifier: BSD-3-Clause
"""Concurrent UI, interrupted HTTP and durable module-withdrawal boundaries."""
import asyncio
import copy
from contextlib import ExitStack, suppress
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import desktop_bridge
from desktop_bridge import DesktopBridge
import fan_backend
from fan_policy import DEFAULT_CURVE
import main
import safe_settings
from safe_settings import SettingsManager


class SharedLifecycleEdges(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        store=SettingsManager('fan_settings',str(self.root))
        self.stack.enter_context(patch.object(fan_backend,'settings',store))
        self.stack.enter_context(patch.object(fan_backend,'read_automatic',return_value={
            'valid':True,'automatic':True,'rpm':1500,'apu_temperature':45,
            'ownership':{'blocked':False,'state':'firmware_auto'}}))
        self.plugin=main.Plugin();self.plugin._check_guard=AsyncMock()
        self.plugin._module_state={};self.plugin._module_health={}
        self.plugin._module_store=SettingsManager('module_settings',str(self.root))
        self.plugin._fan=fan_backend.Plugin();self.plugin._fan._supported=True;self.plugin._fan._load()
        self.bridge=DesktopBridge(self.plugin,self.root,['get_version','fan_get_state','fan_save_curve','fan_save_unit'])
        await self.bridge.start()
        self.releases=[]

    async def asyncTearDown(self):
        for release in self.releases:release.set()
        await self.bridge.close()
        await self.plugin._fan._unload()

    async def until(self,predicate):
        async def wait():
            while not predicate():await asyncio.sleep(.002)
        await asyncio.wait_for(wait(),3)

    async def connect(self,method,args):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.bridge.port)
        body=json.dumps({'method':method,'args':args}).encode()
        writer.write((f'POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{self.bridge.port}\r\n'
            f'Origin: {self.bridge.origin}\r\nAuthorization: Bearer {self.bridge.token}\r\n'
            f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body)
        await writer.drain()
        return reader,writer

    async def request(self,method,args):
        reader,writer=await self.connect(method,args)
        try:return await asyncio.wait_for(reader.read(),3)
        finally:
            writer.close()
            with suppress(ConnectionError,OSError):await writer.wait_closed()

    def hold_commit(self,path):
        arrived,release=threading.Event(),threading.Event();self.releases.append(release)
        original=safe_settings._atomic_write_bytes
        held=False
        def write(target,payload):
            nonlocal held
            if target==path and not held:
                held=True;arrived.set()
                if not release.wait(3):raise TimeoutError('Test commit barrier was not released')
            return original(target,payload)
        self.stack.enter_context(patch.object(safe_settings,'_atomic_write_bytes',side_effect=write))
        return arrived,release

    async def test_simultaneous_decky_and_http_curve_saves_have_one_canonical_winner(self):
        first=copy.deepcopy(DEFAULT_CURVE);first[2][1]=1801
        second=copy.deepcopy(DEFAULT_CURVE);second[2][0]=46
        results=await asyncio.gather(self.request('fan_save_curve',['global',first,0]),
            self.plugin.fan_save_curve('global',second,0),return_exceptions=True)
        desktop=results[0];decky=results[1]
        desktop_ok=desktop.startswith(b'HTTP/1.1 200 ')
        self.assertEqual(int(desktop_ok)+int(isinstance(decky,dict)),1)
        if not desktop_ok:self.assertTrue(desktop.startswith(b'HTTP/1.1 409 '),desktop)
        else:self.assertIsInstance(decky,ValueError)
        state=(await self.plugin.fan_get_state())['settings']
        self.assertEqual(state['global_curve'],first if desktop_ok else second)
        self.assertEqual(state['revision'],1)
        fresh=fan_backend.Plugin();fresh._load()
        self.assertEqual(fresh._state,state)
        self.assertFalse(state['enabled']);self.assertIsNone(self.plugin._fan._client)

    async def test_lost_http_reply_cannot_turn_stale_retry_into_a_second_commit(self):
        arrived,release=self.hold_commit(fan_backend.settings.path)
        curve=copy.deepcopy(DEFAULT_CURVE);curve[2][1]=1801
        reader,writer=await self.connect('fan_save_curve',['global',curve,0])
        await self.until(arrived.is_set)
        writer.close();await writer.wait_closed()
        retry=asyncio.create_task(self.plugin.fan_save_unit('percent',0))
        try:
            await asyncio.sleep(0);self.assertFalse(retry.done())
            release.set()
            with self.assertRaises(ValueError):await retry
            await self.until(lambda:not self.bridge.operations)
            state=(await self.plugin.fan_get_state())['settings']
            self.assertEqual(state['revision'],1);self.assertEqual(state['global_curve'],curve)
            self.assertEqual(state['display_unit'],'rpm');self.assertFalse(state['enabled'])
        finally:
            release.set();await asyncio.gather(retry,return_exceptions=True)

    async def test_disable_waits_for_admitted_save_then_rejects_later_writes(self):
        arrived,release=self.hold_commit(fan_backend.settings.path)
        curve=copy.deepcopy(DEFAULT_CURVE);curve[2][1]=1801
        saving=asyncio.create_task(self.plugin.fan_save_curve('global',curve,0))
        await self.until(arrived.is_set)
        disabling=asyncio.create_task(self.plugin.modules_set_enabled('fan',False))
        try:
            await asyncio.sleep(0);self.assertFalse(disabling.done())
            release.set();await saving;await disabling
            self.assertFalse(self.plugin._module_enabled('fan'))
            with self.assertRaisesRegex(RuntimeError,'disabled'):await self.plugin.fan_save_unit('percent',2)
            fresh=fan_backend.Plugin();fresh._load()
            self.assertEqual(fresh._state['global_curve'],curve)
            self.assertFalse(fresh._state['enabled'])
            self.assertFalse(self.plugin._module_state['fan'].get('pending',False))
        finally:
            release.set();await asyncio.gather(saving,disabling,return_exceptions=True)

    async def test_repeated_cancellation_does_not_leave_half_saved_module_off(self):
        arrived,release=self.hold_commit(self.plugin._module_store.path)
        disabling=asyncio.create_task(self.plugin.modules_set_enabled('fan',False))
        await self.until(arrived.is_set)
        try:
            disabling.cancel();await asyncio.sleep(0);disabling.cancel();await asyncio.sleep(0)
            self.assertFalse(disabling.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):await disabling
            self.assertFalse(self.plugin._module_enabled('fan'))
            self.assertFalse(self.plugin._module_state['fan'].get('pending',False))
            stored=SettingsManager('module_settings',str(self.root)).getSetting('modules')
            self.assertEqual(stored,self.plugin._module_state)
            self.assertFalse(self.plugin._fan._state['enabled'])
        finally:
            release.set();await asyncio.gather(disabling,return_exceptions=True)

    async def test_slow_headers_cannot_keep_capacity_after_the_read_deadline(self):
        peers=[]
        with patch.object(desktop_bridge,'MAX_CLIENTS',3),patch.object(desktop_bridge,'READ_SECONDS',.5):
            try:
                for _ in range(3):
                    peers.append(await asyncio.open_connection('127.0.0.1',self.bridge.port))
                await self.until(lambda:len(self.bridge.clients)==3)
                try:response=await self.request('get_version',[])
                except (ConnectionError,OSError):response=b''  # Refused capacity can be EOF or reset.
                self.assertEqual(response,b'')
                replies=await asyncio.gather(*(asyncio.wait_for(reader.read(),2) for reader,_ in peers))
                self.assertTrue(all(reply.startswith(b'HTTP/1.1 408 ') for reply in replies))
                await self.until(lambda:not self.bridge.clients)
                self.assertFalse(self.bridge.operations)
            finally:
                for _,writer in peers:writer.close()
                await asyncio.gather(*(writer.wait_closed() for _,writer in peers),return_exceptions=True)
        self.assertTrue((await self.request('get_version',[])).startswith(b'HTTP/1.1 200 '))
