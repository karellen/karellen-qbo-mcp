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
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

import karellen_qbo_mcp.server as server
from karellen_qbo_mcp.client import QboAuthError, QboError
from karellen_qbo_mcp.config import ConfigError
from karellen_qbo_mcp.entities import EntityError
from karellen_qbo_mcp.login import LoginError
from qbo_test_support import (REALM, BASE, make_settings, make_tokens, Recorder, json_response, make_client, FakeOAuth, run,
                              watch_token_lock)

CDC_RESPONSE = {"CDCResponse": [
    {"QueryResponse": [{"Invoice": [{"Id": "9", "SyncToken": "3"}, {"Id": "10", "status": "Deleted"}]},
                       {"Payment": [{"Id": "5", "SyncToken": "0"}, {"Id": "6", "SyncToken": "1"}]}]},
    {"QueryResponse": [{"Invoice": [{"Id": "11", "SyncToken": "0"}]}, {"Payment": []}]},
], "time": "2026-09-28T10:00:00-07:00"}


class TagErrorsTests(unittest.TestCase):
    def check(self, error, prefix):
        @server._tag_errors
        async def fail():
            raise error
        with self.assertRaises(ToolError) as ctx:
            run(fail())
        self.assertTrue(str(ctx.exception).startswith(prefix), str(ctx.exception))
        return str(ctx.exception)

    def test_prefixes(self):
        self.check(QboAuthError("sign in"), "auth: sign in")
        self.check(QboError("bad"), "qbo: bad")
        self.check(EntityError("nope"), "invalid: nope")
        self.check(ConfigError("missing"), "config: missing")
        self.check(LoginError("denied"), "auth: denied")
        self.check(ToolError("as is"), "as is")

    def test_unexpected_error_carries_type_and_traceback(self):
        message = self.check(KeyError("Line"), "internal: KeyError")
        self.assertIn("in fail", message)


class ServerTestBase(unittest.TestCase):
    read_only = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = make_settings(self.tmp, read_only=self.read_only)
        self.runtime = server.Runtime(self.settings)
        self.runtime._oauth = FakeOAuth()
        server._runtime = self.runtime

    def tearDown(self):
        server._runtime = None
        self._tmp.cleanup()

    def respond(self, *responses, signed_in=True):
        self.recorder = Recorder(*responses)
        client, store = make_client(self.settings, self.recorder, store=self.runtime.store)
        if signed_in:
            store.save(make_tokens())
        self.runtime._client = client
        return self.recorder

    def audit(self):
        path = self.settings.audit_path
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def errors(self):
        path = self.settings.errors_path
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ErrorLogTests(ServerTestBase):
    def test_read_errors_are_logged_with_arguments_and_intuit_details(self):
        fault = {"Fault": {"Error": [{"Message": "Invalid query", "Detail": "QueryParserError: Encountered \"FORM\"",
                                      "code": "4000"}, {"Message": "Second problem", "code": "4001"}],
                           "type": "ValidationFault"}}
        self.respond(json_response(400, fault, headers={"intuit_tid": "tid-42"}))
        with self.assertRaises(ToolError) as query_error:
            run(server.qbo_query("SELECT * FORM Customer"))
        with self.assertRaises(ToolError) as get_error:
            run(server.qbo_get("NoSuchThing", "1"))

        query, get = self.errors()
        self.assertEqual({k: query[k] for k in ("environment", "realm_id", "tool", "arguments", "type", "status_code",
                                                "fault_type", "intuit_tid")},
                         {"environment": "sandbox", "realm_id": REALM, "tool": "qbo_query",
                          "arguments": {"query": "SELECT * FORM Customer"}, "type": "QboApiError", "status_code": 400,
                          "fault_type": "ValidationFault", "intuit_tid": "tid-42"})
        self.assertEqual([e["code"] for e in query["errors"]], ["4000", "4001"])
        self.assertEqual(query["error"], str(query_error.exception))
        self.assertIn("intuit_tid tid-42", query["error"])
        self.assertIn("T", query["timestamp"])

        self.assertEqual((get["tool"], get["arguments"], get["type"], get["error"]),
                         ("qbo_get", {"entity": "NoSuchThing", "id": "1"}, "EntityError", str(get_error.exception)))
        self.assertTrue(get["error"].startswith("invalid:"))
        self.assertNotIn("intuit_tid", get)
        self.assertEqual(self.audit(), [])  # reads are not writes

    def test_tool_errors_raised_as_is_are_logged_and_reraised_unchanged(self):
        raised = ToolError("invalid: bad argument")

        @server._tag_errors
        async def qbo_example(first, second="default"):
            raise raised

        with self.assertRaises(ToolError) as ctx:
            run(qbo_example("one", second="two"))
        self.assertIs(ctx.exception, raised)
        with self.assertRaises(ToolError):
            run(qbo_example("one", "two", "three"))  # more arguments than the tool takes
        logged, unbindable = self.errors()
        self.assertEqual((logged["tool"], logged["arguments"], logged["type"], logged["realm_id"]),
                         ("qbo_example", {"first": "one", "second": "two"}, "ToolError", None))
        self.assertEqual(unbindable["arguments"], {"args": ["one", "two", "three"], "kwargs": {}})
        self.assertEqual(unbindable["type"], "TypeError")
        self.assertTrue(unbindable["error"].startswith("internal: TypeError"))

    def test_failed_writes_are_logged_as_well_as_audited(self):
        self.respond(json_response(400, {"Fault": {"Error": [{"Message": "Stale object", "code": "5010"}],
                                                   "type": "ValidationFault"}}))
        with self.assertRaises(ToolError):
            run(server.qbo_delete("Invoice", "9", "0"))
        self.assertEqual([e["operation"] for e in self.audit()], ["delete"])
        (logged,) = self.errors()
        self.assertEqual((logged["tool"], logged["fault_type"]), ("qbo_delete", "ValidationFault"))

    def test_logging_failure_does_not_replace_the_tool_error(self):
        with patch.object(self.runtime.errors, "record", side_effect=RuntimeError("disk on fire")), \
                self.assertLogs("karellen_qbo_mcp.server", level="ERROR") as logs, self.assertRaises(ToolError) as ctx:
            run(server.qbo_get("NoSuchThing", "1"))
        self.assertTrue(str(ctx.exception).startswith("invalid:"))
        self.assertIn("disk on fire", "\n".join(logs.output))

    def test_nothing_logged_without_a_runtime(self):
        server._runtime = None
        with patch.dict(os.environ, {"QBO_MCP_ENVIRONMENT": "bogus"}), self.assertRaises(ToolError) as ctx:
            run(server.qbo_get("Customer", "1"))
        self.assertTrue(str(ctx.exception).startswith("config:"))
        self.assertIsNone(server._runtime)
        self.assertEqual(self.errors(), [])


class AuthToolTests(ServerTestBase):
    def test_status_signed_out(self):
        status = run(server.qbo_auth_status())
        self.assertEqual((status["environment"], status["signed_in"], status["client_configured"]), ("sandbox", False, True))
        self.assertNotIn("realm_id", status)

    def test_status_signed_in(self):
        self.runtime.store.save(make_tokens())
        status = run(server.qbo_auth_status())
        self.assertEqual(status["realm_id"], REALM)
        self.assertTrue(status["refresh_token_expires_at"].endswith("+00:00"))

    def test_status_without_client_credentials(self):
        server._runtime = server.Runtime(make_settings(self.tmp, client_id=None, client_secret=None))
        self.assertFalse(run(server.qbo_auth_status())["client_configured"])
        with self.assertRaises(ToolError) as ctx:
            run(server.qbo_get("Customer", "1"))
        self.assertTrue(str(ctx.exception).startswith("config:"))

    def test_login_saves_tokens(self):
        async def fake_login(oauth, redirect_uri, open_browser):
            self.assertEqual(redirect_uri, "http://localhost:8765/callback")
            # webbrowser.open would share the server's stdout, which carries the MCP protocol.
            self.assertIs(open_browser, server.open_browser_detached)
            return make_tokens(label="new", realm_id="777")
        with patch.object(server, "browser_login", fake_login), watch_token_lock() as events:
            result = run(server.qbo_auth_login())
        self.assertEqual(result["realm_id"], "777")
        self.assertEqual(self.runtime.store.load().access_token, "access-new")
        self.assertEqual(events, [("save", True)])

    def test_login_without_redirect(self):
        server._runtime = server.Runtime(make_settings(self.tmp, environment="production", redirect_uri=None))
        with self.assertRaises(ToolError):
            run(server.qbo_auth_login())

    def test_login_with_https_redirect_points_to_terminal(self):
        server._runtime = server.Runtime(make_settings(self.tmp, environment="production",
                                                       redirect_uri="https://karellen.example/qbo-callback"))
        with self.assertRaises(ToolError) as ctx:
            run(server.qbo_auth_login())
        self.assertTrue(str(ctx.exception).startswith("auth: Redirect URI https://karellen.example/qbo-callback"))
        self.assertIn("auth login", str(ctx.exception))

    def test_not_signed_in_is_auth_error(self):
        self.respond(signed_in=False)
        with self.assertRaises(ToolError) as ctx:
            run(server.qbo_query("SELECT * FROM Customer"))
        self.assertTrue(str(ctx.exception).startswith("auth: Not signed in"))


class ReadToolTests(ServerTestBase):
    def test_get(self):
        self.respond(json_response(200, {"Invoice": {"Id": "9", "SyncToken": "2"}}))
        self.assertEqual(run(server.qbo_get("invoice", "9")), {"Id": "9", "SyncToken": "2"})

    def test_get_unknown_entity(self):
        with self.assertRaises(ToolError) as ctx:
            run(server.qbo_get("Widget", "1"))
        self.assertTrue(str(ctx.exception).startswith("invalid:"))

    def test_query_single_call_and_fetch_all(self):
        self.respond(json_response(200, {"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}], "maxResults": 2}}),
                     json_response(200, {"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}]}}))
        self.assertEqual(run(server.qbo_query("SELECT * FROM Customer"))["maxResults"], 2)
        self.assertEqual(run(server.qbo_query("SELECT * FROM Customer", fetch_all=True, limit=50))["count"], 2)

    def test_query_limit_bounds(self):
        for limit in (0, 100001):
            with self.assertRaises(ToolError):
                run(server.qbo_query("SELECT * FROM Customer", fetch_all=True, limit=limit))

    def test_cdc_normalizes_entity_names(self):
        recorder = self.respond(json_response(200, CDC_RESPONSE))
        run(server.qbo_cdc(["invoice", "PAYMENT"], "2026-09-01T00:00:00Z"))
        self.assertEqual(recorder.requests[0].url.params["entities"], "Invoice,Payment")
        with self.assertRaises(ToolError):
            run(server.qbo_cdc([], "2026-09-01T00:00:00Z"))

    def test_cdc_result_passes_tool_output_validation(self):
        # CDCResponse is a JSON array; MCPServer validates tool results against the declared return type.
        self.respond(json_response(200, CDC_RESPONSE))
        result = run(server.mcp.call_tool("qbo_cdc", {"entities": ["Invoice", "Payment"],
                                                      "changed_since": "2026-09-01T00:00:00Z"}))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"result": CDC_RESPONSE["CDCResponse"]})

    def test_report_flat_raw_and_fallback(self):
        columns = [{"ColTitle": "", "ColType": "Account"}, {"ColTitle": "Debit", "ColType": "Money"}]
        report = {"Header": {"ReportName": "TrialBalance"}, "Columns": {"Column": columns},
                  "Rows": {"Row": [{"ColData": [{"value": "Checking", "id": "35"}, {"value": "100.00"}]},
                                   {"ColData": [{"value": "Savings", "id": "36"}, {"value": "200.00"}]}]}}
        self.respond(json_response(200, report), json_response(200, report), json_response(200, {"Weird": True}))
        flat = run(server.qbo_report("TrialBalance", {"end_date": "2026-06-30"}))
        self.assertEqual([r["values"][0] for r in flat["rows"]], ["Checking", "Savings"])
        self.assertEqual(run(server.qbo_report("TrialBalance", output="raw")), report)
        fallback = run(server.qbo_report("TrialBalance"))
        self.assertEqual(fallback["raw"], {"Weird": True})
        with self.assertRaises(ToolError):
            run(server.qbo_report("TrialBalance", output="csv"))
        with self.assertRaises(ToolError):
            run(server.qbo_report("../x"))

    def test_list_tools_static(self):
        self.assertIn("ProfitAndLoss", run(server.qbo_list_reports())["reports"])
        self.assertIn("Invoice", [e["entity"] for e in run(server.qbo_list_entities())])

    def test_download_pdf_writes_file_and_protects_existing(self):
        self.respond(httpx2.Response(200, content=b"%PDF-1.7 a"), httpx2.Response(200, content=b"%PDF-1.7 b"))
        target = self.tmp / "inv.pdf"
        self.assertEqual(run(server.qbo_download_pdf("Invoice", "9", str(target))), {"path": str(target), "bytes": 10})
        with self.assertRaises(ToolError):
            run(server.qbo_download_pdf("Invoice", "9", str(target)))
        run(server.qbo_download_pdf("Invoice", "9", str(target), overwrite=True))
        self.assertEqual(target.read_bytes(), b"%PDF-1.7 b")

    def test_download_path_validation(self):
        with self.assertRaises(ToolError):
            run(server.qbo_download_pdf("Invoice", "9", "relative.pdf"))
        self.respond(httpx2.Response(200, text="https://s3.example.com/f"), httpx2.Response(200, content=b"data"))
        with self.assertRaises(ToolError):
            run(server.qbo_download_attachment("100", str(self.tmp / "missing-dir" / "f.bin")))

    def test_download_attachment(self):
        self.respond(httpx2.Response(200, text="https://s3.example.com/f"), httpx2.Response(200, content=b"data"))
        target = self.tmp / "receipt.jpg"
        run(server.qbo_download_attachment("100", str(target)))
        self.assertEqual(target.read_bytes(), b"data")

    def test_download_tools_are_local_writes(self):
        tools = {t.name: t for t in run(server.mcp.list_tools())}
        for name in ("qbo_download_pdf", "qbo_download_attachment"):
            self.assertFalse(tools[name].annotations.read_only_hint, name)
            self.assertTrue(tools[name].annotations.destructive_hint, name)

    def test_download_refuses_server_state_files(self):
        self.respond(signed_in=True)
        for target in (self.settings.tokens_path, self.settings.client_path, self.settings.state_dir / "x" / ".." / "a"):
            with self.assertRaises(ToolError) as ctx:
                run(server.qbo_download_pdf("Invoice", "9", str(target), overwrite=True))
            self.assertTrue(str(ctx.exception).startswith("invalid:"))
        self.assertEqual(self.recorder.requests, [])
        self.assertEqual(self.runtime.store.load(), make_tokens())

    def test_download_never_clobbers_a_file_created_after_validation(self):
        self.respond(httpx2.Response(200, content=b"%PDF-1.7 new"))
        target = self.tmp / "inv.pdf"
        target.write_bytes(b"theirs")
        with patch.object(server, "_output_target", lambda path, overwrite: target):
            with self.assertRaises(ToolError) as ctx:
                run(server.qbo_download_pdf("Invoice", "9", str(target)))
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(target.read_bytes(), b"theirs")


class WriteToolTests(ServerTestBase):
    def test_create_audited(self):
        recorder = self.respond(json_response(200, {"Customer": {"Id": "58", "SyncToken": "0", "DisplayName": "Acme",
                                                                 "Balance": 0, "Notes": "long"}}))
        result = run(server.qbo_create("Customer", {"DisplayName": "Acme"}))
        self.assertEqual(result["Id"], "58")
        request_id = recorder.requests[0].url.params["requestid"]
        [entry] = self.audit()
        self.assertEqual((entry["operation"], entry["entity"], entry["realm_id"], entry["request_id"]),
                         ("create", "Customer", REALM, request_id))
        self.assertEqual(entry["result"], {"Id": "58", "SyncToken": "0", "DisplayName": "Acme", "Balance": 0})

    def test_failed_write_audited_and_reported(self):
        self.respond(json_response(400, {"Fault": {"Error": [{"Message": "Duplicate Name Exists Error", "code": "6240"}],
                                                   "type": "ValidationFault"}}))
        with self.assertRaises(ToolError) as ctx:
            run(server.qbo_create("Customer", {"DisplayName": "Acme"}))
        self.assertIn("Duplicate Name", str(ctx.exception))
        [entry] = self.audit()
        self.assertIn("Duplicate Name", entry["error"])

    def test_create_dry_run_sends_nothing(self):
        self.respond()
        result = run(server.qbo_create("Bill", {"VendorRef": {"value": "3"}}, dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.recorder.requests, [])
        self.assertEqual(self.audit(), [])

    def test_update_dry_run_diff(self):
        current = {"Id": "9", "SyncToken": "2", "PrivateNote": "old", "DueDate": "2026-10-01", "CustomerMemo": {"value": "x"},
                   "MetaData": {}, "domain": "QBO"}
        self.respond(json_response(200, {"Invoice": current}), json_response(200, {"Invoice": current}))
        sparse = run(server.qbo_update("Invoice", {"Id": "9", "SyncToken": "2", "PrivateNote": "new",
                                                   "DueDate": "2026-10-01"}, dry_run=True))
        self.assertTrue(sparse["sync_token_matches"])
        self.assertEqual(sparse["changes"], {"PrivateNote": {"current": "old", "proposed": "new"}})
        self.assertNotIn("cleared_by_full_update", sparse)
        full = run(server.qbo_update("Invoice", {"Id": "9", "SyncToken": "1", "PrivateNote": "new"}, sparse=False,
                                     dry_run=True))
        self.assertFalse(full["sync_token_matches"])
        self.assertEqual(full["cleared_by_full_update"], ["CustomerMemo", "DueDate"])
        self.assertEqual(self.audit(), [])

    def test_update(self):
        recorder = self.respond(json_response(200, {"Invoice": {"Id": "9", "SyncToken": "3"}}))
        run(server.qbo_update("Invoice", {"Id": "9", "SyncToken": "2", "PrivateNote": "new"}))
        self.assertTrue(recorder.body_json()["sparse"])
        self.assertEqual(self.audit()[0]["operation"], "update")

    def test_delete_deactivate_void_and_dry_runs(self):
        record = {"Id": "9", "SyncToken": "2", "TotalAmt": 50}
        recorder = self.respond(json_response(200, {"Invoice": record}),
                                json_response(200, {"Invoice": {"Id": "9", "status": "Deleted"}}),
                                json_response(200, {"Customer": {"Id": "4", "Active": False}}),
                                json_response(200, {"Payment": {"Id": "5", "SyncToken": "1"}}))
        self.assertEqual(run(server.qbo_delete("Invoice", "9", "2", dry_run=True))["record"], record)
        run(server.qbo_delete("Invoice", "9", "2"))
        run(server.qbo_deactivate("Customer", "4", "0"))
        run(server.qbo_void("Payment", "5", "0"))
        self.assertEqual([e["operation"] for e in self.audit()], ["delete", "deactivate", "void"])
        self.assertEqual(recorder.requests[1].url.params["operation"], "delete")
        with self.assertRaises(ToolError):
            run(server.qbo_delete("Customer", "4", "0"))

    def test_deactivate_and_void_dry_runs(self):
        self.respond(json_response(200, {"Vendor": {"Id": "3"}}), json_response(200, {"Invoice": {"Id": "9"}}))
        self.assertEqual(run(server.qbo_deactivate("Vendor", "3", "0", dry_run=True))["record"], {"Id": "3"})
        self.assertEqual(run(server.qbo_void("Invoice", "9", "0", dry_run=True))["record"], {"Id": "9"})
        self.assertEqual(self.audit(), [])

    def test_send(self):
        recorder = self.respond(json_response(200, {"Invoice": {"Id": "9", "EmailStatus": "EmailSent"}}))
        run(server.qbo_send("Invoice", "9", "ap@example.com"))
        self.assertTrue(str(recorder.requests[0].url).startswith(BASE + "invoice/9/send"))
        with self.assertRaises(ToolError):
            run(server.qbo_send("Bill", "9"))

    def test_batch(self):
        recorder = self.respond(json_response(200, {"BatchItemResponse": [
            {"bId": "2", "Fault": {"Error": [{"Message": "Stale"}], "type": "ValidationFault"}},
            {"bId": "1", "Vendor": {"Id": "11"}},
            {"bId": "3", "QueryResponse": {"Vendor": []}},
        ]}))
        results = run(server.qbo_batch([
            {"operation": "create", "entity": "vendor", "data": {"DisplayName": "Acme"}},
            {"operation": "update", "entity": "Vendor", "data": {"Id": "4", "SyncToken": "0", "Title": "Mr"}},
            {"query": "SELECT * FROM Vendor"},
        ]))
        self.assertEqual([r["bId"] for r in results], ["1", "2", "3"])
        self.assertEqual(results[0], {"bId": "1", "result": {"Id": "11"}, "type": "Vendor"})
        self.assertIn("fault", results[1])
        sent = recorder.body_json()["BatchItemRequest"]
        self.assertEqual(sent[1], {"bId": "2", "operation": "update",
                                   "Vendor": {"Id": "4", "SyncToken": "0", "Title": "Mr", "sparse": True}})
        self.assertEqual(sent[2], {"bId": "3", "Query": "SELECT * FROM Vendor"})
        [entry] = self.audit()
        self.assertEqual(entry["operation"], "batch")
        self.assertEqual(entry["result"][:2], [
            {"bId": "1", "type": "Vendor", "result": {"Id": "11"}},
            {"bId": "2", "fault": {"Error": [{"Message": "Stale"}], "type": "ValidationFault"}},
        ])

    def test_batch_validation_and_dry_run(self):
        with self.assertRaises(ToolError):
            run(server.qbo_batch([{"operation": "void", "entity": "Invoice", "data": {}}]))
        with self.assertRaises(ToolError):
            run(server.qbo_batch([{"operation": "delete", "entity": "Invoice", "data": {"Id": "9"}}]))
        with self.assertRaises(ToolError):
            run(server.qbo_batch([{"query": "SELECT * FROM Bill"}] * 31))
        dry = run(server.qbo_batch([{"operation": "update", "entity": "Bill", "data": {"Id": "1", "SyncToken": "0"},
                                     "sparse": False}, {"query": "SELECT * FROM Bill"}], dry_run=True))
        self.assertEqual(dry[0], {"bId": "1", "operation": "update", "Bill": {"Id": "1", "SyncToken": "0"}, "dry_run": True})

    def test_upload(self):
        receipt = self.tmp / "receipt.pdf"
        receipt.write_bytes(b"%PDF-1.4 receipt")
        recorder = self.respond(json_response(200, {"AttachableResponse": [{"Attachable": {"Id": "200"}}]}))
        links = [{"entity": "bill", "id": "145"}, {"entity": "Vendor", "id": "3"}]
        result = run(server.qbo_upload_attachment(str(receipt), links, note="Office supplies"))
        self.assertEqual(result["Id"], "200")
        body = recorder.requests[0].content
        metadata = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
        self.assertEqual(metadata["ContentType"], "application/pdf")
        self.assertEqual(metadata["AttachableRef"][0]["EntityRef"], {"type": "Bill", "value": "145"})
        self.assertEqual(len(metadata["AttachableRef"]), 2)
        self.assertEqual(self.audit()[0]["request"]["SourcePath"], str(receipt))

    def test_upload_validation(self):
        with self.assertRaises(ToolError):
            run(server.qbo_upload_attachment("receipt.pdf"))
        with self.assertRaises(ToolError):
            run(server.qbo_upload_attachment(str(self.tmp / "missing.pdf")))
        f = self.tmp / "f.txt"
        f.write_text("x")
        with self.assertRaises(ToolError):
            run(server.qbo_upload_attachment(str(f), [{"entity": "Bill"}]))
        with patch.object(server, "MAX_UPLOAD_BYTES", 0):
            with self.assertRaises(ToolError):
                run(server.qbo_upload_attachment(str(f)))


class ReadOnlyModeTests(ServerTestBase):
    read_only = True

    def test_writes_refused(self):
        self.respond()
        receipt = self.tmp / "r.pdf"
        receipt.write_bytes(b"x")
        for call in (server.qbo_create("Customer", {"DisplayName": "A"}),
                     server.qbo_update("Customer", {"Id": "1", "SyncToken": "0"}),
                     server.qbo_delete("Invoice", "1", "0"),
                     server.qbo_deactivate("Customer", "1", "0"),
                     server.qbo_void("Invoice", "1", "0"),
                     server.qbo_send("Invoice", "1"),
                     server.qbo_batch([{"operation": "create", "entity": "Vendor", "data": {}}]),
                     server.qbo_upload_attachment(str(receipt))):
            with self.assertRaises(ToolError) as ctx:
                run(call)
            self.assertTrue(str(ctx.exception).startswith("write: "))
        self.assertEqual(self.recorder.requests, [])
        self.assertEqual(self.audit(), [])

    def test_dry_runs_and_query_batches_allowed(self):
        self.respond(json_response(200, {"BatchItemResponse": [{"bId": "1", "QueryResponse": {"Bill": [{"Id": "1"}]}},
                                                               {"bId": "2", "QueryResponse": {"Vendor": [{"Id": "3"}]}}]}))
        self.assertTrue(run(server.qbo_create("Customer", {"DisplayName": "A"}, dry_run=True))["dry_run"])
        results = run(server.qbo_batch([{"query": "SELECT * FROM Bill"}, {"query": "SELECT * FROM Vendor"}]))
        self.assertEqual([r["bId"] for r in results], ["1", "2"])
        self.assertEqual(self.audit(), [])


class HelperTests(ServerTestBase):
    def test_status_without_hard_expiry(self):
        self.runtime.store.save(make_tokens(hard_ttl=None))
        self.assertIsNone(run(server.qbo_auth_status())["refresh_token_hard_expires_at"])

    def test_perform_records_non_dict_results_as_is(self):
        self.respond()

        async def call(request_id):
            return ["ok", {"Id": "1", "Line": []}]

        self.assertEqual(run(server._perform(self.runtime, "batch", None, [], call)), ["ok", {"Id": "1", "Line": []}])
        self.assertEqual(self.audit()[0]["result"], ["ok", {"Id": "1"}])

    def test_cancelled_write_audited(self):
        # QuickBooks may already have applied a write whose response the cancelled call never saw.
        self.respond()

        async def call(request_id):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            run(server._perform(self.runtime, "create", "Customer", {"DisplayName": "A"}, call))
        [entry] = self.audit()
        self.assertEqual(entry["operation"], "create")
        self.assertIn("cancelled", entry["error"])

    def test_write_reads_token_store_once(self):
        self.respond(json_response(200, {"Customer": {"Id": "58", "SyncToken": "0"}}),
                     json_response(200, {"CompanyInfo": {"CompanyName": "Karellen"}}))
        with patch.object(self.runtime.store, "load", wraps=self.runtime.store.load) as load:
            run(server.qbo_create("Customer", {"DisplayName": "Acme"}))
            self.assertEqual(load.call_count, 1)
            run(server.qbo_get("CompanyInfo"))
            self.assertEqual(load.call_count, 2)
        self.assertEqual(self.audit()[0]["realm_id"], REALM)
        self.assertTrue(str(self.recorder.requests[1].url).startswith(BASE + "companyinfo/%s?" % REALM))


class ServeTests(unittest.TestCase):
    def test_serve_watches_parent_and_runs_stdio(self):
        with patch.object(server, "_watch_parent") as watch, patch.object(server.mcp, "run") as mcp_run:
            server.serve()
        watch.assert_called_once_with()
        mcp_run.assert_called_once_with(transport="stdio")

    def test_watch_parent_exits_when_orphaned(self):
        class Exited(Exception):
            pass

        def fake_exit(code):
            raise Exited(code)

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        with patch("threading.Thread", InlineThread), patch.object(server.os, "getppid", side_effect=[100, 100, 1]), \
                patch.object(server.time, "sleep") as sleep, patch.object(server.os, "_exit", fake_exit):
            with self.assertRaises(Exited):
                server._watch_parent()
        self.assertEqual(sleep.call_count, 2)


class RuntimeTests(unittest.TestCase):
    def tearDown(self):
        server._runtime = None

    def test_runtime_created_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"QBO_MCP_CONFIG_DIR": tmp, "QBO_MCP_ENVIRONMENT": "production",
                                         "QBO_MCP_CLIENT_ID": "i", "QBO_MCP_CLIENT_SECRET": "s"}):
                server._runtime = None
                rt = server._get_runtime()
                self.assertIs(server._get_runtime(), rt)
                self.assertEqual(rt.settings.environment, "production")
                self.assertEqual(rt.client.settings.api_base_url, "https://quickbooks.api.intuit.com")
                self.assertEqual(rt.oauth.client_id, "i")
                self.assertEqual(rt.oauth.discovery_url, "https://developer.api.intuit.com/.well-known/openid_configuration")

    def test_aclose_closes_the_http_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = server.Runtime(make_settings(tmp))
            run(rt.aclose())  # nothing created yet
            client = rt.client
            with patch.object(client, "aclose", wraps=client.aclose) as aclose:
                run(rt.aclose())
            aclose.assert_called_once_with()
            self.assertIsNot(rt.client, client)


if __name__ == "__main__":
    unittest.main()
