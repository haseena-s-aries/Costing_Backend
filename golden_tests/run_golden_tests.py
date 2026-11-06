"""
Golden-set test runner.

Folder layout expected:

    golden_tests/
        inputs/
            invoice_001.pdf
            quotation_email_002.pdf
            spreadsheet_costing_003.xlsx
            msg_email_004.msg
            ...
        expected/
            invoice_001.json          <- same basename as input, .json
            quotation_email_002.json
            spreadsheet_costing_003.json
            msg_email_004.json
        results/                      <- auto-created, gitignore this
            <run_timestamp>/
                invoice_001.actual.json
                invoice_001.report.json
                _summary.json

HOW TO ADD A NEW GOLDEN FILE:
1. Drop the source file into inputs/
2. Run this script once with --dump-only to get a starting actual.json
3. Manually correct that JSON into expected/<name>.json (fix wrong values,
   remove hallucinated items, add missing items) — this becomes ground truth
4. Re-run normally; it should now score ~1.0 against itself as a sanity check

USAGE:
    # Score current pipeline against the whole golden set
    python run_golden_tests.py

    # Just extract + dump raw output for a new file, don't score yet
    python run_golden_tests.py --dump-only --only invoice_001.pdf

    # Re-run only files that previously failed
    python run_golden_tests.py --only invoice_001.pdf quotation_email_002.pdf

IMPORTANT: This must be run from inside your Django project environment
(needs GEMINI_API_KEY set and api.services.parser_service importable),
since it imports your real GeminiExtractionService — no mocking. The whole
point is testing the real pipeline end-to-end.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from scorer import score_result, summarize

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
INPUTS_DIR = BASE_DIR / "inputs"
EXPECTED_DIR = BASE_DIR / "expected"
RESULTS_DIR = BASE_DIR / "results"


class _FileFieldShim:
    """Wraps raw bytes so it behaves like a Django FileField/UploadedFile
    for GeminiExtractionService.extract_from_file (seek/read/open)."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self._opened = True

    def seek(self, pos):
        self._pos = pos

    def read(self):
        return self._data

    def open(self, mode="rb"):
        self._opened = True


def run_one(service, input_path: Path):
    with open(input_path, "rb") as f:
        raw = f.read()
    file_field = _FileFieldShim(raw)
    result = service.extract_from_file(file_field, input_path.name, import_batch=None)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump-only", action="store_true",
                         help="Just run extraction and save actual.json, skip scoring")
    parser.add_argument("--only", nargs="*", default=None,
                         help="Only run these input filenames (space separated)")
    parser.add_argument("--no-fail", action="store_true",
                        help="Report failures but return exit code 0")
    args = parser.parse_args()

    # Import here so --help works without Django configured.
    try:
        import django  # noqa
        # Adjust this if your settings module path differs.
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "costingProject.settings")
        django.setup()
    except Exception as exc:
        print(f"[warn] Django setup skipped/failed ({exc}). "
              f"If GeminiExtractionService needs Django models configured, "
              f"set DJANGO_SETTINGS_MODULE correctly before running.")

    from api.services.gemini_extraction_service import GeminiExtractionService  # adjust import path

    service = GeminiExtractionService()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    input_files = sorted(INPUTS_DIR.glob("*"))
    if args.only:
        input_files = [f for f in input_files if f.name in args.only]

    if not input_files:
        print(f"No input files found in {INPUTS_DIR}. Add files there first.")
        sys.exit(1)

    reports = {}

    for input_path in input_files:
        stem = input_path.stem
        print(f"--- Processing {input_path.name} ---")

        try:
            actual = run_one(service, input_path)
        except Exception as exc:
            print(f"  [ERROR] extraction failed: {exc}")
            reports[input_path.name] = {
                "passed": False, "overall_score": 0.0, "error": str(exc),
            }
            continue

        actual_out = run_dir / f"{stem}.actual.json"
        actual_out.write_text(json.dumps(actual, indent=2, default=str))

        if args.dump_only:
            print(f"  Dumped actual output -> {actual_out}")
            print(f"  Copy/edit this into expected/{stem}.json to create ground truth.")
            continue

        expected_path = EXPECTED_DIR / f"{stem}.json"
        if not expected_path.exists():
            print(f"  [SKIP] no expected/{stem}.json found. "
                  f"Run with --dump-only --only {input_path.name} to bootstrap one.")
            continue

        try:
            expected = json.loads(expected_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  [ERROR] expected/{stem}.json is not valid JSON: {exc}")
            reports[input_path.name] = {"passed": False, "overall_score": 0.0,
                                          "error": f"invalid expected JSON: {exc}"}
            continue

        if not isinstance(expected, dict):
            print(f"  [ERROR] expected/{stem}.json must be a JSON OBJECT ({{...}}), "
                  f"not a {type(expected).__name__} ([...]). Fix the file structure "
                  f"(wrap it as {{\"items\": [...], \"supplier_name\": ..., ...}}) and re-run.")
            reports[input_path.name] = {"passed": False, "overall_score": 0.0,
                                          "error": "expected JSON is not an object"}
            continue

        if not isinstance(actual, dict):
            print(f"  [ERROR] actual extraction for {input_path.name} was not a JSON object "
                  f"(got {type(actual).__name__}). Skipping scoring for this file.")
            reports[input_path.name] = {"passed": False, "overall_score": 0.0,
                                          "error": "actual result is not an object"}
            continue

        try:
            report = score_result(actual, expected)
        except Exception as exc:
            print(f"  [ERROR] scoring crashed for {input_path.name}: {exc}")
            reports[input_path.name] = {"passed": False, "overall_score": 0.0,
                                          "error": f"scoring exception: {exc}"}
            continue

        reports[input_path.name] = report

        report_out = run_dir / f"{stem}.report.json"
        report_out.write_text(json.dumps(report, indent=2, default=str))

        status = "PASS" if report["passed"] else "FAIL"
        print(f"  [{status}] score={report['overall_score']} "
              f"items: {report['correct_item_count']}/{report['expected_item_count']} correct, "
              f"{len(report['missing_items'])} missing, {len(report['extra_items'])} extra")

        # Surface WHY it failed right in the console, so you don't have to
        # open the report.json separately for every low-scoring file.
        if report.get("header_mismatches"):
            print(f"    header mismatches:")
            for m in report["header_mismatches"]:
                print(f"      {m['field']}: actual={m['actual']!r} expected={m['expected']!r}")
        if report.get("item_field_mismatches"):
            print(f"    item field mismatches (first 3 shown):")
            for m in report["item_field_mismatches"][:3]:
                print(f"      item '{m['product_name']}':")
                for issue in m["issues"]:
                    print(f"        {issue['field']}: actual={issue['actual']!r} expected={issue['expected']!r}")

    if not args.dump_only and reports:
        summary = summarize(reports)
        (run_dir / "_summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print("\n=== SUMMARY ===")
        print(json.dumps(summary, indent=2))
        print(f"\nFull reports saved in: {run_dir}")
        if not args.no_fail and summary["passed"] != summary["total_files"]:
            print("\nRegression gate FAILED. Fix the reported category or mark the sample for review; "
                  "do not silently update expected JSON from the actual output.")
            sys.exit(2)


if __name__ == "__main__":
    main()
