import hashlib
import logging
from pathlib import Path

from django.core.files import File
from django.core.management import call_command
from django.db.models import Q
from django.utils import timezone

from api.models import PurchaseQuoteDownload, BulkImportBatch, BulkImportFile, BulkImportFileStatus
from api.services.bulk_import_service import BulkImportService

logger = logging.getLogger(__name__)


class PurchaseAutoExtractionService:
    """Download, deduplicate and queue Purchase Software files for extraction."""

    @staticmethod
    def reconcile_completed_zips():
        """Recover legacy manual ZIP uploads using every member's content hash."""
        from api.services.extraction.archive_extractor import extract_archive

        completed_hashes = set(BulkImportFile.objects.filter(
            status__in=[BulkImportFileStatus.IMPORTED, BulkImportFileStatus.REVIEW_REQUIRED],
        ).values_list("file_hash", flat=True))
        for source in PurchaseQuoteDownload.objects.filter(
            extraction_status="downloaded", file_extension__iexact=".zip",
        ):
            try:
                raw = Path(source.local_path).read_bytes()
                members, skipped = extract_archive(raw, source.file_name)
                if skipped or not members:
                    continue
                if not all(hashlib.sha256(data).hexdigest() in completed_hashes for _, data in members):
                    continue
                source.file_hash = hashlib.sha256(raw).hexdigest()
                source.extraction_status = "extracted"
                source.extraction_completed_at = timezone.now()
                source.error_message = None
                source.save(update_fields=["file_hash", "extraction_status", "extraction_completed_at", "error_message", "updated_at"])
            except OSError:
                logger.warning("Cannot reconcile downloaded archive id=%s", source.id)

    def start(self, *, limit=None, all_files=False, user=None):
        # The existing downloader remains the single source of truth for
        # authentication, date ordering and Purchase Software pagination.
        # Use the downloader's persisted date cursor after the first scan.
        # This prevents every automatic click from walking all historical
        # Purchase Software pages again. Existing downloaded/failed records
        # remain eligible below, so extraction is not lost when a file was
        # downloaded earlier but has not yet completed.
        call_command("sync_purchase_quotes_web", limit=0, new_only=True)
        self.reconcile_completed_zips()

        # Recover records left as queued by an earlier duplicate-only batch.
        # Such batches have no active extraction job, so those files must be
        # visible again for the next requested extraction window.
        stuck = PurchaseQuoteDownload.objects.filter(extraction_status="queued")
        for source in stuck:
            batch = (
                BulkImportBatch.objects.filter(id=source.extraction_batch_id).first()
                if source.extraction_batch_id
                else None
            )
            if not batch:
                source.extraction_status = "downloaded"
                source.extraction_batch_id = None
                source.error_message = None
                source.save(update_fields=["extraction_status", "extraction_batch_id", "error_message", "updated_at"])
            elif batch.status in {"completed", "completed_with_errors", "failed"}:
                # A completed batch must never be reopened. This is especially
                # important for ZIP/RAR parents: their jobs are named after
                # archive members, so the parent cannot be completed by the
                # normal filename-based worker callback.
                source.extraction_status = (
                    "failed" if batch and batch.status == "failed" else "extracted"
                )
                source.extraction_batch_id = source.extraction_batch_id if batch else None
                source.error_message = (
                    "Archive extraction failed."
                    if source.extraction_status == "failed"
                    else None
                )
                source.extraction_completed_at = timezone.now()
                source.save(update_fields=["extraction_status", "extraction_batch_id", "error_message", "extraction_completed_at", "updated_at"])

        candidates = list(
            PurchaseQuoteDownload.objects
            .filter(
                Q(extraction_status__in=["downloaded", "failed"])
                | Q(extraction_status="queued", extraction_batch_id__isnull=True)
            )
            .order_by("quote_date", "downloaded_at", "id")
        )
        selected = candidates if all_files else candidates[: max(int(limit or 0), 0) or 20]
        if not selected:
            return {"status": "completed", "message": "No unprocessed Purchase Software files found.", "files": []}

        unique = []
        seen_hashes = set(
            PurchaseQuoteDownload.objects
            .exclude(file_hash__isnull=True)
            .exclude(extraction_status__in=["downloaded", "failed"])
            .values_list("file_hash", flat=True)
        )
        duplicate_ids = []
        duplicate_names = []
        duplicate_hashes = set()
        opened = []
        try:
            for source in selected:
                path = Path(source.local_path)
                if not path.exists() or path.stat().st_size == 0:
                    source.extraction_status = "failed"
                    source.error_message = "Downloaded file is missing or empty."
                    source.save(update_fields=["extraction_status", "error_message", "updated_at"])
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                source.file_hash = digest
                duplicate = digest in seen_hashes or any(item.file_hash == digest for item in unique)
                if duplicate:
                    source.extraction_status = "duplicate"
                    source.error_message = "Duplicate content; extraction skipped."
                    source.save(update_fields=["file_hash", "extraction_status", "error_message", "updated_at"])
                    duplicate_ids.append(source.id)
                    if digest not in duplicate_hashes:
                        duplicate_names.append(source.file_name)
                        duplicate_hashes.add(digest)
                    continue
                source.extraction_status = "queued"
                source.error_message = None
                source.save(update_fields=["file_hash", "extraction_status", "error_message", "updated_at"])
                handle = open(path, "rb")
                opened.append(handle)
                unique.append(source)

            if not unique:
                return {
                    "status": "completed",
                    "message": f"No new files extracted. {len(duplicate_names)} unique duplicate file(s) skipped.",
                    "total_files": 0,
                    "queued_files": 0,
                    "duplicate_files": len(duplicate_names),
                    "duplicate_source_files": duplicate_names,
                    "files": [],
                }

            files = [File(handle, name=source.file_name) for handle, source in zip(opened, unique)]
            result = BulkImportService().create_bulk_batch(files=files, category="auto", user=user)
            rejected_names = {
                str(entry.get("file_name") or entry.get("name") or "").strip()
                for entry in (result.get("rejected_files") or [])
                if isinstance(entry, dict)
            }
            # A duplicate upload is rejected before a BulkImportFile job is
            # created. Reset the source row from queued so it cannot become a
            # permanently invisible candidate on the next automatic run.
            for source in unique:
                if source.file_name in rejected_names:
                    source.extraction_status = "duplicate"
                    source.error_message = "Duplicate content/file; extraction skipped."
                    source.save(update_fields=["extraction_status", "error_message", "updated_at"])
            for source in unique:
                if source.file_name not in rejected_names and result.get("bulk_batch_id"):
                    source.extraction_batch_id = result["bulk_batch_id"]
                    source.save(update_fields=["extraction_batch_id", "updated_at"])
            return {**result, "duplicate_source_files": duplicate_names, "duplicate_source_ids": duplicate_ids}
        except Exception as exc:
            logger.exception("Automatic Purchase Software extraction queue failed")
            for source in unique:
                source.extraction_status = "failed"
                source.error_message = str(exc)
                source.save(update_fields=["extraction_status", "error_message", "updated_at"])
            raise
        finally:
            for handle in opened:
                handle.close()

    def status(self, batch_id):
        result = BulkImportService().status(BulkImportBatch.objects.get(id=batch_id))
        jobs = BulkImportFile.objects.filter(bulk_batch_id=batch_id)
        for job in jobs:
            source = PurchaseQuoteDownload.objects.filter(file_name=job.original_file_name).first()
            if not source:
                continue
            if job.status == BulkImportFileStatus.FAILED:
                source.extraction_status = "failed"
                source.error_message = job.progress_message or "Extraction failed."
                source.extraction_completed_at = None
            elif job.status in {BulkImportFileStatus.IMPORTED, BulkImportFileStatus.REVIEW_REQUIRED}:
                source.extraction_status = "extracted"
                source.error_message = job.progress_message if job.status == BulkImportFileStatus.REVIEW_REQUIRED else None
                source.extraction_completed_at = timezone.now()
            source.save(update_fields=["extraction_status", "error_message", "extraction_completed_at", "updated_at"])

        # Archive uploads create child BulkImportFile jobs for the files inside
        # the ZIP/RAR. There is no child job whose name equals the archive
        # filename, so the normal filename sync above cannot close the parent
        # PurchaseQuoteDownload row. Finalise those parent rows from the batch
        # terminal state; otherwise every next extraction click sees the same
        # archive as queued and re-displays its child duplicates.
        batch = BulkImportBatch.objects.get(id=batch_id)
        if batch.status in {"completed", "completed_with_errors", "failed"}:
            parent_status = "failed" if batch.status == "failed" else "extracted"
            parent_message = (
                "Archive extraction failed."
                if parent_status == "failed"
                else "Archive extracted; child files processed."
            )
            PurchaseQuoteDownload.objects.filter(
                extraction_batch_id=batch_id,
                extraction_status="queued",
            ).update(
                extraction_status=parent_status,
                error_message=parent_message if parent_status == "failed" else None,
                extraction_completed_at=timezone.now(),
                updated_at=timezone.now(),
            )
        return result
