"""Offline tests for the deterministic rules (no API key, no network).   python test_logic.py"""
import sys
from datetime import date

from crosscheck import cross_check, hospital_match, names_match, normalize_amount, parse_decimal
from nodes import validate_document
from schema import DocType, ExtractedField, LineItem

FAILED = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILED.append(name)


def doc(doc_type, fid, **fields):
    return {"doc_type": doc_type, "file_id": fid, "fields": [{"name": k, "value": v, "confidence": 1.0} for k, v in fields.items()]}


def codes(flags, sev=None):
    return sorted(f.code for f in flags if sev is None or f.severity == sev)


# ---------------- claim amount vs billed amount ----------------
bill500 = doc("hospital_bill", "b1", total_amount="500000", bill_number="B-1")
f = cross_check([doc("claim_form", "c1", claim_amount="200000"), bill500])
check("partial claim (2L of 5L): no errors, only info PARTIAL_CLAIM", codes(f, "error") == [] and codes(f) == ["PARTIAL_CLAIM"])
f = cross_check([doc("claim_form", "c1", claim_amount="600000"), bill500])
check("claim above bill -> CLAIM_EXCEEDS_BILL error", codes(f, "error") == ["CLAIM_EXCEEDS_BILL"])
f = cross_check([doc("claim_form", "c1", claim_amount="500000"), bill500])
check("claim == bill -> nothing", codes(f) == [])
f = cross_check([doc("claim_form", "c1", claim_amount="450000"), doc("hospital_bill", "b1", total_amount="300000", bill_number="B-1"),
                 doc("hospital_bill", "b2", total_amount="200000", bill_number="B-2")])
check("claim vs SUM of two bills (4.5L of 5L) -> partial only", codes(f) == ["PARTIAL_CLAIM"])
f = cross_check([doc("claim_form", "c1", claim_amount="600000"), doc("hospital_bill", "b1", total_amount="300000", bill_number="B-1"),
                 doc("hospital_bill", "b2", total_amount="200000", bill_number="B-2")])
check("claim above sum of two bills -> error", codes(f, "error") == ["CLAIM_EXCEEDS_BILL"])
f = cross_check([doc("claim_form", "c1", claim_amount="500000"), bill500, doc("hospital_bill", "b2", total_amount="500000", bill_number="B-1")])
check("same bill uploaded twice: counted once, DUPLICATE_BILL warning, no false 'exceeds'", codes(f) == ["DUPLICATE_BILL"])
f = cross_check([bill500])
check("no claim form -> no amount check", codes(f) == [])

# ---------------- names ----------------
for a, b in [("Anil Deshmukh", "Deshmukh Anil"), ("Anil Deshmukh", "A. Deshmukh"), ("Anil Deshmukh", "Mr. Anil Deshmukh"),
             ("Anil Deshmukh", "Anil Deshmuhk"), ("Anil Deshmukh", "ANIL R DESHMUKH")]:
    check(f"names match: {a!r} ~ {b!r}", names_match(a, b))
for a, b in [("Anil Deshmukh", "Sunita Joshi"), ("Anil Deshmukh", "Anil Patil")]:
    check(f"names differ: {a!r} != {b!r}", not names_match(a, b))
f = cross_check([doc("claim_form", "c1", patient_name="Anil Deshmukh"), doc("hospital_bill", "b1", patient_name="Sunita Joshi")])
check("patient mismatch across docs -> error", codes(f, "error") == ["PATIENT_NAME_MISMATCH"])
check("hospital abbreviation tolerated", hospital_match("Lakeview Multispeciality Hospital (SAMPLE)", "LAKEVIEW MULTISPECIALITY HOSPITAL"))
check("different hospitals -> mismatch", not hospital_match("Lakeview Hospital", "Sahyadri Hospital"))

# ---------------- dates / policy ----------------
f = cross_check([doc("claim_form", "c1", discharge_date="2026-10-15"), doc("discharge_summary", "d1", discharge_date="2026-10-16")])
check("discharge date differs across docs -> DATE_MISMATCH", codes(f, "error") == ["DATE_MISMATCH"])
f = cross_check([doc("claim_form", "c1", discharge_date="2026-10-15"), doc("discharge_summary", "d1", discharge_date="2026-10-15")])
check("same dates -> nothing", codes(f) == [])
f = cross_check([doc("claim_form", "c1", policy_number="SAMP-HLT-2026-004521"), doc("hospital_bill", "b1", policy_number="SAMP HLT 2026 004521")])
check("policy number formatting differences ignored", codes(f) == [])
f = cross_check([doc("claim_form", "c1", policy_number="SAMP-HLT-2026-004521"), doc("hospital_bill", "b1", policy_number="SAMP-HLT-2026-004522")])
check("policy number differs -> error", codes(f, "error") == ["POLICY_NUMBER_MISMATCH"])

# ---------------- bill arithmetic ----------------
def F(**kw):
    return [ExtractedField(name=k, value=v, confidence=1.0) for k, v in kw.items()]

items = [LineItem(description="Room", quantity="3", rate="4500", amount="13500"), LineItem(description="Surgeon", quantity="1", rate="35000", amount="35000")]
_, fl, comp = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                                total_amount="48500", line_items_total="48500"), items)
check("consistent bill: no flags, computed sum 48500", codes(fl) == [] and comp == 48500)
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                             total_amount="43500", line_items_total="48500", discount_amount="5000"), items)
check("discount applied correctly -> no false TOTAL_MISMATCH", "TOTAL_MISMATCH" not in codes(fl))
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                             total_amount="51500", line_items_total="48500"), items)
check("total inflated by 3000 -> TOTAL_MISMATCH", "TOTAL_MISMATCH" in codes(fl, "error"))
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                             total_amount="48500"), items)
check("no printed sub-total: computed sum used, consistent -> no flags", codes(fl) == [])
bad = [LineItem(description="Room", quantity="3", rate="4500", amount="18500"), items[1]]
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                             total_amount="48500", line_items_total="48500"), bad)
check("tampered line amount -> LINE_AMOUNT_MISMATCH + LINE_ITEMS_SUM_MISMATCH", {"LINE_AMOUNT_MISMATCH", "LINE_ITEMS_SUM_MISMATCH"} <= set(codes(fl)))
unread = [LineItem(description="Room", quantity="3", rate="4500", amount=None), items[1]]
_, fl, comp = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15",
                                total_amount="48500", line_items_total="48500"), unread)
check("unreadable line -> LINE_ITEM_UNREADABLE and no bogus sum check", "LINE_ITEM_UNREADABLE" in codes(fl) and comp is None and "LINE_ITEMS_SUM_MISMATCH" not in codes(fl))
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2099-01-01", total_amount="10"), [])
check("future bill date -> FUTURE_DATE", "FUTURE_DATE" in codes(fl, "error"))
_, fl, _ = validate_document(DocType.HOSPITAL_BILL, F(hospital_name="H", patient_name="P", bill_number="1", bill_date="2026-10-15", total_amount="10", discount_amount="0"), [])
check("discount_amount 0 is valid", "BAD_AMOUNT" not in codes(fl))

# ---------------- amount formats seen from real models ----------------
for raw, want in [("5,00,000", "500000"), ("200000/-", "200000"), ("Rs. 2,00,000/-", "200000"), ("\u20b91,07,450", "107450"),
                  ("1,07,450.00", "107450"), ("1234.50", "1234.5"), ("107450", "107450")]:
    check(f"normalize_amount({raw!r}) == {want!r}", normalize_amount(raw) == want)
for junk in ["Rs 500 (3 days)", "abc", "", None, "1.2.3"]:
    check(f"normalize_amount({junk!r}) is None (no digit-scraping)", normalize_amount(junk) is None)

# ---------------- regression: Indian-format amounts from the model must not break the partial-claim logic ----------------
import hashlib, pathlib
from graph import run_intake
from schema import Classification, ExtractionResult

class IndianFormat:
    name = "indian-format-model"
    def classify(self, pages, user_text):
        return Classification(doc_type=DocType.CLAIM_FORM if pages[0]["file_id"] == CLAIM_ID else DocType.HOSPITAL_BILL)
    def extract(self, doc_type, pages, *, user_text, feedback, attempt):
        def fl(**kw): return [ExtractedField(name=k, value=v, confidence=1.0) for k, v in kw.items()]
        if doc_type == DocType.CLAIM_FORM:
            return ExtractionResult(fields=fl(policy_number="SAMP-HLT-2026-004521", patient_name="Anil Deshmukh", claim_amount="200000/-", date_of_claim="2026-10-20"))
        return ExtractionResult(fields=fl(hospital_name="Lakeview Hospital", patient_name="Anil Deshmukh", bill_number="B-1", bill_date="2026-10-15",
                                          total_amount="5,00,000", line_items_total="5,00,000"),
                                line_items=[LineItem(description="Surgery", quantity="1", rate="5,00,000", amount="5,00,000")])

_files = ["test_docs/01_clean_appendectomy/claim_form.png", "test_docs/01_clean_appendectomy/discharge_summary.png"]
CLAIM_ID = "f_" + hashlib.sha256(pathlib.Path(_files[0]).read_bytes()).hexdigest()[:12]
_r = run_intake(IndianFormat(), _files, "")
_bill = next(d for d in _r["documents"] if d["doc_type"] == "hospital_bill")
check("Indian-format amounts: no BAD_AMOUNT, no review", _r["needs_human_review"] is False and not any(f["code"] == "BAD_AMOUNT" for d in _r["documents"] for f in d["validation_flags"]))
check("Indian-format amounts: PARTIAL_CLAIM info present", [f["code"] for f in _r["cross_document_flags"]] == ["PARTIAL_CLAIM"])
check("amounts stored normalised, original kept as evidence", next(f for f in _bill["fields"] if f["name"] == "total_amount")["value"] == "500000"
      and next(f for f in _bill["fields"] if f["name"] == "total_amount")["evidence"] == "5,00,000")

# ---------------- review-driven regressions ----------------
from crosscheck import name_relation
from nodes import route_after_validate
check("initial vs different full name flagged as initial-only, not silent match", name_relation("A. Sharma", "Ajay Sharma") == "initial")
check("full name vs full name = match", name_relation("Anil Sharma", "Anil Sharma") == "match")
check("title-only name never matches", not names_match("Mr.", "Sunita Joshi"))
check("all-generic hospital names compared as written", not hospital_match("The Private Clinic", "Medical Centre Ltd"))
check("same generic hospital name still matches", hospital_match("Medical Centre Ltd", "MEDICAL CENTRE LTD."))
check("negative amount is not parsed", parse_decimal("-500") is None)
f = cross_check([doc("claim_form", "c1", patient_name="A. Sharma"), doc("hospital_bill", "b1", patient_name="Ajay Sharma")])
check("initial-only name -> warning, no error", codes(f, "warning") == ["PATIENT_NAME_INITIAL_ONLY"] and codes(f, "error") == [])
cross_only = {"documents": [{"validation_flags": []}], "cross_flags": [{"severity": "error"}], "attempt": 1}
check("cross-document errors alone do not trigger a retry", route_after_validate(cross_only) == "emit")
doc_err = {"documents": [{"validation_flags": [{"severity": "error"}]}], "cross_flags": [], "attempt": 1}
check("per-document errors still retry", route_after_validate(doc_err) == "retry")

print(f"\n{len(FAILED)} failed" if FAILED else "\nAll logic tests passed")
sys.exit(1 if FAILED else 0)
