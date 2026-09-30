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

import asyncio
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

import httpx2

from karellen_qbo_mcp.login import (LoginError, browser_login, local_callback_address, parse_callback_target,
                                    open_browser_detached)
from karellen_qbo_mcp.oauth import OAuthClient
from qbo_test_support import DISCOVERY, DISCOVERY_URL, Recorder, json_response, make_tokens


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOAuthClient(OAuthClient):
    def __init__(self):
        super().__init__("the-id", "the-secret", DISCOVERY_URL,
                         http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(Recorder(json_response(200, DISCOVERY)))))
        self.exchanges = []

    async def exchange_code(self, code, redirect_uri, realm_id):
        self.exchanges.append((code, redirect_uri, realm_id))
        return make_tokens(realm_id=realm_id)


async def http_get(port, target):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(("GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n" % target).encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    return response.decode()


class DetachedBrowserTests(unittest.TestCase):
    def test_browser_runs_in_a_detached_process_without_our_stdio(self):
        with patch("karellen_qbo_mcp.login.subprocess.Popen") as popen:
            self.assertTrue(open_browser_detached("https://appcenter.intuit.com/connect/oauth2?x=1"))
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][-1], "https://appcenter.intuit.com/connect/oauth2?x=1")
        self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]),
                         (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL))
        self.assertTrue(kwargs["start_new_session"])

    def test_launch_failure_reported(self):
        with patch("karellen_qbo_mcp.login.subprocess.Popen", side_effect=OSError("no exec")):
            self.assertFalse(open_browser_detached("https://appcenter.intuit.com/connect/oauth2"))


class CallbackParsingTests(unittest.TestCase):
    def test_local_callback_address(self):
        self.assertEqual(local_callback_address("http://localhost:8765/callback"), ("localhost", 8765, "/callback"))
        self.assertEqual(local_callback_address("http://127.0.0.1:9000"), ("127.0.0.1", 9000, "/"))

    def test_non_local_redirect_rejected(self):
        for uri in ("https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl", "https://localhost:8765/callback",
                    "http://example.com:80/cb"):
            with self.subTest(uri=uri):
                with self.assertRaises(LoginError) as ctx:
                    local_callback_address(uri)
                self.assertIn("auth import", str(ctx.exception))

    def test_port_required(self):
        with self.assertRaises(LoginError):
            local_callback_address("http://localhost/callback")

    def test_parse_callback_target(self):
        p = parse_callback_target("/callback?code=AB11&state=xyz&realmId=4620816365")
        self.assertEqual((p.code, p.state, p.realm_id, p.error), ("AB11", "xyz", "4620816365", None))
        p = parse_callback_target("/callback?error=access_denied&error_description=User+denied&state=xyz")
        self.assertEqual((p.error, p.error_description, p.code), ("access_denied", "User denied", None))


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.redirect = "http://127.0.0.1:%d/callback" % self.port
        self.oauth = FakeOAuthClient()
        self.pages = []

    def login(self, callback, **kwargs):
        """Run browser_login with a browser stand-in that hits the listener via `callback(state)`."""
        async def main():
            def open_browser(url):
                state = parse_qs(urlsplit(url).query)["state"][0]
                self.opened = url

                async def visit():
                    for target in callback(state):
                        self.pages.append(await http_get(self.port, target))

                self.visit = asyncio.ensure_future(visit())
                return True

            try:
                return await browser_login(self.oauth, self.redirect, open_browser=open_browser, **kwargs)
            finally:
                await self.visit

        return asyncio.run(main())

    def test_successful_login(self):
        tokens = self.login(lambda state: ["/callback?code=C0DE&state=%s&realmId=123145" % state])
        self.assertEqual(tokens.realm_id, "123145")
        self.assertEqual(self.oauth.exchanges, [("C0DE", self.redirect, "123145")])
        self.assertIn("sign-in received", self.pages[0])
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1", self.opened)
        self.assertTrue(self.opened.startswith(DISCOVERY["authorization_endpoint"] + "?"), self.opened)

    def test_unrelated_path_ignored(self):
        tokens = self.login(lambda state: ["/favicon.ico", "/callback?code=C&state=%s&realmId=9" % state])
        self.assertIn("404 Not Found", self.pages[0])
        self.assertEqual(tokens.realm_id, "9")

    def test_state_mismatch_rejected(self):
        with self.assertRaises(LoginError) as ctx:
            self.login(lambda state: ["/callback?code=C&state=forged&realmId=9"])
        self.assertIn("state does not match", str(ctx.exception))
        self.assertEqual(self.oauth.exchanges, [])

    def test_denied_consent(self):
        with self.assertRaises(LoginError) as ctx:
            self.login(lambda state: ["/callback?error=access_denied&error_description=Denied&state=%s" % state])
        self.assertIn("Denied", str(ctx.exception))
        self.assertIn("sign-in failed", self.pages[0])

    def test_missing_realm(self):
        with self.assertRaises(LoginError):
            self.login(lambda state: ["/callback?code=C&state=%s" % state])

    def test_timeout(self):
        with self.assertRaises(LoginError) as ctx:
            self.login(lambda state: [], timeout=0.2)
        self.assertIn("No sign-in callback", str(ctx.exception))

    def test_announced_url_used_when_browser_unavailable(self):
        announced = []

        async def main():
            async def user_opens_link():
                while not announced:
                    await asyncio.sleep(0.01)
                state = parse_qs(urlsplit(announced[0]).query)["state"][0]
                # A client that connects and hangs up without a request must not break the listener.
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                self.pages.append(await http_get(self.port, "/callback?code=C&state=%s&realmId=42" % state))

            visit = asyncio.ensure_future(user_opens_link())
            try:
                return await browser_login(self.oauth, self.redirect, open_browser=lambda url: False,
                                           announce=announced.append)
            finally:
                await visit

        self.assertEqual(asyncio.run(main()).realm_id, "42")
        self.assertIn("sign-in received", self.pages[0])

    def test_browser_unavailable_without_announcer(self):
        async def main():
            return await browser_login(self.oauth, self.redirect, open_browser=lambda url: False)
        with self.assertRaises(LoginError) as ctx:
            asyncio.run(main())
        self.assertIn("Open this URL manually", str(ctx.exception))

    def test_port_in_use(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen()

            async def main():
                return await browser_login(self.oauth, self.redirect, open_browser=lambda url: True)
            with self.assertRaises(LoginError) as ctx:
                asyncio.run(main())
            self.assertIn("Cannot listen", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
