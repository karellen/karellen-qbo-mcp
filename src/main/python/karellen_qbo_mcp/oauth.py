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

"""Intuit OAuth 2.0 client: authorization URL, code exchange, refresh and revocation."""

import time
from urllib.parse import urlencode

import httpx2

from karellen_qbo_mcp.tokens import Tokens, TokenStoreError

# Discovery document keys of the endpoints this client uses.
AUTHORIZATION_ENDPOINT = "authorization_endpoint"
TOKEN_ENDPOINT = "token_endpoint"
REVOCATION_ENDPOINT = "revocation_endpoint"
ENDPOINTS = (AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT, REVOCATION_ENDPOINT)

ACCOUNTING_SCOPE = "com.intuit.quickbooks.accounting"

# Asks Intuit to report the refresh token's 5-year hard expiry (x_refresh_token_hard_expires_in).
HARD_EXPIRY_HEADER = "x-include-refresh-token-hard-expires-in"

HTTP_TIMEOUT = 60.0


class OAuthError(Exception):
    def __init__(self, message: str, error: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.error = error
        self.status_code = status_code

    @property
    def is_invalid_grant(self) -> bool:
        return self.error == "invalid_grant"


class OAuthClient:
    def __init__(self, client_id: str, client_secret: str, discovery_url: str, http_client: httpx2.AsyncClient | None = None,
                 clock=time.time):
        self.client_id = client_id
        self.discovery_url = discovery_url
        self._auth = httpx2.BasicAuth(client_id, client_secret)
        self._http = http_client
        self._clock = clock
        self._endpoints = None

    async def authorization_url(self, redirect_uri: str, state: str) -> str:
        return await self._endpoint(AUTHORIZATION_ENDPOINT) + "?" + urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": ACCOUNTING_SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        })

    async def exchange_code(self, code: str, redirect_uri: str, realm_id: str) -> Tokens:
        return await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }, realm_id)

    async def refresh(self, refresh_token: str, realm_id: str) -> Tokens:
        return await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, realm_id)

    async def revoke(self, token: str):
        response = await self._post(await self._endpoint(REVOCATION_ENDPOINT), json={"token": token})
        if response.status_code != 200:
            raise self._error(response, "Token revocation failed")

    async def _token_request(self, form: dict, realm_id: str) -> Tokens:
        url = await self._endpoint(TOKEN_ENDPOINT)
        now = self._clock()
        response = await self._post(url, data=form, headers={HARD_EXPIRY_HEADER: "true"})
        if response.status_code != 200:
            raise self._error(response, "Token request failed")
        try:
            payload = response.json()
        except ValueError as e:
            raise OAuthError("Token endpoint returned a non-JSON response", status_code=response.status_code) from e
        try:
            return Tokens.from_token_response(payload, realm_id, now)
        except TokenStoreError as e:
            raise OAuthError(str(e), status_code=response.status_code) from e

    async def _endpoint(self, name: str) -> str:
        """An endpoint from Intuit's discovery document, fetched on first use (a failed fetch is retried next time)."""
        if self._endpoints is None:
            response = await self._request("GET", self.discovery_url)
            if response.status_code != 200:
                raise self._error(response, "Fetching the Intuit discovery document %s failed" % self.discovery_url)
            try:
                document = response.json()
            except ValueError as e:
                raise OAuthError("Intuit discovery document %s is not JSON" % self.discovery_url,
                                 status_code=response.status_code) from e
            endpoints = {}
            for key in ENDPOINTS:
                url = document.get(key) if isinstance(document, dict) else None
                # The client secret and tokens are sent to these URLs.
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise OAuthError("Intuit discovery document %s has no HTTPS %s: %r" % (self.discovery_url, key, url))
                endpoints[key] = url
            self._endpoints = endpoints
        return self._endpoints[name]

    async def _post(self, url: str, **kwargs) -> httpx2.Response:
        return await self._request("POST", url, auth=self._auth, **kwargs)

    async def _request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        headers = {"Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        try:
            if self._http is not None:
                return await self._http.request(method, url, headers=headers, **kwargs)
            async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT) as http:
                return await http.request(method, url, headers=headers, **kwargs)
        except httpx2.HTTPError as e:
            raise OAuthError("Cannot reach Intuit OAuth endpoint %s: %s" % (url, e)) from e

    @staticmethod
    def _error(response: httpx2.Response, prefix: str) -> OAuthError:
        error = None
        description = response.text.strip()
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                description = payload.get("error_description") or error or description
        except ValueError:
            pass
        message = "%s (HTTP %d): %s" % (prefix, response.status_code, description or "no details")
        if error == "invalid_grant":
            message += ". The refresh token is expired or was revoked; sign in again."
        return OAuthError(message, error=error, status_code=response.status_code)
