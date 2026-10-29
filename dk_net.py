# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/backend/dk_net.py): change it there, then `kit sync`. Needs dk_process.py.

"""HTTPS downloads a plugin can trust: verified, bounded in size and time, pinned by hash when it matters.

Sources: LegionGo2Companion/bounded_http.py (isolated fetch process, HTTPS-only URLs, host allow-list on every
redirect, wall-clock deadline, cancel, OS trust store first, anchors handed to the child), DeckyReshade/
reshade_net.py (download through .part + rename, cut-short bodies, IncompleteRead, a full disk reported as a
download error, the body kept on the destination's disk), DeckyLS/lsfg_install.py (pinned SHA-256 of the content),
FSR4Injector/fsr4_dll.py (one part of a large file by an HTTP range, checked against Content-Range; progress; the
body handed over for decoding instead of kept; a deadline long enough for 45 MB on a slow link),
Ayaneo3Companion/lego_updater.py (no fragments; redirects narrower than the first URL's hosts).
The status of an HTTP refusal (NetError.status) is for the shared updater (404: no release yet).

Guarantees:
- Only https:// URLs on port 443, without user:password@, a #fragment or control characters. Every call names its
  `hosts` (None only on purpose: any https host); the first URL and every redirect must pass the same check, with
  redirects=False any redirect is refused, and with redirects=<hosts> a redirect must also go to one of those (the
  updater's release URL on github.com may only lead to GitHub's asset hosts).
- TLS is always verified (hostname and chain), never turned off: the system trust store, or when Decky's frozen
  Python sees none, the OS CA bundles and then certifi. The fetch process gets the same trust anchors, verify flags
  and TLS versions as the plugin's context.
- The request runs in its own short-lived `python -I` process: a hung DNS lookup or TLS handshake cannot hold a
  thread of Decky's executor. The whole request has a wall-clock deadline (default 120 s, at most 1800 s) and can
  be cancelled; either kills the process. progress(bytes so far) is called about five times a second meanwhile.
- At most `limit` bytes are accepted (TooLarge); a body shorter than its Content-Length or cut by the connection is
  an error, never a short file; anything but HTTP 200 is an error. With byte_range=(first, last) only exactly that
  part is accepted: HTTP 206 whose Content-Range names it and exactly last - first + 1 bytes (a server that sends
  the whole file instead is refused). Limits, ranges and deadlines are checked by the plugin and again by the fetch
  process, which trusts no caller.
- Every failure is a NetError (also a fetch process that cannot start, and a full or read-only disk); a
  cancelled request starts no process. An answer other than HTTP 200 carries its code in NetError.status.
- Requests carry a User-Agent (some CDNs refuse urllib's default).
- With `sha256` given, other content is refused (Mismatch) and nothing is kept.
- download() spools the body to an anonymous temp file in dest's folder (SteamOS's /tmp is RAM), checks size
  and hash, then publishes it with dk_fs.write_atomic (no symlinks as root, fsync of file and folder, owner and mode
  kept); on any failure dest is untouched. Needs dk_fs.py. spool() hands the same checked body over as an open
  anonymous file in a folder of the caller's choice (content to decode or unpack, never kept as it is); nothing
  is left on disk once it is closed or after a failure.
- Error messages (NetError and subclasses) say what went wrong without the URL; the caller says what it fetched.

Deliberately not here: retries (the user presses again), GitHub release parsing and per-request redirect
policies such as Legion's metadata/archive (the shared updater passes `hosts` and `redirects`), proxies, cookies,
HTTP auth.
"""

import hashlib
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30  # each connect/read
DEADLINE = 120  # the whole request
MAX_BODY = 256 * 1024 * 1024
MAX_DEADLINE = 1800  # a large file over a slow link (45 MB at 25 kB/s)
USER_AGENT = "DeckyKit/1 (Decky Loader plugin)"
CHUNK = 1 << 16
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/ca-bundle.pem")
_context = None


class NetError(Exception):
    """A download that failed or was refused. The message says why, without the URL: the caller says what it
    was downloading. status: the HTTP status when the server answered with anything but 200."""
    status = None


class TooLarge(NetError):
    pass


class Mismatch(NetError):
    """The content is not the file pinned by its SHA-256."""


def tls_context():
    """One verified context: the system store, else the OS CA bundles, else certifi."""
    global _context
    if _context is None:
        context = ssl.create_default_context()
        if not context.cert_store_stats().get("x509_ca"):
            candidates = list(CA_FILES)
            try:
                import certifi
                candidates.append(certifi.where())
            except ImportError:
                pass
            for path in candidates:
                try:
                    context.load_verify_locations(cafile=path)
                except OSError:
                    continue
                if context.cert_store_stats().get("x509_ca"):
                    break
        _context = context
    return _context


def check_url(url, hosts=None):
    """url when it is an allowed https URL, else NetError."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise NetError("not a valid address")
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or \
            port not in (None, 443) or not parts.hostname or parts.fragment or url.endswith("#"):
        raise NetError("only plain https:// addresses are allowed")
    if hosts is not None and parts.hostname.lower() not in hosts:
        raise NetError(f"{parts.hostname} is not an allowed host")
    return url


# The fetch process: stdlib only, it runs as `python -I -B dk_net.py --fetch` with nothing else importable.

def _check_bounds(limit, deadline, byte_range=None):
    if type(limit) is not int or not 1 <= limit <= MAX_BODY:
        raise ValueError(f"limit must be 1..{MAX_BODY}")
    if type(deadline) not in (int, float) or not 0 < deadline <= MAX_DEADLINE:
        raise ValueError(f"deadline must be in (0, {MAX_DEADLINE}] seconds")
    if byte_range is not None and not (
            isinstance(byte_range, (list, tuple)) and len(byte_range) == 2 and all(type(n) is int for n in byte_range)
            and 0 <= byte_range[0] <= byte_range[1] and byte_range[1] - byte_range[0] + 1 <= limit):
        raise ValueError("byte_range must be (first, last) with 0 <= first <= last, at most limit bytes")


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 10

    def __init__(self, hosts, allowed=True):
        # allowed: True, False or the hosts a redirect may go to
        self.hosts, self.allowed = hosts, allowed if isinstance(allowed, bool) else {h.lower() for h in allowed}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            if not self.allowed:
                raise NetError("the server redirected the request, which is not allowed here")
            check_url(newurl, self.hosts)
            if self.allowed is not True:
                check_url(newurl, self.allowed)
        except NetError:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(request, body, opener=None):
    """Write the body of request["url"] to the binary file body; returns the status dict sent to the parent."""
    hosts = set(request["hosts"]) if request["hosts"] is not None else None
    url, limit, part = check_url(request["url"], hosts), request["limit"], request.get("range")
    _check_bounds(limit, request["deadline"], part)
    deadline = time.monotonic() + request["deadline"]
    if opener is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # check_hostname and CERT_REQUIRED by default
        if "verify_flags" in request:
            context.verify_flags = ssl.VerifyFlags(request["verify_flags"])
            context.minimum_version = ssl.TLSVersion(request["minimum_version"])
            context.maximum_version = ssl.TLSVersion(request["maximum_version"])
        if request["ca"]:
            context.load_verify_locations(cadata=request["ca"])
        else:
            context.load_default_certs()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                             _Redirect(hosts, request.get("redirects", True)))
    size = 0
    with opener.open(urllib.request.Request(url, headers=request["headers"]), timeout=request["timeout"]) as r:
        check_url(r.geturl(), hosts)
        if part and (r.status == 200 or r.status == 206 and
                     r.headers.get("Content-Range", "").partition("/")[0].strip() != f"bytes {part[0]}-{part[1]}"):
            return {"kind": "range"}
        if r.status != (206 if part else 200):
            return {"kind": "http", "code": r.status}
        expected = r.headers.get("Content-Length")
        expected = int(expected) if expected and expected.isdigit() else None
        if expected is not None and expected > limit:
            return {"kind": "large"}
        read = getattr(r, "read1", r.read)
        while chunk := read(min(CHUNK, limit + 1 - size)):
            if time.monotonic() >= deadline:
                return {"kind": "deadline"}
            size += len(chunk)
            if size > limit:
                return {"kind": "large"}
            body.write(chunk)
        if expected is not None and size != expected or part and size != part[1] - part[0] + 1:
            return {"kind": "short"}
    body.flush()
    return {"kind": "ok", "size": size}


def _fetch_main():
    try:
        result = _fetch(json.loads(sys.stdin.buffer.read()), sys.stdout.buffer)
    except urllib.error.HTTPError as e:
        result = {"kind": "http", "code": e.code}
        e.close()
    except NetError as e:
        result = {"kind": "refused", "reason": str(e)}
    except Exception as e:  # the reason without the URL: OS and TLS messages name at most the host
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        result = {"kind": "network", "reason": (str(reason) or type(reason).__name__)[:200]}
    sys.stderr.write(json.dumps(result))
    return 0 if result["kind"] == "ok" else 1


# The plugin's side.

def _anchors():
    return "".join(ssl.DER_cert_to_PEM_cert(c) for c in tls_context().get_ca_certs(binary_form=True))


def _run(url, body, limit, headers, hosts, deadline, cancel, redirects, byte_range=None, progress=None):
    """Fetch url into the open binary file body in the fetch process; NetError on anything but success."""
    import dk_process
    hosts = {h.lower() for h in hosts} if hosts is not None else None
    check_url(url, hosts)
    _check_bounds(limit, deadline, byte_range)
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    poll = cancel
    if progress is not None:
        def poll():  # run_helper asks this every 0.2 s; the child writes the body straight into the file
            progress(os.fstat(body.fileno()).st_size)
            return bool(cancel and cancel())
    context = tls_context()
    request = json.dumps({"url": url, "limit": limit, "headers": headers,
                          "range": list(byte_range) if byte_range is not None else None,
                          "deadline": deadline, "timeout": min(TIMEOUT, deadline), "ca": _anchors(),
                          "verify_flags": int(context.verify_flags), "minimum_version": int(context.minimum_version),
                          "maximum_version": int(context.maximum_version),
                          "redirects": redirects if isinstance(redirects, bool) else sorted(h.lower() for h in redirects),
                          "hosts": sorted(hosts) if hosts is not None else None}).encode("utf-8")
    with tempfile.TemporaryFile() as config, tempfile.TemporaryFile() as status:
        config.write(request)
        config.seek(0)
        try:
            dk_process.run_helper([os.path.abspath(__file__), "--fetch"], deadline + 5, poll, stdin=config,
                                  stdout=body, stderr=status)
        except TimeoutError as e:
            raise NetError("the server took too long") from e
        except InterruptedError as e:
            raise NetError("cancelled") from e
        except (OSError, ValueError) as e:
            raise NetError(f"the download process could not start ({e})") from e
        status.seek(0)
        try:
            result = json.loads(status.read(16384))
            kind = result["kind"]
        except (ValueError, KeyError, TypeError) as e:
            raise NetError("the download process failed") from e
    if kind == "ok":
        return
    if kind == "large":
        raise TooLarge("larger than expected")
    if kind == "http":
        error = NetError(f"HTTP {result.get('code')}")
        error.status = result.get("code") if type(result.get("code")) is int else None
        raise error
    raise NetError({"short": "the connection dropped before the end",
                    "range": "the server did not send the requested part of the file",
                    "deadline": "the server took too long", "refused": str(result.get("reason")),
                    }.get(kind, f"could not connect ({result.get('reason', 'network error')})"))


def _disk(e):
    """A local file error as a NetError, without the path (it may hold the user's name)."""
    return NetError(f"the file could not be saved ({e.strerror or type(e).__name__})")


def get(url, limit, sha256=None, *, hosts, redirects=True, headers=None, deadline=DEADLINE, cancel=None,
        byte_range=None, progress=None):
    """The body of url (at most limit bytes). With sha256 given, any other content is refused."""
    try:
        with tempfile.TemporaryFile() as body:
            _run(url, body, limit, headers, hosts, deadline, cancel, redirects, byte_range, progress)
            body.seek(0)
            data = body.read(limit + 1)
    except OSError as e:
        raise _disk(e) from e
    if len(data) > limit:
        raise TooLarge("larger than expected")
    if sha256 and hashlib.sha256(data).hexdigest() != sha256.lower():
        raise Mismatch("not the expected file (SHA-256 differs)")
    return data


def _spool(url, folder, limit, sha256, *run):
    """(body, its SHA-256): the checked body of url in an anonymous temp file in folder, at its start."""
    body = tempfile.TemporaryFile(dir=folder if os.path.isdir(folder) else None)
    try:
        _run(url, body, limit, *run)
        body.flush()
        body.seek(0)
        digest, size = hashlib.sha256(), 0
        for block in iter(lambda: body.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
        if size > limit:
            raise TooLarge("larger than expected")
        if sha256 and digest.hexdigest() != sha256.lower():
            raise Mismatch("not the expected file (SHA-256 differs)")
        body.seek(0)
        return body, digest.hexdigest()
    except BaseException:
        body.close()
        raise


def spool(url, folder, limit, sha256=None, *, hosts, redirects=True, headers=None, deadline=DEADLINE, cancel=None,
          byte_range=None, progress=None):
    """The checked body of url as an open anonymous file in folder, read from its start; the caller closes it."""
    try:
        return _spool(url, folder, limit, sha256, headers, hosts, deadline, cancel, redirects, byte_range,
                      progress)[0]
    except OSError as e:
        raise _disk(e) from e


def download(url, dest, limit, sha256=None, *, hosts, redirects=True, headers=None, deadline=DEADLINE,
             cancel=None, mode=None, no_links=None, byte_range=None, progress=None):
    """Save url to dest (atomically, through dk_fs); returns the content's SHA-256."""
    import dk_fs
    try:
        body, digest = _spool(url, os.path.dirname(os.path.abspath(dest)), limit, sha256, headers, hosts, deadline,
                              cancel, redirects, byte_range, progress)
        with body:
            dk_fs.write_atomic(dest, body, mode, no_links=no_links)
    except OSError as e:
        raise _disk(e) from e
    return digest


if __name__ == "__main__" and sys.argv[1:] == ["--fetch"]:
    raise SystemExit(_fetch_main())
