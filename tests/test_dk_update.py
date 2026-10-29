# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_dk_update.py): change it there, then `kit sync`.
# One test (or more) per guarantee in dk_update.py's docstring; the root walk runs on POSIX (WSL/CI Linux).
import hashlib
import io
import json
import logging
import os
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

import _harness  # noqa: F401
import dk_fs
import dk_net
import dk_update

POSIX = os.name == "posix"
logging.getLogger("test_dk_update").addHandler(logging.NullHandler())  # assertLogs still sees the lines
REPO, FOLDER, NAME = "Owner/DkPlugin", "DkPlugin", "Dk Plugin"


def stamped(entry):
    """entry as a ZipInfo with a fixed time, so two plugin_zip() calls give the same bytes (a name would get the
    current DOS time, which ticks every 2 s: a different SHA-256 on a loaded machine)."""
    if isinstance(entry, zipfile.ZipInfo):
        return entry
    info = zipfile.ZipInfo(entry, (2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o40775 << 16) | 0x10 if entry.endswith("/") else 0o600 << 16  # what writestr gives a name
    return info


def plugin_zip(version="1.2.0", *, folder=FOLDER, name=NAME, extra=(), drop=(), package=None, manifest=None):
    """A release ZIP: (bytes). extra: (ZipInfo or name, data) entries added; drop: required files left out."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(stamped(f"{folder}/"), b"")
        files = {"main.py": b"print()", "dist/index.js": b"x", "plugin.json": json.dumps({"name": name, "version": version, **(manifest or {})}),
                 "package.json": json.dumps({"name": package or "dkplugin", "version": version})}
        for rel, data in files.items():
            if rel not in drop:
                z.writestr(stamped(f"{folder}/{rel}"), data)
        for entry, data in extra:
            z.writestr(stamped(entry), data)
    return out.getvalue()


def release(version="1.2.0", data=None, **changes):
    data = plugin_zip(version) if data is None else data
    tag = changes.pop("tag", version)
    name = f"{FOLDER}-{version}.zip"
    asset = {"name": name, "state": "uploaded", "size": len(data), "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
             "browser_download_url": f"https://github.com/{REPO}/releases/download/{tag}/{name}"}
    asset.update(changes.pop("asset", {}))
    body = {"tag_name": tag, "draft": False, "prerelease": False,
            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}", "assets": [asset]}
    body.update(changes)
    return body


def encrypted(data, name):
    """data with the entry `name` marked encrypted in the central directory (zipfile cannot write that)."""
    data = bytearray(data)
    at = 0
    while (at := data.find(b"PK\x01\x02", at)) >= 0:
        length = int.from_bytes(data[at + 28:at + 30], "little")
        if data[at + 46:at + 46 + length] == name:
            data[at + 8] |= 1
        at += 4
    return bytes(data)


class GitHub:
    """dk_net.get as GitHub answers: the release metadata, then the ZIP; records each call."""

    def __init__(self, metadata, data=None, status=None):
        self.metadata, self.data, self.status, self.calls = metadata, data, status, []

    def __call__(self, url, limit, sha256=None, **kw):
        self.calls.append((url, limit, sha256, kw))
        if "api.github.com" in url:
            if self.status:
                error = dk_net.NetError(f"HTTP {self.status}")
                error.status = self.status
                raise error
            return json.dumps(self.metadata).encode()
        if sha256 and hashlib.sha256(self.data).hexdigest() != sha256:
            raise dk_net.Mismatch("not the expected file (SHA-256 differs)")
        return self.data


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.logger = logging.getLogger("test_dk_update")

    def updates(self, current="1.0.0", **kw):
        return dk_update.Updates(repo=REPO, folder=FOLDER, plugin_name=NAME, current=current, home=self.home,
                                 logger=self.logger, **kw)

    def check(self, metadata, current="1.0.0", **kw):
        with mock.patch.object(dk_net, "get", GitHub(metadata, **kw)):
            return self.updates(current).check()

    def downloads(self):
        folder = os.path.join(self.home, "Downloads")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class ReleaseTest(Base):
    def test_a_stable_release_with_a_verified_zip_can_be_downloaded(self):
        view = self.check(release())
        self.assertEqual({k: view[k] for k in ("ok", "latest_version", "update_available", "download_available",
                                               "asset_name")},
                         {"ok": True, "latest_version": "1.2.0", "update_available": True, "download_available": True,
                          "asset_name": "DkPlugin-1.2.0.zip"})
        self.assertTrue(self.check(release(tag="v1.2.0"))["download_available"], "v prefix")
        self.assertFalse(self.check(release(), current="1.2.0")["update_available"])

    def test_only_published_stable_x_y_z_releases(self):
        for changes in ({"draft": True}, {"prerelease": True}, {"draft": None}):
            view = self.check(release(**changes))
            self.assertFalse(view["ok"], changes)
            self.assertIn("stable", view["error"])
        for tag in ("1.2.0-beta", "1.2", "1234567.0.0", "01.2.0"):
            self.assertFalse(self.check(release(tag=tag))["ok"], tag)

    def test_release_and_asset_must_be_this_repositorys(self):
        view = self.check(release(html_url="https://github.com/Evil/DkPlugin/releases/tag/1.2.0"))
        self.assertIn("repository", view["error"])
        for asset in ({"browser_download_url": "https://github.com/Evil/x/releases/download/1.2.0/DkPlugin-1.2.0.zip"},
                      {"state": "starter"}, {"digest": None}, {"digest": "md5:00"}, {"size": 0},
                      {"size": 33 << 20}, {"size": "1"}):
            view = self.check(release(asset=asset))
            self.assertTrue(view["ok"], asset)
            self.assertFalse(view["download_available"], asset)
            self.assertIn("trusted", view["message"])
        body = release()
        body["assets"].append(dict(body["assets"][0]))
        view = self.check(body)
        self.assertFalse(view["download_available"], "two assets of that name: neither is taken")
        self.assertIn("single", view["message"])
        body["assets"] = [{"name": "Other.zip"}]
        self.assertFalse(self.check(body)["download_available"])

    def test_with_an_uploader_only_its_asset_is_offered(self):
        bot = {"uploader": {"login": "github-actions[bot]"}}
        with mock.patch.object(dk_net, "get", GitHub(release(asset=bot))):
            self.assertTrue(self.updates(uploader="github-actions[bot]").check()["download_available"])
        for asset in ({"uploader": {"login": "someone"}}, {}, {"uploader": "github-actions[bot]"}):
            with self.subTest(asset=asset), mock.patch.object(dk_net, "get", GitHub(release(asset=asset))):
                view = self.updates(uploader="github-actions[bot]").check()
                self.assertFalse(view["download_available"])
                self.assertIn("release workflow", view["message"])
        with mock.patch.object(dk_net, "get", GitHub(release(asset={"uploader": {"login": "someone"}}))):
            self.assertTrue(self.updates().check()["download_available"], "uploader=None: not checked")

    def test_network_answers_become_messages(self):
        view = self.check(None, status=404)
        self.assertTrue(view["ok"] and view["no_release"])
        self.assertIn("private", view["message"])
        for status, text in ((403, "limit"), (429, "limit"), (500, "HTTP 500")):
            self.assertIn(text, self.check(None, status=status)["error"])
        with mock.patch.object(dk_net, "get", side_effect=dk_net.NetError("could not connect")):
            self.assertIn("connect securely", self.updates().check()["error"])
        with mock.patch.object(dk_net, "get", return_value=b"[1]"):
            self.assertIn("unreadable", self.updates().check()["error"])
        self.assertIn("X.Y.Z", self.check(release(), current="dev")["error"])

    def test_metadata_without_redirects_and_the_zip_only_from_github_hosts(self):
        github = GitHub(release(), plugin_zip())
        u = self.updates()
        with mock.patch.object(dk_net, "get", github):
            self.assertTrue(u.download("1.2.0")["ok"])
        (api, _, _, meta), (url, limit, sha, kw) = github.calls
        self.assertEqual((api, meta["hosts"], meta["redirects"]),
                         (f"https://api.github.com/repos/{REPO}/releases/latest", {"api.github.com"}, False))
        self.assertEqual(url, f"https://github.com/{REPO}/releases/download/1.2.0/DkPlugin-1.2.0.zip")
        self.assertEqual(kw["hosts"], dk_update.ASSET_HOSTS)
        self.assertEqual(set(kw["redirects"]), {"objects.githubusercontent.com", "release-assets.githubusercontent.com"},
                         "the release URL may not redirect to another github.com page")
        self.assertEqual((limit, sha), (len(github.data), hashlib.sha256(github.data).hexdigest()))
        self.assertEqual(kw["cancel"], u._closed.is_set)


class ZipTest(Base):
    def refused(self, data, text="unsafe"):
        with self.assertRaises(dk_update.UpdateError) as caught:
            self.updates().validate(data, "1.2.0")
        self.assertIn(text, str(caught.exception))

    def test_a_good_zip_passes(self):
        self.updates().validate(plugin_zip(), "1.2.0")

    def test_unsafe_names_and_entries_are_refused(self):
        link = zipfile.ZipInfo(f"{FOLDER}/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entry in (f"{FOLDER}/../x", f"{FOLDER}/c:x", f"{FOLDER}/a\x01", "Other/x", "loose.py", f"{FOLDER}//x",
                      link):
            self.refused(plugin_zip(extra=[(entry, b"x")]))
        # names zipfile will not write as they are (it turns \ into / on Windows, cuts at NUL): patched in
        for raw in (b"DkPlugin/a\\y", b"DkPlugin/a\x00y"):
            self.refused(plugin_zip(extra=[(f"{FOLDER}/a_y", b"x")]).replace(b"DkPlugin/a_y", raw), "")

    def test_duplicates_and_clashes_ignoring_case_are_refused(self):
        self.refused(plugin_zip(extra=[(f"{FOLDER}/MAIN.py", b"x")]))
        self.refused(plugin_zip(extra=[(f"{FOLDER}/main.py/inner", b"x")]), "same name")

    def test_encryption_limits_and_wrong_sizes_are_refused(self):
        self.refused(encrypted(plugin_zip(extra=[(f"{FOLDER}/s", b"x")]), b"DkPlugin/s"))
        with self.assertRaises(dk_update.UpdateError):
            self.updates(max_members=3).validate(plugin_zip(), "1.2.0")
        with self.assertRaises(dk_update.UpdateError):
            self.updates(max_expanded=10).validate(plugin_zip(), "1.2.0")
        stored = io.BytesIO()
        with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as z:
            for info in zipfile.ZipFile(io.BytesIO(plugin_zip())).infolist():
                z.writestr(info.filename, zipfile.ZipFile(io.BytesIO(plugin_zip())).read(info.filename))
        corrupt = stored.getvalue().replace(b"print()", b"PRINT()")
        with self.assertRaises(dk_update.UpdateError):
            self.updates().validate(corrupt, "1.2.0")  # CRC
        self.refused(b"not a zip", "complete")

    def test_required_files_and_manifests(self):
        self.refused(plugin_zip(drop=["dist/index.js"]), "required plugin file: dist/index.js")
        self.refused(plugin_zip(drop=["main.py"], extra=[(f"{FOLDER}/main.py", b"")]), "required")
        with self.assertRaisesRegex(dk_update.UpdateError, "extra.py"):
            self.updates(required=(*dk_update.REQUIRED, "extra.py")).validate(plugin_zip(), "1.2.0")
        self.refused(plugin_zip(name="Other"), "different plugin")
        self.refused(plugin_zip("1.1.0"), "version")
        with self.assertRaises(dk_update.UpdateError):
            self.updates(package_name="other").validate(plugin_zip(), "1.2.0")

    def test_a_release_asking_for_other_flags_is_refused(self):
        user = self.updates(flags=[])
        user.validate(plugin_zip(), "1.2.0")  # no flags: none
        user.validate(plugin_zip(manifest={"flags": []}), "1.2.0")
        with self.assertRaisesRegex(dk_update.UpdateError, "different plugin"):
            user.validate(plugin_zip(manifest={"flags": ["root"]}), "1.2.0")
        self.updates().validate(plugin_zip(manifest={"flags": ["root"]}), "1.2.0")  # flags=None: not checked


class SaveTest(Base):
    def download(self, metadata=None, data=None, u=None, expected="1.2.0"):
        data = plugin_zip() if data is None else data
        github = GitHub(release(data=data) if metadata is None else metadata, data)
        with mock.patch.object(dk_net, "get", github):
            return (u or self.updates()).download(expected), github

    def test_saved_to_downloads_atomically_with_mode_0644(self):
        with mock.patch("dk_fs.write_atomic", wraps=dk_fs.write_atomic) as publish:
            result, _ = self.download()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], os.path.join(self.home, "Downloads", "DkPlugin-1.2.0.zip"))
        self.assertEqual(publish.call_args.args[2], 0o644)
        with open(result["path"], "rb") as f:
            self.assertEqual(f.read(), plugin_zip())
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(result["path"])).st_mode), 0o700)

    def test_the_xdg_download_folder_inside_home_only(self):
        os.makedirs(os.path.join(self.home, ".config"))
        user_dirs = os.path.join(self.home, ".config", "user-dirs.dirs")
        for line, folder in (('XDG_DOWNLOAD_DIR="$HOME/Pobrane"', ["Pobrane"]),
                             (f'XDG_DOWNLOAD_DIR="{self.home}/Files/In"', ["Files", "In"]),
                             ('XDG_DOWNLOAD_DIR="/etc"', ["Downloads"]),
                             ('XDG_DOWNLOAD_DIR="$HOME/../x"', ["Downloads"]),
                             ('XDG_DOWNLOAD_DIR="$HOME/$(id)"', ["Downloads"])):
            with open(user_dirs, "w", encoding="utf-8") as f:
                f.write(f"# xdg\n{line}\n")
            self.assertEqual(dk_update.downloads_dir(self.home), os.path.join(self.home, *folder), line)

    def test_nothing_is_kept_when_the_zip_is_bad_or_the_hash_differs(self):
        bad = plugin_zip(extra=[(f"{FOLDER}/../evil", b"x")])
        result, _ = self.download(data=bad)
        self.assertFalse(result["ok"])
        self.assertEqual(self.downloads(), [])
        github = GitHub(release(), plugin_zip("1.2.0") + b"tampered")
        with mock.patch.object(dk_net, "get", github):
            result = self.updates().download("1.2.0")
        self.assertIn("could not be downloaded", result["error"])
        self.assertEqual(self.downloads(), [])

    def test_a_verified_file_is_reused_and_another_one_replaced(self):
        result, _ = self.download()
        result, github = self.download()
        self.assertTrue(result["ok"])
        self.assertEqual(len(github.calls), 1, "only the release check, no second download")
        with open(result["path"], "wb") as f:
            f.write(b"something else")
        result, github = self.download()
        self.assertEqual(len(github.calls), 2)
        with open(result["path"], "rb") as f:
            self.assertEqual(f.read(), plugin_zip())

    def test_a_disk_error_is_a_message_without_the_path(self):
        with mock.patch("dk_fs.write_atomic", side_effect=OSError(28, "No space left on device", self.home)):
            result, _ = self.download()
        self.assertIn("No space left", result["error"])
        self.assertNotIn(self.home, result["error"])

    @unittest.skipUnless(POSIX, "the root walk is POSIX")
    def test_as_root_the_way_to_downloads_never_follows_a_link(self):
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.home, "Downloads"))
        with mock.patch("dk_fs._is_root", return_value=True):
            result, _ = self.download()
        self.assertFalse(result["ok"])
        self.assertEqual(os.listdir(outside), [])
        os.remove(os.path.join(self.home, "Downloads"))
        os.makedirs(os.path.join(self.home, ".config"))
        os.symlink(outside, os.path.join(self.home, "via"))
        with open(os.path.join(self.home, ".config", "user-dirs.dirs"), "w") as f:
            f.write('XDG_DOWNLOAD_DIR="$HOME/via/sub"\n')
        with mock.patch("dk_fs._is_root", return_value=True):
            result, _ = self.download()
        self.assertFalse(result["ok"], "a link in the middle of the way is refused too")
        self.assertEqual(os.listdir(outside), [])
        os.remove(os.path.join(self.home, ".config", "user-dirs.dirs"))
        with mock.patch("dk_fs._is_root", return_value=True):
            result, _ = self.download()
        self.assertTrue(result["ok"], result)
        folder = os.path.join(self.home, "Downloads")
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700, "created 0700 for the user")
        self.assertEqual(os.stat(result["path"]).st_uid, os.stat(self.home).st_uid)


    @unittest.skipUnless(POSIX, "the root walk is POSIX")
    def test_as_root_home_must_belong_to_the_console_user(self):
        with mock.patch("dk_fs._is_root", return_value=True):
            result, _ = self.download(u=self.updates(uid=os.getuid() + 1))
            self.assertFalse(result["ok"])
            self.assertIn("home folder could not be verified", result["error"])
            self.assertEqual(self.downloads(), [])
            result, _ = self.download(u=self.updates(uid=os.getuid()))
        self.assertTrue(result["ok"], result)

    @unittest.skipUnless(POSIX, "the root walk is POSIX")
    def test_as_root_a_linked_config_folder_is_not_read(self):
        elsewhere = tempfile.mkdtemp()
        with open(os.path.join(elsewhere, "user-dirs.dirs"), "w") as f:
            f.write('XDG_DOWNLOAD_DIR="$HOME/Pobrane"' + chr(10))
        os.symlink(elsewhere, os.path.join(self.home, ".config"))
        with mock.patch("dk_fs._is_root", return_value=True):
            result, _ = self.download()
        self.assertEqual(result["path"], os.path.join(self.home, "Downloads", "DkPlugin-1.2.0.zip"))

    @unittest.skipUnless(POSIX, "the root walk is POSIX")
    def test_as_root_the_saved_zip_is_always_the_users(self):
        with mock.patch("dk_fs._is_root", return_value=True), \
                mock.patch("dk_fs.write_atomic", wraps=dk_fs.write_atomic) as publish:
            result, _ = self.download()
        self.assertTrue(result["ok"], result)
        info = os.stat(self.home)
        self.assertEqual(publish.call_args.kwargs["owner"], (info.st_uid, info.st_gid))

    def test_diagnostics_hear_each_check_and_download_without_paths(self):
        heard = []
        u = self.updates(on_event=lambda name, **fields: heard.append((name, fields)))
        self.download(u=u)
        self.download(u=u)
        github = GitHub(release(), plugin_zip("1.2.0") + b"tampered")
        os.remove(os.path.join(self.home, "Downloads", "DkPlugin-1.2.0.zip"))
        with mock.patch.object(dk_net, "get", github):
            u.download("1.2.0")
        downloads = [(f["outcome"], f.get("stage")) for name, f in heard if name == "download"]
        self.assertEqual(downloads, [("saved", None), ("reused", None), ("failed", "network")])
        self.assertTrue(all(f["outcome"] == "ok" and f["latest"] == "1.2.0" for n, f in heard if n == "check"))
        self.assertNotIn(self.home, repr(heard))
        broken = self.updates(on_event=mock.Mock(side_effect=RuntimeError("observer bug")))
        self.assertTrue(self.download(u=broken)[0]["ok"], "a failing observer changes nothing")


class FlowTest(Base):
    def test_check_is_cached_and_a_click_asks_again_soon(self):
        github = GitHub(release(), plugin_zip())
        u = self.updates()
        with mock.patch.object(dk_net, "get", github):
            u.check()
            u.check()
            self.assertEqual(len(github.calls), 1)
            u._cached_at -= dk_update.CLICK_CACHE_SECONDS + 1
            u.check()
            self.assertEqual(len(github.calls), 1, "an automatic check keeps the minute")
            u.check(click=True)
            self.assertEqual(len(github.calls), 2)
            github.status = 500
            u._cached_at -= dk_update.CACHE_SECONDS + 1
            u.check()
            u._cached_at -= dk_update.ERROR_CACHE_SECONDS + 1
            u.check()
            self.assertEqual(len(github.calls), 4, "errors are kept for 20 s only")

    def test_download_refuses_a_release_that_changed_since_the_check(self):
        github = GitHub(release("1.3.0", data=plugin_zip("1.3.0")), plugin_zip("1.3.0"))
        with mock.patch.object(dk_net, "get", github):
            result = self.updates().download("1.2.0")
        self.assertIn("changed", result["error"])
        self.assertEqual(len(github.calls), 1)
        self.assertFalse(self.updates().download("../1")["ok"])

    def test_a_second_download_is_refused_at_once(self):
        started, go = threading.Event(), threading.Event()
        data = plugin_zip()
        github = GitHub(release(data=data), data)

        def slow(url, *a, **kw):
            if "api.github.com" not in url:
                started.set()
                go.wait(5)
            return github(url, *a, **kw)

        u = self.updates()
        results = []
        with mock.patch.object(dk_net, "get", side_effect=slow):
            worker = threading.Thread(target=lambda: results.append(u.download("1.2.0")))
            worker.start()
            self.assertTrue(started.wait(5))
            self.assertIn("already running", u.download("1.2.0")["error"])
            go.set()
            worker.join(5)
        self.assertTrue(results[0]["ok"])

    def test_after_close_nothing_new_is_saved(self):
        data = plugin_zip()
        github = GitHub(release(data=data), data)
        u = self.updates()

        def closing(url, *a, **kw):
            answer = github(url, *a, **kw)
            if "api.github.com" not in url:
                u.close()  # the plugin unloads while the ZIP arrives
            return answer

        with mock.patch.object(dk_net, "get", side_effect=closing):
            result = u.download("1.2.0")
        self.assertIn("cancelled", result["error"])
        self.assertEqual(self.downloads(), [])
        self.assertIn("cancelled", u.check()["error"])

    def test_close_never_waits_for_a_write_in_progress(self):
        data = plugin_zip()
        writing, go = threading.Event(), threading.Event()
        copy = dk_fs._copy

        def slow_copy(source, f):  # a slow card: the bytes take long to reach the disk
            writing.set()
            go.wait(5)
            copy(source, f)

        u, results = self.updates(), []
        with mock.patch.object(dk_net, "get", GitHub(release(data=data), data)), \
                mock.patch.object(dk_fs, "_copy", slow_copy):
            worker = threading.Thread(target=lambda: results.append(u.download("1.2.0")))
            worker.start()
            self.assertTrue(writing.wait(5))
            started = time.monotonic()
            u.close()
            self.assertLess(time.monotonic() - started, 0.5, "close() waited for the write")
            go.set()
            worker.join(5)
        self.assertIn("cancelled", results[0]["error"])
        self.assertEqual(self.downloads(), [], "nothing published after close(), no temp file left")

    def test_logs_name_versions_never_paths(self):
        with self.assertLogs("test_dk_update", "INFO") as logs:
            with mock.patch.object(dk_net, "get", GitHub(release(), plugin_zip())):
                self.updates().download("1.2.0")
        self.assertTrue(any("1.2.0" in line for line in logs.output))
        self.assertFalse([line for line in logs.output if self.home in line])


if __name__ == "__main__":
    unittest.main()
