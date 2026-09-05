# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Release trust boundaries and real, confined atomic download filesystem tests."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

import companion_updates as updates

CURRENT = "0.6.2"
NEW = "0.6.3"


def package(version=NEW, extra=None, manifest=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        files = {
            "plugin.json": json.dumps(manifest or {"name": updates.PLUGIN_NAME, "version": version}),
            "package.json": json.dumps({"name": "legiongo2companion", "version": version}),
            "main.py": "# release payload\n", "dist/index.js": "export default {};\n",
        }
        for name, content in files.items():
            archive.writestr(updates.ARCHIVE_ROOT + "/" + name, content)
        for name, content in (extra or []):
            archive.writestr(name, content)
    return data.getvalue()


def metadata(payload=None, version=NEW):
    payload = package(version) if payload is None else payload
    tag = "v" + version
    name = f"{updates.ARCHIVE_ROOT}-{version}.zip"
    return {
        "tag_name": tag, "draft": False, "prerelease": False,
        "html_url": f"{updates.RELEASE_BASE}/tag/{tag}",
        "assets": [{"name": name, "state": "uploaded", "size": len(payload),
                    "browser_download_url": f"{updates.RELEASE_BASE}/download/{tag}/{name}",
                    "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}],
    }


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, url=updates.API_URL, on_read=None):
        super().__init__(data)
        self.url = url
        self.on_read = on_read

    def geturl(self):
        return self.url

    def read1(self, size=-1):
        if self.on_read:
            self.on_read()
        return super().read(size)


class _UpdateFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "package.json").write_text(json.dumps({"version": CURRENT}))
        self.env = patch.dict(os.environ, {"DECKY_PLUGIN_VERSION": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.updater = updates.CompanionUpdates(str(self.root), "", -1, -1)


class UpdateChecks(_UpdateFixture):
    def check_with(self, data):
        with patch.object(self.updater, "_metadata", return_value=data):
            return self.updater.check()

    def test_only_new_stable_exact_release_is_downloadable(self):
        result = self.check_with(metadata())
        self.assertTrue(result["success"])
        self.assertEqual(result["current_version"], CURRENT)
        self.assertEqual(result["latest_version"], NEW)
        self.assertTrue(result["download_available"])
        self.assertIsInstance(result["checked_at"], float)
        self.assertNotIn("sha256", result)  # Download trusts only backend-owned metadata.

    def test_loader_version_is_authoritative(self):
        with patch.dict(os.environ, {"DECKY_PLUGIN_VERSION": NEW}):
            result = self.check_with(metadata())
        self.assertFalse(result["update_available"])
        self.assertFalse(result["download_available"])

    def test_no_release_is_a_normal_result_and_http_response_closed(self):
        body = io.BytesIO(b"Not Found")
        error = urllib.error.HTTPError(updates.API_URL, 404, "Not Found", {}, body)
        with patch.object(self.updater, "_metadata", side_effect=error):
            result = self.updater.check()
        self.assertTrue(result["success"])
        self.assertTrue(result["no_release"])
        self.assertFalse(result["download_available"])
        self.assertNotIn("error", result)
        self.assertTrue(body.closed)

    def test_versions_reject_prerelease_traversal_and_unbounded_numbers(self):
        for value in ("v0.6.3", "0.6.3-rc1", "0.06.3", "0.6", "0.6.3/..", "9" * 100,
                      "0.6.3\n", None, True, 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                updates._version(value)
        self.assertGreater(updates._version("0.10.0"), updates._version("0.9.9"))

    def test_invalid_releases_never_enable_download(self):
        for field, value in (("draft", True), ("prerelease", True), ("prerelease", None),
                             ("tag_name", "v0.7.0-beta1"), ("html_url", "https://evil.test/release"),
                             ("assets", "bad")):
            data = metadata()
            data[field] = value
            with self.subTest(field=field), patch.object(self.updater, "_metadata", return_value=data):
                self.updater._cached = None
                result = self.updater.check()
                self.assertFalse(result["success"])
                self.assertFalse(result["download_available"])

    def test_missing_untrusted_or_duplicate_assets_never_fall_back_to_source(self):
        mutations = [lambda data: data.update(assets=[]),
                     lambda data: data["assets"].append(dict(data["assets"][0]))]
        for field, value in (("name", "source.zip"), ("digest", None), ("digest", "sha256:bad"),
                             ("size", 0), ("size", True), ("size", updates.MAX_DOWNLOAD + 1),
                             ("browser_download_url", "https://github.com/another/repo/file.zip"),
                             ("state", "open")):
            mutations.append(lambda data, field=field, value=value: data["assets"][0].update({field: value}))
        for mutate in mutations:
            data = metadata()
            mutate(data)
            with patch.object(self.updater, "_metadata", return_value=data):
                self.updater._cached = None
                result = self.updater.check()
            self.assertTrue(result["update_available"])
            self.assertFalse(result["download_available"])
            self.assertIn("error", result)
            self.assertIsNone(self.updater._release)

    def test_concurrent_checks_share_one_request_and_copy_returned_state(self):
        def fetch():
            time.sleep(0.03)
            return metadata()
        with patch.object(self.updater, "_metadata", side_effect=fetch) as request:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.updater.check(), range(8)))
            results[0]["latest_version"] = "9.9.9"
            self.assertEqual(request.call_count, 1)
            self.assertEqual(self.updater.check()["latest_version"], NEW)

    def test_check_and_error_cache_expire(self):
        with patch.object(self.updater, "_metadata", return_value=metadata()) as request:
            self.updater.check()
            self.updater._cache_at -= updates.CACHE_SECONDS + 1
            self.updater.check()
            self.assertEqual(request.call_count, 2)
        self.updater._cached = None
        with patch.object(self.updater, "_metadata", side_effect=TimeoutError()) as request:
            self.assertFalse(self.updater.check()["success"])
            self.assertFalse(self.updater.check()["success"])
            self.assertEqual(request.call_count, 1)
            self.updater._cache_at -= updates.ERROR_CACHE_SECONDS + 1
            self.updater.check()
            self.assertEqual(request.call_count, 2)

    def test_rate_limit_has_a_clear_error(self):
        with patch.object(self.updater, "_metadata", side_effect=urllib.error.HTTPError(
                updates.API_URL, 429, "rate limit", {}, io.BytesIO())):
            self.assertIn("request limit", self.updater.check()["error"])

    def test_download_refreshes_old_metadata_and_refuses_a_changed_release(self):
        with patch.object(self.updater, "_metadata", return_value=metadata()):
            self.updater.check()
        self.updater._cache_at -= updates.CLICK_CACHE_SECONDS + 1
        with patch.object(self.updater, "_metadata", return_value=metadata(version="0.6.4")), \
                patch.object(self.updater, "_save") as save:
            result = self.updater.download(NEW)
        self.assertFalse(result["success"])
        self.assertIn("changed", result["error"])
        save.assert_not_called()

    def test_same_or_older_release_cannot_be_downloaded(self):
        for version in (CURRENT, "0.5.9"):
            with self.subTest(version=version), \
                    patch.object(self.updater, "_metadata", return_value=metadata(version=version)), \
                    patch.object(self.updater, "_save") as save:
                self.updater._cached = None
                self.assertFalse(self.updater.download(version)["success"])
                save.assert_not_called()

    def test_download_rejects_malformed_version_without_network(self):
        with patch.object(self.updater, "_metadata") as request:
            self.assertFalse(self.updater.download("../../other")["success"])
            request.assert_not_called()

    def test_close_cancels_cached_and_inflight_checks(self):
        with patch.object(self.updater, "_open", return_value=Response(
                json.dumps(metadata()).encode(), on_read=self.updater.close)):
            result = self.updater.check()
        self.assertFalse(result["success"])
        self.assertIn("cancelled", result["error"])
        self.assertFalse(self.updater.check()["success"])
        self.assertFalse(self.updater.download(NEW)["success"])

    def test_metadata_has_size_and_time_bounds(self):
        with patch.object(self.updater, "_open", return_value=Response(b" " * (updates.MAX_METADATA + 1))):
            self.assertIn("size limit", self.updater.check()["error"])
        with self.assertRaises(TimeoutError):
            list(updates._read_chunks(Response(b"a"), 10, time.monotonic() - 1))
        with patch.object(self.updater, "_open", side_effect=TimeoutError()):
            self.updater._cached = None
            self.assertFalse(self.updater.check()["success"])

    def test_every_redirect_is_validated_before_a_followup_request(self):
        initial = metadata()["assets"][0]["browser_download_url"]
        handler = updates._ReleaseRedirect(initial, False)
        req = urllib.request.Request(initial)
        for bad in ("http://github.com/file", "https://evil.test/file", "https://github.com/other/file",
                    "https://release-assets.githubusercontent.com.evil.test/file", "https://127.0.0.1/file",
                    "https://release-assets.githubusercontent.com:444/file",
                    "https://user:pass@release-assets.githubusercontent.com/file",
                    "file:///etc/passwd", "https://release-assets.githubusercontent.com/file#frag"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, "Found", {}, bad)
        allowed = "https://release-assets.githubusercontent.com/asset?signature=abc"
        self.assertEqual(handler.redirect_request(req, None, 302, "Found", {}, allowed).full_url, allowed)
        with self.assertRaises(ValueError):
            updates._ReleaseRedirect(updates.API_URL, True).redirect_request(
                urllib.request.Request(updates.API_URL), None, 302, "Found", {}, allowed)

    def test_final_response_url_is_rechecked_and_closed_on_escape(self):
        response = Response(b"bad", url="https://evil.test/file")
        with patch("companion_updates.urllib.request.build_opener") as build, \
                patch.object(self.updater, "_tls"):
            build.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                self.updater._open(updates.API_URL, metadata=True)
        self.assertTrue(response.closed)


class ArchiveChecks(unittest.TestCase):
    def validate(self, payload):
        updates.CompanionUpdates._validate_archive(io.BytesIO(payload), NEW)

    def test_valid_payload(self):
        self.validate(package())

    def test_wrong_version_plugin_or_missing_payload_rejected(self):
        for payload in (package(version="0.6.4"), package(manifest={"version": NEW, "name": "Other"}),
                        b"not a zip"):
            with self.subTest(payload=payload[:30]), self.assertRaises((ValueError, zipfile.BadZipFile)):
                self.validate(payload)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(updates.ARCHIVE_ROOT + "/plugin.json", "{}")
        with self.assertRaises(KeyError):
            self.validate(stream.getvalue())

    def test_path_traversal_duplicate_windows_paths_and_file_directory_conflicts(self):
        for name in ("../evil", "/absolute", "LegionGo2Companion/../evil", "Other/main.py",
                     "LegionGo2Companion/C:evil",
                     "LegionGo2Companion//evil", "LegionGo2Companion/./evil",
                     "legiongo2companion/main.py", "LegionGo2Companion/MAIN.PY",
                     "LegionGo2Companion/dist", "LegionGo2Companion/control\nname"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.validate(package(extra=[(name, "bad")]))

    def test_backslash_in_real_zip_headers_rejected_on_every_platform(self):
        payload = package(extra=[("LegionGo2Companion/evil", "bad")])
        with self.assertRaises(ValueError):
            self.validate(payload.replace(b"Companion/evil", b"Companion\\evil"))

    def test_wrong_entrypoint_rejected(self):
        for field, value in (("main", "evil.py"), ("bin", "evil.js")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(package(manifest={"name": updates.PLUGIN_NAME, "version": NEW, field: value}))

    def test_symlink_rejected(self):
        info = zipfile.ZipInfo("LegionGo2Companion/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            self.validate(package(extra=[(info, "/etc/passwd")]))

    def test_encryption_rejected(self):
        payload = bytearray(package())
        local = payload.index(b"PK\x03\x04")
        central = payload.index(b"PK\x01\x02")
        struct.pack_into("<H", payload, local + 6, 1)
        struct.pack_into("<H", payload, central + 8, 1)
        with self.assertRaises(ValueError):
            self.validate(bytes(payload))

    def test_expansion_count_and_corrupt_crc_rejected(self):
        with patch.object(updates, "MAX_EXPANDED", 10), self.assertRaises(ValueError):
            self.validate(package())
        with patch.object(updates, "MAX_MEMBERS", 2), self.assertRaises(ValueError):
            self.validate(package())
        payload = bytearray(package())
        central = payload.index(b"PK\x01\x02")
        payload[central + 16] ^= 0xff
        with self.assertRaises(zipfile.BadZipFile):
            self.validate(bytes(payload))


@unittest.skipUnless(os.name == "posix", "safe dir_fd downloads require POSIX")
class DownloadFiles(_UpdateFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        self.uid = os.getuid() or 1000
        self.gid = os.getgid() if os.getuid() else 1000
        os.chown(self.home, self.uid, self.gid)
        self.updater = updates.CompanionUpdates(str(self.root), str(self.home), self.uid, self.gid)
        self.payload = package()
        self.request = patch.object(self.updater, "_metadata", return_value=metadata(self.payload))
        self.request.start()
        self.addCleanup(self.request.stop)

    def do_download(self, payload=None):
        with patch.object(self.updater, "_open", return_value=Response(
                self.payload if payload is None else payload)):
            return self.updater.download(NEW)

    def test_atomic_completed_file_is_user_owned_and_repeat_click_reuses_it(self):
        with patch.object(self.updater, "_open", side_effect=lambda *a, **k: Response(self.payload)) as request:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.updater.download(NEW), range(2)))
        self.assertTrue(all(result["success"] for result in results), results)
        self.assertEqual(request.call_count, 1)
        target = Path(results[0]["path"])
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(target.stat().st_uid, self.uid)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(results[0]["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertFalse(list(target.parent.glob("*.part")))

    def test_hash_size_timeout_and_cancellation_leave_no_partial_or_final_file(self):
        for scenario in ("hash", "size", "timeout", "close"):
            with self.subTest(scenario=scenario):
                self.updater._closed.clear()
                body = self.payload
                callback = None
                if scenario == "hash":
                    body = bytes([body[0] ^ 1]) + body[1:]
                if scenario == "size":
                    body = body[:-1]
                if scenario == "timeout":
                    def callback():
                        raise TimeoutError()
                if scenario == "close":
                    callback = self.updater.close
                with patch.object(self.updater, "_open", return_value=Response(body, on_read=callback)):
                    result = self.updater.download(NEW)
                self.assertFalse(result["success"])
                self.assertEqual(list((self.home / "Downloads").iterdir()), [])

    def test_invalid_zip_with_a_matching_hash_is_not_published(self):
        bad = b"valid hash, invalid zip"
        with patch.object(self.updater, "_metadata", return_value=metadata(bad)):
            result = self.do_download(bad)
        self.assertFalse(result["success"])
        self.assertEqual(list((self.home / "Downloads").iterdir()), [])

    def test_existing_matching_hash_still_requires_archive_validation(self):
        bad = b"exact matching digest, but not a plugin ZIP"
        downloads = self.home / "Downloads"
        downloads.mkdir()
        os.chown(downloads, self.uid, self.gid)
        target = downloads / metadata()["assets"][0]["name"]
        target.write_bytes(bad)
        with patch.object(self.updater, "_metadata", return_value=metadata(bad)), \
                patch.object(self.updater, "_open") as request:
            result = self.updater.download(NEW)
        self.assertFalse(result["success"])
        self.assertEqual(target.read_bytes(), bad)
        request.assert_not_called()

    def test_xdg_inside_home_is_honoured_and_shell_or_outside_paths_fall_back(self):
        config = self.home / ".config"
        config.mkdir()
        for path, expected in (("$HOME/Pobrane pliki", "Pobrane pliki"),
                               (str(self.root / "outside"), "Downloads"),
                               ("$HOME/../outside", "Downloads"),
                               ("$(touch /tmp/should-not-execute)", "Downloads")):
            with self.subTest(path=path):
                (config / "user-dirs.dirs").write_text(f'XDG_DOWNLOAD_DIR="{path}"\n')
                result = self.do_download()
                self.assertTrue(result["success"], result)
                self.assertEqual(Path(result["path"]).parent, self.home / expected)

    def test_symlink_home_downloads_and_config_cannot_redirect_root_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        downloads = self.home / "Downloads"
        downloads.symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.do_download()["success"])
        self.assertEqual(list(outside.iterdir()), [])
        downloads.unlink()
        real_home = self.home
        alias = self.root / "alias"
        alias.symlink_to(real_home, target_is_directory=True)
        self.updater.user_home = str(alias)
        self.assertFalse(self.do_download()["success"])
        self.updater.user_home = str(real_home)
        (self.home / ".config").symlink_to(outside, target_is_directory=True)
        (outside / "user-dirs.dirs").write_text(f'XDG_DOWNLOAD_DIR="{outside}"\n')
        self.assertTrue(self.do_download()["success"])
        self.assertEqual([file.name for file in outside.iterdir()], ["user-dirs.dirs"])

    def test_directory_changed_to_symlink_between_creation_check_and_open_is_rejected(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        os.chown(downloads, self.uid, self.gid)
        outside = self.root / "outside"
        outside.mkdir()
        original_open = os.open
        def race_open(path, flags, *args, **kwargs):
            if path == "Downloads":
                downloads.rename(self.home / "original-downloads")
                downloads.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)
        with patch("companion_updates.os.open", side_effect=race_open):
            result = self.do_download()
        self.assertFalse(result["success"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.home / "original-downloads").iterdir()), [])

    def test_exclusive_temporary_name_collision_preserves_the_preexisting_file(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        os.chown(downloads, self.uid, self.gid)
        existing = downloads / ".companion-download-collision.part"
        existing.write_bytes(b"do not remove someone else's partial")
        with patch("companion_updates.secrets.token_hex", return_value="collision"):
            result = self.do_download()
        self.assertFalse(result["success"])
        self.assertEqual(existing.read_bytes(), b"do not remove someone else's partial")
        self.assertEqual(list(downloads.iterdir()), [existing])

    def test_existing_target_symlink_and_hardlink_are_replaced_without_following(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        os.chown(downloads, self.uid, self.gid)
        outside = self.root / "important"
        outside.write_text("do not change")
        target = downloads / metadata()["assets"][0]["name"]
        target.symlink_to(outside)
        self.assertTrue(self.do_download()["success"])
        self.assertEqual(outside.read_text(), "do not change")
        target.unlink()
        os.link(outside, target)
        self.assertTrue(self.do_download()["success"])
        self.assertEqual(outside.read_text(), "do not change")
        self.assertNotEqual(outside.stat().st_ino, target.stat().st_ino)

    def test_unavailable_account_and_wrong_home_owner_fail_before_download(self):
        self.updater.user_uid = -1
        result = self.do_download()
        self.assertFalse(result["success"])
        self.assertIn("desktop user", result["error"])
        self.assertFalse((self.home / "Downloads").exists())
        self.updater.user_uid = self.uid + 1
        self.assertFalse(self.do_download()["success"])
        self.assertFalse((self.home / "Downloads").exists())

    def test_cancel_just_before_publication_retains_the_original_file(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        os.chown(downloads, self.uid, self.gid)
        target = downloads / metadata()["assets"][0]["name"]
        target.write_bytes(b"original")
        original_fsync = os.fsync
        def stop_after_sync(fd):
            original_fsync(fd)
            self.updater.close()
        with patch("companion_updates.os.fsync", side_effect=stop_after_sync):
            result = self.do_download()
        self.assertFalse(result["success"])
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(downloads.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
