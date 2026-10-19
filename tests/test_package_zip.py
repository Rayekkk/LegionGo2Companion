# SPDX-License-Identifier: BSD-3-Clause
"""The release ZIP writer gives byte-identical archives for the same tree and timestamp."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
WRITE = ("const m = await import(process.argv[1]);"
         "m.writeZip(process.argv[2], 'Top', process.argv[3], Number(process.argv[4]));")


@unittest.skipUnless(NODE, 'node is required')
class PackageZipTests(unittest.TestCase):
    def build(self, base, order, mtime):
        for name in order:
            path = base / 'Top' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode() * 50)
            os.utime(path, (mtime, mtime))
        out = base / 'out.zip'
        subprocess.run([NODE, '--input-type=module', '-e', WRITE, (ROOT / 'scripts/package.mjs').as_uri(),
                        str(base), str(out), '1760000000'], check=True, cwd=ROOT)
        return out

    def test_same_tree_gives_same_bytes(self):
        names = ['main.py', 'dist/desktop/app', 'b/z.txt', 'b/a.txt', 'empty/x']
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = self.build(Path(one), names, 1000000000)
            second = self.build(Path(two), list(reversed(names)), 1700000000)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                members = archive.infolist()
                self.assertEqual([m.filename for m in members], sorted(m.filename for m in members))
                self.assertEqual({m.date_time for m in members}, {(2025, 10, 9, 8, 53, 20)})
                for member in members:
                    self.assertEqual(member.external_attr >> 16, 0o40755 if member.is_dir() else 0o100644)
                self.assertEqual(archive.read('Top/b/a.txt'), b'b/a.txt' * 50)


if __name__ == '__main__':
    unittest.main()
