# SPDX-License-Identifier: BSD-3-Clause
"""Exact InputPlumber configuration snapshots for the trusted updater worker.

No startup/CLI hook. Source paths may be renumbered; match the command HID by
its verified physical interface, and preserve the other sources and actual
target types. A service being active alone is never a restoration proof.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from controller_firmware import FirmwareError
from system_process import system_env

SERVICE = 'org.shadowblip.InputPlumber'
ROOT = '/org/shadowblip/InputPlumber'
COMPOSITE = 'org.shadowblip.Input.CompositeDevice'
TARGET = 'org.shadowblip.Input.Target'


def _bus(args):
    result = subprocess.run(['/usr/bin/busctl', '--system', '--json=short', '--timeout=3']+args,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=4, env=system_env(LC_ALL='C'))
    if result.returncode or len(result.stdout)>262144:
        raise FirmwareError('InputPlumber configuration could not be read or restored.')
    try: return json.loads(result.stdout) if result.stdout.strip() else None
    except (ValueError, UnicodeError): raise FirmwareError('InputPlumber returned invalid configuration.') from None


def _owner():
    value = _bus(['call','org.freedesktop.DBus','/org/freedesktop/DBus',
                  'org.freedesktop.DBus','GetNameOwner','s',SERVICE])['data'][0]
    if not isinstance(value,str) or not re.fullmatch(r':[0-9]+\.[0-9]+',value):
        raise FirmwareError('InputPlumber owner is invalid.')
    return value


def _value(properties, name, kind):
    prop = properties.get(name)
    if not isinstance(prop,dict) or prop.get('type')!=kind or 'data' not in prop:
        raise FirmwareError('InputPlumber properties do not match the reviewed contract.')
    return prop['data']


def command_node(observation):
    interface = Path(observation.syspath)/(Path(observation.syspath).name+':1.2')
    nodes = list(interface.glob('0003:17EF:61EB.*/hidraw/hidraw*'))
    if len(nodes)!=1 or not re.fullmatch('hidraw[0-9]+',nodes[0].name):
        raise FirmwareError('The restored command HID node is not unique.')
    return '/dev/'+nodes[0].name


def _source_key(node):
    if re.fullmatch(r'/dev/hidraw[0-9]+',node): return 'hidraw://'+Path(node).name
    if re.fullmatch(r'/dev/input/event[0-9]+',node): return 'evdev://'+Path(node).name
    raise FirmwareError('An input source is outside the qualified physical contract.')


def _source_role(node, observation):
    """Match physical input roles across a HID rebind, without event indices."""
    if node==command_node(observation):return 'qualified-command-hid'
    if not re.fullmatch(r'/dev/input/event[0-9]+',node):
        raise FirmwareError('An unexpected HID source cannot be restored.')
    device=(Path('/sys/class/input')/Path(node).name/'device').resolve(strict=True)
    base=Path(observation.syspath).resolve(strict=True)
    try: relative=device.relative_to(base)
    except ValueError:raise FirmwareError('The input source belongs to another physical device.') from None
    # Kernel instance IDs and input/event indices change on rebind. The USB
    # port/interface, name, phys, IDs and capabilities identify the actual role.
    parts=[part for part in relative.parts
           if not re.fullmatch(r'0003:17EF:61EB\.[0-9A-Fa-f]+|input[0-9]+',part)]
    fields={'physical':parts}
    for name in ('name','phys','id/bustype','id/vendor','id/product','id/version','properties',
                 'capabilities/ev','capabilities/key','capabilities/rel','capabilities/abs',
                 'capabilities/msc','capabilities/led','capabilities/ff','capabilities/sw'):
        with (device/name).open('rb') as stream: value=stream.read(4097)
        if len(value)>4096:raise FirmwareError('Input source identity exceeds its bound.')
        fields[name]=value.decode('ascii').strip()
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()


def _source_map(before,after):
    old,new=before['source_roles'],after['source_roles']
    if len(set(old.values()))!=len(old) or len(set(new.values()))!=len(new) or set(old.values())!=set(new.values()):
        raise FirmwareError('The restored physical input sources do not match their baseline.')
    inverse={role:key for key,role in new.items()}
    return {key:inverse[role] for key,role in old.items()}


def snapshot(observation, *, restoring=False):
    owner = _owner()
    objects = _bus(['call',owner,ROOT,'org.freedesktop.DBus.ObjectManager','GetManagedObjects'])['data'][0]
    if not isinstance(objects,dict) or len(objects)>512:
        raise FirmwareError('InputPlumber topology exceeds its bound.')
    if sum(COMPOSITE in interfaces for interfaces in objects.values()) != 1:
        raise FirmwareError('This handoff requires a single input composite device.')
    node = command_node(observation)
    candidates = [(path,interfaces[COMPOSITE]) for path,interfaces in objects.items()
                  if COMPOSITE in interfaces and node in _value(interfaces[COMPOSITE],'SourceDevicePaths','as')]
    if len(candidates)!=1:
        raise FirmwareError('A unique controller input device was not observed.')
    path, props = candidates[0]
    if not re.fullmatch(ROOT+r'/CompositeDevice[0-9]+',path) or _value(props,'Name','s')!='Lenovo Legion Go 2':
        raise FirmwareError('The input device is not the qualified Legion Go 2.')
    sources = _value(props,'SourceDevicePaths','as')
    targets = _value(props,'TargetDevices','as')
    dbus = _value(props,'DbusDevices','as')
    if (not isinstance(sources,list) or not 1<=len(sources)<=16
            or not all(isinstance(x,list) and (0 if restoring else 1)<=len(x)<=16 for x in (targets,dbus))):
        raise FirmwareError('Input device sources/targets are incomplete.')
    def target_type(target):
        props = objects.get(target,{}).get(TARGET,{})
        if restoring and not props: return '<pending>'
        kind = _value(props,'DeviceType','s')
        if not isinstance(kind,str) or not re.fullmatch(r'[a-z0-9-]{1,64}',kind):
            raise FirmwareError('Input target type is invalid.')
        return kind
    types = [target_type(target) for target in targets]
    dbus_types = [target_type(target) for target in dbus]
    if not restoring and (len(set(types))!=len(types)
            or any(kind not in ('deck-uhid','mouse','keyboard') for kind in types) or dbus_types!=['dbus']):
        raise FirmwareError('The input targets are outside the qualified contract.')
    filters = _value(props,'FilteredEvents','a{sas}')
    filterable = _value(props,'FilterableEvents','a{sas}')
    if any(not isinstance(value,dict) or len(value)>16 for value in (filters,filterable)):
        raise FirmwareError('Input filter configuration is invalid.')
    roles={_source_key(value):_source_role(value,observation) for value in sources}
    if len(roles)!=len(sources) or len(set(roles.values()))!=len(roles):
        raise FirmwareError('Physical input source roles are not unique.')
    allowed_sources=set(roles)
    for source, events in list(filters.items())+list(filterable.items()):
        if (source not in allowed_sources or not isinstance(events,list) or len(events)>256
                or any(not isinstance(event,str) or not 1<=len(event)<=128 for event in events)):
            raise FirmwareError('Input filter configuration cannot be restored safely.')
    if any(not set(events)<=set(filterable.get(source,[])) for source,events in filters.items()):
        raise FirmwareError('Input filters exceed the actual source capabilities.')
    yaml = _bus(['call',owner,path,COMPOSITE,'GetProfileYaml'])
    if yaml.get('type')!='s' or len(yaml.get('data',[]))!=1:
        raise FirmwareError('Input profile snapshot is invalid.')
    yaml = yaml['data'][0]
    if not isinstance(yaml,str) or not 1<=len(yaml.encode())<=65536:
        raise FirmwareError('Input profile snapshot exceeds its bound.')
    force = _value(objects[path]['org.shadowblip.Output.ForceFeedback'],'Enabled','b')
    intercept = _value(props,'InterceptMode','u')
    if type(force) is not bool or type(intercept) is not int or not 0<=intercept<=3:
        raise FirmwareError('Input intercept/output state is invalid.')
    if _owner()!=owner: raise FirmwareError('InputPlumber restarted while taking its snapshot.')
    return {'owner':owner,'path':path,'command_node':node,'sources':sorted(sources),
            'targets':sorted(types),'dbus':dbus_types,'filters':filters,'filterable':filterable,'profile':yaml,
            'source_roles':roles,
            'profile_name':_value(props,'ProfileName','s'),'intercept':intercept,'force_feedback':force,
            'persistent_id_hash':hashlib.sha256(_value(props,'PersistentId','s').encode()).hexdigest()}


def equivalent(before, after):
    """Ignore object/owner renumbering, never device, profile or event changes."""
    try: mapping=_source_map(before,after)
    except FirmwareError:return False
    filters = {mapping[source]:sorted(events)
               for source,events in before['filters'].items() if events}
    return (set(mapping.values())=={_source_key(node) for node in after['sources']}
            and filters=={k:sorted(v) for k,v in after['filters'].items() if v}
            and all(before[name]==after[name] for name in
                    ('targets','dbus','profile','profile_name','intercept','force_feedback','persistent_id_hash')))


def restore(before, observation, *, deadline):
    """One explicit restore, then bounded readback of the actual full state."""
    if time.monotonic()>=deadline:
        raise FirmwareError('Input restoration deadline was lost.')
    fresh = snapshot(observation, restoring=True)
    if fresh['persistent_id_hash']!=before['persistent_id_hash']:
        raise FirmwareError('A different input device appeared during restoration.')
    _source_map(before,fresh)
    owner, path = fresh['owner'], fresh['path']
    def current():
        if time.monotonic()>=deadline or _owner()!=owner or time.monotonic()>=deadline:
            raise FirmwareError('Input restoration deadline/owner was lost.')
    def readback():
        current()
        value=snapshot(observation, restoring=True)
        current()
        if value['owner']!=owner:
            raise FirmwareError('Input readback came from a different service owner.')
        return value
    current()
    _bus(['call',owner,path,COMPOSITE,'LoadProfileFromYaml','s',before['profile']])
    # Profile load can replace targets asynchronously. Wait for the profile
    # itself before the explicit target request; verify both again at the end.
    while time.monotonic()<deadline:
        fresh=readback()
        if fresh['profile']==before['profile']: break
        time.sleep(.1)
    else: raise FirmwareError('The input profile did not return before its deadline.')
    current()
    if fresh['targets']!=before['targets']:
        _bus(['call',owner,path,COMPOSITE,'SetTargetDevices','as',str(len(before['targets'])),*before['targets']])
    mapping=_source_map(before,fresh)
    filters={mapping[k]:v for k,v in before['filters'].items()}
    if any(not set(events)<=set(fresh['filterable'].get(source,[])) for source,events in filters.items()):
        raise FirmwareError('The restored sources cannot support the saved input filters.')
    values=[str(len(filters))]
    for source, events in sorted(filters.items()): values += [source,str(len(events)),*events]
    current(); _bus(['set-property',owner,path,COMPOSITE,'FilteredEvents','a{sas}',*values])
    current(); _bus(['set-property',owner,path,COMPOSITE,'InterceptMode','u',str(before['intercept'])])
    current(); _bus(['set-property',owner,path,'org.shadowblip.Output.ForceFeedback','Enabled','b',
                     'true' if before['force_feedback'] else 'false'])
    # Two separated observations establish settled configuration, not physical
    # button functionality. That final human test remains a separate claim.
    consecutive=0
    while time.monotonic()<deadline:
        fresh=readback()
        consecutive=consecutive+1 if equivalent(before,fresh) else 0
        if consecutive>=2:return fresh
        time.sleep(.1)
    raise FirmwareError('Actual input configuration was not fully restored.')
