# SPDX-License-Identifier: BSD-3-Clause
import errno
import os
import time
import unittest
from unittest.mock import patch
from fan_sleep import SleepSafety


class Function:
    def __init__(self, fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


class Library:
    def __init__(self,fd,preparing=False,failure=None):
        self.fd=fd;self.preparing=preparing;self.failure=failure
        self.messages=[];self.unrefs=[];self.closed=False;self.current=None
        self.sd_bus_open_system=Function(lambda p:self.set(p,100))
        self.sd_bus_set_method_call_timeout=Function(lambda *a:0)
        self.sd_bus_call_method=Function(self.call)
        self.sd_bus_message_read=Function(self.read)
        self.sd_bus_message_unref=Function(lambda m:self.unrefs.append(m.value))
        self.sd_bus_close_unref=Function(lambda b:setattr(self,'closed',True))
        self.sd_bus_process=Function(self.process)
        self.sd_bus_message_is_signal=Function(lambda m,i,n: int(n==(b'NameOwnerChanged' if self.current=='owner-change' else b'PrepareForSleep')))
        self.sd_bus_message_get_sender=Function(lambda m:b':1.5')
    @staticmethod
    def set(pointer,value):pointer._obj.value=value;return 1
    def call(self,bus,dest,path,interface,member,error,reply,types,*args):
        if member==self.failure:return -errno.EIO
        self.set(reply,{'AddMatch':101,'Inhibit':102,'Get':103,'GetNameOwner':104}[member.decode()]);return 1
    def read(self,message,types,*args):
        if types==b's':return self.set(args[0],b':1.5')
        if types==b'sss':
            for pointer,value in zip(args,[b'org.freedesktop.login1',b':1.5',b':1.8']):self.set(pointer,value)
            return 1
        return self.set(args[-1],self.fd if types==b'h' else self.preparing if types==b'v' else self.current)
    def process(self,bus,message):
        if not self.messages:return 0
        self.current=self.messages.pop(0)
        if self.current=='disconnect':return -errno.ECONNRESET
        self.set(message,104);return 1


class SleepSafetyTests(unittest.TestCase):
    def setUp(self):self.r,self.w=os.pipe()
    def tearDown(self):os.close(self.r);os.close(self.w)
    def test_descriptor_is_duplicated_and_retained_until_close(self):
        lib=Library(self.r)
        with patch('fan_sleep.c.CDLL',return_value=lib):s=SleepSafety()
        self.assertNotEqual(s.inhibitor,self.r);os.fstat(s.inhibitor)
        self.assertIn(102,lib.unrefs)
        copy=s.inhibitor;s.close();s.close()
        with self.assertRaises(OSError):os.fstat(copy)
        os.fstat(self.r);self.assertTrue(lib.closed)
    def test_preparing_or_inhibit_failure_cannot_arm_and_releases_resources(self):
        for preparing,failure in [(True,None),(False,b'Inhibit'),(False,b'Get')]:
            lib=Library(self.r,preparing,failure)
            with patch('fan_sleep.c.CDLL',return_value=lib),self.assertRaises((RuntimeError,OSError)):
                SleepSafety()
            self.assertTrue(lib.closed)
    def test_prepare_signal_disconnect_and_queue_flood_fail_closed(self):
        class Flood:
            """Never-empty input; do not pin the production batch size to 16."""
            def __init__(self): self.deadline=None
            def __bool__(self): return True
            def pop(self,index):
                if self.deadline is None: self.deadline=time.monotonic()+5
                if time.monotonic()>=self.deadline:
                    raise AssertionError('Sleep-event draining did not terminate')
                return False
        for events in [[False,True],['disconnect'],['owner-change'],Flood()]:
            lib=Library(self.r)
            with patch('fan_sleep.c.CDLL',return_value=lib):s=SleepSafety()
            lib.messages=events
            try:
                with self.assertRaises((RuntimeError,OSError)):s.check()
                os.fstat(s.inhibitor)
            finally:s.close()
