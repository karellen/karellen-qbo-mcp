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

"""Command line: run the MCP server (default) or manage QuickBooks credentials."""

import argparse
import asyncio
import getpass
import json
import os
import sys
import time

from karellen_qbo_mcp.config import (ENVIRONMENTS, ENV_ENVIRONMENT, ConfigError, client_config_path, load_settings,
                                     save_client_config, select_environment)
from karellen_qbo_mcp.login import LoginError, browser_login, is_local_redirect, pasted_login
from karellen_qbo_mcp.oauth import OAuthClient, OAuthError
from karellen_qbo_mcp.tokens import TokenStore, TokenStoreError, auth_status


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        value = getpass.getpass(prompt)
    else:
        value = sys.stdin.readline()
    value = value.strip()
    if not value:
        raise SystemExit("error: no value given")
    return value


def _store(settings) -> TokenStore:
    return TokenStore(settings.tokens_path)


def _oauth(settings) -> OAuthClient:
    client_id, client_secret = settings.require_client_credentials()
    return OAuthClient(client_id, client_secret, settings.discovery_url)


def cmd_configure(args):
    # Not load_settings(): it reads the client file, which may be the very thing being repaired.
    path = client_config_path()
    secret = _read_secret("Client secret for %s: " % args.client_id)
    save_client_config(path, args.client_id, secret, args.redirect_uri)
    print("Saved %s app credentials to %s" % (select_environment(), path))


def cmd_login(args):
    settings = load_settings()
    if not settings.redirect_uri:
        raise ConfigError("No redirect URI configured for the %s environment" % settings.environment)

    def announce(url):
        print("Open this URL to sign in to QuickBooks (%s):\n%s" % (settings.environment, url), file=sys.stderr)

    def read_redirect():
        print("After approving, your browser is sent to %s (the page itself need not load).\n"
              "Paste the full address from the address bar: " % settings.redirect_uri, end="", file=sys.stderr, flush=True)
        return sys.stdin.readline()

    kwargs = {"announce": announce}
    if args.no_browser:
        kwargs["open_browser"] = lambda url: False

    async def login():
        if is_local_redirect(settings.redirect_uri):
            tokens = await browser_login(_oauth(settings), settings.redirect_uri, **kwargs)
        else:
            tokens = await pasted_login(_oauth(settings), settings.redirect_uri, read_redirect, **kwargs)
        await _store(settings).save_locked(tokens)
        return tokens

    tokens = asyncio.run(login())
    print("Signed in to company (realm) %s in the %s environment" % (tokens.realm_id, settings.environment))


def cmd_import(args):
    settings = load_settings()
    refresh_token = _read_secret("Refresh token: ")

    async def import_token():
        # Refreshing right away validates the token and replaces it with a freshly rotated one.
        tokens = await _oauth(settings).refresh(refresh_token, args.realm_id)
        await _store(settings).save_locked(tokens)
        return tokens

    tokens = asyncio.run(import_token())
    print("Imported authorization for company (realm) %s in the %s environment" % (tokens.realm_id, settings.environment))


def cmd_status(args):
    settings = load_settings()
    print(json.dumps(auth_status(settings, _store(settings), time.time()), indent=2))


def cmd_logout(args):
    settings = load_settings()
    store = _store(settings)

    async def logout():
        # Under the lock, so a server refreshing meanwhile can neither rotate the token being revoked nor
        # write tokens back after they are deleted.
        async with store.lock():
            tokens = store.load()
            if tokens is not None:
                if not args.keep_remote:
                    await _oauth(settings).revoke(tokens.refresh_token)
                store.clear()
            return tokens

    tokens = asyncio.run(logout())
    if tokens is None:
        print("Not signed in to the %s environment" % settings.environment)
        return
    print("Signed out of company (realm) %s in the %s environment" % (tokens.realm_id, settings.environment))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karellen-qbo-mcp",
                                     description="MCP server for QuickBooks Online. Without a command, serves MCP over stdio.")
    parser.add_argument("--environment", choices=ENVIRONMENTS,
                        help="QuickBooks environment (default: $%s or sandbox)" % ENV_ENVIRONMENT)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Serve MCP over stdio (the default)")

    auth = commands.add_parser("auth", help="Manage Intuit app credentials and QuickBooks authorization")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)

    p = auth_commands.add_parser("configure", help="Store the Intuit app's client ID and secret "
                                                   "(secret read from the terminal or stdin)")
    p.add_argument("--client-id", required=True)
    p.add_argument("--redirect-uri", help="Redirect URI registered for the app (sandbox default: http://localhost:8765/callback)")
    p.set_defaults(func=cmd_configure)

    p = auth_commands.add_parser("login", help="Sign in through the browser (a localhost redirect URI is received "
                                               "directly; for any other, paste the address the browser lands on)")
    p.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead of opening a browser")
    p.set_defaults(func=cmd_login)

    p = auth_commands.add_parser("import", help="Import a refresh token obtained elsewhere, e.g. the Intuit OAuth 2.0 Playground "
                                                "(token read from the terminal or stdin)")
    p.add_argument("--realm-id", required=True, help="The QuickBooks company (realm) ID")
    p.set_defaults(func=cmd_import)

    p = auth_commands.add_parser("status", help="Show the stored authorization")
    p.set_defaults(func=cmd_status)

    p = auth_commands.add_parser("logout", help="Revoke the authorization at Intuit and delete it locally")
    p.add_argument("--keep-remote", action="store_true", help="Only delete the local tokens; do not revoke them at Intuit")
    p.set_defaults(func=cmd_logout)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.environment:
        os.environ[ENV_ENVIRONMENT] = args.environment
    if args.command in (None, "serve"):
        from karellen_qbo_mcp.server import serve
        serve()
        return 0
    try:
        args.func(args)
    except (ConfigError, LoginError, OAuthError, TokenStoreError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0
