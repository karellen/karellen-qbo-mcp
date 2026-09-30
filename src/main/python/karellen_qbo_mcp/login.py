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

"""Browser sign-in through a one-shot localhost callback listener.

Intuit accepts plain-HTTP localhost redirect URIs only for development (sandbox) keys.
Production keys need an HTTPS redirect; for those, bootstrap the tokens in Intuit's
OAuth 2.0 Playground and import the refresh token instead.
"""

import asyncio
import html
import secrets
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qs

from karellen_qbo_mcp.oauth import OAuthClient
from karellen_qbo_mcp.tokens import Tokens

LOGIN_TIMEOUT = 300.0

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class LoginError(Exception):
    pass


def open_browser_detached(url: str) -> bool:
    """Open `url` from a separate process that shares none of our stdin/stdout/stderr.

    For the stdio MCP server, whose stdout carries the protocol: webbrowser.open starts launchers that inherit
    stdout (and may print to it), and console browsers take over the terminal and block until they exit.
    """
    try:
        subprocess.Popen([sys.executable, "-c", "import sys, webbrowser; webbrowser.open(sys.argv[1])", url],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class CallbackParams:
    code: str | None
    state: str | None
    realm_id: str | None
    error: str | None
    error_description: str | None


def local_callback_address(redirect_uri: str) -> tuple[str, int, str]:
    """Return (host, port, path) to listen on for `redirect_uri`, which must point at this machine."""
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname not in _LOCAL_HOSTS:
        raise LoginError(
            "Redirect URI %s is not a plain-HTTP localhost address, so this machine cannot receive the "
            "sign-in callback. Intuit only allows such URIs for sandbox keys. For production, obtain tokens "
            "in the Intuit OAuth 2.0 Playground and run `karellen-qbo-mcp --environment production auth import`."
            % redirect_uri)
    if parts.port is None:
        raise LoginError("Redirect URI %s must include an explicit port" % redirect_uri)
    return parts.hostname, parts.port, parts.path or "/"


def parse_callback_target(target: str) -> CallbackParams:
    query = parse_qs(urlsplit(target).query)

    def first(name):
        values = query.get(name)
        return values[0] if values else None

    return CallbackParams(code=first("code"), state=first("state"), realm_id=first("realmId"),
                          error=first("error"), error_description=first("error_description"))


def _page(title: str, message: str) -> bytes:
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body style=\"font-family: sans-serif; margin: 3em\"><h2>%s</h2><p>%s</p></body></html>"
            % (html.escape(title), html.escape(title), html.escape(message))).encode("utf-8")


async def _serve_callback(host: str, port: int, path: str, received: asyncio.Future) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
            while True:
                header = await asyncio.wait_for(reader.readline(), 10)
                if header in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            target = parts[1] if len(parts) >= 2 else ""
            if len(parts) < 2 or parts[0] != "GET" or urlsplit(target).path != path:
                status, body = "404 Not Found", _page("Not found", "This listener only handles the QuickBooks sign-in callback.")
            else:
                params = parse_callback_target(target)
                if params.error:
                    status, body = "200 OK", _page("QuickBooks sign-in failed",
                                                   params.error_description or params.error)
                else:
                    status, body = "200 OK", _page("QuickBooks sign-in received",
                                                   "You can close this window and return to your terminal or Claude Code.")
                if not received.done():
                    received.set_result(params)
            writer.write(("HTTP/1.1 %s\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n"
                          "Connection: close\r\n\r\n" % (status, len(body))).encode("latin-1") + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    # For "localhost" this binds every address it resolves to (IPv4 and IPv6); the browser may pick either.
    try:
        return await asyncio.start_server(handle, host, port)
    except OSError as e:
        raise LoginError("Cannot listen on %s:%d for the sign-in callback: %s" % (host, port, e)) from e


async def browser_login(oauth: OAuthClient, redirect_uri: str, open_browser=webbrowser.open,
                        timeout: float = LOGIN_TIMEOUT, announce=None) -> Tokens:
    """Run the authorization-code flow: open the browser, await the callback, exchange the code."""
    host, port, path = local_callback_address(redirect_uri)
    state = secrets.token_urlsafe(32)
    url = await oauth.authorization_url(redirect_uri, state)
    received = asyncio.get_running_loop().create_future()
    server = await _serve_callback(host, port, path, received)
    try:
        if announce is not None:
            announce(url)
        if not open_browser(url) and announce is None:
            raise LoginError("Could not open a browser. Open this URL manually: %s" % url)
        try:
            params = await asyncio.wait_for(received, timeout)
        except asyncio.TimeoutError as e:
            raise LoginError("No sign-in callback received within %d seconds" % timeout) from e
    finally:
        server.close()
        await server.wait_closed()

    if params.error:
        raise LoginError("Intuit sign-in failed: %s" % (params.error_description or params.error))
    if not params.state or not secrets.compare_digest(params.state, state):
        raise LoginError("Sign-in callback state does not match; the callback was not produced by this sign-in")
    if not params.code or not params.realm_id:
        raise LoginError("Sign-in callback is missing the authorization code or company (realm) ID")
    return await oauth.exchange_code(params.code, redirect_uri, params.realm_id)
