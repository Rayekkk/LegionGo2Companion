# SPDX-License-Identifier: BSD-3-Clause
"""Fake ownership transitions, never a hardware handoff or recovery proof."""
from dataclasses import replace
import unittest
from unittest.mock import patch
import ctypes as C
from types import SimpleNamespace

from controller_firmware import FirmwareError
from controller_firmware_usb_linux import UsbObservation, QualificationInterface, LinuxUsbOwnership, HID_REPORT

HID='/sys/bus/usb/drivers/usbhid'
USBFS='/sys/bus/usb/drivers/usbfs'


class FakeApi:
    def __init__(self, observation):
        self.observation=observation
        self.calls=[]
        self.result=0
        self.error=None
        self.no_readback=False

    def open_matching(self, expected):
        self.calls.append(('open',expected))
        if self.error:raise self.error
        return 1,2

    def action(self, handle, kind):
        self.calls.append((kind,handle))
        if not self.no_readback:
            driver={'detach_kernel_driver':None,'claim_interface':USBFS,
                    'release_interface':None,'attach_kernel_driver':HID}[kind]
            self.observation=replace(self.observation,driver=driver,
                hid_report_sha256=HID_REPORT if driver==HID else None)
        if self.error:raise self.error
        return self.result

    def close_owned(self, context, handle):self.calls.append(('close',context,handle))


class LinuxQualificationTests(unittest.TestCase):
    def make(self, role='probe'):
        baseline=UsbObservation('a106b34d-90a0-4db9-8c76-92b8b2c05cdb',
            '/sys/devices/usb3/3-1',(1,2),3,2,(1,),bytes(18),
            (('3-1:1.0','xpad'),('3-1:1.1',HID),('3-1:1.3',HID)),HID,HID_REPORT)
        self.api=FakeApi(baseline if role=='probe' else replace(baseline,driver=None,hid_report_sha256=None))
        self.permitted=True
        return QualificationInterface(baseline,lambda _:self.permitted,role=role,
            _api=self.api,_observe=lambda:self.api.observation)

    def claimed(self):
        obj=self.make();obj.open();obj.detach();obj.claim();return obj

    def test_no_packet_handoff_and_distinct_recovery_role(self):
        obj=self.claimed();obj.release();obj.close()
        self.assertEqual([c[0] for c in self.api.calls],['open','detach_kernel_driver','claim_interface','release_interface','close'])
        obj=self.make('recovery');obj.open();obj.attach();obj.close()
        self.assertEqual([c[0] for c in self.api.calls],['open','attach_kernel_driver','close'])

    def test_recovery_cannot_claim_detach_or_query(self):
        obj=self.make('recovery');obj.open()
        for call in (obj.detach,obj.claim,lambda:obj.query(3,deadline=10),obj.release):
            with self.assertRaises(FirmwareError):call()
        self.assertEqual([c[0] for c in self.api.calls],['open'])

    def test_probe_cannot_attach_or_reopen(self):
        obj=self.make();obj.open()
        with self.assertRaises(FirmwareError):obj.attach()
        obj.close()
        with self.assertRaises(FirmwareError):obj.open()
        self.assertEqual(sum(c[0]=='open' for c in self.api.calls),1)

    def test_wrong_binding_or_permission_prevents_open(self):
        for fault in ('owner','permission','hid'):
            obj=self.make()
            if fault=='owner':self.api.observation=replace(self.api.observation,driver=USBFS)
            if fault=='permission':self.permitted=False
            if fault=='hid':self.api.observation=replace(self.api.observation,hid_report_sha256='b'*64)
            with self.assertRaises(FirmwareError):obj.open()
            self.assertEqual(self.api.calls,[])

    def test_attachment_boot_address_inode_or_other_bindings_change_prevents_detach(self):
        for change in ({'boot_id':'other'},{'address':3},{'instance':(1,3)},
                       {'ports':(2,)},{'device_descriptor':bytes(19)}, {'other_bindings':()}):
            obj=self.make();obj.open();self.api.observation=replace(self.api.observation,**change)
            with self.assertRaises(FirmwareError):obj.detach()
            self.api.observation=obj._baseline
            with self.assertRaises(FirmwareError):obj.detach()
            self.assertEqual([c[0] for c in self.api.calls],['open'])

    def test_permission_loss_before_action_has_no_side_effect_or_retry(self):
        obj=self.make();obj.open();self.permitted=False
        with self.assertRaises(FirmwareError):obj.detach()
        self.permitted=True
        with self.assertRaises(FirmwareError):obj.detach()
        self.assertEqual([c[0] for c in self.api.calls],['open'])

    def test_uncertain_or_invalid_action_result_never_retries(self):
        for fault in ('exception','code','bool','readback'):
            obj=self.make();obj.open()
            if fault=='exception':self.api.error=OSError('lost result')
            if fault=='code':self.api.result=-1
            if fault=='bool':self.api.result=False
            if fault=='readback':self.api.no_readback=True
            with self.assertRaises((FirmwareError,OSError)):obj.detach()
            with self.assertRaises(FirmwareError):obj.detach()
            self.assertEqual(sum(c[0]=='detach_kernel_driver' for c in self.api.calls),1)

    def test_foreign_owner_before_claim_is_never_replaced(self):
        obj=self.make();obj.open();obj.detach();self.api.observation=replace(self.api.observation,driver=HID)
        with self.assertRaises(FirmwareError):obj.claim()
        self.assertNotIn('claim_interface',[c[0] for c in self.api.calls])

    def test_lost_release_acknowledgement_is_not_repeated(self):
        obj=self.claimed();self.api.error=OSError('uncertain release')
        with self.assertRaises(OSError):obj.release()
        with self.assertRaises(FirmwareError):obj.release()
        self.assertEqual(sum(c[0]=='release_interface' for c in self.api.calls),1)

    def test_query_is_one_shot_and_failed_reply_still_allows_one_owned_release(self):
        obj=self.claimed()
        with patch('controller_firmware_usb_linux.UsbVersionChannel') as channel:
            channel.return_value.query.side_effect=FirmwareError('No reply')
            with self.assertRaises(FirmwareError):obj.query(3,deadline=10)
            with self.assertRaises(FirmwareError):obj.query(3,deadline=10)
            channel.return_value.query.assert_called_once_with(3,deadline=10)
        obj.release()

    def test_reply_not_accepted_after_ownership_changes(self):
        obj=self.claimed()
        def reply(*a,**k):
            self.api.observation=replace(self.api.observation,driver=None)
            return bytes(64)
        with patch('controller_firmware_usb_linux.UsbVersionChannel') as channel:
            channel.return_value.query.side_effect=reply
            with self.assertRaises(FirmwareError):obj.query(3,deadline=10)

    def test_open_race_closes_own_handle_without_detach(self):
        obj=self.make();api=self.api
        def open_raced(_):
            api.calls.append(('open',))
            api.observation=replace(api.observation,driver=None,hid_report_sha256=None)
            return 1,2
        api.open_matching=open_raced
        with self.assertRaises(FirmwareError):obj.open()
        self.assertEqual([c[0] for c in api.calls],['open','close'])

    def test_low_level_adapter_does_not_expose_arbitrary_libusb_action(self):
        api=object.__new__(LinuxUsbOwnership)
        for kind in ('reset_device','set_configuration','set_interface_alt_setting','clear_halt','interrupt_transfer'):
            with self.assertRaises(FirmwareError):api.action(2,kind)

    def test_close_is_idempotent_without_reopening_or_claiming(self):
        obj=self.claimed();obj.close();obj.close()
        self.assertEqual(sum(c[0]=='close' for c in self.api.calls),1)
        with self.assertRaises(FirmwareError):obj.release()

    def test_closed_handle_cannot_claim_after_detach_or_query_after_claim(self):
        for claimed in (False,True):
            obj=self.make();obj.open();obj.detach()
            if claimed:obj.claim()
            obj.close()
            calls=list(self.api.calls)
            for operation in (obj.claim,lambda:obj.query(3,deadline=10),obj.detach,obj.release):
                with self.assertRaises(FirmwareError):operation()
            self.assertEqual(self.api.calls,calls)

    def test_raw_interrupt_rejects_ota_arbitrary_payload_and_infinite_timeout(self):
        api=object.__new__(LinuxUsbOwnership)
        query=bytes((5,0,2,4,3))+bytes(59)
        for endpoint,data,timeout in ((3,bytes((5,0,0xc0))+bytes(61),100),
                (3,query[:-1]+b'\x01',100),(3,bytes((5,0,2,4,7))+bytes(59),100),
                (0x83,query,100),(3,query,0),(3,query,101),(True,query,100),(3,query,True)):
            with self.assertRaises(FirmwareError):api.interrupt(2,endpoint,data,timeout)
        with patch('controller_firmware_usb_linux.Libusb.interrupt',return_value=(0,64,query)) as call:
            self.assertEqual(api.interrupt(2,3,query,100),(0,64,query))
            api.interrupt(2,0x83,bytes(64),5)
            self.assertEqual(call.call_count,2)

    def test_overlarge_enumeration_releases_allocated_list_and_context(self):
        obj=self.make();api=object.__new__(LinuxUsbOwnership);calls=[]
        devices=(C.c_void_p*1)(123)
        def initialize(out):
            C.cast(out,C.POINTER(C.c_void_p))[0]=77
            return 0
        def enumerate_devices(context,out):
            C.cast(out,C.POINTER(C.POINTER(C.c_void_p)))[0]=C.cast(devices,C.POINTER(C.c_void_p))
            return 1025
        api._lib=SimpleNamespace(libusb_init=initialize,libusb_get_device_list=enumerate_devices,
            libusb_free_device_list=lambda ptr,unref:calls.append(('free',unref)),
            libusb_exit=lambda context:calls.append(('exit',context.value)))
        with self.assertRaises(FirmwareError):api.open_matching(obj._baseline)
        self.assertEqual(calls,[('free',1),('exit',77)])


if __name__=='__main__':unittest.main()
