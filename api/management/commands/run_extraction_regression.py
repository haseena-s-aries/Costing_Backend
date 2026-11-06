import json
import time
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.regression_testing import compare_results, json_default


class FileShim:
    def __init__(self, data): self.data, self.pos = data, 0
    def seek(self, pos): self.pos = pos
    def read(self): return self.data
    def open(self, mode="rb"): self.pos = 0


class Command(BaseCommand):
    help = "Run production extraction against JSON golden fixtures."

    def add_arguments(self, parser):
        parser.add_argument("--file", action="append")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--fail-fast", action="store_true")
        parser.add_argument("--update-results", action="store_true")
        parser.add_argument("--fixtures", default="test_fixtures")

    def handle(self, *args, **options):
        root = Path(options["fixtures"])
        inputs, expected_dir = root / "inputs", root / "expected" / "extraction"
        results_dir, reports_dir = root / "results" / "extraction", root / "reports"
        results_dir.mkdir(parents=True, exist_ok=True); reports_dir.mkdir(parents=True, exist_ok=True)
        selected = set(options.get("file") or [])
        # Keep the default inputs folder behavior, but allow a selected fixture
        # to live in any fixture subfolder (for example new_100_input) without
        # requiring callers to copy it into inputs.
        files = [path for path in inputs.iterdir() if path.is_file()]
        if selected:
            fixture_files = [path for path in root.rglob("*") if path.is_file()]
            files = [path for path in fixture_files if path.name in selected or str(path.relative_to(root)) in selected]
        if options.get("limit"): files = files[:options["limit"]]
        if not files: raise CommandError(f"No input files found in {inputs}")
        reports, passed = [], 0
        for path in files:
            started = time.perf_counter()
            try:
                result = ExtractionOrchestrator().extract(FileShim(path.read_bytes()), path.name, None)
                output = results_dir / f"{path.stem}.actual.json"
                output.write_text(json.dumps(result, indent=2, default=json_default), encoding="utf-8")
                expected_path = expected_dir / f"{path.stem}.json"
                comparison = compare_results(result, json.loads(expected_path.read_text(encoding="utf-8"))) if expected_path.exists() else {"passed": False, "differences": [{"kind": "missing_expected"}]}
                passed += int(comparison["passed"])
                reports.append({"filename": path.name, "status": "PASS" if comparison["passed"] else "FAIL", "item_count": len(result.get("items") or []), "review_required": result.get("_gemini_needs_review"), "duration_ms": round((time.perf_counter()-started)*1000), "differences": comparison["differences"]})
                self.stdout.write(f"{'PASS' if comparison['passed'] else 'FAIL'} {path.name}")
                if not comparison["passed"] and options["fail_fast"]: break
            except Exception as exc:
                reports.append({"filename": path.name, "status": "ERROR", "error": str(exc), "duration_ms": round((time.perf_counter()-started)*1000)})
                self.stderr.write(f"ERROR {path.name}: {exc}")
                if options["fail_fast"]: break
        (reports_dir / "latest_report.json").write_text(json.dumps(reports, indent=2, default=json_default), encoding="utf-8")
        self.stdout.write(f"Summary: {passed}/{len(reports)} passed")
        if passed != len(reports): raise CommandError("Regression failures detected")
