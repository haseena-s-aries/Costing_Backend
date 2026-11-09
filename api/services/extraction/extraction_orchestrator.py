import logging
from pathlib import Path
from .archive_extractor import extract_archive, _archive_kind
from api.services.gemini_extraction_service import GeminiExtractionService, normalize_result
from api.services.source_reconciliation import required_metadata
from .confidence_scorer import ConfidenceScorer
from .deterministic_parser import DeterministicSpreadsheetParser
from .document_preprocessor import DocumentPreprocessor
from .profile_detector import ProfileDetector
from .structure_detector import StructureDetector

logger = logging.getLogger(__name__)

class ExtractionOrchestrator:
    def __init__(self):
        self.preprocessor, self.structure_detector = DocumentPreprocessor(), StructureDetector()
        self.profile_detector, self.parser, self.scorer = ProfileDetector(), DeterministicSpreadsheetParser(), ConfidenceScorer()
        self.gemini = None

    def extract(self, file_field, original_file_name, import_batch=None):
        try:
            file_field.seek(0)
            raw = file_field.read()
            file_field.seek(0)
        except (AttributeError, ValueError):
            raw = b""
        # XLSX is physically a ZIP container, but it is a spreadsheet
        # document, not an archive of independent uploadable files. Route by
        # extension before archive signature detection so internal XML/media
        # members are never sent through archive extraction.
        suffix = Path(str(original_file_name or "")).suffix.lower()
        is_spreadsheet = suffix in {".xlsx", ".xls"}
        if (
            not is_spreadsheet
            and isinstance(raw, (bytes, bytearray))
            and _archive_kind(original_file_name, bytes(raw))
        ):
            return self._extract_archive(raw, original_file_name, import_batch)
        try:
            document = self.preprocessor.prepare(file_field, original_file_name)
            structure = self.structure_detector.detect(document)
        except Exception:
            logger.exception("Extraction preparation failed file=%s", original_file_name)
            document = {"file_type": "other"}
            structure = self.structure_detector.detect(document)
            profile_match = {"profile": None, "confidence": 0.0}
        else:
            # Profile lookup is optional. A database/profile-store outage must
            # not discard the already prepared spreadsheet or force it through
            # Gemini, because deterministic table parsing remains valid.
            if document.get("file_type") == "spreadsheet":
                try:
                    profile_match = self.profile_detector.detect(document)
                except Exception:
                    logger.exception("Spreadsheet profile detection failed file=%s", original_file_name)
                    profile_match = {"profile": None, "confidence": 0.0}
            else:
                profile_match = {"profile": None, "confidence": 0.0}
        profile = profile_match.get("profile")
        result, method, confidence = None, "gemini", None
        try:
            # Spreadsheet parsing is self-validating: it only returns a
            # candidate after finding a usable product column and at least one
            # populated price/amount per commercial row. Do not discard that
            # result because metadata is incomplete or the structure score is
            # conservative. Gemini must not rewrite supplier-column mappings.
            spreadsheet_candidate = document.get("file_type") == "spreadsheet"
            table_candidate = (
                spreadsheet_candidate
                or (
                    structure.structure == StructureDetector.STANDARD_TABLE
                    and structure.confidence >= .80
                )
            )
            if table_candidate:
                candidate = self.parser.extract(document, structure, profile)
                if candidate and candidate.get("items"):
                    candidate = normalize_result(candidate)
                    confidence = self.scorer.score(candidate, structure.confidence, .90)
                    if spreadsheet_candidate or confidence["level"] == "high":
                        result, method = candidate, "profile" if profile else "deterministic"
        except Exception:
            logger.exception("Deterministic extraction failed file=%s", original_file_name)
            result = None
        if result is None:
            try: file_field.seek(0)
            except (AttributeError, ValueError): pass
            if self.gemini is None:
                self.gemini = GeminiExtractionService()
            result = normalize_result(self.gemini.extract_from_file(file_field, original_file_name, import_batch) or {})
            gemini_issues = list(result.get("_gemini_quality_issues") or [])
            if result.get("_gemini_needs_review"):
                gemini_issues.append("gemini_needs_review")
            confidence = self.scorer.score(
                result,
                structure.confidence,
                0.0,
                gemini_issues,
                extraction_method="gemini",
            )
        # Enforce printed-email source facts at the common handoff, including
        # deterministic and early-return Gemini paths. No later normalization
        # may put page-level buyer metadata back onto these quote rows.
        if suffix == '.pdf' and raw:
            from api.services.visual_review_policy import require_visual_review
            result = require_visual_review(result, bytes(raw))
            from api.services.pdf_email_quotes import recover_pdf_email_quotes
            from api.services.gemini_extraction_service import build_canonical_display_name
            result = recover_pdf_email_quotes(result, bytes(raw))
            from api.services.freight_quote_sections import restore_freight_offers, pdf_thread_text
            thread_text = pdf_thread_text(bytes(raw))
            if thread_text:
                from api.services.freight_quote_sections import collapse_calculated_totals
                result = collapse_calculated_totals(restore_freight_offers(result, thread_text))
                from api.services.internal_quote_prices import restore_internal_quotes
                result = restore_internal_quotes(result, thread_text)
            if not result.get('_authoritative_supplier_matrix'):
                from api.services.pdf_catalog_rates import recover_catalog_rates
                result = recover_catalog_rates(result, bytes(raw))
            from api.services.pdf_date_evidence import reject_reference_dates
            result = reject_reference_dates(result, bytes(raw))
            from api.services.pdf_table_rows import recover_table_rows
            result = recover_table_rows(result, bytes(raw))
            from api.services.yen_currency_hint import resolve_yen_currency
            from api.services.freight_quote_sections import pdf_text
            result = resolve_yen_currency(result, pdf_text(bytes(raw)))
            if (result.get('_source_email_quote_count') or result.get('_freight_section_count')
                    or result.get('_internal_quote_count')):
                for item in result.get('items') or []:
                    item['display_name'] = build_canonical_display_name(item)
            if result.get('_source_email_quote_count'):
                logger.info('PDF_EMAIL_FINAL file=%s rows=%s source_rows=%s uncertain_suppliers=%s',
                            original_file_name, len(result.get('items') or []), result['_source_email_quote_count'],
                            sum(bool(i.get('_supplier_identity_uncertain')) for i in result.get('items') or []))
        result.update({"_extraction_method": method,
                       "_profile_id": profile.id if profile and method == "profile" else None,
                       "_profile_name": profile.name if profile and method == "profile" else None,
                       "_profile_confidence": profile_match.get("confidence") if profile else None,
                       "_structure_type": structure.structure,
                       "_structure_confidence": confidence.get("structure_confidence"),
                       "_mapping_confidence": confidence.get("mapping_confidence"),
                       "_confidence": confidence,
                       "_needs_review": bool(result.get('_needs_review') or confidence["level"] == "low")})
        if suffix in {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}:
            from api.services.currency_evidence_policy import require_image_currency_evidence
            result = require_image_currency_evidence(result)
        from api.services.commercial_consistency import finalize as finalize_commercial
        result = finalize_commercial(result)
        return required_metadata(result)

    def _extract_archive(self, raw, original_file_name, import_batch):
        members, skipped = extract_archive(raw, original_file_name)
        documents = []
        for inner_name, inner_data in members:
            try:
                child = self.extract(_BytesFile(inner_data), inner_name, import_batch)
                child["_archive_filename"] = original_file_name
                child["_inner_file_path"] = inner_name
                document_items = list(child.get("items") or [])
                for item in document_items:
                    item["source_reference"] = ":".join((original_file_name, inner_name))
                # The archive root owns the canonical flattened ``items`` list.
                # Keep per-document rows available for a detail view under a
                # distinct key so generic response consumers cannot append them
                # to the main product list a second time.
                child["document_items"] = document_items
                child["document_item_count"] = len(document_items)
                child.pop("items", None)
                logger.info(
                    "ARCHIVE_MEMBER_EXTRACTED archive=%s member=%s items=%s",
                    original_file_name,
                    inner_name,
                    len(document_items),
                )
                documents.append(child)
            except Exception as exc:
                skipped.append({"file_name": inner_name, "reason": f"Extraction failed: {exc}"})
                logger.exception("Archive member extraction failed archive=%s member=%s", original_file_name, inner_name)
        if len(documents) == 1:
            # A single-document archive still follows the normal extraction
            # contract: its root ``items`` remain the canonical import list.
            documents[0]["items"] = documents[0].pop("document_items", [])
            documents[0].pop("document_item_count", None)
            documents[0]["_archive_members"] = {"extracted": [name for name, _ in members], "skipped": skipped}
            return documents[0]
        suppliers = {doc.get("supplier_name") for doc in documents if doc.get("supplier_name")}
        currencies = {doc.get("currency") for doc in documents if doc.get("currency")}
        result = {
            "document_type": "archive",
            "category": "ARCHIVE",
            "supplier_name": next(iter(suppliers)) if len(suppliers) == 1 else None,
            "document_date": None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "items": [item for doc in documents for item in doc.get("document_items") or []],
            "_archive_filename": original_file_name,
            "_archive_documents": documents,
            "_archive_members": {"extracted": [name for name, _ in members], "skipped": skipped},
        }
        logger.info(
            "ARCHIVE_AGGREGATED archive=%s members=%s documents=%s canonical_items=%s skipped=%s",
            original_file_name,
            len(members),
            len(documents),
            len(result["items"]),
            len(skipped),
        )
        result["_needs_review"] = bool(skipped or len(documents) != 1)
        return result


class _BytesFile:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    def seek(self, pos):
        self.pos = pos
    def read(self):
        data = self.data[self.pos:]
        self.pos = len(self.data)
        return data
    def open(self, mode="rb"):
        self.pos = 0
