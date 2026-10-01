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

from karellen_qbo_mcp.config import (ConfigError, load_settings, save_client_config, DEFAULT_SANDBOX_REDIRECT_URI,
                                     base_config_dir, client_config_path)


class LoadSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.environ = {"QBO_MCP_CONFIG_DIR": self._tmp.name}

    def tearDown(self):
        self._tmp.cleanup()

    def write_client(self, environment, data):
        d = self.base / environment
        d.mkdir(parents=True)
        (d / "client.json").write_text(json.dumps(data))

    def test_defaults_to_sandbox(self):
        s = load_settings(self.environ)
        self.assertEqual(s.environment, "sandbox")
        self.assertEqual(s.api_base_url, "https://sandbox-quickbooks.api.intuit.com")
        self.assertEqual(s.discovery_url, "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration")
        self.assertEqual(s.redirect_uri, DEFAULT_SANDBOX_REDIRECT_URI)
        self.assertEqual(s.state_dir, self.base / "sandbox")
        self.assertEqual(s.errors_path, self.base / "sandbox" / "errors.jsonl")
        self.assertEqual(s.minor_version, "75")
        self.assertFalse(s.read_only)
        self.assertIsNone(s.client_id)

    def test_production_has_no_default_redirect(self):
        s = load_settings(dict(self.environ, QBO_MCP_ENVIRONMENT="Production"))
        self.assertEqual(s.environment, "production")
        self.assertEqual(s.api_base_url, "https://quickbooks.api.intuit.com")
        self.assertEqual(s.discovery_url, "https://developer.api.intuit.com/.well-known/openid_configuration")
        self.assertIsNone(s.redirect_uri)

    def test_empty_environment_means_sandbox(self):
        # The plugin passes its environment option through, empty when unset.
        self.assertEqual(load_settings(dict(self.environ, QBO_MCP_ENVIRONMENT="")).environment, "sandbox")

    def test_explicit_environment_overrides_variable(self):
        s = load_settings(dict(self.environ, QBO_MCP_ENVIRONMENT="production"), environment="sandbox")
        self.assertEqual(s.environment, "sandbox")

    def test_unknown_environment(self):
        with self.assertRaises(ConfigError):
            load_settings(dict(self.environ, QBO_MCP_ENVIRONMENT="staging"))

    def test_client_file_per_environment(self):
        self.write_client("sandbox", {"client_id": "sb-id", "client_secret": "sb-secret"})
        self.write_client("production", {"client_id": "prod-id", "client_secret": "prod-secret",
                                         "redirect_uri": "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"})
        sandbox = load_settings(self.environ)
        production = load_settings(self.environ, environment="production")
        self.assertEqual((sandbox.client_id, sandbox.client_secret), ("sb-id", "sb-secret"))
        self.assertEqual((production.client_id, production.client_secret), ("prod-id", "prod-secret"))
        self.assertEqual(production.redirect_uri, "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl")

    def test_environment_variables_override_client_file(self):
        self.write_client("sandbox", {"client_id": "file-id", "client_secret": "file-secret",
                                      "redirect_uri": "http://localhost:1/cb"})
        s = load_settings(dict(self.environ, QBO_MCP_CLIENT_ID="env-id", QBO_MCP_CLIENT_SECRET="env-secret",
                               QBO_MCP_REDIRECT_URI="http://localhost:2/cb", QBO_MCP_MINOR_VERSION="76"))
        self.assertEqual((s.client_id, s.client_secret, s.redirect_uri, s.minor_version),
                         ("env-id", "env-secret", "http://localhost:2/cb", "76"))

    def test_read_only_values(self):
        for value, expected in (("1", True), ("TRUE", True), ("true", True), ("yes", True), ("on", True), ("0", False),
                                ("", False), ("no", False), ("false", False)):
            with self.subTest(value=value):
                self.assertEqual(load_settings(dict(self.environ, QBO_MCP_READ_ONLY=value)).read_only, expected)

    def test_malformed_client_file(self):
        d = self.base / "sandbox"
        d.mkdir(parents=True)
        (d / "client.json").write_text("[1, 2]")
        with self.assertRaises(ConfigError):
            load_settings(self.environ)
        (d / "client.json").write_text("{not json")
        with self.assertRaises(ConfigError):
            load_settings(self.environ)

    def test_require_client_credentials(self):
        with self.assertRaises(ConfigError) as ctx:
            load_settings(self.environ).require_client_credentials()
        self.assertIn("auth configure", str(ctx.exception))
        s = load_settings(dict(self.environ, QBO_MCP_CLIENT_ID="a", QBO_MCP_CLIENT_SECRET="b"))
        self.assertEqual(s.require_client_credentials(), ("a", "b"))

    def test_save_client_config_is_private(self):
        s = load_settings(self.environ)
        save_client_config(s.client_path, "id-1", "secret-1", "http://localhost:9000/callback")
        self.assertEqual(stat.S_IMODE(os.stat(s.client_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(s.state_dir).st_mode), 0o700)
        reloaded = load_settings(self.environ)
        self.assertEqual((reloaded.client_id, reloaded.client_secret, reloaded.redirect_uri),
                         ("id-1", "secret-1", "http://localhost:9000/callback"))

    def test_save_client_config_without_redirect(self):
        s = load_settings(self.environ, environment="production")
        save_client_config(s.client_path, "id-1", "secret-1", None)
        self.assertNotIn("redirect_uri", json.loads(s.client_path.read_text()))

    def test_client_config_path_needs_no_readable_config(self):
        s = load_settings(self.environ, environment="production")
        s.client_path.parent.mkdir(parents=True)
        s.client_path.write_text("[not an object]")
        self.assertEqual(client_config_path(self.environ, "production"), s.client_path)
        with self.assertRaises(ConfigError):
            client_config_path(self.environ, "staging")

    def test_base_config_dir_default(self):
        self.assertTrue(str(base_config_dir({})).endswith("karellen-qbo-mcp"))


if __name__ == "__main__":
    unittest.main()
