# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/backend/dk_update.py): change it there, then `kit sync`. Needs dk_net.py, dk_fs.py
# and dk_process.py.

"""The plugin's own update: GitHub's latest release, checked, and its ZIP saved to the user's Downloads folder.

Nothing is installed or run: the user installs the ZIP (Decky -> Developer -> Install Plugin from ZIP).

Sources: LegionGo2Companion/companion_updates.py (stable X.Y.Z only, release and asset bound to the repository,
size and GitHub's digest, the ZIP read entry by entry with real sizes and CRCs, manifests, XDG Downloads walked by
descriptor as root and created for the user, a verified file reused, check cache, a fresh check before the
download, close() on unload), DeckyReshade/FSR4Injector *_updates.py (plugin-specific required files, messages
for rate limits, connections and a changed release), DeckyVibranceHDR/lego_updater.py (a second download refused at
once, logs without the user's paths), DeckyEQ/deckyeq/updates.py (a release asking for other plugin flags, e.g.
root, refused; the asset's uploader; close() that never waits for a write in progress), Ayaneo3Companion/
lego_updater.py (redirects of the ZIP only to the asset hosts, the missing required file named),
LegionGo2Companion again (home bound to the console user's uid, .config read without following a link, the
saved ZIP always the user's, diagnostics of each check and download with the stage that failed).

Guarantees:
- Only a published stable release (not a draft or prerelease) tagged X.Y.Z or vX.Y.Z (parts of at most 6 digits)
  whose page is this repository's, with exactly one asset named <folder>-<version>.zip whose address is this
  repository's download address for that tag, in state "uploaded", 0 < size <= max_download and a sha256 digest;
  with `uploader` (e.g. "github-actions[bot]", the release workflow) only an asset that account uploaded.
  Anything else is never downloaded; for a newer release the answer says why in "message".
- Metadata comes from api.github.com only, without redirects (1 MiB, 20 s); the ZIP from github.com, redirected
  only to GitHub's asset hosts, exactly the size and SHA-256 GitHub states (dk_net: TLS verified,
  separate process, 120 s). close() cancels both.
- The ZIP is checked before it is kept: at most max_members entries; names without "\\", ":", control characters
  or empty, "." and ".." parts, all inside one folder named `folder`, nothing loose next to it; only files and
  folders (no symlinks); no duplicates or file/folder clashes, ignoring case; no encryption, only stored or
  deflated; every entry read to its end (CRC) and exactly its declared size, max_expanded bytes in all; the
  `required` files present and not empty (the message names a missing one); plugin.json's name = plugin_name, its entry points the default ones and
  its version the release's, package.json's version too (and its name when package_name is given); with `flags`
  (e.g. [] for a plugin that must never run as root) plugin.json's flags exactly those.
- Saved to the user's XDG download folder (user-dirs.dirs, a folder inside home only, else ~/Downloads) with
  dk_fs.write_atomic: atomic, fsynced, mode 0644, nothing left behind on failure. As root home must not be root's
  (with `uid`: must be that user's), .config and every folder below home are opened without following links and
  must belong to home's owner, a missing one is created 0700 for them, and the file is theirs even where a file of
  someone else had the name. A file of the same name is replaced only by a verified ZIP; the very same verified ZIP is
  reused without downloading.
- 404 means no published release (also a private repository), 403/429 GitHub's rate limit; messages never carry
  a path or a traceback, and the log names versions and sizes, never paths.
- check() answers from a cache (60 s, 20 s after an error, 2 s for a click); download() checks again first and
  refuses when the latest release is not the version the user saw. One download at a time: a second is refused
  at once, never queued. After close() nothing new is saved, and close() waits only for a rename in progress,
  never for the network or for a file being written and synced (Decky's unload must not stall on a slow card).
- on_event(name, **fields), when given, hears "check" (outcome ok/no_release/failed, latest, duration_ms) and
  "download" (outcome saved/reused/failed, version, stage of a failure: release_check, network, verification,
  destination; duration_ms), with no path or error text in the fields; an exception in it is ignored.
- Answers follow DeckyKit D2. check(): {"ok", "current_version", "update_available", "download_available",
  "latest_version"?, "asset_name"?, "size"?, "no_release"?, "message"?, "error"?, "checked_at"}; download():
  {"ok", "path", "version", "sha256"} or {"ok": False, "error"}.

Deliberately not here: installing (Decky does it when the user picks the ZIP), pre-releases, hosts other than
GitHub, release notes, Windows as a real target (it works there for the plugins' test suites), streaming the ZIP
to disk (FSR4Injector's updater did: a release ZIP is at most max_download, 32 MiB by default and about 1 MiB in
practice, so it is checked in memory).
"""

import contextlib
import hashlib
import io
import os
import re
import stat
import threading
import time
import zipfile

import dk_fs
import dk_net

API_HOSTS = frozenset({"api.github.com"})
ASSET_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
REDIRECT_HOSTS = ASSET_HOSTS - {"github.com"}  # the release URL leads to GitHub's asset storage, never elsewhere
MAX_METADATA = 1 << 20
METADATA_SECONDS = 20
DOWNLOAD_SECONDS = 120
CACHE_SECONDS, ERROR_CACHE_SECONDS, CLICK_CACHE_SECONDS = 60, 20, 2
REQUIRED = ("main.py", "plugin.json", "package.json", "dist/index.js")
CANCELLED = "The update was cancelled because the plugin is stopping."
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")
_XDG = re.compile(r'\s*XDG_DOWNLOAD_DIR="([^"\r\n]*)"\s*')


class UpdateError(Exception):
    """A refusal; the message is shown to the user."""


def version_tuple(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateError("The version must be a stable X.Y.Z number.")
    return tuple(int(part) for part in value.split("."))


def _download_parts(text, home):
    """XDG_DOWNLOAD_DIR from user-dirs.dirs as folder names below home, or None."""
    for line in text.splitlines():
        match = _XDG.fullmatch(line)
        if not match:
            continue
        path = match.group(1)
        if path.startswith("$HOME/"):
            rel = path[6:]
        elif path.startswith(home.rstrip("/") + "/"):
            rel = path[len(home.rstrip("/")) + 1:]
        else:
            return None  # outside home: never written to
        parts = rel.split("/")
        if any(c in rel for c in "$`\\\x00") or any(p in ("", ".", "..") for p in parts):
            return None
        return parts
    return None


def downloads_dir(home):
    """The user's download folder: XDG_DOWNLOAD_DIR inside home (a desktop in another language names it
    differently, e.g. ~/Pobrane), else ~/Downloads."""
    try:
        text = dk_fs.read_bytes(os.path.join(home, ".config", "user-dirs.dirs"), 8192).decode("utf-8")
    except (OSError, ValueError):  # missing, unreadable, too long or not UTF-8: the default
        text = ""
    return os.path.join(home, *(_download_parts(text, home) or ["Downloads"]))


class Updates:
    def __init__(self, *, repo, folder, plugin_name, current, home, package_name=None, required=REQUIRED,
                 max_download=32 << 20, max_expanded=128 << 20, max_members=1024, logger=None, flags=None,
                 uploader=None, uid=None, on_event=None):
        """repo: "owner/name"; folder: the folder inside the ZIP (and its name: <folder>-<version>.zip);
        plugin_name: plugin.json's name; current: the installed version; home: the user's home folder; uid: the
        user home must belong to (as root); on_event(name, **fields): diagnostics (see the module's guarantees)."""
        self.repo, self.folder, self.plugin_name, self.package_name = repo, folder, plugin_name, package_name
        self.flags, self.uploader, self.uid, self.on_event = (None if flags is None else list(flags)), uploader, uid, on_event
        self.current, self.home, self.required, self.logger = current, home, tuple(required), logger
        self.max_download, self.max_expanded, self.max_members = max_download, max_expanded, max_members
        self.api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        self.release_base = f"https://github.com/{repo}/releases"
        self._lock = threading.Lock()
        self._publishing = threading.Lock()
        self._closed = threading.Event()
        self._downloading = False
        self._cached, self._cached_at, self._release = None, 0.0, None

    def close(self):
        """Stop: running requests are cancelled and nothing is saved afterwards. Waits only for a file being
        renamed into place, never for the network or a write."""
        with self._publishing:
            self._closed.set()

    def _log(self, text, warning=False):
        if self.logger:
            (self.logger.warning if warning else self.logger.info)(f"plugin update: {text}")

    def _event(self, name, started, **fields):
        if self.on_event:
            try:
                self.on_event(name, duration_ms=round((time.monotonic() - started) * 1000, 1), **fields)
            except Exception:
                pass  # diagnostics never change an update's answer

    def _alive(self):
        if self._closed.is_set():
            raise UpdateError(CANCELLED)

    @contextlib.contextmanager
    def _commit(self):
        """The rename that publishes the ZIP, never after close()."""
        with self._publishing:
            self._alive()
            yield

    # ---- the release -------------------------------------------------------------------------------------------

    def _parse(self, data, current):
        tag = data.get("tag_name")
        if not isinstance(tag, str):
            raise UpdateError("GitHub returned no release tag.")
        version = tag[1:] if tag.startswith("v") else tag
        available = version_tuple(version) > version_tuple(current)
        if data.get("draft") is not False or data.get("prerelease") is not False:
            raise UpdateError("Only published stable releases can be downloaded.")
        if data.get("html_url") != f"{self.release_base}/tag/{tag}":
            raise UpdateError("The release does not belong to this plugin's repository.")
        view = {"ok": True, "current_version": current, "latest_version": version, "update_available": available,
                "download_available": False}
        name = f"{self.folder}-{version}.zip"
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("GitHub returned unreadable release assets.")
        matching = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
        if len(matching) != 1:
            if available:
                view["message"] = f"The release has no single {name} yet."
            return view, None
        asset, url = matching[0], f"{self.release_base}/download/{tag}/{name}"
        size, digest = asset.get("size"), asset.get("digest")
        if (asset.get("browser_download_url") != url or asset.get("state") != "uploaded" or type(size) is not int
                or not 0 < size <= self.max_download or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            if available:
                view["message"] = "The release ZIP has no trusted size, address or SHA-256 yet."
            return view, None
        uploader = asset.get("uploader")
        if self.uploader is not None and (not isinstance(uploader, dict) or uploader.get("login") != self.uploader):
            if available:
                view["message"] = "The release ZIP was not uploaded by the release workflow."
            return view, None
        view.update(asset_name=name, size=size, download_available=available)
        return view, {"version": version, "name": name, "url": url, "size": size,
                      "sha256": _DIGEST.fullmatch(digest).group(1).lower()}

    def _fetch_release(self):
        try:
            raw = dk_net.get(self.api_url, MAX_METADATA, hosts=API_HOSTS, redirects=False,
                             headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                             deadline=METADATA_SECONDS, cancel=self._closed.is_set)
        except dk_net.NetError as e:
            if e.status == 404:
                return None
            self._alive()
            raise UpdateError("GitHub's request limit was reached. Try again later." if e.status in (403, 429)
                              else f"GitHub could not check releases (HTTP {e.status}). Try again later." if e.status
                              else "Could not connect securely to GitHub. Check the connection and try again.") from e
        try:
            return dk_fs.parse_json(raw, dk_fs.require_object)
        except ValueError as e:
            raise UpdateError("GitHub returned unreadable release data.") from e

    def _check_locked(self, click=False):
        ttl = CLICK_CACHE_SECONDS if click else CACHE_SECONDS if (self._cached or {}).get("ok") else ERROR_CACHE_SECONDS
        if self._cached is not None and time.monotonic() - self._cached_at < ttl and not self._closed.is_set():
            return dict(self._cached)
        self._release = None
        started = time.monotonic()
        try:
            self._alive()
            version_tuple(self.current)
            data = self._fetch_release()
            if data is None:
                view = {"ok": True, "current_version": self.current, "update_available": False,
                        "download_available": False, "no_release": True,
                        "message": "No release has been published yet (or the repository is private)."}
            else:
                view, self._release = self._parse(data, self.current)
            self._log(f"latest {view.get('latest_version', 'none')}, installed {self.current}")
            self._event("check", started, outcome="no_release" if view.get("no_release") else "ok",
                        latest=view.get("latest_version"))
        except UpdateError as e:
            self._log(str(e), warning=True)
            view = {"ok": False, "current_version": self.current, "update_available": False,
                    "download_available": False, "error": str(e)}
            self._event("check", started, outcome="failed")
        view["checked_at"] = time.time()
        self._cached, self._cached_at = dict(view), time.monotonic()
        return view

    def check(self, click=False):
        """The latest release as the panel shows it (cached; click: the user pressed Check)."""
        with self._lock:
            return self._check_locked(click)

    # ---- the ZIP -----------------------------------------------------------------------------------------------

    def validate(self, data, version):
        """UpdateError unless data is this plugin's ZIP at version, safe to unpack."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self._validate(archive, version)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, ValueError, EOFError, OSError) as e:
            if isinstance(e, UpdateError):
                raise
            raise UpdateError(f"The release ZIP is not a complete {self.plugin_name} package.") from e

    def _validate(self, archive, version):
        members = archive.infolist()
        if not members or len(members) > self.max_members:
            raise UpdateError("The release ZIP is empty or has too many entries.")
        seen, files, total = set(), set(), 0
        for m in members:
            name, mode = m.filename, m.external_attr >> 16
            parts = name.rstrip("/").split("/")
            if (not name or len(name) > 1024 or "\\" in name or ":" in name or m.orig_filename != name
                    or any(ord(c) < 32 or ord(c) == 127 for c in name) or any(p in ("", ".", "..") for p in parts)
                    or parts[0] != self.folder or (len(parts) == 1 and not m.is_dir())
                    or name.casefold().rstrip("/") in seen or m.flag_bits & 1
                    or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
                    or m.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise UpdateError("The release ZIP contains an unsafe or unsupported entry.")
            seen.add(name.casefold().rstrip("/"))
            if not m.is_dir():
                files.add(name.casefold())
            total += m.file_size
            if total > self.max_expanded:
                raise UpdateError("The release ZIP expands beyond the size limit.")
        for name in seen:
            parts = name.split("/")
            if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                raise UpdateError("The release ZIP contains a file and a folder of the same name.")
        for m in members:  # every byte read once: the CRC and the real size, nothing written anywhere
            count = 0
            with archive.open(m) as entry:
                while chunk := entry.read(1 << 16):
                    self._alive()
                    count += len(chunk)
                    if count > m.file_size:
                        break
            if count != m.file_size:
                raise UpdateError("The release ZIP has an entry of the wrong size.")
        for required in self.required:
            try:
                info = archive.getinfo(f"{self.folder}/{required}")
            except KeyError:
                info = None
            if info is None or info.is_dir() or info.file_size == 0:
                raise UpdateError(f"The release ZIP is missing a required plugin file: {required}.")
        for manifest in ("plugin.json", "package.json"):
            info = archive.getinfo(f"{self.folder}/{manifest}")
            if info.file_size > 65536:
                raise UpdateError("The release manifest is too large.")
            value = dk_fs.parse_json(archive.read(info), dk_fs.require_object)
            if value.get("version") != version:
                raise UpdateError("The release ZIP's version does not match the release.")
            if manifest == "plugin.json" and (value.get("name") != self.plugin_name
                                              or value.get("main", "main.py") != "main.py"
                                              or value.get("bin", "dist/index.js") != "dist/index.js"
                                              or self.flags is not None and value.get("flags", []) != self.flags):
                raise UpdateError("The release ZIP contains a different plugin or entry point.")
            if manifest == "package.json" and self.package_name and value.get("name") != self.package_name:
                raise UpdateError("The release ZIP contains a different package.")

    # ---- the Downloads folder ----------------------------------------------------------------------------------

    def _open_downloads_as_root(self):
        """(fd, path) of the download folder, walked from home without following links; every folder must belong
        to home's owner, and a missing one is created 0700 for them."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        home = os.path.realpath(self.home)  # links above home are the system's (/home -> /var/home)
        home_fd = os.open(home, flags)
        try:
            owner = os.fstat(home_fd)
            if owner.st_uid == 0 or self.uid is not None and owner.st_uid != self.uid:
                raise UpdateError("The user's home folder could not be verified.")
            uid, gid = owner.st_uid, owner.st_gid
            parts = _download_parts(self._user_dirs(home_fd, uid), home) or ["Downloads"]
        except BaseException:
            os.close(home_fd)
            raise

        def owned(fd):
            if os.fstat(fd).st_uid != uid:
                raise UpdateError("The Downloads folder does not belong to the user.")
            self._alive()

        fd = dk_fs.open_directory(parts, home_fd, create=lambda part: (0o700, uid, gid), check=owned)
        return fd, os.path.join(self.home, *parts), (uid, gid)

    @staticmethod
    def _user_dirs(home_fd, uid):
        """user-dirs.dirs read through home's descriptor, never through a link (.config included) and only when the
        user owns both; "" otherwise (the default Downloads)."""
        try:
            config = dk_fs.open_directory([".config"], os.dup(home_fd))
        except OSError:
            return ""
        try:
            if os.fstat(config).st_uid != uid:
                return ""
            file = os.open("user-dirs.dirs", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                           dir_fd=config)
            with os.fdopen(file, "rb") as f:
                info = os.fstat(f.fileno())
                data = f.read(8193) if stat.S_ISREG(info.st_mode) and info.st_uid == uid else b""
            return data.decode("utf-8") if len(data) <= 8192 else ""
        except (OSError, ValueError):
            return ""
        finally:
            os.close(config)

    def _saved(self, data_of, release):
        """Whether the file data_of() reads (None: none) is this verified release already."""
        try:
            data = data_of()
        except (OSError, ValueError):
            return False
        if data is None or len(data) != release["size"] or hashlib.sha256(data).hexdigest() != release["sha256"]:
            return False
        self.validate(data, release["version"])
        return True

    def _save(self, release, fetch):
        """Save the verified ZIP (fetch() gives its bytes) to Downloads unless it is there; returns its path."""
        name = release["name"]
        if os.name == "posix" and dk_fs._is_root():
            fd, folder, owner = self._open_downloads_as_root()
            try:
                def existing():
                    try:
                        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
                    except FileNotFoundError:
                        return None
                    with os.fdopen(file, "rb") as f:
                        return f.read(release["size"] + 1) if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else None

                if self._saved(existing, release):
                    self.reused = True
                else:
                    dk_fs.write_atomic(name, fetch(), 0o644, folder_fd=fd, commit=self._commit, owner=owner)
            finally:
                os.close(fd)
            return os.path.join(folder, name)
        path = os.path.join(downloads_dir(self.home), name)

        def existing():
            try:
                return dk_fs.read_bytes(path, release["size"] + 1, no_links=False)
            except FileNotFoundError:
                return None

        if self._saved(existing, release):
            self.reused = True
        else:
            dk_fs.write_atomic(path, fetch(), 0o644, folder_mode=0o700, commit=self._commit)
        return path

    def download(self, expected_version):
        """Save the release the user saw (expected_version) to Downloads, checked again first."""
        with self._lock:
            if self._downloading:
                return {"ok": False, "error": "A download is already running."}
            self._downloading = True
        started, stage, self.reused = time.monotonic(), ["release_check"], False
        try:
            version_tuple(expected_version)
            with self._lock:
                view = self._check_locked(click=True)
                release = dict(self._release) if self._release else None
            if not view["ok"]:
                raise UpdateError(view["error"])
            if view.get("latest_version") != expected_version:
                raise UpdateError("The latest release changed. Check for updates again.")
            if not view["download_available"] or not release:
                raise UpdateError(view.get("message") or "There is no newer verified release to download.")

            def fetch():
                stage[0] = "network"
                try:
                    data = dk_net.get(release["url"], release["size"], release["sha256"], hosts=ASSET_HOSTS,
                                      redirects=REDIRECT_HOSTS, deadline=DOWNLOAD_SECONDS, cancel=self._closed.is_set)
                except dk_net.NetError as e:
                    self._alive()
                    raise UpdateError(f"The release ZIP could not be downloaded ({e}).") from e
                stage[0] = "verification"
                self.validate(data, release["version"])
                stage[0] = "destination"
                return data

            stage[0] = "destination"
            try:
                path = self._save(release, fetch)
            except OSError as e:
                raise UpdateError(f"The release ZIP could not be saved to Downloads "
                                  f"({e.strerror or type(e).__name__}).") from e
            self._log(f"{release['version']} saved to Downloads ({release['size']} bytes)")
            self._event("download", started, outcome="reused" if self.reused else "saved", version=release["version"])
            return {"ok": True, "path": path, "version": release["version"], "sha256": release["sha256"]}
        except UpdateError as e:
            self._log(str(e), warning=True)
            self._event("download", started, outcome="failed", stage=stage[0])
            return {"ok": False, "error": str(e)}
        finally:
            with self._lock:
                self._downloading = False
