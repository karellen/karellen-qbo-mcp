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

import logging
import tempfile
import unittest

import httpx2

from karellen_qbo_mcp.client import QboAuthError, QboApiError, QboError, is_count_query
from karellen_qbo_mcp.entities import get_entity, EntityError
from karellen_qbo_mcp.oauth import OAuthError
from qbo_test_support import (NOW, REALM, BASE, make_settings, make_tokens, FakeOAuth, Recorder, json_response,
                              FakeSleep, make_client, run)


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def client(self, *responses, oauth=None, tokens=None, now=NOW):
        self.recorder = Recorder(*responses)
        self.sleep = FakeSleep()
        self.oauth = oauth or FakeOAuth()
        client, store = make_client(self.settings, self.recorder, oauth=self.oauth, now=now, sleep=self.sleep)
        if tokens is not False:
            store.save(tokens or make_tokens())
        self.store = store
        return client

    def request(self, index=-1) -> httpx2.Request:
        return self.recorder.requests[index]


class AuthorizationTests(ClientTestBase):
    def test_not_signed_in(self):
        client = self.client(tokens=False)
        with self.assertRaises(QboAuthError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("Not signed in", str(ctx.exception))
        self.assertEqual(self.recorder.requests, [])

    def test_valid_access_token_used_without_refresh(self):
        client = self.client(json_response(200, {"Customer": {"Id": "1"}}))
        run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(self.request().headers["Authorization"], "Bearer access-1")
        self.assertEqual(self.oauth.refreshes, [])

    def test_expiring_access_token_refreshed_and_persisted(self):
        client = self.client(json_response(200, {"Customer": {"Id": "1"}}), tokens=make_tokens(access_ttl=60.0))
        run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(self.oauth.refreshes, [("refresh-1", REALM)])
        self.assertEqual(self.request().headers["Authorization"], "Bearer access-r1")
        self.assertEqual(self.store.load().refresh_token, "refresh-r1")

    def test_refresh_skipped_when_another_process_already_refreshed(self):
        client = self.client(tokens=make_tokens(access_ttl=60.0))
        stale = self.store.load()
        self.store.save(make_tokens(label="other"))
        tokens = run(client._refresh(stale))
        self.assertEqual(tokens.access_token, "access-other")
        self.assertEqual(self.oauth.refreshes, [])

    def test_expired_refresh_token_requires_sign_in(self):
        client = self.client(tokens=make_tokens(access_ttl=-10.0, refresh_ttl=-1.0))
        with self.assertRaises(QboAuthError) as ctx:
            run(client.tokens())
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.oauth.refreshes, [])

    def test_hard_expired_refresh_token_requires_sign_in(self):
        client = self.client(tokens=make_tokens(access_ttl=-10.0, hard_ttl=-1.0))
        with self.assertRaises(QboAuthError):
            run(client.tokens())

    def test_invalid_grant_becomes_auth_error(self):
        client = self.client(tokens=make_tokens(access_ttl=-10.0),
                             oauth=FakeOAuth(OAuthError("revoked", error="invalid_grant", status_code=400)))
        with self.assertRaises(QboAuthError):
            run(client.tokens())

    def test_other_oauth_failure_is_general_error(self):
        client = self.client(tokens=make_tokens(access_ttl=-10.0), oauth=FakeOAuth(OAuthError("down", status_code=503)))
        with self.assertRaises(QboError) as ctx:
            run(client.tokens())
        self.assertNotIsInstance(ctx.exception, QboAuthError)

    def test_401_refreshes_once_and_retries(self):
        fault = {"fault": {"error": [{"message": "AuthenticationFailed", "code": "3200"}], "type": "AUTHENTICATION"}}
        client = self.client(json_response(401, fault), json_response(200, {"Customer": {"Id": "1"}}))
        self.assertEqual(run(client.read(get_entity("Customer"), "1")), {"Id": "1"})
        self.assertEqual([r.headers["Authorization"] for r in self.recorder.requests],
                         ["Bearer access-1", "Bearer access-r1"])

    def test_second_401_is_reported(self):
        fault = {"fault": {"error": [{"message": "AuthenticationFailed", "detail": "Token invalid", "code": "3200"}],
                           "type": "AUTHENTICATION"}}
        client = self.client(json_response(401, fault), json_response(401, fault))
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.fault_type, "AUTHENTICATION")
        self.assertEqual(len(self.oauth.refreshes), 1)


class TransportTests(ClientTestBase):
    def test_minorversion_on_every_request(self):
        client = self.client(json_response(200, {"Customer": {"Id": "7"}}))
        run(client.read(get_entity("Customer"), "7"))
        request = self.request()
        self.assertEqual(str(request.url).split("?")[0], BASE + "customer/7")
        self.assertEqual(request.url.params["minorversion"], "75")
        self.assertNotIn("requestid", request.url.params)
        self.assertEqual(request.headers["Accept"], "application/json")

    def test_throttled_request_waits_and_retries(self):
        client = self.client(httpx2.Response(429, text="Too many", headers={"Retry-After": "5"}),
                             httpx2.Response(429, text="Too many"),
                             json_response(200, {"Customer": {"Id": "1"}}))
        run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(self.sleep.calls, [5.0, 60.0])

    def test_throttling_gives_up_after_retries(self):
        client = self.client(*[httpx2.Response(429, text="Too many", headers={"Retry-After": "1"})] * 3)
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("throttling", str(ctx.exception))
        self.assertEqual(len(self.recorder.requests), 3)

    def test_transient_server_error_retried_with_backoff(self):
        client = self.client(httpx2.Response(503, text="busy"), httpx2.Response(502, text="bad gateway"),
                             json_response(200, {"Customer": {"Id": "1"}}))
        run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(self.sleep.calls, [1, 2])

    def test_transport_error_retried_then_reported(self):
        failure = httpx2.ConnectError("connection refused")
        client = self.client(failure, failure, failure, failure)
        with self.assertRaises(QboError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("Cannot reach QuickBooks", str(ctx.exception))
        self.assertEqual(len(self.recorder.requests), 4)

    def test_fault_parsed_into_message(self):
        client = self.client(json_response(400, {"Fault": {"Error": [
            {"Message": "Object Not Found", "Detail": "Object Not Found : Something you're trying to use has been made "
                                                      "inactive", "code": "610", "element": ""},
            {"Message": "Required param missing", "Detail": "Required param missing", "code": "2020",
             "element": "Line"}], "type": "ValidationFault"}}, headers={"intuit_tid": "tid-123"}))
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        e = ctx.exception
        self.assertEqual(e.fault_type, "ValidationFault")
        self.assertEqual([err["code"] for err in e.errors], ["610", "2020"])
        self.assertIn("made inactive [code 610]", str(e))
        self.assertIn("Required param missing [code 2020, element Line]", str(e))
        self.assertIn("intuit_tid tid-123", str(e))

    def test_fault_in_successful_response_raises(self):
        fault = {"Fault": {"Error": [{"Message": "Stale object", "code": "5010"}], "type": "ValidationFault"}}
        client = self.client(json_response(200, fault))
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("Stale object", str(ctx.exception))
        self.assertIn("re-read it and retry with the current SyncToken", str(ctx.exception))

    def test_non_json_error_body_reported(self):
        client = self.client(httpx2.Response(400, text="<html>Bad</html>"))
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("<html>Bad</html>", str(ctx.exception))

    def test_missing_response_element(self):
        client = self.client(json_response(200, {"time": "now"}))
        with self.assertRaises(QboError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("no Customer element", str(ctx.exception))

    def test_requests_paced(self):
        clock = [NOW]
        recorder = Recorder(json_response(200, {"Customer": {}}), json_response(200, {"Customer": {}}))
        sleep = FakeSleep()
        client, store = make_client(self.settings, recorder, sleep=sleep)
        client._clock = lambda: clock[0]
        client._min_interval = 0.1
        store.save(make_tokens())
        run(client.read(get_entity("Customer"), "1"))
        run(client.read(get_entity("Customer"), "2"))
        self.assertEqual(len(sleep.calls), 1)
        self.assertAlmostEqual(sleep.calls[0], 0.1, places=5)  # NOW is ~1.8e9, so float sums lose digits


class ReadAndQueryTests(ClientTestBase):
    def test_company_info_uses_realm_path(self):
        client = self.client(json_response(200, {"CompanyInfo": {"CompanyName": "Acme"}}))
        self.assertEqual(run(client.read(get_entity("companyinfo"))), {"CompanyName": "Acme"})
        self.assertTrue(str(self.request().url).startswith(BASE + "companyinfo/" + REALM))

    def test_preferences_path(self):
        client = self.client(json_response(200, {"Preferences": {"SyncToken": "3"}}))
        run(client.read(get_entity("Preferences")))
        self.assertTrue(str(self.request().url).startswith(BASE + "preferences?"))

    def test_read_requires_id(self):
        client = self.client()
        with self.assertRaises(QboError):
            run(client.read(get_entity("Invoice"), None))

    def test_query_posts_statement_as_text(self):
        client = self.client(json_response(200, {"QueryResponse": {"Customer": [{"Id": "1"}, {"Id": "2"}]}}))
        result = run(client.query("SELECT * FROM Customer"))
        self.assertEqual(len(result["Customer"]), 2)
        request = self.request()
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Content-Type"], "application/text")
        self.assertEqual(request.content, b"SELECT * FROM Customer")

    def test_query_all_pages_until_short_page(self):
        page1 = [{"Id": str(i)} for i in range(1000)]
        page2 = [{"Id": "1000"}, {"Id": "1001"}]
        client = self.client(json_response(200, {"QueryResponse": {"Invoice": page1, "startPosition": 1, "maxResults": 1000}}),
                             json_response(200, {"QueryResponse": {"Invoice": page2, "startPosition": 1001, "maxResults": 2}}))
        result = run(client.query_all("SELECT * FROM Invoice;", 5000))
        self.assertEqual(result["entity"], "Invoice")
        self.assertEqual(result["count"], 1002)
        self.assertFalse(result["truncated"])
        self.assertEqual([r.content.decode() for r in self.recorder.requests],
                         ["SELECT * FROM Invoice STARTPOSITION 1 MAXRESULTS 1000",
                          "SELECT * FROM Invoice STARTPOSITION 1001 MAXRESULTS 1000"])

    def test_query_all_truncates_at_limit(self):
        client = self.client(json_response(200, {"QueryResponse": {"Bill": [{"Id": "1"}, {"Id": "2"}]}}),
                             json_response(200, {"QueryResponse": {"Bill": [{"Id": "3"}]}}))
        result = run(client.query_all("SELECT * FROM Bill", 2))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["count"], 2)
        self.assertEqual([r.content for r in self.recorder.requests],
                         [b"SELECT * FROM Bill STARTPOSITION 1 MAXRESULTS 2",
                          b"SELECT * FROM Bill STARTPOSITION 3 MAXRESULTS 1"])

    def test_query_all_exactly_at_limit_is_not_truncated(self):
        client = self.client(json_response(200, {"QueryResponse": {"Bill": [{"Id": "1"}, {"Id": "2"}]}}),
                             json_response(200, {"QueryResponse": {}}))
        result = run(client.query_all("SELECT * FROM Bill", 2))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["count"], 2)

    def test_query_all_count_query_returns_total(self):
        client = self.client(json_response(200, {"QueryResponse": {"totalCount": 57}}))
        result = run(client.query_all("SELECT COUNT(*) FROM Customer WHERE Active = true", 1000))
        self.assertEqual(result, {"totalCount": 57})
        self.assertEqual(self.request().content, b"SELECT COUNT(*) FROM Customer WHERE Active = true")

    def test_query_all_empty_result(self):
        client = self.client(json_response(200, {"QueryResponse": {}}))
        self.assertEqual(run(client.query_all("SELECT * FROM Bill", 10)),
                         {"entity": None, "rows": [], "count": 0, "truncated": False})

    def test_query_all_hands_pages_to_callback_instead_of_collecting(self):
        page1 = [{"Id": str(i)} for i in range(1000)]
        page2 = [{"Id": "1000"}, {"Id": "1001"}]
        client = self.client(json_response(200, {"QueryResponse": {"Invoice": page1}}),
                             json_response(200, {"QueryResponse": {"Invoice": page2}}))
        pages = []

        async def on_page(rows):
            pages.append(rows)

        result = run(client.query_all("SELECT * FROM Invoice", 5000, on_page=on_page))
        self.assertEqual(result, {"entity": "Invoice", "count": 1002, "truncated": False})
        self.assertEqual(pages, [page1, page2])

    def test_query_all_callback_with_truncation(self):
        client = self.client(json_response(200, {"QueryResponse": {"Bill": [{"Id": "1"}, {"Id": "2"}]}}),
                             json_response(200, {"QueryResponse": {"Bill": [{"Id": "3"}]}}))
        pages = []

        async def on_page(rows):
            pages.append(rows)

        result = run(client.query_all("SELECT * FROM Bill", 2, on_page=on_page))
        self.assertEqual(result, {"entity": "Bill", "count": 2, "truncated": True})
        self.assertEqual(pages, [[{"Id": "1"}, {"Id": "2"}]])

    def test_count_query_detection(self):
        self.assertTrue(is_count_query("  select count(*) FROM Bill"))
        self.assertFalse(is_count_query("SELECT * FROM Bill"))

    def test_query_all_rejects_paging_clauses(self):
        client = self.client()
        with self.assertRaises(QboError):
            run(client.query_all("SELECT * FROM Bill maxresults 10", 10))


class WriteTests(ClientTestBase):
    def test_create_sends_request_id(self):
        client = self.client(json_response(200, {"Customer": {"Id": "58", "SyncToken": "0"}}))
        result = run(client.create(get_entity("Customer"), {"DisplayName": "Acme"}, "rid-1"))
        self.assertEqual(result["Id"], "58")
        request = self.request()
        self.assertTrue(str(request.url).startswith(BASE + "customer?"))
        self.assertEqual(request.url.params["requestid"], "rid-1")
        self.assertEqual(self.recorder.body_json(), {"DisplayName": "Acme"})

    def test_create_rejected_for_read_only_entity(self):
        client = self.client()
        with self.assertRaises(EntityError):
            run(client.create(get_entity("TaxCode"), {}, "rid"))

    def test_sparse_update(self):
        client = self.client(json_response(200, {"Invoice": {"Id": "9", "SyncToken": "3"}}))
        data = {"Id": "9", "SyncToken": "2", "PrivateNote": "checked"}
        run(client.update(get_entity("Invoice"), data, True, "rid"))
        self.assertEqual(self.recorder.body_json(), {"Id": "9", "SyncToken": "2", "PrivateNote": "checked", "sparse": True})
        self.assertNotIn("sparse", data)

    def test_sparse_update_fills_required_fields_from_current_record(self):
        current = {"Id": "4", "SyncToken": "5", "VendorRef": {"value": "91"}, "PrivateNote": "old", "TotalAmt": 7.04}
        client = self.client(json_response(200, {"Bill": current}), json_response(200, {"Bill": {"Id": "4"}}))
        data = {"Id": "4", "SyncToken": "3", "PrivateNote": "new"}
        run(client.update(get_entity("Bill"), data, True, "rid"))
        self.assertEqual(self.request(0).method, "GET")
        self.assertTrue(str(self.request(0).url).startswith(BASE + "bill/4?"))
        # Only the missing required field is copied; the caller's SyncToken is kept, so a stale one still fails.
        self.assertEqual(self.recorder.body_json(), {"Id": "4", "SyncToken": "3", "PrivateNote": "new",
                                                     "VendorRef": {"value": "91"}, "sparse": True})
        self.assertEqual(data, {"Id": "4", "SyncToken": "3", "PrivateNote": "new"})

    def test_sparse_update_with_required_fields_does_not_read(self):
        client = self.client(json_response(200, {"Transfer": {"Id": "7"}}))
        data = {"Id": "7", "SyncToken": "1", "FromAccountRef": {"value": "35"}, "ToAccountRef": {"value": "36"},
                "Amount": 10.0, "PrivateNote": "moved"}
        run(client.update(get_entity("Transfer"), data, True, "rid"))
        self.assertEqual(len(self.recorder.requests), 1)
        self.assertEqual(self.recorder.body_json(), dict(data, sparse=True))

    def test_sparse_update_of_term_copies_the_fields_the_record_has(self):
        standard = {"Id": "3", "SyncToken": "0", "Name": "Net 30", "Type": "STANDARD", "DueDays": 30, "Active": True}
        date_driven = {"Id": "8", "SyncToken": "2", "Name": "15th", "Type": "DATE_DRIVEN", "DayOfMonthDue": 15,
                       "Active": True}
        client = self.client(json_response(200, {"Term": standard}), json_response(200, {"Term": standard}),
                             json_response(200, {"Term": date_driven}), json_response(200, {"Term": date_driven}))
        run(client.update(get_entity("Term"), {"Id": "3", "SyncToken": "0", "Active": False}, True, "rid"))
        self.assertEqual(self.recorder.body_json(1), {"Id": "3", "SyncToken": "0", "Active": False, "Name": "Net 30",
                                                      "Type": "STANDARD", "DueDays": 30, "sparse": True})
        run(client.update(get_entity("Term"), {"Id": "8", "SyncToken": "2", "Name": "Mid-month"}, True, "rid"))
        self.assertEqual(self.recorder.body_json(3), {"Id": "8", "SyncToken": "2", "Name": "Mid-month",
                                                      "Type": "DATE_DRIVEN", "DayOfMonthDue": 15, "sparse": True})

    def test_full_update_does_not_fill(self):
        client = self.client(json_response(200, {"Bill": {"Id": "4"}}))
        run(client.update(get_entity("Bill"), {"Id": "4", "SyncToken": "3", "PrivateNote": "new"}, False, "rid"))
        self.assertEqual(len(self.recorder.requests), 1)
        self.assertNotIn("VendorRef", self.recorder.body_json())

    def test_full_update_has_no_sparse_flag(self):
        client = self.client(json_response(200, {"Invoice": {"Id": "9"}}))
        run(client.update(get_entity("Invoice"), {"Id": "9", "SyncToken": "2"}, False, "rid"))
        self.assertNotIn("sparse", self.recorder.body_json())

    def test_update_requires_sync_token_and_id(self):
        client = self.client()
        with self.assertRaises(QboError):
            run(client.update(get_entity("Invoice"), {"Id": "9"}, True, "rid"))
        with self.assertRaises(QboError):
            run(client.update(get_entity("Invoice"), {"SyncToken": "1"}, True, "rid"))

    def test_singleton_update_without_id(self):
        client = self.client(json_response(200, {"Preferences": {"SyncToken": "4"}}))
        run(client.update(get_entity("Preferences"), {"SyncToken": "3", "EmailMessagesPrefs": {}}, True, "rid"))
        self.assertTrue(str(self.request().url).startswith(BASE + "preferences?"))

    def test_delete(self):
        client = self.client(json_response(200, {"Invoice": {"status": "Deleted", "Id": "9"}}))
        run(client.delete(get_entity("Invoice"), "9", "4", "rid"))
        request = self.request()
        self.assertEqual(request.url.params["operation"], "delete")
        self.assertEqual(self.recorder.body_json(), {"Id": "9", "SyncToken": "4"})

    def test_delete_rejected_for_name_list(self):
        client = self.client()
        with self.assertRaises(EntityError) as ctx:
            run(client.delete(get_entity("Customer"), "1", "0", "rid"))
        self.assertIn("deactivate it instead", str(ctx.exception))

    def test_deactivate(self):
        client = self.client(json_response(200, {"Vendor": {"Id": "3", "Active": False}}))
        run(client.deactivate(get_entity("Vendor"), "3", "1", "rid"))
        self.assertEqual(self.recorder.body_json(), {"Id": "3", "SyncToken": "1", "sparse": True, "Active": False})

    def test_deactivate_fills_required_name(self):
        client = self.client(json_response(200, {"Class": {"Id": "5", "SyncToken": "1", "Name": "Consulting"}}),
                             json_response(200, {"Class": {"Id": "5", "Active": False}}))
        run(client.deactivate(get_entity("Class"), "5", "1", "rid"))
        self.assertEqual(self.recorder.body_json(), {"Id": "5", "SyncToken": "1", "Active": False, "Name": "Consulting",
                                                     "sparse": True})

    def test_void_invoice_uses_operation_void(self):
        client = self.client(json_response(200, {"Invoice": {"Id": "9"}}))
        run(client.void(get_entity("Invoice"), "9", "2", "rid"))
        self.assertEqual(self.request().url.params["operation"], "void")
        self.assertEqual(self.recorder.body_json(), {"Id": "9", "SyncToken": "2"})

    def test_void_payment_uses_include_void(self):
        client = self.client(json_response(200, {"Payment": {"Id": "5"}}))
        run(client.void(get_entity("Payment"), "5", "0", "rid"))
        params = self.request().url.params
        self.assertEqual((params["operation"], params["include"]), ("update", "void"))
        self.assertEqual(self.recorder.body_json(), {"Id": "5", "SyncToken": "0", "sparse": True})

    def test_void_unsupported(self):
        client = self.client()
        with self.assertRaises(EntityError):
            run(client.void(get_entity("Bill"), "5", "0", "rid"))

    def test_send_with_recipient(self):
        client = self.client(json_response(200, {"Invoice": {"Id": "9", "EmailStatus": "EmailSent"}}))
        run(client.send(get_entity("Invoice"), "9", "ap@example.com", "rid"))
        request = self.request()
        self.assertTrue(str(request.url).startswith(BASE + "invoice/9/send?"))
        self.assertEqual(request.url.params["sendTo"], "ap@example.com")
        self.assertEqual(request.headers["Content-Type"], "application/octet-stream")

    def test_send_without_recipient(self):
        client = self.client(json_response(200, {"Estimate": {"Id": "2"}}))
        run(client.send(get_entity("Estimate"), "2", None, "rid"))
        self.assertNotIn("sendTo", self.request().url.params)

    def test_pdf(self):
        client = self.client(httpx2.Response(200, content=b"%PDF-1.4 ...", headers={"Content-Type": "application/pdf"}))
        self.assertEqual(run(client.pdf(get_entity("Invoice"), "9")), b"%PDF-1.4 ...")
        self.assertEqual(self.request().headers["Accept"], "application/pdf")

    def test_pdf_rejects_non_pdf(self):
        client = self.client(httpx2.Response(200, content=b"<html/>"))
        with self.assertRaises(QboError):
            run(client.pdf(get_entity("Invoice"), "9"))


class BatchCdcReportAttachmentTests(ClientTestBase):
    def test_batch(self):
        items = [{"bId": "1", "operation": "create", "Vendor": {"DisplayName": "A"}},
                 {"bId": "2", "Query": "SELECT * FROM Vendor"}]
        client = self.client(json_response(200, {"BatchItemResponse": [{"bId": "1", "Vendor": {"Id": "1"}},
                                                                       {"bId": "2", "QueryResponse": {}}]}))
        self.assertEqual(len(run(client.batch(items, "rid"))), 2)
        self.assertEqual(self.recorder.body_json(), {"BatchItemRequest": items})
        self.assertEqual(self.request().url.params["requestid"], "rid")

    def test_batch_limits(self):
        client = self.client()
        with self.assertRaises(QboError):
            run(client.batch([], "rid"))
        with self.assertRaises(QboError):
            run(client.batch([{"bId": str(i)} for i in range(31)], "rid"))

    def test_cdc(self):
        cdc = [{"QueryResponse": [{"Invoice": [{"Id": "9"}, {"Id": "10"}]}, {"Payment": [{"Id": "5"}, {"Id": "6"}]}]},
               {"QueryResponse": [{"Invoice": [{"Id": "11"}]}, {"Payment": []}]}]
        client = self.client(json_response(200, {"CDCResponse": cdc}))
        self.assertEqual(run(client.cdc(["Invoice", "Payment"], "2026-09-01T00:00:00Z")), cdc)
        params = self.request().url.params
        self.assertEqual(params["entities"], "Invoice,Payment")
        self.assertEqual(params["changedSince"], "2026-09-01T00:00:00Z")

    def test_report(self):
        client = self.client(json_response(200, {"Header": {"ReportName": "ProfitAndLoss"}, "Rows": {}}))
        run(client.report("ProfitAndLoss", {"start_date": "2026-01-01", "end_date": "2026-06-30"}))
        request = self.request()
        self.assertTrue(str(request.url).startswith(BASE + "reports/ProfitAndLoss?"))
        self.assertEqual(request.url.params["start_date"], "2026-01-01")
        self.assertEqual(request.url.params["minorversion"], "75")

    def test_upload_multipart(self):
        client = self.client(json_response(200, {"AttachableResponse": [{"Attachable": {"Id": "100", "FileName": "r.pdf"}}]}))
        result = run(client.upload("r.pdf", b"%PDF", "application/pdf", {"FileName": "r.pdf"}, "rid"))
        self.assertEqual(result["Id"], "100")
        request = self.request()
        self.assertTrue(request.headers["Content-Type"].startswith("multipart/form-data"))
        body = request.content
        self.assertIn(b'name="file_metadata_01"', body)
        self.assertIn(b'{"FileName": "r.pdf"}', body)
        self.assertIn(b'name="file_content_01"; filename="r.pdf"', body)

    def test_upload_fault(self):
        fault = {"Fault": {"Error": [{"Message": "Too big"}], "type": "ValidationFault"}}
        client = self.client(json_response(200, {"AttachableResponse": [fault]}, headers={"intuit_tid": "tid-9"}))
        with self.assertRaises(QboApiError) as ctx:
            run(client.upload("r.pdf", b"x", "application/pdf", {}, "rid"))
        self.assertIn("Too big", str(ctx.exception))
        self.assertEqual(ctx.exception.intuit_tid, "tid-9")

    def test_download_follows_temporary_url(self):
        client = self.client(httpx2.Response(200, text="https://intuit-qbo-prod.s3.amazonaws.com/file?sig=abc"),
                             httpx2.Response(200, content=b"file-bytes"))
        self.assertEqual(run(client.download("100")), b"file-bytes")
        self.assertTrue(str(self.recorder.requests[0].url).startswith(BASE + "download/100?"))
        self.assertEqual(str(self.recorder.requests[1].url), "https://intuit-qbo-prod.s3.amazonaws.com/file?sig=abc")
        self.assertNotIn("Authorization", self.recorder.requests[1].headers)

    def test_download_url_not_logged(self):
        # The temporary URL carries credentials; httpx2 would log it at INFO, the level MCPServer configures.
        client = self.client(httpx2.Response(200, text="https://intuit-qbo-prod.s3.amazonaws.com/file?sig=secret"),
                             httpx2.Response(200, content=b"file-bytes"))
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            run(client.download("100"))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertFalse([r for r in records if "sig=secret" in r.getMessage()])

    def test_download_rejects_unexpected_location(self):
        client = self.client(httpx2.Response(200, text="http://example.com/file"))
        with self.assertRaises(QboError):
            run(client.download("100"))

    def test_download_failure(self):
        client = self.client(httpx2.Response(200, text="https://s3.example.com/f"), httpx2.Response(403, text="expired"))
        with self.assertRaises(QboError):
            run(client.download("100"))


class EdgeCaseTests(ClientTestBase):
    def test_single_error_object_and_array_payload(self):
        client = self.client(json_response(400, {"Fault": {"Error": {"Message": "Business Validation Error", "code": "6000"},
                                                           "type": "ValidationFault"}}),
                             json_response(200, [{"Id": "1"}, {"Id": "2"}]))
        with self.assertRaises(QboApiError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertEqual([e["code"] for e in ctx.exception.errors], ["6000"])
        with self.assertRaises(QboError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("returned list", str(ctx.exception))

    def test_non_json_success_response(self):
        client = self.client(httpx2.Response(200, text="<html/>"))
        with self.assertRaises(QboError) as ctx:
            run(client.read(get_entity("Customer"), "1"))
        self.assertIn("non-JSON", str(ctx.exception))

    def test_unparseable_retry_after(self):
        client = self.client(httpx2.Response(429, text="slow down", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                             json_response(200, {"Customer": {"Id": "1"}}))
        run(client.read(get_entity("Customer"), "1"))
        self.assertEqual(self.sleep.calls, [60.0])

    def test_signed_out_while_waiting_for_refresh(self):
        client = self.client(tokens=make_tokens(access_ttl=60.0))
        stale = self.store.load()
        self.store.clear()
        with self.assertRaises(QboAuthError):
            run(client._refresh(stale))

    def test_download_transport_error(self):
        client = self.client(httpx2.Response(200, text="https://s3.example.com/f"), httpx2.ConnectError("reset"))
        with self.assertRaises(QboError) as ctx:
            run(client.download("100"))
        self.assertIn("Cannot download attachment 100", str(ctx.exception))

    def test_owned_http_client_created_and_closed(self):
        client, _ = make_client(self.settings, Recorder())
        client._http, client._owns_http = None, True

        async def main():
            http = client._client()
            self.assertIs(client._client(), http)
            await client.aclose()
            self.assertIsNone(client._http)
            await client.aclose()

        run(main())

    def test_injected_http_client_not_closed(self):
        client = self.client()

        async def main():
            await client.aclose()

        run(main())
        self.assertIsNotNone(client._http)


if __name__ == "__main__":
    unittest.main()
