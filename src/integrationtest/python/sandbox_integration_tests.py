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

"""End-to-end tests against a real QuickBooks Online sandbox company.

Skipped unless the build runs with `-P qbo_sandbox_tests=true` (build.py passes it on as
QBO_MCP_SANDBOX_TESTS=1). They use the sandbox credentials and tokens configured for
karellen-qbo-mcp (`auth configure` + `auth login`) and create, change and remove records in
the sandbox company, so never point them at production.
"""

import datetime
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

import karellen_qbo_mcp.server as server
from karellen_qbo_mcp.config import load_settings


@unittest.skipUnless(os.environ.get("QBO_MCP_SANDBOX_TESTS") == "1",
                     "run the build with -P qbo_sandbox_tests=true to test against the sandbox")
class SandboxTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings(environment="sandbox")
        cls.tag = "kqm-%s" % uuid.uuid4().hex[:8]
        cls.today = datetime.date.today().isoformat()

    async def asyncSetUp(self):
        # Each test runs on its own event loop, and httpx2 clients are bound to the loop that
        # opened their connections: give every test a fresh runtime and close it afterwards.
        server._runtime = server.Runtime(self.settings)

    async def asyncTearDown(self):
        runtime, server._runtime = server._runtime, None
        await runtime.aclose()

    async def first(self, query):
        rows = await server.qbo_query(query)
        for value in rows.values():
            if isinstance(value, list) and value:
                return value[0]
        self.skipTest("sandbox company has no match for: %s" % query)

    async def test_company_and_status(self):
        status = await server.qbo_auth_status()
        self.assertTrue(status["signed_in"], "sign in first: karellen-qbo-mcp auth login")
        info = await server.qbo_get("CompanyInfo")
        self.assertIn("CompanyName", info)
        self.assertEqual((await server.qbo_get("Preferences")).get("domain"), "QBO")

    async def test_customer_lifecycle(self):
        customer = await server.qbo_create("Customer", {"DisplayName": "%s customer" % self.tag})
        preview = await server.qbo_update("Customer", {"Id": customer["Id"], "SyncToken": customer["SyncToken"],
                                                       "Notes": "checked"}, dry_run=True)
        self.assertTrue(preview["sync_token_matches"])
        self.assertIn("Notes", preview["changes"])
        updated = await server.qbo_update("Customer", {"Id": customer["Id"], "SyncToken": customer["SyncToken"],
                                                       "Notes": "checked"})
        self.assertEqual(updated["Notes"], "checked")
        self.assertEqual(updated["DisplayName"], customer["DisplayName"])  # sparse update kept the other fields
        changed = await server.qbo_cdc(["Customer"], (datetime.datetime.now(datetime.timezone.utc)
                                                      - datetime.timedelta(hours=1)).isoformat())
        self.assertIn(customer["Id"], str(changed))
        inactive = await server.qbo_deactivate("Customer", customer["Id"], updated["SyncToken"])
        self.assertFalse(inactive["Active"])

    async def test_invoice_pdf_attachment_void_delete(self):
        item = await self.first("SELECT * FROM Item WHERE Type = 'Service' MAXRESULTS 1")
        customer = await server.qbo_create("Customer", {"DisplayName": "%s invoice customer" % self.tag})
        invoice = await server.qbo_create("Invoice", {
            "CustomerRef": {"value": customer["Id"]}, "TxnDate": self.today,
            "Line": [{"Amount": 25.0, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemRef": {"value": item["Id"]}, "Qty": 1, "UnitPrice": 25.0}},
                     {"Amount": 10.0, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemRef": {"value": item["Id"]}, "Qty": 2, "UnitPrice": 5.0}}]})
        self.assertEqual(float(invoice["TotalAmt"]), 35.0)
        with tempfile.TemporaryDirectory() as tmp:
            pdf = await server.qbo_download_pdf("Invoice", invoice["Id"], str(Path(tmp) / "invoice.pdf"))
            self.assertGreater(pdf["bytes"], 100)
            source = Path(tmp) / "note.txt"
            source.write_text("receipt for %s" % self.tag)
            attachable = await server.qbo_upload_attachment(str(source), [{"entity": "Invoice", "id": invoice["Id"]}],
                                                            note=self.tag)
            copy = Path(tmp) / "copy.txt"
            await server.qbo_download_attachment(attachable["Id"], str(copy))
            self.assertEqual(copy.read_text(), source.read_text())
            await server.qbo_delete("Attachable", attachable["Id"], attachable["SyncToken"])
        # Linking the attachment moved the invoice's SyncToken on.
        invoice = await server.qbo_get("Invoice", invoice["Id"])
        voided = await server.qbo_void("Invoice", invoice["Id"], invoice["SyncToken"])
        self.assertEqual(float(voided["TotalAmt"]), 0.0)
        deleted = await server.qbo_delete("Invoice", voided["Id"], voided["SyncToken"])
        self.assertEqual(deleted["status"], "Deleted")
        await server.qbo_deactivate("Customer", customer["Id"], customer["SyncToken"])

    async def test_journal_entry_and_batch(self):
        bank = await self.first("SELECT * FROM Account WHERE AccountType = 'Bank' MAXRESULTS 1")
        expense = await self.first("SELECT * FROM Account WHERE AccountType = 'Expense' MAXRESULTS 1")
        entry = await server.qbo_create("JournalEntry", {"TxnDate": self.today, "PrivateNote": self.tag, "Line": [
            {"Amount": 12.34, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": expense["Id"]}}},
            {"Amount": 12.34, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": bank["Id"]}}}]})
        results = await server.qbo_batch([
            {"query": "SELECT * FROM JournalEntry WHERE Id = '%s'" % entry["Id"]},
            {"operation": "create", "entity": "Vendor", "data": {"DisplayName": "%s vendor" % self.tag}},
            {"operation": "delete", "entity": "JournalEntry", "data": {"Id": entry["Id"], "SyncToken": entry["SyncToken"]}},
        ])
        self.assertEqual([r["bId"] for r in results], ["1", "2", "3"])
        self.assertTrue(all("fault" not in r for r in results), results)
        vendor = results[1]["result"]
        await server.qbo_deactivate("Vendor", vendor["Id"], vendor["SyncToken"])

    async def test_reports_flatten(self):
        year_start = datetime.date.today().replace(month=1, day=1).isoformat()
        for name in ("ProfitAndLoss", "BalanceSheet", "TrialBalance", "AgedReceivables", "GeneralLedger"):
            with self.subTest(report=name):
                flat = await server.qbo_report(name, {"start_date": year_start, "end_date": self.today})
                self.assertNotIn("raw", flat, "flattening failed for %s: %s" % (name, flat.get("note")))
                self.assertEqual(flat["header"]["ReportName"], name)

    async def test_count_and_paged_query(self):
        count = await server.qbo_query("SELECT COUNT(*) FROM Account")
        self.assertGreater(count["totalCount"], 0)
        everything = await server.qbo_query("SELECT * FROM Account", fetch_all=True, limit=5000)
        self.assertEqual(everything["count"], count["totalCount"])

    async def test_query_and_report_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / "accounts.json"
            saved = await server.qbo_query("SELECT * FROM Account", fetch_all=True, limit=5000,
                                           output_path=str(accounts))
            inline = await server.qbo_query("SELECT * FROM Account", fetch_all=True, limit=5000)
            self.assertEqual(json.loads(accounts.read_text()), inline)
            self.assertEqual(saved["count"], inline["count"])
            report = Path(tmp) / "pnl.json"
            await server.qbo_report("ProfitAndLoss", {"start_date": self.today, "end_date": self.today},
                                    output_path=str(report))
            self.assertEqual(json.loads(report.read_text())["header"]["ReportName"], "ProfitAndLoss")


if __name__ == "__main__":
    unittest.main()
