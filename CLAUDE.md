# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`karellen-qbo-mcp` is a local stdio MCP server that lets an LLM client keep a company's QuickBooks Online books under the owner's supervision: reports, queries, and full record management (create/update/delete/void/deactivate/send, batch, attachments) through the QuickBooks Online Accounting API v3.

## Build & Test

This is a PyBuilder project. Always run `pyb` with `-vX`.

```bash
pyb -vX                      # default: analyze + publish (lint, unit + integration tests, coverage, package)
pyb -vX run_unit_tests       # unit tests only
pyb -vX run_unit_tests -P unittest_module_glob=client_tests
pyb -vX run_integration_tests -P qbo_sandbox_tests=true   # also run the live sandbox suite
```

Unit tests live in `src/unittest/python/*_tests.py` (shared fixtures in `qbo_test_support.py`: fake OAuth, recording `httpx2.MockTransport` handler, fixed clock). Integration tests in `src/integrationtest/python/`: `stdio_protocol_integration_tests.py` spawns the server over stdio and checks both protocol eras (legacy `initialize` → `2025-11-25`, `server/discover` → `2026-07-28`); `sandbox_integration_tests.py` runs against the signed-in sandbox company and is skipped unless the build is given `-P qbo_sandbox_tests=true` (`build.py` hands it to the test subprocess as `QBO_MCP_SANDBOX_TESTS=1`, since the integrationtest plugin only passes environment and command line; don't set that variable by hand). Its tests are `IsolatedAsyncioTestCase`s with a fresh `Runtime` per test: httpx2 clients are bound to the event loop that opened their connections.

**PyBuilder and user site-packages:** PyBuilder builds the test subprocess `sys.path` from pyb's own `sys.path`, remapping only paths under `sys.exec_prefix` to the test venv. When `pyb` is installed with `pip --user`, `~/.local/lib/pythonX.Y/site-packages` stays on the test path ahead of the venv, and whatever is installed there shadows the venv's copies (this is how an `mcp` 1.x once broke the tests with `No module named 'mcp.server.mcpserver'`). Run `pyb` from a venv (e.g. `python3 -m venv .claude-tmp/pyb-venv && .claude-tmp/pyb-venv/bin/pip install pybuilder`), where user site-packages are disabled.

## Lint

Flake8 with: `max_line_length=130`, `extend_ignore=E303,E402`. Applies to source, tests, and scripts. Flake8 violations break the build.

## Architecture

Built on MCP Python SDK **2.x** (`mcp.server.mcpserver.MCPServer`, `ToolError` from `mcp.server.mcpserver.exceptions`) and `httpx2` (the SDK's HTTP client). All tools are `async`.

Module responsibilities (`src/main/python/karellen_qbo_mcp/`):

- **`server.py`** -- `MCPServer` definition and all `@mcp.tool()` endpoints. `Runtime` lazily holds settings, token store, audit and error logs, OAuth client and `QboClient` (module singleton `_runtime`, replaceable in tests; `aclose()` closes the HTTP client). `_tag_errors` converts exceptions to `ToolError` with prefixes (`auth:`, `qbo:`, `invalid:`, `config:`, `write:`, `internal:`) and records every tool error in the error log (tool, bound arguments, message; status, fault, error codes and `intuit_tid` for `QboApiError`), except when settings could not be loaded (no runtime, no state directory). `_perform` is the single path for every write: it enforces read-only mode, runs the write with a fresh `requestid` and records it in the audit log (success, failure, or cancellation — QuickBooks may have applied a cancelled write; batch entries keep their faults). `_record_write` shares the delete/deactivate/void tool logic. Downloads are written atomically and owner-only, never into the server's state directory, and without `overwrite` never replace a file (not even one created meanwhile). Tool annotations mark read tools `read_only_hint`, destructive writes `destructive_hint`, and the download tools `LOCAL_WRITE` (they write local files). `qbo_auth_login` opens the browser through `login.open_browser_detached` and saves under the token lock. Declared return types matter: `MCPServer` validates results against them (tests call tools through `mcp.call_tool` where the shape is non-obvious, e.g. `qbo_cdc` returns a list).
- **`cli.py`** -- argparse CLI and the console-script entry point (`main`): no command/`serve` runs the stdio server; `auth configure|login|import|status|logout` manage credentials. `--environment` sets `QBO_MCP_ENVIRONMENT` for the process. `configure` never parses the existing `client.json` first (so it can repair a corrupt one) and keeps a stored redirect URI unless given a new one; `login`/`import` save and `logout` revokes+clears under the token lock, so a server refreshing concurrently cannot overwrite or resurrect tokens.
- **`client.py`** -- Raises the `httpx2` logger to WARNING on import: `MCPServer` configures INFO logging to stderr, and httpx2 logs request URLs (attachment download URLs carry temporary credentials). `api_error` builds every `QboApiError` and adds a re-read hint to stale-object faults (code 5010). `QboClient`: request loop with minorversion, pacing (10 req/s), 401 → single refresh + retry, 429 → wait `Retry-After`/60 s, 5xx/transport errors → exponential backoff. All requests are retry-safe (GETs, query POSTs, writes carrying `requestid`). Fault parsing (`Fault`/`fault`, also inside HTTP 200 bodies) into `QboApiError`. Token refresh is serialized by an in-process `anyio.Lock` plus a cross-process file lock, re-reading the store under the lock. `realm_id` remembers the realm of the last tokens used (for the audit log); paths may contain `{realm}` (CompanyInfo). Entity operations: read (CompanyInfo/Preferences singleton paths), query/query_all (STARTPOSITION/MAXRESULTS paging; COUNT queries run once; a limit reached on a full page is checked with a one-row probe before reporting `truncated`), create, update (sparse flag), delete, deactivate, void (two request shapes), send, pdf, batch (≤30), cdc, report, upload (multipart `file_metadata_01`/`file_content_01`), download (temporary URL, fetched without auth).
- **`entities.py`** -- `EntitySpec` registry with per-entity capabilities; `require_capability` guards operations. Void shapes: Invoice `?operation=void` {Id, SyncToken}; Payment/SalesReceipt/BillPayment `?operation=update&include=void` {Id, SyncToken, sparse}.
- **`oauth.py`** -- Intuit OAuth 2.0: the authorization, token and revocation endpoints come from Intuit's per-environment OpenID discovery document (`Settings.discovery_url`), fetched once per `OAuthClient` without credentials; only HTTPS endpoints are accepted, and a failed fetch is retried on the next call. Authorization URL, code exchange, refresh (sends `x-include-refresh-token-hard-expires-in: true`), revoke. `OAuthError.is_invalid_grant` marks a dead refresh token.
- **`login.py`** -- Browser sign-in via a one-shot asyncio localhost listener with `state` verification. Only plain-HTTP localhost redirect URIs (Intuit allows them for sandbox keys only); production uses `auth import` of an OAuth Playground refresh token. `open_browser_detached` launches the browser from a separate process with no shared stdio (the MCP server's stdout is the protocol channel).
- **`tokens.py`** -- `Tokens` dataclass (absolute expiry timestamps incl. 5-year hard expiry) and `TokenStore` (atomic 0600 JSON file, `AsyncFileLock` on the sibling `tokens.lock`, hence `filelock>=3.15`; `save_locked` for writers outside a refresh). `auth_status()` builds the status report shared by the `qbo_auth_status` tool and `auth status` (the CLI does not import the server for it).
- **`config.py`** -- `Settings` from `QBO_MCP_*` environment variables and `<config dir>/<environment>/client.json`; per-environment state directory.
- **`reports.py`** -- Known report names (guidance only; any `^[A-Za-z][A-Za-z0-9]*$` name passes) and `flatten_report` for the Header/Rows/Summary tree.
- **`audit.py`** -- Append-only JSONL logs: `AuditLog` of writes (`audit.jsonl`) and `ErrorLog` of every tool error (`errors.jsonl`). A failure to write either is logged, never raised.
- **`files.py`** -- Owner-only atomic writes (unique `mkstemp` temp files; `overwrite=False` links into place so an existing file is never replaced, falling back to an exclusive create where hard links are unsupported) and appends.

## QuickBooks API Facts

- Minor versions 1–74 were retired 2025-08-01; 75 is the default/minimum.
- Linking an attachment to a transaction bumps that transaction's SyncToken (verified in the sandbox).
- Refresh tokens rotate (~24 h) and the previous value dies: always persist the newest. 100-day rolling expiry plus a 5-year hard cap.
- Production redirect URIs must be HTTPS and cannot be localhost or IP addresses.
- No public API for the bank feed "For Review" queue or for performing reconciliations.
- Reads/queries/reports/CDC/batch are metered (500k/month on the free Builder tier); writes are not.

## Claude Code Plugin Artifacts

The repository doubles as a Claude Code plugin (loaded via `--plugin-dir` or marketplace):

- **`.claude-plugin/plugin.json`** -- Plugin manifest
- **`.mcp.json`** -- Registers the `karellen-qbo-mcp` stdio server
- **`hooks/hooks.json`** + **`scripts/check-prerequisites.sh`** -- SessionStart check that `karellen-qbo-mcp` is on PATH
- **`skills/qbo-bookkeeping/SKILL.md`** -- Supervised bookkeeping workflow (orient, look up, propose, post, verify)

When changing tool semantics, MCP server behavior, or environment variable handling, update the relevant plugin artifacts, README and this file in the same commit.

## CI

GitHub Actions (`.github/workflows/build.yml`): matrix build on ubuntu-latest across Python 3.10-3.14. Deploy (PyPI upload) from Python 3.14 on push to master. Uses `pybuilder/build@master` action. Commit messages containing `[release]` or `[release <version>]` trigger a release. Publishing a GitHub release runs `.github/workflows/notify-marketplace.yml`, which asks `karellen/claude-plugins` to update the plugin's marketplace version (needs the `PAT_TOKEN` secret; uploads need `PYPI_TOKEN`).
