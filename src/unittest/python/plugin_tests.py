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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECK_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-prerequisites.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is not available")
class PrerequisiteHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def hook(self, *commands):
        """Run the hook with a PATH holding only the given commands (linked from the real PATH)."""
        for command in commands:
            os.symlink(shutil.which(command), self.bin / command)
        return subprocess.run([BASH, str(CHECK_SCRIPT)], env={"PATH": str(self.bin)}, capture_output=True, text=True)

    def test_silent_when_server_installed(self):
        (self.bin / "karellen-qbo-mcp").write_text("#!/bin/sh\n")
        (self.bin / "karellen-qbo-mcp").chmod(0o755)
        result = self.hook()
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_warns_without_jq(self):
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("karellen-qbo-mcp is not installed", result.stdout)
        self.assertIn("pip install karellen-qbo-mcp", result.stdout)

    @unittest.skipUnless(shutil.which("jq"), "jq is not available")
    def test_warns_as_hook_json_with_jq(self):
        result = self.hook("jq")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("karellen-qbo-mcp is not installed", output["systemMessage"])


if __name__ == "__main__":
    unittest.main()
