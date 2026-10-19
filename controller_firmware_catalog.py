# SPDX-License-Identifier: BSD-3-Clause
"""Explicit official-feed discovery. No image download or device access.

Only the reviewed Go 2 8ASP2 feed is queried. Discovery uses an all-zero query
version, not an observation of installed firmware. Feed metadata is not image
authentication; every selected image is authenticated before device access.
"""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://gms.service.lenovo.com/game'
MODEL = 'Legion Go 8ASP2'
TARGETS = (
    ('left', 62, 'com.lenovo.legionspace.lhandle.sanyi'),
    ('right', 63, 'com.lenovo.legionspace.rhandle.sanyi'),
    ('receiver', 58, 'com.lenovo.legionspace.rx'),
)
MAX_BODY = 128 * 1024
# The feed answers in ~0.2 s, but on 2026-10-17 a third to a half of its replies were
# HTTP 500 after 10 s, sometimes several in a row; short attempts are retried until the
# shared budget runs out, so one bad reply does not fail the check.
REQUEST_SECONDS = 3
HELPER_SECONDS = 85
WALL_SECONDS = 90


class CatalogError(RuntimeError):
    pass


def raw_version(value):
    """Official dotted date/revision domain observed in the current feed."""
    if not isinstance(value, str) or not re.fullmatch(r'v\d{2}\.\d{2}\.\d{2}\.0[0-6]', value, flags=re.ASCII):
        raise CatalogError('Lenovo returned an unsupported version format.')
    year, month, day, revision = (int(part) for part in value[1:].split('.'))
    try:
        date(2000 + year, month, day)
    except ValueError as exc:
        raise CatalogError('Lenovo returned an invalid firmware date.') from exc
    return f'{year:02}{month:02}{day:02}' + ('0' if not revision else chr(64 + revision))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp=None, *args):
        if fp is not None:
            fp.close()
        raise CatalogError('Lenovo catalog redirected. The check was stopped.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError('Lenovo returned ambiguous catalog data.')
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CatalogError('Lenovo returned invalid catalog data.') from exc


def _fetch(path, parameters):
    # Callers use only the two constant API paths below. Returned hosts, links,
    # credentials and serial-number fields are never followed or forwarded.
    url = BASE + path + '?' + urllib.parse.urlencode(parameters)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers={
        'Accept-Encoding': 'identity', 'User-Agent': 'LegionGo2Companion'})
    with opener.open(request, timeout=REQUEST_SECONDS) as response:
        if response.status != 200 or response.geturl() != url:
            raise CatalogError('The Lenovo catalog request did not complete.')
        if response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise CatalogError('Unsupported catalog content encoding.')
        data = response.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise CatalogError('Lenovo catalog exceeds the size limit.')
    return _json(data)


def _transient(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    return isinstance(exc, (TimeoutError, ConnectionError))


def _fetch_retrying(path, parameters, deadline):
    """Only server errors and timeouts are retried; any other answer is final."""
    while True:
        try:
            return _fetch(path, parameters)
        except Exception as exc:
            if not _transient(exc) or time.monotonic() + REQUEST_SECONDS >= deadline:
                raise
            time.sleep(.5)


def _candidate(body, terminal, package):
    if not isinstance(body, dict) or type(body.get('code')) is not int:
        raise CatalogError('Lenovo returned an unsupported catalog response.')
    if body['code'] == 201 and body.get('data') is None:
        return None
    entries = body.get('data')
    if body['code'] != 200 or not isinstance(entries, list) or len(entries) != 1:
        raise CatalogError('Lenovo returned an ambiguous firmware selection.')
    entry = entries[0]
    if (not isinstance(entry, dict) or type(entry.get('terminalId')) is not int or
            entry['terminalId'] != terminal or entry.get('packageEname') != package or
            type(entry.get('updateType')) is not int or entry['updateType'] != 2):
        raise CatalogError('Lenovo catalog component identity does not match.')
    version = raw_version(entry.get('versionCode'))
    # versionName is presentation metadata, not an independent firmware identity.
    # Machine versionCode selects the release; component, URL and signed image
    # validation remain mandatory at the later download/transfer boundaries.
    url = entry.get('packageUrl')
    if not valid_image_url(url):
        raise CatalogError('Lenovo returned an unsupported firmware location.')
    # packageSize is rounded text, not an exact byte size or authenticity gate.
    return {'version': version, 'feed_version': entry['versionCode'], 'url': url}


def valid_image_url(url):
    return isinstance(url, str) and re.fullmatch(
        r'https://s3\.service\.lenovo\.com/legion/cms/package/[A-Za-z0-9_-]{1,128}\.bin', url) is not None


def _check_http():
    deadline = time.monotonic() + HELPER_SECONDS  # Inside the parent's WALL_SECONDS.
    bootstrap = _fetch_retrying('/api/getBaseUrls', {'source': '1', 'language': 'en', 'modelName': 'Legion Go 2'}, deadline)
    if (not isinstance(bootstrap, dict) or type(bootstrap.get('code')) is not int or
            bootstrap['code'] != 200 or not isinstance(bootstrap.get('data'), dict)):
        raise CatalogError('Lenovo catalog configuration is unavailable.')
    data = bootstrap['data']
    if data.get('gameDomain') != BASE:
        raise CatalogError('Lenovo changed the catalog service. Review is required.')
    area = data.get('areaCode')
    if not isinstance(area, str) or not re.fullmatch(r'[A-Z]{2}', area):
        raise CatalogError('Lenovo catalog region is not confirmed.')
    extension = data.get('extend')
    machines = extension.get('MachineAdaption') if isinstance(extension, dict) else None
    if not isinstance(machines, list) or not any(isinstance(m, dict) and
            m.get('MachineName') == MODEL and m.get('MachineType') == 'Legion Go 2' for m in machines):
        raise CatalogError('The reviewed Legion Go 2 model is absent from the catalog.')
    candidates = {}
    for component, terminal, package in TARGETS:
        body = _fetch_retrying('/api/queryPackageInfo', {'modelName': MODEL, 'sn': '',
            'countryCode': area, 'packageCode': package, 'terminalId': str(terminal),
            'packageVer': 'v00.00.00.00'}, deadline)
        candidates[component] = _candidate(body, terminal, package)
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL,
        'region': area, 'candidates': candidates}


def check_catalog(cancelled):
    """Parent wall deadline also covers DNS, TLS, headers and JSON parsing."""
    from system_process import run_helper
    from diagnostics import event
    if cancelled.is_set():
        raise CatalogError('Lenovo catalog check stopped.')
    returncode, data = run_helper([str(Path(__file__).resolve()), '--catalog-data'], cancelled,
        WALL_SECONDS, CatalogError('Lenovo catalog check stopped or timed out.'))
    if returncode != 0 or len(data) > 16384:
        event('firmware','catalog_helper_failed',stage='catalog',returncode=returncode,
            reason='nonzero_exit' if returncode else 'output_size')
        raise CatalogError('Lenovo catalog check failed. Try again later.')
    return _json(data)


if __name__ == '__main__':
    if sys.argv[1:] != ['--catalog-data']:
        raise SystemExit(2)
    try:
        sys.stdout.buffer.write(json.dumps(_check_http(), ensure_ascii=True).encode('utf-8'))
    except Exception:
        raise SystemExit(1)
