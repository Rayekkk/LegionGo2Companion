# SPDX-License-Identifier: BSD-3-Clause
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import controller_maintenance as maintenance


@unittest.skipUnless(os.name == 'posix', 'Protected maintenance filesystem requires POSIX')
class MaintenanceClaimTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'private'
        self.root.mkdir(mode=0o700)
        self.owner = os.geteuid()
        self.claims = []
    def tearDown(self):
        for claim in self.claims: claim.close()
        self.temporary.cleanup()
    def create(self):
        claim = maintenance.MaintenanceClaim._create(str(self.root), self.owner)
        self.claims.append(claim)
        return claim
    def present(self): return maintenance._present(str(self.root), self.owner)

    def test_exclusive_marker_survives_owner_fd_close_and_new_reader(self):
        self.assertFalse(self.present())
        claim = self.create(); self.assertTrue(self.present()); claim.close()
        self.assertTrue(self.present())
        with self.assertRaises(FileExistsError): self.create()
        with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=True)
        self.assertTrue(self.present())

    def test_same_owner_needs_exact_true_restoration_and_releases_once(self):
        claim = self.create()
        for value in (False, None, 1, 'true'):
            with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=value)
            self.assertTrue(self.present())
        claim.release(restored=True); self.assertFalse(self.present())
        with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=True)

    def test_replaced_token_cannot_be_cleared_by_old_owner(self):
        claim = self.create()
        marker = self.root / maintenance.NAME
        marker.write_text('{"schema":1,"token":"' + 'b' * 32 + '"}')
        with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=True)
        self.assertTrue(marker.exists()); self.assertTrue(self.present())

    def test_partial_publication_stays_closed_and_cannot_be_overwritten(self):
        with patch.object(maintenance.os, 'write', return_value=0):
            with self.assertRaises(maintenance.MaintenanceError): self.create()
        self.assertEqual((self.root / maintenance.NAME).stat().st_size, 0)
        self.assertTrue(self.present())
        with self.assertRaises(FileExistsError): self.create()

    def test_symlink_fifo_and_nonprivate_claim_fail_closed(self):
        marker = self.root / maintenance.NAME
        destination = Path(self.temporary.name) / 'unrelated'
        destination.write_text('unchanged')
        marker.symlink_to(destination); self.assertTrue(self.present()); marker.unlink()
        os.mkfifo(marker, 0o600); self.assertTrue(self.present()); marker.unlink()
        claim = self.create(); marker.chmod(0o644); self.assertTrue(self.present())
        with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=True)
        self.assertEqual(destination.read_text(), 'unchanged')

    def test_untrusted_parent_is_not_treated_as_trusted_absence(self):
        self.root.chmod(0o777)
        self.assertTrue(maintenance._present(str(self.root / 'absent'), self.owner))
        with self.assertRaises(maintenance.MaintenanceError): self.create()

    def test_directory_symlink_cannot_redirect_claim(self):
        link = Path(self.temporary.name) / 'link'
        link.symlink_to(self.root)
        self.assertTrue(maintenance._present(str(link), self.owner))
        with self.assertRaises(OSError): maintenance.MaintenanceClaim._create(str(link), self.owner)
        self.assertFalse((self.root / maintenance.NAME).exists())

    def test_fsync_failure_keeps_claim_for_inspection(self):
        with patch.object(maintenance.os, 'fsync', side_effect=OSError('Injected storage fault')):
            with self.assertRaises(OSError): self.create()
        self.assertTrue(self.present())
        with self.assertRaises(FileExistsError): self.create()

    def test_release_retries_own_successful_unlink_after_directory_sync_failure(self):
        claim=self.create()
        with patch.object(maintenance.os,'fsync',side_effect=OSError('Injected directory sync fault')):
            with self.assertRaises(OSError):claim.release(restored=True)
        self.assertFalse((self.root/maintenance.NAME).exists())
        self.assertTrue(claim._unlinked);self.assertFalse(claim._released)
        for proof in (False,None,1,'true'):
            with self.assertRaises(maintenance.MaintenanceError):claim.release(restored=proof)
        claim.release(restored=True)
        self.assertTrue(claim._released);self.assertIsNone(claim._parent)

    def test_repeated_directory_sync_failure_never_reports_release_complete(self):
        claim=self.create()
        with patch.object(maintenance.os,'fsync',side_effect=OSError('Persistent directory sync fault')):
            for _ in range(3):
                with self.assertRaises(OSError):claim.release(restored=True)
                self.assertFalse(claim._released);self.assertIsNotNone(claim._parent)
        self.assertFalse((self.root/maintenance.NAME).exists())

    def test_release_retry_never_removes_or_claims_a_foreign_replacement_is_clear(self):
        claim=self.create()
        with patch.object(maintenance.os,'fsync',side_effect=OSError('Injected directory sync fault')):
            with self.assertRaises(OSError):claim.release(restored=True)
        replacement=self.create();marker=self.root/maintenance.NAME
        captured=marker.read_bytes()
        with self.assertRaises(maintenance.MaintenanceError):claim.release(restored=True)
        self.assertEqual(marker.read_bytes(),captured);self.assertFalse(claim._released)
        replacement.release(restored=True)

    def test_actual_owner_process_exit_cannot_clear_or_replay_claim(self):
        source = str(Path(maintenance.__file__).resolve().parent)
        program = "import sys,os;sys.path.insert(0,sys.argv[1]);import controller_maintenance as m;m.MaintenanceClaim._create(sys.argv[2],os.geteuid());os._exit(23)"
        child = subprocess.run([sys.executable, '-B', '-W', 'error', '-c', program, source, str(self.root)],
            capture_output=True, timeout=5)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.assertTrue(self.present())
        with self.assertRaises(FileExistsError): self.create()

    def test_hardlinked_claim_cannot_be_released(self):
        claim = self.create()
        os.link(self.root / maintenance.NAME, self.root / 'other-link')
        self.assertTrue(self.present())
        with self.assertRaises(maintenance.MaintenanceError): claim.release(restored=True)
        self.assertTrue((self.root / 'other-link').exists())


if __name__ == '__main__': unittest.main()
