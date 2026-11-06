import logging
import time
from datetime import timedelta

from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.exceptions import ImportServiceError
from api.models import (
    BulkImportBatch,
    BulkImportFile,
    BulkImportFileStatus,
    BulkImportStatus,
    PurchaseQuoteDownload,
)
from api.services.bulk_review_service import BulkReviewService
from api.services.import_service import (
    ImportService,
    build_review_reasons,
    resolve_import_metadata,
)
from api.services.upload_service import UploadService
from django.core.files.uploadedfile import SimpleUploadedFile

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued bulk price-import files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sleep",
            type=float,
            default=2,
        )

    def handle(self, *args, **options):
        sleep_seconds = options["sleep"]

        self.stdout.write(
            self.style.SUCCESS(
                "Bulk import worker started."
            )
        )

        while True:
            try:
                job = self._claim_next_job()

                if not job:
                    time.sleep(sleep_seconds)
                    continue

                self._process_job(job)

            except KeyboardInterrupt:
                self.stdout.write(
                    self.style.WARNING(
                        "Bulk import worker stopped."
                    )
                )
                break

            except Exception:
                logger.exception(
                    "Unhandled bulk worker error"
                )
                time.sleep(sleep_seconds)

    def _claim_next_job(self):
        with transaction.atomic():
            # Recover files abandoned by a crashed/terminated worker. Without
            # this guard the UI can poll PROCESSING forever and the parent
            # batch can never become terminal.
            stale_before = timezone.now() - timedelta(minutes=30)
            stale_jobs = BulkImportFile.objects.filter(
                status=BulkImportFileStatus.PROCESSING,
                started_at__lt=stale_before,
            )
            for stale_job in stale_jobs:
                stale_job.status = BulkImportFileStatus.FAILED
                stale_job.progress = 100
                stale_job.progress_message = "Failed: worker timeout"
                stale_job.error_message = "Processing exceeded the worker timeout and was recovered."
                stale_job.completed_at = timezone.now()
                stale_job.save(update_fields=[
                    "status", "progress", "progress_message", "error_message",
                    "completed_at", "updated_at",
                ])
                logger.error(
                    "Recovered stale bulk file bulk_file_id=%s bulk_batch_id=%s started_at=%s",
                    stale_job.id, stale_job.bulk_batch_id, stale_job.started_at,
                )
                self._refresh_batch(stale_job.bulk_batch_id)

            job = (
                BulkImportFile.objects
                .select_for_update()
                .filter(
                    status=BulkImportFileStatus.PENDING
                )
                .order_by("created_at")
                .first()
            )

            if not job:
                return None

            job.status = BulkImportFileStatus.PROCESSING
            job.progress = 5
            job.progress_message = "Preparing file"
            job.started_at = timezone.now()

            job.save(
                update_fields=[
                    "status",
                    "progress",
                    "progress_message",
                    "started_at",
                    "updated_at",
                ]
            )

            BulkImportBatch.objects.filter(
                id=job.bulk_batch_id,
                status=BulkImportStatus.PENDING,
            ).update(
                status=BulkImportStatus.PROCESSING,
                started_at=timezone.now(),
            )

            return job

    def _process_job(self, job):
        previous_file_status = job.status
        bulk = job.bulk_batch

        try:
            self._progress(
                job,
                15,
                "Extracting document",
            )

            job.file.open("rb")

            try:
                file_bytes = job.file.read()

                processing_file = SimpleUploadedFile(
                    name=job.original_file_name,
                    content=file_bytes,
                    content_type=None,
                )

                result = UploadService().upload(
                    file_obj=processing_file,
                    category=bulk.category,
                    supplier=None,
                    supplier_name=bulk.supplier_name or "",
                    default_currency=bulk.default_currency,
                    remarks=bulk.remarks or "",
                    user=bulk.uploaded_by,
                )
            finally:
                job.file.close()

            batch_id = result["batch_id"]

            from api.models import ImportBatch

            batch = ImportBatch.objects.get(
                id=batch_id
            )

            job.import_batch = batch

            self._progress(
                job,
                45,
                "Preparing preview",
            )

            sheet_ids = list(
                batch.sheets.values_list(
                    "id",
                    flat=True,
                )
            )

            if not sheet_ids:
                self._review(
                    job,
                    ["No importable sheet found"],
                )
                return

            ImportService().preview(
                batch,
                sheet_ids,
                5000,
            )

            # Persist one final metadata truth after rows are created.
            final_metadata = resolve_import_metadata(
                batch
            )

            batch.document_metadata = {
                **(batch.document_metadata or {}),
                **{
                    key: value
                    for key, value in final_metadata.items()
                    if value not in (None, "")
                },
            }

            supplier_name = str(
                final_metadata.get("supplier_name")
                or ""
            ).strip()

            if supplier_name:
                batch.supplier_name_from_file = (
                    supplier_name
                )

            batch.save(
                update_fields=[
                    "document_metadata",
                    "supplier_name_from_file",
                    "updated_at",
                ]
            )

            self._progress(
                job,
                70,
                "Validating extracted data",
            )

            decision = BulkReviewService().evaluate(
                batch
            )

            final_reasons = build_review_reasons(
                batch
            )

            # Keep extraction/data-quality reasons,
            # but remove stale metadata-missing reasons.
            decision_reasons = [
                reason
                for reason in (
                    decision.get("reasons")
                    or []
                )
                if str(reason).strip() not in {
                    "Supplier name missing",
                    "Document date missing",
                    "Currency missing",
                    "Category missing",
                }
                and self._is_extraction_review_reason(reason)
            ]

            final_reasons.extend(
                reason
                for reason in decision_reasons
                if reason not in final_reasons
            )

            if final_reasons:
                self._review(
                    job,
                    final_reasons,
                )
                return

            self._progress(
                job,
                85,
                "Importing",
            )

            ImportService().start(
                batch,
                sheet_ids,
            )

            job.status = BulkImportFileStatus.IMPORTED
            job.progress = 100
            job.progress_message = "Imported"
            job.completed_at = timezone.now()
            job.review_reasons = []
            job.error_message = None

            job.save(
                update_fields=[
                    "status",
                    "progress",
                    "progress_message",
                    "completed_at",
                    "review_reasons",
                    "error_message",
                    "import_batch",
                    "updated_at",
                ]
            )

        except ImportServiceError as exc:
            message = str(
                getattr(exc, "message", exc)
            )

            if getattr(exc, "status_code", None) == 409:
                job.status = BulkImportFileStatus.DUPLICATE
                job.progress = 100
                job.progress_message = "Duplicate file"
                job.error_message = message
                job.completed_at = timezone.now()

                job.save(
                    update_fields=[
                        "status",
                        "progress",
                        "progress_message",
                        "error_message",
                        "completed_at",
                        "updated_at",
                    ]
                )

            else:
                self._fail(
                    job,
                    message,
                )

        except Exception as exc:
            logger.exception(
                "Bulk import failed job=%s file=%s",
                job.id,
                job.original_file_name,
            )

            self._fail(
                job,
                str(exc),
            )

        finally:
            self._refresh_batch(job.bulk_batch_id)
            job.refresh_from_db()
            source = PurchaseQuoteDownload.objects.filter(file_name=job.original_file_name).first()
            if source:
                status_map = {
                    BulkImportFileStatus.IMPORTED: ("extracted", None),
                    BulkImportFileStatus.REVIEW_REQUIRED: ("extracted", job.progress_message),
                    BulkImportFileStatus.FAILED: ("failed", job.error_message or job.progress_message),
                    BulkImportFileStatus.DUPLICATE: ("duplicate", job.error_message or job.progress_message),
                }
                extraction_status, error_message = status_map.get(job.status, (None, None))
                if extraction_status:
                    source.extraction_status = extraction_status
                    source.error_message = error_message
                    source.extraction_batch_id = job.bulk_batch_id
                    source.extraction_completed_at = timezone.now()
                    source.save(update_fields=[
                        "extraction_status", "error_message", "extraction_batch_id",
                        "extraction_completed_at", "updated_at",
                    ])
            batch = job.import_batch
            extraction = (batch.extraction_summary or {}) if batch else {}
            raw = extraction.get("gemini_raw_result") or {}
            logger.info(
                "Bulk file terminal status bulk_file_id=%s previous_file_status=%s final_file_status=%s raw_item_count=%s "
                "deduplicated_item_count=%s accepted_item_count=%s actual_dropped_count=%s normalization_actions=%s review_reasons=%s",
                job.id, previous_file_status, job.status,
                extraction.get("raw_extracted_count", len(raw.get("items") or [])),
                extraction.get("deduplicated_item_count", 0),
                extraction.get("accepted_item_count"),
                extraction.get("validation_rejected_count", 0),
                extraction.get("normalization_actions") or extraction.get("dropped_reasons") or [],
                job.review_reasons or [],
            )

    def _progress(self, job, progress, message):
        job.progress = progress
        job.progress_message = message

        job.save(
            update_fields=[
                "progress",
                "progress_message",
                "import_batch",
                "updated_at",
            ]
        )

    def _review(self, job, reasons):
        batch = job.import_batch
        extraction = (batch.extraction_summary or {}) if batch else {}
        raw = extraction.get("gemini_raw_result") or {}
        rows = list(batch.rows.all()) if batch else []
        logger.warning(
            "Bulk extraction review required bulk_file_id=%s import_batch_id=%s file_name=%s "
            "extraction_method=%s gemini_needs_review=%s raw_item_count=%s accepted_item_count=%s "
            "suspicious_count=%s validation_issues=%s product_match_statuses=%s review_reasons=%s",
            job.id, getattr(batch, "id", None), job.original_file_name,
            extraction.get("method") or raw.get("_extraction_method"),
            raw.get("_gemini_needs_review"), len(raw.get("items") or []),
            sum(max(sheet.total_rows or 0, 0) for sheet in batch.sheets.all()) if batch else 0,
            len(extraction.get("suspicious_items") or []),
            extraction.get("warnings") or extraction.get("validation_issues") or [],
            [getattr(row, "product_match_status", None) for row in rows], reasons,
        )
        job.status = BulkImportFileStatus.REVIEW_REQUIRED
        job.progress = 100
        job.progress_message = "Review required"
        job.review_reasons = reasons
        job.completed_at = timezone.now()

        job.save(
            update_fields=[
                "status", "progress", "progress_message", "review_reasons",
                "completed_at", "import_batch", "updated_at",
            ]
        )

    @staticmethod
    def _is_extraction_review_reason(reason):
        text = str(reason or "").lower()
        return any(token in text for token in (
            "extraction", "commercial rows", "dropped", "validation",
            "missing page", "failed validation", "supplier name missing",
            "document date missing", "currency missing", "critical",
            "handwritten", "scanned document", "source verification",
        ))
    def _fail(self, job, message):
        job.status = BulkImportFileStatus.FAILED
        job.progress = 100
        job.progress_message = "Failed"
        job.error_message = message
        job.completed_at = timezone.now()

        job.save(
            update_fields=[
                "status",
                "progress",
                "progress_message",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )

    def _refresh_batch(self, bulk_batch_id):
        bulk = BulkImportBatch.objects.get(
            id=bulk_batch_id
        )

        jobs = bulk.files.all()

        bulk.imported_files = jobs.filter(
            status=BulkImportFileStatus.IMPORTED
        ).count()

        bulk.review_files = jobs.filter(
            status=BulkImportFileStatus.REVIEW_REQUIRED
        ).count()

        bulk.duplicate_files = jobs.filter(
            status=BulkImportFileStatus.DUPLICATE
        ).count()

        bulk.failed_files = jobs.filter(
            status=BulkImportFileStatus.FAILED
        ).count()

        bulk.processed_files = (
            bulk.imported_files
            + bulk.review_files
            + bulk.duplicate_files
            + bulk.failed_files
        )

        if (
            bulk.total_files
            and bulk.processed_files >= bulk.total_files
        ):
            bulk.completed_at = timezone.now()

            if bulk.failed_files or bulk.review_files:
                bulk.status = (
                    BulkImportStatus.COMPLETED_WITH_ERRORS
                )
            else:
                bulk.status = BulkImportStatus.COMPLETED

        else:
            bulk.status = BulkImportStatus.PROCESSING

        bulk.save(
            update_fields=[
                "imported_files",
                "review_files",
                "duplicate_files",
                "failed_files",
                "processed_files",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
        logger.info(
            "Bulk batch finalized bulk_batch_id=%s total_files=%s processed_files=%s imported_files=%s "
            "review_files=%s duplicate_files=%s failed_files=%s terminal_file_count=%s final_batch_status=%s completed_at=%s",
            bulk.id, bulk.total_files, bulk.processed_files, bulk.imported_files,
            bulk.review_files, bulk.duplicate_files, bulk.failed_files,
            bulk.processed_files, bulk.status, bulk.completed_at,
        )
        if bulk.status in {BulkImportStatus.COMPLETED, BulkImportStatus.COMPLETED_WITH_ERRORS}:
            PurchaseQuoteDownload.objects.filter(
                extraction_batch_id=bulk.id, extraction_status="queued",
            ).update(
                extraction_status="failed" if bulk.failed_files else "extracted",
                error_message="Some files failed extraction." if bulk.failed_files else None,
                extraction_completed_at=timezone.now(), updated_at=timezone.now(),
            )
