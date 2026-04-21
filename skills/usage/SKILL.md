---
name: usage
description: How to use the document-search plugin — the six tools cover both the processed document corpus (invoices/contracts) and the raw IMAP mailbox (list/read/draft replies).
version: 1.1.0
---

# document-search — usage

Six tools split across two surfaces:

| Surface | Tools | When |
|---------|-------|------|
| Processed docs (on-disk, `shared/`) | `search_documents`, `read_document`, `open_source_email` | Invoices, contracts, anything the document-processor classified + extracted. |
| Raw mailbox (IMAP) | `list_inbox`, `read_inbox_message`, `create_draft_reply` | "Show me my emails", unread count, threading, composing replies. |

**Never** guess filesystem paths for mail (`/var/mail/...`, `/opt/data/workspace/mail/...`) — there is no local Maildir in this container. Mail is reached over IMAP against `dovecot:993`; the plugin handles auth.

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

## Inbox (IMAP) examples

### "Laat alle emails van olaf.geurs zien"

```
list_inbox(from_sender="olaf.geurs", limit=50)
```

Returns IMAP UIDs + headers. `from_sender` is a server-side substring match
on the From header — the agent doesn't need to SELECT + iterate.

### "Wat is er ongelezen?"

```
list_inbox(unread_only=True)
```

Returns only messages with no `\Seen` flag.

### "Open dat laatste bericht"

```
read_inbox_message(uid=70)   # uid comes from list_inbox
```

Returns headers + plain-text body (≤8 KB). HTML-only messages fall back to
the HTML part.

### "Reageer op deze mail met 'Dank, ik bekijk het morgen'"

```
create_draft_reply(
  uid=70,
  body="Dank, ik bekijk het morgen.",
)
```

Saves a draft to the Drafts folder via IMAP APPEND. Threading headers
(In-Reply-To, References) and `Re: ` prefix are added automatically. The
draft is **not sent** — the user reviews and sends from their mail client.
Returns the new draft's UID.

Extras: `include_quoted=False` to skip the quoted original; `extra_to` /
`extra_cc` to add recipients.

### Choosing between `search_documents` and `list_inbox`

- Question mentions "factuur / invoice / contract / bedrag / vendor" → usually `search_documents` (processed data with extracted amounts).
- Question mentions "email / mail / unread / reply / sender / subject" → usually `list_inbox`.
- "Find the email that this invoice came from" → `search_documents` first to get the doc, then `open_source_email` (archived `.eml`) OR `list_inbox(subject=..., from_sender=...)` (live mailbox) depending on whether the user wants the archive or the live folder state.

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
- **`create_draft_reply` never sends.** It only APPENDs to Drafts. If the
  user says "stuur deze email", create the draft and tell them to open
  Drafts in their mail client — sending requires human review on this
  platform. Do not try to wire up SMTP yourself.
- **Folders are fixed.** `INBOX / Drafts / Sent / Trash / Junk`. Custom
  folders aren't exposed; suggest the user create them via their mail
  client if they need more.
