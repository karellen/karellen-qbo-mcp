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

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import karellen_qbo_mcp.cli as cli
import karellen_qbo_mcp.server as server
from karellen_qbo_mcp.config import load_settings
from karellen_qbo_mcp.oauth import OAuthError
from qbo_test_support import make_store, make_tokens, watch_token_lock


class FakeStdin(io.StringIO):
    def isatty(self):
        return False


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QBO_MCP_CONFIG_DIR": self._tmp.name}, clear=False)
        self.env.start()
        for name in ("QBO_MCP_ENVIRONMENT", "QBO_MCP_CLIENT_ID", "QBO_MCP_CLIENT_SECRET", "QBO_MCP_REDIRECT_URI"):
            os.environ.pop(name, None)
        server._runtime = None

    def tearDown(self):
        server._runtime = None
        self.env.stop()
        self._tmp.cleanup()

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", FakeStdin(stdin)), patch("sys.stdout", out), patch("sys.stderr", err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def store(self, environment="sandbox"):
        return make_store(load_settings(environment=environment))

    def configure(self, environment="sandbox"):
        return self.cli("--environment", environment, "auth", "configure", "--client-id", "the-id", stdin="the-secret\n")

    def test_configure_reads_secret_from_stdin(self):
        code, out, _ = self.cli("--environment", "production", "auth", "configure", "--client-id", "prod-id",
                                "--redirect-uri", "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl",
                                stdin="prod-secret\n")
        self.assertEqual(code, 0)
        data = json.loads((Path(self._tmp.name) / "production" / "client.json").read_text())
        self.assertEqual(data, {"client_id": "prod-id", "client_secret": "prod-secret",
                                "redirect_uri": "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"})
        self.assertIn("production", out)

    def test_secret_prompted_on_terminal(self):
        class TtyStdin(io.StringIO):
            def isatty(self):
                return True

        out = io.StringIO()
        with patch("sys.stdin", TtyStdin()), patch("sys.stdout", out), \
                patch.object(cli.getpass, "getpass", return_value=" typed-secret ") as prompt:
            self.assertEqual(cli.main(["auth", "configure", "--client-id", "the-id"]), 0)
        prompt.assert_called_once_with("Client secret for the-id: ")
        self.assertEqual(load_settings().client_secret, "typed-secret")

    def test_configure_keeps_stored_redirect_uri(self):
        self.cli("auth", "configure", "--client-id", "old-id", "--redirect-uri", "http://localhost:9000/cb", stdin="old\n")
        code, _, _ = self.cli("auth", "configure", "--client-id", "new-id", stdin="new\n")
        self.assertEqual(code, 0)
        settings = load_settings()
        self.assertEqual((settings.client_id, settings.client_secret, settings.redirect_uri),
                         ("new-id", "new", "http://localhost:9000/cb"))

    def test_configure_repairs_unreadable_client_file(self):
        client_file = Path(self._tmp.name) / "sandbox" / "client.json"
        client_file.parent.mkdir(parents=True)
        client_file.write_text("{truncated")
        code, _, err = self.cli("auth", "configure", "--client-id", "the-id", stdin="the-secret\n")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(load_settings().client_id, "the-id")

    def test_empty_secret_rejected(self):
        with self.assertRaises(SystemExit):
            self.cli("auth", "configure", "--client-id", "x", stdin="\n")

    def test_import_refreshes_and_stores(self):
        self.configure("production")
        calls = []

        async def fake_refresh(oauth, refresh_token, realm_id):
            calls.append((oauth.client_id, refresh_token, realm_id))
            return make_tokens(label="rotated", realm_id=realm_id)

        with patch("karellen_qbo_mcp.oauth.OAuthClient.refresh", fake_refresh), watch_token_lock() as events:
            code, out, _ = self.cli("--environment", "production", "auth", "import", "--realm-id", "4620816365",
                                    stdin="playground-refresh-token\n")
        self.assertEqual(code, 0)
        self.assertEqual(events, [("save", True)])
        self.assertEqual(calls, [("the-id", "playground-refresh-token", "4620816365")])
        self.assertEqual(self.store("production").load().refresh_token, "refresh-rotated")
        self.assertIsNone(self.store("sandbox").load())

    def test_import_failure_reported(self):
        self.configure()

        async def fake_refresh(oauth, refresh_token, realm_id):
            raise OAuthError("Token request failed (HTTP 400): invalid_grant", error="invalid_grant")

        with patch("karellen_qbo_mcp.oauth.OAuthClient.refresh", fake_refresh):
            code, _, err = self.cli("auth", "import", "--realm-id", "1", stdin="bad\n")
        self.assertEqual(code, 1)
        self.assertIn("invalid_grant", err)

    def test_import_without_credentials(self):
        code, _, err = self.cli("auth", "import", "--realm-id", "1", stdin="tok\n")
        self.assertEqual(code, 1)
        self.assertIn("auth configure", err)

    def test_login(self):
        self.configure()
        seen = {}

        async def fake_login(oauth, redirect_uri, announce=None, open_browser=None):
            seen.update(redirect_uri=redirect_uri, no_browser=open_browser is not None and not open_browser("u"))
            announce("https://appcenter.intuit.com/connect/oauth2?x")
            return make_tokens(realm_id="555")

        with patch.object(cli, "browser_login", fake_login), watch_token_lock() as events:
            code, out, err = self.cli("auth", "login", "--no-browser")
        self.assertEqual(code, 0)
        self.assertEqual(events, [("save", True)])
        self.assertEqual(seen, {"redirect_uri": "http://localhost:8765/callback", "no_browser": True})
        self.assertIn("https://appcenter.intuit.com", err)
        self.assertEqual(self.store().load().realm_id, "555")

    def paste_login(self, pasted):
        redirect = "https://karellen.example/qbo-callback"
        self.cli("--environment", "production", "auth", "configure", "--client-id", "prod-id", "--redirect-uri", redirect,
                 stdin="prod-secret\n")
        exchanges = []

        async def authorization_url(oauth, redirect_uri, state):
            return "https://appcenter.intuit.com/connect/oauth2?redirect_uri=%s&state=%s" % (redirect_uri, state)

        async def exchange_code(oauth, code, redirect_uri, realm_id):
            exchanges.append((code, redirect_uri, realm_id))
            return make_tokens(realm_id=realm_id)

        with patch("karellen_qbo_mcp.oauth.OAuthClient.authorization_url", authorization_url), \
                patch("karellen_qbo_mcp.oauth.OAuthClient.exchange_code", exchange_code), \
                patch("karellen_qbo_mcp.login.secrets.token_urlsafe", return_value="st8"), \
                watch_token_lock() as events:
            result = self.cli("--environment", "production", "auth", "login", "--no-browser",
                              stdin=pasted.format(redirect=redirect))
        return result, exchanges, events

    def test_login_with_https_redirect_reads_pasted_address(self):
        (code, out, err), exchanges, events = self.paste_login("{redirect}?code=C0DE&state=st8&realmId=4620816365\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(exchanges, [("C0DE", "https://karellen.example/qbo-callback", "4620816365")])
        self.assertEqual(events, [("save", True)])
        self.assertIn("https://appcenter.intuit.com/connect/oauth2?", err)
        self.assertIn("Paste the full address", err)
        self.assertIn("4620816365", out)
        self.assertEqual(self.store("production").load().realm_id, "4620816365")

    def test_login_with_https_redirect_rejects_foreign_state(self):
        (code, _, err), exchanges, events = self.paste_login("{redirect}?code=C0DE&state=other&realmId=4620816365\n")
        self.assertEqual(code, 1)
        self.assertIn("state does not match", err)
        self.assertEqual((exchanges, events), ([], []))
        self.assertIsNone(self.store("production").load())

    def test_login_with_https_redirect_nothing_pasted(self):
        (code, _, err), exchanges, _ = self.paste_login("")
        self.assertEqual(code, 1)
        self.assertIn("not an address under the redirect URI", err)
        self.assertEqual(exchanges, [])

    def test_login_without_redirect(self):
        self.configure("production")
        code, _, err = self.cli("--environment", "production", "auth", "login")
        self.assertEqual(code, 1)
        self.assertIn("redirect URI", err)

    def test_status(self):
        self.store().save(make_tokens())
        code, out, _ = self.cli("auth", "status")
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertTrue(status["signed_in"])
        self.assertEqual(status["environment"], "sandbox")

    def test_logout_revokes_then_clears(self):
        self.configure()
        self.store().save(make_tokens())
        revoked = []

        async def fake_revoke(oauth, token):
            revoked.append(token)

        with patch("karellen_qbo_mcp.oauth.OAuthClient.revoke", fake_revoke), watch_token_lock() as events:
            code, out, _ = self.cli("auth", "logout")
        self.assertEqual((code, revoked), (0, ["refresh-1"]))
        self.assertEqual(events, [("clear", True)])
        self.assertIsNone(self.store().load())
        code, out, _ = self.cli("auth", "logout")
        self.assertIn("Not signed in", out)

    def test_logout_keep_remote(self):
        self.store().save(make_tokens())
        code, _, _ = self.cli("auth", "logout", "--keep-remote")
        self.assertEqual(code, 0)
        self.assertIsNone(self.store().load())

    def test_default_command_serves(self):
        with patch("karellen_qbo_mcp.server.serve") as serve:
            self.assertEqual(self.cli("--environment", "production")[0], 0)
            serve.assert_called_once_with()
            self.assertEqual(os.environ["QBO_MCP_ENVIRONMENT"], "production")
            self.cli("serve")
            self.assertEqual(serve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
