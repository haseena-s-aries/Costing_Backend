import re
from decimal import Decimal, InvalidOperation

from .field_aliases import aliases_for, normalize_header

FIELDS = ("product_name", "quantity", "uom", "unit_price", "line_total")

class DeterministicSpreadsheetParser:
    def extract(self, document, structure, profile=None):
        if getattr(structure, "structure", structure) != "STANDARD_TABLE":
            return None
        rows = self._eligible_commercial_rows(document.get("rows") or [], profile)
        if not rows:
            return None
        header_index = next((i for i, row in enumerate(rows[:30]) if sum(normalize_header(v) in aliases_for(f, profile) for v in row["values"] for f in FIELDS) >= 2), None)
        same_row_product_and_price = bool(
            header_index is not None
            and any(normalize_header(v) in aliases_for("product_name", profile) for v in rows[header_index]["values"])
            and any(
                normalize_header(v) in aliases_for("unit_price", profile)
                or normalize_header(v) in aliases_for("line_total", profile)
                for v in rows[header_index]["values"]
            )
        )
        has_group_header = bool(
            header_index
            and same_row_product_and_price
            and sum(bool(str(v or "").strip()) for v in rows[header_index - 1]["values"]) >= 2
            and not any(re.match(r"(?i)^\s*(?:date|quotation\s+date|ref(?:erence)?\s*(?:no|number)?|to|from)\s*[:.]", str(v or ""))
                        for v in rows[header_index - 1]["values"])
        )
        if same_row_product_and_price and header_index is not None:
            has_group_header = has_group_header or self._has_structural_supplier_prices(rows, header_index, profile)
        grouped = not same_row_product_and_price or has_group_header
        if header_index is None or grouped:
            grouped_header = self._find_grouped_header(rows, profile)
            if grouped_header is not None:
                header_index = grouped_header
                grouped = True
            elif header_index is not None and self._has_structural_supplier_prices(rows, header_index, profile):
                # Some comparison sheets use supplier names directly as
                # price headers and have no UP/Rate subheader.
                grouped = True
            else:
                header_index = None
                grouped = False
        if header_index is None:
            return None
        headers = self._effective_headers(rows, header_index, profile) if grouped else [normalize_header(v) for v in rows[header_index]["values"]]
        columns = ({field: self._grouped_column(rows, header_index, field, profile) for field in FIELDS}
                   if grouped else
                   {field: next((i for i, header in enumerate(headers) if header in aliases_for(field, profile)), None) for field in FIELDS})
        if columns["product_name"] is None or (
            columns["unit_price"] is None and columns["line_total"] is None
            and not (grouped and self._has_structural_supplier_prices(rows, header_index, profile))
        ):
            return None

        vertical_items = self._vertical_supplier_items(rows, header_index, columns, profile)
        if vertical_items:
            suppliers = {item.get("supplier_name") for item in vertical_items if item.get("supplier_name")}
            return {"document_type": "price_list", "category": "PRICE_LIST",
                    "supplier_name": next(iter(suppliers)) if len(suppliers) == 1 else None,
                    "document_date": None, "currency": None, "document_number": None,
                    "items": vertical_items, "_source_header": rows[header_index]["values"]}

        # A plain STANDARD_TABLE header has one price column.  Do not scan
        # arbitrary metadata rows above it as supplier headers (for example
        # REF NO / DATE), because that can turn a reference label into the
        # item supplier. Supplier-column discovery is reserved for grouped
        # comparison layouts.
        supplier_columns = self._grouped_supplier_columns(rows, header_index, headers, columns, profile) if grouped else []
        items = []
        current_parent_category = None
        current_sheet = None
        serial_column = next((i for i, value in enumerate(rows[header_index]["values"])
                              if normalize_header(value) in {"no", "sl no", "s no", "serial no", "item no"}), None)
        numbered_parents = {}
        for row in rows[header_index + 1:]:
            values = row["values"]
            if current_sheet is not None and row.get("sheet") != current_sheet:
                current_parent_category = None
                numbered_parents = {}
            current_sheet = row.get("sheet")
            serial = str(self._value(values, serial_column) or "").strip()
            # Spreadsheet numeric integers arrive as '2.0'. Decimal child
            # identifiers remain paths ('2.1', '2.2'), never quantities.
            serial = re.sub(r"\.0$", "", serial)
            serial = serial if re.fullmatch(r"\d+(?:\.\d+)*", serial) else ""
            if serial:
                had_numbered_parent = bool(numbered_parents)
                numbered_parents = {key: value for key, value in numbered_parents.items()
                                    if serial.startswith(key + '.')}
                if '.' in serial and had_numbered_parent:
                    current_parent_category = next((numbered_parents[key] for key in sorted(numbered_parents, key=len, reverse=True)), None)
                elif had_numbered_parent:
                    current_parent_category = None

            parent = self._detect_section_heading(
                values, columns, supplier_columns, profile
            )
            if parent:
                current_parent_category = parent
                if serial:
                    numbered_parents[serial] = parent
                else:
                    numbered_parents = {}
                continue

            product = self._value(values, columns["product_name"])
            if not product:
                continue
            normalized_product = normalize_header(product)
            if normalized_product.startswith(("total", "subtotal", "grand total")):
                continue

            # Section headings occupy the description column but may contain a
            # formula-generated zero in an amount column. Zero-only cells are
            # not commercial prices and must not turn the heading into an item.
            row_prices = []
            if supplier_columns:
                for group in supplier_columns:
                    _supplier, price_col, total_col = group[:3]
                    row_prices.extend((self._value(values, price_col), self._value(values, total_col)))
            else:
                row_prices = [self._value(values, columns["unit_price"]), self._value(values, columns["line_total"])]
            has_nonzero_price = any(self._is_nonzero_number(value) for value in row_prices)
            if not has_nonzero_price:
                # An unpriced product/reference row must not inherit or create
                # section context. It is not a commercial item in this pass,
                # but it is still a structural boundary for the prior group.
                current_parent_category = None
                continue
            metadata = self._header_metadata(rows, header_index)
            base = {"row_type": "PRODUCT", "product_name": product,
                    "parent_category": current_parent_category,
                    "quantity": self._value(values, columns["quantity"]), "uom": self._value(values, columns["uom"]),
                    "currency": metadata["currency"], "supplier_name": metadata["supplier_name"],
                    "source_reference": f"{row['sheet']} row {row['row_number']}"}
            header_labels = list(rows[header_index - 1]["values"]) if grouped and header_index > 0 else list(rows[header_index]["values"])
            sub_labels = rows[header_index]["values"] if grouped and header_index > 0 else []
            header_labels.extend([""] * max(0, len(values) - len(header_labels)))
            for label_index, label in enumerate(sub_labels):
                if label_index >= len(header_labels):
                    header_labels.append(label)
                elif label:
                    header_labels[label_index] = f"{header_labels[label_index]} {label}".strip()
            supplier_indexes = set()
            for group in supplier_columns:
                supplier_indexes.update(index for index in group[1:7] if index is not None)
            base_attributes = self._base_attributes(values, header_labels, columns, supplier_indexes)
            for attribute in base_attributes:
                if normalize_header(attribute.get("label")) .endswith(" remarks"):
                    attribute["label"] = "REMARKS"
            existing_attributes = {(str(attr.get("label") or "").casefold(), str(attr.get("value") or "").strip())
                                  for attr in base_attributes}
            core_indexes = {index for index in columns.values() if index is not None}
            for index, value in enumerate(values):
                label = str(header_labels[index] or "").strip() if index < len(header_labels) else ""
                text = str(value or "").strip()
                if not label or not text or index in core_indexes:
                    continue
                normalized_label = normalize_header(label)
                if normalized_label.endswith(" remarks") or normalized_label == "remarks":
                    label = "REMARKS"
                    normalized_label = "remarks"
                if index in supplier_indexes and normalized_label not in aliases_for("remark"):
                    continue
                if normalized_label in aliases_for("unit_price", profile) or normalized_label in aliases_for("line_total", profile):
                    continue
                if self._is_number(text):
                    continue
                key = (label.casefold(), text)
                if key not in existing_attributes:
                    base_attributes.append({"label": label, "value": text})
                    existing_attributes.add(key)
            if supplier_columns:
                for group in supplier_columns:
                    supplier, price_col, total_col = group[:3]
                    if re.match(r"(?i)^(?:ref(?:erence)?\s*(?:no|number)?|date|currency|quotation\s*(?:no|number)?|quote\s*(?:no|number)?|po\s*(?:no|number)?|vessel|to|from)\b", str(supplier or "").strip()):
                        supplier = metadata["supplier_name"]
                    unit_price = self._value(values, price_col)
                    line_total = self._value(values, total_col)
                    if unit_price is None and line_total is None:
                        continue
                    quantity_col = group[4] if len(group) > 4 else None
                    uom_col = group[5] if len(group) > 5 else None
                    group_currency = group[7] if len(group) > 7 else None
                    item = dict(base, unit_price=unit_price, line_total=line_total,
                                supplier_name=supplier,
                                quantity=self._value(values, quantity_col) if quantity_col is not None else base["quantity"],
                                uom=self._value(values, uom_col) if uom_col is not None else base["uom"],
                                currency=group_currency,
                                extra_attributes=list(base_attributes))
                    description_col = group[6] if len(group) > 6 else None
                    if description_col is not None:
                        quoted_description = self._value(values, description_col)
                        if quoted_description and quoted_description != product and not self._is_number(quoted_description):
                            item["extra_attributes"].append({"label": f"{supplier} Description", "value": quoted_description})
                    items.append(item)
            else:
                item = dict(base, extra_attributes=base_attributes,
                            unit_price=self._value(values, columns["unit_price"]),
                            line_total=self._value(values, columns["line_total"]))
                if item["unit_price"] or item["line_total"]:
                    items.append(item)
        if not items:
            return None
        metadata = self._header_metadata(rows, header_index)
        suppliers = {item.get("supplier_name") for item in items if item.get("supplier_name")}
        multi_supplier = len(suppliers) > 1
        return {"document_type": "price_list" if multi_supplier else ((profile.document_type if profile else None) or "quotation"),
                "category": "PRICE_LIST" if multi_supplier else "QUOTATION",
                "supplier_name": metadata["supplier_name"] or (next(iter(suppliers)) if len(suppliers) == 1 else None),
                "document_date": metadata["document_date"], "currency": metadata["currency"],
                "document_number": metadata["document_number"],
                "items": items, "_source_header": rows[header_index]["values"]}

    @staticmethod
    def _header_metadata(rows, header_index):
        text = "\n".join(str(value or "").strip() for row in rows[:header_index] for value in row.get("values", []))
        date_match = re.search(r"(?i)\b(?:date|quotation\s*date)\s*[:.-]?\s*(\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2})", text)
        document_date = None
        from api.services.gemini_extraction_service import _valid_date
        # Read only explicitly labelled dates, including an adjacent cell.
        for row in rows[:header_index]:
            values = row.get("values", [])
            for index, value in enumerate(values):
                match = re.match(r"(?i)^\s*(?:date|quotation\s+date)\s*[:.-]?\s*(.*?)\s*$", str(value or ""))
                if not match:
                    continue
                candidate = match.group(1) or next((str(v) for v in values[index + 1:] if str(v or '').strip()), '')
                candidate = re.sub(r"(?i)\bSept\.?\b\.?", "Sep", candidate)
                document_date = _valid_date(candidate) or document_date
        if date_match:
            parts = re.split(r"[-/]", re.sub(r"\s+", "", date_match.group(1)))
            document_date = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        ref_match = re.search(
            r"(?im)^\s*(?:ref(?:erence)?\s*(?:no|number)?|quote\s*(?:no|number)?|quotation\s*(?:no|number)?|po\s*(?:no|number)?)\s*[:#.\-]+\s*([A-Z0-9][A-Z0-9./_-]*)",
            text,
        )
        currency_match = re.search(r"(?i)\b(?:currency)\s*[:.-]?\s*(USD|EUR|GBP|AED|CNY|RMB|SAR|INR)\b", text)
        currency = currency_match.group(1).upper() if currency_match else None
        if not currency:
            header_currencies = set(re.findall(r"\((USD|EUR|GBP|AED|CNY|RMB|SAR|INR)\)", " ".join(str(v or '') for v in rows[header_index]['values']), re.I))
            if len(header_currencies) == 1:
                currency = next(iter(header_currencies)).upper()
        if currency == "RMB":
            currency = "CNY"
        excluded = re.compile(r"(?i)^(?:ref(?:erence)?|date|currency|to|from|vessel|quote|quotation|po)\b")
        supplier_candidates = []
        for row in rows[:header_index]:
            for value in row.get("values", []):
                candidate = re.sub(r"\s+", " ", str(value or "")).strip()
                if candidate and len(candidate) >= 3 and not excluded.search(candidate) and not re.search(r"\b(?:date|currency|ref\s*no|quotation)\s*[:.]", candidate, re.I):
                    if not re.search(r"\d{4}\s*[-/]\s*\d", candidate):
                        supplier_candidates.append(candidate)
        # A single company heading is document metadata. Multiple candidates
        # usually represent comparison-table supplier headers and must remain
        # item-level instead of arbitrarily becoming the document supplier.
        supplier = None
        if supplier_candidates:
            # A company heading is normally the first populated metadata row;
            # later address/contact rows are not supplier identities.
            first_row_values = [re.sub(r"\s+", " ", str(v or "")).strip() for v in rows[:header_index][0].get("values", [])] if rows[:header_index] else []
            first_row_candidates = [v for v in first_row_values if v in supplier_candidates]
            if len(first_row_candidates) == 1:
                supplier = first_row_candidates[0]
            elif len(set(supplier_candidates)) == 1:
                supplier = supplier_candidates[0]
        return {"supplier_name": supplier, "document_date": document_date, "currency": currency,
                "document_number": ref_match.group(1) if ref_match else None}

    @classmethod
    def _vertical_supplier_items(cls, rows, header_index, columns, profile=None):
        """Extract one product's vertically stacked supplier quotation blocks."""
        if header_index >= len(rows) - 2:
            return []
        product_col = columns.get("product_name")
        if product_col is None:
            return []
        header = rows[header_index]["values"]
        first_data = rows[header_index + 1]["values"]
        if product_col >= len(first_data) or not cls._value(first_data, product_col):
            return []
        supplier_indexes = []
        for index, value in enumerate(header):
            name = str(value or "").strip()
            normalized = normalize_header(name)
            if index <= product_col or not name or normalized in {"sl no", "serial no", "no", "description", "qty", "quantity", "unit", "remark", "remarks", "notes"}:
                continue
            if normalized in aliases_for("unit_price", profile) or normalized in aliases_for("line_total", profile):
                continue
            if re.search(r"budget|estimated|target|reference|allowance", normalized, re.I):
                continue
            supplier_indexes.append((index, name))
        if len(supplier_indexes) < 2:
            return []
        end_row = header_index + 1
        while end_row < len(rows):
            values = rows[end_row]["values"]
            if end_row > header_index + 1 and not any(str(value or "").strip() for value in values):
                break
            end_row += 1
        if end_row - (header_index + 1) < 2:
            return []
        if any(
            cls._value(row["values"], product_col)
            for row in rows[header_index + 2:end_row]
        ):
            return []
        has_vertical_detail = any(
            str(value or "").strip()
            and not cls._is_number(value)
            for row in rows[header_index + 2:end_row]
            for value in row["values"][product_col + 1:]
        )
        if not has_vertical_detail:
            return []
        items = []
        for position, (start, supplier) in enumerate(supplier_indexes):
            boundary = supplier_indexes[position + 1][0] if position + 1 < len(supplier_indexes) else max(len(header), start + 1)
            cells = []
            for row in rows[header_index + 1:end_row]:
                values = row["values"]
                for col in range(start, min(boundary, len(values))):
                    value = cls._value(values, col)
                    if value is not None:
                        cells.append((row["row_number"], col, value))
            numeric = [(row_no, col, value) for row_no, col, value in cells if cls._parse_number(value) is not None]
            if not numeric:
                continue
            final_row = max(row_no for row_no, _col, _value in numeric)
            final_candidates = [entry for entry in numeric if entry[0] == final_row]
            final_value = final_candidates[-1][2]
            final_col = final_candidates[-1][1]
            parsed_value, parsed_currency = cls._parse_number(final_value)
            if parsed_currency is None:
                source_row = next((row for row in rows if row.get("row_number") == final_row), None)
                formats = source_row.get("number_formats") if source_row else []
                if formats and final_col < len(formats):
                    parsed_currency = cls._currency_from_text(formats[final_col])
            if parsed_value is None:
                continue
            attrs = []
            for row in rows[header_index + 1:end_row]:
                values = row["values"]
                row_cells = [(col, cls._value(values, col)) for col in range(start, min(boundary, len(values)))
                             if cls._value(values, col) is not None]
                if not row_cells:
                    continue
                row_no = row["row_number"]
                numeric_cells = [(col, value) for col, value in row_cells if cls._is_amount_cell(value)]
                text_cells = [(col, value) for col, value in row_cells if not cls._is_amount_cell(value)]
                for col, value in numeric_cells:
                    if row_no == final_row and value == final_value:
                        continue
                    number, currency = cls._parse_number(value)
                    formats = row.get("number_formats") or []
                    if currency is None and col < len(formats):
                        currency = cls._currency_from_text(formats[col])
                    if text_cells:
                        label = str(text_cells[0][1]).strip()
                    elif row_no == final_row:
                        label = "Converted Total"
                    else:
                        label = "Source Total"
                    formatted = f"{currency} {number}" if currency else str(number)
                    attrs.append({"label": f"{supplier} {label}", "value": formatted})
                for _col, value in text_cells:
                    if not numeric_cells:
                        attrs.append({"label": f"{supplier} Source Detail", "value": str(value).strip()})
            # Keep only the first occurrence of an identical source detail.
            unique_attrs = []
            seen = set()
            for attr in attrs:
                key = (attr["label"], attr["value"])
                if key not in seen:
                    unique_attrs.append(attr)
                    seen.add(key)
            items.append({"row_type": "PRODUCT", "product_name": cls._value(first_data, product_col),
                          "parent_category": None, "quantity": cls._value(first_data, columns.get("quantity")),
                          "uom": cls._value(first_data, columns.get("uom")), "item_code": None, "part_number": None,
                          "source_reference": f"{rows[header_index + 1]['sheet']} row {rows[header_index + 1]['row_number']}",
                          "unit_price": str(parsed_value), "line_total": None, "supplier_name": supplier,
                          "currency": parsed_currency, "extra_attributes": unique_attrs})
        return items

    @staticmethod
    def _parse_number(value):
        text = str(value or "").replace(",", "").strip()
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if not match:
            return None, None
        number = match.group(0)
        try:
            decimal_value = Decimal(number)
            if "." in number and len(number.split(".", 1)[1]) > 4:
                decimal_value = decimal_value.quantize(Decimal("0.01"))
            number = format(decimal_value, "f")
            if "." in number:
                number = number.rstrip("0").rstrip(".")
        except InvalidOperation:
            pass
        currency = DeterministicSpreadsheetParser._currency_from_text(text)
        if "RMB" in text.upper() or "CNY" in text.upper() or "¥" in text:
            currency = "CNY"
        return number, currency

    @staticmethod
    def _is_amount_cell(value):
        text = str(value or "").replace(",", "").strip()
        return bool(re.fullmatch(r"(?:[A-Za-z$€£¥د.إ]+\s*)?[-+]?\d+(?:\.\d+)?(?:\s*[A-Za-z$€£¥]+)?", text))

    @classmethod
    def _find_grouped_header(cls, rows, profile=None):
        for index in range(min(20, len(rows) - 1)):
            combined = []
            for candidate in (index, index + 1):
                combined.extend(rows[candidate]["values"])
            hits = sum(
                normalize_header(value) in aliases_for(field, profile)
                for value in combined
                for field in FIELDS
            )
            has_product = any(normalize_header(value) in aliases_for("product_name", profile) for value in combined)
            # The lower row must actually contain the commercial subheaders;
            # otherwise a data row can be mistaken for a grouped header.
            has_price = any(
                normalize_header(value) in aliases_for("unit_price", profile)
                or normalize_header(value) in aliases_for("line_total", profile)
                for value in rows[index + 1]["values"]
            )
            if has_product and has_price and hits >= 2:
                return index + 1
        return None

    @classmethod
    def _has_structural_supplier_prices(cls, rows, header_index, profile=None):
        headers = rows[header_index]["values"]
        product_col = next(
            (i for i, value in enumerate(headers)
             if normalize_header(value) in aliases_for("product_name", profile)
             or normalize_header(value) in {"product", "description", "item"}),
            None,
        )
        if product_col is None:
            return False
        numeric_columns = 0
        for index, value in enumerate(headers):
            if index <= product_col or not str(value or "").strip():
                continue
            normalized_header = normalize_header(value)
            if (
                normalized_header in aliases_for("unit_price", profile)
                or normalized_header in aliases_for("line_total", profile)
                or normalized_header in aliases_for("quantity", profile)
                or normalized_header in aliases_for("uom", profile)
                or normalized_header in {"sl no", "serial no", "no"}
            ):
                continue
            if any(cls._is_nonzero_number(row["values"][index])
                   for row in rows[header_index + 1:]
                   if index < len(row["values"])):
                numeric_columns += 1
        return numeric_columns >= 1

    @classmethod
    def _effective_headers(cls, rows, header_index, profile=None):
        block = ([rows[header_index - 1]["values"], rows[header_index]["values"]]
                 if header_index > 0 else [rows[header_index]["values"]])
        width = max(len(row) for row in block)
        effective = []
        for col in range(width):
            parts = []
            for row in block:
                value = str(row[col] or "").strip() if col < len(row) else ""
                if value and value not in parts:
                    parts.append(value)
            effective.append(normalize_header(" ".join(parts)))
        return effective

    @classmethod
    def _grouped_supplier_columns(cls, rows, header_index, headers, columns, profile=None):
        top = rows[header_index - 1]["values"] if header_index > 0 else rows[header_index]["values"]
        sub = rows[header_index]["values"] if header_index > 0 else []
        base_indexes = {
            index for field, index in columns.items()
            if index is not None and field not in {"unit_price", "line_total"}
        }
        price_aliases = aliases_for("unit_price", profile)
        first_supplier = next(
            (
                index for index in range(len(top))
                if str(top[index] or "").strip()
                and any(
                    0 <= candidate < len(sub)
                    and normalize_header(sub[candidate]) in price_aliases
                    for candidate in (index - 1, index, index + 1)
                )
            ),
            len(top),
        )
        if first_supplier == len(top):
            # No UP/Rate aliases: supplier names themselves are the price
            # headers.  A supplier candidate must have a non-zero commercial
            # value beneath it; blank/zero-only columns are ignored.
            first_supplier = next(
                (index for index, value in enumerate(top)
                 if str(value or "").strip()
                 and index not in base_indexes
                 and any(cls._is_nonzero_number(row["values"][index])
                         for row in rows[header_index + 1:]
                         if index < len(row["values"]))),
                len(top),
            )
        suppliers = []
        for index, value in enumerate(top):
            name = str(value or "").strip()
            if not name or re.fullmatch(r"l\s*\d+", normalize_header(name)):
                name = cls._meaningful_header_label(rows, header_index, index)
            if (
                not name or index < first_supplier or index in base_indexes
                or normalize_header(name) in aliases_for("product_name", profile)
                or normalize_header(name) in {"sl no", "serial no", "no", "qty", "quantity", "unit"}
            ):
                continue
            total_col = None
            inferred_quantity_col = None
            next_starts = [j for j, v in enumerate(top) if j > index and str(v or "").strip()]
            boundary = min(next_starts) if next_starts else len(headers)
            # Supplier headers normally align with their price column.  Do
            # not scan right into the next supplier group (or a shared budget
            # column) when looking for a price subheader.
            price_col = next(
                (candidate for candidate in range(index, min(boundary, len(sub)))
                 if normalize_header(sub[candidate]) in aliases_for("unit_price", profile)),
                None,
            )
            if price_col is None and index < len(top) and any(
                cls._is_nonzero_number(row["values"][index])
                for row in rows[header_index + 1:]
                if index < len(row["values"])
            ):
                price_col = index
            if price_col is None and not any(
                row["values"][index] not in (None, "")
                for row in rows[header_index + 1:]
                if index < len(row["values"])
            ):
                # Empty shared/budget header columns must not borrow a
                # neighbouring supplier's price pattern.
                continue
            if price_col is None:
                first_data = next(
                    (
                        row["values"] for row in rows[header_index + 1:]
                        if index < len(row["values"])
                        and (
                            (
                                index + 4 < len(row["values"])
                                and row["values"][index + 1] not in (None, "")
                                and cls._is_nonzero_number(row["values"][index + 2])
                                and cls._is_nonzero_number(row["values"][index + 4])
                            )
                            or (
                                index + 3 < len(row["values"])
                                and row["values"][index] not in (None, "")
                                and cls._is_nonzero_number(row["values"][index + 1])
                                and cls._is_nonzero_number(row["values"][index + 3])
                            )
                        )
                    ),
                    None,
                )
                if first_data is None:
                    continue
                if index + 4 < len(first_data) and (
                    first_data[index + 1] not in (None, "")
                    and cls._is_nonzero_number(first_data[index + 2])
                    and cls._is_nonzero_number(first_data[index + 4])
                ):
                    price_col = index + 2
                    inferred_quantity_col = index + 3
                    total_col = index + 4
                elif index + 3 < len(first_data) and (
                    first_data[index] not in (None, "")
                    and cls._is_nonzero_number(first_data[index + 1])
                    and cls._is_nonzero_number(first_data[index + 3])
                ):
                    price_col = index + 1
                    inferred_quantity_col = index + 2
                    total_col = index + 3
                else:
                    continue
            if total_col is None:
                total_col = next((candidate for candidate in range(price_col, min(boundary, len(sub))) if normalize_header(sub[candidate]) in aliases_for("line_total", profile)), None)
            quantity_col = next((candidate for candidate in range(max(index, 0), min(boundary, len(sub))) if normalize_header(sub[candidate]) in aliases_for("quantity", profile)), None)
            if quantity_col is None and inferred_quantity_col is not None and inferred_quantity_col < boundary:
                quantity_col = inferred_quantity_col
            uom_col = next((candidate for candidate in range(max(index, 0), min(boundary, len(sub))) if normalize_header(sub[candidate]) in aliases_for("uom", profile)), None)
            currency = cls._currency_from_text(" ".join(str(v or "") for v in top[max(0, index - 1):boundary]))
            if currency is None:
                for data_row in rows[header_index + 1:]:
                    formats = data_row.get("number_formats") or []
                    format_text = " ".join(
                        formats[col] for col in (price_col, total_col)
                        if col is not None and col < len(formats)
                    )
                    currency = cls._currency_from_text(format_text)
                    if currency:
                        break
            description_col = None
            # A supplier description is only the text column immediately
            # belonging to this supplier group.  Never scan left across a
            # previous supplier's columns, since that leaks descriptions.
            if price_col > index:
                for data_row in rows[header_index + 1:]:
                    data_values = data_row["values"]
                    if price_col >= len(data_values) or not cls._is_nonzero_number(data_values[price_col]):
                        continue
                    group_start = index
                    if index > 0 and not str(top[index - 1] or "").strip():
                        group_start = index - 1
                    for candidate in range(price_col - 1, group_start - 1, -1):
                        if candidate in {quantity_col, uom_col}:
                            continue
                        candidate_value = data_values[candidate] if candidate < len(data_values) else None
                        if candidate_value in (None, ""):
                            continue
                        try:
                            float(str(candidate_value).replace(",", "").strip())
                        except (TypeError, ValueError):
                            description_col = candidate
                            break
                    break
            if description_col is None and index > 0 and not str(top[index - 1] or "").strip():
                candidate = index - 1
                if candidate < len(rows[header_index + 1]["values"]):
                    candidate_value = rows[header_index + 1]["values"][candidate]
                    if candidate_value not in (None, "") and not cls._is_nonzero_number(candidate_value):
                        description_col = candidate
            suppliers.append((name, price_col, total_col, boundary, quantity_col, uom_col, description_col, currency))
        return suppliers

    @staticmethod
    def _meaningful_header_label(rows, header_index, column):
        """Return the nearest real group label above a commercial column."""
        ignored = {"rank", "level", "category", "quote", "quotation", "currency"}
        for row_index in range(header_index - 1, -1, -1):
            values = rows[row_index]["values"]
            if column >= len(values):
                continue
            label = str(values[column] or "").strip()
            normalized = normalize_header(label)
            if not label or normalized in ignored or re.fullmatch(r"l\s*\d+", normalized):
                continue
            return label
        return ""

    @staticmethod
    def _currency_from_text(text):
        value = str(text or "").upper()
        for code, patterns in {
            "GBP": ("GBP", "£"), "AED": ("AED", "د.إ"), "USD": ("USD", "$"),
            "EUR": ("EUR", "€"), "CNY": ("CNY", "RMB", "¥"),
        }.items():
            if any(pattern in value for pattern in patterns):
                return code
        return None

    @classmethod
    def _base_attributes(cls, values, header_values, columns, supplier_indexes=None):
        excluded = {index for index in columns.values() if index is not None}
        excluded.update(supplier_indexes or set())
        attrs = []
        for index, value in enumerate(values):
            text = str(value or "").strip()
            label = str(header_values[index] or "").strip() if index < len(header_values) else ""
            if not text or not label or (index in excluded and normalize_header(label) not in aliases_for("remark")):
                continue
            label_norm = normalize_header(label)
            if label_norm in {"sl no", "serial no", "no"} or label_norm.startswith(("mto", "supplier")):
                continue
            if label_norm in {"up", "tp", "qty", "amount", "amt", "rate", "price"}:
                continue
            try:
                float(text.replace(",", "").strip())
                continue
            except ValueError:
                pass
            attrs.append({"label": label, "value": text})
        return attrs

    @classmethod
    def _grouped_column(cls, rows, header_index, field, profile=None):
        aliases = aliases_for(field, profile)
        top_values = rows[header_index - 1]["values"]
        sub_values = rows[header_index]["values"]
        # Base/shared columns take precedence over supplier subcolumns.  In a
        # grouped table, a supplier's Description or Qty header may otherwise
        # be mistaken for the document's product or quantity column.
        for index, value in enumerate(top_values):
            normalized = normalize_header(value)
            if field == "quantity" and ("quantity" in normalized or normalized in {"base qty", "base quantity"}):
                return index
            if normalized in aliases or (
                field == "product_name" and normalized in {"product", "description", "item", "item description"}
            ):
                return index
        for index, value in enumerate(sub_values):
            if normalize_header(value) in aliases:
                return index
        # Combined labels such as "Quantity in meters" still identify the
        # quantity column structurally when no exact alias exists.
        if field == "quantity":
            for index in range(len(top_values)):
                normalized = normalize_header(top_values[index])
                if "quantity" in normalized or normalized in {"base qty", "base quantity"}:
                    return index
        return None

    @staticmethod
    def _supplier_description_column(supplier_columns, supplier, columns, values):
        for group in supplier_columns:
            if len(group) < 4:
                continue
            name, price_col, total_col, boundary = group[:4]
            if name == supplier:
                candidates = [index for index in range(price_col - 1, -1, -1) if index not in {price_col, total_col}]
                if candidates and candidates[0] < len(values) and values[candidates[0]] not in (None, ""):
                    value = values[candidates[0]]
                    try:
                        float(str(value).replace(",", "").strip())
                        return None
                    except (TypeError, ValueError):
                        return candidates[0]
                return None
        return None

    @classmethod
    def _eligible_commercial_rows(cls, rows, profile=None):
        """Keep only worksheets that contain a real priced table."""
        by_sheet = {}
        order = []
        for row in rows:
            sheet = row.get("sheet") or ""
            if sheet not in by_sheet:
                by_sheet[sheet] = []
                order.append(sheet)
            by_sheet[sheet].append(row)

        eligible = []
        for sheet in order:
            sheet_rows = by_sheet[sheet]
            header_index = next(
                (
                    index for index, row in enumerate(sheet_rows[:30])
                    if sum(
                        normalize_header(value) in aliases_for(field, profile)
                        for value in row["values"]
                        for field in FIELDS
                    ) >= 2
                ),
                None,
            )
            if header_index is None:
                continue
            headers = [normalize_header(value) for value in sheet_rows[header_index]["values"]]
            product_column = next(
                (index for index, value in enumerate(headers) if value in aliases_for("product_name", profile)),
                None,
            )
            price_columns = {
                index for index, value in enumerate(headers)
                if value in aliases_for("unit_price", profile) or value in aliases_for("line_total", profile)
            }
            if product_column is None or not price_columns:
                grouped_header = cls._find_grouped_header(sheet_rows, profile)
                if grouped_header is not None:
                    header_index = grouped_header
                    product_column = cls._grouped_column(sheet_rows, header_index, "product_name", profile)
                    price_columns = {
                        index for index in range(len(sheet_rows[header_index]["values"]))
                        if normalize_header(sheet_rows[header_index]["values"][index]) in aliases_for("unit_price", profile)
                        or normalize_header(sheet_rows[header_index]["values"][index]) in aliases_for("line_total", profile)
                    }
            if product_column is None or not price_columns:
                # Supplier names may be the only price headers.  Keep the
                # worksheet eligible when numeric commercial cells prove the
                # table is priced; grouped extraction will map each column.
                structural_header = header_index
                if structural_header is not None and cls._has_structural_supplier_prices(sheet_rows, structural_header, profile):
                    product_column = cls._grouped_column(sheet_rows, structural_header, "product_name", profile)
                    price_columns = {
                        index for index, value in enumerate(sheet_rows[structural_header]["values"])
                        if index > (product_column if product_column is not None else -1)
                        and any(
                            cls._is_nonzero_number(row["values"][index])
                            for row in sheet_rows[structural_header + 1:]
                            if index < len(row["values"])
                        )
                    }
            if product_column is None or not price_columns:
                continue
            has_priced_row = any(
                any(cls._is_nonzero_number(row["values"][index]) for index in price_columns if index < len(row["values"]))
                for row in sheet_rows[header_index + 1:]
            )
            if not has_priced_row and cls._has_structural_supplier_prices(sheet_rows, header_index, profile):
                has_priced_row = True
            if has_priced_row:
                eligible.extend(sheet_rows)
        return eligible

    @classmethod
    def _supplier_columns(cls, rows, header_index, headers, profile=None):
        """Return (supplier, unit-price-column, amount-column) groups.

        Comparison workbooks commonly put supplier names on a row above the
        U.P./Amount headers. The physical columns are authoritative; blanks
        must remain blanks instead of shifting later supplier values left.
        """
        if header_index < 1:
            return []
        supplier_row = None
        for candidate in range(header_index - 1, max(-1, header_index - 3), -1):
            if any(str(value or "").strip() for value in rows[candidate]["values"]):
                supplier_row = rows[candidate]["values"]
                break
        if supplier_row is None:
            return []
        result = []
        seen = set()
        for index, value in enumerate(supplier_row):
            supplier = str(value or "").strip()
            if not supplier or index in seen:
                continue
            # A supplier group is identified by a price header in the same
            # physical column or the immediately following amount column.
            price_col = None
            total_col = None
            for candidate in (index, index + 1):
                if candidate >= len(headers):
                    continue
                header = headers[candidate]
                if header in aliases_for("unit_price", profile):
                    price_col = candidate
                    if candidate + 1 < len(headers) and headers[candidate + 1] in aliases_for("line_total", profile):
                        total_col = candidate + 1
                    break
            if price_col is None:
                continue
            if total_col is None and price_col + 1 < len(headers) and headers[price_col + 1] in aliases_for("line_total", profile):
                total_col = price_col + 1
            result.append((supplier, price_col, total_col))
            seen.update({price_col, total_col})
        return result

    @staticmethod
    def _value(values, index):
        return values[index] if index is not None and index < len(values) and values[index] != "" else None

    @staticmethod
    def _is_nonzero_number(value):
        if value in (None, ""):
            return False
        try:
            return float(str(value).replace(",", "").strip()) != 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_number(value):
        try:
            float(str(value).replace(",", "").strip())
            return True
        except (TypeError, ValueError):
            return False

    @classmethod
    def _detect_section_heading(cls, values, columns, supplier_columns, profile=None):
        """Infer a section row from its structure, never from its wording."""
        price_indexes = set()
        for group in supplier_columns or []:
            _supplier, price_col, total_col = group[:3]
            if price_col is not None:
                price_indexes.add(price_col)
            if total_col is not None:
                price_indexes.add(total_col)
        if not supplier_columns:
            for field in ("unit_price", "line_total"):
                index = columns.get(field)
                if index is not None:
                    price_indexes.add(index)

        quantity_indexes = {
            index for index in (columns.get("quantity"), columns.get("uom"))
            if index is not None
        }
        if any(
            cls._is_nonzero_number(values[index])
            for index in price_indexes
            if index < len(values)
        ):
            return None
        if any(
            cls._has_meaningful_quantity(values[index])
            for index in quantity_indexes
            if index < len(values)
        ):
            return None

        excluded_indexes = price_indexes | quantity_indexes
        text_parts = []
        for index, value in enumerate(values):
            if index in excluded_indexes:
                continue
            text = str(value or "").strip()
            if not text or cls._is_serial_number(text) or cls._is_zero_number(text):
                continue
            text_parts.append(text)
        if not text_parts:
            return None

        heading = " ".join(text_parts).strip()
        normalized = normalize_header(heading)
        if normalized.startswith(("total", "subtotal", "grand total")):
            return None
        header_hits = sum(
            normalize_header(part) in aliases_for(field, profile)
            for part in text_parts
            for field in FIELDS
        )
        if header_hits >= 2:
            return None
        if cls._looks_like_product_identity(heading):
            return None
        return heading

    @staticmethod
    def _looks_like_product_identity(text):
        value = str(text or "").strip()
        normalized = normalize_header(value)
        return bool(
            re.search(r"\b\d+(?:\.\d+)?\s*[x×*]\s*\d", value, re.I)
            or re.search(r"\b\d+(?:\.\d+)?\s*(?:mm2?|cm|m|kv|v|hz|kw|a|amp|rpm|ip\s*\d+)\b", value, re.I)
            or re.search(r"\b[A-Z]{1,8}[-/]\d[A-Z0-9-]*\b", value, re.I)
            or re.search(r"\b\d+[A-Z]{1,6}\b", value, re.I)
            # Section headings can legitimately contain words such as
            # "TYPE" or "SIZE" (for example, a cable-lug section). Those
            # words alone are not evidence that the heading is a product row;
            # require a technical identifier/measurement above instead.
            or re.search(r"\b(?:model|part|rating|voltage|specification)\b", normalized, re.I)
        )

    @staticmethod
    def _is_zero_number(value):
        try:
            return float(str(value).replace(",", "").strip()) == 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_serial_number(value):
        return str(value or "").strip().isdigit()

    @staticmethod
    def _has_meaningful_quantity(value):
        if value in (None, ""):
            return False
        try:
            return float(str(value).replace(",", "").strip()) != 0
        except (TypeError, ValueError):
            return bool(re.search(r"\d", str(value)))
