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

"""MCP server exposing the QuickBooks Online Accounting API as tools."""

import functools
import inspect
import logging
import mimetypes
import os
import time
import traceback
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from karellen_qbo_mcp.audit import AuditLog, ErrorLog
from karellen_qbo_mcp.client import QboClient, QboError, QboAuthError, QboApiError, BATCH_LIMIT
from karellen_qbo_mcp.config import Settings, ConfigError, load_settings
from karellen_qbo_mcp.entities import EntityError, get_entity, require_capability, describe_entities
from karellen_qbo_mcp.files import write_private_file
from karellen_qbo_mcp.login import LoginError, browser_login, open_browser_detached
from karellen_qbo_mcp.oauth import OAuthClient, OAuthError
from karellen_qbo_mcp.reports import KNOWN_REPORTS, ReportError, validate_report_name, flatten_report
from karellen_qbo_mcp.tokens import TokenStore, TokenStoreError, auth_status, iso_time

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_QUERY_ROWS = 100000

mcp = MCPServer("karellen-qbo-mcp", instructions=(
    "QuickBooks Online (QBO) accounting server acting on the user's real books; work under the user's supervision. "
    "Check qbo_auth_status first: it shows the environment (sandbox or production) and whether writes are allowed. "
    "Read before writing: fetch the record (qbo_get/qbo_query) and use its current SyncToken for updates, deletes "
    "and voids. Prefer sparse updates (the default); a full update clears every writable field you omit. Use "
    "dry_run=True to preview a write, especially updates, whose preview shows a field-by-field diff. Name-list "
    "records (accounts, customers, vendors, items) are deactivated, never deleted. Summarize amounts, accounts and "
    "dates to the user before posting. Verify results with reports (qbo_report) afterwards."
))

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
# Reads from QuickBooks that write a local file (and with overwrite=True replace one).
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TokenStore(settings.tokens_path)
        self.audit = AuditLog(settings.audit_path, settings.environment)
        self.errors = ErrorLog(settings.errors_path, settings.environment)
        self._oauth = None
        self._client = None

    @property
    def oauth(self) -> OAuthClient:
        if self._oauth is None:
            client_id, client_secret = self.settings.require_client_credentials()
            self._oauth = OAuthClient(client_id, client_secret, self.settings.discovery_url)
        return self._oauth

    @property
    def client(self) -> QboClient:
        if self._client is None:
            self._client = QboClient(self.settings, self.oauth, self.store)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_runtime = None


def _get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(load_settings())
    return _runtime


def _tag_errors(fn):
    """Report every failure as a ToolError prefixed with its category, and record it in the error log."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            error = _tool_error(e)
            _log_error(fn, args, kwargs, e, error)
            if error is e:
                raise
            raise error from e
    return wrapper


def _tool_error(e: Exception) -> ToolError:
    if isinstance(e, ToolError):
        return e
    if isinstance(e, (QboAuthError, LoginError, OAuthError)):
        return ToolError("auth: %s" % e)
    if isinstance(e, QboError):
        return ToolError("qbo: %s" % e)
    if isinstance(e, (EntityError, ReportError)):
        return ToolError("invalid: %s" % e)
    if isinstance(e, (ConfigError, TokenStoreError)):
        return ToolError("config: %s" % e)
    tb = traceback.extract_tb(e.__traceback__)
    tb_lines = ["%s:%d in %s" % (f.filename, f.lineno, f.name) for f in tb[-3:]]
    return ToolError("internal: %s: %s\n  %s" % (type(e).__name__, e, "\n  ".join(tb_lines)))


def _log_error(fn, args, kwargs, cause: Exception, error: ToolError):
    rt = _runtime
    if rt is None:  # the settings could not be loaded, so there is no state directory to log into
        return
    try:
        arguments = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
    except TypeError:  # the tool was called with arguments it does not take
        arguments = {"args": args, "kwargs": kwargs}
    details = {"type": type(cause).__name__}
    if isinstance(cause, QboApiError):
        details.update(status_code=cause.status_code, fault_type=cause.fault_type, errors=cause.errors,
                       intuit_tid=cause.intuit_tid)
    try:
        rt.errors.record(fn.__name__, arguments, str(error), realm_id=rt._client.realm_id if rt._client else None,
                         details=details)
    except Exception:
        # The client must get the tool's error, not one from logging it.
        logger.exception("Cannot record the %s error in the error log", fn.__name__)


def _require_writable(rt: Runtime):
    if rt.settings.read_only:
        raise ToolError("write: the server runs in read-only mode (QBO_MCP_READ_ONLY is set); no changes were made")


def _summarize(result) -> Any:
    """Keep the audit log readable: identifying fields of a result, not the whole record."""
    if not isinstance(result, dict):
        return result
    if "bId" in result:  # a qbo_batch result entry: keep its fault, summarize its record
        return {k: _summarize(v) if k == "result" else v for k, v in result.items()}
    keys = ("Id", "SyncToken", "DocNumber", "TxnDate", "TotalAmt", "Balance", "DisplayName", "Name", "Active",
            "PrivateNote", "status", "domain")
    return {k: result[k] for k in keys if k in result}


async def _perform(rt: Runtime, operation: str, entity: str | None, request, call):
    """Run one write with a fresh requestid and record it in the audit log either way.

    Every write goes through here, so this is where read-only mode is enforced.
    """
    _require_writable(rt)
    request_id = QboClient.new_request_id()
    client = rt.client
    try:
        result = await call(request_id)
    except BaseException as e:
        if isinstance(e, Exception):
            error = str(e)
        else:  # cancelled or interrupted: QuickBooks may have applied the write without us seeing its answer
            error = "cancelled or interrupted (%s) before QuickBooks answered; the change may have been applied" % (
                type(e).__name__)
        rt.audit.record(operation, entity, request, realm_id=client.realm_id, request_id=request_id, error=error)
        raise
    summary = [_summarize(r) for r in result] if isinstance(result, list) else _summarize(result)
    rt.audit.record(operation, entity, request, realm_id=client.realm_id, request_id=request_id, result=summary)
    return result


async def _record_write(operation: str, entity: str, id: str, sync_token: str, dry_run: bool, **extra) -> dict:
    """Delete, deactivate or void one record (the QboClient method of the same name), or preview it."""
    rt = _get_runtime()
    spec = require_capability(get_entity(entity), operation)
    if dry_run:
        return {"dry_run": True, "operation": operation, "entity": spec.name, "record": await rt.client.read(spec, id)}
    request = {"Id": id, "SyncToken": sync_token, **extra}
    method = getattr(rt.client, operation)
    return await _perform(rt, operation, spec.name, request, lambda rid: method(spec, id, sync_token, rid))


def _diff(current: dict, proposed: dict, sparse: bool) -> dict:
    changes = {}
    for key, value in proposed.items():
        if key in ("Id", "SyncToken", "sparse"):
            continue
        if current.get(key) != value:
            changes[key] = {"current": current.get(key), "proposed": value}
    result = {"changes": changes}
    if not sparse:
        read_only_fields = ("Id", "SyncToken", "MetaData", "domain", "sparse")
        cleared = sorted(k for k in current if k not in proposed and k not in read_only_fields)
        result["cleared_by_full_update"] = cleared
    return result


def _require_absolute(path: str, what: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ToolError("invalid: %s must be an absolute path, got %r" % (what, path))
    return p


def _output_target(path: str, overwrite: bool) -> Path:
    """Validate a download destination before anything is fetched from QuickBooks."""
    target = _require_absolute(path, "output_path")
    state_dir = _get_runtime().settings.state_dir.resolve()
    if target.resolve().is_relative_to(state_dir):
        raise ToolError("invalid: %s is inside the server's own state directory %s" % (target, state_dir))
    if not target.parent.is_dir():
        raise ToolError("invalid: directory %s does not exist" % target.parent)
    if target.exists() and not overwrite:
        raise ToolError("invalid: %s already exists; pass overwrite=True to replace it" % target)
    return target


async def _write_output(target: Path, content: bytes, overwrite: bool) -> dict:
    try:
        await anyio.to_thread.run_sync(functools.partial(write_private_file, target, content, overwrite=overwrite))
    except FileExistsError:
        raise ToolError("invalid: %s already exists; pass overwrite=True to replace it" % target)
    return {"path": str(target), "bytes": len(content)}


# --- Authentication ------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_auth_status() -> dict[str, Any]:
    """Show the active environment, whether writes are allowed, and the state of the stored QuickBooks authorization.

    Makes no network calls. Token lifetimes: the access token lasts an hour and is refreshed automatically; the
    refresh token expires after 100 days without use, and in any case at its hard expiry (5 years after sign-in).
    """
    rt = _get_runtime()
    return auth_status(rt.settings, rt.store, time.time())


@mcp.tool(annotations=ADDITIVE)
@_tag_errors
async def qbo_auth_login() -> dict[str, Any]:
    """Sign in to QuickBooks in the user's browser and store the resulting authorization.

    Opens Intuit's consent page; the user picks the company and approves. Waits up to 5 minutes for the redirect
    back to the local callback listener. Works only with a plain-HTTP localhost redirect URI, which Intuit allows
    for sandbox (development) keys. For production, the user obtains tokens in Intuit's OAuth 2.0 Playground and
    runs `karellen-qbo-mcp --environment production auth import`.
    """
    rt = _get_runtime()
    if not rt.settings.redirect_uri:
        raise ToolError("config: no redirect URI is configured for the %s environment" % rt.settings.environment)
    tokens = await browser_login(rt.oauth, rt.settings.redirect_uri, open_browser=open_browser_detached)
    await rt.store.save_locked(tokens)
    return {"environment": rt.settings.environment, "realm_id": tokens.realm_id,
            "refresh_token_expires_at": iso_time(tokens.refresh_expires_at),
            "refresh_token_hard_expires_at": iso_time(tokens.refresh_hard_expires_at)}


# --- Reading -----------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_list_entities() -> list[dict[str, Any]]:
    """List the QuickBooks entities this server handles and which operations each supports."""
    return describe_entities()


@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_get(entity: str, id: str | None = None) -> dict[str, Any]:
    """Read one record by Id, including its current SyncToken.

    Args:
        entity: Entity name, e.g. "Invoice", "Customer", "JournalEntry". CompanyInfo and Preferences need no id.
        id: The record's Id.
    """
    spec = get_entity(entity)
    return await _get_runtime().client.read(spec, id)


@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_query(query: str, fetch_all: bool = False, limit: int = 1000) -> dict[str, Any]:
    """Run a QuickBooks query (SQL-like, one entity per query).

    Examples: "SELECT * FROM Invoice WHERE Balance > '0' ORDERBY TxnDate DESC",
    "SELECT COUNT(*) FROM Customer WHERE Active = true". There are no joins, no OR, and only a subset of fields
    is filterable. A single call returns at most 1000 rows; use STARTPOSITION/MAXRESULTS to page, or
    fetch_all=True (without those clauses) to page automatically.

    Args:
        query: The query statement.
        fetch_all: Page through all results up to `limit` rows.
        limit: Row cap for fetch_all (at most 100000).
    """
    client = _get_runtime().client
    if fetch_all:
        if limit < 1 or limit > MAX_QUERY_ROWS:
            raise ToolError("invalid: limit must be between 1 and %d" % MAX_QUERY_ROWS)
        return await client.query_all(query, limit)
    return await client.query(query)


@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_cdc(entities: list[str], changed_since: str) -> list[dict[str, Any]]:
    """Change data capture: every record of the given entities created, changed or deleted since a time.

    QuickBooks looks back at most 30 days and returns at most 1000 records per entity. Deleted records come back
    with status "Deleted". The result is QuickBooks' CDCResponse list: QueryResponse entries holding one list of
    records per entity.

    Args:
        entities: Entity names, e.g. ["Invoice", "Payment"].
        changed_since: ISO 8601 timestamp, e.g. "2026-09-01T00:00:00-07:00".
    """
    names = [get_entity(e).name for e in entities]
    if not names:
        raise ToolError("invalid: name at least one entity")
    return await _get_runtime().client.cdc(names, changed_since)


@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_list_reports() -> dict[str, Any]:
    """List common QuickBooks report names and the parameters most reports accept."""
    return {
        "reports": KNOWN_REPORTS,
        "common_parameters": {
            "start_date / end_date": "YYYY-MM-DD",
            "date_macro": "e.g. 'This Fiscal Year', 'Last Month', 'This Year-to-date'",
            "accounting_method": "Cash or Accrual",
            "summarize_column_by": "Total, Month, Quarter, Year, Customers, Vendors, Classes, Departments",
            "customer / vendor / item / department / class / account": "comma-separated Ids to filter by",
        },
        "note": "Keep date ranges at six months or less for detail reports. Report availability and parameters vary "
                "by report; QuickBooks rejects unknown ones.",
    }


@mcp.tool(annotations=READ_ONLY)
@_tag_errors
async def qbo_report(report: str, params: dict[str, str] | None = None, output: str = "flat") -> dict[str, Any]:
    """Run a QuickBooks report, e.g. ProfitAndLoss, BalanceSheet, TrialBalance, GeneralLedger, AgedReceivables.

    Args:
        report: Report name (see qbo_list_reports).
        params: Report parameters, e.g. {"start_date": "2026-01-01", "end_date": "2026-06-30",
            "accounting_method": "Accrual"}.
        output: "flat" (default) returns ordered rows with a nesting depth and kind (header/data/summary);
            "raw" returns QuickBooks' nested JSON unchanged.
    """
    if output not in ("flat", "raw"):
        raise ToolError("invalid: output must be 'flat' or 'raw'")
    name = validate_report_name(report)
    raw = await _get_runtime().client.report(name, params)
    if output == "raw":
        return raw
    try:
        return flatten_report(raw)
    except ReportError as e:
        return {"note": "Could not flatten this report (%s); returning raw output" % e, "raw": raw}


@mcp.tool(annotations=LOCAL_WRITE)
@_tag_errors
async def qbo_download_pdf(entity: str, id: str, output_path: str, overwrite: bool = False) -> dict[str, Any]:
    """Save the PDF of an invoice, estimate or sales receipt to a local file.

    Args:
        entity: "Invoice", "Estimate" or "SalesReceipt".
        id: The record's Id.
        output_path: Absolute path of the PDF to write; its directory must exist.
        overwrite: Replace the file if it already exists.
    """
    spec = require_capability(get_entity(entity), "pdf")
    target = _output_target(output_path, overwrite)
    return await _write_output(target, await _get_runtime().client.pdf(spec, id), overwrite)


@mcp.tool(annotations=LOCAL_WRITE)
@_tag_errors
async def qbo_download_attachment(attachable_id: str, output_path: str, overwrite: bool = False) -> dict[str, Any]:
    """Save the file behind an Attachable record to a local file.

    Args:
        attachable_id: Id of the Attachable (find them with qbo_query "SELECT * FROM Attachable ...").
        output_path: Absolute path of the file to write; its directory must exist.
        overwrite: Replace the file if it already exists.
    """
    target = _output_target(output_path, overwrite)
    return await _write_output(target, await _get_runtime().client.download(attachable_id), overwrite)


# --- Writing -----------------------------------------------------------------------

@mcp.tool(annotations=ADDITIVE)
@_tag_errors
async def qbo_create(entity: str, data: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Create a record: a customer, vendor, account, item, invoice, bill, payment, journal entry, and so on.

    `data` is the QuickBooks JSON for the entity without Id/SyncToken, e.g. for a JournalEntry:
    {"TxnDate": "2026-09-30", "Line": [{"Amount": 100.0, "DetailType": "JournalEntryLineDetail",
    "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "60"}}}, ...]}.
    Reference other records by Id ({"value": "<Id>"}); look them up first.

    Args:
        entity: Entity name.
        data: The record's fields.
        dry_run: Validate locally and return what would be sent, without sending it.
    """
    rt = _get_runtime()
    spec = require_capability(get_entity(entity), "create")
    if dry_run:
        return {"dry_run": True, "operation": "create", "entity": spec.name, "data": data}
    return await _perform(rt, "create", spec.name, data, lambda rid: rt.client.create(spec, data, rid))


@mcp.tool(annotations=DESTRUCTIVE)
@_tag_errors
async def qbo_update(entity: str, data: dict[str, Any], sparse: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Update a record. `data` must carry the record's Id and its current SyncToken.

    With sparse=True (default) only the fields in `data` change. With sparse=False the record is replaced: every
    writable field missing from `data` is cleared. Lists such as Line are always replaced whole, so send the
    complete list when changing lines. A stale SyncToken is rejected by QuickBooks (someone changed the record).

    Args:
        entity: Entity name.
        data: Fields to change plus Id and SyncToken.
        sparse: Partial update (True) or full replacement (False).
        dry_run: Fetch the current record and return a field-by-field diff instead of updating.
    """
    rt = _get_runtime()
    spec = require_capability(get_entity(entity), "update")
    if dry_run:
        current = await rt.client.read(spec, data.get("Id"))
        preview = {"dry_run": True, "operation": "update", "entity": spec.name, "sparse": sparse,
                   "sync_token_current": current.get("SyncToken"),
                   "sync_token_matches": str(current.get("SyncToken")) == str(data.get("SyncToken"))}
        preview.update(_diff(current, data, sparse))
        return preview
    return await _perform(rt, "update", spec.name, data, lambda rid: rt.client.update(spec, data, sparse, rid))


@mcp.tool(annotations=DESTRUCTIVE)
@_tag_errors
async def qbo_delete(entity: str, id: str, sync_token: str, dry_run: bool = False) -> dict[str, Any]:
    """Delete a transaction (invoice, bill, payment, journal entry, ...) or an attachment. Cannot be undone.

    Name-list records (accounts, customers, vendors, items, ...) cannot be deleted; use qbo_deactivate.

    Args:
        entity: Entity name.
        id: The record's Id.
        sync_token: The record's current SyncToken.
        dry_run: Return the record that would be deleted instead of deleting it.
    """
    return await _record_write("delete", entity, id, sync_token, dry_run)


@mcp.tool(annotations=DESTRUCTIVE)
@_tag_errors
async def qbo_deactivate(entity: str, id: str, sync_token: str, dry_run: bool = False) -> dict[str, Any]:
    """Deactivate a name-list record (account, customer, vendor, item, class, ...); QuickBooks' way of deleting it.

    Reactivate with qbo_update {"Id", "SyncToken", "Active": true}.

    Args:
        entity: Entity name.
        id: The record's Id.
        sync_token: The record's current SyncToken.
        dry_run: Return the record that would be deactivated instead of deactivating it.
    """
    return await _record_write("deactivate", entity, id, sync_token, dry_run, Active=False)


@mcp.tool(annotations=DESTRUCTIVE)
@_tag_errors
async def qbo_void(entity: str, id: str, sync_token: str, dry_run: bool = False) -> dict[str, Any]:
    """Void an Invoice, Payment, SalesReceipt or BillPayment: the record stays, its amounts become zero.

    Args:
        entity: Entity name.
        id: The record's Id.
        sync_token: The record's current SyncToken.
        dry_run: Return the record that would be voided instead of voiding it.
    """
    return await _record_write("void", entity, id, sync_token, dry_run)


@mcp.tool(annotations=ADDITIVE)
@_tag_errors
async def qbo_send(entity: str, id: str, send_to: str | None = None) -> dict[str, Any]:
    """Email an Invoice, Estimate or PurchaseOrder from QuickBooks to the customer or vendor.

    Args:
        entity: Entity name.
        id: The record's Id.
        send_to: Recipient email; defaults to the email address on the record.
    """
    rt = _get_runtime()
    spec = require_capability(get_entity(entity), "send")
    request = {"Id": id, "sendTo": send_to}
    return await _perform(rt, "send", spec.name, request, lambda rid: rt.client.send(spec, id, send_to, rid))


@mcp.tool(annotations=DESTRUCTIVE)
@_tag_errors
async def qbo_batch(operations: list[dict[str, Any]], dry_run: bool = False) -> list[dict[str, Any]]:
    """Run up to 30 creates, updates, deletes and queries in one request. Each operation succeeds or fails alone.

    Each operation is one of:
      {"operation": "create", "entity": "Bill", "data": {...}}
      {"operation": "update", "entity": "Customer", "data": {"Id", "SyncToken", ...}, "sparse": true}
      {"operation": "delete", "entity": "Invoice", "data": {"Id", "SyncToken"}}
      {"query": "SELECT * FROM Vendor WHERE DisplayName = 'Acme'"}
    Results come back in order, each with the created/updated record or a "fault".

    Args:
        operations: The operations.
        dry_run: Validate locally and return the batch request without sending it.
    """
    rt = _get_runtime()
    if len(operations) > BATCH_LIMIT:
        raise ToolError("invalid: at most %d operations per batch, got %d" % (BATCH_LIMIT, len(operations)))
    items, writes = [], False
    for index, op in enumerate(operations, 1):
        bid = str(index)
        if "query" in op:
            items.append({"bId": bid, "Query": op["query"]})
            continue
        kind = op.get("operation")
        if kind not in ("create", "update", "delete"):
            raise ToolError("invalid: operation %d: 'operation' must be create, update or delete, or give a 'query'"
                            % index)
        spec = require_capability(get_entity(op.get("entity")), kind)
        data = dict(op.get("data") or {})
        if kind in ("update", "delete") and (not data.get("Id") or "SyncToken" not in data):
            raise ToolError("invalid: operation %d: %s needs Id and SyncToken in data" % (index, kind))
        if kind == "update" and op.get("sparse", True):
            data["sparse"] = True
        items.append({"bId": bid, "operation": kind, spec.name: data})
        writes = True
    if dry_run:
        return [dict(item, dry_run=True) for item in items]

    async def call(rid):
        responses = await rt.client.batch(items, rid)
        results = []
        for response in responses:
            entry = {"bId": response.get("bId")}
            for key, value in response.items():
                if key == "bId":
                    continue
                entry["fault" if key == "Fault" else "result"] = value
                if key != "Fault":
                    entry["type"] = key
            results.append(entry)
        return sorted(results, key=lambda r: int(r["bId"]) if str(r["bId"]).isdigit() else 0)

    if not writes:
        return await call(QboClient.new_request_id())
    return await _perform(rt, "batch", None, items, call)


@mcp.tool(annotations=ADDITIVE)
@_tag_errors
async def qbo_upload_attachment(file_path: str, attach_to: list[dict[str, str]] | None = None,
                                note: str | None = None) -> dict[str, Any]:
    """Upload a local file (receipt, statement, contract; up to 100 MB) as an attachment.

    Args:
        file_path: Absolute path of the file to upload.
        attach_to: Records to link it to, e.g. [{"entity": "Bill", "id": "145"}]. Omit to upload it unlinked.
            Linking changes each linked record's SyncToken; re-read a record before updating, voiding or
            deleting it afterwards.
        note: Optional note stored with the attachment.
    """
    rt = _get_runtime()
    path = _require_absolute(file_path, "file_path")
    if not path.is_file():
        raise ToolError("invalid: %s is not a file" % path)
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise ToolError("invalid: %s is %d bytes; QuickBooks accepts at most 100 MB" % (path, size))
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    metadata = {"FileName": path.name, "ContentType": content_type}
    if note:
        metadata["Note"] = note
    refs = []
    for link in attach_to or []:
        spec = get_entity(link.get("entity"))
        if not link.get("id"):
            raise ToolError("invalid: every attach_to entry needs an entity and an id")
        refs.append({"EntityRef": {"type": spec.name, "value": str(link["id"])}, "IncludeOnSend": False})
    if refs:
        metadata["AttachableRef"] = refs
    request = dict(metadata, SourcePath=str(path), Bytes=size)

    async def call(rid):
        content = await anyio.Path(path).read_bytes()
        return await rt.client.upload(path.name, content, content_type, metadata, rid)

    return await _perform(rt, "upload", "Attachable", request, call)


def _watch_parent():
    """Exit when the parent process (the MCP client) dies, so the server is never orphaned."""
    import threading

    ppid = os.getppid()

    def _monitor():
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                os._exit(0)

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()


def serve():
    _watch_parent()
    mcp.run(transport="stdio")
