---
name: usage
description: How to use the document-search plugin — when to call each tool, what the corpus looks like, worked examples for common queries (spending by vendor, date ranges, finding the source email).
version: 1.0.0
---

# document-search — usage

Three read-only tools that query the tenant's processed document corpus.
**Use these instead of grepping `shared/` directly** — they understand the
metadata + extraction sidecars and apply structured filters.

## The corpus, one paragraph

The document-processor writes every matched incoming email into
`shared/<kind>/<YYYY-MM>/<DD>/` as three files: the original attachment
(`foo.pdf`), a `foo.pdf.metadata.json` sidecar (routing + email provenance),
and — for extractor-backed kinds — a `foo.pdf.result.json` with extracted
fields. The raw `.eml` lives once at `shared/emails/<YYYY-MM>/<DD>/` and is
referenced by every sidecar that came from that message via
`metadata.email_link`.

Kinds: `invoice`, `contract`, `document`, `ticket`. Invoice results have
`company_name`, `total_amount`, `currency`, `invoice_date`, `invoice_number`,
`vat_*`, `iban`, `category`. Generic `document` kind uses the OCR-only
extractor and has a `text` field instead.

## When to use which tool

| You want to… | Tool |
|--------------|------|
| Find documents matching filters (most common) | `search_documents` |
| Get all extracted fields for one document | `read_document` |
| See the email thread/body that produced a document | `open_source_email` |

## Worked examples

### "Wat heb ik bij DigitalOcean uitgegeven in Q1?"

```
search_documents(
  kind="invoice",
  vendor="digitalocean",
  from_date="2026-01-01",
  to_date="2026-03-31",
)
```

Then sum `amount` over `results`. Currency is on each hit — be careful mixing
USD and EUR.

### "Toon de laatste 5 facturen boven €200."

```
search_documents(kind="invoice", min_amount=200, limit=5)
```

Results are already sorted by date descending.

### "Welke factuur hoorde bij die mail van MVGM vorige week?"

```
search_documents(kind="invoice", sender="mvgm", from_date="2026-04-14")
```

Each hit's `email_link` points at the shared `.eml`. Pass any hit's `path`
to `open_source_email` for the raw message.

### "Ik zoek een contract met Acme."

```
search_documents(kind="contract", query="acme")
```

`query` searches subject, sender, analysis_text, extracted company_name,
description, invoice_number, category, address.

### Inspecting one document

```
read_document(path="shared/invoices/2026-03/01/digitalocean-...pdf")
```

Returns `{doc: {path, exists, size_bytes}, metadata: {...}, result: {...}}`.
The `path` can be the doc file or its `.metadata.json` sidecar; relative
paths are resolved under `shared/`.

### Going from a document to its source email

```
open_source_email(path="shared/invoices/2026-03/01/digitalocean-...pdf")
```

Returns up to ~8 KB of the raw `.eml` plus the archive path. Useful when the
user asks "waarom is dit als factuur geclassificeerd" or needs the full
message body / headers / From line.

## Pitfalls

- **Amount filters skip un-extracted docs.** `min_amount` / `max_amount`
  require a numeric `total_amount` or `due_amount` — documents still in
  `new/` without a `.result.json` are excluded. Run without amount filters
  to see them.
- **Currency is per-row.** Aggregating `amount` across hits mixes currencies.
  Filter by currency in post-processing or group by `currency`.
- **`limit` caps at 100.** For larger answers, narrow the query (date range
  + kind) rather than raising the limit.
- **Manual drops have no `email_link`.** `open_source_email` returns an
  error for these — metadata has `source: "manual"`.
- **`id` kind is intentionally not searchable here.** ID documents live
  under `private/` which is not exposed to Nextcloud or this plugin.
