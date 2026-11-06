"""Run the intake agent over test_docs/ and compare with each case's expected.json.

    python score.py                    # all cases, real multimodal LLM (expects ./test_docs)
    python score.py --docs "C:/Users/me/Downloads/test_docs"   # test data anywhere else
    python score.py 01 05 10           # only cases whose folder name starts with these
    python score.py --offline          # tesseract test double (no API key); expect weaker results
Raw agent output for every case is saved to results/<case>.json
"""
import argparse
import difflib
import json
import re
from pathlib import Path

from crosscheck import parse_decimal
from schema import AMOUNT_FIELDS, NONNEG_AMOUNT_FIELDS

from graph import run_intake

ROOT = Path("test_docs")
RESULTS = Path("results")


def norm(v):
    """Case/whitespace-insensitive; ignores the '(SAMPLE)' tag that only exists in the synthetic test data."""
    if v in (None, ""):
        return None
    return re.sub(r"\s+", " ", re.sub(r"\(sample\)", "", str(v).lower())).strip()


FUZZY_FIELDS = {"hospital_name", "patient_name", "doctor_name", "diagnosis"}  # free text: tolerate small spelling drift


def same(field, got, want):
    if field in AMOUNT_FIELDS | NONNEG_AMOUNT_FIELDS and got and want:
        return parse_decimal(got) is not None and parse_decimal(got) == parse_decimal(want)
    if field in FUZZY_FIELDS and got and want:
        return difflib.SequenceMatcher(None, norm(got), norm(want)).ratio() >= 0.9
    return norm(got) == norm(want)


def score_case(case_dir: Path, extractor) -> dict:
    exp = json.loads((case_dir / "expected.json").read_text())
    files = sorted(case_dir / f for f in exp["files"])
    result = run_intake(extractor, [str(f) for f in files], user_text="")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{case_dir.name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))

    name_by_id = {s["file_id"]: s["original_name"] for s in result["source_files"]}
    docs = {name_by_id[d["source_file_id"]]: d for d in result["documents"]}
    lines, type_ok, f_ok, f_total = [], 0, 0, 0
    for fname, spec in exp["files"].items():
        d = docs[fname]
        t_ok = d["doc_type"] == spec["doc_type"]
        type_ok += t_ok
        lines.append(f"    {fname}: type {d['doc_type']} {'OK' if t_ok else 'WRONG (want ' + spec['doc_type'] + ')'}")
        got = {f["name"]: f["value"] for f in d["fields"]}
        for k, want in spec["fields"].items():
            f_total += 1
            ok = same(k, got.get(k), want)
            f_ok += ok
            if not ok:
                lines.append(f"      x {k}: got {got.get(k)!r}, want {want!r}")
    got_flags = {fl["code"] for d in result["documents"] for fl in d["validation_flags"]} | {fl["code"] for fl in result.get("cross_document_flags", [])}
    flags_ok = set(exp["expected_flags"]) <= got_flags
    review_ok = result["needs_human_review"] == exp["needs_human_review"]
    if not flags_ok:
        lines.append(f"      x flags: got {sorted(got_flags)}, want superset of {exp['expected_flags']}")
    return {"name": case_dir.name, "desc": exp["description"], "info": exp["informational"], "lines": lines,
            "types": (type_ok, len(exp["files"])), "fields": (f_ok, f_total), "flags_ok": flags_ok, "review_ok": review_ok,
            "got_review": result["needs_human_review"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*", help="case-name prefixes, e.g. 01 05")
    ap.add_argument("--docs", default="test_docs", help="folder that contains the case folders (01_..., 02_...)")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    if a.offline:
        from extractors import OfflineTestExtractor
        ex = OfflineTestExtractor()
    else:
        from extractors import MultimodalExtractor
        ex = MultimodalExtractor(a.model)

    root = Path(a.docs)
    if not root.is_dir():
        raise SystemExit(f"Test-data folder not found: {root.resolve()}  (use --docs <path>)")
    cases = [d for d in sorted(root.iterdir()) if d.is_dir() and (not a.cases or any(d.name.startswith(c) for c in a.cases))]
    tot_f = [0, 0]
    for d in cases:
        try:
            r = score_case(d, ex)
        except Exception as e:  # keep going: one bad case should not hide the rest
            print(f"\n{d.name}: ERROR {type(e).__name__}: {e}")
            continue
        passed = r["types"][0] == r["types"][1] and r["fields"][0] == r["fields"][1] and r["flags_ok"] and r["review_ok"]
        tag = "INFO" if r["info"] else ("PASS" if passed else "FAIL")
        print(f"\n[{tag}] {r['name']}  - {r['desc']}")
        print(f"    doc types {r['types'][0]}/{r['types'][1]} | fields {r['fields'][0]}/{r['fields'][1]} | flags {'OK' if r['flags_ok'] else 'MISSING'} | review flag {'OK' if r['review_ok'] else 'WRONG (got ' + str(r['got_review']) + ')'}")
        print(*r["lines"], sep="\n") if any("x " in l or "WRONG" in l for l in r["lines"]) else None
        if not r["info"]:
            tot_f[0] += r["fields"][0]; tot_f[1] += r["fields"][1]
    if tot_f[1]:
        print(f"\nField accuracy on scored (non-informational) cases: {tot_f[0]}/{tot_f[1]} = {100 * tot_f[0] / tot_f[1]:.0f}%")


if __name__ == "__main__":
    main()
