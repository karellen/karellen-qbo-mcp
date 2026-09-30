# MCP Server for QuickBooks Online (karellen-qbo-mcp)

[![Gitter](https://img.shields.io/gitter/room/karellen/lobby?logo=gitter)](https://gitter.im/karellen/Lobby)
[![Build Status](https://img.shields.io/github/actions/workflow/status/karellen/karellen-qbo-mcp/build.yml?branch=master)](https://github.com/karellen/karellen-qbo-mcp/actions/workflows/build.yml)
[![Coverage Status](https://img.shields.io/coveralls/github/karellen/karellen-qbo-mcp/master?logo=coveralls)](https://coveralls.io/r/karellen/karellen-qbo-mcp?branch=master)

[![karellen-qbo-mcp Version](https://img.shields.io/pypi/v/karellen-qbo-mcp?logo=pypi)](https://pypi.org/project/karellen-qbo-mcp/)
[![karellen-qbo-mcp Python Versions](https://img.shields.io/pypi/pyversions/karellen-qbo-mcp?logo=pypi)](https://pypi.org/project/karellen-qbo-mcp/)
[![karellen-qbo-mcp Downloads Per Day](https://img.shields.io/pypi/dd/karellen-qbo-mcp?logo=pypi)](https://pypi.org/project/karellen-qbo-mcp/)
[![karellen-qbo-mcp Downloads Per Week](https://img.shields.io/pypi/dw/karellen-qbo-mcp?logo=pypi)](https://pypi.org/project/karellen-qbo-mcp/)
[![karellen-qbo-mcp Downloads Per Month](https://img.shields.io/pypi/dm/karellen-qbo-mcp?logo=pypi)](https://pypi.org/project/karellen-qbo-mcp/)

## Overview

`karellen-qbo-mcp` is an [MCP](https://modelcontextprotocol.io/) (Model Context Protocol)
server that lets an MCP client such as Claude Code keep a company's
[QuickBooks Online](https://quickbooks.intuit.com/) books under the owner's supervision:
run financial reports, query any record, and create, update, void, delete or deactivate
records through the QuickBooks Online Accounting API.

It runs locally as a stdio server. Your Intuit app credentials and OAuth tokens stay on
your machine, and the only services it talks to are Intuit's.

## Features

- **Reports**: profit and loss, balance sheet, cash flow, trial balance, general ledger,
  A/R and A/P aging, transaction lists and any other QuickBooks report, returned either
  flattened into ordered rows or as QuickBooks' raw JSON
- **Queries**: the QuickBooks query language with automatic paging, single-record reads,
  change data capture
- **Full record management**: create and update (sparse or full) on 30+ entities;
  delete transactions; void invoices, payments, sales receipts and bill payments;
  deactivate name-list records; email invoices, estimates and purchase orders; batches
  of up to 30 operations
- **Attachments and PDFs**: upload receipts and documents linked to records, download
  attachments and invoice/estimate/sales receipt PDFs
- **Supervision**: read and write tools are separate (and annotated as such), so Claude
  Code can allow reads and ask before every write; `dry_run` previews with field-level
  diffs; a read-only mode; a local audit log of every write and an error log of every
  failure
- **Safe retries**: every write carries a QuickBooks `requestid`, so a retried request is
  de-duplicated by QuickBooks instead of being posted twice
- **Token care**: access tokens refresh automatically; the rotating refresh token is
  persisted after every refresh, and refreshes are serialized across processes
- **Both MCP protocol eras**: built on MCP Python SDK 2.x, serving `2026-07-28` clients and
  older (`initialize` handshake) clients alike

## Requirements

- **Python** >= 3.10
- An **Intuit Developer** account and an app with the `com.intuit.quickbooks.accounting`
  scope (free; see below)
- A QuickBooks Online company: a sandbox company for development, your real company for
  production

## Installation

```bash
pip install --user karellen-qbo-mcp
```

The Claude Code plugin adds the MCP server, a bookkeeping skill, and a prerequisite
check. From the Karellen marketplace:

```bash
claude plugin marketplace add karellen/claude-plugins
claude plugin install karellen-qbo-mcp@karellen-plugins
```

From a local checkout:

```bash
claude --plugin-dir /path/to/karellen-qbo-mcp
```

Or register only the MCP server:

```bash
claude mcp add --transport stdio qbo -- karellen-qbo-mcp
claude mcp add --transport stdio --env QBO_MCP_ENVIRONMENT=production qbo-production -- karellen-qbo-mcp
```

## Setting Up Intuit Access

### 1. Create an app

At [developer.intuit.com](https://developer.intuit.com/), create an app for QuickBooks
Online with the **Accounting** scope. Development (sandbox) keys are available right away,
along with a sandbox company to test against.

### 2. Sandbox

Under the app's development settings, add the redirect URI
`http://localhost:8765/callback` (Intuit accepts plain-HTTP localhost redirects for
development keys). Then:

```bash
karellen-qbo-mcp auth configure --client-id <development client ID>   # prompts for the secret
karellen-qbo-mcp auth login                                          # opens the browser
karellen-qbo-mcp auth status
```

Choose the sandbox company on Intuit's consent page. Inside Claude Code, the
`qbo_auth_login` tool runs the same browser sign-in.

### 3. Production

For production keys, Intuit asks a private (unlisted) app to fill in its app details and
an App Assessment Questionnaire; no App Store review is involved. The app details require
host, launch, disconnect, reconnect, EULA and privacy policy URLs. Intuit does not
validate these for private apps, so for an app that only you use, placeholder HTTPS URLs
on a domain you own will do.

Intuit does not allow `localhost` redirect URIs for production keys, so for production
`auth login` does not receive the redirect itself; you paste the address the browser ends
up on:

1. Add an HTTPS address on a domain you control, for example
   `https://example.com/qbo-callback`, as a production redirect URI of your app. The page
   does not need to exist: only its address matters.
2. Store the production keys with that redirect URI and sign in:

   ```bash
   karellen-qbo-mcp --environment production auth configure --client-id <production client ID> \
       --redirect-uri https://example.com/qbo-callback                  # prompts for the secret
   karellen-qbo-mcp --environment production auth login                # opens the browser
   ```

3. Sign in as an administrator of your company (any QuickBooks admin login; it needs no
   Intuit Developer account), choose the company and approve. Copy the full address from
   the browser's address bar and paste it at the prompt.

The address carries a one-time authorization code that expires within minutes and is
useless without your app's client secret; `auth login` checks that it belongs to this
sign-in and exchanges it right away. The site behind the redirect URI may log the address,
which is why it should be one you control.

From then on the server keeps the authorization alive on its own. A refresh token expires
after 100 days without use and, regardless of use, five years after the original sign-in;
`auth status` shows both dates. When it expires, run `auth login` again.

Alternatively, a refresh token from Intuit's **OAuth 2.0 Playground** (with
`https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl` registered as a production
redirect URI) can be imported. The Playground needs one Intuit login that is both a member
of the app's developer workspace and an administrator of the company.

```bash
karellen-qbo-mcp --environment production auth import --realm-id <company ID>   # prompts for the refresh token
```

The import refreshes the token immediately, which validates it and replaces it with a
fresh one the Playground no longer knows.

`auth logout` revokes the authorization at Intuit and deletes the local tokens.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `QBO_MCP_ENVIRONMENT` | `sandbox` or `production` | `sandbox` |
| `QBO_MCP_READ_ONLY` | `1`/`true`/`yes`/`on` refuses every write | off |
| `QBO_MCP_CONFIG_DIR` | Where credentials, tokens and the logs live | `~/.config/karellen-qbo-mcp` |
| `QBO_MCP_CLIENT_ID`, `QBO_MCP_CLIENT_SECRET` | App credentials, overriding `auth configure` | |
| `QBO_MCP_REDIRECT_URI` | Redirect URI for browser sign-in (`localhost` is received directly, any other is pasted back) | `http://localhost:8765/callback` (sandbox) |
| `QBO_MCP_MINOR_VERSION` | Accounting API minor version | `75` |

Each environment keeps its own `client.json`, `tokens.json`, `audit.jsonl` and `errors.jsonl` under
`<config dir>/<environment>/`, all readable only by you. The command line option
`--environment` overrides `QBO_MCP_ENVIRONMENT`.

## Tools

| Tool | Kind | Purpose |
|---|---|---|
| `qbo_auth_status` | read | Environment, read-only mode, token state |
| `qbo_auth_login` | auth | Browser sign-in (sandbox) |
| `qbo_list_entities` | read | Supported entities and their operations |
| `qbo_get` | read | One record by Id, with its SyncToken |
| `qbo_query` | read | Query language, optional automatic paging |
| `qbo_cdc` | read | Records changed since a time (30-day lookback) |
| `qbo_list_reports` | read | Common report names and parameters |
| `qbo_report` | read | Run a report, flattened or raw |
| `qbo_download_pdf` | local write | Save an invoice/estimate/sales receipt PDF to a local file |
| `qbo_download_attachment` | local write | Save an attachment's file to a local file |
| `qbo_create` | write | Create a record |
| `qbo_update` | write | Sparse or full update, with a diff preview |
| `qbo_delete` | write | Delete a transaction or attachment |
| `qbo_deactivate` | write | Deactivate a name-list record |
| `qbo_void` | write | Void an invoice, payment, sales receipt or bill payment |
| `qbo_send` | write | Email an invoice, estimate or purchase order |
| `qbo_batch` | write | Up to 30 creates/updates/deletes/queries |
| `qbo_upload_attachment` | write | Upload a file, optionally linked to records |

Every write tool except `qbo_send` and `qbo_upload_attachment` accepts `dry_run=True`.

## Supervision

- **Permission rules.** Let Claude Code run reads without asking and prompt for every
  write by allowing only the read tools. For the plugin, tool names take the form
  `mcp__plugin_karellen-qbo-mcp_karellen-qbo-mcp__<tool>`; for a server added with
  `claude mcp add ... qbo`, they are `mcp__qbo__<tool>`. For example, in
  `.claude/settings.json`:

  ```json
  {
    "permissions": {
      "allow": [
        "mcp__qbo__qbo_auth_status",
        "mcp__qbo__qbo_list_entities",
        "mcp__qbo__qbo_get",
        "mcp__qbo__qbo_query",
        "mcp__qbo__qbo_cdc",
        "mcp__qbo__qbo_list_reports",
        "mcp__qbo__qbo_report"
      ]
    }
  }
  ```

  The two download tools are marked as local writes rather than reads: they create files
  (owner-readable only, never inside the server's own configuration directory) and with
  `overwrite=True` replace them, so leave them out of the allow list to confirm each one.
- **Previews.** `dry_run=True` shows what would be sent; for updates it fetches the
  record and shows each changed field, whether your SyncToken is current, and which fields
  a full (non-sparse) update would clear.
- **Read-only mode.** `QBO_MCP_READ_ONLY=1` makes every write fail before anything is sent.
  Dry runs and query-only batches still work.
- **Audit log.** Every write, successful or failed, is appended to
  `<config dir>/<environment>/audit.jsonl` with its request, `requestid` and result
  (for batches, each operation's record or fault). A write cancelled while waiting for
  QuickBooks is logged too, since QuickBooks may have applied it.
- **Error log.** Every error a tool reports, from reads and writes alike, is appended to
  `<config dir>/<environment>/errors.jsonl` with the tool, its arguments and the error. For
  QuickBooks API errors it also records the HTTP status, fault type, error codes and the
  `intuit_tid` that Intuit support asks for.

## MCP Protocol Versions

The server answers both the `initialize` handshake (protocol `2025-11-25` and earlier)
and `server/discover` (`2026-07-28`). As of Claude Code 2.1.283, Claude Code connects to
stdio servers with the handshake unless `MCP_PROTOCOL_NEGOTIATION=auto` is set; both
work.

## Limitations

These come from the QuickBooks Online API, not from this server:

- The bank feed ("For Review") queue is not available through any public API, and
  reconciliations cannot be performed through the API.
- Reads, queries, reports, change data capture and batches count toward the app's
  monthly quota (500,000 calls on Intuit's free Builder tier); writes do not.
- Requests are limited to 500 per minute and 10 per second per company; the server paces
  itself and waits out throttling.
- Intuit moved reports to a new reporting service in 2026 and dropped some undocumented
  reports; report names outside the documented set may be rejected.

## Development

This is a [PyBuilder](https://pybuilder.io/) project:

```bash
pyb -vX                      # lint, unit tests, integration tests, coverage, package
pyb -vX run_unit_tests       # unit tests only
```

The integration tests start the server over stdio and exercise both protocol eras. A
second suite runs end to end against a sandbox company; it creates and removes records
there, and runs only when asked:

```bash
pyb -vX run_integration_tests -P qbo_sandbox_tests=true
```
