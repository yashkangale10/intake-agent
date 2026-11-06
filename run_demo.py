"""Terminal demo: Terminal 1 (input) -> Agent 1 -> structured JSON on stdout.

    python run_demo.py bill.pdf discharge.png --text "Claim for knee surgery"      # real LLM (needs API key)
    python run_demo.py bill.png --offline                                           # test double (tesseract+regex), no LLM
"""
import argparse
import json
import sys

from graph import HAVE_LANGGRAPH, run_intake


def main() -> None:
    ap = argparse.ArgumentParser(description="Health-insurance Document Intake Agent (Agent 1)")
    ap.add_argument("files", nargs="+", help="image/PDF files belonging to one claim")
    ap.add_argument("--text", default="", help="free-text note from the user")
    ap.add_argument("--offline", action="store_true", help="use the offline test double instead of a multimodal LLM")
    ap.add_argument("--model", default=None, help="vision-capable model, e.g. anthropic:claude-sonnet-5-5 (default from INTAKE_MODEL)")
    args = ap.parse_args()

    if not HAVE_LANGGRAPH:
        print("[warn] langgraph not installed - running the sequential dev fallback", file=sys.stderr)

    if args.offline:
        from extractors import OfflineTestExtractor
        extractor = OfflineTestExtractor()
    else:
        from extractors import MultimodalExtractor
        extractor = MultimodalExtractor(args.model)

    print(json.dumps(run_intake(extractor, args.files, args.text), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
