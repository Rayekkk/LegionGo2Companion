# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGo2BrightnessFix

"""Local version metadata and TLS compatibility retained by the imported backend.
This module does not check, download or install standalone plugin updates."""

import json
import os
import ssl

CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)

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
