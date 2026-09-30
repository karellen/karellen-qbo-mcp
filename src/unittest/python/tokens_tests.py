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
import os
import stat
import tempfile
import unittest
from pathlib import Path

from karellen_qbo_mcp.tokens import Tokens, TokenStore, TokenStoreError
from qbo_test_support import NOW, make_tokens


class TokensTests(unittest.TestCase):
    def test_from_token_response(self):
        t = Tokens.from_token_response({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                        "x_refresh_token_expires_in": 8640000, "x_refresh_token_hard_expires_in": 157680000,
                                        "token_type": "bearer"}, 12345, NOW)
        self.assertEqual(t.realm_id, "12345")
        self.assertEqual(t.access_expires_at, NOW + 3600)
        self.assertEqual(t.refresh_expires_at, NOW + 8640000)
        self.assertEqual(t.refresh_hard_expires_at, NOW + 157680000)

    def test_from_token_response_without_hard_expiry(self):
        t = Tokens.from_token_response({"access_token": "a", "refresh_token": "r", "expires_in": "3600",
                                        "x_refresh_token_expires_in": "8640000"}, "1", NOW)
        self.assertIsNone(t.refresh_hard_expires_at)

    def test_from_token_response_malformed(self):
        with self.assertRaises(TokenStoreError):
            Tokens.from_token_response({"access_token": "a"}, "1", NOW)
        with self.assertRaises(TokenStoreError):
            Tokens.from_token_response({"access_token": "a", "refresh_token": "r", "expires_in": "soon",
                                        "x_refresh_token_expires_in": 1}, "1", NOW)

    def test_access_token_validity_has_margin(self):
        t = make_tokens(access_ttl=3600.0)
        self.assertTrue(t.access_token_valid(NOW))
        self.assertTrue(t.access_token_valid(NOW + 3600 - 121))
        self.assertFalse(t.access_token_valid(NOW + 3600 - 119))

    def test_refresh_token_validity(self):
        t = make_tokens(refresh_ttl=100.0, hard_ttl=50.0)
        self.assertTrue(t.refresh_token_valid(NOW + 49))
        self.assertFalse(t.refresh_token_valid(NOW + 50))
        t = make_tokens(refresh_ttl=100.0, hard_ttl=None)
        self.assertTrue(t.refresh_token_valid(NOW + 99))
        self.assertFalse(t.refresh_token_valid(NOW + 100))


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name) / "sandbox"
        self.store = TokenStore(d / "tokens.json")
        self.assertEqual(self.store.lock_path, d / "tokens.lock")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_store_loads_none(self):
        self.assertIsNone(self.store.load())

    def test_round_trip_and_permissions(self):
        self.store.save(make_tokens("1"))
        self.store.save(make_tokens("2"))
        self.assertEqual(self.store.load(), make_tokens("2"))
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.store.path.parent.iterdir()), ["tokens.json"])

    def test_clear(self):
        self.store.save(make_tokens())
        self.store.clear()
        self.assertIsNone(self.store.load())
        self.store.clear()

    def test_malformed_store(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text("{broken")
        with self.assertRaises(TokenStoreError):
            self.store.load()
        self.store.path.write_text('{"realm_id": "1"}')
        with self.assertRaises(TokenStoreError):
            self.store.load()

    def test_lock_is_exclusive_across_holders(self):
        events = []

        async def holder(name, delay):
            async with self.store.lock():
                events.append("enter-%s" % name)
                await asyncio.sleep(delay)
                events.append("exit-%s" % name)

        async def main():
            await asyncio.gather(asyncio.to_thread(asyncio.run, holder("a", 0.2)),
                                 asyncio.to_thread(asyncio.run, holder("b", 0.0)))

        asyncio.run(main())
        self.assertEqual(len(events), 4)
        self.assertTrue(events[0].startswith("enter-"))
        self.assertEqual(events[1], "exit-" + events[0][len("enter-"):])


if __name__ == "__main__":
    unittest.main()
