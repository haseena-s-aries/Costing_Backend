import json
import logging
import time
import os
from django.conf import settings
import re
from collections import defaultdict
from types import SimpleNamespace
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from api.services.gemini_extraction_service import (
    GeminiExtractionService,
    validate_gemini_extraction,
    normalize_result,
    _extract_pdf_email_freight_charges,
    _extract_pdf_email_tariff_charge_items,
    _tariff_recovery_key,
)
from api.exceptions import ImportServiceError
from api.models import ImportBatch, ImportBatchStatus, ImportRow, ImportRowStatus, PriceHistory, ProductMatchResult, ProductMatchStatus
from api.services.duplicate_service import DuplicateService
from api.services.parser_service import ParserService, VIRTUAL_HEADERS, _norm
from api.services.product_normalization_service import group_key, line_item_group_key
from api.services.product_resolution_service import ProductResolutionService
from api.services.supplier_matching_service import SupplierMatchingService
from api.services.validation_service import ValidationService, parse_date, parse_decimal, parse_quantity_decimal
from api.services.normalization import is_contact_only_supplier
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator

logger = logging.getLogger(__name__)
EXTRACTION_PIPELINE_VERSION = "2026-11-07-email-thread-quotes-1"
def resolve_import_metadata(batch):
    """
    Single source of truth for document metadata.

    Priority:
    1. document_metadata
    2. batch supplier
    3. unique values from normalized ImportRows

    Never let Review, History and Import use different rules.
    """

    metadata = dict(
        batch.document_metadata or {}
    )

    rows = list(
        batch.rows.all()
    )

    def row_values(*keys):
        values = []

        for row in rows:
            data = (
                row.normalized_data
                or row.mapped_data
                or {}
            )

            for key in keys:
                value = str(
                    data.get(key) or ""
                ).strip()

                if (
                    value
                    and value.upper() != "UNKNOWN"
                ):
                    values.append(value)
                    break

        unique = list(
            dict.fromkeys(values)
        )

        return unique

    # ---------------------------
    # SUPPLIER
    # ---------------------------
    supplier = str(
        metadata.get("supplier_name")
        or metadata.get("supplier")
        or batch.supplier_name_from_file
        or (
            batch.supplier.supplier_name
            if batch.supplier_id
            else ""
        )
        or ""
    ).strip()

    if not supplier:
        suppliers = row_values(
            "supplier_name",
            "supplier",
        )

        if len(suppliers) == 1:
            supplier = suppliers[0]

    if is_contact_only_supplier(supplier):
        supplier = ""

    # ---------------------------
    # DATE
    # ---------------------------
    document_date = str(
        metadata.get("document_date")
        or ""
    ).strip()

    if not document_date:
        dates = row_values(
            "purchase_date",
            "document_date",
        )

        if len(dates) == 1:
            document_date = dates[0]

    # ---------------------------
    # CURRENCY
    # ---------------------------
    currency = str(
        metadata.get("currency")
        or ""
    ).strip()

    if not currency:
        currencies = row_values(
            "currency",
        )

        if len(currencies) == 1:
            currency = currencies[0]

    # ---------------------------
    # CATEGORY
    # ---------------------------
    category = str(
        metadata.get("category")
        or batch.category
        or ""
    ).strip()

    if not category:
        categories = row_values(
            "source_category",
            "category",
        )

        if len(categories) == 1:
            category = categories[0]

    return {
        **metadata,
        "supplier_name": supplier or None,
        "supplier": supplier or None,
        "document_date": document_date or None,
        "currency": currency or None,
        "category": category or None,
    }

def build_review_reasons(batch):
    """
    Build review reasons only from final persisted data.
    """

    metadata = resolve_import_metadata(
        batch
    )

    reasons = []
    from api.services.visual_review_policy import batch_visual_review_reasons
    reasons.extend(batch_visual_review_reasons(batch))
    raw_extraction = (batch.extraction_summary or {}).get('gemini_raw_result') or {}
    visual_reasons = raw_extraction.get('_review_reasons') or []
    if 'handwritten_content_requires_review' in visual_reasons:
        reasons.append('Handwritten content — verify all extracted fields')
    if 'scanned_content_requires_review' in visual_reasons:
        reasons.append('Scanned document — verify extracted text and amounts')

    document_supplier = str(
        metadata.get("supplier_name")
        or ""
    ).strip()

    row_suppliers = {
        str(
            (
                row.normalized_data
                or row.mapped_data
                or {}
            ).get("supplier_name")
            or (
                row.normalized_data
                or row.mapped_data
                or {}
            ).get("supplier")
            or ""
        ).strip()
        for row in batch.rows.all()
    }

    row_suppliers = {
        supplier
        for supplier in row_suppliers
        if supplier
        and supplier.upper() != "UNKNOWN"
        and not is_contact_only_supplier(supplier)
    }

    # Review supplier as missing ONLY when
    # neither document nor any item has supplier.
    if (
        not document_supplier
        and not row_suppliers
    ):
        reasons.append(
            "Supplier name missing"
        )

    row_payloads = [
        {**(row.raw_data or {}), **(row.mapped_data or {}), **(row.normalized_data or {})}
        for row in batch.rows.all() if row.row_type != "IGNORE"
    ]
    from api.services.row_metadata_policy import missing_row_metadata
    missing_metadata = missing_row_metadata(row_payloads, metadata)
    if missing_metadata['date']:
        reasons.append('Document date missing')
    if missing_metadata['currency']:
        reasons.append(
            "Currency missing"
        )

    if not str(
        metadata.get("category")
        or ""
    ).strip():
        reasons.append(
            "Category missing"
        )

    if not batch.rows.exists():
        reasons.append(
            "No commercial rows extracted"
        )

    return reasons
class ImportService:
        def _clean_document_supplier(self, value):
            text = re.sub(
                r"\s+",
                " ",
                str(value or ""),
            ).strip(" \t\r\n:-|,[]")
            # If supplier accidentally comes as a domain, convert it to a readable name.
            domain_match = re.fullmatch(
                r"(?:www\.)?([a-z0-9._-]+)\.(?:com|net|org|co|in|ae|io)",
                text,
                re.I,
            )

            if domain_match:
                text = domain_match.group(1)
                text = text.replace("-", " ").replace("_", " ").replace(".", " ")
                text = re.sub(r"\s+", " ", text).strip().upper()
            text = re.sub(
                r"\b(?:Voucher\s+No\.?|Dated|Mode/Terms\s+of\s+Payment|Buyer'?s\s+Ref\.?/Order\s+No\.?|Other\s+Reference\(s\)|Terms\s+of\s+Delivery).*$",
                "",
                text,
                flags=re.I,
            ).strip(" :-|")
            if not text:
                return None
            legal_company_match = re.search(
                r"(?P<company>[A-Z][A-Z0-9 &._/-]{2,}?\b(?:L\.?\s*L\.?\s*C\.?|LLC|LTD|LIMITED|FZE|FZC|COMPANY|CO\.?))\b",
                text.split("=")[-1],
                re.I,
            )
            if legal_company_match and self._is_noisy_supplier_value(text):
                text = legal_company_match.group("company").strip(" :-|,[]")
            email_match = re.search(
                r"<[^@\s<>]+@(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})>",
                text,
                re.I,
            )
            if email_match:
                domain = email_match.group("domain").lower()
                ignored_domains = {
                    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                    "icloud.com", "aol.com", "proton.me", "protonmail.com",
                }
                if domain not in ignored_domains:
                    parts = domain.split(".")
                    company_part = parts[-3] if parts[-2] in {"co", "com", "net", "org"} and len(parts) >= 3 else parts[-2]
                    if company_part not in {"mail", "email", "info", "sales", "admin", "accounts"}:
                        return company_part.replace("-", " ").replace("_", " ").upper()
            text = re.sub(
                r"^\s*(?:"
                r"for|from|by|supplier|vendor|seller|"
                r"quotation from|issued by|offered by"
                r")\s*[,:\-]?\s*",
                "",
                text,
                flags=re.I,
            ).strip(" \t\r\n:-|,[]")
            text = re.split(
                r"\s+(?:quote\s*#|quote\s+no|quotation\s+no|po\s+number|po\s+date|date|valid\s+until|trn)(?:\b|\s|:|$)",
                text,
                maxsplit=1,
                flags=re.I,
            )[0].strip(" \t\r\n:-|,")
            # Remove duplicate adjacent words caused by PDF text extraction.
            # Example: "Electronic Services Services Ltd."
            words = text.split()
            deduplicated_words = []

            for word in words:
                if (
                    deduplicated_words
                    and word.lower() == deduplicated_words[-1].lower()
                ):
                    continue

                deduplicated_words.append(word)

            text = " ".join(deduplicated_words).strip()
            return text or None


        def _is_reliable_supplier(self, value):
            text = self._clean_document_supplier(value)

            if not text:
                return False

            normalized = text.lower()
            normalized_label = re.sub(
                r"[^a-z0-9]+",
                " ",
                normalized,
            ).strip()

            if normalized_label in {
                "prepared",
                "prepared by",
                "proposal",
                "project",
                "quotation",
                "quote",
                "offer",
                "customer",
                "supplier",
                "vendor",
                "seller",
                "company",
                "teamwork",
                "our works",
                "cooperation",
            }:
                return False
            normalized_for_terms = re.sub(r"[^a-z0-9]+", " ", normalized).strip()

            supplier_noise_tokens = {
                "unit", "price", "total", "material", "category",
                "quantity", "qty", "description", "item", "product",
                "amount", "rate", "currency", "meter", "pcs",
                "prepared", "proposal", "project", "teamwork","cancellation",
                "allowed",
                "unless",
                "agreed",
            }

            normalized_tokens = normalized_label.split()

            # Reject pure table headings and document labels.
            if normalized_tokens and all(
                token in supplier_noise_tokens
                for token in normalized_tokens
            ):
                return False

            # Reject prose/marketing sentences. A supplier name should not look
            # like a sentence with connectors or verbs.
            sentence_words = {
                "that", "which", "with", "through", "provide", "provides",
                "providing", "deliver", "delivers", "support", "supports",
                "ensure", "ensures", "create", "creates", "aim", "aims",
                "our", "your", "their", "this", "these", "those", "and",
                "for", "from", "into", "across", "within", "without",
            }

            if (
                len(normalized_tokens) >= 4
                and any(token in sentence_words for token in normalized_tokens)
            ):
                return False

            # Remove every non-letter character so OCR-spaced headings such as
            # "Q U OTATIO N" become "quotation".
            compact = re.sub(r"[^a-z]", "", normalized)

            rejected_compact_terms = {
                "quotation",
                "commercialoffer",
                "purchaseorder",
                "proformainvoice",
                "invoice",
                "bankdetails",
                "bankname",
                "amountinwords",
                "grandtotal",
                "billingaddress",
                "shippingaddress",
                "deliveryaddress",
                "unitprice",
                "unitrate",
                "description",
                "quantity",
                "authorisedsignatory",
                "authorizedsignatory",
                "subject",
                "followup",
                "flagstatus",
                "certificate",
                "certificates",
                "information",
                
            }

            if any(term in compact for term in rejected_compact_terms):
                return False

            rejected_normal_terms = {
                "customer",
                "buyer",
                "bill to",
                "ship to",
                "prepared by",
                "make",
                "origin",
                "country of origin",
                "coo",
                "hs code",
                "warranty",
                "account no",
                "account number",
                "swift code",
                "ifsc",
                "iban",
                "aries group",
                "aries marine",
                "al jerf",
                "ajman",
                "united arab emirates",
                "m/s. aries",
                "m/s aries",
                "m s aries",
                "group of companies",
                "sarika",
                "aries trading",
                "subject:",
                "follow up",
                "flagged",
                "sales contract",
                "supplier 1 revised quote",
                "mooring equipment",
                "product status",
                "row status",
                "information",
                "certificate",
                "certificates",
                "description of goods",
                "qty pcs",
                "sum usd",
                "terms of delivery",
                "despatch through",
                "destination",
                "once the order is placed",
                "cancellation is not allowed",
                "unless agreed",
                "terms and conditions",
            }

            if any(term in normalized for term in rejected_normal_terms):
                return False
            if any(term in normalized_for_terms for term in {"m s aries", "group of companies"}):
                return False

            if "@" in text or "www." in normalized:
                return False

            if len(text) > 180:
                return False

            if len(re.findall(r"\d", text)) >= 5:
                return False
            # Do not accept arbitrary uppercase headings as suppliers.
            # A short uppercase value must still contain a real company/legal
            # indicator. This rejects PREPARED, TEAMWORK, UNIT UNIT TOTAL, etc.
            if (
                re.fullmatch(r"[A-Z][A-Z0-9 &._/-]{2,}", text)
                and len(text.split()) <= 5
            ):
                return bool(
                    re.search(
                        r"\b(?:"
                        r"LLC|L\.L\.C|LTD|LIMITED|COMPANY|CO\.?|"
                        r"TRADING|TECHNICAL|TECHNOLOGY|TECHNOLOGIES|"
                        r"SERVICES|SUPPLIES|ENGINEERING|ELECTRONICS|"
                        r"ELECTRICAL|MARINE|OILFIELD|INDUSTRIES|"
                        r"INDUSTRIAL|ENTERPRISES|FZE|FZC"
                        r")\b",
                        text,
                        re.I,
                    )
                )
            return bool(
                re.search(
                    r"\b(?:"
                    r"llc|l\.l\.c|ltd|limited|company|co\.?|"
                    r"trading|technical|technologies|technology|"
                    r"services|supplies|industries|"
                    r"engineering|electronics|electrical|marine|"
                    r"oilfield|group|alliances|agencies|hardware|"
                    r"electric|elect|enterprises|fze|fzc|establishment"
                    r")\b",
                    text,
                    re.I,
                )
            )


        def _supplier_from_native_text(self, parsed_result):
            """
            Resolve supplier safely.

            Priority:
            1. Latest ParserService document fields.
            2. Structured document header.
            3. A restricted scan of only the document header area.

            Arbitrary body text, marketing text, table headings and footer
            sentences are never accepted as supplier names.
            """

            document_fields = (
                parsed_result.get("document_fields")
                or {}
            )

            structured_header = (
                (
                    parsed_result.get("extraction_summary")
                    or {}
                )
                .get("structured_document", {})
                .get("header", {})
                or {}
            )

            metadata_candidates = [
                document_fields.get("supplier"),
                document_fields.get("supplier_name"),
                structured_header.get("supplier_name"),
            ]

            for candidate in metadata_candidates:
                if self._is_reliable_supplier(candidate):
                    return self._clean_document_supplier(candidate)

            extracted_text = str(
                parsed_result.get("extracted_text")
                or ""
            )

            # Supplier identity normally appears in the header. Restricting the
            # scan prevents page-body phrases such as
            # "innovative technical solutions that..." from being selected.
            all_lines = [
                re.sub(r"\s+", " ", line).strip(" :-|")
                for line in extracted_text.splitlines()
                if line.strip()
            ]

            # Supplier/company identity normally appears in the document header.
            # Scan only the first page/header area, not the full PDF body.
            lines = all_lines[:35]

            if not lines:
                return None

            company_indicator_pattern = re.compile(
                r"\b(?:"
                r"llc|l\.l\.c|ltd|limited|company|co\.?|corporation|corp|"
                r"trading|technical|technology|technologies|services|"
                r"supplies|engineering|electronics|electrical|marine|"
                r"oilfield|industries|industrial|enterprises|fze|fzc"
                r")\b",
                re.I,
            )

            legal_suffix_pattern = re.compile(
                r"\b(?:llc|l\.l\.c|ltd|limited|fze|fzc)\b",
                re.I,
            )

            sentence_pattern = re.compile(
                r"\b(?:"
                r"that|which|with|through|provide|provides|providing|"
                r"deliver|delivers|support|supports|ensure|ensures|"
                r"create|creates|aim|aims|our|your|their|this|these|"
                r"those|and|for|from|into|across|within|without"
                r")\b",
                re.I,
            )

            candidates = []

            for start_index in range(len(lines)):
                
                for size in (3, 2, 1):
                    end_index = start_index + size

                    if end_index > len(lines):
                        continue

                    selected_lines = lines[start_index:end_index]

                    # Reject combinations containing obvious document labels.
                    normalized_combined = _norm(
                        " ".join(selected_lines)
                    )

                    rejected_header_terms = {
                        "prepared",
                        "prepared by",
                        "proposal",
                        "project",
                        "quotation",
                        "customer",
                        "teamwork",
                        "our works",
                        "unit price",
                        "total price",
                        "material unit",
                        "category unit",
                        "terms of delivery",
                        "despatch through",
                        "destination",
                        "buyer",
                    }

                    if any(
                        term in normalized_combined
                        for term in rejected_header_terms
                    ):
                        continue

                    combined = " ".join(selected_lines)

                    candidate = self._clean_document_supplier(
                        combined
                    )

                    if not candidate:
                        continue

                    # Do not accept marketing/body sentences.
                    normalized_tokens = _norm(candidate).split()

                    sentence_words = {
                        "that",
                        "which",
                        "through",
                        "provide",
                        "provides",
                        "deliver",
                        "delivers",
                        "support",
                        "supports",
                        "ensure",
                        "ensures",
                        "create",
                        "creates",
                        "aim",
                        "aims",
                        "our",
                        "your",
                        "their",
                        "this",
                        "these",
                        "those",
                    }

                    if (
                        len(normalized_tokens) >= 4
                        and any(
                            token in sentence_words
                            for token in normalized_tokens
                        )
                    ):
                        continue

                    if not self._is_reliable_supplier(candidate):
                        continue

                    company_indicators = re.findall(
                        r"\b(?:"
                        r"llc|l\.l\.c|ltd|limited|company|co\.?|"
                        r"trading|technical|technology|technologies|"
                        r"services|supplies|engineering|electronics|"
                        r"electrical|marine|oilfield|industries|"
                        r"industrial|enterprises|fze|fzc"
                        r")\b",
                        candidate,
                        re.I,
                    )

                    legal_suffix_found = bool(
                        re.search(
                            r"\b(?:"
                            r"llc|l\.l\.c|ltd|limited|fze|fzc"
                            r")\b",
                            candidate,
                            re.I,
                        )
                    )

                    # Require a meaningful company identity.
                    if not legal_suffix_found and len(company_indicators) < 2:
                        continue

                    score = 50

                    # Complete legal company names are strongest.
                    if legal_suffix_found:
                        score += 100

                    score += min(
                        len(company_indicators),
                        5,
                    ) * 20

                    # Earlier header lines get higher priority.
                    score += max(0, 40 - start_index)

                    # Prefer reconstructed multi-line company names.
                    if size > 1:
                        score += 25

                    # Prefer a reasonable company-name length.
                    if 2 <= len(candidate.split()) <= 10:
                        score += 20

                    candidates.append(
                        (
                            score,
                            -start_index,
                            candidate,
                        )
                    )

            if not candidates:
                return None

            candidates.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                    len(item[2]),
                ),
                reverse=True,
            )

            return candidates[0][2]
        def _parser_quality(self, parsed_result):
            parsed = parsed_result.get("sheets") or []
            fields = parsed_result.get("document_fields") or {}
            summary = parsed_result.get("extraction_summary") or {}
            method = str(summary.get("method") or "").lower()
            extracted_text = str(parsed_result.get("extracted_text") or "")

            product_rows = 0
            rows_with_price = 0
            rows_with_quantity = 0
            bad_product_names = 0
            total_commercial_rows = 0
            page_numbers = set()
            tables = summary.get("tables") or []
            for table in tables:
                try:
                    page_numbers.add(int(table.get("page")))
                except (TypeError, ValueError, AttributeError):
                    pass

            for sheet in parsed:
                rows = sheet.get("rows") or []
                if not rows:
                    continue
                headers = rows[0]
                if "product_name" not in headers:
                    continue

                product_index = headers.index("product_name")
                row_type_index = headers.index("row_type") if "row_type" in headers else None
                quantity_index = headers.index("quantity") if "quantity" in headers else None
                unit_price_index = headers.index("unit_price") if "unit_price" in headers else None
                line_total_index = headers.index("line_total") if "line_total" in headers else None

                for row in rows[1:]:
                    row_type = str(row[row_type_index] if row_type_index is not None and row_type_index < len(row) else "PRODUCT").upper()
                    if row_type not in {"PRODUCT", "SERVICE", "CHARGE"}:
                        continue
                    total_commercial_rows += 1

                    product_name = str(row[product_index] if product_index < len(row) else "").strip()
                    if product_name:
                        product_rows += 1
                    if quantity_index is not None and quantity_index < len(row) and row[quantity_index]:
                        rows_with_quantity += 1
                    if (
                        (unit_price_index is not None and unit_price_index < len(row) and row[unit_price_index])
                        or (line_total_index is not None and line_total_index < len(row) and row[line_total_index])
                    ):
                        rows_with_price += 1
                    if self._bad_extracted_product_name(product_name):
                        bad_product_names += 1

            missing_prices_ratio = 1
            if total_commercial_rows:
                missing_prices_ratio = round(1 - (rows_with_price / total_commercial_rows), 2)

            missing_supplier = not self._is_reliable_supplier(fields.get("supplier") or fields.get("supplier_name"))
            missing_date = not fields.get("document_date")
            missing_currency = not fields.get("currency")

            reasons = []
            if product_rows == 0:
                reasons.append("no_products")
            if missing_supplier:
                reasons.append("missing_supplier")
            if missing_date:
                reasons.append("missing_date")
            if missing_currency:
                reasons.append("missing_currency")
            if bad_product_names:
                reasons.append("bad_product_names")
            if missing_prices_ratio >= 0.75 and total_commercial_rows:
                reasons.append("mostly_missing_prices")
            if "ocr" in method or "needs_review" in method:
                reasons.append("ocr_or_needs_review")
            if len(extracted_text.strip()) < 80 and not total_commercial_rows:
                reasons.append("suspiciously_short_extraction")
            if tables and len(page_numbers) <= 1 and re.search(r"\b(?:page\s+2|continued|continuation|carry\s+forward|brought\s+forward)\b", extracted_text, re.I):
                reasons.append("possible_first_page_only_extraction")
            if re.search(r"\b(?:continued|continuation|next\s+page|page\s+\d+\s+of\s+\d+)\b", extracted_text, re.I) and total_commercial_rows <= 1:
                reasons.append("low_item_count_for_multipage_document")

            score = 100
            score -= 40 if product_rows == 0 else 0
            score -= 15 if missing_supplier else 0
            score -= 10 if missing_date else 0
            score -= 10 if missing_currency else 0
            score -= min(25, bad_product_names * 5)
            score -= 20 if missing_prices_ratio >= 0.75 and total_commercial_rows else 0
            score -= 25 if "possible_first_page_only_extraction" in reasons else 0
            score -= 20 if "low_item_count_for_multipage_document" in reasons else 0
            score -= 20 if "suspiciously_short_extraction" in reasons else 0

            return {
                "score": max(score, 0),
                "product_rows": product_rows,
                "total_commercial_rows": total_commercial_rows,
                "missing_supplier": missing_supplier,
                "missing_date": missing_date,
                "missing_currency": missing_currency,
                "bad_product_names": bad_product_names,
                "missing_prices_ratio": missing_prices_ratio,
                "page_numbers": sorted(page_numbers),
                "extracted_text_length": len(extracted_text.strip()),
                "reasons": reasons,
            }

        def _should_use_gemini(self, quality):
            if not quality["reasons"]:
                return False
            if quality["product_rows"] == 0:
                return True
            if quality["bad_product_names"]:
                return True
            if quality["missing_prices_ratio"] >= 0.75 and quality["total_commercial_rows"]:
                return True
            if any(reason in quality["reasons"] for reason in {
                "possible_first_page_only_extraction",
                "low_item_count_for_multipage_document",
                "suspiciously_short_extraction",
            }):
                return True
            if quality["score"] < 70:
                return True
            return False

        def _gemini_primary_supported(self, original_file_name):
            extension = os.path.splitext(str(original_file_name or "").lower())[1]
            return extension in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".eml", ".msg", ".txt"}

        def _apply_gemini_result(self, parsed_result, gemini_result, parser_quality, reason):
            from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet, validate_gemini_extraction

            validation = validate_gemini_extraction(gemini_result)
            gemini_sheet = gemini_result_to_virtual_sheet(gemini_result)
            accepted_items = max(len(gemini_sheet.get("rows") or []) - 1, 0)
            parsed_result["sheets"] = [gemini_sheet]
            parsed_result.setdefault("extraction_summary", {})
            parsed_result["extraction_summary"].update({
                "method": "gemini_complete_document",
                "extraction_source": "gemini",
                "gemini_reason": reason,
                "gemini_item_count": accepted_items,
                "gemini_document_type": gemini_result.get("document_type"),
                "gemini_validation": validation,
                "gemini_validation_warnings": gemini_sheet.get("gemini_validation_warnings", []),
                "parser_quality_before_gemini": parser_quality,
            })
            parsed_result["document_fields"] = self._merge_document_fields(
                parsed_result.get("document_fields") or {},
                gemini_result,
                {
                    **parser_quality,
                    "missing_supplier": True,
                    "missing_date": True,
                    "missing_currency": not (parsed_result.get("document_fields") or {}).get("currency"),
                } if reason == "primary_document_ai" else parser_quality,
            )
            parsed_result["document_fields"]["document_type"] = gemini_result.get("document_type")
            parsed_result["document_fields"]["category"] = gemini_result.get("category") or parsed_result["document_fields"].get("category")
            return parsed_result, validation, accepted_items

        def _validate_history_only_row(self, mapped, default_currency, row_type):
            errors = []
            warnings = [f"{row_type.title()} row will be stored in import history; no product price record will be created."]
            if not str(mapped.get("product_name") or "").strip():
                errors.append("Service/charge description is required.")
            quantity = None
            unit_price = None
            if mapped.get("quantity") not in (None, ""):
                try:
                    quantity = parse_quantity_decimal(mapped.get("quantity"))
                    if quantity < 0:
                        errors.append("Quantity cannot be negative.")
                    mapped["quantity"] = str(quantity)
                except Exception:
                    warnings.append("Quantity must be numeric.")
                    mapped["quantity"] = None
            if mapped.get("unit_price") not in (None, ""):
                try:
                    unit_price = parse_decimal(mapped.get("unit_price"))
                    if unit_price < 0:
                        errors.append("Unit price cannot be negative.")
                    mapped["unit_price"] = str(unit_price)
                except Exception:
                    warnings.append("Unit price must be numeric.")
                    mapped["unit_price"] = None
            if mapped.get("line_total") not in (None, ""):
                try:
                    line_total = parse_decimal(mapped.get("line_total"))
                    mapped["line_total"] = str(line_total) if line_total is not None else None
                    if line_total is not None and quantity is not None and unit_price is not None:
                        expected = quantity * unit_price
                        tolerance = max(Decimal("0.05"), abs(expected) * Decimal("0.03"))
                        if abs(line_total - expected) > tolerance:
                            warnings.append("Line total does not match quantity x unit price.")
                except Exception:
                    warnings.append("Line total must be numeric.")
                    mapped["line_total"] = None
            if mapped.get("purchase_date"):
                try:
                    mapped["purchase_date"] = parse_date(mapped.get("purchase_date")).isoformat()
                except Exception:
                    warnings.append("Purchase date could not be parsed.")
                    mapped["purchase_date"] = None
            currency = str(mapped.get("currency") or default_currency or "").strip().upper()
            if currency in {"DHS", "DH", "DIRHAM", "DIRHAMS"}:
                currency = "AED"
            if not currency:
                warnings.append("Currency missing from source document.")
            elif not re.fullmatch(r"[A-Z]{3}", currency):
                errors.append("Currency must be a valid 3-letter code.")
            mapped["currency"] = currency
            status = ImportRowStatus.FAILED if errors else (ImportRowStatus.WARNING if len(warnings) > 1 else ImportRowStatus.VALID)
            return status, errors, warnings, mapped

        def _bad_extracted_product_name(self, value):
            text = str(value or "").strip()
            normalized = _norm(text)
            if not text or re.fullmatch(r"\d+", text):
                return True
            if re.search(r"\b(?:qty|quantity|vat|tax|total|subtotal|grand total|bank|iban|swift)\b", normalized):
                return True
            if re.search(r"\b\d+(?:\.\d+)?\s+(?:nos?|pcs?|kg|mtr|meter|unit|set)\s+\d", normalized):
                return True
            return False

        def _is_noisy_supplier_value(self, value):
            text = str(value or "").strip()
            normalized = _norm(text)
            if not text:
                return True
            if len(text) > 80:
                return True
            if any(term in normalized for term in {
                "po box",
                "p o box",
                "po sox",
                "p o sox",
                "tel",
                "telephone",
                "fax",
                "email",
                "www",
                "trn",
                "tax invoice",
                "invoice",
                "bill to",
                "ship to",
                "customer",
                "buyer",
            }):
                return True
            if re.search(r"\b\d{3,}\b", text) and len(text.split()) > 4:
                return True
            return False

        def _select_supplier_candidate(self, *, stored_supplier, file_supplier, metadata_supplier, native_text_supplier, gemini_corrected=False):
            candidates = (
                [stored_supplier, metadata_supplier, native_text_supplier, file_supplier]
                if gemini_corrected
                else [stored_supplier, metadata_supplier, native_text_supplier, file_supplier]
            )
            for candidate in candidates:
                if not candidate:
                    continue
                cleaned = self._clean_document_supplier(candidate)
                if not cleaned:
                    continue
                if candidate == stored_supplier:
                    return cleaned
                if self._is_noisy_supplier_value(cleaned):
                    continue
                if self._is_reliable_supplier(cleaned):
                    return cleaned
            return None

        def _merge_document_fields(self, existing_fields, gemini_result, quality):
            merged = {**(existing_fields or {})}
            gemini_fields = {
                "supplier": gemini_result.get("supplier_name"),
                "supplier_name": gemini_result.get("supplier_name"),
                "document_date": gemini_result.get("document_date"),
                "currency": gemini_result.get("currency"),
                "document_number": gemini_result.get("document_number"),
                "category": gemini_result.get("category"),
                "grand_total": gemini_result.get("grand_total"),
            }

            for field, value in gemini_fields.items():
                if value in (None, ""):
                    continue
                if field in {"supplier", "supplier_name"}:
                    current_supplier = merged.get("supplier") or merged.get("supplier_name")
                    if (
                        (quality["missing_supplier"] or self._is_noisy_supplier_value(current_supplier))
                        and self._is_reliable_supplier(value)
                    ):
                        merged["supplier"] = value
                        merged["supplier_name"] = value
                        merged["gemini_supplier_corrected"] = True
                    continue
                if field == "document_date":
                    if quality["missing_date"]:
                        merged[field] = value
                    continue
                if field == "currency":
                    existing_currency = str(merged.get("currency") or "").strip().upper()
                    gemini_currency = str(value or "").strip().upper()
                    if existing_currency:
                        continue
                    if re.fullmatch(r"[A-Z]{3}", gemini_currency):
                        merged[field] = gemini_currency
                    continue
                if not merged.get(field):
                    merged[field] = value

            merged["gemini_checked"] = True
            return merged

        def preview(self, batch, sheet_ids, limit=50):
            sheets = batch.sheets.filter(id__in=sheet_ids)
            if not sheets.exists():
                raise ImportServiceError("No valid sheets selected.")
            ImportRow.objects.filter(import_batch=batch).delete()

            gemini_configured = bool(settings.GEMINI_API_KEY)
            if not gemini_configured:
                raise ImportServiceError(
                    "Gemini extraction is not configured.",
                    {"file": ["Set GEMINI_API_KEY to extract documents."]},
                )

            

            # Minimal skeleton — parser_service regex/OCR pipeline is not used.
            # Gemini vision/text extraction is the single source of truth.
            parsed_result = {
                "sheets": [{
                    "sheet_name": "Gemini Extracted",
                    "sheet_index": 0,
                    "rows": [VIRTUAL_HEADERS],
                    "is_virtual": True,
                    "row_meta": [],
                }],
                "document_fields": {},
                "document_field_meta": {},
                "extracted_text": "",
                "extraction_summary": {"method": "pending_gemini"},
            }
            parsed = parsed_result["sheets"]
            
            parser_quality = {
                "reasons": [],
                "score": 0,
                "missing_supplier": True,
                "missing_date": True,
                "missing_currency": True,
                "product_rows": 0,
                "total_commercial_rows": 0,
                "bad_product_names": 0,
                "missing_prices_ratio": 1,
                "page_numbers": [],
                "extracted_text_length": 0,
            }
            gemini_reason = "gemini_only"

            start = time.monotonic()
            try:
                
                cached_summary = batch.extraction_summary or {}
                cached_raw_result = cached_summary.get("gemini_raw_result")
                cache_is_current = cached_summary.get("pipeline_version") == EXTRACTION_PIPELINE_VERSION

                # The upload path persists the finalized virtual sheet as the
                # canonical preview input.  A raw-result cache can survive a
                # retry/import independently and may no longer describe those
                # rows, so never reuse it when its identity fields disagree
                # with the persisted virtual sheet.
                cached_virtual_rows = []
                cached_virtual_headers = []
                for cached_sheet in sheets:
                    rows = cached_sheet.cached_rows or []
                    headers = cached_sheet.cached_headers or []
                    if rows and headers and len(rows) > 1:
                        cached_virtual_rows = rows
                        cached_virtual_headers = headers
                        break
                # Virtual sheets already contain the finalized upload output.
                # Do not replay a separately persisted raw Gemini payload here:
                # it can belong to an older extraction/reconciliation pass even
                # when the upload rows themselves are current.  Re-extracting
                # through the normal orchestrator keeps preview/import aligned
                # with the current finalization pipeline.
                if cached_virtual_rows:
                    # Upload already produced and persisted the finalized
                    # virtual rows. Re-running Gemini during preview can make
                    # the same document non-deterministic and must not replace
                    # those rows. Reconstruct the structured result from the
                    # persisted virtual sheet instead.
                    header_index = {
                        str(header).strip(): index
                        for index, header in enumerate(cached_virtual_headers)
                    }
                    cached_items = []
                    for row in cached_virtual_rows[1:]:
                        if not any(value not in (None, "") for value in row):
                            continue
                        item = {
                            field: row[header_index[field]]
                            if field in header_index and header_index[field] < len(row)
                            else None
                            for field in (
                                "row_type", "product_name", "quantity",
                                "unit_of_measurement", "unit_price", "currency",
                                "line_total", "purchase_date", "source_reference",
                                "document_number", "supplier_name", "buyer_name",
                                "source_document_type", "source_document_number",
                                "source_category", "display_name", "extra_attributes",
                                "_source_currency_missing",
                            )
                        }
                        item["uom"] = item.get("unit_of_measurement")
                        from api.services.pdf_commercial_geometry import PROTECTED_FIELDS
                        for field in PROTECTED_FIELDS:
                            index = header_index.get(field)
                            if index is not None and index < len(row):
                                item[field] = row[index]
                        cached_items.append(item)
                    cached_raw_result = {
                        **(batch.document_metadata or {}),
                        "document_type": (batch.document_metadata or {}).get("document_type")
                        or (batch.extraction_summary or {}).get("document_type"),
                        "category": batch.category,
                        "items": cached_items,
                        "currency": (batch.document_metadata or {}).get("currency") or batch.default_currency,
                    }
                    cache_is_current = True
                    if cached_items and all(item.get('_physical_pdf_row') for item in cached_items):
                        cached_raw_result['_physical_pdf_complete'] = True
                    logger.info(
                        "VIRTUAL_SHEET_REUSED_AS_CANONICAL_UPLOAD_RESULT batch=%s file=%s rows=%s",
                        batch.id,
                        batch.uploaded_file.original_file_name,
                        len(cached_items),
                    )
                if cached_raw_result and cached_virtual_rows:
                    header_index = {
                        str(header).strip(): index
                        for index, header in enumerate(cached_virtual_headers)
                    }
                    identity_fields = ("product_name", "display_name", "extra_attributes")
                    cached_items = cached_raw_result.get("items") or []
                    persisted_items = []
                    for row in cached_virtual_rows[1:]:
                        if not any(value not in (None, "") for value in row):
                            continue
                        persisted_items.append({
                            field: row[header_index[field]]
                            if field in header_index and header_index[field] < len(row)
                            else None
                            for field in identity_fields
                        })
                    cache_matches_sheet = len(cached_items) == len(persisted_items)
                    if cache_matches_sheet:
                        for cached_item, persisted_item in zip(cached_items, persisted_items):
                            if any(
                                cached_item.get(field) != persisted_item.get(field)
                                for field in identity_fields
                            ):
                                cache_matches_sheet = False
                                break
                    if not cache_matches_sheet:
                        logger.warning(
                            "STALE_GEMINI_CACHE_CONTENT_IGNORED batch=%s file=%s",
                            batch.id,
                            batch.uploaded_file.original_file_name,
                        )
                        cached_raw_result = None

                if cached_raw_result and cache_is_current:
                    gemini_result = normalize_result(cached_raw_result)
                    logger.info("Gemini extraction reused from upload cache batch=%s", batch.id)
                else:
                    logger.warning("STALE_GEMINI_CACHE_IGNORED batch=%s file=%s cache_version=%r current_version=%s", batch.id, batch.uploaded_file.original_file_name, cached_summary.get("pipeline_version"), EXTRACTION_PIPELINE_VERSION)
                    extraction_service = ExtractionOrchestrator()
                    gemini_result = extraction_service.extract(
                        batch.uploaded_file.file,
                        batch.uploaded_file.original_file_name,
                        import_batch=batch,
                    )
                    
                    logger.info("Gemini extraction called fresh (no cache) batch=%s", batch.id)
                # ExtractionOrchestrator already finalized PDF recovery. Preview
                # consumes that result; replaying tariff recovery here replaced
                # source identities and attributes using price/date matches.
                gemini_validation = validate_gemini_extraction(gemini_result)


                for trace_index, trace_item in enumerate(gemini_result.get("items") or []):
                    logger.warning(
                        "FINAL_GEMINI batch=%s index=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                        batch.id, trace_index, trace_item.get("product_name"),
                        trace_item.get("display_name"), trace_item.get("extra_attributes"),
                        trace_item.get("quantity"), trace_item.get("uom"),
                        trace_item.get("unit_price"), trace_item.get("line_total"),
                    )

                parsed_result, gemini_validation, gemini_item_count = self._apply_gemini_result(
                    parsed_result,
                    gemini_result,
                    parser_quality,
                    gemini_reason,
                )
           
                parsed_result.setdefault("extraction_summary", {})
                parsed_result["extraction_summary"]["gemini_raw_result"] = gemini_result
                parsed_result["extraction_summary"]["pipeline_version"] = EXTRACTION_PIPELINE_VERSION
                parsed = parsed_result.get("sheets") or parsed
                for trace_index, virtual_row in enumerate((parsed[0].get("rows") or [])[1:] if parsed and parsed[0].get("is_virtual") else []):
                    trace_row = dict(zip(parsed[0]["rows"][0], virtual_row))
                    logger.warning(
                        "VIRTUAL_SHEET batch=%s index=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                        batch.id, trace_index, trace_row.get("product_name"),
                        trace_row.get("display_name"), trace_row.get("extra_attributes"),
                        trace_row.get("quantity"), trace_row.get("unit_of_measurement"),
                        trace_row.get("unit_price"), trace_row.get("line_total"),
                    )
                extraction_sheet = ((parsed.get("sheets") or parsed)[0] if isinstance(parsed, dict) else parsed[0]) if parsed else {}
                raw_item_count = len(gemini_result.get("items") or [])
                raw_extracted_count = int(
                    gemini_result.get("_raw_extracted_count")
                    or raw_item_count
                )
                deduplicated_item_count = int(
                    gemini_result.get("_deduplicated_item_count") or 0
                )
                validation_rejected_count = int(
                    extraction_sheet.get("validation_rejected_count") or 0
                )
                dropped_warnings = extraction_sheet.get("gemini_validation_warnings", [])
                parsed_result["extraction_summary"]["warnings"] = (
                    gemini_validation.get("warnings", [])
                )

                parsed_result["extraction_summary"]["dropped_reasons"] = (
                    dropped_warnings
                )

                parsed_result["extraction_summary"]["raw_item_count"] = (
                    raw_extracted_count
                )
                parsed_result["extraction_summary"]["raw_extracted_count"] = raw_extracted_count
                parsed_result["extraction_summary"]["deduplicated_item_count"] = deduplicated_item_count
                normalization_actions = list(
                    gemini_result.get("_normalization_actions") or []
                )
                normalization_actions.extend(
                    warning for warning in dropped_warnings
                    if warning in {
                        "adjustment_note_row_skipped",
                        "duplicate_commercial_row_removed",
                        "unit_price_defaulted_from_line_total",
                        "document_summary_row_removed",
                    }
                )
                parsed_result["extraction_summary"]["normalization_actions"] = list(
                    dict.fromkeys(normalization_actions)
                )
                parsed_result["extraction_summary"]["validation_rejected_count"] = validation_rejected_count

                parsed_result["extraction_summary"]["accepted_item_count"] = (
                    gemini_item_count
                )
                logger.info(
                    "Gemini extraction result batch=%s document_type=%s raw_items=%s accepted_items=%s suspicious=%s elapsed_ms=%s warnings=%s dropped_reasons=%s",
                    batch.id,
                    gemini_result.get("document_type"),
                    raw_extracted_count,
                    gemini_item_count,
                    len(gemini_validation.get("suspicious_items") or []),
                    int((time.monotonic() - start) * 1000),
                    gemini_validation.get("warnings", []),
                    dropped_warnings,
                )
                if validation_rejected_count > 0:
                    logger.warning(
                        "Gemini raw extraction produced %s items but only %s made it into the "
                        "virtual sheet for batch=%s — %s item(s) were silently dropped during "
                        "normalization. Raw items: %s",
                        raw_extracted_count, gemini_item_count, batch.id,
                        validation_rejected_count,
                        json.dumps(gemini_result.get("items"), default=str),
                    )
            except Exception as exc:
                logger.exception("Gemini extraction failed batch=%s error=%s", batch.id, exc)
                raise ImportServiceError(
                    "Extraction failed.",
                    {"file": [f"Gemini could not extract this document: {exc}"]},
                )

            # Supplier/date/currency come from Gemini only.
            # Manual override (if the user picked a supplier explicitly) still wins.
            latest_document_fields = parsed_result.get("document_fields") or {}
            latest_extraction_summary = parsed_result.get("extraction_summary") or {}

            stored_manual_supplier = (
                batch.supplier.supplier_name
                if batch.supplier_id
                else None
            ) or batch.supplier_name_from_file

            if stored_manual_supplier:
                latest_document_fields["supplier"] = stored_manual_supplier
                latest_document_fields["supplier_name"] = stored_manual_supplier
                parsed_result.setdefault("document_fields", {})
                parsed_result["document_fields"]["supplier"] = stored_manual_supplier
                parsed_result["document_fields"]["supplier_name"] = stored_manual_supplier
                structured_document = latest_extraction_summary.get("structured_document", {})
                structured_header = structured_document.setdefault("header", {})
                structured_header["supplier_name"] = stored_manual_supplier

            if latest_document_fields:
                batch.document_metadata = {
                    **(batch.document_metadata or {}),
                    **latest_document_fields,
                }

            if latest_extraction_summary:
                batch.extraction_summary = {
                    **(batch.extraction_summary or {}),
                    **latest_extraction_summary,
                }

            batch.save(
                update_fields=[
                    "document_metadata",
                    "extraction_summary",
                    "updated_at",
                ]
            )
            duplicate_service = DuplicateService()
            validation = ValidationService()
            resolver = ProductResolutionService()
            seen = set()
            rows_out = []
            totals = {"valid": 0, "warning": 0, "failed": 0, "duplicate": 0, "ready_rows": 0, "product_rows": 0, "service_rows": 0, "charge_rows": 0, "unknown_rows": 0, "ignored_rows": 0, "matched_rows": 0, "new_product_rows": 0, "suggested_match_rows": 0, "ambiguous_rows": 0, "invalid_rows": 0, "unique_new_products": 0}
            new_groups = set()
            with transaction.atomic():
                virtual_processed = False
                for sheet in sheets:
                    parsed_sheet = parsed[0] if len(parsed) == 1 and parsed[0].get("is_virtual") else parsed[sheet.sheet_index]
                    if parsed_sheet.get("is_virtual") and virtual_processed:
                        continue
                    virtual_processed = parsed_sheet.get("is_virtual")
                    rows = parsed_sheet["rows"]
                    mappings = list(sheet.column_mappings.filter(is_ignored=False))

                    if parsed_sheet.get("is_virtual"):
                        headers = [str(cell or "") for cell in rows[0]] if rows else []
                        mappings = [
                            SimpleNamespace(
                                uploaded_column_index=index,
                                system_field_name=header,
                            )
                            for index, header in enumerate(headers)
                            if header and header != "ignore"
                        ]
                    elif not mappings:
                        raise ImportServiceError("Column mappings are required before preview.")
                    row_meta = parsed_sheet.get("row_meta") or []
                    for offset, row in enumerate(rows[sheet.data_start_row_number - 1:], start=sheet.data_start_row_number):
                        if not any(cell not in (None, "") for cell in row):
                            continue
                        raw = {str(index): row[index] if index < len(row) else None for index in range(len(row))}
                        meta_index = offset - sheet.data_start_row_number
                        if 0 <= meta_index < len(row_meta):
                            raw["_extraction"] = row_meta[meta_index]
                        is_gemini_row = any(
                            isinstance(details, dict)
                            and (details.get("source") or {}).get("type") == "gemini"
                            for details in (raw.get("_extraction") or {}).values()
                        )
                        mapped = {mapping.system_field_name: row[mapping.uploaded_column_index] if mapping.uploaded_column_index < len(row) else None for mapping in mappings}
                        physical_source = dict(mapped) if mapped.get('_physical_pdf_row') else None
                        if not mapped.get("item_code") and mapped.get("part_number"):
                            mapped["item_code"] = mapped.get("part_number")
                        if not mapped.get("part_number") and mapped.get("item_code"):
                            mapped["part_number"] = mapped.get("item_code")
                        metadata = (
                            parsed_result.get("document_fields")
                            or batch.document_metadata
                            or {}
                        )

                        structured_document = (
                            parsed_result.get("extraction_summary")
                            or batch.extraction_summary
                            or {}
                        ).get(
                            "structured_document",
                            {},
                        )
                        structured_header = (
                            structured_document.get("header")
                            or {}
                        )

                        metadata_supplier = (
                            metadata.get("supplier")
                            or metadata.get("supplier_name")
                            or structured_header.get("supplier_name")
                        )

                        stored_supplier = (
                            batch.supplier.supplier_name
                            if batch.supplier_id
                            else None
                        )

                        
                        selected_supplier = stored_supplier or metadata_supplier

                        # Prefer supplier detected for this specific Gemini item/page.
                        # Use document-level supplier only as fallback.
                        row_supplier = (
                            mapped.get("supplier_name")
                            or mapped.get("supplier")
                        )

                        if row_supplier and self._is_reliable_supplier(row_supplier):
                            mapped["supplier"] = self._clean_document_supplier(row_supplier)
                        elif selected_supplier:
                            mapped["supplier"] = selected_supplier
                        if not mapped.get("purchase_date") and metadata.get("document_date"):
                            mapped["purchase_date"] = metadata.get("document_date")
                        if mapped.get("currency"):
                            mapped["currency"] = str(mapped.get("currency")).strip().upper()
                        remark_text = str(mapped.get("remarks") or "").strip()
                        product_text = str(mapped.get("product_name") or "").strip()

                        if remark_text and product_text and remark_text not in product_text:
                            mapped["product_name"] = f"{product_text} | {remark_text}"
                        mapped = self._build_display_name(mapped)
                        logger.warning(
                            "PREVIEW_MAPPED batch=%s row=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                            batch.id, offset, mapped.get("product_name"), mapped.get("display_name"),
                            mapped.get("extra_attributes"), mapped.get("quantity"), mapped.get("uom"),
                            mapped.get("unit_price"), mapped.get("line_total"),
                        )
                        mapped["source_file_name"] = batch.uploaded_file.original_file_name
                        # Prefer the commercial section/category detected for this exact row.
                        # Fall back to the uploaded document-level category only when missing.
                        mapped["category"] = (
                            mapped.get("source_category")
                            or mapped.get("source_document_type")
                            or metadata.get("category")
                            or batch.uploaded_file.document_type
                            or batch.category
                        )
                        row_type = str(mapped.get("row_type") or "PRODUCT").upper()
                        if row_type not in {"PRODUCT", "SERVICE", "CHARGE", "UNKNOWN", "IGNORE"}:
                            row_type = "UNKNOWN"
                        mapped["row_type"] = row_type
                        mapped["line_group_key"] = line_item_group_key(mapped, row_type)
                        display_context = {
                            "parent_category": mapped.get("parent_category"),
                            "display_name": mapped.get("display_name"),
                            "specification": mapped.get("specification"),
                            "model": mapped.get("model"),
                            "remark": mapped.get("remark"),
                            "note": mapped.get("note"),
                        }

                        if row_type == "PRODUCT":
                            totals["product_rows"] += 1
                            status, errors, warnings, mapped = validation.validate(mapped, None, row_type)
                            mapped = self._restore_display_context(mapped, display_context)
                            mapped = self._build_display_name(mapped)
                            logger.warning("PREVIEW_NORMALIZED batch=%s row=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r", batch.id, offset, mapped.get("product_name"), mapped.get("display_name"), mapped.get("extra_attributes"), mapped.get("quantity"), mapped.get("uom"), mapped.get("unit_price"), mapped.get("line_total"))
                        elif row_type == "SERVICE":
                            totals["service_rows"] += 1
                            status, errors, warnings, mapped = validation.validate(mapped, None, row_type)
                            mapped = self._restore_display_context(mapped, display_context)
                            mapped = self._build_display_name(mapped)
                        elif row_type == "CHARGE":
                            totals["charge_rows"] += 1
                            status, errors, warnings, mapped = validation.validate(mapped, None, row_type)
                            mapped = self._restore_display_context(mapped, display_context)
                            mapped = self._build_display_name(mapped)
                        elif row_type == "IGNORE":
                            totals["ignored_rows"] += 1
                            status, errors, warnings = ImportRowStatus.IGNORED, [], ["Ignored row skipped."]
                        else:
                            totals["unknown_rows"] += 1
                            status, errors, warnings = ImportRowStatus.FAILED, ["Unknown row type requires review."], []
                        fingerprint = duplicate_service.fingerprint(mapped, batch)
                        is_duplicate = fingerprint in seen
                        seen.add(fingerprint)
                        resolution = resolver.resolve_preview(mapped, supplier=batch.supplier) if row_type == "PRODUCT" else {"status": ProductMatchStatus.IGNORED, "product": None, "product_id": None, "match_method": "none", "confidence": 0, "suggestions": [], "group_key": ""}
                        logger.info(
                            "Product mapping batch=%s row=%s row_type=%s method=%s confidence=%s status=%s",
                            batch.id,
                            offset,
                            row_type,
                            resolution["match_method"],
                            resolution["confidence"],
                            resolution["status"],
                        )
                        supplier_for_duplicate = SupplierMatchingService().match(batch, mapped)
                        existing_duplicate = duplicate_service.existing_price_history(
                            product=resolution["product"],
                            supplier=supplier_for_duplicate,
                            batch=batch,
                            mapped=mapped,
                        ) if resolution["product"] else None
                        if existing_duplicate:
                            is_duplicate = True
                        row_status = ImportRowStatus.DUPLICATE if is_duplicate else status
                        if resolution["status"] == ProductMatchStatus.INVALID:
                            row_status = ImportRowStatus.FAILED
                            errors.append("Product identity is missing.")
                        if is_duplicate:
                            totals["duplicate"] += 1
                        elif row_status == ImportRowStatus.FAILED:
                            totals["failed"] += 1
                        elif row_status == ImportRowStatus.WARNING:
                            totals["warning"] += 1
                        else:
                            totals["valid"] += 1
                        logger.warning(
                            "BEFORE_IMPORTROW_SAVE batch=%s product=%r display=%r extra=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                            batch.id,
                            mapped.get("product_name"),
                            mapped.get("display_name"),
                            mapped.get("extra_attributes"),
                            mapped.get("quantity"),
                            mapped.get("uom"), mapped.get("unit_price"), mapped.get("line_total"),
                        )
                        if is_duplicate and row_type == "PRODUCT":
                            totals["ready_rows"] += 1
                        elif (not is_duplicate) and row_status != ImportRowStatus.FAILED and (
                            resolution["status"] in {ProductMatchStatus.MATCHED, ProductMatchStatus.SUGGESTED, ProductMatchStatus.NEW_PRODUCT, ProductMatchStatus.AMBIGUOUS}
                            or row_type in {"SERVICE", "CHARGE"}
                        ):
                            totals["ready_rows"] += 1
                        if resolution["status"] == ProductMatchStatus.MATCHED:
                            totals["matched_rows"] += 1
                        elif resolution["status"] == ProductMatchStatus.NEW_PRODUCT:
                            totals["new_product_rows"] += 1
                            new_groups.add(resolution["group_key"])
                        elif resolution["status"] == ProductMatchStatus.SUGGESTED:
                            totals["suggested_match_rows"] += 1
                        elif resolution["status"] == ProductMatchStatus.AMBIGUOUS:
                            totals["ambiguous_rows"] += 1
                        elif resolution["status"] == ProductMatchStatus.INVALID:
                            totals["invalid_rows"] += 1
                        if physical_source:
                            from api.services.pdf_commercial_geometry import PROTECTED_FIELDS
                            mapped.update({key: physical_source[key] for key in PROTECTED_FIELDS if key in physical_source})
                        normalized_for_save = dict(mapped)
                        assert normalized_for_save.get("display_name") == mapped.get("display_name")
                        assert normalized_for_save.get("product_name") == mapped.get("product_name")
                        assert normalized_for_save.get("extra_attributes") == mapped.get("extra_attributes")
                        import_row = ImportRow.objects.create(
                            import_batch=batch,
                            import_sheet=sheet,
                            row_number=offset,
                            raw_data=raw,
                            mapped_data=mapped,
                            normalized_data=normalized_for_save,
                            row_type=row_type,
                            validation_status=row_status,
                            validation_errors=errors,
                            validation_warnings=warnings,
                            is_duplicate=is_duplicate,
                            duplicate_reference=(f"price_history:{existing_duplicate.id}" if existing_duplicate else fingerprint) if is_duplicate else None,
                        )
                        stored_row = ImportRow.objects.get(id=import_row.id)
                        stored_payload = {
                            **(stored_row.raw_data or {}),
                            **(stored_row.mapped_data or {}),
                            **(stored_row.normalized_data or {}),
                        }
                        logger.warning(
                            "AFTER_IMPORTROW_SAVE row_id=%s product=%r display=%r extra=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                            stored_row.id,
                            stored_payload.get("product_name"),
                            stored_payload.get("display_name"),
                            stored_payload.get("extra_attributes"),
                            stored_payload.get("quantity"),
                            stored_payload.get("uom"), stored_payload.get("unit_price"), stored_payload.get("line_total"),
                        )
                        ProductMatchResult.objects.create(
                            import_row=import_row,
                            imported_item_code=mapped.get("item_code"),
                            imported_part_number=mapped.get("part_number"),
                            imported_description=mapped.get("product_name"),
                            imported_brand=mapped.get("brand"),
                            suggested_product=resolution["product"],
                            confirmed_product=resolution["product"] if resolution["status"] == ProductMatchStatus.MATCHED else None,
                            match_score=resolution["confidence"],
                            match_method=resolution["match_method"],
                            status=resolution["status"],
                            remarks=json.dumps({"group_key": resolution["group_key"], "suggestions": resolution["suggestions"]}),
                        )
                        if len(rows_out) < limit:
                            rows_out.append(self._preview_row(import_row))
                totals["unique_new_products"] = len(new_groups)
                totals["document_validation"] = self._validate_document_totals(batch, rows_out)
                batch.total_rows = ImportRow.objects.filter(import_batch=batch).count()
                batch.failed_rows = totals["failed"]
                batch.warning_rows = totals["warning"]
                batch.duplicate_rows = totals["duplicate"]
                batch.status = ImportBatchStatus.PREVIEW_READY
                batch.save(update_fields=["total_rows", "failed_rows", "warning_rows", "duplicate_rows", "status", "updated_at"])
            return {"batch_id": batch.id, "summary": totals, "rows": rows_out,"document_fields": batch.document_metadata,}
        def _restore_display_context(self, mapped, source):
            mapped = dict(mapped or {})
            source = source or {}

            for field in (
                "parent_category",
                "display_name",
                "specification",
                "model",
                "remark",
                "note",
                 "extra_attributes",
            ):
                source_value = source.get(field)

                if source_value not in (None, ""):
                    mapped[field] = source_value

            return self._build_display_name(mapped)

        def _build_display_name(self, mapped):
            mapped = dict(mapped or {})
            from api.services.commercial_attribute_policy import hidden_display_attribute as commercial_field, clean_commercial_display
            mapped = clean_commercial_display(mapped)

            supplier_text = str(
                mapped.get("supplier")
                or mapped.get("supplier_name")
                or ""
            ).strip()
            brand_text = str(mapped.get("brand") or "").strip()

            if (
                brand_text
                and supplier_text
                and brand_text.lower() == supplier_text.lower()
            ):
                mapped["brand"] = None

            parent_text = str(mapped.get("parent_category") or "").strip()
            product_text = str(mapped.get("product_name") or "").strip()
            display_text = str(mapped.get("display_name") or "").strip()
            # Deterministic spreadsheet rows commonly carry a stale display
            # value equal to product_name. Rebuild that value so the verified
            # section/parent is visible in review. Preserve a richer display
            # string when it already contains source identity text.
            if display_text and not (
                parent_text
                and product_text
                and display_text.casefold() == product_text.casefold()
                and parent_text.casefold() not in display_text.casefold()
            ):
                mapped["display_name"] = display_text
                return mapped
            extra_attributes = mapped.get("extra_attributes") or []
            specification_text = str(mapped.get("specification") or "").strip()
            plain_specification = (
                specification_text
                and ":" not in specification_text
                and " | " not in specification_text
                and len(specification_text) <= 50
            )

            parts = []

            if parent_text and product_text and parent_text.lower() not in product_text.lower():
                parts.append(parent_text)

            if product_text:
                parts.append(product_text)

            # Item-specific source columns are carried through the generic
            # extra_attributes contract. Deprecated dedicated fields remain
            # in the mapper for DB/API compatibility but never affect display.
            standard_attributes = []
            has_structured_context = bool(
                parent_text
                or extra_attributes
            )
            supplier_values = {
                str(mapped.get("supplier") or "").strip().lower(),
                str(mapped.get("supplier_name") or "").strip().lower(),
            }

            product_lower = product_text.lower()

            for label, value in standard_attributes:
                value_text = str(value or "").strip(" |:-")

                if not value_text:
                    continue

                value_lower = value_text.lower()

                if label == "Brand" and value_lower in supplier_values:
                    continue

                # If Gemini split brand/model/spec from the same product name,
                # do not append it again. Only append attributes that add new text.
                if value_lower and value_lower in product_lower:
                    continue

                text = f"{label}: {value_text}"

                if text.lower() not in " | ".join(parts).lower():
                    parts.append(text)

            hidden_display_attribute_labels = {
                "rate basis",
                "additional rate",
                "applies after",
                "vat", "vat %", "vat amount", "tax", "tax %", "gst",
                "discount", "discount %", "subtotal", "total", "total amount",
                "grand total", "net amount", "line amount", "extended amount",
                "lead time", "leadtime", "lead period",
            }

            for attr in extra_attributes:
                if not isinstance(attr, dict):
                    continue

                label = str(attr.get("label") or "").strip(" |:-")
                value = str(attr.get("value") or "").strip(" |:-")

                if commercial_field(label) or label.lower() in hidden_display_attribute_labels:
                    continue

                if label and value:
                    text = f"{label}: {value}"
                    if text.lower() not in " | ".join(parts).lower():
                        parts.append(text)

            mapped["display_name"] = " | ".join(parts) or product_text
            return mapped

        def _restore_from_cached_extraction(self, row, mapped):
            mapped = dict(mapped or {})

            if mapped.get("parent_category") and mapped.get("display_name"):
                return self._build_display_name(mapped)

            try:
                extraction = row.import_batch.extraction_summary or {}
            except Exception:
                extraction = {}

            raw_result = extraction.get("gemini_raw_result") or {}
            items = raw_result.get("items") or []

            if not items:
                return self._build_display_name(mapped)

            def norm(value):
                return re.sub(
                    r"\s+",
                    " ",
                    str(value or "").strip().lower(),
                )

            def money_key(value):
                try:
                    return str(Decimal(str(value).replace(",", "")).normalize())
                except Exception:
                    return norm(value)

            mapped_product = norm(mapped.get("product_name"))
            mapped_supplier = norm(
                mapped.get("supplier_name")
                or mapped.get("supplier")
            )
            mapped_source = norm(mapped.get("source_reference"))
            mapped_price = money_key(mapped.get("unit_price"))
            mapped_total = money_key(mapped.get("line_total"))

            best_item = None
            best_score = 0

            for item in items:
                score = 0

                if mapped_product and mapped_product == norm(item.get("product_name")):
                    score += 4

                item_supplier = norm(
                    item.get("supplier_name")
                    or item.get("supplier")
                )

                if mapped_supplier and mapped_supplier == item_supplier:
                    score += 3

                if mapped_source and mapped_source == norm(item.get("source_reference")):
                    score += 3

                if mapped_price and mapped_price == money_key(item.get("unit_price")):
                    score += 2

                if mapped_total and mapped_total == money_key(
                    item.get("line_total") or item.get("total")
                ):
                    score += 2

                if score > best_score:
                    best_score = score
                    best_item = item

            if best_item and best_score >= 7:
                for field in (
                    "parent_category",
                    "display_name",
                    "specification",
                    "model",
                    "remark",
                    "note",
                    "extra_attributes",
                ):
                    value = best_item.get(field)

                    if value not in (None, ""):
                        mapped[field] = value

            return self._build_display_name(mapped)

        def _restore_msg_decimal_price(self, row, mapped):
            mapped = dict(mapped or {})

            try:
                uploaded_file = row.import_batch.uploaded_file
                original_file_name = uploaded_file.original_file_name
            except Exception:
                return mapped

            if not str(original_file_name or "").lower().endswith(".msg"):
                return mapped

            try:
                from api.services.gemini_extraction_service import (
                    _extract_msg_priced_table_items,
                    _recover_msg_decimal_price,
                )

                import extract_msg

                message = extract_msg.Message(uploaded_file.file.path)
                msg_body = message.body or ""
            except Exception:
                return mapped

            def norm(value):
                return re.sub(
                    r"\s+",
                    " ",
                    str(value or "").strip().lower(),
                )

            def same_money(left, right):
                try:
                    return Decimal(str(left)) == Decimal(str(right))
                except Exception:
                    return False

            table_items = _extract_msg_priced_table_items(
                msg_body,
                mapped.get("supplier_name") or mapped.get("supplier"),
                mapped.get("purchase_date") or mapped.get("document_date"),
            )

            mapped_name = norm(
                mapped.get("display_name")
                or mapped.get("product_name")
                or mapped.get("description")
            )
            mapped_qty = mapped.get("quantity")
            mapped_total = mapped.get("line_total")

            for item in table_items:
                item_name = norm(
                    item.get("display_name")
                    or item.get("product_name")
                )

                if (
                    mapped_name == item_name
                    and same_money(mapped_qty, item.get("quantity"))
                    and (
                        not mapped_total
                        or same_money(mapped_total, item.get("line_total"))
                    )
                ):
                    mapped["unit_price"] = item.get("unit_price")
                    mapped["line_total"] = item.get("line_total")
                    mapped["_unit_price_verified_from_msg"] = True
                    return mapped

            current_price = mapped.get("unit_price")
            corrected_price = _recover_msg_decimal_price(
                current_price,
                msg_body,
                mapped.get("currency"),
            )

            if corrected_price == current_price:
                return mapped

            mapped["unit_price"] = corrected_price
            mapped["_unit_price_verified_from_msg"] = True

            quantity = parse_quantity_decimal(
                mapped.get("quantity")
            )

            if (
                quantity is not None
                and not mapped.get("line_total")
            ):
                mapped["line_total"] = str(
                    quantity * Decimal(str(corrected_price))
                )

            return mapped

        def _row_import_data(self, row):
            mapped = {
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }

            if mapped.get('_physical_pdf_row'):
                return mapped
            mapped = self._restore_from_cached_extraction(row, mapped)
            mapped = self._restore_msg_decimal_price(row, mapped)

            return mapped
        def start(self, batch, sheet_ids, *, review_confirmed=False):
            from api.services.visual_review_policy import batch_visual_review_reasons
            if not review_confirmed:
                visual_reasons = batch_visual_review_reasons(batch)
                if visual_reasons:
                    raise ImportServiceError('Manual review required before import.',
                                             errors={'review_reasons': visual_reasons})
            duplicate_service = DuplicateService()
            service_charge_rows = list(ImportRow.objects.filter(
                import_batch=batch,
                import_sheet_id__in=sheet_ids,
                is_duplicate=False,
                row_type__in=["SERVICE", "CHARGE"],
                is_imported=False,
            ).exclude(validation_status=ImportRowStatus.FAILED))
            duplicate_product_rows = list(ImportRow.objects.filter(
                import_batch=batch,
                import_sheet_id__in=sheet_ids,
                is_duplicate=True,
                row_type="PRODUCT",
                is_imported=False,
            ).exclude(validation_status=ImportRowStatus.FAILED))
            rows = list(ImportRow.objects.filter(import_batch=batch, import_sheet_id__in=sheet_ids, is_duplicate=False, row_type="PRODUCT").exclude(validation_status=ImportRowStatus.FAILED).select_related("product_match"))
            resolver = ProductResolutionService()
            grouped = defaultdict(list)
            for row in rows:
                # Rows manually added during Review do not have the one-to-one
                # ProductMatchResult created by the normal preview pipeline.
                # Treat them as new-product candidates instead of raising
                # RelatedObjectDoesNotExist during review import.
                try:
                    match = row.product_match
                except ProductMatchResult.DoesNotExist:
                    match = ProductMatchResult.objects.create(
                        import_row=row,
                        imported_description=(row.mapped_data or {}).get("product_name"),
                        status=ProductMatchStatus.NEW_PRODUCT,
                    )
                if match.status == ProductMatchStatus.MATCHED and match.confirmed_product_id:
                    grouped[f"existing:{match.confirmed_product_id}"].append(row)
                elif match.status in {ProductMatchStatus.SUGGESTED, ProductMatchStatus.NEW_PRODUCT, ProductMatchStatus.AMBIGUOUS}:
                    grouped[group_key(row.mapped_data)].append(row)
            records = []
            imported_rows = []
            created_products = 0
            now = timezone.now()
            with transaction.atomic():
                batch.status = ImportBatchStatus.PROCESSING
                batch.processing_started_at = now
                batch.save(update_fields=["status", "processing_started_at", "updated_at"])
                for key, group_rows in grouped.items():
                    if key.startswith("existing:"):
                        product = group_rows[0].product_match.confirmed_product
                        created = False
                    else:
                        product, created = resolver.get_or_create_for_group(group_rows, batch)
                    if created:
                        created_products += 1
                    for row in group_rows:
                        mapped = self._row_import_data(row)
                        logger.warning(
                            "IMPORT_START_ROW row_id=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                            row.id, mapped.get("product_name"), mapped.get("display_name"),
                            mapped.get("extra_attributes"), mapped.get("quantity"), mapped.get("uom"),
                            mapped.get("unit_price"), mapped.get("line_total"),
                        )
                        row.mapped_data = {**(row.mapped_data or {}), **mapped}
                        row.normalized_data = {**(row.normalized_data or {}), **mapped}
                        row.save(
                            update_fields=[
                                "mapped_data",
                                "normalized_data",
                                "updated_at",
                            ]
                        )
                        logger.warning(
                            "IMPORT_ROW_PERSISTED row_id=%s product_name=%r display_name=%r extra_attributes=%r quantity=%r uom=%r unit_price=%r line_total=%r",
                            row.id, row.normalized_data.get("product_name"), row.normalized_data.get("display_name"),
                            row.normalized_data.get("extra_attributes"), row.normalized_data.get("quantity"),
                            row.normalized_data.get("uom"), row.normalized_data.get("unit_price"),
                            row.normalized_data.get("line_total"),
                        )
                        line_total = mapped.get("line_total")
                        quantity = mapped.get("quantity")
                        unit_price = mapped.get("unit_price")

                        try:
                            qty_decimal = Decimal(str(quantity or "0"))
                            unit_decimal = Decimal(str(unit_price or "0"))
                            total_decimal = Decimal(str(line_total or "0"))
                            looks_derived = bool(re.search(r"\.\d{3,}$", str(unit_price or "")))
                            expected = qty_decimal * unit_decimal
                            tolerance = max(Decimal("0.05"), abs(total_decimal) * Decimal("0.001"))

                            if (
                                not mapped.get("_physical_pdf_row")
                                and qty_decimal > 1
                                and looks_derived
                                and not mapped.get("_unit_price_verified_from_msg")
                                and abs(expected - total_decimal) <= tolerance
                            ):
                                mapped = {**mapped, "unit_price": str(total_decimal)}
                                row.mapped_data = {**(row.mapped_data or {}), **mapped}
                                row.normalized_data = {**(row.normalized_data or {}), **mapped}
                                row.save(update_fields=["mapped_data", "normalized_data", "updated_at"])
                        except Exception:
                            pass

                        raw_price = mapped.get("unit_price")
                        if raw_price in (None, ""):
                            row.validation_status = ImportRowStatus.FAILED
                            row.validation_errors = ["Unit price is required before importing this row."]
                            row.validation_warnings = []
                            row.save(update_fields=["validation_status", "validation_errors", "validation_warnings", "updated_at"])
                            logger.warning("IMPORT_ROW_SKIPPED row_id=%s reason=missing_unit_price", row.id)
                            continue
                        try:
                            price = Decimal(str(raw_price).replace(",", "").strip())
                        except (InvalidOperation, ValueError):
                            row.validation_status = ImportRowStatus.FAILED
                            row.validation_errors = [f"Invalid unit price: {raw_price}"]
                            row.validation_warnings = []
                            row.save(update_fields=["validation_status", "validation_errors", "validation_warnings", "updated_at"])
                            logger.warning("IMPORT_ROW_SKIPPED row_id=%s reason=invalid_unit_price value=%r", row.id, raw_price)
                            continue
                        exchange = Decimal(str(mapped.get("exchange_rate") or "1"))
                        supplier = SupplierMatchingService().match(batch, mapped)
                        existing_duplicate = duplicate_service.existing_price_history(
                            product=product,
                            supplier=supplier,
                            batch=batch,
                            mapped=mapped,
                        )
                        if existing_duplicate:
                            row.is_duplicate = True
                            row.duplicate_reference = f"price_history:{existing_duplicate.id}"
                            row.validation_status = ImportRowStatus.DUPLICATE
                            row.save(update_fields=["is_duplicate", "duplicate_reference", "validation_status", "updated_at"])
                            continue
                        resolver.update_alias(product, supplier, mapped, batch)
                        source_reference = mapped.get("source_reference") or mapped.get("lpo_reference") or mapped.get("grn_reference") or mapped.get("quotation_reference")
                        records.append(PriceHistory(
                            product=product,
                            supplier=supplier,
                            import_batch=batch,
                            import_row=row,
                            source_type=batch.category,
                            source_reference=source_reference,
                            source_file_name=batch.uploaded_file.original_file_name,
                            purchase_date=mapped.get("purchase_date") or None,
                            quantity=Decimal(str(mapped.get("quantity") or "1")),
                            unit_of_measurement=mapped.get("unit_of_measurement"),
                            original_unit_price=price,
                            currency=mapped.get("currency") or batch.default_currency,
                            exchange_rate=exchange,
                            converted_unit_price=price * exchange,
                            purchase_location=mapped.get("purchase_location"),
                            lpo_reference=mapped.get("lpo_reference"),
                            grn_reference=mapped.get("grn_reference"),
                            quotation_reference=mapped.get("quotation_reference"),
                            remarks=mapped.get("remarks"),
                            created_by=batch.uploaded_by,
                            is_active=True,
                        ))
                        row.product_match.confirmed_product = product
                        row.product_match.status = ProductMatchStatus.MATCHED
                        row.product_match.save(update_fields=["confirmed_product", "status", "updated_at"])
                        imported_rows.append(row.id)
                PriceHistory.objects.bulk_create(records)
                for row in service_charge_rows:
                    data = self._row_import_data(row)
                    data["row_type"] = row.row_type
                    data["line_group_key"] = line_item_group_key(data, row.row_type)
                    row.normalized_data = data
                    row.mapped_data = {**(row.mapped_data or {}), **data}
                    row.save(update_fields=["normalized_data", "mapped_data", "updated_at"])
                for row in duplicate_product_rows:
                    data = self._row_import_data(row)
                    data["row_type"] = row.row_type
                    data["line_group_key"] = data.get("line_group_key") or line_item_group_key(data, row.row_type)
                    row.normalized_data = data
                    row.mapped_data = {**(row.mapped_data or {}), **data}
                    row.save(update_fields=["normalized_data", "mapped_data", "updated_at"])
                service_charge_ids = [row.id for row in service_charge_rows]
                duplicate_product_ids = [row.id for row in duplicate_product_rows]
                ImportRow.objects.filter(id__in=imported_rows + service_charge_ids + duplicate_product_ids).update(is_imported=True, imported_at=now, validation_status=ImportRowStatus.IMPORTED)
                batch.imported_rows = len(records) + len(service_charge_ids) + len(duplicate_product_ids)
                batch.duplicate_rows = ImportRow.objects.filter(import_batch=batch, is_duplicate=True).count()
                batch.completed_at = now
                skipped = len(rows) - len(records)
                batch.status = ImportBatchStatus.COMPLETED_WITH_ERRORS if skipped or batch.failed_rows or batch.duplicate_rows else ImportBatchStatus.COMPLETED
                batch.save(update_fields=["imported_rows", "duplicate_rows", "completed_at", "status", "updated_at"])
            return {"batch_id": batch.id, "imported_rows": len(records) + len(service_charge_rows) + len(duplicate_product_rows), "price_history_rows": len(records), "history_only_rows": len(service_charge_rows) + len(duplicate_product_rows), "created_products": created_products, "status": batch.status}

        def _validate_document_totals(self, batch, rows):
            metadata = batch.document_metadata or {}
            try:
                grand_total = Decimal(str(metadata.get("grand_total"))) if metadata.get("grand_total") not in (None, "") else None
            except Exception:
                grand_total = None
            line_total = Decimal("0")
            for row in rows:
                mapped = row.get("mapped_data") or {}
                if row.get("row_type") in {"PRODUCT", "SERVICE", "CHARGE"}:
                    value = mapped.get("line_total")
                    if value not in (None, ""):
                        try:
                            line_total += Decimal(str(value).replace(",", ""))
                        except Exception:
                            pass
            if grand_total is None or line_total == 0:
                return {"status": "not_checked", "message": "Document total could not be checked."}
            delta = abs(grand_total - line_total)
            tolerance = max(Decimal("1.00"), grand_total * Decimal("0.02"))
            if delta <= tolerance:
                return {"status": "valid", "message": "Line totals are within tolerance.", "difference": str(delta)}
            return {"status": "needs_review", "message": "Line totals do not match grand total.", "difference": str(delta)}

        def _preview_row(self, row):
            match = getattr(row, "product_match", None)
            product_status = match.status if match else "invalid"
            try:
                match_meta = json.loads(match.remarks or "{}") if match else {}
            except (TypeError, ValueError):
                match_meta = {}
            is_importable = (not row.is_duplicate) and row.validation_status != ImportRowStatus.FAILED and (
                product_status in {ProductMatchStatus.MATCHED, ProductMatchStatus.SUGGESTED, ProductMatchStatus.NEW_PRODUCT, ProductMatchStatus.AMBIGUOUS}
                or row.row_type in {"SERVICE", "CHARGE"}
            )
            return {
                "row_id": row.id,
                "row_number": row.row_number,
                "mapped_data": self._row_import_data(row),
                "normalized_data": self._row_import_data(row),
                "row_type": row.row_type,
                "validation_status": row.validation_status,
                "validation_errors": row.validation_errors,
                "validation_warnings": row.validation_warnings,
                "is_duplicate": row.is_duplicate,
                "is_importable": is_importable,
                "duplicate_reference": row.duplicate_reference,
                "product_match": {
                    "status": product_status,
                    "product_id": match.confirmed_product_id if match else None,
                    "match_method": match.match_method if match else "none",
                },
                "product_resolution": {
                    "status": product_status,
                    "product_id": match.confirmed_product_id if match else None,
                    "match_method": match.match_method if match else "none",
                    "confidence": float(match.match_score or 0) if match else 0,
                    "group_key": match_meta.get("group_key"),
                    "suggestions": match_meta.get("suggestions", []),
                    "proposed_product_name": row.mapped_data.get("product_name"),
                },
            }
