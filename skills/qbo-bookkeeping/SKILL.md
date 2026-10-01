---
description: Keep QuickBooks Online books with the user - look up records, run reports, and post, correct or remove transactions under the user's supervision through the karellen-qbo-mcp tools.
---

# Supervised QuickBooks Bookkeeping

Use this skill when the user asks to review, record, correct or report on anything in
their QuickBooks Online company: invoices, bills, payments, expenses, deposits,
transfers, journal entries, customers, vendors, accounts, items, or financial reports.

These are the user's real books. Every change is visible to their accountant and tax
filings. Work like a careful bookkeeper who asks before posting.

## 1. Orient

- Call `qbo_auth_status`. Note `environment` (sandbox or production) and `read_only`,
  and tell the user which company environment you are working in before any change.
- If not signed in: sandbox uses `qbo_auth_login`; production needs the user to run
  `karellen-qbo-mcp --environment production auth login` in a terminal (it asks them to
  paste the address the browser was redirected to).
- `qbo_get` with `CompanyInfo` confirms which company you are connected to.

## 2. Look up before you write

- Never guess Ids. Resolve every reference (customer, vendor, account, item, class,
  department, tax code) with `qbo_query`, e.g.
  `SELECT Id, DisplayName FROM Vendor WHERE DisplayName LIKE '%Acme%'`.
- Query language limits: one entity per query, no joins, no `OR`, only some fields
  are filterable, at most 1000 rows per call (`fetch_all=True` pages for you).
- Never pull large results inline. For `fetch_all`, wide entities (`Attachable` rows
  carry long temporary download URLs) and detail reports (`TransactionList`,
  `GeneralLedger`), pass `output_path` to `qbo_query`/`qbo_report` and read the file
  with `jq`. Select only the fields you need, and run `SELECT COUNT(*)` first when
  unsure of the size.
- Check for duplicates before creating: search by DocNumber, amount and date, or by
  DisplayName for name-list records.
- For accounts, confirm the AccountType/AccountSubType fits the posting (an expense to
  an expense account, a loan to a liability, and so on). When unsure which account the
  user uses for something, ask; do not pick one.

## 3. Propose, then post

- Before any write, summarize for the user in plain terms: what record, which
  accounts, amounts, dates, customer/vendor, memo. For multi-line transactions show
  the lines. Journal entries must balance (total debits = total credits).
- Use `dry_run=True` for updates: the preview shows each field's current and proposed
  value, whether the SyncToken is current, and for full updates which fields would be
  cleared. Prefer sparse updates (the default).
- Line arrays are replaced whole on update: send every line you want to keep.
- Post only after the user agrees. The Claude Code permission prompt for the write
  tool is the final check; do not work around a denial.
- Deleting is permanent. Prefer voiding (invoices, payments, sales receipts, bill
  payments) when the user wants an audit trail. Name-list records are deactivated
  (`qbo_deactivate`), never deleted.
- `qbo_batch` runs up to 30 independent operations; each can fail on its own, so read
  every result's `fault`.

## 4. Verify

- Re-read what you wrote (`qbo_get`) and confirm the totals.
- Check the effect with a report: `qbo_report` with `ProfitAndLoss`, `BalanceSheet`,
  `TrialBalance`, `GeneralLedger`, `AgedReceivables` or `AgedPayables` for the
  affected period. Keep detail report ranges to six months or less.
- Tell the user what changed, with Ids and DocNumbers, so they can find it in
  QuickBooks. Every write is also recorded in the local audit log.

## Limits to tell the user about

- The QuickBooks API does not expose the bank feed ("For Review") queue and cannot
  perform a reconciliation. Bank-feed matching and reconciling stay in the QuickBooks
  UI; you can help by comparing a statement against posted transactions.
- Change data capture (`qbo_cdc`) looks back at most 30 days.
- Reads, queries and reports count toward Intuit's monthly quota for the app; avoid
  needless repeated large queries.
