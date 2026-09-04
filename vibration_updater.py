# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGo-Vibe-Control

"""Update checks and downloads, shared verbatim between the LeGo plugins.

This file is kept identical in LeGoTDP and LeGo-Vibe-Control. Everything
plugin specific arrives through `Updater`'s constructor, so the module can be
copied between the two repos without producing a diff. Both plugins had grown
their own drifted copy of this logic; keeping one shape means a fix to the
trust store or the host allowlist lands in both.

The `lego_` prefix is not decoration. Before running a plugin, the loader
aliases each of its own submodules to a bare name:

    keys = [key for key in sys.modules if key.startswith("decky_loader.")]
    for key in keys:
        sys.modules[key.replace("decky_loader.", "")] = sys.modules[key]

That happens before this module is ever imported, and `import x` consults
sys.modules before sys.path - so a plugin file called `updater.py` never
loads at all. `from updater import Updater` silently returned the loader's
own Updater class and both plugins died on the constructor. The names to
stay away from are browser, enums, helpers, injector, loader, main, settings,
updater, utilities and wsrouter. (`settings` is the exception we want: that
alias is how every plugin reaches SettingsManager.)

Nothing here imports `decky`, so the module can be exercised by the test
suites without the loader present.
"""

import json
import os
import pwd
import re
import ssl
import urllib.parse
import urllib.request

# Only these hosts may be contacted. The plugins run as root, so an
# unrestricted URL would be an arbitrary-fetch primitive - and in LeGoTDP's
# case the fetched file is then executed.
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

# Refuse absurd downloads. Release archives and the RyzenAdj tarball are a
# few MB at most.
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

# Decky runs plugins inside a PyInstaller-frozen PluginLoader whose OpenSSL has
# its CA paths baked in from the build machine. They do not exist on the
# device, so ssl.create_default_context() comes back with an empty trust store
# and every request dies with CERTIFICATE_VERIFY_FAILED. That is what the old
# CERT_NONE was working around. Point the context at a real bundle instead.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


# ── Pure helpers ───────────────────────────────────────────────────────────────

def checked_url(url: str) -> str:
    """Reject anything that is not an https URL on a known GitHub host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-https URL scheme '{parsed.scheme}'")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"refusing download from untrusted host '{parsed.hostname}'")
    return url


def version_tuple(text: str) -> tuple[int, ...]:
    """Numeric components of a version string, for ordering comparisons."""
    return tuple(int(part) for part in re.findall(r"\d+", text))


def real_user() -> pwd.struct_passwd | None:
    """The plugins run as root, so '~' is /root. Find the desktop user."""
    return next(
        (p for p in sorted(pwd.getpwall(), key=lambda p: p.pw_uid)
         if p.pw_uid >= 1000 and os.path.isdir(p.pw_dir)),
        None,
    )


def xdg_download_dir(home_dir: str) -> str:
    try:
        with open(os.path.join(home_dir, ".config", "user-dirs.dirs")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip('"')
                    return value.replace("$HOME", home_dir)
    except OSError:
        pass
    return os.path.join(home_dir, "Downloads")


# ── Updater ────────────────────────────────────────────────────────────────────

class Updater:
    """Checks GitHub releases and downloads an asset for the user to install.

    The plugin supplies its own release URL, User-Agent, log prefix, directory
    and logger; everything else is common to both.
    """

    def __init__(self, *, releases_url: str, user_agent: str, log_prefix: str,
                 plugin_dir: str, logger):
        self.releases_url = releases_url
        self.user_agent = user_agent
        self.log_prefix = log_prefix
        self.plugin_dir = plugin_dir
        self.logger = logger
        self._ssl_ctx: ssl.SSLContext | None = None

    # ---- logging ----------------------------------------------------- #

    def _info(self, message: str) -> None:
        self.logger.info(f"{self.log_prefix} {message}")

    def _warning(self, message: str) -> None:
        self.logger.warning(f"{self.log_prefix} {message}")

    def _error(self, message: str) -> None:
        self.logger.error(f"{self.log_prefix} {message}")

    # ---- TLS ---------------------------------------------------------- #

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

    # ---- HTTP --------------------------------------------------------- #

    def open_url(self, url: str, timeout: int, headers: dict | None = None):
        """urlopen with certificate verification left on and the host checked."""
        request = urllib.request.Request(
            checked_url(url),
            headers=headers or {"User-Agent": self.user_agent},
        )
        return urllib.request.urlopen(request, context=self.ssl_context(), timeout=timeout)

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

    # ---- RPC bodies ---------------------------------------------------- #

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

    def check(self) -> dict:
        """Ask GitHub for the latest release. Never raises."""
        current = self.plugin_version()
        try:
            with self.open_url(self.releases_url, timeout=10, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": self.user_agent,
            }) as resp:
                data = json.loads(resp.read(MAX_DOWNLOAD_BYTES))
            tag = str(data.get("tag_name", ""))
            if not tag:
                return {"current_version": current,
                        "error": data.get("message", "Unexpected GitHub API response")}
            latest = tag.lstrip("vV").split("-")[0]
            current_release = current.split("-")[0]
            latest_t, current_t = version_tuple(latest), version_tuple(current_release)
            # A tag that is not purely numeric leaves one tuple empty; fall back
            # to a string comparison rather than raising.
            available = (latest_t > current_t) if (latest_t and current_t) \
                else (latest != current_release)
            asset = next((a for a in data.get("assets", [])
                          if str(a.get("name", "")).endswith(".zip")), None)
            return {
                "current_version":  current,
                "latest_version":   latest,
                "update_available": available,
                "download_url":     asset.get("browser_download_url") if asset else None,
                "asset_name":       asset.get("name") if asset else None,
            }
        except Exception as e:
            self._error(f"check_for_updates: {e}")
            return {"current_version": current, "error": str(e)}

    def download(self, download_url: str, asset_name: str) -> dict:
        """Fetch a release asset into the desktop user's download directory."""
        dest = None
        try:
            user = real_user()
            downloads_dir = xdg_download_dir(user.pw_dir) if user else "/home/deck/Downloads"
            created_dir = not os.path.isdir(downloads_dir)
            os.makedirs(downloads_dir, exist_ok=True)
            # If we had to create it, it is owned by root and the user would not
            # be able to manage their own download directory.
            if user and created_dir:
                os.chown(downloads_dir, user.pw_uid, user.pw_gid)
            dest = os.path.join(downloads_dir, os.path.basename(asset_name))
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass

            with open(dest, "wb") as f:
                written = self.download_to(download_url, f, timeout=60)

            # Written as root, so hand it back to the desktop user - otherwise
            # they cannot move or delete their own download.
            if user:
                os.chown(dest, user.pw_uid, user.pw_gid)
            os.chmod(dest, 0o644)

            self._info(f"update downloaded to {dest} ({written} bytes)")
            return {"success": True, "path": dest}
        except Exception as e:
            self._error(f"perform_update: {e}")
            # Never leave a truncated or oversized file behind.
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
            return {"success": False, "error": str(e)}
