# SPDX-License-Identifier: BSD-3-Clause
"""Integration contracts; fake USB and source staging are not flash evidence."""
from dataclasses import replace
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import controller_firmware as fw
import controller_firmware_install as install
import controller_firmware_process as process
import controller_firmware_stage as stage
from controller_firmware_usb_linux import UsbObservation

REPO = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_enabled_staged_coordinator_restores_after_success_or_changed_version(self):
        # Exercise the actual coordinator and worker with simulated OS/device
        # boundaries. Synthetic scheduler bytes are not authentication evidence.
        import controller_firmware_input as inputs
        import controller_firmware_sleep as sleep
        import controller_firmware_transfer as transfer
        import controller_firmware_usb_linux as usb
        import controller_firmware_worker as worker
        import controller_maintenance as maintenance
        from firmware_worker_fixture import Transport, Journal, Inhibitor
        from test_controller_firmware_input import snapshot

        class Clock:
            def __call__(self): return time.monotonic()
            def advance(self, amount=.001): pass
            @property
            def value(self): return self()
            @value.setter
            def value(self, deadline): time.sleep(max(0,deadline-self()))

        for changed_version in (False,True):
            with self.subTest(changed_version=changed_version),tempfile.TemporaryDirectory() as temporary,ExitStack() as seams:
                root=Path(temporary);events=[];records=[];transports=[]
                versions={c:'250101A' for c in fw.RELEASES}
                (root/'plan.json').write_text(json.dumps({'components':['left'],'versions':versions}))
                (root/'left.bin').write_bytes(b'X'*65)
                attribute=root/'preference';attribute.write_text('saved')
                cgroup=root/'cgroup';cgroup.write_text('0::/system.slice/plugin_loader.service\n')
                process_stat=root/'stat';process_stat.write_text('123 (backend) '+' '.join(['0']*20))
                state={'service':'active','driver':'/sys/bus/usb/drivers/usbhid'}
                baseline=UsbObservation('a1c2d3e4-1234-4321-abcd-0123456789ab',
                    '/sys/devices/usb3/3-1',(1,2),3,2,(1,),bytes(18),(),state['driver'],usb.HID_REPORT)
                def observe():
                    return replace(baseline,driver=state['driver'],
                        hid_report_sha256=usb.HID_REPORT if state['driver']==baseline.driver else None)
                class Api:
                    def open_matching(self, observation): events.append('open');return 1,2
                    def action(self, handle, action):
                        events.append(action)
                        state['driver']={'detach_kernel_driver':None,'claim_interface':'usbfs',
                            'release_interface':None,'attach_kernel_driver':baseline.driver}[action]
                        return 0
                    def close_owned(self, context, handle): events.append('close_usb')
                class Gate:
                    def settimeout(self, value): pass
                    def connect(self, path): events.append('connect')
                    def getsockopt(self, *args): return struct.pack('3i',123,0,0)
                    def sendall(self, raw):
                        action=json.loads(raw)['action'];events.append(action)
                        if action=='pause': (root/maintenance.NAME).touch()
                        else: (root/maintenance.NAME).unlink()
                        self.reply=json.dumps({'paused':True} if action=='pause' else {'resumed':True}).encode()+b'\n'
                    def recv(self, size): return self.reply
                    def close(self): events.append('close_gate')
                def service_action(action):
                    events.append(action);state['service']='inactive' if action=='stop' else 'active'
                def save(directory, name, value):
                    events.append(name);records.append((name,copy.deepcopy(value)))
                def mapped_path(value):
                    return cgroup if str(value)=='/proc/123/cgroup' else process_stat if str(value)=='/proc/123/stat' else Path(value)
                def transport(plan, component):
                    self.assertTrue(plan.claimed);self.assertIsNotNone(plan.handle)
                    attribute.write_text('changed')
                    value=Transport(Clock(),events);transports.append(value);return value
                def validate(component, data, release=None):
                    return fw.ValidatedImage(release or fw.release_for(component),data)
                socket_info=SimpleNamespace(st_mode=stat.S_IFSOCK|0o600,st_uid=0)
                claim_info=SimpleNamespace(st_dev=1,st_ino=2)
                os_boundary=SimpleNamespace(**dict(vars(os),name='posix',geteuid=lambda:0,
                    stat=lambda *a,**k:socket_info,close=lambda fd:None))
                for target,name,value in (
                    (install,'OTA_ENABLED',True),(install,'os',os_boundary),(install,'Path',mapped_path),
                    (install,'_read_json',lambda *a:{'components':['left']}),
                    (install,'_save',save),(install,'console_power',lambda:(True,80)),
                    (install,'socket',SimpleNamespace(socket=lambda *a:Gate(),AF_UNIX=1,
                        SOCK_STREAM=1,SOL_SOCKET=1,SO_PEERCRED=17)),
                    (maintenance,'ROOT',str(root)),(maintenance,'_directory',lambda *a:99),
                    (maintenance,'_read',lambda *a:(claim_info,'owned')),
                    (usb,'observe_command_interface',observe),(usb,'LinuxUsbOwnership',Api),
                    (sleep,'FirmwareInhibitor',lambda:Inhibitor(events)),
                    (inputs,'snapshot',lambda *a,**k:snapshot()),
                    (inputs,'restore',lambda saved,*a,**k:copy.deepcopy(saved)),
                    (install.LinuxPlan,'service',staticmethod(lambda:{'ActiveState':state['service'],
                        'SubState':'dead' if state['service']=='inactive' else 'running','MainPID':'0'})),
                    (install.LinuxPlan,'set_service',staticmethod(service_action)),
                    (install.LinuxPlan,'attributes',staticmethod(lambda *a:{'preference':attribute})),
                    (install.LinuxPlan,'consumers',lambda *a:None),
                    (install.LinuxPlan,'query',lambda self,c,d:'250102A' if changed_version else versions[c]),
                    (install.LinuxPlan,'transport',transport),
                    (install.LinuxPlan,'journal',lambda *a:Journal(events)),
                    (install,'validate_image',validate),(worker,'validate_image',validate),(transfer,'validate_image',validate)):
                    seams.enter_context(patch.object(target,name,value))
                result=install.run_staged(str(root))
                outcome=next(value for name,value in reversed(records) if name=='status.json')
                self.assertEqual(result,2 if changed_version else 0)
                self.assertEqual(outcome['phase'],'failed_before_write' if changed_version else 'succeeded')
                self.assertTrue(outcome['restored']);self.assertTrue(outcome['cleanup_complete'])
                self.assertEqual(len(transports),0 if changed_version else 1)
                self.assertEqual(attribute.read_text(),'saved')
                self.assertEqual(state['service'],'active');self.assertEqual(state['driver'],baseline.driver)
                self.assertFalse((root/maintenance.NAME).exists())
                for event in ('recovery.json','stop','start','restored','inhibitor'):
                    self.assertIn(event,events)
                self.assertLess(events.index('recovery.json'),events.index('stop'))
                self.assertLess(events.index('start'),events.index('restored'))
                self.assertLess(events.index('restored'),events.index('inhibitor'))

    def test_slow_power_read_does_not_refresh_earlier_preflight_observations(self):
        from controller_firmware_usb import Libusb
        value=install.LinuxPlan.__new__(install.LinuxPlan)
        value.same=lambda:None;value.consumers=lambda _:None
        value.generation=lambda *_:2;value.query=lambda *_:'251204A'
        value.valid=lambda:True;value.verify=lambda *a:None
        value.handle=111;value.api=None;value.binding='a'*64
        clock=[0.]
        def power():clock[0]=5.;return True,80
        report=bytearray(64);report[:3]=b'\x04\x3c\x74';report[12]=report[13]=2
        with patch.object(install.time,'monotonic',side_effect=lambda:clock[0]),\
                patch.object(install,'console_power',side_effect=power),\
                patch.object(Libusb,'interrupt',return_value=(0,64,bytes(report))):
            transport=value.transport('left')
            with self.assertRaises(fw.FirmwareError):
                transport.claim.preflight.require('270101A',transport.claim.connection_epoch,clock[0])

    def test_malformed_optional_journal_cannot_prevent_input_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'recovery.json').touch();(root/'left').mkdir();(root/'left/journal.json').touch()
            versions={c:'250101A' for c in fw.RELEASES}
            events=[];saved=[]
            def read(path,*args):
                if path.name=='journal.json':raise fw.FirmwareError('Broken journal')
                if path.name=='plan.json':return {'components':['left'],'versions':versions}
                return {'binding':'a'*64,'input_stopped':True,'claim_identity':[1,2],'claim_token':'old'}
            class Plan:
                def __init__(self,*args):pass
                def consumers(self,*a):events.append('consumers')
                def same(self):return None
                def restore(self):events.append('restore');return True
            with patch.object(install,'_read_json',side_effect=read),patch.object(install,'LinuxPlan',Plan),\
                    patch.object(install,'_save',side_effect=lambda d,n,v:saved.append(v)):
                self.assertEqual(install.cleanup_staged(str(root)),0)
            self.assertIn('restore',events)
            self.assertEqual(saved[-1]['phase'],'uncertain');self.assertTrue(saved[-1]['restored'])
            self.assertFalse(saved[-1]['cleanup_complete']);self.assertIn('left journal',saved[-1]['cleanup_errors'])
    def test_enabled_status_reports_admitted_installer_without_starting_it(self):
        def installer(*args):raise AssertionError('status must never install')
        status=fw.FirmwarePreparation(installer=installer).status()
        self.assertTrue(status['flash_available'])
        self.assertEqual(status['job']['phase'],'idle')
        self.assertIn('qualification is pending',status['reason'])

    def test_device_scoped_usb_power_cannot_authorize_console_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name,fields in {'controller':{'type':'USB','online':'1','scope':'Device'},
                    'console':{'type':'Battery','scope':'System','capacity':'20'}}.items():
                folder=root/name;folder.mkdir()
                for key,value in fields.items():(folder/key).write_text(value)
            real_path=Path
            def mapped(value):return root if value=='/sys/class/power_supply' else real_path(value)
            with patch.object(install,'Path',side_effect=mapped):
                with self.assertRaises(fw.FirmwareError):install.console_power()
                (root/'console'/'capacity').write_text('30')
                self.assertEqual(install.console_power(),(False,30))

    def test_generation_query_is_02_06_uint8_not_protocol_or_be32(self):
        from controller_firmware_protocol import hardware_generation_report, parse_hardware_generation
        for component,target in (('left',3),('right',4),('receiver',1)):
            self.assertEqual(hardware_generation_report(component),bytes((5,0,2,6,target))+bytes(59))
            reply=bytes((4,0,2,6,target,2))+bytes(58)
            self.assertEqual(parse_hardware_generation(reply,component),2)
            for wrong in (reply[:3]+b'\3'+reply[4:],reply[:5]+b'\1'+reply[6:],reply[:5]+b'\0\0\0\2'+bytes(55)):
                with self.assertRaises(fw.FirmwareError):parse_hardware_generation(wrong,component)

    def test_post_update_reconnect_after_query_uses_new_handle_and_fresh_generation(self):
        from controller_firmware_usb import Libusb
        value=install.LinuxPlan.__new__(install.LinuxPlan)
        old=SimpleNamespace(address=2,instance=(1,2))
        fresh=SimpleNamespace(address=4,instance=(1,4))
        value.baseline=old;value.handle=111;value.api=None;value.binding='a'*64
        value.verification={'component':'left','token':'token','sent':True,'reconnects':0,'query':'version','generation':2}
        value.same=lambda:fresh;value.progress=lambda *a:None
        events=[]
        value.release_handle=lambda:events.append('release')
        def reopen(observation):events.append('reopen');value.handle=222
        value.open_owned=reopen
        def io(api,handle,endpoint,data,timeout):
            self.assertEqual(handle,222)
            if endpoint==3:
                events.append(data[3]);return 0,64,bytes(64)
            return 0,64,bytes((4,0,2,6,3,2))+bytes(58)
        with patch.object(Libusb,'interrupt',side_effect=io):
            self.assertIsNone(value.verify('left','token',install.time.monotonic()+.01))
        self.assertEqual(events,['release','reopen',6])
        self.assertEqual(value.verification['query'],'version')
        self.assertEqual(value.verification['generation'],2)

    def test_no_hardware_mutation_unwind_never_rewrites_pre_drain_preferences(self):
        value=install.LinuxPlan.__new__(install.LinuxPlan)
        value.release_handle=lambda:None
        value.data={'input_stopped':False};value.claim=('owned','token')
        value.fence=lambda:True;value.same=lambda:SimpleNamespace(driver='/sys/bus/usb/drivers/usbhid')
        value.service=lambda:{'ActiveState':'active'};value.resume_gate=lambda:True
        value.attributes=lambda *a:(_ for _ in ()).throw(AssertionError('must not restore old preferences'))
        self.assertTrue(value.restore())

    def test_connected_gate_before_snapshot_never_implies_a_pause_or_restore_write(self):
        value=install.LinuxPlan.__new__(install.LinuxPlan)
        value.context=value.handle=None;value.claimed=False
        value.api=Mock();value.data=None;gate=Mock();value.gate=gate
        value.resume_gate=Mock(side_effect=AssertionError('No pause request was sent'))
        value.attributes=Mock(side_effect=AssertionError('No input settings were captured or changed'))
        self.assertTrue(value.restore())
        gate.close.assert_called_once();gate.sendall.assert_not_called();self.assertIsNone(value.gate)
        self.assertEqual(value.api.mock_calls,[]);value.resume_gate.assert_not_called()

    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'),'Original Lenovo BIN required')
    def test_passive_constructor_failure_publishes_retryable_result_without_begin_or_usb(self):
        import controller_firmware_worker as worker
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);versions={c:'250101A' for c in fw.RELEASES}
            (root/'plan.json').write_text(json.dumps({'components':['left'],'versions':versions}))
            (root/'left.bin').write_bytes((Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])/'left-v26.03.10.01.bin').read_bytes())
            saved=[]
            boundary=SimpleNamespace(**dict(vars(os),name='posix',geteuid=lambda:0))
            with patch.object(install,'os',boundary),patch.object(install,'_read_json',return_value={'components':['left']}), \
                    patch.object(install,'LinuxPlan',side_effect=fw.FirmwareError('Disconnected during passive observation')), \
                    patch.object(install,'_save',side_effect=lambda d,n,v:saved.append(v)),patch.object(worker,'run_update_plan') as run:
                self.assertEqual(install.run_staged(str(root)),2)
            run.assert_not_called();self.assertFalse((root/'recovery.json').exists())
            self.assertEqual(saved[-1]['phase'],'failed_before_write');self.assertTrue(saved[-1]['restored'])
            self.assertTrue(saved[-1]['cleanup_complete']);self.assertEqual(saved[-1]['cleanup_errors'],[])
            self.assertEqual(saved[-1]['versions'],versions);self.assertEqual(saved[-1]['completed'],[])

    def test_unclaimed_pre_handoff_failure_recovers_only_live_original_backend_without_usb_writes(self):
        import controller_maintenance as maintenance
        value=install.LinuxPlan.__new__(install.LinuxPlan)
        value.release_handle=lambda:None
        value.data={'input_stopped':False,'backend':{'pid':123,'start':'original'}}
        value.claim=None;value.input_stopped=False
        old_gate=Mock();value.gate=old_gate
        value.same=lambda:SimpleNamespace(driver='/sys/bus/usb/drivers/usbhid')
        value.service=lambda:{'ActiveState':'active'}
        value.resume_gate=Mock(return_value=True)
        value.attributes=Mock(side_effect=AssertionError('No preferences may be rewritten'))
        value.api=Mock()
        process=Mock();process.read_text.return_value='123 (backend) '+' '.join(['0']*19+['original'])
        marker=MagicMock();marker.exists.return_value=False;marker.__truediv__.return_value=marker
        def path(value):return process if str(value)=='/proc/123/stat' else marker
        with patch.object(install,'Path',side_effect=path), \
                patch.object(install.time,'monotonic',side_effect=[0.,21.]), \
                patch.object(maintenance,'_directory',side_effect=AssertionError('No claim exists')):
            self.assertTrue(value.restore())
        old_gate.close.assert_called_once();self.assertIsNone(value.gate)
        value.resume_gate.assert_called_once()
        value.attributes.assert_not_called();self.assertEqual(value.api.mock_calls,[])

        value.resume_gate.reset_mock();value.gate=None
        process.read_text.return_value='123 (backend) '+' '.join(['0']*19+['reused-pid'])
        with patch.object(install,'Path',side_effect=path):
            with self.assertRaises(fw.FirmwareError):value.restore()
        value.resume_gate.assert_not_called()

    def test_disabled_build_never_provisions_launches_or_opens_usb(self):
        with patch.object(install,'OTA_ENABLED',False):
            with patch.object(stage,'provision_root',side_effect=AssertionError('provision')):
                with self.assertRaises(fw.FirmwareError):install.IndependentInstaller(REPO)((),{},lambda *a:None)
            with patch.object(install,'LinuxPlan',side_effect=AssertionError('USB')):
                with self.assertRaises(fw.FirmwareError):install.run_staged(str(REPO))

    def test_private_plan_only_ordered_actual_components_and_all_versions(self):
        versions = {c:r.version for c,r in fw.RELEASES.items()}
        raw = json.dumps({'components':['left','right'],'versions':versions}).encode()
        self.assertEqual(install.validate_plan(raw,['left','right'])['versions'],versions)
        for value in ({'components':['left'],'versions':{'left':'260310A'}},
                {'components':['left'],'versions':versions,'path':'elsewhere'},
                {'components':['right'],'versions':versions}):
            with self.assertRaises(fw.FirmwareError):install.validate_plan(json.dumps(value).encode(),['left'])
        with self.assertRaises(fw.FirmwareError):install.validate_plan(b'{"components":[],"components":["left"],"versions":{}}',['left'])

    def test_binding_survives_expected_reenumeration_not_device_port_or_boot_change(self):
        descriptor = bytes.fromhex('1201000200000040ef17eb61000101020301')
        before = UsbObservation('boot','/sys/devices/usb3/3-1',(1,2),3,2,(1,),descriptor,
            (('3-1:1.0','xpad'),),None,None)
        after = replace(before,address=4,instance=(1,9),device_descriptor=descriptor[:12]+b'\2\0'+descriptor[14:])
        self.assertEqual(install.stable_binding(before),install.stable_binding(after))
        for changed in (replace(before,boot_id='other'),replace(before,ports=(2,)),
                replace(before,syspath='/sys/devices/usb3/3-2'),replace(before,other_bindings=())):
            self.assertNotEqual(install.stable_binding(before),install.stable_binding(changed))

    def test_shared_service_cleanup_is_independent_and_never_restarts_worker(self):
        value = process.service_plan('a'*32,675,'b'*64,cleanup=True)
        cleanup = next(x for x in value.argv if x.startswith('--property=ExecStopPost='))
        self.assertIn('/usr/bin/python3 -I -B ',cleanup)
        self.assertTrue(cleanup.endswith(' --cleanup'))
        self.assertNotIn('plugin_loader',cleanup)
        self.assertIn('--property=Restart=no',value.argv)

    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'),'Original Lenovo BINs required')
    def test_all_original_images_and_sources_frozen_without_producer_or_paths(self):
        root = Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])
        names = {'left':'left-v26.03.10.01.bin','right':'right-v26.03.10.01.bin','receiver':'rx-v26.04.22.01.bin'}
        images = tuple(fw.validate_image(c,(root/name).read_bytes()) for c,name in names.items())
        bundle = install.freeze_plan(images,{'versions':{c:'250101A' for c in names}},REPO)
        manifest = stage.validate_bundle(bundle)
        self.assertEqual(manifest['schema'],2)
        self.assertEqual(manifest['components'],list(names))
        self.assertEqual(set(bundle.files),stage.PLAN_SOURCES|{'plan.json','bootstrap.py','left.bin','right.bin','receiver.bin'})
        self.assertEqual(bundle.files['bootstrap.py'],stage.BOOTSTRAP.encode('ascii'))
        with self.assertRaises(TypeError):bundle.files['left.bin']=b'changed'
        if os.name=='posix':
            with tempfile.TemporaryDirectory() as temporary:
                folder=Path(temporary);folder.chmod(0o700)
                job=stage._publish(bundle,str(folder),os.geteuid())
                private=Path(job.directory)
                for name in ('status.tmp','recovery.tmp'):
                    (private/name).touch(mode=0o600)
                namespace={'__name__':'_test'};exec(stage.BOOTSTRAP,namespace)
                self.assertEqual(namespace['verify'](job.directory,job.manifest_sha256,os.geteuid())['schema'],2)


class ConsumerDiscoveryTests(unittest.TestCase):
    def fixture(self, root):
        class FixturePath(type(Path())):
            # Preserve real globbing on Windows, which forbids sysfs colons.
            def __truediv__(self, value):
                return super().__truediv__(str(value).replace(':','_') if os.name=='nt' else value)
            def glob(self, pattern):
                return super().glob(pattern.replace(':','_') if os.name=='nt' else pattern)
        base=FixturePath(root)/'devices'/'3-1';base.mkdir(parents=True)
        command=base/'3-1:1.2'/'0003:17EF:61EB.0009'
        for name in ('hidraw/hidraw8','input/input20/event10'):
            (command/name).mkdir(parents=True)
        proc=root/'proc';(proc/'123'/'fd').mkdir(parents=True)
        (proc/'123'/'fd'/'0').touch()
        observation=SimpleNamespace(bus=3,address=4,syspath=str(base))
        def mapped(value):return FixturePath(proc if str(value)=='/proc' else value)
        return observation,mapped

    def test_current_command_nodes_and_raw_usb_foreign_readers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);observation,mapped=self.fixture(root)
            value=install.LinuxPlan.__new__(install.LinuxPlan)
            value.data={'input':{'sources':['/dev/hidraw1','/dev/input/event6']}}
            for target in ('/dev/hidraw8','/dev/input/event10','/dev/bus/usb/003/004'):
                with self.subTest(target=target),patch.object(install,'Path',side_effect=mapped), \
                        patch.object(install.os,'readlink',return_value=target):
                    with self.assertRaises(fw.FirmwareError):value.consumers(observation)

    def test_reused_old_indices_and_other_interfaces_are_not_command_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);observation,mapped=self.fixture(root)
            (mapped(observation.syspath)/'3-1:1.0'/'input'/'input21'/'event11').mkdir(parents=True)
            value=install.LinuxPlan.__new__(install.LinuxPlan)
            value.data={'input':{'sources':['/dev/hidraw1','/dev/input/event6']}}
            for target in ('/dev/hidraw1','/dev/input/event6','/dev/input/event11'):
                with self.subTest(target=target),patch.object(install,'Path',side_effect=mapped), \
                        patch.object(install.os,'readlink',return_value=target):
                    value.consumers(observation)

    def test_cleanup_attempts_restore_with_vanished_or_reused_baseline_event(self):
        for reused in (False,True):
            with self.subTest(reused=reused),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);observation,mapped=self.fixture(root)
                old=root/'old-class-input'/'event6'/'device'
                if reused:old.mkdir(parents=True)
                versions={c:'250101A' for c in fw.RELEASES}
                data={'binding':'a'*64,'input_stopped':True,'claim_identity':[1,2],'claim_token':'old',
                    'input':{'sources':['/dev/hidraw1','/dev/input/event6']}}
                (root/'recovery.json').write_text(json.dumps(data))
                (root/'plan.json').write_text(json.dumps({'components':['left'],'versions':versions}))
                calls=[];saved=[]
                original_consumers=install.LinuxPlan.consumers
                class Plan:
                    consumers=original_consumers
                    def __init__(self,*a):pass
                    def same(self):return observation
                    def restore(self):calls.append('restore');return True
                def mapped_all(value):
                    return root/'old-class-input' if str(value)=='/sys/class/input' else mapped(value)
                with patch.object(install,'Path',side_effect=mapped_all),patch.object(install,'LinuxPlan',Plan), \
                        patch.object(install,'_read_json',side_effect=lambda p,*a:json.loads(Path(p).read_text())), \
                        patch.object(install,'_save',side_effect=lambda d,n,v:saved.append(v)), \
                        patch.object(install.os,'readlink',return_value='/dev/input/event6'):
                    self.assertEqual(install.cleanup_staged(str(root)),0)
                self.assertEqual(calls,['restore'])
                self.assertTrue(saved[-1]['restored']);self.assertEqual(saved[-1]['phase'],'uncertain')
                self.assertEqual(saved[-1]['completed'],[])


if __name__ == '__main__':unittest.main()
