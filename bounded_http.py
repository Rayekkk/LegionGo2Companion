# SPDX-License-Identifier: BSD-3-Clause
"""GitHub downloads in an owned subprocess, including DNS and TLS deadlines.

The worker only reads HTTPS and writes an anonymous temporary body. It cannot
install files or touch hardware. The caller still validates metadata, hashes,
archives and the final publication. Closing/cancelling it kills and reaps only
this request's process, so an OS resolver cannot retain Decky's executor.
"""
import json
import io
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_HOSTS = frozenset({"api.github.com", "github.com", "codeload.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
ASSET_HOSTS = frozenset({"objects.githubusercontent.com", "release-assets.githubusercontent.com"})
MAX_BODY = 48 * 1024 * 1024
MAX_REQUEST = 2 * 1024 * 1024
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)
_tls_context = None


def tls_context():
    """One verified TLS context for every GitHub download in the plugin.

    The frozen loader can start with an empty default store; then the OS bundle
    (it gets security updates) wins over certifi. Verification is never turned
    off: this runs as root and what comes back is installed or executed.
    """
    global _tls_context
    if _tls_context is not None:
        return _tls_context
    from diagnostics import event  # not at import time: the fetch worker runs isolated
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        candidates = list(CA_BUNDLES)
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass
        for path in candidates:
            try:
                if path and os.path.exists(path):
                    context.load_verify_locations(cafile=path)
            except OSError as exc:
                event('updates', 'tls_bundle_failed', level='warning', error=exc, repeat=60)
                continue
            if context.cert_store_stats().get("x509_ca"):
                event('updates', 'tls_ready', scope='fallback_trust_store',
                      count=context.cert_store_stats()['x509_ca'])
                break
        else:
            event('updates', 'tls_unavailable', level='error')
    _tls_context = context
    return context


def checked_url(url, initial_url, policy):
    if policy not in ("metadata", "archive", "helper"):
        raise ValueError("Unknown GitHub request policy.")
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid release URL.")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or
            parsed.port not in (None, 443) or (policy != "helper" and parsed.fragment)):
        raise ValueError("Release downloads require a verified HTTPS URL.")
    if policy == "helper" and (parsed.hostname or "").lower() in GITHUB_HOSTS:
        return url
    if policy != "helper" and (url == initial_url or
            policy == "archive" and parsed.hostname in ASSET_HOSTS):
        return url
    raise ValueError("GitHub redirected the request outside the allowed release hosts.")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, initial_url, policy):
        self.initial_url, self.policy = initial_url, policy
        self.max_redirections = 10 if policy == "helper" else 5
        self.max_repeats = 4 if policy == "helper" else 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            checked_url(newurl, self.initial_url, self.policy)
        except Exception:
            if fp is not None: fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Response:
    def __init__(self, body, url):
        self.body, self.url, self.status = body, url, 200

    def geturl(self): return self.url
    def read(self, size=-1): return self.body.read(size)
    def read1(self, size=-1): return self.body.read(size)
    def close(self): self.body.close()
    @property
    def closed(self): return self.body.closed
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def _check_bounds(limit, timeout, total_seconds):
    if (type(limit) is not int or not 1 <= limit <= MAX_BODY or
            type(total_seconds) not in (int, float) or not 0 < total_seconds <= 120 or
            type(timeout) not in (int, float) or not 0 < timeout <= 30):
        raise ValueError("Invalid GitHub download bounds.")


def _status_error(url, code):
    # Status-only exception: never retain a response backing object. An empty
    # stream also keeps close() well-defined on Python versions where fp=None
    # does not initialize HTTPError's inherited file wrapper completely.
    error = urllib.error.HTTPError(url, code, "GitHub request failed", {}, io.BytesIO())
    error.close()
    return error


def open_http(url, *, headers, context, timeout, limit, total_seconds, policy, cancel=None):
    """Return an already downloaded, bounded response or raise before publication.

    Anonymous files avoid buffering a release archive in the parent's RAM.
    The input contains public trust anchors, never private key material.
    """
    checked_url(url, url, policy)
    _check_bounds(limit, timeout, total_seconds)
    deadline = time.monotonic() + total_seconds

    def check():
        if cancel is not None and cancel():
            raise InterruptedError("The GitHub request was cancelled.")
        if time.monotonic() >= deadline:
            raise TimeoutError("The GitHub request took too long. Try again later.")

    check()
    anchors = "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in context.get_ca_certs(binary_form=True))
    request = json.dumps({"url": url, "headers": headers, "ca": anchors, "policy": policy,
        "verify_flags": int(context.verify_flags), "minimum_version": int(context.minimum_version),
        "maximum_version": int(context.maximum_version),
        "timeout": timeout, "limit": limit, "total_seconds": total_seconds}).encode("utf-8")
    if len(request) > MAX_REQUEST: raise ValueError("The HTTPS trust configuration is too large.")
    body, worker = None, None
    try:
        body = tempfile.TemporaryFile("w+b")
        with tempfile.TemporaryFile("w+b") as configuration, tempfile.TemporaryFile("w+b") as status:
            configuration.write(request); configuration.seek(0)
            from system_process import NO_WINDOW, PYTHON, system_env
            check()
            worker = subprocess.Popen([PYTHON, "-I", "-B", str(Path(__file__).resolve()), "--fetch"],
                stdin=configuration, stdout=body, stderr=status, env=system_env(),
                **NO_WINDOW)
            while True:
                check()
                try: worker.wait(timeout=min(.1, max(.001, deadline-time.monotonic())))
                except subprocess.TimeoutExpired: continue
                check()
                break
            status.seek(0)
            raw = status.read(16385)
            try:
                result = json.loads(raw) if len(raw) <= 16384 else None
                if not isinstance(result, dict): raise ValueError("Invalid helper result")
            except (ValueError, UnicodeError) as exc:
                raise urllib.error.URLError("The HTTPS helper returned an invalid result.") from exc
            if result.get("kind") == "http" and type(result.get("code")) is int:
                raise _status_error(url, result["code"])
            if result.get("kind") == "invalid":
                raise ValueError("The GitHub request failed URL or response size validation.")
            if worker.returncode != 0 or result.get("kind") != "ok":
                reason = result.get("reason", "network")
                if not isinstance(reason, str) or not reason.isidentifier() or len(reason) > 64: reason = "network"
                code = result.get("errno")
                detail = f"{reason}, errno {code}" if type(code) is int else reason
                raise urllib.error.URLError(f"The HTTPS request could not complete securely ({detail}).")
            checked_url(result.get("url"), url, policy)
            if body.seek(0, os.SEEK_END) > limit: raise ValueError("The GitHub response exceeded the size limit.")
            body.seek(0)
            response, body = Response(body, result["url"]), None
            return response
    finally:
        try:
            if worker is not None:
                if worker.poll() is None: worker.kill()
                worker.wait(timeout=3)
        finally:
            if body is not None: body.close()


def _fetch(request, body):
    url, policy = request["url"], request["policy"]
    checked_url(url, url, policy)
    limit, timeout, total_seconds = request["limit"], request["timeout"], request["total_seconds"]
    _check_bounds(limit, timeout, total_seconds)  # The worker trusts no caller.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_flags = request["verify_flags"]
    context.minimum_version = ssl.TLSVersion(request["minimum_version"])
    context.maximum_version = ssl.TLSVersion(request["maximum_version"])
    if request["ca"]: context.load_verify_locations(cadata=request["ca"])
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _Redirect(url, policy))
    deadline, size = time.monotonic()+total_seconds, 0
    with opener.open(urllib.request.Request(url, headers=request["headers"]), timeout=timeout) as response:
        final = checked_url(response.geturl(), url, policy)
        if response.status != 200:
            raise _status_error(url, response.status)
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() >= deadline: raise TimeoutError("HTTP deadline expired")
            chunk = reader(min(65536, limit+1-size))
            if time.monotonic() >= deadline: raise TimeoutError("HTTP deadline expired")
            if not chunk: break
            size += len(chunk)
            if size > limit: raise ValueError("Response size limit exceeded")
            body.write(chunk)
    body.flush()
    return {"kind": "ok", "url": final}


def _main():
    if sys.argv[1:] != ["--fetch"]: return 2
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST+1)
        if len(raw) > MAX_REQUEST: raise ValueError("Request too large")
        result = _fetch(json.loads(raw), sys.stdout.buffer)
        code = 0
    except urllib.error.HTTPError as exc:
        result, code = {"kind": "http", "code": exc.code}, 1
        exc.close()
    except ValueError:
        result, code = {"kind": "invalid"}, 1
    except Exception as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        result, code = {"kind": "network", "reason": type(reason).__name__, "errno": getattr(reason, "errno", None)}, 1
    sys.stderr.write(json.dumps(result))
    return code


if __name__ == "__main__": raise SystemExit(_main())
