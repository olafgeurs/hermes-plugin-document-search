"""Hermes plugin — structured search over the per-tenant document corpus.

The document-processor writes three artifacts per incoming message that matches
a kind: the original file (pdf/png/...), a ``<name>.metadata.json`` sidecar with
routing + email-provenance fields, and (for extractor-backed kinds) a
``<name>.result.json`` with the extracted fields.

This plugin exposes three read-only tools:

* ``search_documents`` — filter the corpus by kind/date/sender/vendor/amount/query
* ``read_document``    — return full metadata + extraction result for one doc
* ``open_source_email`` — return the raw .eml that produced a document

Paths in tool output are agent-root-relative (e.g. ``shared/invoices/...``) so
they're copy-pastable into other file tools and into chat.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import imaplib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

# Inside the Hermes container the tenant's data lives under /opt/data/workspace.
# `shared/` is the user-visible root; private/ is excluded on purpose.
_AGENT_ROOT = Path("/opt/data/workspace")
_SHARED_ROOT = _AGENT_ROOT / "shared"

_KIND_DIRS = {
    "invoice": "invoices",
    "contract": "contracts",
    "document": "documents",
    "ticket": "tickets",
}


def _iter_metadata(kind_dir: str | None):
    dirs = [kind_dir] if kind_dir else list(_KIND_DIRS.values())
    for d in dirs:
        root = _SHARED_ROOT / d
        if not root.exists():
            continue
        for p in root.rglob("*.metadata.json"):
            yield p


def _load_record(metadata_path: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    base = metadata_path.name[: -len(".metadata.json")]
    result_path = metadata_path.parent / f"{base}.result.json"
    result: dict[str, Any] = {}
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            result = {}
    return {"meta": meta, "result": result, "metadata_path": metadata_path, "base": base}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _record_date(rec: dict) -> date | None:
    for key in ("invoice_date", "received_at", "original_sent", "saved_at"):
        v = rec["result"].get(key) or rec["meta"].get(key)
        d = _parse_date(v)
        if d:
            return d
    return None


def _record_amount(rec: dict) -> float | None:
    for key in ("total_amount", "due_amount", "amount"):
        v = rec["result"].get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _record_vendor(rec: dict) -> str:
    return str(rec["result"].get("company_name") or rec["meta"].get("from_name") or "").strip()


def _matches_query(rec: dict, q: str) -> bool:
    for field in ("subject", "from_email", "from_name", "analysis_text", "forward_comment"):
        v = rec["meta"].get(field)
        if v and q in str(v).lower():
            return True
    for field in ("company_name", "description", "invoice_number", "category", "address", "iban"):
        v = rec["result"].get(field)
        if v and q in str(v).lower():
            return True
    return False


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(_AGENT_ROOT))
    except ValueError:
        return str(p)


def _summarize(rec: dict) -> dict:
    m, r = rec["meta"], rec["result"]
    d = _record_date(rec)
    doc_path = rec["metadata_path"].parent / rec["base"]
    return {
        "path": _rel(doc_path),
        "kind": m.get("kind") or "",
        "date": d.isoformat() if d else "",
        "subject": m.get("subject") or "",
        "from": m.get("from_name") or m.get("from_email") or "",
        "vendor": _record_vendor(rec),
        "amount": _record_amount(rec),
        "currency": r.get("currency") or "",
        "invoice_number": r.get("invoice_number") or "",
        "email_link": m.get("email_link") or "",
    }


def _safe_path(user_path: str) -> Path:
    up = (user_path or "").strip()
    if not up:
        raise ValueError("path is required")
    p = Path(up)
    if p.is_absolute():
        candidate = p
    elif up.startswith("shared/") or up.startswith("./shared/"):
        candidate = _AGENT_ROOT / up
    else:
        candidate = _SHARED_ROOT / up
    candidate = candidate.resolve()
    try:
        candidate.relative_to(_SHARED_ROOT)
    except ValueError as e:
        raise ValueError(f"path must be under {_SHARED_ROOT}") from e
    return candidate


# --- Tool handlers -----------------------------------------------------------

def _search_documents(params: dict) -> str:
    kind = params.get("kind")
    if kind and kind not in _KIND_DIRS:
        return json.dumps({"error": f"unknown kind '{kind}'. One of: {list(_KIND_DIRS)}"})
    kind_dir = _KIND_DIRS.get(kind) if kind else None

    q = str(params.get("query") or "").strip().lower()
    from_date = _parse_date(params.get("from_date"))
    to_date = _parse_date(params.get("to_date"))
    sender = str(params.get("sender") or "").strip().lower()
    vendor_q = str(params.get("vendor") or "").strip().lower()
    min_amt = params.get("min_amount")
    max_amt = params.get("max_amount")
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    hits: list[dict] = []
    scanned = 0
    for mpath in _iter_metadata(kind_dir):
        scanned += 1
        rec = _load_record(mpath)
        if not rec:
            continue

        d = _record_date(rec)
        if from_date and (not d or d < from_date):
            continue
        if to_date and (not d or d > to_date):
            continue
        if sender:
            hay = " ".join(str(rec["meta"].get(f) or "") for f in ("from_email", "from_name")).lower()
            if sender not in hay:
                continue
        if vendor_q and vendor_q not in _record_vendor(rec).lower():
            continue
        amt = _record_amount(rec)
        if min_amt is not None and (amt is None or amt < float(min_amt)):
            continue
        if max_amt is not None and (amt is None or amt > float(max_amt)):
            continue
        if q and not _matches_query(rec, q):
            continue

        hits.append(_summarize(rec))

    hits.sort(key=lambda r: r.get("date") or "", reverse=True)
    truncated = len(hits) > limit
    hits = hits[:limit]
    return json.dumps(
        {"scanned": scanned, "returned": len(hits), "truncated": truncated, "results": hits},
        indent=2,
        default=str,
    )


def _read_document(params: dict) -> str:
    try:
        p = _safe_path(params.get("path", ""))
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if p.name.endswith(".metadata.json"):
        meta_path = p
        base = p.name[: -len(".metadata.json")]
    else:
        meta_path = p.parent / f"{p.name}.metadata.json"
        base = p.name

    if not meta_path.exists():
        return json.dumps({"error": f"no metadata sidecar at {_rel(meta_path)}"})

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return json.dumps({"error": f"unreadable metadata: {e}"})

    result: dict[str, Any] = {}
    result_path = meta_path.parent / f"{base}.result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            result = {"_error": "unreadable result.json"}

    doc_path = meta_path.parent / base
    doc_info: dict[str, Any] = {"path": _rel(doc_path), "exists": doc_path.exists()}
    if doc_path.exists():
        doc_info["size_bytes"] = doc_path.stat().st_size

    return json.dumps({"doc": doc_info, "metadata": meta, "result": result}, indent=2, default=str)


_EML_PREVIEW_BYTES = 8000


def _open_source_email(params: dict) -> str:
    try:
        p = _safe_path(params.get("path", ""))
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if p.name.endswith(".metadata.json"):
        meta_path = p
    else:
        meta_path = p.parent / f"{p.name}.metadata.json"
    if not meta_path.exists():
        return json.dumps({"error": f"no metadata sidecar at {_rel(meta_path)}"})

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return json.dumps({"error": f"unreadable metadata: {e}"})

    link = meta.get("email_link")
    if not link:
        return json.dumps({"error": "metadata has no email_link — manual drop or pre-archive message"})

    eml_path = (_AGENT_ROOT / link).resolve()
    try:
        eml_path.relative_to(_SHARED_ROOT)
    except ValueError:
        return json.dumps({"error": "email_link points outside shared/"})
    if not eml_path.exists():
        return json.dumps({"error": f"eml not found at {_rel(eml_path)}"})

    raw = eml_path.read_text(encoding="utf-8", errors="replace")
    truncated = len(raw) > _EML_PREVIEW_BYTES
    return json.dumps(
        {
            "eml_path": _rel(eml_path),
            "bytes": eml_path.stat().st_size,
            "truncated": truncated,
            "content": raw[:_EML_PREVIEW_BYTES],
        },
        indent=2,
        default=str,
    )


# --- IMAP inbox tools --------------------------------------------------------
#
# Reads the tenant's own Dovecot mailbox via IMAP against the internal Docker
# service name ``dovecot:993`` with IMAPS. Credentials come from the agent's
# environment (set by provisioning): EMAIL_ADDRESS / EMAIL_PASSWORD /
# EMAIL_IMAP_HOST / EMAIL_IMAP_PORT. No network egress — traffic stays on the
# internal ``mpa-mail`` Docker network.

_KNOWN_FOLDERS = ("INBOX", "Drafts", "Sent", "Trash", "Junk")
_BODY_PREVIEW = 8000
_IMAP_TIMEOUT = 15


@contextmanager
def _imap():
    host = os.environ.get("EMAIL_IMAP_HOST", "dovecot")
    port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
    addr = os.environ.get("EMAIL_ADDRESS")
    pw = os.environ.get("EMAIL_PASSWORD")
    if not addr or not pw:
        raise RuntimeError("EMAIL_ADDRESS / EMAIL_PASSWORD not set in the agent environment")
    conn = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT)
    try:
        conn.login(addr, pw)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _extract_text_body(msg: email.message.EmailMessage) -> str:
    """Return the best-effort plain-text body from a parsed MIME message."""
    try:
        body_part = msg.get_body(preferencelist=("plain", "html"))
        if body_part is not None:
            content = body_part.get_content()
            return content if isinstance(content, str) else str(content)
    except Exception:
        pass
    try:
        return str(msg.get_payload(decode=True) or "")
    except Exception:
        return ""


def _list_inbox(params: dict) -> str:
    folder = str(params.get("folder") or "INBOX")
    if folder not in _KNOWN_FOLDERS:
        return json.dumps({"error": f"unknown folder '{folder}'. One of: {list(_KNOWN_FOLDERS)}"})

    from_sender = str(params.get("from_sender") or "").strip()
    subject = str(params.get("subject") or "").strip()
    since_raw = str(params.get("since") or "").strip()
    unread_only = bool(params.get("unread_only"))
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    criteria: list[str] = []
    if unread_only:
        criteria.append("UNSEEN")
    if from_sender:
        # IMAP SEARCH disallows unescaped quotes in astring; keep it simple.
        if '"' in from_sender:
            return json.dumps({"error": "from_sender must not contain \""})
        criteria.append(f'FROM "{from_sender}"')
    if subject:
        if '"' in subject:
            return json.dumps({"error": "subject must not contain \""})
        criteria.append(f'SUBJECT "{subject}"')
    if since_raw:
        try:
            d = date.fromisoformat(since_raw[:10])
        except Exception:
            return json.dumps({"error": "since must be YYYY-MM-DD"})
        criteria.append(f"SINCE {d.strftime('%d-%b-%Y')}")
    if not criteria:
        criteria = ["ALL"]

    try:
        with _imap() as conn:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                return json.dumps({"error": f"SELECT {folder} failed"})
            typ, data = conn.uid("SEARCH", None, *criteria)
            if typ != "OK":
                return json.dumps({"error": "SEARCH failed"})
            uids_all = (data[0] or b"").split()
            total = len(uids_all)
            uids = uids_all[-limit:]
            if not uids:
                return json.dumps({"folder": folder, "total": 0, "returned": 0, "messages": []})
            uids_csv = b",".join(uids).decode()
            typ, fetched = conn.uid(
                "FETCH",
                uids_csv,
                "(FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])",
            )
            if typ != "OK":
                return json.dumps({"error": "FETCH failed"})

            messages: list[dict] = []
            for raw in fetched:
                if not isinstance(raw, tuple):
                    continue
                meta_bytes, header_bytes = raw
                meta = meta_bytes.decode(errors="replace") if isinstance(meta_bytes, bytes) else str(meta_bytes)
                uid_m = re.search(r"UID (\d+)", meta)
                flags_m = re.search(r"FLAGS \(([^)]*)\)", meta)
                size_m = re.search(r"RFC822\.SIZE (\d+)", meta)
                idate_m = re.search(r'INTERNALDATE "([^"]+)"', meta)
                hmsg = email.message_from_bytes(header_bytes or b"", policy=email.policy.default)
                messages.append({
                    "uid": int(uid_m.group(1)) if uid_m else None,
                    "flags": (flags_m.group(1).split() if flags_m and flags_m.group(1) else []),
                    "size": int(size_m.group(1)) if size_m else None,
                    "internal_date": idate_m.group(1) if idate_m else "",
                    "from": str(hmsg.get("From") or ""),
                    "to": str(hmsg.get("To") or ""),
                    "cc": str(hmsg.get("Cc") or ""),
                    "date": str(hmsg.get("Date") or ""),
                    "subject": str(hmsg.get("Subject") or ""),
                    "message_id": str(hmsg.get("Message-Id") or ""),
                })
            messages.sort(key=lambda m: m.get("uid") or 0, reverse=True)
            return json.dumps(
                {"folder": folder, "total": total, "returned": len(messages), "messages": messages},
                indent=2, default=str,
            )
    except imaplib.IMAP4.error as e:
        return json.dumps({"error": f"IMAP error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def _read_inbox_message(params: dict) -> str:
    folder = str(params.get("folder") or "INBOX")
    if folder not in _KNOWN_FOLDERS:
        return json.dumps({"error": f"unknown folder '{folder}'. One of: {list(_KNOWN_FOLDERS)}"})
    try:
        uid = int(params.get("uid"))
    except (TypeError, ValueError):
        return json.dumps({"error": "uid (integer) is required"})

    try:
        with _imap() as conn:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                return json.dumps({"error": f"SELECT {folder} failed"})
            typ, data = conn.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                return json.dumps({"error": f"message uid={uid} not found in {folder}"})
            meta_bytes, raw = data[0]
            meta = meta_bytes.decode(errors="replace") if isinstance(meta_bytes, bytes) else str(meta_bytes)
            flags_m = re.search(r"FLAGS \(([^)]*)\)", meta)
            msg = email.message_from_bytes(raw or b"", policy=email.policy.default)
            body = _extract_text_body(msg)
            truncated = len(body) > _BODY_PREVIEW
            return json.dumps(
                {
                    "folder": folder,
                    "uid": uid,
                    "flags": (flags_m.group(1).split() if flags_m and flags_m.group(1) else []),
                    "from": str(msg.get("From") or ""),
                    "to": str(msg.get("To") or ""),
                    "cc": str(msg.get("Cc") or ""),
                    "date": str(msg.get("Date") or ""),
                    "subject": str(msg.get("Subject") or ""),
                    "message_id": str(msg.get("Message-Id") or ""),
                    "in_reply_to": str(msg.get("In-Reply-To") or ""),
                    "references": str(msg.get("References") or ""),
                    "body_text": body[:_BODY_PREVIEW],
                    "body_truncated": truncated,
                },
                indent=2, default=str,
            )
    except imaplib.IMAP4.error as e:
        return json.dumps({"error": f"IMAP error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def _quote_original(orig: email.message.EmailMessage) -> str:
    sender = str(orig.get("From") or "someone")
    dt = str(orig.get("Date") or "")
    body = _extract_text_body(orig) or ""
    quoted = "\n".join("> " + ln for ln in body.splitlines())
    header = f"On {dt}, {sender} wrote:" if dt else f"{sender} wrote:"
    return header + "\n" + quoted


def _create_draft_reply(params: dict) -> str:
    folder = str(params.get("folder") or "INBOX")
    if folder not in _KNOWN_FOLDERS:
        return json.dumps({"error": f"unknown folder '{folder}'. One of: {list(_KNOWN_FOLDERS)}"})
    try:
        uid = int(params.get("uid"))
    except (TypeError, ValueError):
        return json.dumps({"error": "uid (integer) is required"})
    body = str(params.get("body") or "").strip()
    if not body:
        return json.dumps({"error": "body is required"})
    include_quoted = params.get("include_quoted", True)
    if isinstance(include_quoted, str):
        include_quoted = include_quoted.lower() not in ("false", "0", "no", "")

    def _as_list(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []
    extra_to = _as_list(params.get("extra_to"))
    extra_cc = _as_list(params.get("extra_cc"))

    from_addr = os.environ.get("EMAIL_ADDRESS")
    if not from_addr:
        return json.dumps({"error": "EMAIL_ADDRESS not set — cannot set From: header"})

    try:
        with _imap() as conn:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                return json.dumps({"error": f"SELECT {folder} failed"})
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                return json.dumps({"error": f"message uid={uid} not found in {folder}"})
            _, raw = data[0]
            orig = email.message_from_bytes(raw or b"", policy=email.policy.default)

            reply = email.message.EmailMessage(policy=email.policy.SMTP)
            reply["From"] = from_addr

            reply_to_raw = str(orig.get("Reply-To") or orig.get("From") or "").strip()
            to_addrs = [reply_to_raw] if reply_to_raw else []
            to_addrs.extend(extra_to)
            reply["To"] = ", ".join(to_addrs) if to_addrs else from_addr
            if extra_cc:
                reply["Cc"] = ", ".join(extra_cc)

            subj = str(orig.get("Subject") or "")
            if not re.match(r"^re:\s*", subj, re.IGNORECASE):
                subj = "Re: " + subj
            reply["Subject"] = subj

            mid = orig.get("Message-Id")
            if mid:
                reply["In-Reply-To"] = mid
                refs = str(orig.get("References") or "").strip()
                reply["References"] = (refs + " " + mid).strip() if refs else mid

            reply["Date"] = email.utils.formatdate(localtime=True)
            reply["Message-Id"] = email.utils.make_msgid(domain=from_addr.split("@")[-1])

            full_body = body + ("\n\n" + _quote_original(orig) if include_quoted else "")
            reply.set_content(full_body)

            raw_msg = bytes(reply)

            typ, append_data = conn.append(
                "Drafts",
                r"(\Draft)",
                imaplib.Time2Internaldate(time.time()),
                raw_msg,
            )
            if typ != "OK":
                return json.dumps({"error": f"APPEND failed: {append_data}"})

            new_uid = None
            for item in (append_data or []):
                s = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
                m = re.search(r"APPENDUID (\d+) (\d+)", s)
                if m:
                    new_uid = int(m.group(2))
                    break

            return json.dumps(
                {
                    "status": "ok",
                    "folder": "Drafts",
                    "uid": new_uid,
                    "subject": subj,
                    "to": str(reply["To"]),
                    "cc": str(reply.get("Cc") or ""),
                    "bytes": len(raw_msg),
                    "hint": "Draft saved. Edit/send from your mail client's Drafts folder.",
                },
                indent=2, default=str,
            )
    except imaplib.IMAP4.error as e:
        return json.dumps({"error": f"IMAP error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# --- Plugin entry point ------------------------------------------------------

def register(ctx):
    search_schema = {
        "name": "search_documents",
        "description": (
            "Search the tenant's processed document corpus (invoices, contracts, tickets, generic documents). "
            "All filters combine with AND. Results are sorted by date descending.\n\n"
            "Examples:\n"
            "  search_documents(kind='invoice', vendor='digitalocean', from_date='2026-01-01')\n"
            "  search_documents(kind='invoice', min_amount=200, limit=5)\n"
            "  search_documents(kind='contract', query='acme')\n\n"
            "For the full playbook (when to use which tool, pitfalls, aggregation tips), load the skill "
            "'document-search:usage'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind":       {"type": "string", "enum": list(_KIND_DIRS), "description": "Restrict to one kind."},
                "query":      {"type": "string", "description": "Case-insensitive substring match on subject/body/sender/vendor/invoice_number/category/description."},
                "from_date":  {"type": "string", "description": "ISO date lower bound (YYYY-MM-DD)."},
                "to_date":    {"type": "string", "description": "ISO date upper bound (YYYY-MM-DD)."},
                "sender":     {"type": "string", "description": "Substring match on from_email / from_name."},
                "vendor":     {"type": "string", "description": "Substring match on extracted company_name."},
                "min_amount": {"type": "number", "description": "Minimum total/due amount."},
                "max_amount": {"type": "number", "description": "Maximum total/due amount."},
                "limit":      {"type": "integer", "description": "Max results (default 20, clamped to 100)."},
            },
        },
    }

    read_schema = {
        "name": "read_document",
        "description": (
            "Return the full metadata + extraction result for one document. Accepts the doc path or its "
            "``.metadata.json`` sidecar — agent-root-relative (``shared/invoices/...``), relative to shared/, "
            "or absolute under /opt/data/workspace/shared.\n\n"
            "Example: read_document(path='shared/invoices/2026-03/01/digitalocean-...pdf')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Document path."},
            },
            "required": ["path"],
        },
    }

    open_schema = {
        "name": "open_source_email",
        "description": "Return the raw .eml source that produced a document (up to ~8 KB of content).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Document path whose originating email you want."},
            },
            "required": ["path"],
        },
    }

    ctx.register_tool(
        name="search_documents",
        toolset="document-search",
        schema=search_schema,
        handler=_search_documents,
        description="Search the processed document corpus (invoices/contracts/tickets/documents) with structured filters.",
    )
    ctx.register_tool(
        name="read_document",
        toolset="document-search",
        schema=read_schema,
        handler=_read_document,
        description="Return full metadata + extracted fields for one document.",
    )
    ctx.register_tool(
        name="open_source_email",
        toolset="document-search",
        schema=open_schema,
        handler=_open_source_email,
        description="Return the originating raw .eml for a document.",
    )

    # --- IMAP inbox tools ---------------------------------------------------

    list_inbox_schema = {
        "name": "list_inbox",
        "description": (
            "List messages from the tenant's own mailbox via IMAP. Use this for raw-mailbox queries "
            "('show me mail from X', 'what's unread', 'recent messages'). For processed invoices/contracts "
            "prefer search_documents.\n\n"
            "Examples:\n"
            "  list_inbox(from_sender='olaf', limit=20)\n"
            "  list_inbox(unread_only=True)\n"
            "  list_inbox(folder='Sent', since='2026-04-01')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder":      {"type": "string", "enum": list(_KNOWN_FOLDERS), "description": "Mail folder (default INBOX)."},
                "from_sender": {"type": "string", "description": "IMAP FROM substring match."},
                "subject":     {"type": "string", "description": "IMAP SUBJECT substring match."},
                "since":       {"type": "string", "description": "ISO date lower bound (YYYY-MM-DD) — maps to IMAP SINCE."},
                "unread_only": {"type": "boolean", "description": "Only return UNSEEN messages."},
                "limit":       {"type": "integer", "description": "Max results (default 50, clamped to 200). Returns the newest N of the matching set."},
            },
        },
    }

    read_inbox_schema = {
        "name": "read_inbox_message",
        "description": (
            "Return headers + plain-text body (up to ~8 KB) for one message in the tenant's mailbox. "
            "Pair with list_inbox which returns UIDs.\n\n"
            "Example: read_inbox_message(uid=12345)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "enum": list(_KNOWN_FOLDERS), "description": "Folder where the UID lives (default INBOX)."},
                "uid":    {"type": "integer", "description": "IMAP UID from list_inbox."},
            },
            "required": ["uid"],
        },
    }

    draft_reply_schema = {
        "name": "create_draft_reply",
        "description": (
            "Compose a reply to a message and save it to the user's Drafts folder via IMAP APPEND. "
            "Does NOT send — the user reviews + sends from their mail client. Threading headers "
            "(In-Reply-To, References) and a 'Re: ' subject are set automatically. Returns the new draft UID.\n\n"
            "Example:\n"
            "  create_draft_reply(uid=12345, body='Dank, ik ga het morgen bekijken.')\n"
            "  create_draft_reply(uid=12345, body='...', include_quoted=False, extra_cc=['boss@company.nl'])"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder":         {"type": "string", "enum": list(_KNOWN_FOLDERS), "description": "Folder of the original message (default INBOX)."},
                "uid":            {"type": "integer", "description": "IMAP UID of the message being replied to."},
                "body":           {"type": "string", "description": "Reply body (plain text). Will be placed ABOVE any quoted original."},
                "include_quoted": {"type": "boolean", "description": "Append the original message as `> `-quoted text (default true)."},
                "extra_to":       {"type": "array", "items": {"type": "string"}, "description": "Additional To: recipients beyond the original sender."},
                "extra_cc":       {"type": "array", "items": {"type": "string"}, "description": "Cc: recipients."},
            },
            "required": ["uid", "body"],
        },
    }

    ctx.register_tool(
        name="list_inbox",
        toolset="document-search",
        schema=list_inbox_schema,
        handler=_list_inbox,
        description="List messages from the tenant's IMAP mailbox with optional filters.",
    )
    ctx.register_tool(
        name="read_inbox_message",
        toolset="document-search",
        schema=read_inbox_schema,
        handler=_read_inbox_message,
        description="Return headers + decoded body for one IMAP message.",
    )
    ctx.register_tool(
        name="create_draft_reply",
        toolset="document-search",
        schema=draft_reply_schema,
        handler=_create_draft_reply,
        description="Save a reply to the user's Drafts folder (does not send).",
    )

    # Register the usage skill so the agent can load it explicitly via
    # ``skill_view('document-search:usage')`` when it needs the playbook.
    skill_path = Path(__file__).parent / "skills" / "usage" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            name="usage",
            path=skill_path,
            description="How to use document-search — worked examples and pitfalls.",
        )
