# SPDX-License-Identifier: BSD-3-Clause
"""One Linux worker for the complete Lenovo controller update.

This experimental build enables the user-authorized hardware trial. It contains
one integration, not a user-selectable transport. No import or startup performs
device operations; only an explicit update action admits the complete plan.
"""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import time
from types import MappingProxyType
import uuid

from controller_firmware import (FirmwareError, RELEASES, validate_image, version_number,
    release_record, release_from_record)
from controller_firmware_transfer import TransferPolicy, InstalledObservation
from diagnostics import event, transition

OTA_ENABLED = True
POLICY = TransferPolicy(None, 1., 5., 15., 180.)
CONTROL = ('left_handle/imu_bypass_enabled','right_handle/imu_bypass_enabled','rumble_intensity',
    'left_handle/rumble_mode','right_handle/rumble_mode','touchpad/enabled',
    'touchpad/vibration_intensity','touchpad/vibration_enabled')
RINGS = ('profile','speed','multi_intensity','brightness','effect','mode','enabled')


def validate_plan(raw, components):
    from controller_firmware_stage import BOOTSTRAP
    namespace = {'__name__':'_plan_validator'}
    exec(BOOTSTRAP, namespace)
    try:
        plan = json.loads(raw, object_pairs_hook=namespace['unique'])
        if (set(plan) not in ({'components','versions'}, {'components','versions','offers'}) or plan['components'] != components or
                not isinstance(plan['versions'],dict) or set(plan['versions']) != set(RELEASES)):
            raise ValueError('plan')
        for value in plan['versions'].values(): version_number(value)
        # Old frozen jobs retain their original reference identity. New jobs
        # always capture live authenticated identities; never consult the feed
        # again after admission or infer success from mutable catalog metadata.
        if 'offers' not in plan:
            plan['offers'] = {c:release_record(RELEASES[c]) for c in components}
        if not isinstance(plan['offers'],dict) or set(plan['offers']) != set(components): raise ValueError('offers')
        for c in components: release_from_record(c,plan['offers'][c])
        return plan
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise FirmwareError('Invalid controller update plan.') from exc


def freeze_plan(images, before, source_root):
    from controller_firmware_process import FrozenBundle, freeze_bundle
    from controller_firmware_stage import PLAN_SOURCES, SOURCES, MAX_SOURCE, BOOTSTRAP, validate_bundle
    components = [image.release.component for image in images]
    if not components or components != [c for c in RELEASES if c in components]:
        raise FirmwareError('A single ordered update plan is required.')
    job = uuid.uuid4().hex
    first = images[0]
    old = freeze_bundle(job, first.release.component, first.original, source_root, release=first.release)
    files = {name: old.files[name] for name in SOURCES}
    root = Path(source_root)
    for name in PLAN_SOURCES - SOURCES:
        path = root/name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_SOURCE:
            raise FirmwareError('Invalid updater source.')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev,opened.st_ino) != (info.st_dev,info.st_ino):
                raise FirmwareError('Updater source changed during staging.')
            data = stream.read(MAX_SOURCE+1)
        if not 1 <= len(data) <= MAX_SOURCE: raise FirmwareError('Updater source exceeds its bound.')
        files[name] = data
    for image in images:
        checked = validate_image(image.release.component, image.original, release=image.release)
        files[checked.release.component+'.bin'] = checked.original
    files['plan.json'] = json.dumps({'components':components,'versions':before['versions'],
        'offers':{image.release.component:release_record(image.release) for image in images}},sort_keys=True).encode('ascii')
    files['bootstrap.py'] = BOOTSTRAP.encode('ascii')
    manifest = {'schema':2,'job_id':job,'components':components,'files':{
        name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()}}
    bundle = FrozenBundle(job,MappingProxyType(files),(json.dumps(manifest,sort_keys=True)+'\n').encode('ascii'))
    validate_bundle(bundle)
    return bundle


def _read_json(path, maximum=131072):
    fd = os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= maximum):
            raise FirmwareError('Updater result is not a private regular file.')
        raw = os.read(fd,maximum+1)
        if len(raw) != info.st_size: raise FirmwareError('Updater result is incomplete.')
        def unique(pairs):
            value = {}
            for key,item in pairs:
                if key in value: raise FirmwareError('Duplicate updater state field.')
                value[key] = item
            return value
        return json.loads(raw,object_pairs_hook=unique)
    finally: os.close(fd)


def _save(directory, name, value):
    # Fixed private worker directory, one writer, atomic durable replacement.
    target = Path(directory)/name
    temporary = target.with_suffix('.tmp')
    raw = json.dumps(value,sort_keys=True).encode('ascii')
    if len(raw) > 131072: raise FirmwareError('Updater state exceeds its bound.')
    fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(fd,raw[offset:])
            if count <= 0: raise OSError('short state write')
            offset += count
        os.fsync(fd)
    finally: os.close(fd)
    os.replace(temporary,target)
    parent = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)


class IndependentInstaller:
    """Backend waits for a systemd worker, never owns its USB transaction."""
    def __init__(self, source_root):
        self.source_root = str(Path(source_root).resolve())
        self.diagnostic_job_id = ''
    def previous_status(self):
        from controller_firmware_stage import RUNTIME_ROOT, _private_directory
        from controller_firmware_process import service_state
        root = Path(RUNTIME_ROOT)
        if not root.exists(): return None
        try:
            parent = _private_directory(str(root),0)
            try: names = os.listdir(parent)
            finally: os.close(parent)
            jobs = [root/name for name in names if re.fullmatch('[0-9a-f]{32}',name)]
            if len(jobs)>128: raise FirmwareError('Updater history exceeds its bound.')
            latest = None
            for job in sorted(jobs,key=lambda p:p.stat().st_mtime,reverse=True):
                if not (job/'launch.claim').exists(): continue
                owned = _private_directory(str(job),0);os.close(owned)
                value = _read_json(job/'status.json') if (job/'status.json').exists() else {'phase':'installing'}
                phase = value.get('phase')
                if phase in ('installing','verifying','restoring'):
                    if service_state(job.name)['state'] not in ('active','activating','deactivating'):
                        phase = 'uncertain'
                elif phase not in ('succeeded','failed_before_write','uncertain'):
                    phase = 'uncertain'
                if phase in ('succeeded','failed_before_write') and (value.get('restored') is not True or value.get('cleanup_complete') is not True):
                    phase = 'uncertain'
                status = {'id':job.name,'phase':phase,'component':'','pending':[],
                    'completed':value.get('completed',[]),'error':value.get('error',''),
                    'message':value.get('message','Controllers updated.' if phase=='succeeded' else
                        'The previous update has an unverified outcome. Do not retry.' if phase=='uncertain' else
                        'The update did not start. Controller input was restored.' if phase=='failed_before_write' else 'Updating controllers.')}
                if phase in ('installing','verifying','restoring','uncertain'): return status
                if latest is None: latest = status
            return latest
        except Exception as exc:
            event('firmware','history_unverified',level='warning',error=exc,repeat=60,stage='history')
            return {'id':'unresolved','phase':'uncertain','component':'','pending':[],'completed':[],
                'error':'An earlier controller update could not be verified. Do not retry.',
                'message':'An earlier controller update could not be verified. Do not retry.'}
    def __call__(self, images, before, progress):
        self.diagnostic_job_id = ''
        if not OTA_ENABLED: raise FirmwareError('Controller installation is disabled in this build.')
        from controller_firmware_stage import provision_root, stage_bundle
        from controller_firmware_process import launch_service, service_state
        provision_root()
        from controller_firmware_stage import RUNTIME_ROOT
        # A lost UI/backend cannot admit another job while an old outcome is
        # unresolved. Durable worker results, never service absence, decide it.
        jobs = list(Path(RUNTIME_ROOT).glob('[0-9a-f]'*32))
        if len(jobs) > 128: raise FirmwareError('Updater history needs inspection.')
        for prior in jobs:
            if (prior/'launch.claim').exists():
                if not (prior/'status.json').exists(): raise FirmwareError('An earlier updater result is unresolved.')
                result = _read_json(prior/'status.json')
                if not (result.get('phase') in ('succeeded','failed_before_write') and result.get('restored') is True and
                        result.get('cleanup_complete') is True and result.get('cleanup_errors')==[]):
                    raise FirmwareError('An earlier updater result is unresolved. Do not retry.')
        stage = stage_bundle(freeze_plan(images,before,self.source_root))
        self.diagnostic_job_id = stage.job_id
        event('firmware','worker_staged',worker_id=stage.job_id,count=len(images))
        launch = launch_service(stage,POLICY,components=len(images))
        event('firmware','worker_submission',worker_id=stage.job_id,
            outcome='submitted' if launch['submitted'] is True else 'unconfirmed',
            level='info' if launch['submitted'] is True else 'warning')
        if launch['submitted'] is not True:
            raise FirmwareError('The updater worker submission could not be confirmed. Do not retry.')
        deadline = time.monotonic()+len(images)*225+70
        result_path = Path(stage.directory)/'status.json'
        while time.monotonic() < deadline:
            if result_path.exists():
                result = _read_json(result_path)
                if result.get('phase') in ('succeeded','uncertain','failed_before_write'):
                    event('firmware','worker_result',worker_id=stage.job_id,phase=result['phase'],
                        restored=result.get('restored') is True,
                        level='warning' if result['phase']=='uncertain' else 'info')
                    return result
                if result.get('phase') in ('installing','verifying','restoring'):
                    progress(result['phase'],result.get('message','Updating controllers.'))
            state = service_state(stage.job_id)
            transition('firmware','worker_service',state['state'],worker_id=stage.job_id,
                level='warning' if state['state']=='unknown' else 'info')
            if state['state'] in ('inactive','failed'):
                raise FirmwareError('The updater exited without a verified result. Do not retry.')
            time.sleep(.5)
        raise FirmwareError('The updater result was not confirmed. Do not retry.')


def stable_binding(observation):
    # Re-enumeration may change address/inode or the descriptor firmware bcd.
    # VID/PID/interface geometry is still independently checked by observation.
    descriptor = bytearray(observation.device_descriptor)
    descriptor[12:14] = b'\0\0'
    value = [observation.boot_id,observation.syspath,observation.bus,observation.ports,
        bytes(descriptor).hex(),observation.other_bindings]
    return hashlib.sha256(json.dumps(value).encode('ascii')).hexdigest()


def console_power():
    supplies = list(Path('/sys/class/power_supply').iterdir())
    if len(supplies) > 32: raise FirmwareError('Power supply inventory exceeds its bound.')
    ac, batteries = False, []
    for path in supplies:
        kind = (path/'type').read_text().strip()
        if (path/'scope').exists() and (path/'scope').read_text().strip() == 'Device':
            continue
        if kind in ('Mains','USB','USB_C','USB_PD') and (path/'online').exists():
            ac = ac or (path/'online').read_text().strip() == '1'
        if kind == 'Battery' and (path/'scope').exists() and (path/'scope').read_text().strip() == 'System':
            batteries.append(int((path/'capacity').read_text().strip()))
    if len(batteries) > 1: raise FirmwareError('Console battery could not be identified.')
    percent = batteries[0] if batteries else None
    if not ac and (percent is None or not 30 <= percent <= 100):
        raise FirmwareError('Connect power or charge the console to at least 30%.')
    return ac, percent


class LinuxPlan:
    """Concrete single-owner adapter; no additional watcher/heartbeat daemon."""
    def __init__(self, directory, plan, progress):
        from controller_firmware_usb_linux import observe_command_interface, LinuxUsbOwnership
        from controller_firmware_sleep import FirmwareInhibitor
        self.directory,self.plan,self.progress = Path(directory),plan,progress
        self.observe_usb = observe_command_interface
        self.api = LinuxUsbOwnership()
        self.baseline = self.observe_usb()
        self.binding = stable_binding(self.baseline)
        self.inhibitor = FirmwareInhibitor()
        self.context = self.handle = self.gate = None
        self.claim = None
        self.detached = self.claimed = self.input_stopped = False
        self.data = None
        self.verification = None
        self.diagnostic_stage = 'qualification'

    def same(self):
        fresh = self.observe_usb()
        if stable_binding(fresh) != self.binding:
            raise FirmwareError('A different controller appeared during the update.')
        return fresh

    @staticmethod
    def service():
        from controller_firmware_process import _environment
        result = subprocess.run(['/usr/bin/systemctl','show','inputplumber.service',
            '-p','ActiveState','-p','SubState','-p','MainPID','-p','ControlGroup'],
            capture_output=True,text=True,timeout=3,env=_environment())
        if result.returncode: raise FirmwareError('Input service could not be observed.')
        return dict(line.split('=',1) for line in result.stdout.splitlines())

    @staticmethod
    def set_service(action):
        from controller_firmware_process import _environment
        subprocess.run(['/usr/bin/systemctl',action,'inputplumber.service'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            check=True,timeout=12,env=_environment())

    @staticmethod
    def attributes(observation):
        base = Path(observation.syspath)
        roots = list((base/(base.name+':1.2')).glob('0003:17EF:61EB.*'))
        if len(roots) != 1: raise FirmwareError('Controller attributes are unavailable.')
        paths = {'control/'+name:roots[0]/name for name in CONTROL}
        paths.update({'rings/'+name:Path('/sys/class/leds/go:rgb:joystick_rings')/name for name in RINGS})
        return paths

    def fence(self):
        from controller_maintenance import ROOT, _directory, _read
        if self.claim is None: return False
        parent = _directory(ROOT,0)
        try:
            info,token = _read(parent,0)
            return self.claim == ((info.st_dev,info.st_ino),token)
        finally: os.close(parent)

    def valid(self):
        return self.fence() and self.inhibitor.valid()

    def consumers(self, observation):
        usb = '/dev/bus/usb/%03d/%03d'%(observation.bus,observation.address)
        base = Path(observation.syspath)
        interface = base/(base.name+':1.2')
        targets = {usb}
        # Saved source indices can disappear or belong to another device after
        # a USB/HID rebind. Only nodes currently under this freshly qualified
        # command interface establish ownership; the restore snapshot matches
        # physical source roles separately after the driver returns.
        for node in interface.glob('0003:17EF:61EB.*/hidraw/hidraw*'):
            if re.fullmatch(r'hidraw[0-9]+',node.name): targets.add('/dev/'+node.name)
        for node in interface.glob('0003:17EF:61EB.*/input/input*/event*'):
            if re.fullmatch(r'event[0-9]+',node.name): targets.add('/dev/input/'+node.name)
        for process in Path('/proc').glob('[0-9]*'):
            for fd in (process/'fd').glob('*'):
                try: target = os.readlink(fd).removesuffix(' (deleted)')
                except FileNotFoundError: continue
                if target in targets and not (int(process.name) == os.getpid() and target == usb):
                    raise FirmwareError('Another process owns the controller interface. Close Steam before updating.')

    def message(self, action):
        self.gate.sendall(('{"action":"'+action+'"}\n').encode())
        raw = b''
        while not raw.endswith(b'\n'):
            piece = self.gate.recv(1024)
            if not piece or len(raw)+len(piece) > 1024: raise FirmwareError('Updater coordination failed.')
            raw += piece
        return json.loads(raw)

    def begin(self):
        from controller_firmware_input import snapshot
        from controller_maintenance import ROOT, NAME, SOCKET_NAME, _directory, _read
        self.diagnostic_stage = 'input_readiness'
        if self.baseline.driver != '/sys/bus/usb/drivers/usbhid' or self.service()['ActiveState'] != 'active':
            raise FirmwareError('The controller input service is not ready.')
        self.diagnostic_stage = 'power';console_power()
        self.diagnostic_stage = 'inhibition'
        self.inhibitor.acquire(time.monotonic()+5)
        self.diagnostic_stage = 'coordination'
        parent = _directory(ROOT,0)
        try:
            info = os.stat(SOCKET_NAME,dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise FirmwareError('Companion updater endpoint is unavailable.')
        finally: os.close(parent)
        self.gate = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.gate.settimeout(8);self.gate.connect(ROOT+'/'+SOCKET_NAME)
        pid,uid,_ = struct.unpack('3i',self.gate.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != 0 or '/system.slice/plugin_loader.service' not in Path(f'/proc/{pid}/cgroup').read_text():
            raise FirmwareError('The updater endpoint belongs to an unexpected process.')
        if (Path(ROOT)/NAME).exists(): raise FirmwareError('An earlier controller updater still owns maintenance.')
        self.diagnostic_stage = 'input_snapshot'
        fresh = self.same();paths = self.attributes(fresh)
        usb = dataclasses.asdict(fresh);usb['device_descriptor'] = fresh.device_descriptor.hex()
        self.data = {'usb':usb,'binding':self.binding,'input':snapshot(fresh),
            'attributes':{key:path.read_text().strip() for key,path in paths.items()},
            'claim_identity':None,'claim_token':None,'input_stopped':False,'pause_requested':True,
            'backend':{'pid':pid,'start':Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]}}
        # A crash after pause but before the detailed post-drain snapshot can
        # release maintenance without rewriting any controller preferences.
        _save(self.directory,'recovery.json',self.data)
        self.diagnostic_stage = 'pause'
        if self.message('pause') != {'paused':True}: raise FirmwareError('Companion could not pause controller operations.')
        event('firmware','controller_paused',worker_id=self.directory.name)
        parent = _directory(ROOT,0)
        try: info,token = _read(parent,0);self.claim = ((info.st_dev,info.st_ino),token)
        finally: os.close(parent)
        fresh = self.same()
        paths = self.attributes(fresh)
        usb = dataclasses.asdict(fresh);usb['device_descriptor'] = fresh.device_descriptor.hex()
        self.data.update(usb=usb,input=snapshot(fresh),
            attributes={key:path.read_text().strip() for key,path in paths.items()},
            claim_identity=list(self.claim[0]),claim_token=token)
        _save(self.directory,'recovery.json',self.data)
        # Durable restore snapshot exists BEFORE stopping a consumer or detaching.
        self.data['input_stopped'] = True;_save(self.directory,'recovery.json',self.data)
        self.diagnostic_stage = 'input_stop'
        self.input_stopped = True;self.set_service('stop')
        state = self.service()
        if state['ActiveState'] != 'inactive' or state['SubState'] != 'dead' or state['MainPID'] != '0':
            raise FirmwareError('Input service did not release the controller.')
        self.diagnostic_stage = 'exclusive_ownership'
        self.consumers(fresh)
        self.open_owned(fresh)
        # All actual versions checked on the owned wire before the first C0.
        self.diagnostic_stage = 'version_preflight'
        for component, value in self.plan['versions'].items():
            if self.query(component,time.monotonic()+2) != value:
                raise FirmwareError('Controller versions changed since checking for updates.')

    def open_owned(self, fresh):
        self.context,self.handle = self.api.open_matching(fresh)
        if fresh.driver == '/sys/bus/usb/drivers/usbhid':
            if self.api.action(self.handle,'detach_kernel_driver') != 0:
                raise FirmwareError('Controller input ownership could not be released.')
            self.detached = True
        elif fresh.driver is not None: raise FirmwareError('An unexpected driver owns the controller.')
        if self.same().driver is not None or self.api.action(self.handle,'claim_interface') != 0:
            raise FirmwareError('Exclusive controller ownership could not be acquired.')
        self.claimed = True

    def query(self, component, deadline):
        from controller_firmware_usb import UsbVersionChannel
        from controller_firmware_protocol import VERSION_QUERY_TARGETS, parse_firmware_version
        report = UsbVersionChannel(self.api,self.handle,self.valid).query(VERSION_QUERY_TARGETS[component],deadline=deadline)
        return parse_firmware_version(report,component)

    def generation(self, component, deadline):
        from controller_firmware_usb import UsbVersionChannel
        from controller_firmware_protocol import VERSION_QUERY_TARGETS, parse_hardware_generation
        report = UsbVersionChannel(self.api,self.handle,self.valid).query(
            VERSION_QUERY_TARGETS[component],deadline=deadline,subcommand=6)
        return parse_hardware_generation(report,component)

    def release_handle(self):
        context,handle = self.context,self.handle
        self.context = self.handle = None
        claimed,self.claimed = self.claimed,False
        if handle is not None:
            try:
                if claimed:
                    rc = self.api.action(handle,'release_interface')
                    if rc not in (0,-4): raise FirmwareError('Controller interface release failed.')
            finally: self.api.close_owned(context,handle)

    def transport(self, component):
        from controller_firmware_ota import OtaTransport
        from controller_firmware_worker import ControllerClaim
        from controller_firmware_protocol import Preflight
        from controller_firmware_usb import Libusb
        self.consumers(self.same())
        observed_at = time.monotonic()
        generation = self.generation(component,time.monotonic()+2)
        version = self.query(component,time.monotonic()+2)
        deadline = time.monotonic()+1
        attachment = None
        while time.monotonic() < deadline:
            rc,count,report = Libusb.interrupt(self.api,self.handle,0x83,bytes(64),50)
            if rc == -7 and count == 0: continue
            if rc != 0 or count != 64 or report[:3] != b'\x04\x3c\x74':
                raise FirmwareError('Fresh controller attachment status is unavailable.')
            attachment = report[12] == 2 and report[13] == 2
            break
        if attachment is not True: raise FirmwareError('Attach both controllers before updating firmware.')
        ac,battery = console_power();epoch = uuid.uuid4().hex
        claim = ControllerClaim(self.binding,epoch,Preflight('Legion Go 8ASP2',generation,version,True,True,
            ac,battery,epoch,observed_at))
        self.verification = None
        owner = self
        class RawApi:
            def interrupt(self,handle,endpoint,data,timeout):
                if not owner.valid(): raise FirmwareError('Controller updater ownership was lost.')
                return Libusb.interrupt(owner.api,handle,endpoint,data,timeout)
        return OtaTransport(component,RawApi(),self.handle,claim,self.valid,self.verify,
            lambda:None)  # whole plan owns the physical handle

    def verify(self, component, token, deadline):
        from controller_firmware_protocol import (firmware_version_report, hardware_generation_report,
            parse_hardware_generation, VERSION_QUERY_TARGETS)
        from controller_firmware_usb import Libusb
        self.progress('verifying','Verifying controller firmware.')
        if self.verification is None:
            # A0 is completion, not success. Version query below is sent once.
            self.verification = {'component':component,'token':token,'sent':False,'reconnects':0,
                'query':'generation','generation':None}
        if self.verification['component'] != component or self.verification['token'] != token:
            raise FirmwareError('Controller version query ownership changed.')
        try:
            fresh = self.same()
        except (FileNotFoundError,FirmwareError):
            time.sleep(max(0,deadline-time.monotonic()))
            return None
        # A version query is read-only. If its receiver re-enumerates, discard
        # the old handle and issue a NEW query on the same qualified attachment.
        # No OTA command is retried. Bound even repeated reconnects.
        current = self.baseline
        if (fresh.address,fresh.instance) != (current.address,current.instance):
            self.verification['reconnects'] += 1
            if self.verification['reconnects'] > 2: raise FirmwareError('Controller reconnection did not settle.')
            self.release_handle();self.open_owned(fresh);self.baseline = fresh
            self.verification['sent'] = False
            self.verification['query'] = 'generation';self.verification['generation'] = None
        if not self.verification['sent']:
            report = hardware_generation_report(component) if self.verification['query']=='generation' else firmware_version_report(component)
            self.verification['sent'] = True  # no resend after uncertain output
            rc,count,_ = Libusb.interrupt(self.api,self.handle,3,report,max(1,min(100,int((deadline-time.monotonic())*1000))))
            if rc != 0 or count != 64: raise FirmwareError('Post-update version query failed.')
        while time.monotonic() < deadline:
            timeout = int((deadline-time.monotonic())*1000)
            if timeout < 1: break
            rc,count,report = Libusb.interrupt(self.api,self.handle,0x83,bytes(64),min(timeout,100))
            if rc == -7 and count == 0: continue
            if rc == -4 and count == 0:
                time.sleep(max(0,deadline-time.monotonic()))
                return None
            if rc != 0 or count != 64: raise FirmwareError('Post-update version reply was incomplete.')
            if report[:2] == b'\x04\x3c': continue
            command = 6 if self.verification['query']=='generation' else 4
            if report[:5] != bytes((4,0,2,command,VERSION_QUERY_TARGETS[component])):
                raise FirmwareError('Unexpected post-update reply.')
            if command==6:
                self.verification['generation'] = parse_hardware_generation(report,component)
                self.verification['query'] = 'version';self.verification['sent'] = False
                time.sleep(max(0,deadline-time.monotonic()))
                return None
            return InstalledObservation(self.binding,'Legion Go 8ASP2',self.verification['generation'],uuid.uuid4().hex,
                time.monotonic(),token,report)
        time.sleep(max(0,deadline-time.monotonic()))
        return None

    def journal(self, component):
        from controller_firmware_journal import FirmwareJournal
        path = self.directory/component
        path.mkdir(mode=0o700)
        return FirmwareJournal(str(path),self.baseline.boot_id)

    def restore(self):
        from controller_firmware_input import restore, equivalent, snapshot
        from controller_firmware_usb_linux import HID_REPORT
        self.diagnostic_stage = 'restore_handles'
        self.release_handle()
        if self.data is None:
            # The complete snapshot is assigned before any pause request. A
            # connected socket alone never admitted input/USB mutations.
            if self.gate is not None: self.gate.close();self.gate = None
            return True
        if self.claim is None:
            self.diagnostic_stage = 'restore_unclaimed_pause'
            from controller_maintenance import ROOT, NAME, _directory, _read
            backend = self.data['backend']
            current = Path(f"/proc/{backend['pid']}/stat").read_text().rsplit(')',1)[1].split()[19]
            if current != backend['start']: raise FirmwareError('The original maintenance backend exited.')
            deadline = time.monotonic()+20
            while not (Path(ROOT)/NAME).exists() and time.monotonic()<deadline: time.sleep(.1)
            if not (Path(ROOT)/NAME).exists() and not self.data['input_stopped']:
                # Pause can fail after closing Companion's local admission but
                # before publishing a marker. No USB/input mutation was admitted.
                # Reconcile only the original live backend and qualified input;
                # its private recovery callback must drain its own hold again.
                fresh = self.same()
                if fresh.driver != '/sys/bus/usb/drivers/usbhid' or self.service()['ActiveState'] != 'active':
                    raise FirmwareError('Controller state changed before updater handoff.')
                if self.gate is not None: self.gate.close();self.gate = None
                return self.resume_gate()
            parent = _directory(ROOT,0)
            try:
                info,token = _read(parent,0)
                self.claim = ((info.st_dev,info.st_ino),token)
            finally: os.close(parent)
        self.diagnostic_stage = 'restore_fence'
        if not self.fence(): raise FirmwareError('Controller maintenance ownership was lost.')
        fresh = self.same()
        if not self.data['input_stopped']:
            if fresh.driver != '/sys/bus/usb/drivers/usbhid' or self.service()['ActiveState'] != 'active':
                raise FirmwareError('Controller state changed before updater handoff.')
            # No updater hardware mutation occurred. Preserve normal operations
            # which completed while Companion was draining; do not roll them back.
            return self.resume_gate()
        if fresh.driver is None:
            self.diagnostic_stage = 'restore_driver'
            self.context,self.handle = self.api.open_matching(fresh)
            try:
                if self.api.action(self.handle,'attach_kernel_driver') != 0:
                    raise FirmwareError('Controller driver could not be restored.')
            finally: self.release_handle()
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            fresh = self.same()
            if fresh.driver == '/sys/bus/usb/drivers/usbhid' and fresh.hid_report_sha256 == HID_REPORT:
                break
            time.sleep(.1)
        else: raise FirmwareError('Controller input did not return.')
        # Driver initialization may recreate attributes; bounded settled check.
        time.sleep(.6)
        for key,path in self.attributes(fresh).items():
            self.same()
            value = self.data['attributes'][key]
            if path.read_text().strip() != value: path.write_text(value)
        self.diagnostic_stage = 'restore_input_service'
        if self.input_stopped: self.set_service('start')
        while time.monotonic() < deadline:
            try: snapshot(self.same(),restoring=True);break
            except (FileNotFoundError,FirmwareError): time.sleep(.1)
        else: raise FirmwareError('Controller input composite did not return.')
        self.diagnostic_stage = 'restore_input_configuration'
        after = restore(self.data['input'],self.same(),deadline=deadline)
        if (not equivalent(self.data['input'],after) or
                any(path.read_text().strip() != self.data['attributes'][key] for key,path in self.attributes(self.same()).items())):
            raise FirmwareError('Controller settings were not restored.')
        return self.resume_gate()

    def resume_gate(self):
        self.diagnostic_stage = 'restore_backend'
        action = 'restored' if self.gate is not None else 'recover'
        if self.gate is None:
            from controller_maintenance import ROOT, SOCKET_NAME
            self.gate = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.gate.settimeout(8);self.gate.connect(ROOT+'/'+SOCKET_NAME)
            pid,uid,_ = struct.unpack('3i',self.gate.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid != 0 or '/system.slice/plugin_loader.service' not in Path(f'/proc/{pid}/cgroup').read_text():
                raise FirmwareError('Companion recovery endpoint changed.')
        if self.message(action) != {'resumed':True}: raise FirmwareError('Companion maintenance did not complete.')
        self.gate.close();self.gate = None
        return True


def run_staged(directory):
    event('firmware','worker_started',worker_id=Path(directory).name)
    if not OTA_ENABLED or os.name != 'posix' or os.geteuid() != 0:
        raise FirmwareError('Controller installation is disabled in this build.')
    from controller_firmware_worker import run_update_plan
    manifest = _read_json(Path(directory)/'manifest.json',16384)
    plan = validate_plan((Path(directory)/'plan.json').read_bytes(),manifest['components'])
    images = tuple(validate_image(c,(Path(directory)/(c+'.bin')).read_bytes(),
        release=release_from_record(c,plan['offers'][c])) for c in plan['components'])
    def progress(phase,message): _save(directory,'status.json',{'phase':phase,'message':message})
    linux = None
    try:
        # Construction only loads libusb and passively qualifies the attachment.
        # A transient failure here has not admitted any input/USB operation.
        linux = LinuxPlan(directory,plan,progress)
        linux.begin()
        result = run_update_plan(images,plan['versions'],POLICY,linux.transport,linux.journal,
            linux.inhibitor,lambda:linux.valid(),linux.restore,progress)
    except Exception as exc:
        event('firmware','worker_start_failed',level='warning',error=exc,
            worker_id=Path(directory).name,stage=getattr(linux,'diagnostic_stage','qualification'))
        restored = linux is None
        try:
            if linux is not None: restored = linux.restore() is True
        except Exception as exc:
            event('firmware','restore_failed',level='error',error=exc,worker_id=Path(directory).name,
                stage=getattr(linux,'diagnostic_stage','restoration'))
        finally:
            if linux is not None: linux.inhibitor.close()
        result = {'phase':'failed_before_write' if restored else 'uncertain','restored':restored,
            'versions':plan['versions'],'completed':[],'cleanup_complete':restored,
            'cleanup_errors':[] if restored else ['restoration'],'error':'The controller update could not start.'}
    finally:
        if linux is not None and linux.gate is not None: linux.gate.close();linux.gate = None
    _save(directory,'status.json',result)
    event('firmware','worker_finished',worker_id=Path(directory).name,phase=result['phase'],
        restored=result.get('restored') is True,level='warning' if result['phase']=='uncertain' else 'info')
    return 0 if result['phase'] == 'succeeded' else 2


def cleanup_staged(directory):
    """ExecStopPost runs only AFTER all worker processes/USB FDs have exited.

    It restores input/settings, never sends OTA, retries a block, or rolls back
    firmware. Interrupted firmware outcome stays uncertain even after restore.
    No autonomous bootloader recovery is claimed by this path.
    """
    directory = Path(directory)
    if not (directory/'recovery.json').exists(): return 0
    event('firmware','recovery_started',worker_id=directory.name)
    data = _read_json(directory/'recovery.json')
    proof_errors = []
    result = {}
    if (directory/'status.json').exists():
        try:
            result = _read_json(directory/'status.json')
            if result.get('restored') is True: return 0
        except Exception as exc:
            event('firmware','recovery_evidence_invalid',level='warning',error=exc,worker_id=directory.name,stage='status')
            proof_errors.append('status evidence');result = {}
    captured = _read_json(directory/'plan.json')
    plan = validate_plan(json.dumps(captured).encode('ascii'),captured['components'])
    completed = []
    versions = dict(plan['versions'])
    # Progress/status can be interrupted between sessions. Retain authenticated
    # per-component success journals without replaying them or claiming that the
    # incomplete overall update succeeded.
    from controller_firmware_journal import recovered_record
    for component in plan['components']:
        release = release_from_record(component,plan['offers'][component])
        path = directory/component/'journal.json'
        if path.exists():
            try:
                record = recovered_record(json.dumps(_read_json(path,16384)).encode('ascii'))
                value = record['snapshot']
                if (value['phase']=='succeeded' and value['component']==component and
                        value['image_sha256']==release.sha256 and value['total_bytes']==release.size and
                        value['offered_version']==release.version):
                    if component not in completed: completed.append(component)
                    versions[component] = value['offered_version']
            except Exception as exc:
                event('firmware','recovery_evidence_invalid',level='warning',error=exc,worker_id=directory.name,scope=component,stage='journal')
                # Optional completion evidence is not a prerequisite for input
                # restoration from the protected recovery snapshot and plan.
                proof_errors.append(component+' journal')
    def progress(phase,message): pass
    linux = LinuxPlan(str(directory),plan,progress)
    linux.binding = data['binding'];linux.data = data
    linux.input_stopped = data['input_stopped']
    linux.claim = (tuple(data['claim_identity']),data['claim_token']) if data['claim_identity'] is not None else None
    try:
        # Native exclusive claim cannot be reused by the dead worker. Still
        # reject a foreign raw handle before restoration of the HID driver.
        if linux.data['input_stopped']: linux.consumers(linux.same())
        restored = linux.restore() is True
    except Exception as exc:
        event('firmware','recovery_failed',level='error',error=exc,worker_id=directory.name,
            stage=getattr(linux,'diagnostic_stage','restoration'))
        restored = False
    # Our old writer is gone. Only its fixed private interrupted temp files may
    # be removed; never change images, source, journal, prefs or foreign files.
    for name in ('status.tmp','recovery.tmp'):
        temporary = directory/name
        if temporary.exists():
            fd = os.open(temporary,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or
                        stat.S_IMODE(info.st_mode)!=0o600): raise FirmwareError('Invalid interrupted updater state.')
            finally: os.close(fd)
            temporary.unlink()
    _save(directory,'status.json',{'phase':'uncertain','restored':restored,
        'completed':completed,'versions':versions,'cleanup_complete':restored and not proof_errors,
        'cleanup_errors':proof_errors+([] if restored else ['restoration']),
        'error':'The update was interrupted. Its firmware outcome is unverified. Do not retry.'})
    event('firmware','recovery_finished',worker_id=directory.name,restored=restored,count=len(proof_errors),
        outcome='input_restored' if restored else 'unverified',level='info' if restored else 'error')
    return 0 if restored else 2
