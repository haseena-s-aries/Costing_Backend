from pathlib import Path
import hashlib
import csv
from django.core.management.base import BaseCommand
from api.management.commands.run_extraction_regression import FileShim
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.regression_testing import json_default
import json, time

class Command(BaseCommand):
    help = "Run production extraction over a directory without expected fixtures."
    def add_arguments(self, parser):
        parser.add_argument("--input-dir", required=True)
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--recursive", action="store_true")
        parser.add_argument("--overwrite", action="store_true", help="Re-extract files that already have a result JSON")
    def handle(self, *args, **opts):
        source, target = Path(opts["input_dir"]), Path(opts["output_dir"]); target.mkdir(parents=True, exist_ok=True)
        report_rows = []
        manifest_path = target / "_content_hashes.json"
        try:
            content_hashes = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        except Exception:
            content_hashes = {}
        files = source.rglob("*") if opts["recursive"] else source.glob("*")
        for path in files:
            if not path.is_file(): continue
            output_path = target / f"{path.stem}.json"
            if output_path.exists() and not opts["overwrite"]:
                try:
                    existing = json.loads(output_path.read_text(encoding="utf-8"))
                    duplicate = bool(existing.get("_duplicate_content"))
                    review_required = bool(existing.get("_gemini_needs_review") or existing.get("_needs_review"))
                    quality_issues = existing.get("_gemini_quality_issues") or []
                    report_rows.append({"filename": path.name, "status": "DUPLICATE" if duplicate else ("REVIEW" if review_required or quality_issues else "PASS"), "review_required": review_required, "quality_issues": " | ".join(map(str, quality_issues)), "error": "", "result_file": output_path.name})
                except Exception as exc:
                    report_rows.append({"filename": path.name, "status": "FAILED", "review_required": False, "quality_issues": "", "error": f"Unreadable result: {exc}", "result_file": output_path.name})
                self.stdout.write(f"SKIP {path.name} (result already exists)")
                continue
            started=time.perf_counter()
            try:
                file_bytes = path.read_bytes()
                content_hash = hashlib.sha256(file_bytes).hexdigest()
                duplicate_of = content_hashes.get(content_hash)
                if duplicate_of and not opts["overwrite"]:
                    output_path.write_text(json.dumps({
                        "_duplicate_content": True,
                        "_duplicate_of": duplicate_of,
                        "_source_file_name": path.name,
                        "_source_sha256": content_hash,
                    }, indent=2), encoding="utf-8")
                    report_rows.append({"filename": path.name, "status": "DUPLICATE", "review_required": False, "quality_issues": "", "error": "", "result_file": output_path.name})
                    self.stdout.write(f"DUPLICATE {path.name} (same content as {duplicate_of})")
                    continue
                result=ExtractionOrchestrator().extract(FileShim(file_bytes), path.name, None)
                result["_batch_duration_ms"] = round((time.perf_counter()-started)*1000)
                result["_source_file_name"] = path.name
                result["_source_sha256"] = content_hash
                output_path.write_text(json.dumps(result, indent=2, default=json_default), encoding="utf-8")
                content_hashes[content_hash] = path.name
                manifest_path.write_text(json.dumps(content_hashes, indent=2), encoding="utf-8")
                review_required = bool(result.get("_gemini_needs_review") or result.get("_needs_review"))
                quality_issues = result.get("_gemini_quality_issues") or []
                status = "REVIEW" if review_required or quality_issues else "PASS"
                report_rows.append({"filename": path.name, "status": status, "review_required": review_required, "quality_issues": " | ".join(map(str, quality_issues)), "error": "", "result_file": output_path.name})
                self.stdout.write(f"{status} {path.name}")
            except Exception as exc:
                error_path = target/f"{path.stem}.error.json"
                error_path.write_text(json.dumps({"error":str(exc)}, indent=2), encoding="utf-8")
                report_rows.append({"filename": path.name, "status": "FAILED", "review_required": False, "quality_issues": "", "error": str(exc), "result_file": error_path.name})
                self.stderr.write(f"ERROR {path.name}: {exc}")

        report_json = target / "latest_report.json"
        report_csv = target / "latest_report.csv"
        report_json.write_text(json.dumps(report_rows, indent=2), encoding="utf-8")
        with report_csv.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["filename", "status", "review_required", "quality_issues", "error", "result_file"])
            writer.writeheader()
            writer.writerows(report_rows)
        self.stdout.write(f"Report JSON: {report_json}")
        self.stdout.write(f"Report CSV: {report_csv}")
