# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Shared one-button controller-update workflow and image authentication.

The live model-specific Lenovo feed selects releases; the pinned vendor key and
signed component footer authenticate images, including new release versions.
Both frontends observe this one backend job. Original signed bytes are retained
in memory; decoded bytes are used only for structural checks.
"""
from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType
import urllib.request
import uuid


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    component: str
    label: str
    version: str
    size: int
    sha256: str
    footer: bytes
    filename: str

    @property
    def url(self):
        return 'https://s3.service.lenovo.com/legion/cms/package/' + self.filename


CATALOG_DATE = '2026-10-12'
RELEASES = MappingProxyType({
    'left': Release('left', 'Left controller', '260310A', 424320,
        '0822e989fc9df6cb4e3ec38d5a6b468403998f56dd37abbcecbe3d9c0e12879b',
        bytes.fromhex('05070000'), 'hCPsaM0h202605140316057562066.bin'),
    'right': Release('right', 'Right controller', '260310A', 444800,
        '664721020f1e6a7b38797c3877cbf457043f01c858d196481aeb34bf0afa0a1e',
        bytes.fromhex('06080000'), 'GA6eAdKL202605140317207996326.bin'),
    'receiver': Release('receiver', 'Receiver', '260422A', 440192,
        'e26d0f9f64b8f8685260fa548f34f682e82f5f1ce20ae5686ee43939e3895e07',
        bytes.fromhex('01020000'), 'zpJZHUrs202605140314438709061.bin'),
})
SIGNATURE_BYTES = 384
MIN_IMAGE_BYTES = SIGNATURE_BYTES + 192 + 4
MAX_IMAGE_BYTES = 1024 * 1024  # Existing bounded C0 stream/staging envelope.
PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAuWzi2SQCB/Em+5aW7OUmRWgSCvnfY0aYlRNwgalwMtMUlrdijdELq10mTOa5ita1PdM1Xsr3IZflsXw4aH5Xq812JfR/rT7LvegJEdw4N5EkXpcfaDOxBI/ScUdJLqcetRVNgKpn036WKyv/mW/Jnjo97f2rbGuJDBiMhzwdBG+wFR0PsM+VY2whGO66t+xPqJ4FRNaZGRqRpM1pimkSvn1Pewy4nR4hKxkzU1BuAM5QSsgI9dBoBAAih19ydvaMyIMRTUUqK631ATv7/4OdB7ks4E+xWaDRXF2jVk61l3U7HumE3h3WtRqRJoOjopp1v4CWrnudLMJYWMNOZQX/8t/ykO06p0TcbWnzvsOmCarFsHuvP33U4emPBwaeMOhF/EzGSVZQHXS6HotZ6UMSKA9/gz4T2orq8mNKbtb2YGh0+hRz28t3g6lkK5iM7scuvt/BLcaQo8yVs5Jwpa0uxzHpCcwnzV3rmiGmGSVcJgVfDJ3bKHOcEuuYGsAQy8nBAgMBAAE=
-----END PUBLIC KEY-----
'''
KEY_SHA256 = '93c6b74837c9d082c6914598baa877b0b4461f636a9be13661473d97571d520e'


def release_for(component):
    if not isinstance(component, str) or component not in RELEASES:
        raise FirmwareError('Select a supported controller component.')
    return RELEASES[component]


def version_number(value):
    """Supported YYMMDD + hex revision domain; unknown formats fail closed."""
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{6}[0-9a-fA-F]', value):
        raise FirmwareError('The installed controller version is not confirmed.')
    try:
        date(2000 + int(value[:2]), int(value[2:4]), int(value[4:6]))
    except ValueError as exc:
        raise FirmwareError('The controller version contains an invalid date.') from exc
    return int(value, 16)


def update_needed(installed, offered):
    return version_number(offered) > version_number(installed)


def valid_image_url(url):
    # The -I download entry point is stdlib-only: do not import a neighbouring
    # plugin module to validate its one fixed official-host URL domain.
    return isinstance(url, str) and re.fullmatch(
        r'https://s3\.service\.lenovo\.com/legion/cms/package/[A-Za-z0-9_-]{1,128}\.bin', url) is not None


def candidate_release(component, candidate):
    """Select only a model/component-specific official-feed URL and version.

    Size and digest are learned from authenticated bytes, never rounded feed
    packageSize. The signed footer remains a component/protocol binding.
    """
    from controller_firmware_catalog import raw_version
    reference = release_for(component)
    if (not isinstance(candidate, dict) or
            raw_version(candidate.get('feed_version')) != candidate.get('version') or
            not valid_image_url(candidate.get('url'))):
        raise FirmwareError('Lenovo returned an invalid controller release.')
    version_number(candidate['version'])
    return replace(reference, version=candidate['version'], size=0, sha256='',
        filename=candidate['url'].rsplit('/', 1)[1])


def release_record(release):
    """Private plan identity, not authorization from a caller-supplied hash."""
    if not isinstance(release, Release):
        raise FirmwareError('Invalid controller image identity.')
    reference = release_for(release.component)
    version_number(release.version)
    if (release.label != reference.label or release.footer != reference.footer or
            not valid_image_url(release.url) or type(release.size) is not int or
            not MIN_IMAGE_BYTES <= release.size <= MAX_IMAGE_BYTES or
            not isinstance(release.sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', release.sha256)):
        raise FirmwareError('Invalid authenticated controller image identity.')
    return {'version': release.version, 'url': release.url,
        'size': release.size, 'sha256': release.sha256}


def release_from_record(component, value):
    if not isinstance(value, dict) or set(value) != {'version', 'url', 'size', 'sha256'}:
        raise FirmwareError('Invalid controller image plan.')
    if not valid_image_url(value['url']):
        raise FirmwareError('Invalid controller image location.')
    release = replace(release_for(component), version=value['version'], size=value['size'],
        sha256=value['sha256'], filename=value['url'].rsplit('/', 1)[1])
    release_record(release)
    return release


def _decode_sfc(original):
    decoded = bytearray(original)
    for offset in range(0, len(decoded), 32):
        state = 0xa80f ^ (offset >> 2)
        for index in range(offset, min(offset + 32, len(decoded))):
            decoded[index] ^= state & 255
            state = ((state << 1) ^ (0x1021 if state & 0x8000 else 0)) & 65535
    return decoded


def _check_structure(original):
    decoded = _decode_sfc(original[:-SIGNATURE_BYTES])
    entries = []
    for offset in range(0, 192, 32):
        header_crc, data_crc, address, size, flags, reserved, index, name = struct.unpack_from('<HHIIBBH16s', decoded, offset)
        if binascii.crc_hqx(decoded[offset+2:offset+32], 0) != header_crc:
            raise FirmwareError('Firmware directory checksum failed.')
        name = name.rstrip(b'\xff').split(b'\0', 1)[0]
        entries.append((name, address, size, data_crc))
    if [entry[0] for entry in entries] != [b'app_area_head', b'app.bin', b'cfg_tool.bin', b'VM', b'PRCT', b'BTIF']:
        raise FirmwareError('Unsupported firmware directory layout.')
    # Remaining entries describe flash regions, not file payload offsets.
    previous_end = 192
    for name, address, size, checksum in entries[1:3]:
        if not size or address < previous_end or address + size > len(decoded) - 4:
            raise FirmwareError('Firmware payload bounds failed.')
        if binascii.crc_hqx(decoded[address:address+size], 0) != checksum:
            raise FirmwareError('Firmware payload checksum failed.')
        previous_end = address + size


def _openssl_executable():
    # Do not execute an arbitrary program from the user's PATH or environment.
    candidates = ('/usr/bin/openssl', '/bin/openssl') if os.name != 'nt' else (
        'C:/Program Files/Git/usr/bin/openssl.exe',)
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FirmwareError('OpenSSL is unavailable. Firmware validation is disabled.')


def _verify_signature(original):
    key_der = base64.b64decode(b''.join(PUBLIC_KEY.splitlines()[1:-1]), validate=True)
    if hashlib.sha256(key_der).hexdigest() != KEY_SHA256:
        raise FirmwareError('The trusted firmware key is invalid.')
    # Vetted OpenSSL verifies PSS; no home-grown cryptographic implementation.
    with tempfile.TemporaryDirectory(prefix='companion-firmware-') as directory:
        directory = Path(directory)
        key, payload, signature = (directory / name for name in ('key.pem', 'payload.bin', 'signature.bin'))
        key.write_bytes(PUBLIC_KEY)
        payload.write_bytes(original[:-SIGNATURE_BYTES])
        signature.write_bytes(original[-SIGNATURE_BYTES:])
        try:
            result = subprocess.run([_openssl_executable(), 'dgst', '-sha256', '-verify', str(key),
                '-signature', str(signature), '-sigopt', 'rsa_padding_mode:pss',
                '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256', str(payload)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False, env={'PATH': os.defpath, 'OPENSSL_CONF': os.devnull},
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FirmwareError('Signature verification could not complete.') from exc
        if result.returncode != 0:
            raise FirmwareError('The Lenovo firmware signature is not valid.')


@dataclass(frozen=True)
class ValidatedImage:
    release: Release
    original: bytes


def validate_image(component, data, release=None):
    # Default is retained for offline reference-image checks. Production supplies
    # the live offer, then the frozen authenticated release at every boundary.
    reference = release_for(component)
    release = reference if release is None else release
    if (not isinstance(release, Release) or release.component != component or
            release.footer != reference.footer or release.label != reference.label):
        raise FirmwareError('Firmware component identity does not match.')
    version_number(release.version)
    if not valid_image_url(release.url):
        raise FirmwareError('Invalid controller image location.')
    if (not isinstance(data, bytes) or not MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES or
            (release.size != 0 and len(data) != release.size)):
        raise FirmwareError('Firmware size is invalid or changed after validation.')
    digest = hashlib.sha256(data).hexdigest()
    if release.sha256 and digest != release.sha256:
        raise FirmwareError('Firmware changed after validation.')
    if data[-SIGNATURE_BYTES-4:-SIGNATURE_BYTES] != release.footer:
        raise FirmwareError('Firmware belongs to a different component.')
    _verify_signature(data)
    _check_structure(data)
    return ValidatedImage(replace(release, size=len(data), sha256=digest), data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise FirmwareError('Lenovo firmware download redirected. Nothing was prepared.')


def _download_http(release, cancelled):
    if not isinstance(release, Release) or not valid_image_url(release.url):
        raise FirmwareError('Invalid Lenovo firmware location.')
    limit = release.size or MAX_IMAGE_BYTES
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    deadline = time.monotonic() + 35
    request = urllib.request.Request(release.url, headers={'Accept-Encoding': 'identity', 'User-Agent': 'LegionGo2Companion'})
    with opener.open(request, timeout=8) as response:
        if response.status != 200 or response.geturl() != release.url:
            raise FirmwareError('Lenovo did not return the expected firmware file.')
        if response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise FirmwareError('Unsupported firmware content encoding.')
        length = response.headers.get('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]{1,8}', length) or
                not MIN_IMAGE_BYTES <= int(length) <= limit or
                (release.size and int(length) != release.size)):
            raise FirmwareError('Lenovo returned an unexpected firmware size.')
        chunks, size = [], 0
        while True:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise FirmwareError('Firmware preparation stopped before validation.')
            # read1 performs at most one buffered/raw read. read(n) can keep
            # waiting for n bytes from a trickling peer beyond our deadline.
            chunk = response.read1(min(65536, limit + 1 - size))
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise FirmwareError('Firmware preparation stopped before validation.')
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise FirmwareError('Downloaded firmware exceeds the size limit.')
            chunks.append(chunk)
    return b''.join(chunks)


def download_image(release, cancelled):
    """Isolate DNS/HTTP/TLS parsing behind a parent-enforced wall deadline."""
    from system_process import system_env
    from diagnostics import event
    if not isinstance(release, Release) or not valid_image_url(release.url):
        raise FirmwareError('Invalid Lenovo firmware location.')
    release_for(release.component)
    version_number(release.version)
    if type(release.size) is not int or (release.size != 0 and not MIN_IMAGE_BYTES <= release.size <= MAX_IMAGE_BYTES):
        raise FirmwareError('Invalid firmware download size.')
    if cancelled.is_set():
        raise FirmwareError('Firmware preparation stopped before validation.')
    interpreter = sys.executable if os.name == 'nt' else '/usr/bin/python3'
    deadline = time.monotonic() + 35
    worker = subprocess.Popen([interpreter, '-I', str(Path(__file__).resolve()),
        '--download-data', release.component, release.version, release.url, str(release.size)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=system_env(),
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        while True:
            remaining = deadline - time.monotonic()
            if cancelled.is_set() or remaining <= 0:
                raise FirmwareError('Firmware preparation stopped before validation.')
            try:
                data, _ = worker.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise FirmwareError('Firmware preparation stopped before validation.')
            if worker.returncode != 0 or not MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES or (release.size and len(data) != release.size):
                event('firmware','download_helper_failed',level='warning',scope=release.component,
                    stage='download',returncode=worker.returncode,bytes=len(data),reason='nonzero_exit' if worker.returncode else 'output_size')
                raise FirmwareError('The Lenovo image download did not complete.')
            return data
    finally:
        # This process only downloads bytes. It never owns a device or flashes.
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)


def read_reported_versions():
    """Read driver-reported versions on the qualified console, without OTA.

    These sysfs reads trigger version-query output in the HID driver, whose
    attributes can return cached values after a timeout. They suffice for a
    conservative catalog comparison, never for flash preflight or post-flash
    success. The independent installer must query its owned USB channel.
    """
    from controller_firmware_usb_linux import observe_command_interface
    before = observe_command_interface()
    if before.driver != '/sys/bus/usb/drivers/usbhid':
        raise FirmwareError('The controller is busy. Try again when it reconnects.')
    base = Path(before.syspath)
    roots = list((base / (base.name + ':1.2')).glob('0003:17EF:61EB.*'))
    if len(roots) != 1:
        raise FirmwareError('The supported controllers are not connected.')
    versions = {}
    # RX image/OTA target1 belongs to the root MCU. TX dongle/target2 is
    # different hardware, even when its firmware version happens to match.
    for component, prefix in (('left', 'left_handle'), ('right', 'right_handle'), ('receiver', '')):
        root = roots[0] / prefix
        if (root / 'hardware_generation').read_text().strip() != '2':
            raise FirmwareError('This controller generation is not supported.')
        value = (root / 'firmware_version').read_text().strip().upper()
        version_number(value)
        versions[component] = value
    after = observe_command_interface()
    if not before.same_attachment(after) or before.driver != after.driver:
        raise FirmwareError('The controller connection changed during the check.')
    return {'versions': versions, 'attachment': (before.boot_id, before.syspath, before.instance,
        before.bus, before.address), 'source': 'hid_driver_reported'}


class FirmwarePreparation:
    """Single bounded backend job, no device access and no persisted arm state."""
    def __init__(self, *, installed_reader=None, installer=None):
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._thread = None
        self._image = None
        self._job = {'id': '', 'phase': 'idle', 'component': '', 'error': '',
            'message': ('Check for Lenovo updates. Available controller updates are installed together.' if installer is not None else
                'Check for Lenovo updates. Installation is not enabled in this experimental build.')}
        self._catalog = {'phase': 'unchecked', 'checked_at': '', 'model': 'Legion Go 8ASP2',
            'region': '', 'candidates': {}, 'error': ''}
        self._installed_reader = installed_reader or read_reported_versions
        # Only a qualified, independent Linux worker may be supplied here.
        # No UI setting, path or flag can substitute for that capability.
        self._installer = installer
        self._installed = None

    def status(self):
        with self._lock:
            self._refresh_prior()
            return {'stage': 'controller_updates', 'flash_available': self._installer is not None,
                'reason': ('Experimental installation is enabled. Live OTA qualification is pending.' if self._installer is not None else
                    'Device transfer is not enabled. Linux transport and recovery checks are pending.'),
                'catalog_date': CATALOG_DATE, 'catalog_live': self._catalog['phase'] == 'checked',
                'catalog': copy.deepcopy(self._catalog),
                'installed_versions': copy.deepcopy(self._installed), 'job': copy.deepcopy(self._job),
                'components': [{'id': r.component, 'label': r.label, 'version': r.version,
                    'bytes': r.size, 'sha256': r.sha256} for r in RELEASES.values()]}

    def check_updates(self):
        """One action for both frontends; component selection is backend-only."""
        from diagnostics import event
        with self._lock:
            self._refresh_prior()
            if self._cancelled.is_set():
                raise FirmwareError('The controller updater is closed.')
            if self._job['phase'] == 'uncertain':
                raise FirmwareError('The previous update has an unverified outcome. Do not retry it.')
            if self._job['phase'] in ('installing','verifying','restoring'):
                return self.status()
            if self._thread is not None and self._thread.is_alive():
                # Two frontends/double activation observe the same job.
                return self.status()
            self._installed = None
            self._job = {'id': uuid.uuid4().hex, 'phase': 'checking', 'component': '',
                'error': '', 'message': 'Checking Lenovo for controller updates.',
                'pending': [], 'completed': []}
            event('firmware','check_started',job_id=self._job['id'])
            self._thread = threading.Thread(target=self._run_updates,
                name='companion-controller-update', daemon=True)
            self._thread.start()
            return self.status()

    def _refresh_prior(self):
        """A backend restart observes the independent job, never replays it."""
        if self._thread is not None and self._thread.is_alive(): return
        reader = getattr(self._installer, 'previous_status', None)
        if not callable(reader): return
        previous = reader()
        if previous and (not self._job['id'] or self._job['id'] == previous['id'] or
                previous['phase'] in ('installing','verifying','restoring','uncertain')):
            self._job = copy.deepcopy(previous)

    def _update_phase(self, phase, message, **fields):
        from diagnostics import event
        with self._lock:
            changed = self._job.get('phase') != phase
            self._job.update(phase=phase, message=message, **fields)
            if changed:
                event('firmware','phase_changed',job_id=self._job['id'],phase=phase,
                    level='warning' if phase in ('failed','uncertain') else 'info')

    def _run_updates(self):
        from controller_firmware_catalog import CatalogError, check_catalog
        from diagnostics import event
        installation_started = False
        stage = 'qualification'
        try:
            before = self._installed_reader()
            stage = 'catalog'
            catalog = check_catalog(self._cancelled)
            stage = 'qualification_readback'
            after = self._installed_reader()
            if before != after:
                raise FirmwareError('Controller connection or versions changed. Check again.')
            if self._cancelled.is_set():
                raise FirmwareError('Controller update check stopped.')
            versions = after['versions']
            stage = 'selection'
            candidates = catalog['candidates']
            if set(versions) != set(RELEASES) or set(candidates) != set(RELEASES):
                raise FirmwareError('Versions for all controller components are required.')
            needed = []
            for component in RELEASES:
                version_number(versions[component])
                candidate = candidates[component]
                if candidate is None:
                    raise FirmwareError('Lenovo did not return firmware for all controller components. Try again later.')
                if update_needed(versions[component], candidate['version']):
                    needed.append(component)
            with self._lock:
                self._installed = copy.deepcopy(versions)
                self._job['pending'] = needed
                self._catalog = dict(catalog, phase='checked', error='')
            event('firmware','catalog_checked',job_id=self._job['id'],count=len(needed))
            if not needed:
                self._update_phase('up_to_date',
                    'Lenovo lists no newer firmware for the versions reported by your controllers.')
                return
            offers = {c: candidate_release(c, candidates[c]) for c in needed}
            if self._installer is None:
                self._update_phase('blocked', 'An update is available. Installation is not enabled in this experimental build. Nothing was installed.')
                return
            images = []
            for component in needed:
                if self._cancelled.is_set():
                    raise FirmwareError('Controller update stopped before installation.')
                self._update_phase('downloading', 'Downloading the controller update.')
                stage = 'download'
                event('firmware','component_started',job_id=self._job['id'],scope=component,stage=stage,version=offers[component].version)
                original = download_image(offers[component], self._cancelled)
                self._update_phase('validating', 'Checking the controller update.')
                stage = 'authentication'
                images.append(validate_image(component, original, release=offers[component]))
                event('firmware','component_authenticated',job_id=self._job['id'],scope=component,bytes=len(original))
            # One independent worker owns the ENTIRE plan. It must revalidate
            # fresh identity/power/versions, run serially, restore input, and
            # report genuine post-update version queries before success.
            with self._lock:
                if self._cancelled.is_set():
                    raise FirmwareError('Controller update stopped before installation.')
                self._job.update(phase='installing',message='Updating controllers. Keep both controllers attached.')
                installation_started = True
            event('firmware','installation_admitted',job_id=self._job['id'],phase='installing',count=len(images))
            stage = 'installer'
            progress_open = True
            def progress(phase, message, **fields):
                with self._lock:
                    if not progress_open:
                        return
                    if phase not in ('installing', 'verifying', 'restoring'):
                        raise FirmwareError('The installer returned an invalid progress phase.')
                    if self._job.get('phase') != phase:
                        event('firmware','phase_changed',job_id=self._job['id'],phase=phase)
                    self._job.update(phase=phase, message=message)
            try:
                result = self._installer(tuple(images), copy.deepcopy(after), progress)
            finally:
                with self._lock:
                    progress_open = False
            worker_id = getattr(self._installer,'diagnostic_job_id','')
            if isinstance(worker_id,str) and re.fullmatch('[0-9a-f]{32}',worker_id):
                event('firmware','worker_observed',job_id=self._job['id'],worker_id=worker_id,
                    outcome=result.get('phase','unknown') if isinstance(result,dict) else 'invalid')
            if (isinstance(result,dict) and result.get('phase')=='failed_before_write' and
                    result.get('restored') is True and result.get('cleanup_complete') is True and
                    result.get('cleanup_errors')==[] and result.get('completed')==[] and
                    result.get('versions')==versions):
                message='The update did not start. Controller input was restored. Check the connection and power, then try again.'
                self._update_phase('failed_before_write',message,error=message)
                return
            if (not isinstance(result, dict) or result.get('phase') != 'succeeded' or
                    result.get('restored') is not True or result.get('cleanup_complete') is not True or
                    result.get('cleanup_errors') != [] or result.get('error') != '' or
                    result.get('completed') != needed):
                self._update_phase('uncertain', 'The controller update could not be verified. Do not retry the update.')
                return
            observed = result.get('versions', {})
            if (not isinstance(observed, dict) or set(observed) != set(RELEASES) or
                    any(observed.get(c) != offers[c].version for c in needed) or
                    any(observed.get(c) != versions[c] for c in RELEASES if c not in needed)):
                self._update_phase('uncertain', 'The installed firmware could not be verified. Do not retry the update.')
                return
            with self._lock:
                self._installed = copy.deepcopy(observed)
            self._update_phase('succeeded', 'All required controller updates were installed and verified.', completed=needed)
        except Exception as exc:
            # Do not turn an exception AFTER admission into a safe-to-retry
            # check failure. The independent worker owns restoration/result.
            installing = installation_started
            phase = 'uncertain' if installing else 'stopped' if self._cancelled.is_set() else 'failed'
            message = ('Controller update could not be verified. Do not retry the update.' if installing else
                str(exc) if isinstance(exc, (FirmwareError, CatalogError)) else 'The controller update check failed. Try again later.')
            worker_id = getattr(self._installer,'diagnostic_job_id','')
            event('firmware','check_failed',level='warning',error=exc,job_id=self._job['id'],stage=stage,
                outcome=phase,worker_id=worker_id if isinstance(worker_id,str) and re.fullmatch('[0-9a-f]{32}',worker_id) else '')
            self._update_phase(phase, message, error=message)

    def prepare(self, component):
        release = release_for(component)
        with self._lock:
            if self._cancelled.is_set():
                raise FirmwareError('Firmware preparation is closed.')
            if self._thread is not None and self._thread.is_alive():
                raise FirmwareError('A firmware check is already running.')
            self._image = None
            self._job = {'id': uuid.uuid4().hex, 'phase': 'downloading', 'component': component, 'error': ''}
            self._thread = threading.Thread(target=self._run, args=(release,),
                name='companion-firmware-prepare', daemon=True)
            self._thread.start()
            return self.status()

    def check_catalog(self):
        with self._lock:
            if self._cancelled.is_set():
                raise FirmwareError('Firmware preparation is closed.')
            if self._thread is not None and self._thread.is_alive():
                raise FirmwareError('A firmware check is already running.')
            self._catalog['phase'] = 'checking'
            self._catalog['error'] = ''
            self._job = {'id': uuid.uuid4().hex, 'phase': 'checking_catalog', 'component': '', 'error': ''}
            self._thread = threading.Thread(target=self._run_catalog,
                name='companion-firmware-catalog', daemon=True)
            self._thread.start()
            return self.status()

    def _run_catalog(self):
        from controller_firmware_catalog import CatalogError, check_catalog
        from diagnostics import event
        try:
            catalog = check_catalog(self._cancelled)
            for component, candidate in catalog['candidates'].items():
                if candidate is not None:
                    release = release_for(component)
                    candidate['reviewed'] = (candidate['version'] == release.version and
                        candidate['url'] == release.url)
                    # Frontends need the version/review result, not a download link.
                    del candidate['url']
            with self._lock:
                if self._cancelled.is_set():
                    raise CatalogError('Lenovo catalog check stopped.')
                self._catalog = dict(catalog, phase='checked', error='')
                self._job['phase'] = 'catalog_checked'
        except Exception as exc:
            event('firmware','catalog_check_failed',level='warning',error=exc,job_id=self._job['id'],stage='catalog')
            with self._lock:
                phase = 'stopped' if self._cancelled.is_set() else 'failed'
                error = str(exc) if isinstance(exc, CatalogError) else 'Lenovo catalog check did not complete.'
                # Keep the prior timestamp/results, explicitly stale after failure.
                self._catalog.update(phase=phase, error=error)
                self._job.update(phase=phase, error=error)

    def _run(self, release):
        from diagnostics import event
        try:
            original = download_image(release, self._cancelled)
            with self._lock:
                if self._cancelled.is_set():
                    raise FirmwareError('Firmware preparation stopped.')
                self._job['phase'] = 'validating'
            validated = validate_image(release.component, original)
            with self._lock:
                if self._cancelled.is_set():
                    raise FirmwareError('Firmware preparation stopped.')
                self._image = validated
                self._job['phase'] = 'validated'
        except Exception as exc:
            event('firmware','image_preparation_failed',level='warning',error=exc,scope=release.component,job_id=self._job['id'])
            with self._lock:
                self._image = None
                self._job['phase'] = 'stopped' if self._cancelled.is_set() else 'failed'
                self._job['error'] = (str(exc) if isinstance(exc, FirmwareError) else
                    'Image preparation did not complete. No device was changed.')

    def close(self):
        from diagnostics import event
        with self._lock:
            if not self._cancelled.is_set():
                event('firmware','observer_closed',job_id=self._job['id'],phase=self._job['phase'])
            self._cancelled.set()
            if self._installer is not None and self._job['phase'] in ('installing','verifying','restoring'):
                # An admitted frozen systemd worker owns the transaction. Its
                # backend observer must not hold up unrelated module shutdown.
                self._image = None
                return
            worker = self._thread
        if worker is not None:
            worker.join(timeout=55)
            if worker.is_alive():
                raise FirmwareError('Firmware preparation is still stopping.')
        with self._lock:
            self._image = None


if __name__ == '__main__':
    # Isolated official-host download only. No file path or device I/O.
    if len(sys.argv) != 6 or sys.argv[1] != '--download-data':
        raise SystemExit(2)
    try:
        reference = release_for(sys.argv[2])
        version_number(sys.argv[3])
        size = int(sys.argv[5])
        if size != 0 and not MIN_IMAGE_BYTES <= size <= MAX_IMAGE_BYTES: raise FirmwareError('size')
        release = replace(reference, version=sys.argv[3], size=size, sha256='', filename=sys.argv[4].rsplit('/',1)[1])
        if release.url != sys.argv[4]: raise FirmwareError('url')
        content = _download_http(release, threading.Event())
        sys.stdout.buffer.write(content)
    except Exception:
        raise SystemExit(1)
