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

"""OAuth token persistence.

Intuit rotates the refresh token roughly every 24 hours and invalidates the previous
value, so the newest token must always be persisted, and refreshes must be serialized
across every process sharing the store (the lock file does that).
"""

import dataclasses
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from filelock import AsyncFileLock

from karellen_qbo_mcp.files import ensure_private_dir, write_private_file

# Refresh the access token this many seconds before it expires.
ACCESS_TOKEN_EXPIRY_MARGIN = 120.0

LOCK_TIMEOUT = 60.0


class TokenStoreError(Exception):
    pass


@dataclass(frozen=True)
class Tokens:
    realm_id: str
    access_token: str
    access_expires_at: float
    refresh_token: str
    refresh_expires_at: float
    refresh_hard_expires_at: float | None
    obtained_at: float

    @classmethod
    def from_token_response(cls, payload: dict, realm_id: str, now: float) -> "Tokens":
        try:
            hard = payload.get("x_refresh_token_hard_expires_in")
            return cls(
                realm_id=str(realm_id),
                access_token=payload["access_token"],
                access_expires_at=now + float(payload["expires_in"]),
                refresh_token=payload["refresh_token"],
                refresh_expires_at=now + float(payload["x_refresh_token_expires_in"]),
                refresh_hard_expires_at=None if hard is None else now + float(hard),
                obtained_at=now,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise TokenStoreError("Malformed token response: missing or invalid %s" % e) from e

    def access_token_valid(self, now: float) -> bool:
        return now < self.access_expires_at - ACCESS_TOKEN_EXPIRY_MARGIN

    def refresh_token_valid(self, now: float) -> bool:
        if self.refresh_hard_expires_at is not None and now >= self.refresh_hard_expires_at:
            return False
        return now < self.refresh_expires_at


def iso_time(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def load(self) -> Tokens | None:
        try:
            with open(self.path, "rb") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise TokenStoreError("Cannot read token store %s: %s" % (self.path, e)) from e
        try:
            return Tokens(**data)
        except TypeError as e:
            raise TokenStoreError("Token store %s is malformed: %s" % (self.path, e)) from e

    def save(self, tokens: Tokens):
        write_private_file(self.path, json.dumps(dataclasses.asdict(tokens), indent=2).encode("utf-8"))

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def save_locked(self, tokens: Tokens):
        """Save under the store lock, so a refresh running in another process cannot overwrite these tokens."""
        async with self.lock():
            self.save(tokens)

    def lock(self) -> AsyncFileLock:
        ensure_private_dir(self.lock_path.parent)
        return AsyncFileLock(str(self.lock_path), timeout=LOCK_TIMEOUT)


def auth_status(settings, store: TokenStore, now: float) -> dict:
    """The environment, whether writes are allowed, and the state of the stored authorization (no network calls)."""
    tokens = store.load()
    status = {
        "environment": settings.environment,
        "api_base_url": settings.api_base_url,
        "read_only": settings.read_only,
        "minor_version": settings.minor_version,
        "client_configured": bool(settings.client_id and settings.client_secret),
        "redirect_uri": settings.redirect_uri,
        "state_dir": str(settings.state_dir),
        "signed_in": tokens is not None,
    }
    if tokens is not None:
        status.update({
            "realm_id": tokens.realm_id,
            "access_token_expires_at": iso_time(tokens.access_expires_at),
            "refresh_token_expires_at": iso_time(tokens.refresh_expires_at),
            "refresh_token_hard_expires_at": iso_time(tokens.refresh_hard_expires_at),
            "refresh_token_valid": tokens.refresh_token_valid(now),
        })
    return status
