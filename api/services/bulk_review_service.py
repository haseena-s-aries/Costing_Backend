from api.models import ImportRow, ImportRowStatus


class BulkReviewService:
    def evaluate(self, batch):
        reasons = []

        metadata = batch.document_metadata or {}
        extraction = batch.extraction_summary or {}

        supplier = (
            metadata.get("supplier_name")
            or metadata.get("supplier")
            or batch.supplier_name_from_file
            or ""
        ).strip()

        rows = ImportRow.objects.filter(
            import_batch=batch,
        )

        row_supplier_found = False

        for row in rows.iterator():
            row_data = {
                **(row.raw_data or {}),
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }
            row_supplier = str(
                row_data.get("supplier")
                or row_data.get("supplier_name")
                or ""
            ).strip()

            if row_supplier and row_supplier.upper() != "UNKNOWN":
                row_supplier_found = True
                break

        if (
            not supplier
            or supplier.upper() == "UNKNOWN"
        ) and not row_supplier_found:
            reasons.append("Supplier name missing")

        document_date = (
            metadata.get("document_date")
            or ""
        )

        row_date_found = False

        for row in rows.iterator():
            row_data = {
                **(row.raw_data or {}),
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }
            row_date = str(
                row_data.get("purchase_date")
                or row_data.get("document_date")
                or ""
            ).strip()

            if row_date:
                row_date_found = True
                break

        from api.services.import_service import build_review_reasons
        if 'Document date missing' in build_review_reasons(batch):
            reasons.append("Document date missing")

        currency = (
            metadata.get("currency")
            or ""
        ).strip()

        row_currency_found = False

        for row in rows.iterator():
            row_data = {
                **(row.raw_data or {}),
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }
            row_currency = str(
                row_data.get("currency")
                or ""
            ).strip()

            if row_currency:
                row_currency_found = True
                break

        from api.services.import_service import build_review_reasons
        currency_missing = "Currency missing" in build_review_reasons(batch)
        if currency_missing:
            reasons.append("Currency missing")

        raw_gemini = (
            extraction.get("gemini_raw_result")
            or {}
        )
        if 'handwritten_content_requires_review' in (raw_gemini.get('_review_reasons') or []):
            reasons.append('Handwritten content — verify all extracted fields')
        if 'scanned_content_requires_review' in (raw_gemini.get('_review_reasons') or []):
            reasons.append('Scanned document — verify extracted text and amounts')
        import_warnings = extraction.get("warnings") or []
        dropped_reasons = extraction.get("dropped_reasons") or []

        ignored_warning_tokens = (
            "line_total_includes_tax",
            "unit_price_defaulted_from_line_total",
            "possible_vat_or_total_bound_as_line_total",
            "Qty x Unit Price does not match Line Total",
            "Line total does not match quantity x unit price",
            "gemini_divided_single_visible_amount_corrected",
            "row will be stored in import history",
            "adjustment_note_row_skipped",
            "tax_or_total_row_skipped",
            "summary_or_non_reusable_service_skipped",
            "document_summary_amount_dropped",
            "discount_row_skipped",
            "zero_value_row_skipped",
            "no_amount_dropped",
            "permit_non_commercial_row_skipped",
            "non_commercial_arabic_template_row_skipped",
            "non_commercial_template_row_skipped",
        )

        critical_import_warnings = [
            str(value)
            for value in import_warnings
            if not any(token in str(value) for token in ignored_warning_tokens)
            and (str(value) != "missing_currency" or currency_missing)
        ]

        critical_dropped_reasons = [
            str(value)
            for value in dropped_reasons
            if not any(token in str(value) for token in ignored_warning_tokens)
            and "Gemini counted" not in str(value)
        ]

        if critical_import_warnings or critical_dropped_reasons:
            reasons.append("Extraction validation needs review")
        raw_item_count = extraction.get("raw_extracted_count")
        if raw_item_count is None:
            raw_item_count = len(raw_gemini.get("items") or [])
        accepted_item_count = extraction.get("accepted_item_count")
        if accepted_item_count is None:
            accepted_item_count = sum(max(sheet.total_rows or 0, 0) for sheet in batch.sheets.all())
        validation_rejected_count = extraction.get("validation_rejected_count")
        if validation_rejected_count is None:
            validation_rejected_count = max(
                int(raw_item_count or 0) - int(accepted_item_count or 0),
                0,
            )
        actual_dropped_count = max(int(validation_rejected_count or 0), 0)

        if (
            actual_dropped_count > 0
        ):
            reasons.append("Some extracted rows were dropped during validation")
        if raw_gemini.get("_gemini_needs_review") or raw_gemini.get("_gemini_quality_issues"):
            reasons.append("Extraction needs review")
        missing_pages = raw_gemini.get(
            "_gemini_missing_pages"
        ) or []

        if missing_pages:
            reasons.append(
                f"Missing page extraction: {missing_pages}"
            )

        if not rows.exists():
            reasons.append("No commercial rows extracted")

        failed_count = rows.filter(
            validation_status=ImportRowStatus.FAILED
        ).count()

        if failed_count:
            reasons.append(
                f"{failed_count} row(s) failed validation"
            )

        currency_defaulted = False
        row_quality_warning = False

        for row in rows.iterator():
            warnings = row.validation_warnings or []

            critical_warnings = [
                str(value)
                for value in warnings
                if not any(token in str(value) for token in ignored_warning_tokens)
            ]

            if critical_warnings:
                row_quality_warning = True

            if any(
                "currency_missing_from_source" in str(value)
                or "currency_defaulted_not_detected" in str(value)
                for value in warnings
            ):
                currency_defaulted = True

        if row_quality_warning:
            reasons.append("One or more rows need review")

        if currency_defaulted:
            reasons.append(
                "Currency was not detected from source document"
            )

        return {
            "requires_review": bool(reasons),
            "reasons": reasons,
        }
