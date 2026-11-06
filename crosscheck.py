"""Cross-document consistency checks (deterministic, no LLM).

Design rule: Agent 1 only reports INCONSISTENCIES between documents. It never decides eligibility.
  * claim < billed  -> normal (partial claim, e.g. capped by sum insured): only an 'info' flag for Agent 2.
  * claim > billed  -> error (cannot claim more than the uploaded bills show).
  * patient / dates / policy number differ between documents -> error.
  * hospital name differs -> warning (abbreviations are common).
"""
from __future__ import annotations

import difflib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from schema import DocType, ValidationFlag

TOL = Decimal("1")  # rupee tolerance for rounding
_TITLES = {"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "dr", "master", "baby", "late"}
_GENERIC_HOSPITAL_WORDS = {"hospital", "hospitals", "multispeciality", "multispecialty", "super", "speciality", "specialty",
                           "clinic", "pvt", "ltd", "private", "limited", "the", "and", "centre", "center", "medical",
                           "research", "institute", "sample", "nursing", "home"}
_PERSON_DOC_TYPES = {DocType.CLAIM_FORM.value, DocType.HOSPITAL_BILL.value, DocType.DISCHARGE_SUMMARY.value, DocType.PRESCRIPTION.value}


_AMOUNT_RE = re.compile(r"^\s*(?:rs\.?|inr|\u20b9)?\s*([\d,]*\d(?:\.\d+)?)\s*(?:/-|-|only)?\s*$", re.I)


def parse_decimal(s) -> Optional[Decimal]:
    """Strict-but-tolerant amount parser: '1,07,450', 'Rs. 500/-', '200000/-', '\u20b92,00,000' -> Decimal.
    Anything with other text mixed in ('Rs 500 (3 days)') -> None, rather than guessing which digits count."""
    if s is None:
        return None
    m = _AMOUNT_RE.match(str(s))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def normalize_amount(s) -> Optional[str]:
    """'5,00,000' / '200000/-' / '1,07,450.00' -> '500000' / '200000' / '107450'; None if not a clean amount."""
    d = parse_decimal(s)
    if d is None:
        return None
    t = format(d, "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _val(doc: dict, name: str) -> Optional[str]:
    for f in doc["fields"]:
        if f["name"] == name and f.get("value"):
            return f["value"]
    return None


def _name_tokens(s: str) -> list[str]:
    return [t for t in re.sub(r"[^\w\s]", " ", s.lower()).split() if t not in _TITLES]


def _tok_rel(t: str, pool: list[str]) -> Optional[str]:
    """'full' if t matches a token outright (or within OCR-typo tolerance), 'initial' if only via a one-letter initial."""
    if any(t == u or difflib.SequenceMatcher(None, t, u).ratio() >= 0.85 for u in pool):
        return "full"
    if any((len(t) == 1 and u.startswith(t)) or (len(u) == 1 and t.startswith(u)) for u in pool):
        return "initial"
    return None


def name_relation(a: str, b: str) -> str:
    """'match' | 'initial' (matches only because an initial stands for a full name) | 'mismatch'.
    A name that is empty after dropping titles ('Mr.') never matches anything."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return "mismatch"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    rels = [_tok_rel(t, long_) for t in short]
    if all(rels):
        return "initial" if "initial" in rels else "match"
    if difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() >= 0.88:
        return "match"
    return "mismatch"


def names_match(a: str, b: str) -> bool:
    """Order-insensitive; tolerates titles, initials ('A. Deshmukh'), a missing middle name, small OCR typos."""
    return name_relation(a, b) != "mismatch"


def hospital_match(a: str, b: str) -> bool:
    ta = {t for t in re.sub(r"[^\w\s]", " ", a.lower()).split() if t not in _GENERIC_HOSPITAL_WORDS}
    tb = {t for t in re.sub(r"[^\w\s]", " ", b.lower()).split() if t not in _GENERIC_HOSPITAL_WORDS}
    if not ta and not tb:  # both names are only generic words: compare them as written instead of matching everything
        raw = lambda s: " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())
        return difflib.SequenceMatcher(None, raw(a), raw(b)).ratio() >= 0.85
    if not ta or not tb:
        return False
    if len(ta & tb) / len(ta | tb) >= 0.5:
        return True
    return difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() >= 0.7


def _label(d: dict) -> str:
    return f"{d['doc_type']} ({d['file_id']})"


def cross_check(docs: list[dict]) -> list[ValidationFlag]:
    flags: list[ValidationFlag] = []

    # ---- claim amount vs billed amount ------------------------------------------------------------
    claims = [(d, parse_decimal(_val(d, "claim_amount"))) for d in docs if d["doc_type"] == DocType.CLAIM_FORM.value]
    claims = [(d, a) for d, a in claims if a is not None]
    seen: dict[tuple, str] = {}
    bills: list[tuple[dict, Decimal]] = []
    for d in docs:
        if d["doc_type"] != DocType.HOSPITAL_BILL.value:
            continue
        total, num = parse_decimal(_val(d, "total_amount")), _val(d, "bill_number")
        if total is None:
            continue
        key = (re.sub(r"\W", "", num.lower()) if num else None, total)
        if key[0] and key in seen:  # same bill uploaded twice: count it once, but tell a human
            flags.append(ValidationFlag(code="DUPLICATE_BILL", severity="warning", field="bill_number",
                                        message=f"Bill {num} appears more than once; counted once.",
                                        source_file_ids=[seen[key], d["file_id"]]))
            continue
        if key[0]:
            seen[key] = d["file_id"]
        bills.append((d, total))
    if claims and bills:
        billed = sum(t for _, t in bills)
        for cd, claim in claims:
            ids = [cd["file_id"]] + [b["file_id"] for b, _ in bills]
            if claim > billed + TOL:
                flags.append(ValidationFlag(code="CLAIM_EXCEEDS_BILL", severity="error", field="claim_amount", source_file_ids=ids,
                                            message=f"Claimed {claim} is more than the total of the uploaded bills ({billed}). "
                                                    "A bill may be missing, or an amount was misread."))
            elif claim < billed - TOL:
                flags.append(ValidationFlag(code="PARTIAL_CLAIM", severity="info", field="claim_amount", source_file_ids=ids,
                                            message=f"Claimed {claim} of {billed} billed. Normal for partial claims; "
                                                    "sum insured / limits are checked by the claims agent."))

    # ---- patient name -----------------------------------------------------------------------------
    named = [(d, _val(d, "patient_name")) for d in docs if d["doc_type"] in _PERSON_DOC_TYPES]
    named = [(d, n) for d, n in named if n]
    if len(named) > 1:
        ref_doc, ref_name = next(((d, n) for d, n in named if d["doc_type"] == DocType.CLAIM_FORM.value), named[0])
        for d, n in named:
            if d is ref_doc:
                continue
            rel = name_relation(ref_name, n)
            if rel == "initial":
                flags.append(ValidationFlag(code="PATIENT_NAME_INITIAL_ONLY", severity="warning", field="patient_name",
                                            source_file_ids=[ref_doc["file_id"], d["file_id"]],
                                            message=f"Patient names match only through an initial: '{ref_name}' on {_label(ref_doc)} vs '{n}' on {_label(d)}. Verify identity."))
            elif rel == "mismatch":
                flags.append(ValidationFlag(code="PATIENT_NAME_MISMATCH", severity="error", field="patient_name",
                                            source_file_ids=[ref_doc["file_id"], d["file_id"]],
                                            message=f"Patient name differs: '{ref_name}' on {_label(ref_doc)} vs '{n}' on {_label(d)}."))

    # ---- admission / discharge dates --------------------------------------------------------------
    for name in ("admission_date", "discharge_date"):
        found = []
        for d in docs:
            v = _val(d, name)
            try:
                if v:
                    date.fromisoformat(v)
                    found.append((d, v))
            except ValueError:
                pass  # bad dates are flagged per document
        if len({v for _, v in found}) > 1:
            flags.append(ValidationFlag(code="DATE_MISMATCH", severity="error", field=name,
                                        source_file_ids=[d["file_id"] for d, _ in found],
                                        message=f"{name} differs across documents: " + "; ".join(f"{v} on {_label(d)}" for d, v in found)))

    # ---- policy number ----------------------------------------------------------------------------
    pols = [(d, _val(d, "policy_number")) for d in docs]
    pols = [(d, p) for d, p in pols if p]
    if len({re.sub(r"[^A-Za-z0-9]", "", p).upper() for _, p in pols}) > 1:
        flags.append(ValidationFlag(code="POLICY_NUMBER_MISMATCH", severity="error", field="policy_number",
                                    source_file_ids=[d["file_id"] for d, _ in pols],
                                    message="Policy number differs across documents: " + "; ".join(f"{p} on {_label(d)}" for d, p in pols)))

    # ---- hospital name (warning only: abbreviations are common) -----------------------------------
    hosp = [(d, _val(d, "hospital_name")) for d in docs]
    hosp = [(d, h) for d, h in hosp if h]
    if len(hosp) > 1:
        ref_doc, ref = hosp[0]
        for d, h in hosp[1:]:
            if not hospital_match(ref, h):
                flags.append(ValidationFlag(code="HOSPITAL_NAME_MISMATCH", severity="warning", field="hospital_name",
                                            source_file_ids=[ref_doc["file_id"], d["file_id"]],
                                            message=f"Hospital name differs: '{ref}' on {_label(ref_doc)} vs '{h}' on {_label(d)}."))
    return flags
