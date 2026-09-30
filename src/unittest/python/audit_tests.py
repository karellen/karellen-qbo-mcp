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

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from karellen_qbo_mcp.audit import AuditLog


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sandbox" / "audit.jsonl"
        self.log = AuditLog(self.path, "sandbox")

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_records_success_and_failure(self):
        self.log.record("create", "Customer", {"DisplayName": "Acme"}, realm_id="1", request_id="r1",
                        result={"Id": "58"})
        self.log.record("delete", "Invoice", {"Id": "9", "SyncToken": "0"}, realm_id="1", request_id="r2",
                        error="Stale object")
        first, second = self.entries()
        self.assertEqual((first["operation"], first["entity"], first["result"], first["environment"]),
                         ("create", "Customer", {"Id": "58"}, "sandbox"))
        self.assertNotIn("error", first)
        self.assertEqual((second["error"], second["request_id"]), ("Stale object", "r2"))
        self.assertNotIn("result", second)
        self.assertIn("T", first["timestamp"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_write_failure_is_logged_not_raised(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()  # a directory where the file should be
        with self.assertLogs("karellen_qbo_mcp.audit", level="ERROR"):
            self.log.record("create", "Customer", {}, result={})


if __name__ == "__main__":
    unittest.main()
