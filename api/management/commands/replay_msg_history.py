"""Offline source replay; does not call Gemini or replace live-run artifacts."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import extract_msg
from django.core.management.base import BaseCommand, CommandError

from api.services.msg_commercial_history import extract_history
from api.services.gemini_extraction_service import (
    normalize_result, _reconcile_placeholder_commercial_items,
    gemini_result_to_virtual_sheet, _extraction_quality_issues,
)
from api.services.regression_testing import compare_results


class Command(BaseCommand):
    help = "Replay selected MSG source histories offline against expected JSON (zero Gemini calls)."

    def add_arguments(self, parser):
        parser.add_argument("--file", action="append", required=True)
        parser.add_argument("--fixtures", default="test_fixtures")

    def handle(self, *args, **options):
        root = Path(options["fixtures"])
        output = root / "results" / "msg_history_replay" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        output.mkdir(parents=True, exist_ok=False)
        reports = []
        for name in options["file"]:
            if Path(name).name != name or Path(name).suffix.lower() != ".msg":
                raise CommandError("--file must be an MSG filename within inputs")
            path = root / "inputs" / name
            report = {"filename": name, "mode": "offline_source_replay", "gemini_calls": 0}
            try:
                message = extract_msg.Message(str(path))
                try:
                    result = extract_history(message.body, message.sender, str(message.date))
                finally:
                    message.close()
                if result is None:
                    report.update(status="NEEDS_LIVE_EXTRACTION", reason="Source grammar not fully supported; nothing replaced")
                else:
                    result = normalize_result(_reconcile_placeholder_commercial_items(normalize_result(result), name))
                    expected_path = root / "expected" / "extraction" / f"{path.stem}.json"
                    expected = json.loads(expected_path.read_text(encoding="utf-8"))
                    comparison = compare_results(result, expected)
                    issues = _extraction_quality_issues(result)
                    sheet = gemini_result_to_virtual_sheet(deepcopy(result))
                    accepted = len(sheet["rows"]) - 1
                    passed = comparison["passed"] and not issues and accepted == len(result["items"])
                    report.update(status="PASS" if passed else "FAIL", item_count=len(result["items"]),
                                  accepted_item_count=accepted, raw_extracted_count=result["_raw_extracted_count"],
                                  differences=comparison["differences"], quality_issues=issues,
                                  normalization_warnings=sheet["gemini_validation_warnings"])
                    (output / f"{path.stem}.actual.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                report.update(status="ERROR", error=str(exc))
            reports.append(report)
            self.stdout.write(f"{report['status']} {name}")
        report_path = output / "report.json"
        report_path.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(f"Offline report: {report_path}")
        passed = sum(r["status"] == "PASS" for r in reports)
        self.stdout.write(f"Summary: {passed}/{len(reports)} passed; Gemini calls: 0")
        if passed != len(reports):
            raise CommandError("Offline replay has failures or unsupported source blocks")
