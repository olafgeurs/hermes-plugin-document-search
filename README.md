# hermes-plugin-document-search

Structured search over the per-tenant document corpus produced by
[privateassistant.ai](https://privateassistant.ai)'s document-processor.

The processor writes three files per matched incoming message into
`shared/<kind>/<YYYY-MM>/<DD>/`:

- the original file (`.pdf`, `.png`, `.eml`, …)
- a `<name>.metadata.json` sidecar with routing + email provenance
- a `<name>.result.json` with extracted fields (for extractor-backed kinds)

This plugin exposes three read-only tools that iterate over those sidecars:

| Tool | Description |
|------|-------------|
| `search_documents` | Filter the corpus by kind/date/sender/vendor/amount/query. Sorted by date desc. |
| `read_document` | Full metadata + extraction result for one document. |
| `open_source_email` | Raw `.eml` source (≤8 KB preview) for the email that produced the document. |

No network egress. Reads `/opt/data/workspace/shared/` directly — no DB, no index.

## Install

Via the account portal (`app.privateassistant.ai/account` → Plugins → Catalog →
**document-search**), or:

```sh
git clone https://github.com/olafgeurs/hermes-plugin-document-search \
  /opt/data/plugins/document-search
```

Restart the agent (portal "Wijzigingen toepassen" button, or
`docker restart mpa-agent-<id>`) so `discover_plugins()` picks it up.

## Example

```
search_documents(kind="invoice", vendor="digitalocean", from_date="2026-01-01")
→ {
    "scanned": 42,
    "returned": 3,
    "results": [{"path": "shared/invoices/2026-03/01/digitalocean-...pdf",
                 "date": "2026-03-01", "amount": 127.78, "currency": "USD", ...}]
  }
```
