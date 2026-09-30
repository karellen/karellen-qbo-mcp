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
import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

import httpx2

from karellen_qbo_mcp.oauth import OAuthClient, OAuthError
from qbo_test_support import NOW, DISCOVERY, DISCOVERY_URL, Recorder, json_response

TOKEN_PAYLOAD = {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600,
                 "x_refresh_token_expires_in": 8640000, "x_refresh_token_hard_expires_in": 157000000,
                 "token_type": "bearer"}


class OAuthClientTests(unittest.TestCase):
    def client(self, *responses, discovery=True):
        """A client whose transport first serves the discovery document (unless `discovery` is False), then `responses`."""
        self.recorder = Recorder(*(((json_response(200, DISCOVERY),) if discovery else ()) + responses))
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.recorder))
        return OAuthClient("the-id", "the-secret", DISCOVERY_URL, http_client=http, clock=lambda: NOW)

    def form(self, index=-1):
        return {k: v[0] for k, v in parse_qs(self.recorder.requests[index].content.decode()).items()}

    def test_authorization_url(self):
        client = self.client()
        url = asyncio.run(client.authorization_url("http://localhost:8765/callback", "st4te"))
        parts = urlsplit(url)
        self.assertEqual("%s://%s%s" % (parts.scheme, parts.netloc, parts.path), DISCOVERY["authorization_endpoint"])
        self.assertEqual({k: v[0] for k, v in parse_qs(parts.query).items()}, {
            "client_id": "the-id", "response_type": "code", "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": "http://localhost:8765/callback", "state": "st4te"})

    def test_discovery_document_is_fetched_once_without_credentials(self):
        client = self.client(json_response(200, TOKEN_PAYLOAD), json_response(200, TOKEN_PAYLOAD), httpx2.Response(200))

        async def main():
            await client.authorization_url("http://localhost:8765/callback", "st4te")
            await client.exchange_code("the-code", "http://localhost:8765/callback", "4620816365")
            await client.refresh("old-refresh", "4620816365")
            await client.revoke("tok")

        asyncio.run(main())
        self.assertEqual([(r.method, str(r.url)) for r in self.recorder.requests], [
            ("GET", DISCOVERY_URL),
            ("POST", DISCOVERY["token_endpoint"]),
            ("POST", DISCOVERY["token_endpoint"]),
            ("POST", DISCOVERY["revocation_endpoint"])])
        self.assertNotIn("Authorization", self.recorder.requests[0].headers)
        self.assertEqual(self.recorder.requests[0].headers["Accept"], "application/json")

    def test_discovery_failure_is_reported_and_retried(self):
        client = self.client(json_response(503, {"error": "unavailable"}), json_response(200, DISCOVERY),
                             json_response(200, TOKEN_PAYLOAD), discovery=False)
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertIn("discovery document", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(asyncio.run(client.refresh("old", "1")).access_token, "acc")
        self.assertEqual([str(r.url) for r in self.recorder.requests],
                         [DISCOVERY_URL, DISCOVERY_URL, DISCOVERY["token_endpoint"]])

    def test_discovery_network_failure(self):
        client = self.client(httpx2.ConnectError("no route"), discovery=False)
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertIn("Cannot reach", str(ctx.exception))
        self.assertIn(DISCOVERY_URL, str(ctx.exception))

    def test_discovery_document_not_json(self):
        client = self.client(httpx2.Response(200, text="<html/>"), discovery=False)
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertIn("not JSON", str(ctx.exception))

    def test_discovery_document_without_usable_endpoints(self):
        documents = [
            ["not", "an", "object"],
            {k: v for k, v in DISCOVERY.items() if k != "token_endpoint"},
            dict(DISCOVERY, token_endpoint=42),
            dict(DISCOVERY, revocation_endpoint="http://developer.example.test/v2/oauth2/tokens/revoke"),
        ]
        for document in documents:
            with self.subTest(document=document):
                client = self.client(json_response(200, document), discovery=False)
                with self.assertRaises(OAuthError) as ctx:
                    asyncio.run(client.refresh("old", "1"))
                self.assertIn("has no HTTPS", str(ctx.exception))
                self.assertEqual(len(self.recorder.requests), 1)  # nothing sent to an unvetted endpoint

    def test_exchange_code(self):
        client = self.client(json_response(200, TOKEN_PAYLOAD))
        tokens = asyncio.run(client.exchange_code("the-code", "http://localhost:8765/callback", "4620816365"))
        request = self.recorder.requests[1]
        self.assertEqual(str(request.url), DISCOVERY["token_endpoint"])
        self.assertEqual(request.headers["Authorization"], "Basic " + base64.b64encode(b"the-id:the-secret").decode())
        self.assertEqual(request.headers["x-include-refresh-token-hard-expires-in"], "true")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(self.form(), {"grant_type": "authorization_code", "code": "the-code",
                                       "redirect_uri": "http://localhost:8765/callback"})
        self.assertEqual((tokens.realm_id, tokens.access_token, tokens.refresh_token), ("4620816365", "acc", "ref"))
        self.assertEqual(tokens.refresh_hard_expires_at, NOW + 157000000)

    def test_refresh(self):
        client = self.client(json_response(200, TOKEN_PAYLOAD))
        tokens = asyncio.run(client.refresh("old-refresh", "4620816365"))
        self.assertEqual(self.form(), {"grant_type": "refresh_token", "refresh_token": "old-refresh"})
        self.assertEqual(tokens.refresh_token, "ref")

    def test_invalid_grant(self):
        client = self.client(json_response(400, {"error": "invalid_grant", "error_description": "Token invalid"}))
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertTrue(ctx.exception.is_invalid_grant)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("sign in again", str(ctx.exception))

    def test_non_json_error(self):
        client = self.client(httpx2.Response(503, text="Service Unavailable"))
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertFalse(ctx.exception.is_invalid_grant)
        self.assertIn("Service Unavailable", str(ctx.exception))

    def test_json_error_that_is_not_an_object(self):
        client = self.client(json_response(500, ["internal", "error"]))
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertFalse(ctx.exception.is_invalid_grant)
        self.assertIn('(HTTP 500): ["internal","error"]', str(ctx.exception))

    def test_non_json_success(self):
        client = self.client(httpx2.Response(200, text="<html/>"))
        with self.assertRaises(OAuthError):
            asyncio.run(client.refresh("old", "1"))

    def test_incomplete_token_response(self):
        client = self.client(json_response(200, {"access_token": "a"}))
        with self.assertRaises(OAuthError):
            asyncio.run(client.refresh("old", "1"))

    def test_network_failure(self):
        client = self.client(httpx2.ConnectError("no route"))
        with self.assertRaises(OAuthError) as ctx:
            asyncio.run(client.refresh("old", "1"))
        self.assertIn("Cannot reach", str(ctx.exception))

    def test_default_http_client(self):
        recorder = Recorder(json_response(200, DISCOVERY), json_response(200, TOKEN_PAYLOAD))
        real_client = httpx2.AsyncClient

        def mock_client(**kwargs):
            self.assertEqual(kwargs, {"timeout": 60.0})
            return real_client(transport=httpx2.MockTransport(recorder))

        with patch("karellen_qbo_mcp.oauth.httpx2.AsyncClient", mock_client):
            tokens = asyncio.run(OAuthClient("the-id", "the-secret", DISCOVERY_URL, clock=lambda: NOW).refresh("old", "1"))
        self.assertEqual(tokens.access_token, "acc")
        self.assertEqual([str(r.url) for r in recorder.requests], [DISCOVERY_URL, DISCOVERY["token_endpoint"]])

    def test_revoke(self):
        client = self.client(httpx2.Response(200), json_response(400, {"error": "invalid_request"}))
        asyncio.run(client.revoke("tok"))
        request = self.recorder.requests[1]
        self.assertEqual(str(request.url), DISCOVERY["revocation_endpoint"])
        self.assertEqual(json.loads(request.content), {"token": "tok"})
        with self.assertRaises(OAuthError):
            asyncio.run(client.revoke("tok"))


if __name__ == "__main__":
    unittest.main()
