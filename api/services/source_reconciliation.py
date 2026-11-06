"""Reconcile alternate extraction passes using explicit source facts."""
import io
import re
import unicodedata
from copy import deepcopy
from decimal import Decimal, InvalidOperation


def text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def number(value):
    try:
        value = Decimal(str(value).replace(",", ""))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def name_key(value):
    value = text(value).lower()
    value = re.sub(r"\blump\s*sum\b|\bper\s+(?:hr|hour)\b", " ", value)
    value = re.sub(r"\b(?:charges?\s+for)\b", " ", value)
    value = re.sub(r"\bthere\s+after\b", " ", value)
    value = re.sub(r"\([^)]*(?:USD|AED|EUR|GBP|/hr)[^)]*\)", " ", value, flags=re.I)
    return re.sub(r"[^\w]+", " ", value).strip()


def _verified_pdf_identity(source_context, fallback_name):
    """Return source-backed identity and explicitly labelled attributes.

    Text-line recovery must not infer a semantic label from an identifier's
    shape.  Only labels that are visibly present in the source line are
    eligible for attributes; text before the first commercial price remains
    the product identity.
    """
    source = text(source_context)
    if not source:
        return text(fallback_name), set()
    identity_source = re.sub(r"^\s*\d+\s*[、.)\-:]\s*", "", source).strip()
    price = re.search(
        r"\b(?:AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|US\$|\$)\s*\d",
        identity_source, re.I,
    )
    identity = identity_source[:price.start()] if price else identity_source
    identity = re.sub(r"\s+", " ", identity).strip(" -–—:.,")
    return identity or text(fallback_name), {
        re.sub(r"\s+", " ", match.group(1)).strip().casefold()
        for match in re.finditer(
            r"\b(P/?N|Part\s+Number|Part\s+No\.?|Model(?:\s+No\.?)?|Material|Specification)\s*:",
            source, re.I,
        )
    }


def _clean_verified_pdf_row(row, source_context=None):
    """Apply authoritative PDF text-line identity without semantic guessing."""
    source = source_context or row.get("source_context")
    if not row.get("_source_fact_verified") or not source:
        return row
    identity, explicit_labels = _verified_pdf_identity(source, row.get("product_name"))
    if identity:
        row["product_name"] = identity
    attrs = []
    for attr in row.get("extra_attributes") or []:
        if not isinstance(attr, dict):
            continue
        label = text(attr.get("label"))
        normalized = re.sub(r"\s+", " ", label).strip().casefold()
        # A Part Number/Model attribute is valid only when that label is
        # visibly present in the source context. Other labels remain intact.
        if normalized in {"part number", "part no.", "p/n", "p n", "model", "model no.", "material", "specification"}:
            if normalized in {"part number", "part no.", "p/n", "p n"}:
                label_is_explicit = bool(explicit_labels & {"part number", "part no.", "p/n", "p n"})
            elif normalized in {"model", "model no."}:
                label_is_explicit = bool(explicit_labels & {"model", "model no."})
            else:
                label_is_explicit = normalized in explicit_labels
            if not label_is_explicit:
                continue
        attrs.append(attr)
    row["extra_attributes"] = attrs
    row["source_context"] = source
    return row


def uom_key(value):
    value = text(value).casefold()
    return "hr" if value in {"hour", "hours", "hr", "hrs"} else value


def required_metadata(result):
    """Hard review requirements survive any confidence score."""
    from api.services.visual_review_policy import require_visual_review
    result = require_visual_review(result)
    from api.services.normalization import is_contact_only_supplier
    uncertain = any(item.get('_supplier_identity_uncertain') for item in result.get('items') or [])
    if uncertain:
        # A document-level buyer/default must not fill an unverified seller.
        result['supplier_name'] = result['supplier'] = None
        result['_review_reasons'] = list(dict.fromkeys([*(result.get('_review_reasons') or []), 'supplier_identity_uncertain']))
        for item in result.get('items') or []:
            if item.get('_supplier_identity_uncertain'):
                item['supplier_name'] = item['supplier'] = None
    for record in [result, *(result.get("items") or [])]:
        for field in ("supplier_name", "supplier"):
            if is_contact_only_supplier(record.get(field)):
                record[field] = None
    from api.services.commercial_attribute_policy import clean_commercial_display
    for item in result.get('items') or []:
        clean_commercial_display(item)
    from api.services.gemini_extraction_service import _valid_date, _valid_currency
    rejected_dates = set(result.get('_rejected_reference_dates') or [])
    for record in [result, *(result.get('items') or [])]:
        for field in ('document_date', 'purchase_date'):
            if str(_valid_date(record.get(field))) in rejected_dates:
                record[field] = None
    reasons = [reason for reason in (result.get("_review_reasons") or [])
               if reason not in {"missing_supplier_name", "missing_document_date", "missing_currency"}]
    priced_items = [
        item for item in result.get("items") or []
        if number(item.get("unit_price")) is not None
        or number(item.get("line_total")) is not None
    ]
    item_suppliers = [
        text(item.get("supplier_name") or item.get("supplier"))
        for item in priced_items
        if text(item.get("supplier_name") or item.get("supplier"))
        and text(item.get("supplier_name") or item.get("supplier")).upper()
        not in {"UNKNOWN", "OTHER"}
    ]
    # A comparison document legitimately has no single document supplier.
    # Require supplier completeness across priced rows instead.
    missing_item_supplier = not priced_items or len(item_suppliers) < len(priced_items)
    if (
        (not text(result.get("supplier_name")) or text(result.get("supplier_name")).upper() in {"UNKNOWN", "OTHER"})
        and missing_item_supplier
    ):
        reasons.append("missing_supplier_name")
    from api.services.row_metadata_policy import missing_row_metadata
    missing_metadata = missing_row_metadata(priced_items, result)
    if missing_metadata['date']:
        reasons.append("missing_document_date")
    if any((number(i.get("unit_price")) is not None or number(i.get("line_total")) is not None)
           and not (_valid_currency(i.get("currency")) or _valid_currency(result.get("currency")))
           for i in result.get("items") or []):
        reasons.append("missing_currency")
    result["_review_reasons"] = list(dict.fromkeys(reasons))
    if any(number(i.get("unit_price")) is None and number(i.get("line_total")) is not None
           and number(i.get("quantity")) is not None for i in priced_items):
        reasons.append("missing_unit_price")
        result["_review_reasons"] = list(dict.fromkeys(reasons))
    if reasons:
        result["_needs_review"] = True
        result["_gemini_needs_review"] = True
    return result


def finalize_msg_table(result, body, sent_date):
    """Recover complete cells from a numbered vertical quotation table."""
    from api.services.msg_commercial_history import split_thread, is_commercial, email_date
    from api.services.gemini_extraction_service import normalize_result
    blocks = split_thread(body, sent_date=sent_date)
    if not blocks:
        return result
    if any(is_commercial(b) for b in blocks[1:]):
        return result
    lines = [text(line) for line in blocks[0]["body"].splitlines() if text(line)]
    keys = [line.lower() for line in lines]
    header = ["s.n", "description", "unit", "unit price", "brand", "availability"]
    starts = [i for i in range(len(lines) - 5) if keys[i:i+6] == header]
    if len(starts) != 1:
        return result
    from api.services.gemini_extraction_service import _valid_currency
    explicit_currencies = {_valid_currency(token) for token in re.findall(r"\b[A-Z]{3}\b", blocks[0]["body"])} - {None, ""}
    currency = next(iter(explicit_currencies)) if len(explicit_currencies) == 1 else None
    rows, offset, serial = [], starts[0] + 6, 1
    while offset < len(lines) and lines[offset] == str(serial):
        end = next((i for i in range(offset + 1, len(lines)) if lines[i] == str(serial + 1)), len(lines))
        cells = lines[offset + 1:end]
        unit_index = next((i for i, cell in enumerate(cells) if re.fullmatch(r"\d+(?:\.\d+)?\s*[A-Za-z]+", cell)), None)
        if unit_index is None or unit_index == 0 or unit_index + 1 >= len(cells):
            return result
        rate = number(cells[unit_index + 1])
        if rate is None:
            if not re.fullmatch(r"no\s+stock|not\s+available", cells[unit_index + 1], re.I):
                return result
        else:
            if unit_index + 3 >= len(cells):
                return result
            qty, uom = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", cells[unit_index]).groups()
            rows.append(dict(row_type="PRODUCT", product_name=" ".join(cells[:unit_index]),
                             quantity=qty, uom=uom, unit_price=str(rate), line_total=str(Decimal(qty)*rate),
                             currency=currency, purchase_date=email_date(sent_date), page_number=None,
                             supplier_name=result.get("supplier_name"),
                             extra_attributes=[{"label": "Brand", "value": cells[unit_index + 2]},
                                               {"label": "Availability", "value": cells[unit_index + 3]}],
                             source_block_id="email:0", source_row_id=f"email:0:table:row:{serial}",
                             _source_fact_verified=True))
        serial += 1
        offset = end
    if rows:
        result.update(items=rows, document_date=email_date(sent_date), currency=currency,
                      _raw_extracted_count=len(rows), visible_priced_row_count=len(rows))
        return required_metadata(normalize_result(result))
    return result


def merge_recovery(existing, recovered):
    """Pair a unique primary row with a unique recovered fact on its page.

    Never pair two independently located rows. Never pair by amount alone.
    Source metadata is required for recovery; uncertain matches remain visible.
    """
    output = deepcopy(existing or [])
    used = set()
    for fact in recovered:
        matches = []
        for index, row in enumerate(output):
            if index in used:
                continue
            if row.get("source_row_id") and row.get("source_row_id") != fact.get("source_row_id"):
                continue
            if row.get("page_number") != fact.get("page_number"):
                continue
            if row.get("purchase_date") and row.get("purchase_date") != fact.get("purchase_date"):
                continue
            if row.get("supplier_name") and fact.get("supplier_name") and text(row["supplier_name"]).casefold() != text(fact["supplier_name"]).casefold():
                continue
            # A verified source recovery may reconcile a Gemini row whose
            # pass-specific block id differs. Unverified rows still require
            # the same block when both sides provide one.
            if (row.get("source_block_id") and fact.get("source_block_id")
                    and row["source_block_id"] != fact["source_block_id"]
                    and not fact.get("_source_fact_verified")):
                continue
            row_name = name_key(row.get("product_name"))
            fact_name = name_key(fact.get("product_name"))
            # Recovery may expose a fuller source identity than Gemini (or
            # Gemini may omit a part/model suffix).  With page, price,
            # supplier/date and quantity/UOM already constrained above, allow
            # containment so the recovery enriches the same physical row.
            if row_name != fact_name:
                row_words = set(row_name.split())
                fact_words = set(fact_name.split())
                if not (len(row_words) >= 2 and len(fact_words) >= 2
                        and (row_name in fact_name or fact_name in row_name)):
                    continue
            field = "line_total" if fact.get("_amount_semantics") == "total" else "unit_price"
            if number(row.get(field)) != number(fact.get(field)):
                continue
            # A source total may correct an erroneously inherited tariff currency.
            if field != "line_total" and row.get("currency") and row.get("currency") != fact.get("currency"):
                continue
            qty = number(row.get("quantity"))
            if qty is not None and qty != number(fact.get("quantity")):
                duration_values = [text(a.get("value")) for a in row.get("extra_attributes") or []
                                   if text(a.get("label")).casefold() == "duration"]
                duration_match = any(
                    (m := re.fullmatch(r"(\d+)\s*hrs?\s*(\d+)\s*min", value, re.I))
                    and qty == Decimal(m[1]) + Decimal(m[2]) / 60
                    for value in duration_values
                )
                if not (fact.get("_amount_semantics") == "total"
                        and (duration_match or fact.get("_source_fact_verified"))):
                    continue
            if row.get("uom") and fact.get("uom") and uom_key(row["uom"]) != uom_key(fact["uom"]):
                continue
            if field == "unit_price" and row.get("line_total") is not None and fact.get("line_total") is not None and number(row["line_total"]) != number(fact["line_total"]):
                continue
            matches.append(index)
        if len(matches) == 1:
            index = matches[0]
            existing = output[index]
            merged = dict(existing)
            for key, value in deepcopy(fact).items():
                if key == "extra_attributes":
                    continue
                if key == "product_name" and existing.get("product_name"):
                    current_name = text(existing.get("product_name"))
                    recovered_name = text(value)
                    # Keep the more complete source identity.  In particular,
                    # a recovery pass must not replace a part/model-bearing
                    # Gemini description with a shortened or malformed name.
                    if ("(" in current_name and ")" not in recovered_name) or len(current_name) > len(recovered_name):
                        continue
                # Recovery is supplemental; an empty/null field must never
                # erase a valid value already extracted from Gemini.
                if value not in (None, "", []):
                    merged[key] = value
            attrs = []
            seen_attrs = set()
            for attr in list(existing.get("extra_attributes") or []) + list(fact.get("extra_attributes") or []):
                if not isinstance(attr, dict):
                    continue
                key = (text(attr.get("label")).casefold(), text(attr.get("value")))
                if key not in seen_attrs:
                    seen_attrs.add(key)
                    attrs.append(deepcopy(attr))
            merged["extra_attributes"] = attrs
            # Recovery is a source-fact supplement.  It must not downgrade a
            # Gemini PRODUCT into the generic SERVICE default used by older
            # text-line recovery rows.
            existing_type = str(output[index].get("row_type") or "").upper()
            recovered_type = str(fact.get("row_type") or "").upper()
            if existing_type in {"PRODUCT", "CHARGE", "SERVICE"} and recovered_type == "SERVICE":
                merged["row_type"] = existing_type
            merged.pop("_keep_duplicate_priced_row", None)
            output[index] = merged
            used.add(index)
        else:
            output.append(deepcopy(fact))
            used.add(len(output) - 1)
    return output


def finalize_pdf(result, file_bytes, recovery_items=None):
    """Recover tariff/summary facts with page and row provenance."""
    from pypdf import PdfReader
    from api.services.gemini_extraction_service import (
        _extract_pdf_email_tariff_charge_items, _nearest_previous_email_date,
        normalize_result,
    )
    pages = [unicodedata.normalize("NFKC", p.extract_text() or "") for p in PdfReader(io.BytesIO(file_bytes)).pages]
    all_text = "\n".join(pages)
    # This recovery applies only to an email quotation and explicit summary.
    total_pattern = r"\bt\s*otal\s+amount\s*\(\s*([A-Z]{3})\s*\)\s*[:\-]?\s*([\d,]+(?:\.\d+)?)"
    totals = [(n + 1, re.search(total_pattern, p, re.I)) for n, p in enumerate(pages)]
    totals = [(n, m) for n, m in totals if m]
    # A replied/forwarded quotation can contain no grand-total line.  The
    # sender boundary is still sufficient to distinguish the priced reply
    # from the earlier unpriced RFQ, so do not bypass reconciliation merely
    # because totals are absent.
    if not re.search(r"(?im)^From:", all_text):
        if recovery_items:
            from api.services.commercial_row_identity import is_true_duplicate
            items = list(result.get("items") or [])
            for row in recovery_items:
                if not any(is_true_duplicate(row, item) for item in items):
                    items.append(row)
            result["items"] = items
        if re.search(r"\bcommercial\s+(?:offer|proposal)\b|\bquotation\b|\boffer\b", all_text, re.I):
            result.update(category="QUOTATION", document_type="offer")
            suppliers = [str(row.get("supplier_name") or "").strip() for row in result.get("items") or [] if str(row.get("supplier_name") or "").strip()]
            supplier_keys = {re.sub(r"\b(?:and|co|company|ltd|limited|llc|corp|corporation)\b|[^a-z0-9]", "", value.casefold()) for value in suppliers}
            if len(supplier_keys) == 1 and suppliers:
                result["supplier_name"] = max(suppliers, key=len)
            for row in result.get("items") or []:
                row["source_document_type"] = "offer"
                row["source_category"] = "QUOTATION"
        return result
    sections = re.split(r"(?im)^From:", pages[0])
    if len(sections) < 2:
        return result
    first_section = sections[1]
    signature = re.split(r"(?i)best\s+regards[^\n]*", first_section, maxsplit=1)
    seller = None
    if len(signature) == 2:
        for line in signature[1].splitlines():
            company = re.match(r"^(.+?\b(?:L\.?L\.?C\.?|LTD\.?|LIMITED))(?=\s|$)", text(line), re.I)
            if company:
                seller = company.group(1)
                break
    buyer = re.search(r"(?im)^To:\s*(.+)", first_section)
    buyer_key = re.sub(r"\W", "", buyer.group(1).lower()) if buyer else ""
    if not seller:
        names = {text(i.get("supplier_name")) for i in result.get("items") or []
                 if text(i.get("supplier_name")) and re.sub(r"\W", "", text(i.get("supplier_name")).lower()) != buyer_key}
        if len(names) == 1:
            seller = names.pop()
    recovered = _extract_pdf_email_tariff_charge_items(file_bytes, seller)
    lines = [(page + 1, text(line)) for page, content in enumerate(pages) for line in content.splitlines() if text(line)]
    summary_pages = {n: m for n, m in totals}
    from api.services.msg_commercial_history import email_date
    sent = re.search(r"(?im)^Sent\s*:\s*(.+)", first_section)
    doc_date = email_date(sent.group(1)) if sent else _nearest_previous_email_date(first_section, len(first_section))
    for fact in recovered:
        loc = re.fullmatch(r"pdf-text-line:(\d+)", str(fact.get("source_row_id")))
        if not loc or int(loc.group(1)) >= len(lines):
            continue
        page, source = lines[int(loc.group(1))]
        fact.update(page_number=page, _source_fact_verified=True, source_context=source)
        _clean_verified_pdf_row(fact, source)
        fact.pop("_keep_duplicate_priced_row", None)
        fact["purchase_date"] = doc_date
        if re.search(r"\btemporary\s+fuel\s+price\s+adjustment\b|\bfuel\s+price\s+adjustment\b", fact.get("product_name") or "", re.I):
            fact["_normalization_action"] = "adjustment_note_row_skipped"
            continue
        if re.fullmatch(r"(?:the\s+)?updated\s+tariff", text(fact.get("product_name")), re.I):
            fact["product_name"] = "launch hire tariff"
        attrs = fact.get("extra_attributes") or []
        if page in summary_pages:
            fact.update(quantity=None, uom=None, unit_price=None, _amount_semantics="total")
            fact["currency"] = summary_pages[page].group(1).upper()
            # Remove only parenthesized rate/duration metadata, preserving words.
            fact["product_name"] = re.sub(r"\([^)]*\d[^)]*\)", "", fact["product_name"]).strip()
            prefix = re.match(r".*?\(\d+\s*hrs?\s*\d+\s*min\)\s*(.+)", source, re.I)
            if prefix:
                label = re.sub(r"\([^)]*\)", "", prefix.group(1))
                fact["product_name"] = re.sub(r"\s+[\d,.]+$", "", label).strip()
        elif any(a.get("value") == "Per hour" for a in attrs):
            fact.update(quantity=None, uom="HR", line_total=None, _amount_semantics="rate")
        elif any(a.get("value") == "Lump sum" for a in attrs):
            fact.update(quantity=None, uom=None, _amount_semantics="lump_sum")
        # Exact duration from this summary, not from another page/rate.
        duration = re.search(r"\((\d+\s*hrs?\s*\d+\s*min)\)", pages[page - 1], re.I)
        if duration and page in summary_pages:
            attrs = [a for a in attrs if a.get("label") != "Duration"] + [{"label": "Duration", "value": duration.group(1)}]
        fact["extra_attributes"] = attrs
        if page not in summary_pages:
            effective = re.search(r"effective\s+from\s+(\d+)(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})", pages[page - 1], re.I)
            if effective:
                fact["purchase_date"] = email_date(" ".join(effective.groups())) or doc_date
            if re.search(r"\bupdated\s+tariff\b", source, re.I):
                fact["product_name"] = re.search(r"\bupdated\s+tariff\b", source, re.I).group()
    # Bind conditional tariffs to their preceding physical service statement.
    # Summary amounts are independent of parenthesized reference rates.
    last_base = None
    canonical_tariffs = []
    for fact in recovered:
        source = fact.get("source_context") or ""
        if fact.get("_amount_semantics") == "lump_sum":
            last_base = fact
            duration = re.search(r"\bfor\s+(\d+)\s*h(?:ou)?rs?\b", source, re.I)
            if duration:
                fact["row_type"] = "SERVICE"
                context = re.search(r"\btariff\s+for\s+[^\n]+?\s+at\s+([^\n.]+)", pages[fact["page_number"] - 1], re.I)
                if context:
                    fact["product_name"] += " - " + context[1].strip()
                fact["product_name"] += f" | First {int(duration[1])} hours, lumpsum"
                canonical_tariffs.append(fact)
        elif re.match(r"there\s*after\b", source, re.I) and last_base and fact.get("page_number") == last_base.get("page_number"):
            after = re.search(r"\bafter\s+(\d+)\s*h(?:ou)?rs?\b", source, re.I)
            if after:
                base = last_base["product_name"].split(" | ")[0]
                fact.update(product_name=f"{base} | Additional hire after first {int(after[1])} hours | Per hour",
                            row_type="SERVICE", uom="HR", quantity=None, line_total=None)
                canonical_tariffs.append(fact)
        elif fact.get("page_number") in summary_pages and any(f.get("uom") == "HR" for f in canonical_tariffs):
            amount = re.search(r"\s+([\d,]+(?:\.\d+)?)\s*$", source)
            if amount and number(amount[1]) == number(fact.get("line_total")):
                label = source[:amount.start()].strip()
                label = re.sub(r"\([^)]*(?:\b[A-Z]{3}\b|/\s*h(?:ou)?r)[^)]*\)", "", label).strip()
                fact.update(product_name=label, quantity=None, unit_price=fact["line_total"],
                            _unit_price_from_source_total=True, extra_attributes=[])
                if re.search(r"\b(?:hire|rental|crane|labou?r|operation)\b", label, re.I):
                    fact["row_type"] = "SERVICE"
                canonical_tariffs.append(fact)
        if fact in canonical_tariffs:
            fact["display_name"] = fact["product_name"]

    def canonical_match(row):
        if row.get("source_attachment") or row.get("source_attachment_name"):
            return False
        matches = []
        for fact in canonical_tariffs:
            if row.get("page_number") and str(row["page_number"]) != str(fact.get("page_number")):
                continue
            if row.get("purchase_date") and row["purchase_date"] != fact.get("purchase_date"):
                continue
            if row.get("source_row_id") == fact.get("source_row_id"):
                matches.append(fact)
                continue
            a = set(re.findall(r"[a-z]+", str(row.get("product_name") or '').lower()))
            b = set(re.findall(r"[a-z]+", fact["product_name"].split(' | ')[0].lower()))
            overlap = (a & b) - {'for', 'the', 'and', 'after', 'first', 'with'}
            same_rate = number(row.get("unit_price")) == number(fact.get("unit_price"))
            summary = fact.get("page_number") in summary_pages
            if len(overlap) >= 2 and (same_rate or summary):
                matches.append(fact)
            elif re.match(r"there\s*after\b", str(row.get("product_name") or ''), re.I) and fact.get("uom") == "HR" and same_rate:
                matches.append(fact)
        return len(matches) == 1

    # Old recovery copies are replaced from their source, not appended again.
    primary = [i for i in result.get("items") or [] if not canonical_match(i)
               if not re.search(r"\btemporary\s+fuel\s+price\s+adjustment\b|\bfuel\s+price\s+adjustment\b", str(i.get("product_name") or ""), re.I)
               and (i.get("_source_fact_verified") or not str(i.get("source_row_id") or "").startswith("pdf-text-line:"))]
    # Bind description variants to a unique local source phrase. Conditions
    # are compared separately through the distinct rate and page.
    for row in primary:
        candidates = [f for f in recovered if f.get("page_number") == row.get("page_number")
                      and (not row.get("purchase_date") or row.get("purchase_date") == f.get("purchase_date"))
                      and number(f.get("line_total") if f.get("_amount_semantics") == "total" else f.get("unit_price"))
                      == number(row.get("line_total") if f.get("_amount_semantics") == "total" else row.get("unit_price"))]
        variants = []
        for fact in candidates:
            a, b = name_key(row.get("product_name")), name_key(fact.get("product_name"))
            # Require the complete shorter phrase, never amount alone.
            if a == b or (min(len(a.split()), len(b.split())) >= 2 and (a in b or b in a)):
                variants.append(fact)
        if len(variants) == 1:
            fact = variants[0]
            _clean_verified_pdf_row(row, fact.get("source_context"))
            # Distinct effective dates still prevent merging unless the source
            # line itself explicitly supplies that date.
            row["product_name"] = fact["product_name"]
            if fact.get("_amount_semantics") == "rate" and number(row.get("quantity")) == 1 and number(row.get("line_total")) == number(row.get("unit_price")):
                # A source per-hour offer is not evidence of one purchased hour.
                # Limit this repair to a verified rate-only source statement.
                if re.search(r"\bper\s+(?:hr|hour)\b", fact.get("source_context") or "", re.I):
                    row.update(quantity=None, line_total=None)
            if fact.get("_amount_semantics") == "lump_sum" and text(row.get("uom")).lower() in {"hr", "hrs", "hour", "hours"}:
                duration = re.search(r"\bfor\s+(\d+(?:\.\d+)?)\s*h(?:ou)?rs?\b", fact.get("source_context") or "", re.I)
                if duration and number(row.get("quantity")) == number(duration[1]):
                    row.update(quantity=None, uom=None)
            duration_attrs = [a.get("value") for a in row.get("extra_attributes") or [] if a.get("label") == "Duration"]
            if fact.get("_amount_semantics") == "total" and duration_attrs:
                # Correct only a quantity demonstrably copied from duration.
                for duration_value in duration_attrs:
                    match = re.fullmatch(r"(\d+)\s*hrs?\s*(\d+)\s*min", text(duration_value), re.I)
                    if match and (text(row.get("quantity")) == text(duration_value) or number(row.get("quantity")) == Decimal(match[1]) + Decimal(match[2]) / 60):
                        row["quantity"] = None
            if re.search(r"\b(?:price\s+adjustment|tariff)\b", str(row.get("product_name")), re.I):
                row["row_type"] = "SERVICE"
    for row in primary:
        if seller and row.get("page_number") in {1, *summary_pages}:
            row["supplier_name"] = seller
        if re.fullmatch(r"(?:the\s+)?updated\s+tariff", text(row.get("product_name")), re.I):
            row["product_name"] = "launch hire tariff"
        if re.search(r"\bprice\s+adjustment\b", str(row.get("product_name")), re.I) and number(row.get("unit_price")) is not None:
            row.update(row_type="SERVICE", _source_fact_verified=True, _amount_semantics="rate")
    merged_items = merge_recovery(primary, recovered)
    # The same verified PDF source line can be present in a saved Gemini
    # result, an explicit recovery pass, and a retry. Collapse that locator
    # before returning; source_row_id identifies the original source fact.
    by_source = {}
    ordered = []
    for row in merged_items:
        source_id = row.get("source_row_id") if row.get("_source_fact_verified") else None
        if source_id and source_id in by_source:
            index = by_source[source_id]
            ordered[index] = {**ordered[index], **row}
        else:
            if source_id:
                by_source[source_id] = len(ordered)
            ordered.append(row)
    # Remove an unverified pass copy when the verified source fact has the
    # same page, date, description and explicit total. Rate metadata may differ
    # between passes, but the source total identifies the same commercial row.
    verified = [r for r in ordered if r.get("_source_fact_verified")]
    ordered = [r for r in ordered if not (
        not r.get("_source_fact_verified")
        and any(name_key(r.get("product_name")) == name_key(v.get("product_name"))
                and r.get("page_number") == v.get("page_number")
                and r.get("purchase_date") == v.get("purchase_date")
                and number(r.get("line_total")) is not None
                and number(r.get("line_total")) == number(v.get("line_total"))
                for v in verified)
    )]
    for row in ordered:
        if re.fullmatch(r"(?:the\s+)?updated\s+tariff", text(row.get("product_name")), re.I):
            row["product_name"] = "launch hire tariff"
            row["display_name"] = "launch hire tariff"
    # In an email-thread PDF, rows without a visible amount belong to the
    # enquiry/request context, not the supplier's commercial reply.
    ordered = [row for row in ordered if number(row.get("unit_price")) is not None
               or number(row.get("line_total")) is not None]
    result["items"] = ordered
    if seller:
        result["supplier_name"] = seller
        for row in result["items"]:
            if row.get("page_number") in {1, *summary_pages}:
                row["supplier_name"] = seller
    if doc_date:
        result["document_date"] = doc_date
    # A standalone document explicitly titled Commercial Offer/Proposal is a
    # quotation offer, even when its body contains a conventional price table.
    # Email-thread PDFs retain the email_quote classification above.
    if re.search(r"\bcommercial\s+(?:offer|proposal)\b|\bquotation\b|\boffer\b", all_text, re.I) and not re.search(r"(?im)^From:", all_text):
        result.update(category="QUOTATION", document_type="offer")
        for row in result.get("items") or []:
            row["source_document_type"] = "offer"
            row["source_category"] = "QUOTATION"
    else:
        result.update(category="QUOTATION", document_type="email_quote")
    if len(totals) == 1:
        result["grand_total"] = totals[0][1].group(2).replace(",", "")
    currencies = {i.get("currency") for i in result["items"] if i.get("currency")}
    if len(currencies) > 1:
        result["currency"] = None
    result["_raw_extracted_count"] = len(result["items"])
    normalized_result = normalize_result(result)
    if normalized_result.get("document_type") == "offer" and not normalized_result.get("supplier_name"):
        suppliers = {str(row.get("supplier_name") or "").strip() for row in normalized_result.get("items") or [] if str(row.get("supplier_name") or "").strip()}
        if len(suppliers) == 1:
            normalized_result["supplier_name"] = next(iter(suppliers))
    return required_metadata(normalized_result)
