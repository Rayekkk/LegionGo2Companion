# SPDX-License-Identifier: BSD-3-Clause
"""Dormant, interface-02-only qualification executor; never an OTA transport.

Blocking libusb operations belong in a separately supervised owned helper,
never the guardian event loop. Permission callbacks are coordinator contracts,
not independent ownership proofs. The admitted installer uses the ownership/ABI interface; startup and polling
do not acquire USB or submit OTA.
The caller must durably record intent, exclude consumers, arm rescue and prove
the probe process/FDs gone before starting the distinct recovery helper.
"""
import ctypes as C
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re

from controller_firmware import FirmwareError
from controller_firmware_usb import Libusb, UsbVersionChannel, _Descriptor

KERNEL='7.2.7-valve1-1-neptune-72-gc8730d37f9c6'
DESCRIPTORS='89478aa6a57612d310137adefd96cdbea88d9d825dfb2fcec18f07d6b3167653'
HID_REPORT='46e1ad0c15ee4d17b745111c20e36dd8c7e9eae67c5e9aaa2ff5f5eee64c8cd3'
MODULE_ZST='49f01decec6a5583808f40aee12f997899a44eb81fc537faec841ee3363faf57'


def _bytes(path, limit):
    with Path(path).open('rb') as stream:value=stream.read(limit+1)
    if not value or len(value)>limit:raise FirmwareError('USB identity field exceeds its bound.')
    return value


def _text(path):return _bytes(path,4096).decode('ascii').strip()


@dataclass(frozen=True)
class UsbObservation:
    boot_id: str
    syspath: str
    instance: tuple[int,int]
    bus: int
    address: int
    ports: tuple[int,...]
    device_descriptor: bytes
    other_bindings: tuple[tuple[str,str],...]
    driver: str | None
    hid_report_sha256: str | None

    def same_attachment(self, other):
        if type(other) is not UsbObservation:return False
        return (self.boot_id,self.syspath,self.instance,self.bus,self.address,self.ports,
                self.device_descriptor,self.other_bindings)==(
                other.boot_id,other.syspath,other.instance,other.bus,other.address,other.ports,
                other.device_descriptor,other.other_bindings)


def observe_command_interface():
    """Passive fixed-support sysfs/module observation; no USB handle/query."""
    if os.name!='posix' or os.uname().release!=KERNEL:
        raise FirmwareError('This kernel has not been qualified for USB handoff.')
    dmi=Path('/sys/class/dmi/id')
    if tuple(_text(dmi/name) for name in ('sys_vendor','product_name','product_version','bios_version','ec_firmware_release'))!=('LENOVO','83N0','Legion Go 8ASP2','RRCN16WW','1.16'):
        raise FirmwareError('This console has not been qualified for USB handoff.')
    if (_text('/sys/module/hid_lenovo_go/srcversion')!='8C69A1B3F44E453E04FD17D' or
            hashlib.sha256(_bytes('/lib/modules/'+KERNEL+'/kernel/drivers/hid/hid-lenovo-go.ko.zst',1048576)).hexdigest()!=MODULE_ZST):
        raise FirmwareError('The observed HID driver does not match the reviewed module.')
    matches=[]
    roots=list(Path('/sys/bus/usb/devices').iterdir())
    if len(roots)>512:raise FirmwareError('USB topology exceeds its qualification bound.')
    for path in roots:
        if not re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*){0,6}',path.name):continue
        try:
            if _text(path/'idVendor')=='17ef' and _text(path/'idProduct')=='61eb':matches.append(path)
        except FileNotFoundError:raise FirmwareError('USB topology changed during qualification.') from None
    if len(matches)!=1:raise FirmwareError('A unique qualified controller was not observed.')
    base=matches[0];raw=_bytes(base/'descriptors',4096)
    if hashlib.sha256(raw).hexdigest()!=DESCRIPTORS or _text(base/'bConfigurationValue')!='1':
        raise FirmwareError('USB descriptors/configuration changed.')
    interface=base.parent/(base.name+':1.2')
    if tuple(_text(interface/name) for name in ('bInterfaceNumber','bAlternateSetting','bInterfaceClass'))!=('02','0','03'):
        raise FirmwareError('Only interface02 alternate0 HID is qualified.')
    for endpoint in ('ep_03','ep_83'):
        ep=interface/endpoint
        if tuple(_text(ep/name) for name in ('bmAttributes','wMaxPacketSize','bInterval'))!=('03','0040','02'):
            raise FirmwareError('The interface02 interrupt endpoint layout changed.')
    driver=str((interface/'driver').resolve(strict=True)) if (interface/'driver').is_symlink() else None
    reports=list(interface.glob('0003:*/report_descriptor'))
    report_hash=None
    if reports:
        if len(reports)!=1 or str((reports[0].parent/'driver').resolve(strict=True))!='/sys/bus/hid/drivers/hid-lenovo-go':
            raise FirmwareError('Unexpected HID child during USB qualification.')
        report_hash=hashlib.sha256(_bytes(reports[0],4096)).hexdigest()
        if report_hash!=HID_REPORT:raise FirmwareError('The controller HID descriptor changed.')
    other=[]
    for sibling in sorted(base.parent.glob(base.name+':*')):
        if sibling==interface:continue
        if not (sibling/'driver').is_symlink():raise FirmwareError('Another controller interface lost its binding.')
        other.append((sibling.name,str((sibling/'driver').resolve(strict=True))))
    if tuple(name.rsplit('.',1)[-1] for name,_ in other)!=('0','1','3'):
        raise FirmwareError('The controller interface topology changed.')
    info=base.resolve(strict=True).stat()
    ports=tuple(int(x) for x in base.name.split('-',1)[1].split('.'))
    return UsbObservation(_text('/proc/sys/kernel/random/boot_id'),str(base.resolve(strict=True)),
        (info.st_dev,info.st_ino),int(_text(base/'busnum')),int(_text(base/'devnum')),ports,
        raw[:18],tuple(other),driver,report_hash)


class LinuxUsbOwnership(Libusb):
    def __init__(self):
        super().__init__()
        class Version(C.Structure):
            _fields_=[(name,C.c_uint16) for name in ('major','minor','micro','nano')]+[
                ('rc',C.c_char_p),('description',C.c_char_p)]
        self._lib.libusb_get_version.restype=C.POINTER(Version)
        self._lib.libusb_get_version.argtypes=[]
        version=self._lib.libusb_get_version().contents
        if tuple(getattr(version,name) for name in ('major','minor','micro','nano'))!=(1,0,30,12037):
            raise FirmwareError('This libusb build has not been reviewed for handoff.')
        p=C.c_void_p
        for name,result,args in (
            ('libusb_open',C.c_int,[p,C.POINTER(p)]),('libusb_close',None,[p]),
            ('libusb_get_configuration',C.c_int,[p,C.POINTER(C.c_int)]),
            ('libusb_kernel_driver_active',C.c_int,[p,C.c_int]),
            ('libusb_set_auto_detach_kernel_driver',C.c_int,[p,C.c_int]),
            ('libusb_detach_kernel_driver',C.c_int,[p,C.c_int]),
            ('libusb_attach_kernel_driver',C.c_int,[p,C.c_int]),
            ('libusb_claim_interface',C.c_int,[p,C.c_int]),
            ('libusb_release_interface',C.c_int,[p,C.c_int])):
            fn=getattr(self._lib,name);fn.restype=result;fn.argtypes=args

    def open_matching(self, expected):
        context,devices,handle=C.c_void_p(),C.POINTER(C.c_void_p)(),C.c_void_p()
        if self._lib.libusb_init(C.byref(context))!=0:raise FirmwareError('USB context initialization failed.')
        try:
            count=self._lib.libusb_get_device_list(context,C.byref(devices))
            try:
                if not 0<=count<=1024:raise FirmwareError('USB enumeration failed/exceeded its bound.')
                matches=[]
                for index in range(count):
                    device=devices[index];ports=(C.c_uint8*7)();d=_Descriptor()
                    if self._lib.libusb_get_device_descriptor(device,C.byref(d))!=0:raise FirmwareError('USB descriptor read failed.')
                    if (d.vendor,d.product)!=(0x17ef,0x61eb):continue
                    depth=self._lib.libusb_get_port_numbers(device,ports,7)
                    if (1<=depth<=7 and bytes(d)==expected.device_descriptor and
                            self._lib.libusb_get_bus_number(device)==expected.bus and
                            self._lib.libusb_get_device_address(device)==expected.address and
                            tuple(ports[:depth])==expected.ports):matches.append(device)
                if len(matches)!=1:raise FirmwareError('The observed USB instance changed before open.')
                if self._lib.libusb_open(matches[0],C.byref(handle))!=0:raise FirmwareError('USB open failed.')
                config=C.c_int()
                if self._lib.libusb_get_configuration(handle,C.byref(config))!=0 or config.value!=1:
                    raise FirmwareError('The active USB configuration changed.')
                if self._lib.libusb_set_auto_detach_kernel_driver(handle,0)!=0:
                    raise FirmwareError('Explicit USB handoff mode is unavailable.')
                result=context,handle;context=handle=None
                return result
            finally:
                if devices:self._lib.libusb_free_device_list(devices,1)
        finally:
            if handle is not None and handle.value:self._lib.libusb_close(handle)
            if context is not None and context.value:self._lib.libusb_exit(context)

    def action(self, handle, kind):
        # Interface index is hardcoded. No reset/config/alternate/clear-halt ABI.
        if kind not in ('detach_kernel_driver','attach_kernel_driver','claim_interface','release_interface'):
            raise FirmwareError('Only fixed interface02 handoff actions are available.')
        return getattr(self._lib,'libusb_'+kind)(handle,2)

    def interrupt(self, handle, endpoint, data, timeout_ms):
        # Defence against accidentally passing a transfer-core packet into this
        # qualification ABI. A future OTA adapter must be a separate capability.
        if (type(endpoint) is not int or type(timeout_ms) is not int or
                not 1<=timeout_ms<=100 or type(data) is not bytes or len(data)!=64 or
                not (endpoint==0x83 and data==bytes(64) or endpoint==3 and
                     data[:3]==bytes((5,0,2)) and data[3] in (4,6) and data[4] in (1,2,3,4) and data[5:]==bytes(59))):
            raise FirmwareError('Only bounded version qualification reports are available.')
        return super().interrupt(handle,endpoint,data,timeout_ms)

    def close_owned(self, context, handle):
        try:self._lib.libusb_close(handle)
        finally:self._lib.libusb_exit(context)


class QualificationInterface:
    """One helper role/handle, no reopening or retry after uncertainty.

    Probe and recovery are distinct process roles. The recovery role has no
    claim/query permission. Calling close is not proof the process/FDs are gone.
    The independent guardian must establish that before a recovery process.
    """
    def __init__(self, baseline, permission, *, role='probe', _api=None, _observe=observe_command_interface):
        if type(baseline) is not UsbObservation or role not in ('probe','recovery') or not callable(permission):
            raise FirmwareError('A fixed USB qualification role/baseline is required.')
        self._baseline,self._permit,self._role=baseline,permission,role
        self._api=_api or LinuxUsbOwnership();self._observe=_observe
        self._context=self._handle=None
        self._used=self._failed=self._detached=self._claimed=self._queried=False
        self._attempted=set()

    def _check(self, phase):
        if self._permit(phase) is not True:raise FirmwareError('Independent qualification permission was lost.')
        fresh=self._observe()
        if not self._baseline.same_attachment(fresh):raise FirmwareError('The controller attachment/bindings changed.')
        return fresh

    def open(self):
        if self._used:raise FirmwareError('A qualification handle cannot be reopened.')
        self._used=True
        try:
            current=self._check('open')
            expected='/sys/bus/usb/drivers/usbhid' if self._role=='probe' else None
            if current.driver!=expected or (self._role=='probe' and current.hid_report_sha256!=HID_REPORT):
                raise FirmwareError('The observed USB binding does not match this helper role.')
            self._context,self._handle=self._api.open_matching(self._baseline)
            fresh=self._check('opened')
            if fresh.driver!=expected or fresh.hid_report_sha256!=current.hid_report_sha256:
                raise FirmwareError('USB binding changed while opening the handle.')
        except BaseException:
            self._failed=True
            self.close()
            raise

    def _operation(self, kind, expected, returned):
        if kind in self._attempted:raise FirmwareError('An uncertain USB operation cannot be retried.')
        self._attempted.add(kind)
        try:
            if self._check(kind).driver!=expected:raise FirmwareError('USB owner changed before the operation.')
            result=self._api.action(self._handle,kind)
            if self._check(kind+'_returned').driver!=returned:raise FirmwareError('USB binding readback did not confirm the operation.')
            if type(result) is not int or result!=0:raise FirmwareError('USB qualification action failed; no retry is permitted.')
        except BaseException:
            self._failed=True
            raise

    def detach(self):
        if self._role!='probe' or self._failed or not self._handle or self._detached:
            raise FirmwareError('Only the admitted probe can detach interface02 once.')
        self._operation('detach_kernel_driver','/sys/bus/usb/drivers/usbhid',None);self._detached=True

    def claim(self):
        if self._role!='probe' or self._failed or not self._handle or not self._detached or self._claimed:
            raise FirmwareError('A confirmed detach is required before the one-shot claim.')
        self._operation('claim_interface',None,'/sys/bus/usb/drivers/usbfs');self._claimed=True

    def query(self, target, *, deadline):
        if self._role!='probe' or self._failed or not self._handle or not self._claimed or self._queried:
            raise FirmwareError('Version qualification requires the current one-shot claim.')
        self._queried=True
        try:
            if self._check('query').driver!='/sys/bus/usb/drivers/usbfs':raise FirmwareError('Own USB claim readback is missing.')
            channel=UsbVersionChannel(self._api,self._handle,lambda:self._permit('query') is True)
            reply=channel.query(target,deadline=deadline)
            if self._check('query_returned').driver!='/sys/bus/usb/drivers/usbfs':
                raise FirmwareError('USB ownership changed before reply confirmation.')
            return reply
        except BaseException:
            self._failed=True
            raise

    def release(self):
        if self._role!='probe' or not self._handle or not self._claimed:raise FirmwareError('No exact probe claim is available for release.')
        self._operation('release_interface','/sys/bus/usb/drivers/usbfs',None);self._claimed=False

    def attach(self):
        if self._role!='recovery' or self._failed or not self._handle:
            raise FirmwareError('Only the independently admitted recovery helper can attach interface02.')
        self._operation('attach_kernel_driver',None,'/sys/bus/usb/drivers/usbhid')

    def close(self):
        context,handle=self._context,self._handle
        self._context=self._handle=None
        self._failed=True
        self._claimed=False
        if handle is not None:self._api.close_owned(context,handle)
