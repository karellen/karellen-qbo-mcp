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

"""Async client for the QuickBooks Online Accounting API (v3).

Every request carries the configured minorversion. Reads, queries and writes are all
retry-safe: writes carry a `requestid`, which QuickBooks uses to de-duplicate a
repeated request instead of applying it twice.
"""

import json
import logging
import re
import time
import uuid
from urllib.parse import quote

import anyio
import httpx2

from karellen_qbo_mcp.config import Settings
from karellen_qbo_mcp.entities import EntitySpec, VOID_OPERATION, require_capability
from karellen_qbo_mcp.oauth import OAuthClient, OAuthError
from karellen_qbo_mcp.tokens import Tokens, TokenStore

# httpx2 logs every request URL at INFO, and attachment download URLs carry temporary credentials.
logging.getLogger("httpx2").setLevel(logging.WARNING)

HTTP_TIMEOUT = 130.0              # QuickBooks times requests out after 120 seconds
MIN_REQUEST_INTERVAL = 0.1        # 10 requests per second per realm and app
THROTTLE_WAIT = 60.0              # Intuit asks clients to wait 60 seconds after HTTP 429
MAX_THROTTLE_RETRIES = 2
TRANSIENT_STATUSES = (500, 502, 503, 504)
MAX_TRANSIENT_RETRIES = 3
QUERY_PAGE_SIZE = 1000            # the most rows one query can return
BATCH_LIMIT = 30
STALE_OBJECT_CODE = "5010"

REALM_PLACEHOLDER = "{realm}"     # replaced in request paths by the signed-in company's realm id

_PAGING_CLAUSE = re.compile(r"\b(STARTPOSITION|MAXRESULTS)\b", re.IGNORECASE)
_COUNT_QUERY = re.compile(r"^\s*SELECT\s+COUNT\s*\(", re.IGNORECASE)


class QboError(Exception):
    pass


class QboAuthError(QboError):
    """Not signed in, or the stored authorization can no longer be refreshed."""


class QboApiError(QboError):
    def __init__(self, message: str, status_code: int, errors: list, fault_type: str | None, intuit_tid: str | None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors
        self.fault_type = fault_type
        self.intuit_tid = intuit_tid


def _find_fault(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("Fault", "fault"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return None


def fault_to_errors(fault: dict) -> tuple[list, str | None]:
    raw = fault.get("Error") or fault.get("error") or []
    errors = []
    for e in raw if isinstance(raw, list) else [raw]:
        if isinstance(e, dict):
            errors.append({
                "message": e.get("Message") or e.get("message"),
                "detail": e.get("Detail") or e.get("detail"),
                "code": e.get("code"),
                "element": e.get("element"),
            })
    return errors, fault.get("type")


def _format_errors(errors: list) -> str:
    parts = []
    for e in errors:
        text = e.get("message") or "error"
        if e.get("detail") and e.get("detail") != e.get("message"):
            text += ": %s" % e["detail"]
        extra = ", ".join("%s %s" % (k, e[k]) for k in ("code", "element") if e.get(k))
        if extra:
            text += " [%s]" % extra
        parts.append(text)
    return "; ".join(parts)


def api_error(status_code: int, errors: list, fault_type: str | None, intuit_tid: str | None,
              hint: str | None = None) -> QboApiError:
    message = "QuickBooks API error (HTTP %d%s): %s" % (
        status_code, ", %s" % fault_type if fault_type else "", _format_errors(errors))
    if intuit_tid:
        message += " (intuit_tid %s)" % intuit_tid
    if hint is None and any(str(e.get("code")) == STALE_OBJECT_CODE for e in errors):
        hint = "The record changed since its SyncToken was read; re-read it and retry with the current SyncToken."
    if hint:
        message += ". " + hint
    return QboApiError(message, status_code, errors, fault_type, intuit_tid)


def _fault_error(fault: dict, response: httpx2.Response) -> QboApiError:
    errors, fault_type = fault_to_errors(fault)
    return api_error(response.status_code, errors, fault_type, response.headers.get("intuit_tid"))


class QboClient:
    def __init__(self, settings: Settings, oauth: OAuthClient, token_store: TokenStore,
                 http_client: httpx2.AsyncClient | None = None, clock=time.time, sleep=anyio.sleep,
                 min_request_interval: float = MIN_REQUEST_INTERVAL):
        self.settings = settings
        self._oauth = oauth
        self._store = token_store
        self._http = http_client
        self._owns_http = http_client is None
        self._clock = clock
        self._sleep = sleep
        self._min_interval = min_request_interval
        self._next_slot = 0.0
        self._pace_lock = anyio.Lock()
        self._refresh_lock = anyio.Lock()
        self.realm_id = None  # of the last tokens used; for the audit log

    async def aclose(self):
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- Authorization -------------------------------------------------------

    async def tokens(self) -> Tokens:
        """Stored tokens with a usable access token, refreshing it when it is about to expire."""
        tokens = self._store.load()
        if tokens is None:
            raise QboAuthError(self._not_signed_in())
        self.realm_id = tokens.realm_id
        if tokens.access_token_valid(self._clock()):
            return tokens
        return await self._refresh(tokens)

    async def _refresh(self, stale: Tokens) -> Tokens:
        # The in-process lock orders coroutines; the file lock orders processes sharing the store.
        async with self._refresh_lock:
            async with self._store.lock():
                current = self._store.load()
                if current is None:
                    raise QboAuthError(self._not_signed_in())
                now = self._clock()
                if current.access_token != stale.access_token and current.access_token_valid(now):
                    return current  # someone else refreshed while we waited
                if not current.refresh_token_valid(now):
                    raise QboAuthError("The stored QuickBooks authorization for the %s environment has expired; sign in again."
                                       % self.settings.environment)
                try:
                    fresh = await self._oauth.refresh(current.refresh_token, current.realm_id)
                except OAuthError as e:
                    if e.is_invalid_grant:
                        raise QboAuthError(str(e)) from e
                    raise QboError(str(e)) from e
                self._store.save(fresh)
                return fresh

    def _not_signed_in(self) -> str:
        return ("Not signed in to QuickBooks (%s environment). Use the qbo_auth_login tool (sandbox), or run "
                "`karellen-qbo-mcp --environment %s auth login` in a terminal."
                % (self.settings.environment, self.settings.environment))

    # --- Transport -----------------------------------------------------------

    def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = httpx2.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._http

    async def _pace(self):
        async with self._pace_lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await self._sleep(wait)
            self._next_slot = max(now, self._next_slot) + self._min_interval

    async def request(self, method: str, path: str, *, params: dict | None = None, json_body=None,
                      content: bytes | None = None, content_type: str | None = None, files=None,
                      accept: str = "application/json", request_id: str | None = None) -> httpx2.Response:
        tokens = await self.tokens()
        realm = quote(tokens.realm_id, safe="")
        url = "%s/v3/company/%s/%s" % (self.settings.api_base_url, realm, path.replace(REALM_PLACEHOLDER, realm))
        query = {"minorversion": self.settings.minor_version}
        query.update(params or {})
        if request_id:
            query["requestid"] = request_id

        refreshed = False
        throttled = 0
        transient = 0
        while True:
            await self._pace()
            headers = {"Authorization": "Bearer %s" % tokens.access_token, "Accept": accept}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                response = await self._client().request(method, url, params=query, json=json_body, content=content,
                                                        files=files, headers=headers)
            except httpx2.TransportError as e:
                if transient < MAX_TRANSIENT_RETRIES:
                    transient += 1
                    await self._sleep(2 ** (transient - 1))
                    continue
                raise QboError("Cannot reach QuickBooks at %s: %s" % (self.settings.api_base_url, e)) from e

            status = response.status_code
            if status == 401 and not refreshed:
                refreshed = True
                tokens = await self._refresh(tokens)
                continue
            if status == 429 and throttled < MAX_THROTTLE_RETRIES:
                throttled += 1
                await self._sleep(self._retry_after(response))
                continue
            if status in TRANSIENT_STATUSES and transient < MAX_TRANSIENT_RETRIES:
                transient += 1
                await self._sleep(2 ** (transient - 1))
                continue
            if status >= 400:
                raise self._api_error(response)
            return response

    @staticmethod
    def _retry_after(response: httpx2.Response) -> float:
        try:
            return max(float(response.headers.get("Retry-After", THROTTLE_WAIT)), 1.0)
        except ValueError:
            return THROTTLE_WAIT

    @staticmethod
    def _api_error(response: httpx2.Response) -> QboApiError:
        errors, fault_type = [], None
        try:
            fault = _find_fault(response.json())
            if fault is not None:
                errors, fault_type = fault_to_errors(fault)
        except ValueError:
            pass
        if not errors:
            errors = [{"message": response.text.strip()[:2000] or response.reason_phrase,
                       "detail": None, "code": None, "element": None}]
        hint = None
        if response.status_code == 429:
            hint = ("QuickBooks is throttling requests (500/minute, 10/second per company) "
                    "or the monthly read quota is used up.")
        return api_error(response.status_code, errors, fault_type, response.headers.get("intuit_tid"), hint)

    async def _json(self, method: str, path: str, **kwargs) -> tuple[dict, httpx2.Response]:
        """The JSON payload of a request, which QuickBooks may carry a Fault in even with HTTP 200."""
        response = await self.request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError as e:
            raise QboError("QuickBooks returned a non-JSON response for %s %s" % (method, path)) from e
        fault = _find_fault(payload)
        if fault is not None:
            raise _fault_error(fault, response)
        return payload, response

    async def _get(self, path: str, key: str, **kwargs):
        payload, _ = await self._json("GET", path, **kwargs)
        return self._unwrap(payload, key)

    async def _post(self, path: str, key: str, **kwargs):
        payload, _ = await self._json("POST", path, **kwargs)
        return self._unwrap(payload, key)

    @staticmethod
    def _unwrap(payload: dict, key: str) -> dict:
        if not isinstance(payload, dict):
            raise QboError("QuickBooks returned %s where a %s object was expected" % (type(payload).__name__, key))
        if key not in payload:
            raise QboError("QuickBooks response has no %s element (keys: %s)" % (key, ", ".join(payload)))
        return payload[key]

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex

    # --- Entities ------------------------------------------------------------

    async def read(self, spec: EntitySpec, entity_id: str | None = None) -> dict:
        if spec.name == "CompanyInfo":
            path = "companyinfo/" + REALM_PLACEHOLDER
        elif spec.name == "Preferences":
            path = "preferences"
        else:
            if not entity_id:
                raise QboError("%s requires an Id" % spec.name)
            path = "%s/%s" % (spec.path, quote(str(entity_id), safe=""))
        return await self._get(path, spec.name)

    async def query(self, statement: str) -> dict:
        return await self._post("query", "QueryResponse", content=statement.encode("utf-8"),
                                content_type="application/text")

    async def query_all(self, statement: str, limit: int) -> dict:
        """Run a query page by page (1000 rows per page) until exhausted or `limit` rows are collected.

        A COUNT query has no rows to page through; it runs once and returns QuickBooks' response.
        """
        if _PAGING_CLAUSE.search(statement):
            raise QboError("Remove STARTPOSITION/MAXRESULTS from the query to fetch all pages")
        if _COUNT_QUERY.match(statement):
            return await self.query(statement)
        base = statement.rstrip().rstrip(";")
        rows, entity, position = [], None, 1
        while len(rows) < limit:
            page_size = min(QUERY_PAGE_SIZE, limit - len(rows))
            page_entity, page = await self._query_page(base, position, page_size)
            entity = page_entity or entity
            rows.extend(page)
            if len(page) < page_size:
                return {"entity": entity, "rows": rows, "count": len(rows), "truncated": False}
            position += len(page)
        # The limit was reached on a full page: only one more row tells whether anything was left out.
        _, rest = await self._query_page(base, position, 1)
        return {"entity": entity, "rows": rows, "count": len(rows), "truncated": bool(rest)}

    async def _query_page(self, base: str, position: int, size: int) -> tuple[str | None, list]:
        response = await self.query("%s STARTPOSITION %d MAXRESULTS %d" % (base, position, size))
        for key, value in response.items():
            if isinstance(value, list):
                return key, value
        return None, []

    async def create(self, spec: EntitySpec, data: dict, request_id: str) -> dict:
        require_capability(spec, "create")
        return await self._post(spec.path, spec.name, json_body=data, request_id=request_id)

    async def update(self, spec: EntitySpec, data: dict, sparse: bool, request_id: str) -> dict:
        require_capability(spec, "update")
        if "SyncToken" not in data:
            raise QboError("Updating %s requires its current SyncToken" % spec.name)
        if spec.kind != "singleton" and not data.get("Id"):
            raise QboError("Updating %s requires its Id" % spec.name)
        body = dict(data)
        if sparse:
            body["sparse"] = True
        return await self._post(spec.path, spec.name, json_body=body, request_id=request_id)

    async def delete(self, spec: EntitySpec, entity_id: str, sync_token: str, request_id: str) -> dict:
        require_capability(spec, "delete")
        return await self._post(spec.path, spec.name, params={"operation": "delete"},
                                json_body={"Id": str(entity_id), "SyncToken": str(sync_token)}, request_id=request_id)

    async def deactivate(self, spec: EntitySpec, entity_id: str, sync_token: str, request_id: str) -> dict:
        require_capability(spec, "deactivate")
        return await self.update(spec, {"Id": str(entity_id), "SyncToken": str(sync_token), "Active": False}, True,
                                 request_id)

    async def void(self, spec: EntitySpec, entity_id: str, sync_token: str, request_id: str) -> dict:
        require_capability(spec, "void")
        body = {"Id": str(entity_id), "SyncToken": str(sync_token)}
        if spec.void_style == VOID_OPERATION:
            params = {"operation": "void"}
        else:
            params = {"operation": "update", "include": "void"}
            body["sparse"] = True
        return await self._post(spec.path, spec.name, params=params, json_body=body, request_id=request_id)

    async def send(self, spec: EntitySpec, entity_id: str, send_to: str | None, request_id: str) -> dict:
        require_capability(spec, "send")
        params = {"sendTo": send_to} if send_to else None
        return await self._post("%s/%s/send" % (spec.path, quote(str(entity_id), safe="")), spec.name, params=params,
                                content=b"", content_type="application/octet-stream", request_id=request_id)

    async def pdf(self, spec: EntitySpec, entity_id: str) -> bytes:
        require_capability(spec, "pdf")
        response = await self.request("GET", "%s/%s/pdf" % (spec.path, quote(str(entity_id), safe="")),
                                      accept="application/pdf")
        if not response.content.startswith(b"%PDF"):
            raise QboError("QuickBooks did not return a PDF for %s %s" % (spec.name, entity_id))
        return response.content

    # --- Batch, change data capture, reports, attachments ----------------------

    async def batch(self, items: list, request_id: str) -> list:
        if not items:
            raise QboError("A batch needs at least one operation")
        if len(items) > BATCH_LIMIT:
            raise QboError("A batch can hold at most %d operations; got %d" % (BATCH_LIMIT, len(items)))
        return await self._post("batch", "BatchItemResponse", json_body={"BatchItemRequest": items}, request_id=request_id)

    async def cdc(self, entities: list[str], changed_since: str) -> dict:
        return await self._get("cdc", "CDCResponse", params={"entities": ",".join(entities), "changedSince": changed_since})

    async def report(self, name: str, params: dict | None) -> dict:
        payload, _ = await self._json("GET", "reports/%s" % quote(name, safe=""), params=params)
        return payload

    async def upload(self, file_name: str, content: bytes, content_type: str, metadata: dict, request_id: str) -> dict:
        files = {
            "file_metadata_01": (None, json.dumps(metadata).encode("utf-8"), "application/json"),
            "file_content_01": (file_name, content, content_type),
        }
        payload, response = await self._json("POST", "upload", files=files, request_id=request_id)
        responses = self._unwrap(payload, "AttachableResponse")
        first = responses[0] if responses else {}
        fault = _find_fault(first)
        if fault is not None:
            raise _fault_error(fault, response)
        return self._unwrap(first, "Attachable")

    async def download(self, attachable_id: str) -> bytes:
        """Fetch an attachment's content through the temporary URL QuickBooks issues for it."""
        response = await self.request("GET", "download/%s" % quote(str(attachable_id), safe=""), accept="text/plain")
        url = response.text.strip()
        if not url.startswith("https://"):
            raise QboError("QuickBooks returned an unexpected download location for attachment %s" % attachable_id)
        try:
            content = await self._client().get(url)
        except httpx2.HTTPError as e:
            raise QboError("Cannot download attachment %s: %s" % (attachable_id, e)) from e
        if content.status_code != 200:
            raise QboError("Downloading attachment %s failed with HTTP %d" % (attachable_id, content.status_code))
        return content.content
