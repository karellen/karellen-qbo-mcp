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

"""Runtime settings: environment selection, credential and state file locations."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from karellen_qbo_mcp.files import write_private_file

SANDBOX = "sandbox"
PRODUCTION = "production"
ENVIRONMENTS = (SANDBOX, PRODUCTION)

API_BASE_URLS = {
    SANDBOX: "https://sandbox-quickbooks.api.intuit.com",
    PRODUCTION: "https://quickbooks.api.intuit.com",
}

# Intuit discontinued minor versions 1-74 on 2025-08-01; 75 is the minimum and the default.
DEFAULT_MINOR_VERSION = "75"

# Sandbox apps may redirect to plain-HTTP localhost; production apps may not.
DEFAULT_SANDBOX_REDIRECT_URI = "http://localhost:8765/callback"

ENV_ENVIRONMENT = "QBO_MCP_ENVIRONMENT"
ENV_CONFIG_DIR = "QBO_MCP_CONFIG_DIR"
ENV_CLIENT_ID = "QBO_MCP_CLIENT_ID"
ENV_CLIENT_SECRET = "QBO_MCP_CLIENT_SECRET"
ENV_REDIRECT_URI = "QBO_MCP_REDIRECT_URI"
ENV_READ_ONLY = "QBO_MCP_READ_ONLY"
ENV_MINOR_VERSION = "QBO_MCP_MINOR_VERSION"

CLIENT_FILE = "client.json"
TOKENS_FILE = "tokens.json"
AUDIT_FILE = "audit.jsonl"

_TRUE_VALUES = ("1", "true", "yes", "on")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    environment: str
    state_dir: Path
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    read_only: bool
    minor_version: str

    @property
    def api_base_url(self) -> str:
        return API_BASE_URLS[self.environment]

    @property
    def client_path(self) -> Path:
        return self.state_dir / CLIENT_FILE

    @property
    def tokens_path(self) -> Path:
        return self.state_dir / TOKENS_FILE

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_FILE

    def require_client_credentials(self) -> tuple[str, str]:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "No Intuit app credentials for the %s environment. Run "
                "`karellen-qbo-mcp --environment %s auth configure --client-id <ID>` "
                "or set %s and %s." % (self.environment, self.environment, ENV_CLIENT_ID, ENV_CLIENT_SECRET))
        return self.client_id, self.client_secret


def base_config_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get(ENV_CONFIG_DIR)
    if configured:
        return Path(configured).expanduser()
    return Path(platformdirs.user_config_dir("karellen-qbo-mcp", appauthor=False))


def select_environment(environ=None, environment=None) -> str:
    environ = os.environ if environ is None else environ
    environment = (environment or environ.get(ENV_ENVIRONMENT) or SANDBOX).strip().lower()
    if environment not in ENVIRONMENTS:
        raise ConfigError("Unknown QuickBooks environment %r; expected one of %s" % (environment, ", ".join(ENVIRONMENTS)))
    return environment


def client_config_path(environ=None, environment=None) -> Path:
    """Where `auth configure` stores the app credentials; found without reading any configuration file."""
    return base_config_dir(environ) / select_environment(environ, environment) / CLIENT_FILE


def load_settings(environ=None, environment=None) -> Settings:
    """Build settings from environment variables and the per-environment client file.

    Environment variables take precedence over values stored by `auth configure`.
    """
    environ = os.environ if environ is None else environ
    environment = select_environment(environ, environment)
    state_dir = base_config_dir(environ) / environment
    stored = read_client_config(state_dir / CLIENT_FILE)

    redirect_uri = environ.get(ENV_REDIRECT_URI) or stored.get("redirect_uri")
    if not redirect_uri and environment == SANDBOX:
        redirect_uri = DEFAULT_SANDBOX_REDIRECT_URI

    return Settings(
        environment=environment,
        state_dir=state_dir,
        client_id=environ.get(ENV_CLIENT_ID) or stored.get("client_id"),
        client_secret=environ.get(ENV_CLIENT_SECRET) or stored.get("client_secret"),
        redirect_uri=redirect_uri,
        read_only=environ.get(ENV_READ_ONLY, "").strip().lower() in _TRUE_VALUES,
        minor_version=environ.get(ENV_MINOR_VERSION) or DEFAULT_MINOR_VERSION,
    )


def read_client_config(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError("Cannot read %s: %s" % (path, e)) from e
    if not isinstance(data, dict):
        raise ConfigError("%s does not contain a JSON object" % path)
    return data


def save_client_config(path: Path, client_id: str, client_secret: str, redirect_uri: str | None):
    """Store app credentials. Without a redirect URI the stored one is kept; an unreadable file is replaced."""
    if not redirect_uri:
        try:
            redirect_uri = read_client_config(path).get("redirect_uri")
        except ConfigError:
            pass
    data = {"client_id": client_id, "client_secret": client_secret}
    if redirect_uri:
        data["redirect_uri"] = redirect_uri
    write_private_file(path, json.dumps(data, indent=2).encode("utf-8"))
