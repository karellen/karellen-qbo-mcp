# Privacy Policy

**karellen-qbo-mcp** — MCP Server for QuickBooks Online

*Last updated: 2026-09-28*

## Summary

karellen-qbo-mcp runs entirely on your local machine. It talks to Intuit's QuickBooks
Online and OAuth services on your behalf and to nothing else. Karellen, Inc. receives
no data from it.

## Data Collection

This software does **not**:

- Collect or transmit any personal information to Karellen, Inc. or any third party
  other than Intuit
- Send telemetry, analytics, or usage data

## Network Connections

The server connects only to:

- Intuit's OAuth 2.0 discovery document (`developer.api.intuit.com`) and the endpoints it
  names (currently `appcenter.intuit.com`, `oauth.platform.intuit.com` and
  `developer.api.intuit.com`) to authorize access and refresh or revoke tokens
- The QuickBooks Online Accounting API (`quickbooks.api.intuit.com`, or
  `sandbox-quickbooks.api.intuit.com` for sandbox companies)
- The temporary download location QuickBooks issues when an attachment is downloaded
- Your own machine (`localhost`), for the one-time browser sign-in callback

## Data Stored Locally

In your user configuration directory (by default `~/.config/karellen-qbo-mcp/`, one
subdirectory per environment), readable only by your user account:

- `client.json` — your Intuit app's client ID and secret, if you store them there
- `tokens.json` — the OAuth access and refresh tokens and your company (realm) ID
- `audit.jsonl` — a log of every change the server sends to QuickBooks, including the
  submitted data and the identifying fields of the result

Files you ask it to download (PDFs, attachments) are written to the paths you choose.
Nothing else is stored.

## Data Processing

The MCP server relays requests from your MCP client (for example Claude Code) to
QuickBooks and returns the results to that client. What the client does with that data
is governed by the client's own privacy policy; for Claude Code, data returned by tools
becomes part of the conversation sent to Anthropic's API.

## Third-Party Services

Your use of QuickBooks Online is governed by Intuit's terms and privacy statement.

## Changes to This Policy

If this policy changes, the updated version will be published in the project
repository at
[https://github.com/karellen/karellen-qbo-mcp](https://github.com/karellen/karellen-qbo-mcp).

## Contact

If you have questions about this privacy policy, please open an issue at
[https://github.com/karellen/karellen-qbo-mcp/issues](https://github.com/karellen/karellen-qbo-mcp/issues)
or contact [supervisor@karellen.co](mailto:supervisor@karellen.co).
