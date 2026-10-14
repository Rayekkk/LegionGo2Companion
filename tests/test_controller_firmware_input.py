# SPDX-License-Identifier: BSD-3-Clause
import copy
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import controller_firmware_input as ip
from controller_firmware import FirmwareError


def snapshot(node='/dev/hidraw1'):
    return {'owner':':1.6','path':ip.ROOT+'/CompositeDevice0','command_node':node,
            'sources':[node,'/dev/input/event6'],'targets':['deck-uhid','keyboard','mouse'],
            'dbus':['dbus'],'profile':'name: Test\n','profile_name':'Test','intercept':0,
            'force_feedback':True,'persistent_id_hash':'a'*64,
            'source_roles':{'hidraw://'+Path(node).name:'qualified-command-hid','evdev://event6':'gamepad-role'},
            'filterable':{'hidraw://'+Path(node).name:['Gyroscope:Left','Accelerometer:Right']},
            'filters':{'hidraw://'+Path(node).name:['Gyroscope:Left','Accelerometer:Right']}}


def objects(targets=('deck-uhid','keyboard','mouse')):
    def prop(kind,value): return {'type':kind,'data':value}
    state=snapshot();path=state['path']
    result={path:{ip.COMPOSITE:{
        'Name':prop('s','Lenovo Legion Go 2'), 'PersistentId':prop('s','test'),
        'SourceDevicePaths':prop('as',state['sources']),
        'TargetDevices':prop('as',[ip.ROOT+'/devices/target/'+str(i) for i in range(len(targets))]),
        'DbusDevices':prop('as',[ip.ROOT+'/devices/target/dbus0']),
        'FilteredEvents':prop('a{sas}',state['filters']),
        'FilterableEvents':prop('a{sas}',state['filterable']),
        'ProfileName':prop('s',state['profile_name']),'InterceptMode':prop('u',0)},
        'org.shadowblip.Output.ForceFeedback':{'Enabled':prop('b',True)}},
        ip.ROOT+'/devices/target/dbus0':{ip.TARGET:{'DeviceType':prop('s','dbus')}}}
    for i,kind in enumerate(targets):
        result[ip.ROOT+'/devices/target/'+str(i)]={ip.TARGET:{'DeviceType':prop('s',kind)}}
    return result


class InputRestoreTests(unittest.TestCase):
    def test_actual_snapshot_allows_transitional_targets_only_during_restore(self):
        for targets in (('xbox360',),(),('deck-uhid','mouse')):
            managed=objects(targets)
            def bus(args):
                return ({'data':[managed]} if 'GetManagedObjects' in args
                        else {'type':'s','data':[snapshot()['profile']]})
            with self.subTest(targets=targets),patch.object(ip,'_owner',return_value=':1.6'), \
                    patch.object(ip,'command_node',return_value='/dev/hidraw1'),patch.object(ip,'_bus',side_effect=bus), \
                    patch.object(ip,'_source_role',side_effect=lambda node,obs:snapshot()['source_roles'][ip._source_key(node)]):
                actual=ip.snapshot(object(),restoring=True)
                self.assertEqual(actual['targets'],sorted(targets))
                if targets==('xbox360',) or not targets:
                    with self.assertRaises(FirmwareError):ip.snapshot(object())

    def test_empty_filter_entries_are_equivalent_to_omitted_entries(self):
        before=snapshot();after=copy.deepcopy(before)
        before['filters']['evdev://event6']=[]
        self.assertTrue(ip.equivalent(before,after))

    def test_multiple_composites_are_not_stopped_by_this_scoped_handoff(self):
        managed=objects();managed[ip.ROOT+'/CompositeDevice1']=copy.deepcopy(managed[snapshot()['path']])
        with patch.object(ip,'_owner',return_value=':1.6'),patch.object(ip,'_bus',return_value={'data':[managed]}):
            with self.assertRaises(FirmwareError):ip.snapshot(object())

    def test_saved_filter_cannot_be_written_to_a_source_without_that_capability(self):
        fresh=snapshot();fresh['filterable']={}
        with patch.object(ip,'snapshot',return_value=fresh),patch.object(ip,'_owner',return_value=':1.6'), \
                patch.object(ip,'_bus') as bus:
            with self.assertRaises(FirmwareError):ip.restore(snapshot(),object(),deadline=ip.time.monotonic()+10)
        self.assertFalse(any('FilteredEvents' in c.args[0] for c in bus.call_args_list))

    def test_readback_after_deadline_or_owner_change_cannot_succeed(self):
        for late in ('deadline','owner'):
            # Require a real restoration change. If everything already matches,
            # a valid implementation may skip the idempotent setter entirely.
            state=snapshot();observed=copy.deepcopy(state);observed['force_feedback']=False
            clock=[0.];owner=[':1.6'];writes_finished=[False];fault_fired=[False]
            def bus(args):
                if 'ForceFeedback' in ' '.join(args):
                    observed['force_feedback']=args[-1]=='true'
                    writes_finished[0]=True
            def read(*args,**kwargs):
                # Fail a settled readback after restoration writes, regardless
                # of how many harmless snapshots an implementation took first.
                if writes_finished[0]:
                    fault_fired[0]=True
                    if late=='deadline':clock[0]=11.
                    else:owner[0]=':1.8'
                return copy.deepcopy(observed)
            with self.subTest(late=late),patch.object(ip,'snapshot',side_effect=read), \
                    patch.object(ip,'_owner',side_effect=lambda:owner[0]),patch.object(ip,'_bus',side_effect=bus), \
                    patch.object(ip.time,'monotonic',side_effect=lambda:clock[0]),patch.object(ip.time,'sleep'):
                with self.assertRaises(FirmwareError):ip.restore(state,object(),deadline=10.)
                self.assertTrue(fault_fired[0], 'The intended settled-readback fault must have fired.')

    def test_blocking_owner_query_past_deadline_never_receives_a_write(self):
        clock=[0.]
        def owner():clock[0]=11.;return ':1.6'
        with patch.object(ip,'snapshot',return_value=snapshot()),patch.object(ip,'_owner',side_effect=owner), \
                patch.object(ip.time,'monotonic',side_effect=lambda:clock[0]),patch.object(ip,'_bus') as bus:
            with self.assertRaises(FirmwareError):ip.restore(snapshot(),object(),deadline=10.)
        bus.assert_not_called()

    def test_renumbering_only_does_not_change_the_input_contract(self):
        before,after=snapshot(),snapshot('/dev/hidraw7')
        after.update(owner=':1.45',path=ip.ROOT+'/CompositeDevice3')
        after['filters']['hidraw://hidraw7'].reverse()
        self.assertTrue(ip.equivalent(before,after))

    def test_evdev_renumbering_matches_actual_role_and_preserves_filter(self):
        before=snapshot();before['filters']['evdev://event6']=['Gamepad:Button:South']
        after=copy.deepcopy(before);after['sources']=['/dev/hidraw1','/dev/input/event21']
        after['source_roles']['evdev://event21']=after['source_roles'].pop('evdev://event6')
        after['filters']['evdev://event21']=after['filters'].pop('evdev://event6')
        self.assertTrue(ip.equivalent(before,after))
        after['source_roles']['evdev://event21']='different-physical-role'
        self.assertFalse(ip.equivalent(before,after))

    def test_service_active_or_same_name_cannot_hide_actual_configuration_loss(self):
        before=snapshot()
        changes={'sources':['/dev/hidraw1'],'targets':['xbox360'],'dbus':[],
                 'profile':'name: Default\n','profile_name':'Default','intercept':1,
                 'force_feedback':False,'persistent_id_hash':'b'*64,'filters':{}}
        for key,value in changes.items():
            after=copy.deepcopy(before);after[key]=value
            with self.subTest(field=key): self.assertFalse(ip.equivalent(before,after))

    def test_restoration_uses_original_yaml_filters_output_and_actual_target_types(self):
        before=snapshot();fresh=snapshot('/dev/hidraw9')
        incomplete=copy.deepcopy(fresh);incomplete['targets']=['xbox360']
        with patch.object(ip,'snapshot',side_effect=[incomplete,incomplete,fresh,fresh]), \
                patch.object(ip,'_owner',return_value=':1.6'), \
                patch.object(ip,'_bus') as bus, patch.object(ip.time,'sleep'):
            result=ip.restore(before,object(),deadline=ip.time.monotonic()+10)
        self.assertTrue(ip.equivalent(before,result))
        calls=[c.args[0] for c in bus.call_args_list]
        self.assertIn(['call',':1.6',before['path'],ip.COMPOSITE,'LoadProfileFromYaml','s',before['profile']],calls)
        self.assertTrue(any('SetTargetDevices' in c and c[-3:]==before['targets'] for c in calls))
        filtered=next(c for c in calls if 'FilteredEvents' in c)
        self.assertIn('hidraw://hidraw9',filtered)
        self.assertNotIn('hidraw://hidraw1',filtered)

    def test_changed_owner_or_device_identity_never_receives_restore_writes(self):
        for changed in ('owner','device'):
            before=snapshot();fresh=snapshot()
            if changed=='device':fresh['persistent_id_hash']='b'*64
            with patch.object(ip,'snapshot',return_value=fresh), \
                    patch.object(ip,'_owner',return_value=':1.99' if changed=='owner' else fresh['owner']), \
                    patch.object(ip,'_bus') as bus:
                with self.assertRaises(FirmwareError):ip.restore(before,object(),deadline=ip.time.monotonic()+10)
                bus.assert_not_called()

    def test_expired_deadline_never_loads_profile_or_changes_input(self):
        with patch.object(ip,'snapshot',return_value=snapshot()),patch.object(ip,'_bus') as bus:
            with self.assertRaises(FirmwareError):ip.restore(snapshot(),object(),deadline=ip.time.monotonic()-1)
            bus.assert_not_called()

    @unittest.skipUnless(os.name=='posix','Physical HID sysfs names require POSIX')
    def test_command_hid_node_resolves_within_the_observed_usb_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)/'3-1'
            node=base/'3-1:1.2'/'0003:17EF:61EB.0007'/'hidraw'/'hidraw9'
            node.mkdir(parents=True)
            self.assertEqual(ip.command_node(SimpleNamespace(syspath=str(base))),'/dev/hidraw9')
            (node.parent/'hidraw10').mkdir()
            with self.assertRaises(FirmwareError):ip.command_node(SimpleNamespace(syspath=str(base)))
