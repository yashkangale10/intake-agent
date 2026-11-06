"""Pipeline nodes. Each node is a plain function: state dict in -> partial state dict out.

Only `classify` and `extract` call a (multimodal) LLM. Ingest, page rendering, arithmetic and all
consistency checks are deterministic on purpose: the model never grades its own work.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from crosscheck import TOL, cross_check, normalize_amount, parse_decimal
from extractors import Extractor
from pages import STORE_DIR, SUPPORTED, PageRef, render_pages
from schema import (
    AMOUNT_FIELDS, DATE_FIELDS, NONNEG_AMOUNT_FIELDS, REQUIRED_FIELDS, ClaimIntake, DocType, ExtractedDocument,
    ExtractedField, LineItem, SourceFile, ValidationFlag,
)

MAX_RETRIES = int(os.getenv("INTAKE_MAX_RETRIES", "1"))   # extra extraction rounds after the first (each costs model calls)
MIN_FIELD_CONFIDENCE = 0.7
LINE_TOL = Decimal("0.5")
POLICY_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/]{5,24}$")  # loose; tighten per insurer
# Warnings that still send the claim to a human. Everything else at 'warning'/'info' level is context only.
REVIEW_WARNING_CODES = {"PATIENT_NAME_INITIAL_ONLY", "LOW_CONFIDENCE", "MULTI_DOCUMENT_SUSPECTED", "HOSPITAL_NAME_MISMATCH", "DUPLICATE_BILL",
                        "LINE_AMOUNT_MISMATCH", "LINE_ITEM_UNREADABLE"}


class IntakeState(TypedDict, total=False):
    file_paths: list[str]
    user_text: str
    claim_id: str
    source_files: list[dict]
    pages: list[PageRef]     # one PNG per page; no OCR text anywhere
    documents: list[dict]    # per file: doc_type, page_count, fields, line_items, flags, ...
    cross_flags: list[dict]  # cross-document flags
    duplicate_uploads: list[str]  # names of files dropped because identical bytes were already uploaded
    attempt: int
    result: dict


# --------------------------------------------------------------------------- deterministic helpers
def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _parse_amount(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except Exception:
        return None


def _check_bill_arithmetic(by_name: dict, items: list[LineItem], flags: list[ValidationFlag]) -> Optional[Decimal]:
    """Arithmetic done in code from transcribed numbers: per-row qty x rate, sum of rows, and
    total = subtotal - discount + tax. Returns the computed sum of line items (or None)."""
    amounts: list[Decimal] = []
    all_readable = bool(items)
    for i, li in enumerate(items, start=1):
        a, q, r = parse_decimal(li.amount), parse_decimal(li.quantity), parse_decimal(li.rate)
        if a is None:
            all_readable = False
            flags.append(ValidationFlag(field="line_items", code="LINE_ITEM_UNREADABLE", severity="warning",
                                        message=f"Line {i} ('{li.description}') has no readable amount."))
            continue
        amounts.append(a)
        if q is not None and r is not None and abs(q * r - a) > LINE_TOL:
            flags.append(ValidationFlag(field="line_items", code="LINE_AMOUNT_MISMATCH", severity="warning",
                                        message=f"Line {i} ('{li.description}'): {q} x {r} = {q * r}, but the bill prints {a}. "
                                                "(Line-level tax/discount can cause this.)"))
    computed = sum(amounts, Decimal(0)) if (items and all_readable) else None

    def num(name: str) -> Optional[Decimal]:
        f = by_name.get(name)
        return parse_decimal(f.value) if f and f.value else None

    printed_sub, total = num("line_items_total"), num("total_amount")
    discount, tax = num("discount_amount") or Decimal(0), num("tax_amount") or Decimal(0)
    if computed is not None and printed_sub is not None and abs(computed - printed_sub) > TOL:
        flags.append(ValidationFlag(field="line_items_total", code="LINE_ITEMS_SUM_MISMATCH", severity="error",
                                    message=f"Line items add up to {computed}, but the bill's printed sub-total is {printed_sub}."))
    subtotal = printed_sub if printed_sub is not None else computed
    if subtotal is not None and total is not None:
        expected = subtotal - discount + tax
        if abs(total - expected) > TOL:
            flags.append(ValidationFlag(field="total_amount", code="TOTAL_MISMATCH", severity="error",
                                        message=f"Bill total {total} != sub-total {subtotal} - discount {discount} + tax {tax} = {expected}."))
    return computed


def validate_document(doc_type: DocType, fields: list[ExtractedField], line_items: list[LineItem] | None = None,
                      today: date | None = None) -> tuple[list[str], list[ValidationFlag], Optional[Decimal]]:
    """Deterministic checks. Returns (missing_required, flags, computed_line_items_total)."""
    today = today or date.today()
    by_name = {f.name: f for f in fields}
    flags: list[ValidationFlag] = []
    missing: list[str] = []

    if doc_type == DocType.UNKNOWN:
        flags.append(ValidationFlag(code="UNKNOWN_DOC_TYPE", severity="error", message="Could not classify the document"))

    for name in REQUIRED_FIELDS[doc_type]:
        f = by_name.get(name)
        if f is None or not f.value:
            missing.append(name)
            flags.append(ValidationFlag(field=name, code="MISSING_REQUIRED", severity="error", message=f"Required field '{name}' not found"))

    for f in fields:
        if not f.value:
            continue
        if f.confidence < MIN_FIELD_CONFIDENCE:
            flags.append(ValidationFlag(field=f.name, code="LOW_CONFIDENCE", severity="warning",
                                        message=f"'{f.name}' confidence {f.confidence:.2f} < {MIN_FIELD_CONFIDENCE}"))
        if f.name in DATE_FIELDS:
            d = _parse_date(f.value)
            if d is None:
                flags.append(ValidationFlag(field=f.name, code="BAD_DATE", severity="error", message=f"'{f.value}' is not an ISO date"))
            elif d > today:
                flags.append(ValidationFlag(field=f.name, code="FUTURE_DATE", severity="error", message=f"'{f.value}' is in the future"))
        if f.name in AMOUNT_FIELDS:
            amt = _parse_amount(f.value)
            if amt is None or amt <= 0:
                flags.append(ValidationFlag(field=f.name, code="BAD_AMOUNT", severity="error", message=f"'{f.value}' is not a positive amount"))
        if f.name in NONNEG_AMOUNT_FIELDS:
            amt = _parse_amount(f.value)
            if amt is None or amt < 0:
                flags.append(ValidationFlag(field=f.name, code="BAD_AMOUNT", severity="error", message=f"'{f.value}' is not a valid amount"))
        if f.name == "policy_number" and not POLICY_NUMBER_RE.match(f.value):
            flags.append(ValidationFlag(field=f.name, code="BAD_POLICY_NUMBER", severity="error", message=f"'{f.value}' does not look like a policy number"))

    adm, dis = (_parse_date(by_name[n].value or "") if n in by_name else None for n in ("admission_date", "discharge_date"))
    if adm and dis and dis < adm:
        flags.append(ValidationFlag(field="discharge_date", code="DATE_ORDER", severity="error", message="Discharge date is before admission date"))

    computed = _check_bill_arithmetic(by_name, line_items or [], flags) if doc_type == DocType.HOSPITAL_BILL else None
    return missing, flags, computed


def _normalize_amounts(res):
    """Models sometimes return '5,00,000' or '200000/-' despite instructions. Normalise in code; keep the original as evidence."""
    for f in res.fields:
        if f.name in AMOUNT_FIELDS or f.name in NONNEG_AMOUNT_FIELDS:
            norm = normalize_amount(f.value)
            if norm is not None and norm != f.value:
                f.evidence = f.evidence or f.value
                f.value = norm
    for li in res.line_items:
        for attr in ("quantity", "rate", "amount"):
            v = getattr(li, attr)
            norm = normalize_amount(v)
            if norm is not None and norm != v:
                setattr(li, attr, norm)
    return res


def _needs_review(f: ValidationFlag) -> bool:
    return f.severity == "error" or f.code in REVIEW_WARNING_CODES


def route_after_validate(state: IntakeState) -> str:
    """'retry' re-extracts documents with per-document errors (error text as feedback); otherwise finish (flagging for review).
    Cross-document errors never trigger a retry: they describe real differences between documents, and re-reading one
    document cannot fix them (feeding the other document's value back would invite the model to copy it)."""
    has_error = any(f["severity"] == "error" for d in state["documents"] for f in d["validation_flags"])
    return "retry" if has_error and state.get("attempt", 0) < 1 + MAX_RETRIES else "emit"


# --------------------------------------------------------------------------- node factory
def make_nodes(extractor: Extractor) -> dict[str, Callable[[IntakeState], dict[str, Any]]]:
    def ingest(state: IntakeState) -> dict:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        sources, kept, dupes = [], [], []
        for p in state["file_paths"]:
            path = Path(p)
            if path.suffix.lower() not in SUPPORTED:
                raise ValueError(f"Unsupported file type: {path.name}")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            file_id = f"f_{sha[:12]}"
            if any(s["file_id"] == file_id for s in sources):  # same bytes uploaded twice: process once, tell a human
                dupes.append(path.name)
                continue
            kept.append(p)
            shutil.copy(path, STORE_DIR / f"{file_id}{path.suffix.lower()}")  # in production: upload to the shared Server
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            sources.append(SourceFile(file_id=file_id, original_name=path.name, sha256=sha, mime_type=mime).model_dump())
        return {"source_files": sources, "file_paths": kept, "duplicate_uploads": dupes,
                "claim_id": state.get("claim_id") or f"clm_{uuid.uuid4().hex[:10]}", "attempt": 0}

    def prepare(state: IntakeState) -> dict:
        pages: list[PageRef] = []
        for src, p in zip(state["source_files"], state["file_paths"]):
            pages.extend(render_pages(Path(p), src["file_id"]))
        return {"pages": pages}

    def classify(state: IntakeState) -> dict:
        docs = []
        for src in state["source_files"]:
            fp = [p for p in state["pages"] if p["file_id"] == src["file_id"]]
            c = extractor.classify(fp, state.get("user_text", ""))
            docs.append({"file_id": src["file_id"], "doc_type": c.doc_type.value, "page_count": len(fp),
                         "multiple_documents": c.multiple_documents, "other_doc_types": [t.value for t in c.other_doc_types],
                         "extracted_by": extractor.name, "fields": [], "line_items": [], "computed_line_items_total": None,
                         "missing_required": [], "validation_flags": []})
        return {"documents": docs}

    def extract(state: IntakeState) -> dict:
        attempt = state.get("attempt", 0)
        docs = []
        for d in state["documents"]:
            doc_type = DocType(d["doc_type"])
            fp = [p for p in state["pages"] if p["file_id"] == d["file_id"]]
            feedback = [f["message"] for f in d["validation_flags"] if f["severity"] == "error"]
            if doc_type == DocType.UNKNOWN or (attempt > 0 and not feedback):
                docs.append(d)  # unknown type, or already clean: carry forward instead of re-asking the model
                continue
            res = _normalize_amounts(extractor.extract(doc_type, fp, user_text=state.get("user_text", ""), feedback=feedback, attempt=attempt))
            docs.append({**d, "fields": [f.model_dump() for f in res.fields], "line_items": [li.model_dump() for li in res.line_items]})
        return {"documents": docs, "attempt": attempt + 1}

    def validate(state: IntakeState) -> dict:
        docs = []
        for d in state["documents"]:
            fields = [ExtractedField(**f) for f in d["fields"]]
            items = [LineItem(**li) for li in d.get("line_items", [])]
            missing, flags, computed = validate_document(DocType(d["doc_type"]), fields, items)
            if d.get("multiple_documents"):
                others = ", ".join(d.get("other_doc_types") or []) or "another document"
                flags.append(ValidationFlag(code="MULTI_DOCUMENT_SUSPECTED", severity="warning",
                                            message=f"File appears to contain more than one document (also: {others}); "
                                                    "only the first was extracted. Split the file and resubmit."))
            docs.append({**d, "missing_required": missing, "validation_flags": [f.model_dump() for f in flags],
                         "computed_line_items_total": format(computed, "f") if computed is not None else None})
        return {"documents": docs}

    def cross_check_node(state: IntakeState) -> dict:
        return {"cross_flags": [f.model_dump() for f in cross_check(state["documents"])]}

    def emit(state: IntakeState) -> dict:
        documents, reasons = [], []
        for d in state["documents"]:
            doc = ExtractedDocument(
                doc_type=DocType(d["doc_type"]), source_file_id=d["file_id"], page_count=d["page_count"], extracted_by=d["extracted_by"],
                fields=[ExtractedField(**f) for f in d["fields"]], line_items=[LineItem(**li) for li in d.get("line_items", [])],
                computed_line_items_total=d.get("computed_line_items_total"), missing_required=d["missing_required"],
                validation_flags=[ValidationFlag(**f) for f in d["validation_flags"]],
            )
            documents.append(doc)
            reasons += [f"{doc.doc_type.value}/{doc.source_file_id}: {f.message}" for f in doc.validation_flags if _needs_review(f)]
        cross = [ValidationFlag(**f) for f in state.get("cross_flags", [])]
        reasons += [f"cross-check: {f.message}" for f in cross if _needs_review(f)]
        reasons += [f"duplicate upload ignored: {n}" for n in state.get("duplicate_uploads", [])]
        intake = ClaimIntake(
            claim_id=state["claim_id"], user_text=state.get("user_text", ""),
            source_files=[SourceFile(**s) for s in state["source_files"]],
            documents=documents, cross_document_flags=cross, needs_human_review=bool(reasons), review_reasons=reasons,
        )
        return {"result": intake.model_dump(mode="json")}

    return {"ingest": ingest, "prepare": prepare, "classify": classify, "extract": extract, "validate": validate,
            "cross_check": cross_check_node, "emit": emit}
