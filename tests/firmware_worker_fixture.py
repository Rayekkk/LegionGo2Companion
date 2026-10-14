# SPDX-License-Identifier: BSD-3-Clause
"""Test-only transport model. Never included in the plugin package."""
from dataclasses import replace
import json
from pathlib import Path
import struct
import time

import controller_firmware as fw
import controller_firmware_protocol as protocol
import controller_firmware_transfer as transfer
from controller_firmware_worker import ControllerClaim, FirmwareWorker

BINDING = 'a' * 64


def reply(component, command, payload=b'', family=0xc0):
    target = protocol.OTA_TARGETS[component] if family != 2 else protocol.VERSION_QUERY_TARGETS[component]
    return bytes((4, 0, family, command, target)) + payload + bytes(59-len(payload))


class Clock:
    def __init__(self): self.value = 0.
    def __call__(self): return self.value
    def advance(self, amount=.001): self.value += amount


class Journal:
    def __init__(self, events): self.records=[]; self.events=events; self.fail_at=None
    def save(self, value):
        self.records.append(dict(value))
        if len(self.records) == self.fail_at: raise OSError('injected secret')
    def close(self): self.events.append('journal')


class Inhibitor:
    def __init__(self, events): self.events=events; self.alive=True; self.acquired=False
    def acquire(self, deadline): self.acquired=True
    def valid(self): return self.alive and self.acquired
    def close(self): self.events.append('inhibitor')


class Transport:
    def __init__(self, clock, events, boundary=64):
        self.clock=clock; self.events=events; self.boundary=boundary
        self.component=None; self.commands=[]; self.queue=[]; self.alive=True
        self.drop=False; self.early=False; self.first_write=None
        self.claim=None
    def acquire(self, component, deadline):
        self.component=component
        self.claim=ControllerClaim(BINDING, 'before', protocol.Preflight(
            'Legion Go 8ASP2', 2, '251204A', True, True, True, None, 'before', self.clock()))
        return self.claim
    def valid(self, claim): return self.alive and claim is self.claim
    def write(self, command, deadline):
        if not self.commands and self.first_write: self.first_write()
        self.commands.append(command); self.clock.advance()
        if command.needs_reply and not self.drop:
            payload=struct.pack('<I', self.boundary) if command.kind=='boundary' else b'\0'
            if command.kind=='finish' and self.early:
                self.queue.append(reply(self.component, 3, b'\1', 0xa0))
            self.queue.append(reply(self.component, command.report[3], payload))
            if command.kind=='finish' and not self.early:
                self.queue.append(reply(self.component, 3, b'\1', 0xa0))
        return 64
    def read(self, deadline):
        if self.queue:
            self.clock.advance(); return self.queue.pop(0)
        self.clock.value=deadline; return None
    def observe(self, token, deadline):
        self.clock.advance()
        return transfer.InstalledObservation(BINDING, 'Legion Go 8ASP2', 2,
            'after', self.clock(), token, reply(self.component, 4,
            struct.pack('>I', int(fw.release_for(self.component).version, 16)), 2))
    def close(self): self.events.append('transport')


def process_fixture(stage, marker, gate, result):
    """Finite subprocess experiment with authenticated bytes and simulated I/O."""
    clock=Clock(); events=[]; transport=Transport(clock, events, 1024)
    def wait_for_parent_test():
        Path(marker).write_text('ready', encoding='ascii')
        deadline=time.monotonic()+8
        while not Path(gate).exists():
            if time.monotonic() >= deadline: raise TimeoutError('test gate expired')
            time.sleep(.02)
    transport.first_write=wait_for_parent_test
    worker=FirmwareWorker('left', (Path(stage)/'image.bin').read_bytes(),
        transfer.TransferPolicy(frozenset((1024,)), 10., 5., 5., 60.),
        transport, Inhibitor(events), Journal(events), lambda: True, clock)
    outcome=worker.run()
    outcome['close_order']=events
    Path(result).write_text(json.dumps(outcome), encoding='utf-8')
