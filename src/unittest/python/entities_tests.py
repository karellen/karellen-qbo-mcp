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

from karellen_qbo_mcp.entities import (EntityError, get_entity, require_capability, describe_entities, VOID_OPERATION,
                                       VOID_INCLUDE)


class EntityRegistryTests(unittest.TestCase):
    def test_lookup_is_case_insensitive(self):
        self.assertEqual(get_entity(" journalentry ").name, "JournalEntry")
        self.assertEqual(get_entity("JOURNALENTRY").path, "journalentry")

    def test_unknown_entity(self):
        with self.assertRaises(EntityError) as ctx:
            get_entity("Widget")
        self.assertIn("JournalEntry", str(ctx.exception))
        with self.assertRaises(EntityError):
            get_entity(None)

    def test_name_list_capabilities(self):
        self.assertEqual(get_entity("Customer").capabilities(), ["read", "query", "create", "update", "deactivate"])

    def test_transaction_capabilities(self):
        self.assertEqual(get_entity("Invoice").capabilities(),
                         ["read", "query", "create", "update", "delete", "void", "send", "pdf"])
        self.assertEqual(get_entity("Bill").capabilities(), ["read", "query", "create", "update", "delete"])

    def test_singleton_and_reference_capabilities(self):
        self.assertEqual(get_entity("CompanyInfo").capabilities(), ["read", "query", "update"])
        self.assertEqual(get_entity("TaxCode").capabilities(), ["read", "query"])

    def test_void_styles(self):
        self.assertEqual(get_entity("Invoice").void_style, VOID_OPERATION)
        for name in ("Payment", "SalesReceipt", "BillPayment"):
            self.assertEqual(get_entity(name).void_style, VOID_INCLUDE)

    def test_require_capability(self):
        spec = get_entity("Vendor")
        self.assertIs(require_capability(spec, "deactivate"), spec)
        with self.assertRaises(EntityError) as ctx:
            require_capability(spec, "delete")
        self.assertIn("deactivate it instead", str(ctx.exception))
        with self.assertRaises(EntityError) as ctx:
            require_capability(get_entity("Bill"), "void")
        self.assertNotIn("deactivate", str(ctx.exception))

    def test_describe_entities(self):
        described = {d["entity"]: d for d in describe_entities()}
        self.assertGreater(len(described), 30)
        self.assertEqual(described["Payment"]["kind"], "transaction")
        self.assertIn("void", described["Payment"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
