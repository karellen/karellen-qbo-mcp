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

"""Run the real server as a stdio subprocess and talk to it on both MCP protocol eras.

Claude Code connects to stdio servers with the `initialize` handshake (2025-11-25) by
default, and with `server/discover` (2026-07-28) when MCP_PROTOCOL_NEGOTIATION=auto.
No QuickBooks credentials are needed: only tools that make no network calls are used.
"""

import asyncio
import os
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = {
    "qbo_auth_status", "qbo_auth_login", "qbo_list_entities", "qbo_get", "qbo_query", "qbo_cdc", "qbo_list_reports",
    "qbo_report", "qbo_download_pdf", "qbo_download_attachment", "qbo_create", "qbo_update", "qbo_delete",
    "qbo_deactivate", "qbo_void", "qbo_send", "qbo_batch", "qbo_upload_attachment",
}


class StdioProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = dict(os.environ, QBO_MCP_CONFIG_DIR=self._tmp.name, QBO_MCP_ENVIRONMENT="sandbox",
                   PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        for name in ("QBO_MCP_CLIENT_ID", "QBO_MCP_CLIENT_SECRET", "QBO_MCP_READ_ONLY"):
            env.pop(name, None)
        self.params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "import sys; from karellen_qbo_mcp.cli import main; sys.exit(main())"],
            env=env)

    def tearDown(self):
        self._tmp.cleanup()

    def exercise(self, mode):
        async def main():
            async with Client(self.params, mode=mode, read_timeout_seconds=60) as client:
                tools = await client.list_tools()
                status = await client.call_tool("qbo_auth_status", {})
                entities = await client.call_tool("qbo_list_entities", {})
                failure = await client.call_tool("qbo_get", {"entity": "Invoice", "id": "1"})
                return client.protocol_version, tools, status, entities, failure

        version, tools, status, entities, failure = asyncio.run(main())
        by_name = {t.name: t for t in tools.tools}
        self.assertEqual(set(by_name), EXPECTED_TOOLS)
        self.assertTrue(by_name["qbo_query"].annotations.read_only_hint)
        self.assertTrue(by_name["qbo_delete"].annotations.destructive_hint)
        self.assertFalse(status.is_error)
        self.assertEqual(status.structured_content["environment"], "sandbox")
        self.assertFalse(status.structured_content["signed_in"])
        self.assertFalse(entities.is_error)
        self.assertTrue(failure.is_error)
        self.assertIn("config:", failure.content[0].text)
        return version

    def test_legacy_handshake(self):
        self.assertEqual(self.exercise("legacy"), "2025-11-25")

    def test_modern_protocol(self):
        self.assertEqual(self.exercise("auto"), "2026-07-28")


if __name__ == "__main__":
    unittest.main()
