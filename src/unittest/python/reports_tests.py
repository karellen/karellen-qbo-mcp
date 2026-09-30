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

import unittest

from karellen_qbo_mcp.reports import ReportError, flatten_report, validate_report_name

PROFIT_AND_LOSS = {
    "Header": {"ReportName": "ProfitAndLoss", "StartPeriod": "2026-01-01", "EndPeriod": "2026-06-30", "Currency": "USD"},
    "Columns": {"Column": [
        {"ColTitle": "", "ColType": "Account", "MetaData": [{"Name": "ColKey", "Value": "account"}]},
        {"ColTitle": "Total", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "total"}]},
    ]},
    "Rows": {"Row": [
        {"type": "Section", "group": "Income",
         "Header": {"ColData": [{"value": "Income"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Data", "ColData": [{"value": "Consulting", "id": "79"}, {"value": "12000.00"}]},
             {"type": "Data", "ColData": [{"value": "Training", "id": "80"}, {"value": "3000.00"}]},
         ]},
         "Summary": {"ColData": [{"value": "Total Income"}, {"value": "15000.00"}]}},
        {"type": "Section", "group": "Expenses",
         "Header": {"ColData": [{"value": "Expenses"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Section", "Header": {"ColData": [{"value": "Payroll"}, {"value": ""}]},
              "Rows": {"Row": [{"type": "Data", "ColData": [{"value": "Wages", "id": "90"}, {"value": "8000.00"}]},
                               {"type": "Data", "ColData": [{"value": "Taxes", "id": "91"}, {"value": "800.00"}]}]},
              "Summary": {"ColData": [{"value": "Total Payroll"}, {"value": "8800.00"}]}},
         ]},
         "Summary": {"ColData": [{"value": "Total Expenses"}, {"value": "8800.00"}]}},
        {"type": "Section", "group": "NetIncome",
         "Summary": {"ColData": [{"value": "Net Income"}, {"value": "6200.00"}]}},
    ]},
}


class FlattenReportTests(unittest.TestCase):
    def test_flatten_nested_sections(self):
        flat = flatten_report(PROFIT_AND_LOSS)
        self.assertEqual(flat["header"]["ReportName"], "ProfitAndLoss")
        self.assertEqual(flat["columns"], [
            {"title": "", "type": "Account", "metadata": {"ColKey": "account"}},
            {"title": "Total", "type": "Money", "metadata": {"ColKey": "total"}},
        ])
        rows = [(r["kind"], r["depth"], r["values"][0], r["values"][1]) for r in flat["rows"]]
        self.assertEqual(rows, [
            ("header", 0, "Income", ""),
            ("data", 1, "Consulting", "12000.00"),
            ("data", 1, "Training", "3000.00"),
            ("summary", 0, "Total Income", "15000.00"),
            ("header", 0, "Expenses", ""),
            ("header", 1, "Payroll", ""),
            ("data", 2, "Wages", "8000.00"),
            ("data", 2, "Taxes", "800.00"),
            ("summary", 1, "Total Payroll", "8800.00"),
            ("summary", 0, "Total Expenses", "8800.00"),
            ("summary", 0, "Net Income", "6200.00"),
        ])
        self.assertEqual(flat["rows"][1]["ids"], ["79", None])
        self.assertEqual(flat["rows"][0]["group"], "Income")

    def test_report_without_rows(self):
        flat = flatten_report({"Header": {"ReportName": "TrialBalance", "Option": [{"Name": "NoReportData", "Value": "true"}]}})
        self.assertEqual((flat["columns"], flat["rows"]), ([], []))

    def test_unexpected_shape(self):
        with self.assertRaises(ReportError):
            flatten_report({"Rows": {}})
        with self.assertRaises(ReportError):
            flatten_report({"Header": {}, "Rows": {"Row": [{"ColData": "not-a-list"}]}})

    def test_validate_report_name(self):
        self.assertEqual(validate_report_name(" ProfitAndLoss "), "ProfitAndLoss")
        for bad in ("", "../company", "Profit And Loss", "reports/BalanceSheet"):
            with self.subTest(name=bad):
                with self.assertRaises(ReportError):
                    validate_report_name(bad)


if __name__ == "__main__":
    unittest.main()
