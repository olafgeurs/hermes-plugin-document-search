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

import json
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


# --- Plugin entry point ------------------------------------------------------

def register(ctx):
    search_schema = {
        "name": "search_documents",
        "description": (
            "Search the tenant's processed document corpus (invoices, contracts, tickets, generic documents). "
            "All filters combine with AND. Results are sorted by date descending."
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
            "or absolute under /opt/data/workspace/shared."
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
