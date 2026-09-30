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

"""QuickBooks report names and a flattener for the nested report row structure.

Intuit moved reports to a new reporting service in 2026 and dropped some undocumented
reports, so the name list is guidance rather than a whitelist: any report name is
passed through, and QuickBooks decides whether it exists.
"""

import re

KNOWN_REPORTS = {
    "AccountList": "Chart of accounts with balances",
    "AgedPayableDetail": "Open bills by aging bucket, per transaction",
    "AgedPayables": "Open bills by aging bucket, per vendor",
    "AgedReceivableDetail": "Open invoices by aging bucket, per transaction",
    "AgedReceivables": "Open invoices by aging bucket, per customer",
    "BalanceSheet": "Balance sheet",
    "CashFlow": "Statement of cash flows",
    "ClassSales": "Sales by class",
    "CustomerBalance": "Open balance per customer",
    "CustomerBalanceDetail": "Open balance per customer, per transaction",
    "CustomerIncome": "Income per customer",
    "CustomerSales": "Sales per customer",
    "DepartmentSales": "Sales by department/location",
    "GeneralLedger": "General ledger detail",
    "InventoryValuationSummary": "Inventory valuation summary",
    "ItemSales": "Sales by product/service",
    "JournalReport": "Journal of all transactions with their postings",
    "ProfitAndLoss": "Profit and loss (income statement)",
    "ProfitAndLossDetail": "Profit and loss, per transaction",
    "TaxSummary": "Sales tax summary",
    "TransactionList": "Transaction list",
    "TransactionListByCustomer": "Transaction list grouped by customer",
    "TransactionListByVendor": "Transaction list grouped by vendor",
    "TransactionListWithSplits": "Transaction list with split lines",
    "TrialBalance": "Trial balance",
    "VendorBalance": "Open balance per vendor",
    "VendorBalanceDetail": "Open balance per vendor, per transaction",
    "VendorExpenses": "Expenses per vendor",
}

_REPORT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


class ReportError(Exception):
    pass


def validate_report_name(name: str) -> str:
    name = (name or "").strip()
    if not _REPORT_NAME.match(name):
        raise ReportError("Invalid report name %r; use a QuickBooks report name such as ProfitAndLoss" % name)
    return name


def _row(kind: str, depth: int, group, col_data) -> dict:
    cells = col_data or []
    return {"kind": kind, "depth": depth, "group": group,
            "values": [cell.get("value") for cell in cells], "ids": [cell.get("id") for cell in cells]}


def _walk(rows, depth: int, out: list):
    for row in (rows or {}).get("Row") or []:
        group = row.get("group")
        if "Header" in row:
            out.append(_row("header", depth, group, row["Header"].get("ColData")))
        if "ColData" in row:
            out.append(_row("data", depth, group, row.get("ColData")))
        if "Rows" in row:
            _walk(row["Rows"], depth + 1, out)
        if "Summary" in row:
            out.append(_row("summary", depth, group, row["Summary"].get("ColData")))


def flatten_report(report: dict) -> dict:
    """Turn the nested Header/Rows/Summary tree into an ordered list of rows with a depth.

    Each row keeps its cell values and, where QuickBooks provides them, the ids of the
    referenced entities (accounts, customers, transactions). Raises ReportError if the
    response does not have the expected shape, so the caller can fall back to raw output.
    """
    if not isinstance(report, dict) or "Header" not in report:
        raise ReportError("Report response has no Header")
    columns = []
    for column in (report.get("Columns") or {}).get("Column") or []:
        entry = {"title": column.get("ColTitle"), "type": column.get("ColType")}
        meta = {m.get("Name"): m.get("Value") for m in column.get("MetaData") or [] if isinstance(m, dict)}
        if meta:
            entry["metadata"] = meta
        columns.append(entry)
    rows = []
    try:
        _walk(report.get("Rows"), 0, rows)
    except (AttributeError, TypeError) as e:
        raise ReportError("Unexpected report row structure: %s" % e) from e
    return {"header": report["Header"], "columns": columns, "rows": rows}
