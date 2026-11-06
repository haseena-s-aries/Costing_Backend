"""Offline replay of saved extraction output; never calls Gemini or edits goldens.

Run from the project root with the project virtualenv Python.
Outputs a separate timestamped directory, preserving the original actual files.
This checks post-processing, NOT the live model/page-extraction path.
"""
import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "costingProject.settings")
os.environ["RUN_MAIN"] = "false"

import django
django.setup()

from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet
from api.services.source_reconciliation import finalize_pdf, finalize_msg_table, required_metadata
from api.services.regression_testing import compare_results, json_default


def main():
    fixtures = ROOT / "test_fixtures"
    output = fixtures / "results" / "source_reconciliation" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True)
    inputs = {p.stem: p for p in (fixtures / "inputs").iterdir() if p.is_file()}
    reports = []
    for saved in sorted((fixtures / "results" / "extraction").glob("*.actual.json")):
        stem = saved.name.removesuffix(".actual.json")
        path = inputs.get(stem)
        if not path:
            continue
        original = json.loads(saved.read_text(encoding="utf-8"))
        result = deepcopy(original)
        if path.suffix.lower() == ".pdf":
            result = finalize_pdf(result, path.read_bytes())
        elif path.suffix.lower() == ".msg":
            import extract_msg
            with extract_msg.Message(str(path)) as msg:
                result = finalize_msg_table(result, msg.body or "", str(msg.date))
        result = required_metadata(normalize_result(result))
        sheet = gemini_result_to_virtual_sheet(deepcopy(result))
        expected_path = fixtures / "expected" / "extraction" / (stem + ".json")
        expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else None
        before = compare_results(original, expected) if expected is not None else None
        after = compare_results(result, expected) if expected is not None else None
        report = dict(filename=path.name, before_count=len(original.get("items") or []),
                      after_count=len(result.get("items") or []), virtual_count=len(sheet["rows"])-1,
                      baseline_passed=before["passed"] if before else None,
                      passed=after["passed"] if after else None,
                      differences=after["differences"] if after else [{"kind": "missing_expected"}],
                      review_reasons=result.get("_review_reasons"), mode="saved-output replay; no Gemini calls")
        reports.append(report)
        (output / saved.name).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
        print(f"{path.name}: {report['before_count']} -> {report['after_count']}; golden={report['passed']}; baseline={report['baseline_passed']}")
    (output / "report.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
    print(f"Report: {output / 'report.json'}")


if __name__ == "__main__":
    main()
