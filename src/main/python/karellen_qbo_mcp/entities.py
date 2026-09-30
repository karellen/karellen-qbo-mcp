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

"""QuickBooks Online Accounting API entity registry and per-entity capabilities.

Name-list entities (accounts, customers, vendors, items, ...) cannot be deleted in
QuickBooks; they are deactivated by setting Active=false. Transactions can be deleted,
and a few can be voided, which keeps the record but zeroes its amounts.
"""

from dataclasses import dataclass

# How a void request is shaped differs by entity.
VOID_OPERATION = "operation"   # POST /<entity>?operation=void, body {Id, SyncToken}
VOID_INCLUDE = "include"       # POST /<entity>?operation=update&include=void, body {Id, SyncToken, sparse: true}


class EntityError(Exception):
    pass


@dataclass(frozen=True)
class EntitySpec:
    name: str
    kind: str                   # "list", "transaction", "singleton" or "reference" (read-only)
    deletable: bool = False
    deactivatable: bool = False
    void_style: str | None = None
    sendable: bool = False
    pdf: bool = False

    @property
    def path(self) -> str:
        return self.name.lower()

    def capabilities(self) -> list[str]:
        caps = ["read", "query"]
        if self.kind != "reference":
            caps.extend(["update"] if self.kind == "singleton" else ["create", "update"])
        for flag, cap in ((self.deletable, "delete"), (self.deactivatable, "deactivate"),
                          (self.void_style is not None, "void"), (self.sendable, "send"), (self.pdf, "pdf")):
            if flag:
                caps.append(cap)
        return caps


def _list(name, **kwargs):
    return EntitySpec(name, "list", deactivatable=True, **kwargs)


def _txn(name, **kwargs):
    return EntitySpec(name, "transaction", deletable=True, **kwargs)


_ENTITIES = [
    _list("Account"),
    _list("Class"),
    _list("CompanyCurrency"),
    _list("Customer"),
    _list("Department"),
    _list("Employee"),
    _list("Item"),
    _list("PaymentMethod"),
    _list("Term"),
    _list("Vendor"),
    EntitySpec("TaxAgency", "list"),
    EntitySpec("Budget", "reference"),
    EntitySpec("CustomerType", "reference"),
    EntitySpec("TaxCode", "reference"),
    EntitySpec("TaxRate", "reference"),
    EntitySpec("CompanyInfo", "singleton"),
    EntitySpec("Preferences", "singleton"),
    _txn("Attachable"),
    _txn("Bill"),
    _txn("BillPayment", void_style=VOID_INCLUDE),
    _txn("CreditCardPayment"),
    _txn("CreditMemo"),
    _txn("Deposit"),
    _txn("Estimate", sendable=True, pdf=True),
    _txn("Invoice", void_style=VOID_OPERATION, sendable=True, pdf=True),
    _txn("JournalEntry"),
    _txn("Payment", void_style=VOID_INCLUDE),
    _txn("Purchase"),
    _txn("PurchaseOrder", sendable=True),
    _txn("RefundReceipt"),
    _txn("SalesReceipt", void_style=VOID_INCLUDE, pdf=True),
    _txn("TimeActivity"),
    _txn("Transfer"),
    _txn("VendorCredit"),
]

ENTITIES = {spec.name.lower(): spec for spec in _ENTITIES}


def get_entity(name: str) -> EntitySpec:
    spec = ENTITIES.get((name or "").strip().lower())
    if spec is None:
        known = ", ".join(sorted(s.name for s in _ENTITIES))
        raise EntityError("Unknown QuickBooks entity %r. Known entities: %s" % (name, known))
    return spec


def require_capability(spec: EntitySpec, capability: str) -> EntitySpec:
    if capability not in spec.capabilities():
        hint = ""
        if capability == "delete" and spec.deactivatable:
            hint = " %s is a name-list entity; deactivate it instead." % spec.name
        raise EntityError("%s does not support %s (supports: %s).%s" % (
            spec.name, capability, ", ".join(spec.capabilities()), hint))
    return spec


def describe_entities() -> list[dict]:
    return [{"entity": s.name, "kind": s.kind, "capabilities": s.capabilities()} for s in _ENTITIES]
