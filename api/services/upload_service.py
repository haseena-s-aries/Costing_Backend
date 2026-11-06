import hashlib
import mimetypes
import os
import re
from pathlib import Path

from django.db import transaction
from django.conf import settings

from api.constants import MAX_UPLOAD_SIZE, SUPPORTED_EXTENSIONS
from api.exceptions import ImportServiceError
from api.models import ImportBatch, ImportBatchStatus, ImportCategory, ImportSheet, Supplier, UploadedFile
from api.services.header_detection_service import HeaderDetectionService
from api.services.mapping_service import MappingService
from api.services.normalization import normalize_supplier
from api.services.parser_service import ParserService, VIRTUAL_HEADERS

DOCUMENT_TYPE_TO_CATEGORY = {
    "PURCHASE_ORDER": "purchase_order",
    "PURCHASE ORDER": "purchase_order",
    "PO": "purchase_order",
    "QUOTATION": ImportCategory.SUPPLIER_QUOTATION,
    "QUOTE": ImportCategory.SUPPLIER_QUOTATION,
    "INVOICE": ImportCategory.OTHER,
    "GRN": ImportCategory.GRN,
    "LPO": ImportCategory.LPO,
}


class UploadService:
    GEMINI_EXTENSIONS = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tif",
        ".tiff",
        ".eml",
        ".msg",
        ".txt",
        ".xlsx",
        ".xls",
        ".csv",
    }

    def __init__(self):
        self.parser = ParserService()
        self.header_detector = HeaderDetectionService()
        self.mapping_service = MappingService()

    def _parse_with_gemini(self, file_obj, filename):
        from api.services.gemini_extraction_service import (
            gemini_result_to_virtual_sheet,
            normalize_result,
            _extract_pdf_email_freight_charges,
        )
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        from api.services.import_service import EXTRACTION_PIPELINE_VERSION

        if not settings.GEMINI_API_KEY:
            raise ImportServiceError(
                "Gemini extraction is not configured.",
                {"file": ["Set GEMINI_API_KEY to upload PDF/image/email files."]},
            )

        gemini_result = ExtractionOrchestrator().extract(file_obj, filename, import_batch=None)
        gemini_result = normalize_result(gemini_result)

        if str(filename or "").lower().endswith(".pdf") and not gemini_result.get('_physical_pdf_complete'):
            try:
                try:
                    file_obj.seek(0)
                except Exception:
                    pass

                file_bytes = file_obj.read()

                try:
                    file_obj.seek(0)
                except Exception:
                    pass

                freight_items = _extract_pdf_email_freight_charges(
                    file_bytes,
                    gemini_result.get("supplier_name"),
                )
                if freight_items:
                    existing_items = gemini_result.get("items") or []

                    non_freight_items = [
                        item
                        for item in existing_items
                        if not (
                            str(item.get("row_type") or "").upper()
                            in {"CHARGE", "SERVICE", "PRODUCT"}
                            and re.search(
                                r"\bfreight|door\s+to\s+airport|shipment\b",
                                str(item.get("product_name") or ""),
                                re.I,
                            )
                        )
                    ]

                    gemini_result["items"] = non_freight_items + freight_items
                    gemini_result = normalize_result(gemini_result)
            except Exception:
                pass
        gemini_sheet = gemini_result_to_virtual_sheet(gemini_result)
        document_fields = {
            "supplier": gemini_result.get("supplier_name"),
            "supplier_name": gemini_result.get("supplier_name"),
            "document_date": gemini_result.get("document_date"),
            "currency": gemini_result.get("currency"),
            "document_number": gemini_result.get("document_number"),
            "category": gemini_result.get("category"),
            "grand_total": gemini_result.get("grand_total"),
        }
        document_fields = {key: value for key, value in document_fields.items() if value not in (None, "")}

        return {
            "sheets": [gemini_sheet],
            "document_fields": document_fields,
            "document_field_meta": {
                field: {"confidence": 82, "source": {"type": "gemini"}}
                for field in document_fields
            },
            "extracted_text": "",
            "source_type": "gemini",
            "document_type": gemini_result.get("document_type") or "other",
            "extraction_summary": {
                "method": "gemini_only",
                "gemini_document_type": gemini_result.get("document_type"),
                "pipeline_version": EXTRACTION_PIPELINE_VERSION,
                "gemini_item_count": max(len(gemini_sheet.get("rows") or []) - 1, 0),
                "gemini_raw_result": gemini_result,
            },
        }

    def upload(self, *, file_obj, category, supplier=None, supplier_name="", default_currency="", remarks="", user=None):
        if not file_obj:
            raise ImportServiceError("No file uploaded.", {"file": ["This field is required."]})
        suffix = Path(file_obj.name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ImportServiceError(
                "Unsupported file type.",
                {
                    "file": [
                        "Supported files: Excel, CSV, PDF, image, "
                        "EML, MSG and text files."
                    ]
                },
            )
        if getattr(file_obj, "size", 0) <= 0:
            raise ImportServiceError("Empty file uploaded.", {"file": ["The uploaded file is empty."]})
        if file_obj.size > MAX_UPLOAD_SIZE:
            raise ImportServiceError("File is too large.", {"file": ["Maximum file size is 25 MB."]})
        file_hash = self._hash(file_obj)
        duplicate_batch = ImportBatch.objects.filter(
            uploaded_file__file_hash=file_hash,
            uploaded_file__is_deleted=False,
        ).exclude(status=ImportBatchStatus.DELETED).select_related("uploaded_file").first()
        if duplicate_batch:
            raise ImportServiceError(
                "Duplicate File",
                {"file": [f"This file was already uploaded as {duplicate_batch.batch_number}. Same content cannot be uploaded again."]},
                status_code=409,
            )
        if suffix in self.GEMINI_EXTENSIONS:
            parsed = self._parse_with_gemini(file_obj, file_obj.name)
        else:
           
            parsed = self.parser.parse(file_obj, file_obj.name)
        document_signature = self._document_signature(parsed)
        if document_signature:
            duplicate_doc = ImportBatch.objects.filter(
                document_metadata__document_signature=document_signature,
            ).exclude(status=ImportBatchStatus.DELETED).first()
            if duplicate_doc:
                raise ImportServiceError(
                    "Duplicate Document",
                    {"file": [f"Same quotation content was already uploaded as {duplicate_doc.batch_number}. Different file name is not allowed."]},
                    status_code=409,
                )
        category = self._resolve_category(category, parsed)
        document_fields = parsed.get("document_fields", {}) or {}

        if suffix not in self.GEMINI_EXTENSIONS:
            from api.services.import_service import ImportService

            native_text_supplier = ImportService()._supplier_from_native_text(parsed)
            if native_text_supplier:
                document_fields["supplier"] = native_text_supplier
                document_fields["supplier_name"] = native_text_supplier
                parsed["document_fields"] = document_fields

                extraction_summary = parsed.get("extraction_summary") or {}
                structured_document = extraction_summary.get("structured_document") or {}
                structured_header = structured_document.get("header") or {}
                structured_header["supplier_name"] = native_text_supplier
                structured_document["header"] = structured_header
                extraction_summary["structured_document"] = structured_document
                parsed["extraction_summary"] = extraction_summary
        if document_signature:
            document_fields["document_signature"] = document_signature
            parsed["document_fields"] = document_fields
        detected_supplier_name = (
            document_fields.get("supplier_name")
            or document_fields.get("supplier")
            or ""
        )
        detected_currency = document_fields.get("currency") or ""
        with transaction.atomic():
            duplicate_batch = ImportBatch.objects.select_for_update().filter(
                uploaded_file__file_hash=file_hash,
                uploaded_file__is_deleted=False,
            ).exclude(status=ImportBatchStatus.DELETED).first()
            if duplicate_batch:
                raise ImportServiceError(
                    "Duplicate File",
                    {"file": [f"This file was already uploaded as {duplicate_batch.batch_number}. Same content cannot be uploaded again."]},
                    status_code=409,
                )
            supplier_name = (supplier_name or detected_supplier_name or "").strip()
            default_currency = (
                detected_currency
                or default_currency
                or ""
            ).strip()
            if supplier is None and supplier_name:
                normalized_name = normalize_supplier(supplier_name)
                supplier = Supplier.objects.filter(normalized_supplier_name=normalized_name, is_active=True).first()
                if supplier is None:
                    supplier = Supplier.objects.create(
                        supplier_name=supplier_name,
                        normalized_supplier_name=normalized_name,
                        default_currency=default_currency,
                        is_active=True,
                    )
            uploaded = UploadedFile.objects.create(
                original_file_name=file_obj.name,
                file=file_obj,
                mime_type=getattr(file_obj, "content_type", None) or mimetypes.guess_type(file_obj.name)[0],
                file_hash=file_hash,
                source_type=parsed.get("source_type"),
                document_type=parsed.get("document_type"),
                extraction_status="completed",
                uploaded_by=user,
            )
            batch = ImportBatch.objects.create(
                uploaded_file=uploaded,
                category=category,
                supplier=supplier,
                supplier_name_from_file=supplier_name or (supplier.supplier_name if supplier else ""),
                default_currency=default_currency,
                remarks=remarks,
                document_metadata=parsed.get("document_fields", {}),
                extraction_summary={
                    "source_type": parsed.get("source_type"),
                    "document_type": parsed.get("document_type"),
                    "sheet_count": len(parsed.get("sheets", [])),
                    "document_field_meta": parsed.get("document_field_meta", {}),
                    **parsed.get("extraction_summary", {}),
                },
                uploaded_by=user,
            )
            sheets = []
            total_rows = 0
            is_gemini_upload = suffix in self.GEMINI_EXTENSIONS
            for data in parsed["sheets"]:
                rows = data["rows"]
              
                header_row = 1 if (is_gemini_upload or data.get("is_virtual")) else self.header_detector.suggest_header_row(rows)
                cached_headers_value = [str(cell or "") for cell in rows[header_row - 1]] if rows else list(VIRTUAL_HEADERS)
                sheet = ImportSheet.objects.create(
                    import_batch=batch,
                    sheet_name=data["sheet_name"],
                    sheet_index=data["sheet_index"],
                    header_row_number=header_row,
                    data_start_row_number=header_row + 1,
                    total_rows=max(len(rows) - header_row, 0),
                    cached_rows=rows,
                    cached_headers=cached_headers_value,
                )
                sheets.append((sheet, rows, header_row))
                total_rows += sheet.total_rows
            batch.total_rows = total_rows
            batch.save(update_fields=["total_rows", "updated_at"])
        return {
            "batch_id": batch.id,
            "batch_number": batch.batch_number,
            "file_name": uploaded.original_file_name,
            "file_type": suffix.lstrip("."),
            "duplicate_file_warning": False,
            "source_type": parsed.get("source_type"),
            "category": batch.category,
            "document_type": parsed.get("document_type"),
            "document_fields": batch.document_metadata,
            "document_field_meta": parsed.get("document_field_meta", {}),
            "extraction_summary": parsed.get("extraction_summary", {}),
            "sheets": [
                {
                    "id": sheet.id,
                    "sheet_name": sheet.sheet_name,
                    "sheet_index": sheet.sheet_index,
                    "suggested_header_row": header_row,
                    "total_rows": sheet.total_rows,
                }
                for sheet, _rows, header_row in sheets
            ],
        }
    def _document_signature(self, parsed):
        import hashlib
        import json
        import re

        def clean(value):
            text = str(value or "").lower()
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r"[^a-z0-9./@%()+*# -]+", "", text)
            return text.strip()

        def money(value):
            text = str(value or "")
            text = re.sub(r"[^\d.,-]", "", text)
            if "," in text and "." in text:
                text = text.replace(",", "")
            elif "," in text:
                text = text.replace(",", ".")
            try:
                return f"{float(text):.6f}"
            except Exception:
                return clean(value)

        fields = parsed.get("document_fields") or {}
        parts = [
            clean(fields.get("document_date")),
            clean(fields.get("currency")),
        ]

        row_parts = []

        for sheet in parsed.get("sheets", []):
            rows = sheet.get("rows") or []
            headers = rows[0] if rows else []

            for row in rows[1:]:
                data = dict(zip(headers, row))

                product_name = (
                    data.get("product_name")
                    or data.get("description")
                    or data.get("item_description")
                    or data.get("Product Name")
                    or data.get("Description")
                    or ""
                )

                if not clean(product_name):
                    continue

                row_parts.append([
                    clean(data.get("item_code") or data.get("Item Code")),
                    clean(data.get("part_number") or data.get("Part Number")),
                    clean(product_name),
                    money(data.get("quantity") or data.get("Quantity")),
                    clean(data.get("uom") or data.get("unit_of_measurement") or data.get("UoM")),
                    money(data.get("unit_price") or data.get("Unit Price")),
                    money(data.get("row_total") or data.get("line_total") or data.get("Row Total")),
                    clean(data.get("currency") or data.get("Currency") or fields.get("currency")),
                ])

        if not row_parts:
            return ""

        compact = json.dumps(
            parts + sorted(row_parts),
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(compact.encode("utf-8")).hexdigest()
    def _resolve_category(self, category, parsed):
        category = (category or "").strip()
        source_type = str(parsed.get("source_type") or "").lower()
        document_type = str(parsed.get("document_type") or "").upper().replace("-", "_")

        if source_type not in {"excel", "csv"}:
            resolved = DOCUMENT_TYPE_TO_CATEGORY.get(document_type)
            if resolved:
                return str(resolved)

        if category.lower() in {"", "auto", "auto_detect", "auto-detect"}:
            if source_type == "csv":
                return ImportCategory.OTHER
            if source_type == "excel":
                return ImportCategory.HISTORICAL_PURCHASE
            return ImportCategory.OTHER

        return category


    def _hash(self, file_obj):
        file_obj.seek(0)
        digest = hashlib.sha256()
        for chunk in file_obj.chunks() if hasattr(file_obj, "chunks") else iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
        file_obj.seek(0)
        return digest.hexdigest()
