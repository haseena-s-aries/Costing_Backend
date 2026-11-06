import json
from pathlib import Path
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from api.models import ImportBatch, ImportCategory, UploadedFile
from api.services.import_service import ImportService
from api.management.commands.run_extraction_regression import FileShim
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.regression_testing import compare_results, json_default


class Command(BaseCommand):
    help = "Run the production ImportService flow in a rollback-only transaction."
    def add_arguments(self, parser):
        parser.add_argument("--file", action="append", required=True)
        parser.add_argument("--fixtures", default="test_fixtures")

    def handle(self, *args, **opts):
        root = Path(opts["fixtures"]); expected_dir = root / "expected" / "import"
        for filename in opts["file"]:
            source = root / "inputs" / filename
            if not source.exists(): raise CommandError(f"Missing input: {source}")
            uploaded = None
            try:
                with transaction.atomic():
                    uploaded = UploadedFile.objects.create(
                        original_file_name=source.name,
                        mime_type="application/pdf" if source.suffix.lower() == ".pdf" else "application/octet-stream",
                        file=ContentFile(source.read_bytes(), name=source.name),
                    )
                    batch = ImportBatch.objects.create(
                        uploaded_file=uploaded,
                        category=ImportCategory.OTHER,
                        default_currency="AED",
                    )
                    preview = ImportService().preview(batch, [], limit=10000)
                    sheet_ids = list(batch.sheets.values_list("id", flat=True))
                    ImportService().start(batch, sheet_ids)
                    rows = [ImportService()._preview_row(row) for row in batch.rows.all()]
                    actual = {"items": [row.get("mapped_data") or row.get("normalized_data") or {} for row in rows], "review_status": batch.status}
                    expected_path = expected_dir / f"{source.stem}.json"
                    if expected_path.exists():
                        result = compare_results(actual, json.loads(expected_path.read_text(encoding="utf-8")))
                        if not result["passed"]:
                            raise CommandError(f"Import regression failed for {filename}: {result['differences']}")
                    self.stdout.write(f"PASS {filename} rows={len(actual['items'])} status={batch.status}")
                    transaction.set_rollback(True)
            finally:
                if uploaded and uploaded.file:
                    try: uploaded.file.delete(save=False)
                    except Exception: pass
