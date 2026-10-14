# SPDX-License-Identifier: BSD-3-Clause
"""Private POSIX publication and stdlib-only verification before worker import.

No controller is opened. The trusted producer supplies the manifest digest to
the service, independently of the staged manifest. This is integrity checking,
not a signature on arbitrary Python or protection against a hostile root.
Jobs are retained for inspection; no automatic deletion of active/unknown jobs.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import uuid

from controller_firmware import FirmwareError, validate_image, release_from_record

RUNTIME_ROOT = '/var/lib/legiongo2companion/firmware-jobs'
SOURCES = frozenset(('controller_firmware_worker.py', 'controller_firmware_transfer.py',
    'controller_firmware_journal.py', 'controller_firmware_protocol.py',
    'controller_firmware.py', 'controller_firmware_catalog.py', 'system_process.py',
    'controller_firmware_process.py', 'controller_firmware_stage.py',
    'controller_firmware_sleep.py', 'diagnostics.py'))
PLAN_SOURCES = SOURCES | frozenset(('controller_firmware_install.py', 'controller_firmware_ota.py',
    'controller_firmware_usb.py', 'controller_firmware_usb_linux.py',
    'controller_firmware_input.py', 'controller_maintenance.py'))
MAX_SOURCE = 1024 * 1024
MAX_MANIFEST = 16 * 1024

# Only stdlib is imported before every staged source has been checked. Python
# -I excludes the plugin/user site and -B avoids changing the staged payload.
# Extra mutable launch/journal files are not importable source directories.
BOOTSTRAP = r'''
import hashlib,json,os,re,stat,sys
def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result
def verify(directory,expected,owner=0,cleanup=False):
    if os.name!='posix' or not re.fullmatch('[0-9a-f]{64}',expected): raise ValueError('platform/digest')
    if not directory.startswith('/') or '..' in directory.split('/') or '//' in directory: raise ValueError('path')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    fd=os.open('/',flags)
    try:
        for part in directory.split('/')[1:]:
            child=os.open(part,flags,dir_fd=fd);os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_uid not in (0,owner) or (info.st_mode&0o022 and not (info.st_uid==0 and info.st_mode&stat.S_ISVTX)): raise ValueError('parent')
        info=os.fstat(fd)
        if info.st_uid!=owner or stat.S_IMODE(info.st_mode)!=0o700: raise ValueError('directory')
        def read(name,maximum,minimum=1):
            file=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            try:
                info=os.fstat(file)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or not minimum<=info.st_size<=maximum: raise ValueError('file')
                chunks=[];size=0
                while size<=maximum:
                    chunk=os.read(file,min(65536,maximum+1-size))
                    if not chunk: break
                    chunks.append(chunk);size+=len(chunk)
                data=b''.join(chunks)
                if len(data)!=info.st_size or len(data)>maximum: raise ValueError('size')
                return data
            finally: os.close(file)
        raw=read('manifest.json',16384)
        if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('manifest digest')
        manifest=json.loads(raw,object_pairs_hook=unique)
        if type(manifest.get('schema')) is not int or manifest['schema'] not in (1,2): raise ValueError('schema')
        plan=manifest['schema']==2
        if set(manifest)!=({'schema','job_id','components','files'} if plan else {'schema','job_id','component','image_sha256','files'}): raise ValueError('schema')
        if not re.fullmatch('[0-9a-f]{32}',manifest['job_id']) or directory.rsplit('/',1)[-1]!=manifest['job_id']: raise ValueError('job')
        if not plan and (manifest['component'] not in ('left','right','receiver') or not re.fullmatch('[0-9a-f]{64}',manifest['image_sha256'])): raise ValueError('image')
        required={'controller_firmware_worker.py','controller_firmware_transfer.py','controller_firmware_journal.py','controller_firmware_protocol.py','controller_firmware.py','controller_firmware_catalog.py','system_process.py','controller_firmware_process.py','controller_firmware_stage.py','controller_firmware_sleep.py','diagnostics.py','image.bin'}
        if plan:
            components=manifest['components']
            if not isinstance(components,list) or not components or components!=[c for c in ('left','right','receiver') if c in components]: raise ValueError('components')
            required.remove('image.bin')
            required|={'controller_firmware_install.py','controller_firmware_ota.py','controller_firmware_usb.py','controller_firmware_usb_linux.py','controller_firmware_input.py','controller_maintenance.py','plan.json','bootstrap.py'}|{c+'.bin' for c in components}
        if set(manifest['files'])!=required: raise ValueError('source set')
        # In particular, forbid extra importable modules and __pycache__ which
        # could otherwise override checked source/stdlib at the next import.
        extras=set(os.listdir(fd))-required-{'manifest.json'}
        if not extras<={'launch.claim','worker.lock','journal.json','status.json','recovery.json','status.tmp','recovery.tmp','left','right','receiver'}: raise ValueError('unexpected entry')
        for name in tuple(extras&{'left','right','receiver'}):
            item=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if not plan or not stat.S_ISDIR(item.st_mode) or item.st_uid!=owner or stat.S_IMODE(item.st_mode)!=0o700: raise ValueError('journal directory')
            extras.remove(name)
        for name in extras:
            if cleanup and name in ('status.json','journal.json','status.tmp','recovery.tmp','worker.lock','launch.claim'):
                continue  # optional output must not gate trusted input recovery
            read(name,131072,0 if name in ('worker.lock','status.tmp','recovery.tmp') else 1)
        for name,record in manifest['files'].items():
            if set(record)!={'bytes','sha256'} or type(record['bytes']) is not int or not 1<=record['bytes']<=1048576 or not re.fullmatch('[0-9a-f]{64}',record['sha256']): raise ValueError('record')
            data=read(name,1048576)
            if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']: raise ValueError('source digest')
        if not plan and manifest['files']['image.bin']['sha256']!=manifest['image_sha256']: raise ValueError('image digest')
        return manifest
    finally: os.close(fd)
if __name__=='__main__':
    try:
        verify(sys.argv[1],sys.argv[2],cleanup=len(sys.argv)==4 and sys.argv[3]=='--cleanup')
    except BaseException:
        print('{"flash_available":false,"error":"Worker staging verification failed. No device was opened."}')
        print('{"level":"error","component":"firmware","event":"staging_verification_failed"}',file=sys.stderr)
        raise SystemExit(2)
    sys.path.insert(0,sys.argv[1])
    from diagnostics import configure_worker,event,operation
    configure_worker('firmware')
    cleanup=len(sys.argv)==4 and sys.argv[3]=='--cleanup'
    with operation(source='firmware_worker',name='recovery' if cleanup else 'update'):
        try:
            if cleanup:
                from controller_firmware_install import cleanup_staged
                result=cleanup_staged(sys.argv[1])
            else:
                from controller_firmware_worker import main
                result=main()
        except BaseException as exc:
            event('firmware','worker_entry_failed',level='error',error=exc,worker_id=sys.argv[1].rsplit('/',1)[-1],stage='cleanup_entry' if cleanup else 'worker_entry')
            raise SystemExit(1)
    raise SystemExit(result)
'''


@dataclass(frozen=True)
class StagedJob:
    job_id: str
    directory: str
    manifest_sha256: str


def validate_bundle(bundle):
    """Check producer capture before touching the filesystem."""
    try:
        if not re.fullmatch(r'[0-9a-f]{32}', bundle.job_id): raise ValueError('job')
        if not isinstance(bundle.manifest, bytes) or not 1 <= len(bundle.manifest) <= MAX_MANIFEST:
            raise ValueError('manifest')
        namespace = {'__name__': '_stage_verifier'}
        exec(BOOTSTRAP, namespace)  # Fixed source, never manifest-provided code.
        manifest = json.loads(bundle.manifest, object_pairs_hook=namespace['unique'])
        if manifest.get('schema') == 2:
            components = manifest.get('components')
            if (set(manifest) != {'schema','job_id','components','files'} or type(manifest['schema']) is not int or
                    manifest['job_id'] != bundle.job_id or not isinstance(components,list) or not components or
                    components != [c for c in ('left','right','receiver') if c in components] or
                    set(bundle.files) != PLAN_SOURCES | {'plan.json','bootstrap.py'} | {c+'.bin' for c in components} or
                    set(manifest['files']) != set(bundle.files)):
                raise ValueError('plan schema')
            from controller_firmware_install import validate_plan
            plan = validate_plan(bundle.files['plan.json'], components)
            for component in components:
                validate_image(component, bundle.files[component+'.bin'],
                    release=release_from_record(component,plan['offers'][component]))
            if bundle.files['bootstrap.py'] != BOOTSTRAP.encode('ascii'): raise ValueError('bootstrap')
        elif (set(manifest) != {'schema', 'job_id', 'component', 'image_sha256', 'files'} or
                type(manifest['schema']) is not int or manifest['schema'] != 1 or
                manifest['job_id'] != bundle.job_id or set(bundle.files) != SOURCES | {'image.bin'} or
                set(manifest['files']) != set(bundle.files)):
            raise ValueError('schema')
        for name, data in bundle.files.items():
            if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_SOURCE:
                raise ValueError('size')
            record = manifest['files'][name]
            if (set(record) != {'bytes', 'sha256'} or type(record['bytes']) is not int or
                    record['bytes'] != len(data) or record['sha256'] != hashlib.sha256(data).hexdigest()):
                raise ValueError('digest')
        if manifest['schema'] == 1:
            image = validate_image(manifest['component'], bundle.files['image.bin'])
            if manifest['image_sha256'] != image.release.sha256: raise ValueError('image')
        return manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
        raise FirmwareError('The captured firmware worker bundle is invalid.') from exc


def _private_directory(path, owner):
    if os.name != 'posix' or not path.startswith('/') or '..' in path.split('/') or '//' in path:
        raise FirmwareError('Firmware staging requires an absolute protected POSIX path.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.split('/')[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            if (info.st_uid not in (0, owner) or
                    (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise FirmwareError('The firmware staging parent is not trusted.')
        info = os.fstat(fd)
        if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
            raise FirmwareError('The firmware staging directory is not private.')
        result, fd = fd, None
        return result
    finally:
        if fd is not None: os.close(fd)


def _publish(bundle, root, owner):
    """Internal testable implementation; production root/owner are fixed below."""
    validate_bundle(bundle)
    parent = _private_directory(root, owner)
    temporary = '.stage-' + uuid.uuid4().hex
    directory = None
    created = []
    directory_created = False
    published = False
    try:
        # Serialise publishers; never overwrite a job or recreate its lock.
        import fcntl
        lock = os.open('publish.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent)
        try:
            info = os.fstat(lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise FirmwareError('Firmware publication lock is not private.')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: os.stat(bundle.job_id, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise FirmwareError('A firmware job with this identity already exists.')
            os.mkdir(temporary, mode=0o700, dir_fd=parent)
            directory_created = True
            directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            for name, data in dict(bundle.files, **{'manifest.json': bundle.manifest}).items():
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=directory)
                created.append(name)
                try:
                    offset = 0
                    while offset < len(data):
                        count = os.write(fd, data[offset:])
                        if count <= 0: raise OSError('short staging write')
                        offset += count
                    os.fsync(fd)
                finally: os.close(fd)
            os.fsync(directory)
            os.rename(temporary, bundle.job_id, src_dir_fd=parent, dst_dir_fd=parent)
            published = True
            os.fsync(parent)
            return StagedJob(bundle.job_id, root + '/' + bundle.job_id,
                hashlib.sha256(bundle.manifest).hexdigest())
        finally: os.close(lock)
    except OSError as exc:
        raise FirmwareError('The firmware worker could not be privately staged.') from exc
    finally:
        cleanup_failed = False
        try:
            if directory is not None:
                try:
                    if not published:
                        for name in created:
                            try: os.unlink(name, dir_fd=directory)
                            except FileNotFoundError: pass
                            except OSError: cleanup_failed = True
                finally: os.close(directory)
            if directory_created and not published:
                try: os.rmdir(temporary, dir_fd=parent)
                except FileNotFoundError: pass
                except OSError: cleanup_failed = True
        finally: os.close(parent)
        if cleanup_failed:
            raise FirmwareError('Firmware staging cleanup failed; private remnants were retained.')


def stage_bundle(bundle):
    # Installer must provision the trusted root. Do not chmod/chown arbitrary
    # existing paths or place jobs in a user/plugin-controlled directory.
    if os.name != 'posix' or os.geteuid() != 0:
        raise FirmwareError('Firmware worker publication requires the system service owner.')
    return _publish(bundle, RUNTIME_ROOT, 0)


def provision_root():
    """Create only our two fixed directories beneath trusted /var/lib.

    Dormant; not run at plugin startup. Refuse incorrect existing ownership or
    permissions rather than modifying someone else's files. No recursive tools.
    """
    if os.name != 'posix' or os.geteuid() != 0:
        raise FirmwareError('Firmware staging provisioning requires the system owner.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in ('var','lib','legiongo2companion','firmware-jobs'):
            if part in ('legiongo2companion','firmware-jobs'):
                try: os.mkdir(part,mode=0o700,dir_fd=fd)
                except FileExistsError: pass
                os.fsync(fd)
            child = os.open(part,flags,dir_fd=fd)
            os.close(fd);fd=child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise FirmwareError('The firmware staging parent is not root-owned and protected.')
            if part in ('legiongo2companion','firmware-jobs') and stat.S_IMODE(info.st_mode) != 0o700:
                raise FirmwareError('The firmware staging root is not private.')
        os.fsync(fd)
    except OSError as exc:
        raise FirmwareError('The firmware staging root could not be provisioned.') from exc
    finally: os.close(fd)


def verify_stage(job):
    if not isinstance(job, StagedJob) or job.directory != RUNTIME_ROOT + '/' + job.job_id:
        raise FirmwareError('Invalid firmware staging identity.')
    namespace = {'__name__': '_stage_verifier'}
    exec(BOOTSTRAP, namespace)
    try:
        return namespace['verify'](job.directory, job.manifest_sha256)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
        raise FirmwareError('Firmware worker staging verification failed.') from exc
