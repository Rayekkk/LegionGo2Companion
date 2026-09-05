# SPDX-License-Identifier: BSD-3-Clause
"""Offline coverage for Companion's deliberately limited download surface."""
import io
import hashlib
import json
from pathlib import Path
import ssl
import tempfile
import tarfile
import unittest
from unittest.mock import patch

import test_integration  # Install isolated Decky stubs before backend imports.
import main
import tdp_backend as tdp
import vibration_backend as vibration
import display_backend as display
import tdp_updater
import vibration_updater
import display_updater


HELPERS = (tdp_updater, vibration_updater, display_updater)


class UpdaterScopeTests(unittest.TestCase):
    def helper(self, module, directory):
        return module.Updater(user_agent="test", log_prefix="[test]",
                              plugin_dir=directory, logger=test_integration.decky.logger)

    def test_entrypoint_and_components_have_no_standalone_update_rpc(self):
        for backend in (main, tdp, vibration, display):
            with self.subTest(backend=backend.__name__):
                for name in ("check_for_updates", "perform_update", "apply_update",
                             "vibe_check_for_updates", "vibe_perform_update",
                             "display_check_for_updates", "display_perform_update"):
                    self.assertFalse(hasattr(backend.Plugin, name), name)

    def test_no_helper_can_download_a_standalone_plugin_archive(self):
        for module in HELPERS:
            with self.subTest(module=module.__name__):
                for name in ("check", "download", "download_latest", "_download_asset",
                             "check_version_from_asset"):
                    self.assertFalse(hasattr(module.Updater, name), name)
                for name in ("real_user", "xdg_download_dir", "confined_download_dir"):
                    self.assertFalse(hasattr(module, name), name)
        for module in (vibration_updater, display_updater):
            self.assertFalse(hasattr(module.Updater, "open_url"))
            self.assertFalse(hasattr(module.Updater, "download_to"))

    def test_version_metadata_still_prefers_loader_and_falls_back_to_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "plugin.json").write_text(json.dumps({"version": "0.6.0"}), encoding="utf-8")
            for module in HELPERS:
                with self.subTest(module=module.__name__):
                    helper = self.helper(module, raw)
                    with patch.dict(module.os.environ, {"DECKY_PLUGIN_VERSION": "9.1.2"}):
                        self.assertEqual(helper.plugin_version(), "9.1.2")
                    with patch.dict(module.os.environ, {"DECKY_PLUGIN_VERSION": ""}):
                        self.assertEqual(helper.plugin_version(), "0.6.0")

    def test_retained_tls_contexts_verify_certificates_and_are_cached(self):
        for module in HELPERS:
            with self.subTest(module=module.__name__):
                helper = self.helper(module, ".")
                context = helper.ssl_context()
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertIs(helper.ssl_context(), context)

    def test_pinned_helper_download_still_enforces_https_and_size_limit(self):
        helper = self.helper(tdp_updater, ".")
        for url in ("http://github.com/test", "file:///tmp/test", "https://example.org/test"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                tdp_updater.checked_url(url)
        self.assertEqual(tdp_updater.checked_url(tdp.RYZENADJ_URL), tdp.RYZENADJ_URL)
        output = io.BytesIO()
        with patch.object(helper, "open_url", return_value=io.BytesIO(b"ok")):
            self.assertEqual(helper.download_to(tdp.RYZENADJ_URL, output, timeout=1), 2)
        self.assertEqual(output.getvalue(), b"ok")
        with patch.object(helper, "open_url", return_value=io.BytesIO(b"abcd")), \
             patch.object(tdp_updater, "MAX_DOWNLOAD_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "size limit"):
                helper.download_to(tdp.RYZENADJ_URL, io.BytesIO(), timeout=1)

    def test_wrong_ryzenadj_archive_is_rejected_before_extraction_or_install(self):
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "ryzenadj"
            binary.write_bytes(b"previous helper")
            def wrong_archive(_url, out, timeout):
                out.write(b"untrusted replacement archive")
            with patch.object(tdp, "BIN_DIR", raw), \
                 patch.object(tdp, "BIN_PATH", str(binary)), \
                 patch.object(tdp.updater, "download_to", side_effect=wrong_archive), \
                 patch.object(tdp.tarfile, "open") as extract:
                with self.assertRaisesRegex(RuntimeError, "archive checksum mismatch"):
                    tdp._download_ryzenadj()
            extract.assert_not_called()
            self.assertEqual(binary.read_bytes(), b"previous helper")

    def test_ryzenadj_binary_pin_is_also_checked_after_archive_verification(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as packed:
            entry = tarfile.TarInfo("ryzenadj")
            entry.size = len(b"wrong binary")
            packed.addfile(entry, io.BytesIO(b"wrong binary"))
        payload = archive.getvalue()
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "ryzenadj"
            binary.write_bytes(b"previous helper")
            def fixture_archive(_url, out, timeout):
                out.write(payload)
            with patch.object(tdp, "BIN_DIR", raw), \
                 patch.object(tdp, "BIN_PATH", str(binary)), \
                 patch.object(tdp, "RYZENADJ_SHA256", hashlib.sha256(payload).hexdigest()), \
                 patch.object(tdp.updater, "download_to", side_effect=fixture_archive):
                with self.assertRaisesRegex(RuntimeError, "binary checksum mismatch"):
                    tdp._download_ryzenadj()
            self.assertEqual(binary.read_bytes(), b"previous helper")
            self.assertEqual([item.name for item in Path(raw).iterdir()], ["ryzenadj"])


if __name__ == "__main__":
    unittest.main()
