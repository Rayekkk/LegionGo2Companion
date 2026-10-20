# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Controller firmware updates: Lenovo feed, image authentication and fwupd install.

The live model-specific Lenovo feed selects releases; the pinned vendor key and
signed component footer authenticate images, including new release versions.
fwupd's legion-hid plugin writes them. Both frontends observe this one backend
job. Original signed bytes are kept in memory; decoded bytes are used only for
structural checks.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import hmac
import json
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
MAX_IMAGE_BYTES = 1024 * 1024
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
    from system_process import NO_WINDOW, run
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
            result = run([_openssl_executable(), 'dgst', '-sha256', '-verify', str(key),
                '-signature', str(signature), '-sigopt', 'rsa_padding_mode:pss',
                '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256', str(payload)],
                10, env={'PATH': os.defpath, 'OPENSSL_CONF': os.devnull},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
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
    # shortcut: the image carries no verifiable version, so only pinned releases are
    # caught under a wrong catalog label; an unpinned older image still passes.
    if any(known.sha256 == digest and known.version != release.version for known in RELEASES.values()):
        raise FirmwareError("Lenovo's catalog gives a known controller image a different version. "
            'Nothing was installed. Try again later.')
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
    from system_process import run_helper
    from diagnostics import event
    if not isinstance(release, Release) or not valid_image_url(release.url):
        raise FirmwareError('Invalid Lenovo firmware location.')
    release_for(release.component)
    version_number(release.version)
    if type(release.size) is not int or (release.size != 0 and not MIN_IMAGE_BYTES <= release.size <= MAX_IMAGE_BYTES):
        raise FirmwareError('Invalid firmware download size.')
    if cancelled.is_set():
        raise FirmwareError('Firmware preparation stopped before validation.')
    # The helper only downloads bytes. It never owns a device or flashes.
    returncode, data = run_helper([str(Path(__file__).resolve()), '--download-data', release.component,
        release.version, release.url, str(release.size)], cancelled, 35,
        FirmwareError('Firmware preparation stopped before validation.'))
    if returncode != 0 or not MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES or (release.size and len(data) != release.size):
        event('firmware','download_helper_failed',level='warning',scope=release.component,
            stage='download',returncode=returncode,bytes=len(data),reason='nonzero_exit' if returncode else 'output_size')
        raise FirmwareError('The Lenovo image download did not complete.')
    return data


FWUPDTOOL = '/usr/bin/fwupdtool'
FWUPDMGR = '/usr/bin/fwupdmgr'
# Supervised same-version installs through the plugin passed on 2026-10-17 (right 69.5 s,
# left 45.2 s, receiver 37.1 s). False is the kill switch; Check and Download stay available.
INSTALL_ENABLED = True
INSTALL_DISABLED_REASON = ('Installation is turned off in this build. '
    'Use Legion Space on Windows to update the controllers.')
CHECK_MAX_AGE = 15 * 60
# Per device. Writes take 37-70 s and LVFS states 300 s for all three components. fwupdtool is stopped
# only after INSTALL_STALL_SECONDS without new log output (the former 600 s total limit, so never earlier
# than before) or at the absolute INSTALL_SECONDS cap; a run that is still writing is never cut short.
INSTALL_STALL_SECONDS, INSTALL_SECONDS = 600, 1800
VERIFY_ATTEMPTS, VERIFY_DELAY = 10, 2  # The same version re-read budget fwupd legion-hid uses.
CONTAINER_ORDER = ('receiver', 'left', 'right')
INSTALL_ORDER = ('left', 'right', 'receiver')  # Pads are written through the MCU, so it restarts last.
FWUPD_INSTANCES = MappingProxyType({'receiver': 'HIDRAW\\VEN_17EF&DEV_61EB',
    'left': 'HIDRAW\\VEN_17EF&DEV_61EB&CHILD_LEFT', 'right': 'HIDRAW\\VEN_17EF&DEV_61EB&CHILD_RIGHT'})
SYSFS_NODES = (('left', 'left_handle'), ('right', 'right_handle'), ('receiver', ''))
UPDATER_PROCESSES = frozenset({'fwupdtool', 'fwupdmgr', 'steamos-firmwar'})  # comm is cut to 15 bytes.


def _text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_versions_once(root):
    versions = {}
    for component, node in SYSFS_NODES:
        if _text(root / node / 'hardware_generation') != '2':
            raise FirmwareError('This controller generation is not supported.')
        value = (_text(root / node / 'firmware_version') or '').upper()
        version_number(value)
        versions[component] = value
    return versions


def applies(sysfs=Path('/sys')):
    """The model gate (DMI only); elsewhere the firmware page stays hidden."""
    dmi = sysfs / 'class/dmi/id'
    return tuple(_text(dmi / name) for name in ('sys_vendor', 'product_name', 'product_version')) == (
        'LENOVO', '83N0', 'Legion Go 8ASP2')


def read_installed_versions(sysfs=Path('/sys')):
    """Versions reported by the in-tree hid-lenovo-go driver; read-only.

    Each read makes the driver send its own version query. After a 50 ms
    timeout it returns a cached value, so two equal reads are required.
    """
    if not applies(sysfs):
        raise FirmwareError('Controller updates are available only on the Legion Go 2 (83N0).')
    usb = [path for path in (sysfs / 'bus/usb/devices').glob('*')
        if _text(path / 'idVendor') == '17ef' and _text(path / 'idProduct') == '61eb']
    roots = [path for path in (sysfs / 'bus/hid/devices').glob('0003:17EF:61EB.*')
        if (path / 'firmware_version').is_file()]
    if len(usb) != 1 or len(roots) != 1:
        raise FirmwareError('The supported controllers are not connected.')
    root = roots[0]
    if (os.path.basename(os.path.realpath(root / 'driver')) != 'hid-lenovo-go' or
            not Path(os.path.realpath(root)).is_relative_to(os.path.realpath(usb[0]))):
        raise FirmwareError('The controllers are not managed by the hid-lenovo-go driver.')
    first = _read_versions_once(root)
    if _read_versions_once(root) != first:
        raise FirmwareError('Controller versions changed while reading. Check again.')
    return first


def console_power():
    supplies = list(Path('/sys/class/power_supply').iterdir())
    if len(supplies) > 32: raise FirmwareError('Power supply inventory exceeds its bound.')
    ac, batteries = False, []
    for path in supplies:
        kind = (path/'type').read_text().strip()
        scope = (path/'scope').read_text().strip() if (path/'scope').exists() else None
        if scope == 'Device':
            continue
        if kind in ('Mains','USB','USB_C','USB_PD') and (path/'online').exists():
            ac = ac or (path/'online').read_text().strip() == '1'
        # Same rule as battery_backend: ACPI batteries such as the Legion Go 2's BAT0 have no scope file.
        if kind == 'Battery' and (scope == 'System' or (scope is None and re.fullmatch(r'BAT\d+', path.name))):
            batteries.append(int((path/'capacity').read_text().strip()))
    if len(batteries) > 1: raise FirmwareError('Console battery could not be identified.')
    percent = batteries[0] if batteries else None
    if not ac and (percent is None or not 30 <= percent <= 100):
        raise FirmwareError('Connect power or charge the console to at least 30%.')
    return ac, percent


def pad_power(read, components, partial=False):
    """fwupd writes each pad over the receiver link: a detached or low pad could drop mid-write.

    partial: an earlier component of this install is already written."""
    pads = [c for c in ('left', 'right') if c in components]
    levels = read() if pads and read else None
    for side in pads:
        level = levels.get(side) if isinstance(levels, dict) else None
        if levels is None or levels.get('connection_' + side) != 'attached' or type(level) is not int or level < 30:
            raise FirmwareError(f'{RELEASES[side].label}: attach it to the console and charge it to at least 30%. '
                                + ('Components updated before it keep their new firmware; check again to finish.'
                                   if partial else 'Nothing was installed.'))


def build_container(images):
    """fwupd legion-hid container: six LE u32 (size, version) for the receiver,
    left and right images, then the three signed images unchanged.

    fwupd rejects a zero-sized component and writes only the image whose
    footer id matches the target device (receiver 2, left 7, right 8).
    """
    if set(images) != set(CONTAINER_ORDER) or any(not isinstance(images[c], ValidatedImage) or
            images[c].release.component != c for c in CONTAINER_ORDER):
        raise FirmwareError('All three verified controller images are required.')
    header = b''.join(struct.pack('<II', len(images[c].original), version_number(images[c].release.version))
        for c in CONTAINER_ORDER)
    return header + b''.join(images[c].original for c in CONTAINER_ORDER)


def _run_tool(argv, timeout, **streams):
    from system_process import run, system_env
    return run(argv, timeout, env=system_env(), **streams)  # fwupd keeps the parent's locale


def fwupd_devices():
    """legion-hid device ids and versions from the running fwupd daemon (read-only)."""
    try:
        result = _run_tool([FWUPDMGR, 'get-devices', '--json'], 60, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        devices = json.loads(result.stdout).get('Devices') if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as exc:
        raise FirmwareError('fwupd did not list the controllers.') from exc
    if not isinstance(devices, list):
        raise FirmwareError('fwupd did not list the controllers.')
    found = {}
    for component, instance in FWUPD_INSTANCES.items():
        matches = [d for d in devices if isinstance(d, dict) and d.get('Plugin') == 'legion_hid'
            and isinstance(d.get('InstanceIds'), list) and instance in d['InstanceIds']]
        device = matches[0] if len(matches) == 1 else {}
        if (not isinstance(device.get('DeviceId'), str) or not re.fullmatch('[0-9a-f]{40}', device['DeviceId']) or
                not isinstance(device.get('Flags'), list) or 'updatable' not in device['Flags'] or
                not isinstance(device.get('Version'), str)):
            raise FirmwareError('fwupd does not list the controllers as updatable.')
        found[component] = {'id': device['DeviceId'], 'version': device['Version'].upper()}
    return found


def updater_running(proc=Path('/proc')):
    """Steam's steamos-firmware-update and any fwupd client could write the same device."""
    return any(_text(path) in UPDATER_PROCESSES for path in proc.glob('[0-9]*/comm'))


def install_blob(container, device_id, log_path):
    """One fwupd write. As root, fwupdtool stops the daemon and runs its own engine."""
    from system_process import NO_WINDOW, system_env
    started = progress = time.monotonic()
    size = 0
    with open(log_path, 'wb') as log:
        tool = subprocess.Popen([FWUPDTOOL, 'install-blob', str(container), device_id, '--no-reboot-check', '--verbose'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=system_env(), **NO_WINDOW)
        while True:
            try:
                returncode = tool.wait(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                pass
            now, grown = time.monotonic(), os.fstat(log.fileno()).st_size
            if grown != size:
                size, progress = grown, now
            if now - progress > INSTALL_STALL_SECONDS or now - started > INSTALL_SECONDS:
                tool.terminate()  # Let fwupdtool exit on SIGTERM first; SIGKILL only if it does not.
                try:
                    tool.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    tool.kill()
                    tool.wait()
                returncode = None
                break
    with open(log_path, 'rb') as log:
        log.seek(max(0, os.path.getsize(log_path) - 4096))
        tail = log.read().decode('utf-8', 'replace')
    return returncode, round(time.monotonic() - started, 1), tail


PHASE_MESSAGES = MappingProxyType({
    'idle': 'Check Lenovo for controller firmware updates.',
    'checking': 'Reading controller versions and checking Lenovo.',
    'up_to_date': 'Your controllers have the latest Lenovo firmware.',
    'available': 'A newer controller firmware is available. Download it to verify the files.',
    'downloading': 'Downloading and verifying the Lenovo firmware.',
    'downloaded': 'The firmware is downloaded and verified.',
    'installing': 'Installing. Keep AC power connected and do not detach the controllers.',
    'verifying': 'Waiting for the controller to report the new version.',
    'installed': 'The controller update was installed and verified.',
    'failed': 'The last firmware step did not complete. Check again.',
})
BUSY_PHASES = frozenset({'checking', 'downloading', 'installing', 'verifying'})


class FirmwarePreparation:
    """One firmware step at a time: check, download, then install with a token.

    Downloaded images live only in memory and stay bound to the check that
    selected them. Nothing is persisted, so a failed step never locks retries.
    """

    def __init__(self, *, reader=None, pads=None, sleep=time.sleep):
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._thread = None
        self._reader = reader or read_installed_versions
        self._pads = pads  # Returns controller attach/battery levels; None refuses every pad install.
        self._sleep = sleep
        self._phase, self._component, self._error = 'idle', '', ''
        self._check = None   # id, at, checked_at, installed, offers, newer
        self._images = None  # All three ValidatedImage objects for self._check.
        self._token = ''

    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        check = self._check or {}
        installed, offers, newer = check.get('installed', {}), check.get('offers', {}), check.get('newer', ())
        message = self._error or PHASE_MESSAGES[self._phase]
        if self._component and not self._error:
            message = RELEASES[self._component].label + ': ' + message
        return {'phase': self._phase, 'message': message, 'error': self._error, 'component': self._component,
            'install_enabled': INSTALL_ENABLED, 'install_reason': '' if INSTALL_ENABLED else INSTALL_DISABLED_REASON,
            'checked_at': check.get('checked_at', ''), 'token': self._token,
            'components': [{'id': c, 'label': r.label, 'installed': installed.get(c, ''),
                'available': offers[c].version if c in offers else '', 'newer': c in newer,
                'downloaded': self._images is not None and c in newer} for c, r in RELEASES.items()]}

    def _busy(self):
        return self._phase in BUSY_PHASES

    def _start(self, phase, target, *args):
        if self._cancelled.is_set():
            raise FirmwareError('The controller updater is closed.')
        self._phase, self._component, self._error = phase, '', ''
        self._thread = threading.Thread(target=target, args=args, name='companion-firmware', daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._phase, self._error = 'failed', 'The firmware step could not start. Try again.'
            raise

    def _fresh_check(self, phase):
        check = self._check
        if self._phase != phase or check is None:
            raise FirmwareError('This step is not available now. Check for updates again.')
        if time.monotonic() - check['at'] > CHECK_MAX_AGE:
            # Leave the phase from which both UIs offer Check, or the stale step could never be retried.
            self._check, self._images, self._token = None, None, ''
            self._phase, self._component, self._error = 'failed', '', 'The update check is out of date. Check for updates again.'
            raise FirmwareError(self._error)
        return check

    def check(self):
        from diagnostics import event
        with self._lock:
            if self._busy():
                return self._status()  # Both frontends observe the step already running.
            self._check, self._images, self._token = None, None, ''
            self._start('checking', self._run_check)
            event('firmware', 'check_started')
            return self._status()

    def download(self):
        with self._lock:
            if self._busy():
                return self._status()
            self._start('downloading', self._run_download, self._fresh_check('available'))
            return self._status()

    def admit_install(self, token):
        """Refuse before the caller pauses controller modules; only reads the pads' input report."""
        with self._lock:
            return self._admit(token)

    def _admit(self, token):
        if not INSTALL_ENABLED:
            raise FirmwareError(INSTALL_DISABLED_REASON)
        if self._busy():
            raise FirmwareError('A firmware step is already running.')
        check = self._fresh_check('downloaded')
        if (not isinstance(token, str) or not self._token or self._images is None or
                not hmac.compare_digest(token, self._token)):
            raise FirmwareError('The confirmed update no longer matches the download. Check for updates again.')
        try:
            console_power()  # A refusal here keeps the verified download; _run_install re-checks it.
        except (OSError, ValueError) as exc:
            raise FirmwareError('Console power could not be read. Nothing was installed.') from exc
        pad_power(self._pads, check['newer'])
        return check, self._images

    def install(self, token, on_done):
        """on_done runs exactly once: when the install ends or is refused."""
        try:
            with self._lock:
                check, images = self._admit(token)
                self._start('installing', self._run_install, check, images, on_done)
                return self._status()
        except BaseException:
            on_done()
            raise

    def _finish(self, phase, **state):
        with self._lock:
            for key, value in state.items():
                setattr(self, '_' + key, value)
            if self._cancelled.is_set():
                self._images, self._token = None, ''  # A closed updater still reports its outcome, but offers no install.
            self._phase, self._component, self._error = phase, '', ''

    def _step(self, phase, component):
        with self._lock:
            self._phase, self._component = phase, component

    def _fail(self, stage, exc):
        from controller_firmware_catalog import CatalogError
        from diagnostics import event
        message = (str(exc) if isinstance(exc, (FirmwareError, CatalogError)) else
            'The firmware step did not complete. Check again.')
        event('firmware', 'step_failed', level='warning', error=exc, stage=stage)
        with self._lock:
            self._check, self._images, self._token = None, None, ''
            self._phase, self._component, self._error = 'failed', '', message

    def _run_check(self):
        from controller_firmware_catalog import check_catalog
        from diagnostics import event
        try:
            # Version reads query the controllers; an updater that outlived a restart may be writing them.
            if updater_running():
                raise FirmwareError('Another firmware update is running. Try again when it finishes.')
            before = self._reader()
            catalog = check_catalog(self._cancelled)
            if self._reader() != before:
                raise FirmwareError('Controller versions changed during the check. Check again.')
            candidates = catalog.get('candidates') if isinstance(catalog, dict) else None
            if not isinstance(candidates, dict) or any(candidates.get(c) is None for c in RELEASES):
                raise FirmwareError('Lenovo did not return firmware for all controller components. Try again later.')
            offers = {c: candidate_release(c, candidates[c]) for c in RELEASES}
            newer = tuple(c for c in RELEASES if update_needed(before[c], offers[c].version))
            self._finish('available' if newer else 'up_to_date', check={'id': uuid.uuid4().hex,
                'at': time.monotonic(), 'checked_at': str(catalog.get('checked_at', '')),
                'installed': dict(before), 'offers': offers, 'newer': newer})
            event('firmware', 'check_finished', count=len(newer))
        except Exception as exc:
            self._fail('check', exc)

    def _run_download(self, check):
        from diagnostics import event
        try:
            # fwupd needs all three images in the container, even for one device.
            images = {c: validate_image(c, download_image(check['offers'][c], self._cancelled),
                release=check['offers'][c]) for c in CONTAINER_ORDER}
            identity = {'check': check['id'], 'targets': list(check['newer']), 'images': {c: [check['installed'][c],
                image.release.version, image.release.sha256] for c, image in images.items()}}
            token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode('ascii')).hexdigest()
            self._finish('downloaded', images=images, token=token)
            event('firmware', 'download_finished', count=len(images))
        except Exception as exc:
            self._fail('download', exc)

    def _verified(self, expected):
        for _ in range(VERIFY_ATTEMPTS):
            self._sleep(VERIFY_DELAY)
            try:
                if self._reader() == expected:
                    return True
            except (OSError, FirmwareError):
                pass  # The controller may still be restarting.
        return False

    def _run_install(self, check, images, on_done):
        from diagnostics import event
        try:
            installed = dict(check['installed'])
            if not Path(FWUPDTOOL).is_file():
                raise FirmwareError('fwupd is not available on this system. Nothing was installed.')
            console_power()
            with tempfile.TemporaryDirectory(prefix='companion-fwupd-') as directory:
                container = Path(directory) / 'legion-go2-controllers.bin'
                container.write_bytes(build_container(images))
                for component in INSTALL_ORDER:
                    if component not in check['newer']:
                        continue
                    label = RELEASES[component].label
                    if self._reader() != installed:
                        raise FirmwareError('Controller versions changed before installation. Check again.')
                    devices = fwupd_devices()
                    if any(devices[c]['version'] != installed[c] for c in devices):
                        raise FirmwareError('fwupd and the controller driver report different versions. Check again.')
                    if updater_running():
                        raise FirmwareError('Another firmware update is running. Try again when it finishes.')
                    pad_power(self._pads, (component,), partial=installed != check['installed'])
                    self._step('installing', component)
                    event('firmware', 'install_started', scope=component, version=images[component].release.version)
                    returncode, seconds, tail = install_blob(container, devices[component]['id'],
                        Path(directory) / 'fwupdtool.log')
                    event('firmware', 'fwupd_finished', level='info' if returncode == 0 else 'warning',
                        scope=component, returncode=returncode, duration_ms=seconds * 1000,
                        error=None if returncode == 0 else tail[-512:] or 'no output')
                    if returncode != 0:
                        raise FirmwareError(f'{label}: fwupd did not complete the update. Check again to retry. '
                            'If a controller does not respond, restart the console.')
                    installed[component] = images[component].release.version
                    self._step('verifying', component)
                    if not self._verified(installed):
                        raise FirmwareError(f'{label} did not report the new version. Check again to retry. '
                            'If a controller does not respond, restart the console.')
            self._finish('installed', check=dict(check, installed=installed, newer=()), images=None, token='')
            event('firmware', 'install_finished', count=len(check['newer']))
        except Exception as exc:
            self._fail('install', exc)
        finally:
            on_done()

    def close(self):
        with self._lock:
            self._cancelled.set()
            worker = self._thread
            installing = self._phase in ('installing', 'verifying')
        # fwupd owns an admitted write; interrupting it is worse than letting it finish.
        if worker is not None and not installing:
            worker.join(timeout=55)
            if worker.is_alive():
                raise FirmwareError('Firmware preparation is still stopping.')
        with self._lock:
            if not installing:
                self._images, self._token = None, ''


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
