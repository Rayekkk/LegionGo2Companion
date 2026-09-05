# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Explicit release checks and verified downloads; never installs or executes them."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "Rayekkk/LegionGo2Companion"
PLUGIN_NAME = "Legion Go 2 Companion"
ARCHIVE_ROOT = "LegionGo2Companion"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_BASE = f"https://github.com/{REPOSITORY}/releases"
MAX_METADATA = 1024 * 1024
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MEMBERS = 1024
REQUEST_TIMEOUT = 6
METADATA_SECONDS = 20
DOWNLOAD_SECONDS = 120
CACHE_SECONDS = 60
ERROR_CACHE_SECONDS = 20
CLICK_CACHE_SECONDS = 2
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("The release version must use stable X.Y.Z numbering.")
    return tuple(int(part) for part in value.split("."))


def _checked_url(url, initial_url, metadata=False):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid release URL.")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username is not None or
            parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("Release downloads require a verified HTTPS URL.")
    if url == initial_url:
        return url
    if not metadata and parsed.hostname in {
            "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        return url
    raise ValueError("GitHub redirected the request outside the allowed release hosts.")


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, initial_url, metadata):
        self.initial_url = initial_url
        self.metadata = metadata

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _checked_url(newurl, self.initial_url, self.metadata)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_chunks(response, limit, deadline, cancel=None):
    total = 0
    reader = getattr(response, "read1", response.read)
    while True:
        if cancel:
            cancel()
        if time.monotonic() >= deadline:
            raise TimeoutError("The GitHub request took too long. Try again later.")
        chunk = reader(min(65536, limit + 1 - total))
        if cancel:
            cancel()
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            raise ValueError("The GitHub response exceeded the size limit.")
        yield chunk


def _json_object(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("GitHub returned invalid release metadata.")
    return value


class CompanionUpdates:
    def __init__(self, plugin_dir: str, user_home: str, user_uid: int, user_gid: int):
        self.plugin_dir = Path(plugin_dir)
        self.user_home = user_home
        self.user_uid = user_uid
        self.user_gid = user_gid
        self._lock = threading.Lock()
        self._ssl_context = None
        self._cached = None
        self._cache_at = 0.0
        self._release = None
        self._closed = threading.Event()
        self._publication_lock = threading.Lock()

    def close(self):
        # Synchronize only the final rename, never wait for the network worker.
        with self._publication_lock:
            self._closed.set()

    def _ensure_open(self):
        if self._closed.is_set():
            raise ValueError("The update operation was cancelled because the plugin is unloading.")

    def _current_version(self):
        version = os.environ.get("DECKY_PLUGIN_VERSION")
        if not version:
            version = _json_object((self.plugin_dir / "package.json").read_bytes()).get("version")
        _version(version)
        return version

    def _tls(self):
        if self._ssl_context is None:
            context = ssl.create_default_context()
            if not context.cert_store_stats().get("x509_ca"):
                for bundle in CA_BUNDLES:
                    try:
                        context.load_verify_locations(cafile=bundle)
                    except (OSError, ssl.SSLError):
                        continue
                    if context.cert_store_stats().get("x509_ca"):
                        break
            self._ssl_context = context
        return self._ssl_context

    def _open(self, url, *, metadata):
        self._ensure_open()
        _checked_url(url, url, metadata)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._tls()), _ReleaseRedirect(url, metadata))
        request = urllib.request.Request(url, headers={
            "User-Agent": "LegionGo2Companion-release-check",
            "Accept": "application/vnd.github+json" if metadata else "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        response = opener.open(request, timeout=REQUEST_TIMEOUT)
        try:
            _checked_url(response.geturl(), url, metadata)
            if response.status != 200:
                raise ValueError("GitHub returned an unexpected response status.")
        except BaseException:
            response.close()
            raise
        return response

    def _metadata(self):
        deadline = time.monotonic() + METADATA_SECONDS
        with self._open(API_URL, metadata=True) as response:
            return _json_object(b"".join(_read_chunks(response, MAX_METADATA, deadline, self._ensure_open)))

    def _parse_release(self, data, current):
        tag = data.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("GitHub returned no stable release tag.")
        version = tag[1:] if tag.startswith("v") else tag
        available = _version(version) > _version(current)
        if data.get("draft") is not False or data.get("prerelease") is not False:
            raise ValueError("Only published stable releases can be downloaded.")
        release_url = f"{RELEASE_BASE}/tag/{tag}"
        if data.get("html_url") != release_url:
            raise ValueError("The release does not belong to the Companion repository.")
        result = {
            "success": True, "current_version": current, "latest_version": version,
            "update_available": available, "download_available": False, "release_url": release_url,
        }
        name = f"{ARCHIVE_ROOT}-{version}.zip"
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise ValueError("GitHub returned invalid release assets.")
        matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matching) != 1:
            if available:
                result["error"] = "The release has no unique Companion installation ZIP yet."
            return result, None
        asset = matching[0]
        size = asset.get("size")
        digest = asset.get("digest")
        url = f"{RELEASE_BASE}/download/{tag}/{name}"
        if (asset.get("browser_download_url") != url or asset.get("state") != "uploaded" or
                type(size) is not int or not 0 < size <= MAX_DOWNLOAD or
                not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            if available:
                result["error"] = "The release ZIP is missing trusted size, URL or SHA-256 metadata."
            return result, None
        result.update(asset_name=name, size=size, download_available=available)
        return result, {"version": version, "name": name, "url": url,
                        "size": size, "sha256": _DIGEST.fullmatch(digest).group(1).lower()}

    def _check_locked(self, *, click=False):
        now = time.monotonic()
        ttl = CLICK_CACHE_SECONDS if click else (
            CACHE_SECONDS if self._cached and self._cached.get("success") else ERROR_CACHE_SECONDS)
        if self._closed.is_set():
            return {"success": False, "current_version": "", "update_available": False,
                    "download_available": False, "error": "The update operation was cancelled because the plugin is unloading."}
        if self._cached is not None and now - self._cache_at < ttl:
            return dict(self._cached)
        current = ""
        self._release = None
        try:
            current = self._current_version()
            try:
                metadata = self._metadata()
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                error.close()
                result = {"success": True, "current_version": current, "update_available": False,
                          "download_available": False, "no_release": True}
            else:
                result, self._release = self._parse_release(metadata, current)
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                message = ("GitHub's request limit was reached. Try again later." if error.code in (403, 429)
                           else f"GitHub could not check releases (HTTP {error.code}). Try again later.")
                error.close()
            elif isinstance(error, (TimeoutError, urllib.error.URLError, OSError)):
                message = "Could not connect securely to GitHub. Check the connection and try again later."
            else:
                message = str(error) if isinstance(error, ValueError) else "Could not read the release metadata."
            result = {"success": False, "current_version": current, "update_available": False,
                      "download_available": False, "error": message}
        result["checked_at"] = time.time()
        self._cache_at = time.monotonic()
        self._cached = dict(result)
        return result

    def check(self) -> dict:
        with self._lock:
            return self._check_locked()

    @staticmethod
    def _validate_archive(stream, version, cancel=None):
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise ValueError("The release ZIP has too many entries or is empty.")
            seen = set()
            files = set()
            total = 0
            for member in members:
                name = member.filename
                parts = name.rstrip("/").split("/")
                mode = member.external_attr >> 16
                if (not name or len(name) > 1024 or "\\" in name or ":" in name or
                        member.orig_filename != name or any(ord(c) < 32 or ord(c) == 127 for c in name) or
                        any(part in ("", ".", "..") for part in parts) or parts[0] != ARCHIVE_ROOT or
                        name.casefold().rstrip("/") in seen or member.flag_bits & 1 or
                        (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))) or
                        (len(parts) == 1 and not member.is_dir()) or
                        member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError("The release ZIP contains an unsafe or unsupported entry.")
                seen.add(name.casefold().rstrip("/"))
                if not member.is_dir():
                    files.add(name.casefold())
                total += member.file_size
                if member.file_size < 0 or total > MAX_EXPANDED:
                    raise ValueError("The release ZIP expands beyond the size limit.")
            for name in seen:
                parts = name.split("/")
                if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
                    raise ValueError("The release ZIP contains conflicting file and directory paths.")
            # Validate all CRCs and actual decompressed sizes without extracting or executing anything.
            for member in members:
                count = 0
                with archive.open(member) as entry:
                    while chunk := entry.read(65536):
                        if cancel:
                            cancel()
                        count += len(chunk)
                        if count > member.file_size:
                            raise ValueError("The release ZIP has an invalid entry size.")
                if count != member.file_size:
                    raise ValueError("The release ZIP is incomplete.")
            for required in ("main.py", "dist/index.js", "plugin.json", "package.json"):
                info = archive.getinfo(f"{ARCHIVE_ROOT}/{required}")
                if info.is_dir() or info.file_size == 0:
                    raise ValueError("The release ZIP is missing a required plugin file.")
            for filename in ("plugin.json", "package.json"):
                name = f"{ARCHIVE_ROOT}/{filename}"
                if archive.getinfo(name).file_size > 65536:
                    raise ValueError("The release manifest is too large.")
                manifest = _json_object(archive.read(name))
                if manifest.get("version") != version:
                    raise ValueError("The release ZIP version does not match the GitHub release.")
                if filename == "plugin.json":
                    if (manifest.get("name") != PLUGIN_NAME or
                            manifest.get("main", "main.py") != "main.py" or
                            manifest.get("bin", "dist/index.js") != "dist/index.js"):
                        raise ValueError("The release ZIP contains a different plugin or entry point.")
                elif manifest.get("name") != "legiongo2companion":
                    raise ValueError("The release ZIP contains a different package.")
        stream.seek(0)

    def _open_home(self):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise ValueError("Safe release downloads are available only on SteamOS/Linux.")
        home = self.user_home
        if type(self.user_uid) is not int or self.user_uid <= 0:
            raise ValueError("The desktop user is unavailable; a release cannot be saved safely.")
        if (not isinstance(home, str) or not home.startswith("/") or home == "/" or
                os.path.normpath(home) != home or
                type(self.user_gid) is not int or self.user_gid < 0):
            raise ValueError("The console user's home directory could not be verified.")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        try:
            for part in home.lstrip("/").split("/"):
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            if os.fstat(fd).st_uid != self.user_uid:
                raise ValueError("The home directory does not belong to the console user.")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _download_components(self, home_fd):
        config_fd = None
        try:
            config_fd = os.open(".config", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=home_fd)
            fd = os.open("user-dirs.dirs", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=config_fd)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return ["Downloads"]
                raw = stream.read(8193)
            if len(raw) > 8192:
                return ["Downloads"]
            for line in raw.decode("utf-8").splitlines():
                match = re.fullmatch(r'\s*XDG_DOWNLOAD_DIR="([^"\r\n]*)"\s*', line)
                if not match:
                    continue
                path = match.group(1)
                if path.startswith("$HOME/"):
                    path = self.user_home + path[5:]
                if any(c in path for c in ("$", "`", "\\")):
                    break
                if (not path.startswith(self.user_home + "/") or
                        os.path.normpath(path) != path):
                    break
                return path[len(self.user_home) + 1:].split("/")
        except (OSError, UnicodeError):
            pass
        finally:
            if config_fd is not None:
                os.close(config_fd)
        return ["Downloads"]

    def _open_downloads(self):
        fd = self._open_home()
        try:
            parts = self._download_components(fd)
            for part in parts:
                self._ensure_open()
                created = False
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=fd)
                os.close(fd)
                fd = next_fd
                if created:
                    os.fchown(fd, self.user_uid, self.user_gid)
                if os.fstat(fd).st_uid != self.user_uid:
                    raise ValueError("The Downloads directory does not belong to the console user.")
            return fd, self.user_home + "/" + "/".join(parts)
        except BaseException:
            os.close(fd)
            raise

    def _existing_matches(self, directory_fd, release):
        try:
            fd = os.open(release["name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != release["size"]:
                    return False
                digest = hashlib.sha256()
                count = 0
                while chunk := stream.read(65536):
                    self._ensure_open()
                    count += len(chunk)
                    if count > release["size"]:
                        return False
                    digest.update(chunk)
                if count != release["size"] or digest.hexdigest() != release["sha256"]:
                    return False
                self._validate_archive(stream, release["version"], self._ensure_open)
                self._ensure_open()
                return True
        except OSError:
            return False

    def _save(self, release):
        self._ensure_open()
        directory_fd, path = self._open_downloads()
        temporary = None
        try:
            if self._existing_matches(directory_fd, release):
                return path + "/" + release["name"]
            candidate = f".companion-download-{secrets.token_hex(16)}.part"
            fd = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            temporary = candidate  # Clean up only a file this operation actually created.
            with os.fdopen(fd, "w+b") as stream:
                digest = hashlib.sha256()
                count = 0
                deadline = time.monotonic() + DOWNLOAD_SECONDS
                with self._open(release["url"], metadata=False) as response:
                    for chunk in _read_chunks(response, release["size"], deadline, self._ensure_open):
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                if count != release["size"] or digest.hexdigest() != release["sha256"]:
                    raise ValueError("The downloaded ZIP failed its size or SHA-256 verification.")
                stream.flush()
                self._validate_archive(stream, release["version"], self._ensure_open)
                os.fchown(stream.fileno(), self.user_uid, self.user_gid)
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
                with self._publication_lock:
                    self._ensure_open()
                    os.replace(temporary, release["name"], src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    temporary = None
                os.fsync(directory_fd)
            return path + "/" + release["name"]
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)

    def download(self, expected_version: str) -> dict:
        with self._lock:
            try:
                _version(expected_version)
                result = self._check_locked(click=True)
                if not result["success"]:
                    raise ValueError(result.get("error", "Could not check the latest release."))
                if result.get("latest_version") != expected_version:
                    raise ValueError("The latest release changed. Check for updates again before downloading.")
                if not result["download_available"] or self._release is None:
                    raise ValueError(result.get("error", "There is no newer verified release ZIP to download."))
                release = dict(self._release)
                path = self._save(release)
                return {"success": True, "path": path, "version": release["version"],
                        "sha256": release["sha256"]}
            except Exception as error:
                message = (str(error) if isinstance(error, ValueError) else
                           "The release ZIP could not be downloaded safely. Check the connection and Downloads folder.")
                return {"success": False, "error": message}
