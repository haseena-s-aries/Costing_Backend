import hashlib
import io
import logging
import os
import zipfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from api.constants import MAX_UPLOAD_SIZE, SUPPORTED_EXTENSIONS
from api.exceptions import ImportServiceError
from api.models import (
    BulkImportBatch,
    BulkImportFile,
    BulkImportFileStatus,
    BulkImportStatus,
    UploadedFile,
    PurchaseQuoteDownload,
)
from api.services.extraction.archive_extractor import extract_archive


logger = logging.getLogger(__name__)


class BulkImportService:
    MAX_FILES = 500
    MAX_EXTRACTED_SIZE = 1024 * 1024 * 500  # 500 MB

    def create_bulk_batch(
        self,
        *,
        files,
        category="auto",
        supplier_name="",
        default_currency="",
        remarks="",
        user=None,
    ):
        if not files:
            raise ImportServiceError(
                "No files uploaded.",
                {"files": ["Select one or more files or a ZIP file."]},
            )

        bulk_batch = BulkImportBatch.objects.create(
            category=category or "auto",
            supplier_name=(supplier_name or "").strip(),
            default_currency=(default_currency or "").strip(),
            remarks=remarks,
            uploaded_by=user,
            status=BulkImportStatus.PENDING,
        )

        queued = []
        rejected = []

        try:
            for uploaded in files:
                suffix = Path(uploaded.name).suffix.lower()

                if suffix in {".zip", ".rar"}:
                    archive_results = self._extract_archive(
                        bulk_batch,
                        uploaded,
                    )
                    queued.extend(archive_results["queued"])
                    rejected.extend(archive_results["rejected"])
                else:
                    result = self._queue_file(
                        bulk_batch,
                        uploaded.name,
                        uploaded.read(),
                    )

                    if result:
                        if result.status == BulkImportFileStatus.DUPLICATE:
                            rejected.append(
                                {
                                    "file_name": result.original_file_name,
                                    "reason": result.progress_message or "Duplicate file",
                                }
                            )
                        else:
                            queued.append(result)

            if not queued:
                duplicate_count = bulk_batch.files.filter(
                    status=BulkImportFileStatus.DUPLICATE
                ).count()

                if duplicate_count:
                    bulk_batch.status = BulkImportStatus.COMPLETED
                    bulk_batch.total_files = bulk_batch.files.count()
                    bulk_batch.duplicate_files = duplicate_count
                    bulk_batch.processed_files = duplicate_count

                    bulk_batch.save(
                        update_fields=[
                            "status",
                            "total_files",
                            "duplicate_files",
                            "processed_files",
                            "updated_at",
                        ]
                    )

                    return {
                        "bulk_batch_id": bulk_batch.id,
                        "batch_number": bulk_batch.batch_number,
                        "status": bulk_batch.status,
                        "total_files": bulk_batch.total_files,
                        "queued_files": 0,
                        "rejected_files": rejected,
                    }

                bulk_batch.status = BulkImportStatus.FAILED
                bulk_batch.total_files = 0

                bulk_batch.save(
                    update_fields=[
                        "status",
                        "total_files",
                        "updated_at",
                    ]
                )

                raise ImportServiceError(
                    "No supported files found.",
                    {
                        "files": [
                            (
                                rejected[0].get("reason")
                                if rejected and isinstance(rejected[0], dict)
                                else "The upload did not contain any supported files."
                            )
                        ]
                    },
                )

            bulk_batch.total_files = bulk_batch.files.count()
            bulk_batch.original_upload_name = (
                files[0].name if len(files) == 1 else f"{len(files)} uploaded files"
            )
            bulk_batch.save(
                update_fields=[
                    "total_files",
                    "original_upload_name",
                    "updated_at",
                ]
            )

            return {
                "bulk_batch_id": bulk_batch.id,
                "batch_number": bulk_batch.batch_number,
                "status": bulk_batch.status,
                "total_files": bulk_batch.total_files,
                "queued_files": len(queued),
                "rejected_files": rejected,
            }

        except Exception:
            if bulk_batch.total_files == 0:
                bulk_batch.status = BulkImportStatus.FAILED
                bulk_batch.save(
                    update_fields=["status", "updated_at"]
                )
            raise

    def _extract_zip(self, bulk_batch, uploaded):
        try:
            uploaded.seek(0)

            with zipfile.ZipFile(uploaded) as archive:
                infos = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                ]

                if len(infos) > self.MAX_FILES:
                    raise ImportServiceError(
                        "Too many files in ZIP.",
                        {
                            "files": [
                                f"Maximum {self.MAX_FILES} files are allowed."
                            ]
                        },
                    )

                total_size = sum(item.file_size for item in infos)

                if total_size > self.MAX_EXTRACTED_SIZE:
                    raise ImportServiceError(
                        "ZIP extracted size is too large.",
                        {
                            "files": [
                                "Maximum extracted size is 500 MB."
                            ]
                        },
                    )

                queued = []
                rejected = []

                for info in infos:
                    safe_name = self._safe_zip_name(info.filename)

                    if not safe_name:
                        rejected.append(
                            {
                                "file_name": info.filename,
                                "reason": "Unsafe ZIP path",
                            }
                        )
                        continue

                    suffix = Path(safe_name).suffix.lower()

                    if suffix not in SUPPORTED_EXTENSIONS:
                        rejected.append(
                            {
                                "file_name": safe_name,
                                "reason": "Unsupported file type",
                            }
                        )
                        continue

                    if info.file_size <= 0:
                        rejected.append(
                            {
                                "file_name": safe_name,
                                "reason": "Empty file",
                            }
                        )
                        continue

                    if info.file_size > MAX_UPLOAD_SIZE:
                        rejected.append(
                            {
                                "file_name": safe_name,
                                "reason": "File too large",
                            }
                        )
                        continue

                    data = archive.read(info)

                    job = self._queue_file(
                        bulk_batch,
                        safe_name,
                        data,
                    )

                    if job:
                        if job.status == BulkImportFileStatus.DUPLICATE:
                            rejected.append(
                                {
                                    "file_name": job.original_file_name,
                                    "reason": job.progress_message or "Duplicate file",
                                }
                            )
                        else:
                            queued.append(job)

                return {
                    "queued": queued,
                    "rejected": rejected,
                }

        except zipfile.BadZipFile as exc:
            raise ImportServiceError(
                "Invalid ZIP file.",
                {"files": ["The uploaded ZIP file is corrupted or invalid."]},
            ) from exc

    def _extract_archive(self, bulk_batch, uploaded):
        """Queue each supported archive member exactly once.

        The orchestrator uses the same secure reader for direct extraction;
        bulk upload keeps members as independent import files rather than
        creating a second flattened parent list.
        """
        try:
            uploaded.seek(0)
            raw = uploaded.read()
            members, rejected = extract_archive(raw, uploaded.name)
        except Exception as exc:
            logger.exception("Archive extraction failed file=%s", uploaded.name)
            raise ImportServiceError(
                "Could not extract archive.",
                {"files": [str(exc)]},
            ) from exc

        queued = []
        for member_name, data in members:
            job = self._queue_file(bulk_batch, member_name, data)
            if not job:
                continue
            if job.status == BulkImportFileStatus.DUPLICATE:
                rejected.append({
                    "file_name": job.original_file_name,
                    "reason": job.progress_message or "Duplicate file",
                })
            else:
                queued.append(job)
        # Manual uploads must also link the downloaded archive by CONTENT.
        # Child names cannot identify their containing ZIP, and filenames alone
        # are not sufficient evidence that two archives are the same.
        digest = hashlib.sha256(raw).hexdigest()
        for source in PurchaseQuoteDownload.objects.filter(file_extension__iexact=Path(uploaded.name).suffix):
            source_hash = source.file_hash
            if not source_hash:
                try:
                    source_hash = hashlib.sha256(Path(source.local_path).read_bytes()).hexdigest()
                except OSError:
                    continue
            if source_hash != digest:
                continue
            source.file_hash = digest
            source.extraction_batch_id = bulk_batch.id
            source.extraction_status = "queued" if queued else "extracted"
            if rejected and any("Duplicate" not in str(entry.get("reason", "")) for entry in rejected):
                source.extraction_status = "failed"
            source.error_message = "Some archive members could not be extracted." if source.extraction_status == "failed" else None
            source.save(update_fields=["file_hash", "extraction_batch_id", "extraction_status", "error_message", "updated_at"])
        return {"queued": queued, "rejected": rejected}

    def _safe_zip_name(self, value):
        normalized = str(value or "").replace("\\", "/")

        path = Path(normalized)

        if path.is_absolute():
            return None

        if ".." in path.parts:
            return None

        name = path.name.strip()

        return name or None

    def _queue_file(self, bulk_batch, file_name, data):
        suffix = Path(file_name).suffix.lower()

        if suffix not in SUPPORTED_EXTENSIONS:
            return None

        if not data:
            return None

        if len(data) > MAX_UPLOAD_SIZE:
            return None

        digest = hashlib.sha256(data).hexdigest()

        existing_job = (
            BulkImportFile.objects
            .filter(file_hash=digest)
            .exclude(
                status__in=[
                    BulkImportFileStatus.FAILED,
                    BulkImportFileStatus.DUPLICATE,
                ]
            )
            .order_by("-id")
            .first()
        )

        # Bulk uploads and single-file uploads share the same content hash.
        # Check both stores before creating a pending extraction job so a file
        # already imported through the normal upload flow is never extracted
        # again just because it came through bulk upload.
        existing_uploaded = (
            UploadedFile.objects
            # A file removed from Import History/Product Search is soft
            # deleted.  It must not continue blocking a later re-upload of
            # the same corrected source file.
            .filter(file_hash=digest, is_deleted=False)
            .order_by("-id")
            .first()
        )

        if existing_uploaded and not existing_job:
            existing_job = existing_uploaded

        if existing_job:
            duplicate_job = BulkImportFile(
                bulk_batch=bulk_batch,
                original_file_name=file_name,
                file_hash=digest,
                status=BulkImportFileStatus.DUPLICATE,
                progress=100,
                progress_message=(
                    "Duplicate file - same content already uploaded as "
                    f"{getattr(existing_job, 'original_file_name', file_name)}"
                ),
                completed_at=timezone.now(),
            )

            # Keep the uploaded file only if your FileField is mandatory.
            duplicate_job.file.save(
                file_name,
                ContentFile(data),
                save=False,
            )

            duplicate_job.save()

            logger.info(
                "Duplicate upload blocked "
                "new_file=%s existing_file=%s hash=%s",
                file_name,
                existing_job.original_file_name,
                digest,
            )

            return duplicate_job
        job = BulkImportFile(
            bulk_batch=bulk_batch,
            original_file_name=file_name,
            file_hash=digest,
            status=BulkImportFileStatus.PENDING,
            progress=0,
            progress_message="Waiting",
        )

        job.file.save(
            file_name,
            ContentFile(data),
            save=False,
        )

        job.save()

        return job

    def status(self, bulk_batch):
        jobs = bulk_batch.files.all()

        return {
            "bulk_batch_id": bulk_batch.id,
            "batch_number": bulk_batch.batch_number,
            "status": bulk_batch.status,

            "total": bulk_batch.total_files,
            "processed": bulk_batch.processed_files,
            "imported": bulk_batch.imported_files,
            "review": bulk_batch.review_files,
            "duplicate": bulk_batch.duplicate_files,
            "failed": bulk_batch.failed_files,

            "processing": jobs.filter(
                status=BulkImportFileStatus.PROCESSING
            ).count(),

            "pending": jobs.filter(
                status=BulkImportFileStatus.PENDING
            ).count(),

            "progress_percentage": (
                round(
                    (
                        bulk_batch.processed_files
                        / bulk_batch.total_files
                    )
                    * 100,
                    2,
                )
                if bulk_batch.total_files
                else 0
            ),

            "files": [
                {
                    "id": job.id,
                    "file_name": job.original_file_name,
                    "status": job.status,
                    "progress": job.progress,
                    "message": job.progress_message,
                    "review_reasons": job.review_reasons,
                    "error_message": job.error_message,
                    "import_batch_id": job.import_batch_id,
                }
                for job in jobs
            ],
        }
