# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

"""TLS, bounded downloads and version metadata for the pinned RyzenAdj helper.
Standalone plugin update checks and archive downloads are intentionally absent."""

import json
import os
import ssl
import time
from diagnostics import event
from bounded_http import GITHUB_HOSTS, checked_url as _http_url, open_http

import urllib.parse
import urllib.request

ALLOWED_HOSTS = GITHUB_HOSTS

MAX_DOWNLOAD_BYTES = 48 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120
READ_TIMEOUT_SECONDS = 3

CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)

def checked_url(url: str) -> str:
    """Reject anything that is not an https URL on a known GitHub host."""
    return _http_url(url, url, "helper")

class _HelperRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Check each hop before urllib opens it, including an intermediate
        # downgrade which a final-response check would see only afterwards.
        try:
            checked_url(newurl)
        except Exception:
            # urllib only closes the preceding response after this hook returns.
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class Updater:
    """Internal backend helper; no standalone plugin update API."""

    def __init__(self, *, user_agent: str, log_prefix: str,
                 plugin_dir: str, logger):
        self.user_agent = user_agent
        self.log_prefix = log_prefix
        self.plugin_dir = plugin_dir
        self.logger = logger
        self._ssl_ctx: ssl.SSLContext | None = None

    def _info(self, message: str) -> None:
        self.logger.info(f"{self.log_prefix} {message}")

    def _warning(self, message: str) -> None:
        self.logger.warning(f"{self.log_prefix} {message}")

    def _error(self, message: str) -> None:
        self.logger.error(f"{self.log_prefix} {message}")

    def ssl_context(self) -> ssl.SSLContext:
        if self._ssl_ctx is not None:
            return self._ssl_ctx

        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509_ca"):
            event('updates', 'tls_ready', scope='default_trust_store')
            self._ssl_ctx = ctx
            return ctx

        # Prefer the OS bundle (it gets security updates) over the copy of
        # certifi the frozen loader unpacks into a temp dir that changes on
        # every restart.
        candidates = list(CA_BUNDLES)
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass

        for path in candidates:
            try:
                if not path or not os.path.exists(path):
                    continue
                ctx.load_verify_locations(cafile=path)
                if ctx.cert_store_stats().get("x509_ca"):
                    event('updates', 'tls_ready', scope='fallback_trust_store', count=ctx.cert_store_stats()['x509_ca'])
                    self._ssl_ctx = ctx
                    return ctx
            except OSError as exc:
                event('updates', 'tls_bundle_failed', level='warning', error=exc, repeat=60)

        # Verification stays on. Failing loudly beats silently trusting
        # anything, since this runs as root and what comes back is installed
        # or executed.
        event('updates', 'tls_unavailable', level='error')
        self._ssl_ctx = ctx
        return ctx

    def open_url(self, url: str, timeout: int, headers: dict | None = None, *, cancel=None, deadline=None):
        """Verified HTTPS with a parent deadline that also covers OS DNS."""
        checked_url(url)
        remaining = MAX_DOWNLOAD_SECONDS if deadline is None else deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError("RyzenAdj download exceeded its total time limit")
        response = open_http(url, headers=headers or {"User-Agent": self.user_agent},
            context=self.ssl_context(), timeout=timeout, limit=MAX_DOWNLOAD_BYTES,
            total_seconds=min(MAX_DOWNLOAD_SECONDS, remaining), policy="helper", cancel=cancel)
        try:
            # Retain a final-response check alongside the pre-request hop guard.
            checked_url(response.geturl())
        except Exception:
            response.close()
            raise
        return response

    def download_to(self, url: str, out, timeout: int, cancel=None) -> int:
        """Stream a URL with size/time ceilings and cooperative cancellation.

        Returns the number of bytes written. The caller is responsible for
        removing a partial file, since a truncated archive that looks complete
        is worse than no archive at all.
        """
        written = 0
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        def check():
            if cancel is not None and cancel():
                raise InterruptedError("RyzenAdj download was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("RyzenAdj download exceeded its total time limit")
        check()
        with self.open_url(url, timeout=min(timeout, READ_TIMEOUT_SECONDS), cancel=cancel, deadline=deadline) as resp:
            reader = getattr(resp, "read1", resp.read)
            while True:
                check()
                chunk = reader(64 * 1024)
                check()
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download exceeded the size limit")
                out.write(chunk)
        return written

    def plugin_version(self) -> str:
        """The installed version, as the loader itself understands it.

        DECKY_PLUGIN_VERSION is authoritative: PluginWrapper takes the version
        from package.json, never from plugin.json, so that is the number Decky
        shows in its own plugin list. Reading it here means the panel and the
        loader can never disagree about what is installed.

        Falls back to parsing plugin.json, which is what keeps this module
        importable by the test suites with no loader in the environment.
        """
        version = os.environ.get("DECKY_PLUGIN_VERSION", "")
        if version:
            return version
        try:
            with open(os.path.join(self.plugin_dir, "plugin.json")) as f:
                return json.load(f).get("version", "0.0.0")
        except (OSError, ValueError):
            return "0.0.0"
