# Document Intake Agent (Agent 1) - multimodal

Image/PDF/text in -> `ClaimIntake` JSON out (contract in `schema.py`, v1.1) -> Agent 2 (Claims Processing) over A2A.
No OCR tool: page images go straight to a vision-capable LLM. Everything after extraction is plain code.

```
ingest -> prepare -> classify (LLM) -> extract (LLM) -> validate (code) -> cross_check (code) -> emit
                                          ^                                    |
                                          +---------------- retry -------------+   (INTAKE_MAX_RETRIES, default 1;
                                                                                     error messages fed back to the model)
```

| File | Role |
|---|---|
| `schema.py` | Versioned contract: fields (value, evidence, confidence, page), line items, flags, cross-document flags |
| `pages.py` | Deterministic page rendering: EXIF rotate, 2000px cap, 20-page cap |
| `nodes.py` | Pipeline nodes, per-document validation, bill arithmetic, retry router |
| `crosscheck.py` | Cross-document consistency rules |
| `extractors.py` | `MultimodalExtractor` (production) and `OfflineTestExtractor` (tests only) |
| `graph.py` | LangGraph wiring, `run_intake()` entry point (CLI now, A2A server later) |
| `run_demo.py`, `score.py` | Terminal demo; scorer over `test_docs/` |
| `make_test_docs.py`, `test_logic.py` | Synthetic test-data generator; offline rule tests (no API key needed) |

## What is checked, and by whom
Agent 1 reports inconsistencies. It does NOT decide eligibility, sum insured or payable amount: that is Agent 2.

| Check | Severity | Notes |
|---|---|---|
| Required fields missing, bad dates/amounts/policy format, future dates, discharge < admission | error | per document |
| Line items: qty x rate != amount | warning -> review | line-level tax can cause this; tune per insurer |
| Sum of line items != printed sub-total | error | sum computed in code |
| total != sub-total - discount + tax (Rs 1 tolerance) | error | discount/tax read from the bill |
| Claim amount > total of uploaded bills | error | message notes a bill may be missing |
| Claim amount < billed (partial claim) | **info only** | NOT an error; both numbers passed to Agent 2 |
| Patient name differs across documents | error | tolerant of word order, titles, small typos |
| Names match only via an initial (`A. Sharma` vs `Ajay Sharma`) | warning (review) | verify identity |
| Admission/discharge date or policy number differs across documents | error | |
| Hospital name differs | warning -> review | abbreviations are common |
| Same bill uploaded twice | warning -> review | counted once |
| File seems to bundle several documents | warning -> review | splitting not built yet |

Severities: `error` and the listed warnings set `needs_human_review`; `info` never does.

## Run
```bash
pip install -r requirements.txt            # + poppler-utils for PDFs
python test_logic.py                       # offline rule tests, no API key
# model (any vision-capable provider):
#   $env:GOOGLE_API_KEY="..."; $env:INTAKE_MODEL="google_genai:gemini-2.5-flash"     (free tier)
#   $env:ANTHROPIC_API_KEY="..."; $env:INTAKE_MODEL="anthropic:claude-sonnet-5-5"
python score.py                            # all 16 cases (use e.g. `python score.py 11 12` for some)
python run_demo.py claim_form.png bill.pdf --text "note"
```
Free tiers are rate-limited: each case makes 2+ model calls per document (more on retry). Run a few cases at a time.

## Known gaps / next steps
1. Bundled PDFs are detected and flagged, not split.
2. `needs_human_review` is only a flag. Add a checkpointer + `interrupt()` for pause/correct/resume.
3. A very long bill (100+ rows) may exceed the model's output length; flagged by a sum mismatch, not silently wrong.
4. Bills that deduct an advance/deposit in the 'Total' line will look like TOTAL_MISMATCH: adjust hints/rules for your hospitals.
5. Files go to local `./store`; swap for the shared Server and pass `file_id` references.
6. Wrap `run_intake` in an A2A server (agent card + task status).


Retry: only per-document errors trigger a re-extraction, and only for the documents that have them. Cross-document errors go straight to human review.
