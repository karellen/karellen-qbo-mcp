#   -*- coding: utf-8 -*-
#   Copyright 2026 Karellen, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from karellen_qbo_mcp.files import PrivateFile, write_private_file, append_private_line


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "state"
        self.path = self.dir / "tokens.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_write_keeps_previous_content_and_leaves_no_temp_file(self):
        write_private_file(self.path, b"old")
        with patch("karellen_qbo_mcp.files.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_private_file(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tokens.json"])

    def test_failed_write_when_temp_file_already_gone(self):
        def replace_fails(src, dst):
            os.unlink(src)
            raise OSError("cross-device link")

        with patch("karellen_qbo_mcp.files.os.replace", side_effect=replace_fails):
            with self.assertRaises(OSError):
                write_private_file(self.path, b"new")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_concurrent_writes_use_distinct_temp_files(self):
        sources = []
        replace = os.replace

        def recording_replace(src, dst):
            sources.append(src)
            replace(src, dst)

        with patch("karellen_qbo_mcp.files.os.replace", side_effect=recording_replace):
            write_private_file(self.path, b"a")
            write_private_file(self.path, b"b")
        self.assertEqual(len(set(sources)), 2)

    def test_no_clobber_write(self):
        write_private_file(self.path, b"first", overwrite=False)
        self.assertEqual(self.path.read_bytes(), b"first")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            write_private_file(self.path, b"second", overwrite=False)
        self.assertEqual(self.path.read_bytes(), b"first")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tokens.json"])

    def test_no_clobber_write_without_hard_links(self):
        with patch("karellen_qbo_mcp.files.os.link", side_effect=PermissionError("hard links not supported")):
            write_private_file(self.path, b"first", overwrite=False)
            self.assertEqual(self.path.read_bytes(), b"first")
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_private_file(self.path, b"second", overwrite=False)
            self.assertEqual(self.path.read_bytes(), b"first")
            with patch("karellen_qbo_mcp.files.os.fsync", side_effect=[None, OSError("disk full")]):
                with self.assertRaises(OSError):
                    write_private_file(self.dir / "other.json", b"x", overwrite=False)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tokens.json"])

    def test_private_file_written_in_pieces_appears_only_on_commit(self):
        with PrivateFile(self.path, overwrite=False) as f:
            f.write(b"one,")
            f.write(b"two")
            self.assertFalse(self.path.exists())
        self.assertEqual(f.size, 7)
        self.assertEqual(self.path.read_bytes(), b"one,two")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            with PrivateFile(self.path, overwrite=False) as f:
                f.write(b"three")
        with PrivateFile(self.path) as f:
            f.write(b"four")
        self.assertEqual(self.path.read_bytes(), b"four")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tokens.json"])

    def test_private_file_discarded_when_writing_fails(self):
        with self.assertRaises(RuntimeError):
            with PrivateFile(self.path) as f:
                f.write(b"partial")
                raise RuntimeError("source failed")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_append_creates_private_file_and_adds_lines(self):
        append_private_line(self.path, "first\n")
        append_private_line(self.path, "second")
        self.assertEqual(self.path.read_text(), "first\nsecond\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
