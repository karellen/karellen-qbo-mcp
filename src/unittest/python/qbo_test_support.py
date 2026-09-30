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

"""Shared fixtures for the unit tests: settings, tokens, a fake OAuth client and a recording HTTP transport."""

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import httpx2

from karellen_qbo_mcp.client import QboClient
from karellen_qbo_mcp.config import Settings
from karellen_qbo_mcp.oauth import OAuthError
from karellen_qbo_mcp.tokens import Tokens, TokenStore

NOW = 1_800_000_000.0
REALM = "9341455555555555"
BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company/%s/" % REALM


def make_settings(state_dir, environment="sandbox", read_only=False, client_id="cid", client_secret="csecret",
                  redirect_uri="http://localhost:8765/callback") -> Settings:
    return Settings(environment=environment, state_dir=Path(state_dir) / environment, client_id=client_id,
                    client_secret=client_secret, redirect_uri=redirect_uri, read_only=read_only, minor_version="75")


def make_tokens(label="1", now=NOW, access_ttl=3600.0, refresh_ttl=8640000.0, hard_ttl=157680000.0,
                realm_id=REALM) -> Tokens:
    return Tokens(realm_id=realm_id, access_token="access-%s" % label, access_expires_at=now + access_ttl,
                  refresh_token="refresh-%s" % label, refresh_expires_at=now + refresh_ttl,
                  refresh_hard_expires_at=None if hard_ttl is None else now + hard_ttl, obtained_at=now)


def make_store(settings) -> TokenStore:
    return TokenStore(settings.tokens_path)


def run(coro):
    return asyncio.run(coro)


@contextlib.contextmanager
def watch_token_lock():
    """Record every TokenStore save/clear as (operation, whether the store's lock was held)."""
    events = []
    held = []

    class Lock:
        async def __aenter__(self):
            held.append(True)

        async def __aexit__(self, *exc):
            held.pop()

    save, clear = TokenStore.save, TokenStore.clear

    def recording_save(store, tokens):
        events.append(("save", bool(held)))
        save(store, tokens)

    def recording_clear(store):
        events.append(("clear", bool(held)))
        clear(store)

    with patch.object(TokenStore, "lock", lambda store: Lock()), \
            patch.object(TokenStore, "save", recording_save), patch.object(TokenStore, "clear", recording_clear):
        yield events


class FakeOAuth:
    """Stands in for OAuthClient: each refresh returns the next labelled token pair, or raises."""

    def __init__(self, error: OAuthError | None = None):
        self.refreshes = []
        self.error = error

    async def refresh(self, refresh_token, realm_id):
        self.refreshes.append((refresh_token, realm_id))
        if self.error is not None:
            raise self.error
        return make_tokens(label="r%d" % len(self.refreshes))


class Recorder:
    """httpx2 transport handler returning queued responses and recording every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected request %s %s" % (request.method, request.url))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body_json(self, index=-1):
        return json.loads(self.requests[index].content)


def json_response(status, payload, headers=None) -> httpx2.Response:
    return httpx2.Response(status, json=payload, headers=headers)


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def make_client(settings, recorder, oauth=None, now=NOW, sleep=None, store=None) -> tuple[QboClient, TokenStore]:
    store = store or make_store(settings)
    client = QboClient(settings, oauth or FakeOAuth(), store,
                       http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
                       clock=lambda: now, sleep=sleep or FakeSleep(), min_request_interval=0.0)
    return client, store
