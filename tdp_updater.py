# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

"""TLS, bounded downloads and version metadata for the pinned RyzenAdj helper.
Standalone plugin update checks and archive downloads are intentionally absent."""

import json
import os
import ssl

import urllib.parse
import urllib.request

ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)

def checked_url(url: str) -> str:
    """Reject anything that is not an https URL on a known GitHub host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-https URL scheme '{parsed.scheme}'")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"refusing download from untrusted host '{parsed.hostname}'")
    return url

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
            self._info("TLS: using the default trust store")
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
                    self._info(
                        f"TLS: default store was empty, loaded CA bundle {path} "
                        f"({ctx.cert_store_stats()['x509_ca']} certs)")
                    self._ssl_ctx = ctx
                    return ctx
            except OSError as exc:
                self._warning(f"TLS: cannot load {path}: {exc}")

        # Verification stays on. Failing loudly beats silently trusting
        # anything, since this runs as root and what comes back is installed
        # or executed.
        self._error("TLS: no usable CA bundle found, downloads will fail to verify")
        self._ssl_ctx = ctx
        return ctx

    def open_url(self, url: str, timeout: int, headers: dict | None = None):
        """urlopen with certificate verification left on and the host checked."""
        request = urllib.request.Request(
            checked_url(url),
            headers=headers or {"User-Agent": self.user_agent},
        )
        response = urllib.request.urlopen(
            request, context=self.ssl_context(), timeout=timeout)
        try:
            # urllib follows redirects automatically. Validate the final URL as
            # well, otherwise an allowed host could redirect the root process
            # to a host the initial check would have rejected.
            checked_url(response.geturl())
        except Exception:
            response.close()
            raise
        return response

    def download_to(self, url: str, out, timeout: int) -> int:
        """Stream a URL into a file object, aborting past the size ceiling.

        Returns the number of bytes written. The caller is responsible for
        removing a partial file, since a truncated archive that looks complete
        is worse than no archive at all.
        """
        written = 0
        with self.open_url(url, timeout=timeout) as resp:
            while True:
                chunk = resp.read(64 * 1024)
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
