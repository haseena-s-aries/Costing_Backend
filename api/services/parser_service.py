import csv
import email
import io
import re
from decimal import Decimal, InvalidOperation
from dateutil import parser as date_parser
from pathlib import Path
from datetime import date, datetime
import openpyxl
import pdfplumber
from PIL import Image
from rapidfuzz import fuzz

from api.constants import SUPPORTED_EXTENSIONS
from api.exceptions import ImportServiceError
from api.services.parser_utils import (
    INVOICE_ITEM_ROW_PATTERN,
    SECTION_CONTINUATION_STOP_PATTERN,
    clean_spaces,
    is_section_stop_line,
    strip_section_continuation,
)

DOCUMENT_SCHEMA = {
    "supplier": [
        "supplier",
        "supplier name",
        "vendor",
        "seller",
        "company",
        "quotation from",
        "order issued to",
        "issued by",
        "vendor name",
    ],
    "supplier_address": ["supplier address", "vendor address", "seller address", "from address"],
    "supplier_email": ["supplier email", "vendor email", "email", "e-mail"],
    "supplier_phone": ["supplier phone", "vendor phone", "phone", "tel", "telephone"],
    "buyer_name": [
        "buyer",
        "buyer name",
        "customer",
        "customer name",
        "client",
    ],
    "buyer_company": ["buyer company", "customer company", "bill to", "ship to"],
    "buyer_address": ["buyer address", "customer address", "bill to address", "ship to address"],
    "document_number": ["document number", "document no", "doc no", "quote number", "quote no", "quote #", "quotation no", "invoice no", "po no", "lpo no", "grn no", "reference", "ref no"],
    "quotation_number": ["quotation number", "quotation no", "quote number", "quote no", "quote #"],
    "purchase_order_number": ["purchase order number", "purchase order no", "po number", "po no"],
    "invoice_number": ["invoice number", "invoice no"],
    "lpo_number": ["lpo number", "lpo no"],
    "grn_number": ["grn number", "grn no"],
    "reference_number": ["reference number", "reference no", "ref no"],
    "document_date": ["document date", "date", "quote date", "quotation date", "invoice date", "po date"],
    "delivery_date": ["delivery date", "required delivery date", "delivery on"],
    "quotation_validity": ["quotation validity", "validity", "valid until"],
    "currency": ["currency", "curr", "currency code"],
    "subtotal": ["subtotal", "sub total", "before tax total"],
    "vat": ["vat", "vat amount", "tax", "tax amount"],
    "discount": ["discount"],
    "freight": ["freight"],
    "other_charges": ["other charges", "other charge", "handling charge"],
    "grand_total": ["grand total", "net total", "total amount", "amount due", "total"],
    "payment_terms": ["payment terms", "payment term", "terms of payment"],
    "country": ["country", "origin country", "country of origin"],
}

LINE_SCHEMA = {
    "product_name": ["description", "product", "product description", "item", "item description", "material description", "particulars", "details", "product details", "service description", "goods description"],
    "quantity": ["qty", "quantity", "order qty", "ordered quantity", "required qty", "required quantity"],
    "unit_of_measurement": ["uom", "unit", "unit of measure", "unit of measurement", "measure"],
    "unit_price": ["rate", "unit rate", "price", "unit price", "quoted rate", "quoted price", "purchase rate", "price per unit"],
    "currency": ["currency", "curr", "currency code"],
    "line_total": ["amount", "total price", "line total", "line amount", "total amount", "extended price", "net amount"],
}

VIRTUAL_HEADERS = ["row_type", *LINE_SCHEMA.keys()]
DOCUMENT_LABELS = {alias for aliases in DOCUMENT_SCHEMA.values() for alias in aliases}
LINE_LABELS = {alias for aliases in LINE_SCHEMA.values() for alias in aliases}
CHARGE_TERMS = {"freight", "tax", "vat", "certificate", "fee", "charge", "clearance", "discount", "other charges"}
TOTAL_STOP_TERMS = {"subtotal", "sub total", "grand total", "net total", "total before", "amount in words", "terms", "terms and conditions", "bank details"}
NOISE_TERMS = {"phone", "fax", "trn", "vat no", "vat number", "tax registration", "email", "www", "po box", "address"}
SERVICE_TERMS = {"transport", "transportation", "delivery", "installation", "service", "labor", "labour", "commissioning"}
OCR_TEXT_MINIMUM = 40
MIN_LINE_CONFIDENCE = 55
CATEGORY_ALIASES = [
    ("PURCHASE_ORDER", ["purchase order", "p.o. number", "po number", "po no"]),
    ("GRN", ["goods received note", "goods receipt note", "grn"]),
    ("LPO", ["local purchase order", "lpo"]),
    ("INVOICE", ["tax invoice", "commercial invoice", "invoice"]),
    ("QUOTATION", ["supplier quotation", "quotation list", "quotation", "quote #", "quote", "qtn#", "qtn #", "offer below", "our offer", "sales contract"]),
    ("PRICE_LIST", ["price list"]),
]
def _fix_pdf_text(value):
    return (
        str(value or "")
        .replace("(cid:43)", "t")
        .replace("(cid:46)", "ft")
        .replace("(cid:12)", "ti")
    )

def _clean(value):
    if value in ("", None):
        return None
    return str(value).strip() if isinstance(value, str) else value


def _norm(value):
    return re.sub(r"[^a-z0-9$]+", " ", str(value or "").replace("\n", " ").lower()).strip()


def _document_type(filename, suffix, text=""):
    return _detect_category(text or filename)


def _detect_category(text):
    header_text = "\n".join(str(text or "").splitlines()[:12]).lower()
    full_text = str(text or "").lower()
    # Invoice title in the document header must win over PO/reference text
    # that appears later as buyer reference.
    if re.search(r"\b(?:commercial|tax|proforma)?\s*invoice\b", header_text, re.I):
        return "INVOICE"
    for category, aliases in CATEGORY_ALIASES:
        if any(alias in header_text for alias in aliases):
            return category
    for category, aliases in CATEGORY_ALIASES:
        if category == "INVOICE":
            continue
        if any(alias in full_text for alias in aliases):
            return category
    if any(alias in full_text for alias in dict(CATEGORY_ALIASES)["INVOICE"]):
        return "INVOICE"
    return "OTHER"


def _match_field(label, schema):
    normalized = _norm(label)
    if not normalized:
        return None, 0
    for field, aliases in schema.items():
        if normalized in {_norm(alias) for alias in aliases}:
            return field, 100
    best = (None, 0)
    for field, aliases in schema.items():
        for alias in aliases:
            alias_norm = _norm(alias)
            score = fuzz.token_set_ratio(normalized, alias_norm)
            if score > best[1]:
                best = (field, score)
    return best if best[1] >= MIN_LINE_CONFIDENCE else (None, best[1])

def _decimal(value):
    if value in (None, ""):
        return None
    try:
        cleaned = re.sub(
            r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b|[$\u20ac\u00a3\u00a5]",
            "",
            str(value),
            flags=re.I,
        )
        cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()

        # European quotation style:
        # $ 1 .400,00 => 1400.00
        # $ 2.800,00 => 2800.00
        # 2.050 => 2050
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", cleaned):
            cleaned = cleaned.replace(",", "")
        elif re.search(r"\d\s*\.\s*\d{3},\d{2}", str(value or "")):
            cleaned = re.sub(r"\s+", "", cleaned)
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned and re.fullmatch(r"\d+,\d{2}", cleaned):
            cleaned = cleaned.replace(",", ".")
        elif "," not in cleaned and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        else:
            cleaned = cleaned.replace(",", "")

        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _decimal_comma_decimal(value):
    if value in (None, ""):
        return None
    cleaned = re.sub(
        r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b|[$\u20ac\u00a3\u00a5]",
        "",
        str(value),
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()
    if not re.fullmatch(r"-?\d+,\d{1,3}", cleaned):
        return None
    try:
        return Decimal(cleaned.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


class ParserService:
    def _material_description_map_from_pdf_table(self, table):
        if not table:
            return {}

        cleaned = [
            [
                re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()
                for cell in row
            ]
            for row in table
            if row and any(str(cell or "").strip() for cell in row)
        ]

        result = {}
        material_terms = re.compile(
            r"\b(?:thickness|swl|base|cover|steel|ss\s*316|stainless|"
            r"aluminium|aluminum|cast|iron|bronze|rubber|"
            r"plastic|grade)\b",
            re.I,
        )

        for row in cleaned:
            material = next(
                (
                    cell
                    for cell in row
                    if cell and (
                        material_terms.search(str(cell))
                        or material_terms.search(_norm(cell))
                    )
                ),
                None,
            )
            if not material:
                continue

            product = None
            for cell in row:
                cell_text = self._clean_product_description(cell)
                cell_norm = _norm(cell_text)

                if not cell_text or cell_text == material:
                    continue
                if _decimal(cell_text) is not None:
                    continue
                if cell_norm in {"sr", "sr no", "item description", "material description"}:
                    continue
                if material_terms.search(cell_text) or material_terms.search(cell_norm):
                    continue
                if len(re.findall(r"[A-Za-z]", cell_text)) < 4:
                    continue

                product = cell_text
                break

            if product:
                result.setdefault(_norm(product), self._clean_product_description(material))

        return result

    def _description_map_from_pdf_table(self, table):
        """
        Build {part_number: full_description} from a generic PDF table.

        Handles merged PDF headers where the header label and the actual
        data column are shifted by one or more empty spacer columns.
        """
        if not table:
            return {}

        cleaned = [
            [
                re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()
                for cell in row
            ]
            for row in table
            if row and any(str(cell or "").strip() for cell in row)
        ]

        if len(cleaned) < 2:
            return {}

        header_index = None
        part_header_index = None
        description_header_index = None

        for row_index, row in enumerate(cleaned[:12]):
            normalized = [_norm(cell) for cell in row]

            current_part = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if (
                        value in {"part no", "part number", "p n", "pn", "model"}
                        or "part no" in value
                        or "part number" in value
                        or "model code" in value
                    )
                ),
                None,
            )

            current_description = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if (
                        value in {
                            "description",
                            "item description",
                            "product description",
                            "description of goods",
                            "details",
                            "item",
                        }
                        or "description" in value
                        or "details" in value
                    )
                ),
                None,
            )

            if current_part is not None and current_description is not None:
                header_index = row_index
                part_header_index = current_part
                description_header_index = current_description
                break

        if (
            header_index is None
            or part_header_index is None
            or description_header_index is None
        ):
            return {}

        def nearest_nonempty(row, preferred_index, radius=3):
            """
            PDF tables with merged headers often place the header text in
            one cell while row values begin in the adjacent cell.
            """
            if preferred_index < len(row) and str(row[preferred_index] or "").strip():
                return str(row[preferred_index]).strip()

            for distance in range(1, radius + 1):
                for candidate_index in (
                    preferred_index - distance,
                    preferred_index + distance,
                ):
                    if (
                        0 <= candidate_index < len(row)
                        and str(row[candidate_index] or "").strip()
                    ):
                        return str(row[candidate_index]).strip()

            return None

        result = {}

        for row in cleaned[header_index + 1:]:
            joined = _norm(" ".join(row))

            if joined.startswith(
                (
                    "total",
                    "subtotal",
                    "grand total",
                    "commercial terms",
                    "terms and conditions",
                )
            ):
                break

            raw_part_number = nearest_nonempty(
                row,
                part_header_index,
                radius=3,
            )

            # Description columns are usually wider and should not borrow
            # distant numeric cells. Use a smaller search radius.
            raw_description = nearest_nonempty(
                row,
                description_header_index,
                radius=1,
            )

            part_number = re.sub(
                r"\s+",
                "",
                str(raw_part_number or ""),
            ).strip(" |,;:")

            description = self._clean_product_description(
                raw_description
            )

            if not part_number or not description:
                continue

            # Reject serial numbers, quantities and prices as part numbers.
            if not re.search(r"[A-Za-z]", part_number):
                continue

            if not re.search(r"[A-Za-z]", description):
                continue

            normalized_description = _norm(description)

            if normalized_description.startswith(
                (
                    "total",
                    "subtotal",
                    "grand total",
                    "terms",
                    "note",
                )
            ):
                continue

            result.setdefault(
                part_number.upper(),
                description,
            )

        return result


    def _enrich_products_from_native_text(
        self,
        sheets,
        text,
        table_description_map=None,
    ):
        """
        Produce: PART NUMBER | FULL DESCRIPTION.

        Priority:
        1. Description from the same native PDF table.
        2. Strict native-text fallback.

        Quantity, UoM, prices, supplier and all other fields are untouched.
        """
        if not sheets:
            return

        description_map = {
            str(key or "").upper(): str(value or "").strip()
            for key, value in (table_description_map or {}).items()
            if key and value
        }

        lines = [
            re.sub(r"\s+", " ", str(line or "")).strip()
            for line in str(text or "").splitlines()
            if str(line or "").strip()
        ]

        description_map.update({
            str(key or "").upper(): str(value or "").strip()
            for key, value in self._part_item_description_map_from_quote_lines(lines).items()
            if key and value
        })
        description_map.update({
            str(key or "").upper(): str(value or "").strip()
            for key, value in self._invoice_item_description_map_from_lines(lines).items()
            if key and value
        })
        uom_map = self._invoice_item_uom_map_from_lines(lines)
        part_pattern = re.compile(
            r"^[A-Z0-9][A-Z0-9./_-]*(?:-[A-Z0-9./_-]+)+$",
            re.I,
        )

        footer_pattern = re.compile(
            r"^(?:"
            r"kind regards|best regards|regards|thanks|thank you|"
            r"from aries group|dear\b|commercial terms|terms and conditions|"
            r"total\b|subtotal\b|grand total\b|vat\b|brand\b|"
            r"delivery period|delivery terms|payment terms|validity|warranty|"
            r"await your reply|disclaimer|phone\b|mobile\b|mob\b|tel\b|"
            r"email\b|web\b|www\.|https?://|address\b"
            r")",
            re.I,
        )

        row_end_pattern = re.compile(
            r"\b\d+(?:\.\d+)?\s+"
            r"(?:PC|PCS|EA|EACH|NO|NOS|SET|PAIR|LOT|LS|KG|GM|G|"
            r"MTR|M|MM|CM|LTR|L|BOX|PACK|PKT|ROLL|TIN|DRUM|BAG|"
            r"COIL|SHEET|TUBE)\s+"
            r"[\d,]+(?:\.\d+)?\s+[\d,]+(?:\.\d+)?\b",
            re.I,
        )

        def compact(value):
            return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

        def text_fallback(part_number):
            """
            Strict fallback only. It never crosses into signatures, disclaimers,
            totals, terms, or the next product.
            """
            part_compact = compact(part_number)
            candidates = []

            for start_index, source_line in enumerate(lines):
                if part_compact not in compact(source_line):
                    continue

                parts = []
                completed = False

                for index in range(start_index, min(len(lines), start_index + 10)):
                    line = lines[index]

                    if index == start_index:
                        match = re.search(re.escape(part_number), line, re.I)
                        line = (
                            line[match.end():].strip(" |:-")
                            if match
                            else ""
                        )

                    if index != start_index and footer_pattern.search(line):
                        break

                    if (
                        index != start_index
                        and re.match(
                            r"^\d{1,3}\s+[A-Z0-9][A-Z0-9./_-]*"
                            r"(?:-[A-Z0-9./_-]+)+",
                            line,
                            re.I,
                        )
                    ):
                        break

                    first_token = line.split()[0] if line.split() else ""

                    if (
                        index != start_index
                        and part_pattern.fullmatch(first_token)
                    ):
                        break

                    row_end = row_end_pattern.search(line)

                    if row_end:
                        line = line[:row_end.start()].strip()
                        completed = True

                    line = re.sub(
                        r"\bEx[- ]?stock\b.*$",
                        "",
                        line,
                        flags=re.I,
                    ).strip()

                    if line and not footer_pattern.search(line):
                        parts.append(line)

                    if completed:
                        break

                description = self._clean_product_description(" ".join(parts))

                if not description:
                    continue

                if compact(description) == part_compact:
                    continue

                score = len(description) + (1000 if completed else 0)
                candidates.append((score, description))

            if not candidates:
                return None

            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        for sheet in sheets:
            rows = sheet.get("rows") or []

            if len(rows) < 2:
                continue

            headers = rows[0]

            if "product_name" not in headers:
                continue

            product_index = headers.index("product_name")
            part_index = (
                headers.index("part_number")
                if "part_number" in headers
                else None
            )
            item_code_index = (
                headers.index("item_code")
                if "item_code" in headers
                else None
            )
            specification_index = (
                headers.index("specification")
                if "specification" in headers
                else None
            )
            uom_index = (
                headers.index("unit_of_measurement")
                if "unit_of_measurement" in headers
                else None
            )
            for row in rows[1:]:
                current_product = (
                    str(row[product_index] or "").strip()
                    if product_index < len(row)
                    else ""
                )
                if (
                    " | " in current_product
                    and not re.search(r"\b(?:AED|USD|EUR|GBP|INR)\b\s*\d", current_product, re.I)
                ):
                    continue

                part_number = None

                for candidate_index in (part_index, item_code_index):
                    if (
                        candidate_index is not None
                        and candidate_index < len(row)
                        and row[candidate_index]
                    ):
                        candidate = re.sub(
                            r"\s+",
                            "",
                            str(row[candidate_index]),
                        ).strip(" |,;:")

                        if candidate:
                            part_number = candidate
                            break

                if not part_number and current_product:
                    # Product name may already contain a partial description,
                    # for example:
                    # "6ES7231-4HF50-0XB0 inputs: 8x AI..."
                    #
                    # Extract only the leading industrial part number instead
                    # of treating the entire product text as the part number.
                    leading_part_match = re.match(
                        r"^\s*("
                        r"[A-Z0-9][A-Z0-9./_-]*"
                        r"(?:-[A-Z0-9./_-]+)+"
                        r")(?=\s|\||$)",
                        current_product,
                        re.I,
                    )

                    if leading_part_match:
                        part_number = leading_part_match.group(1).strip()

                if not part_number:
                    continue

                explicit_item_code = (
                    str(row[item_code_index] or "").strip()
                    if item_code_index is not None
                    and item_code_index < len(row)
                    and row[item_code_index]
                    else ""
                )
                lookup_keys = [
                    part_number,
                    explicit_item_code,
                ]
                description = next(
                    (
                        description_map.get(str(key or "").upper())
                        for key in lookup_keys
                        if description_map.get(str(key or "").upper())
                    ),
                    None,
                )
                if not description and is_section_stop_line(current_product):
                    loose_lookup_keys = [
                        str(part_number or "").split()[0] if str(part_number or "").split() else "",
                        str(explicit_item_code or "").split()[0] if str(explicit_item_code or "").split() else "",
                    ]
                    description = next(
                        (
                            description_map.get(str(key or "").upper())
                            for key in loose_lookup_keys
                            if description_map.get(str(key or "").upper())
                        ),
                        None,
                    )

                if not description and re.fullmatch(r"[A-Z0-9][A-Z0-9./_-]*(?:-[A-Z0-9./_-]+)+", str(part_number or ""), re.I):
                    description = text_fallback(part_number)

                if not description:
                    continue

                description = strip_section_continuation(description)
                description = re.sub(
                    r"\s+",
                    " ",
                    str(description),
                ).strip()

                # Remove commercial columns accidentally merged before description.
                #
                # Examples:
                # 2.00 9,450.00 5.00 945.00 18,900.00 TAPFLO...
                # 1.00 4,900.00 5.00 245.00 4,900.00 TAPFLO...
                number_token = r"\d[\d,]*(?:\.\d+)?"

                description = re.sub(
                    rf"^\s*"
                    rf"{number_token}"  # Quantity
                    rf"(?:\s+(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS))?"
                    rf"\s+{number_token}"  # Unit price
                    rf"\s+{number_token}"  # VAT %
                    rf"\s+{number_token}"  # VAT amount
                    rf"\s+{number_token}"  # Line total
                    rf"\s*",
                    "",
                    description,
                    count=1,
                    flags=re.I,
                ).strip(" |:-")

                # Handle descriptions where UoM was extracted after commercial values.
                description = re.sub(
                    r"^\s*(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS)\s+",
                    "",
                    description,
                    count=1,
                    flags=re.I,
                ).strip(" |:-")

                # Remove wrongly inserted UoM inside the readable description:
                # "Diaphragm pcs Pump" -> "Diaphragm Pump"
                description = re.sub(
                    r"\b(Diaphragm)\s+(?:PCS?|PC|EA|EACH|NOS?|UNIT)\s+(Pump)\b",
                    r"\1 \2",
                    description,
                    flags=re.I,
                )

                description = re.sub(
                    rf"^\s*{re.escape(part_number)}\s*[|:\-]?\s*",
                    "",
                    description,
                    flags=re.I,
                ).strip()

                if not description or footer_pattern.search(description):
                    continue

                if " | " in description and explicit_item_code and not re.search(r"\d", explicit_item_code):
                    row[product_index] = description

                    if (
                        specification_index is not None
                        and specification_index < len(row)
                    ):
                        row[specification_index] = description

                    if (
                        uom_index is not None
                        and uom_index < len(row)
                        and not row[uom_index]
                    ):
                        uom_value = uom_map.get(str(part_number or "").upper())
                        if not uom_value and explicit_item_code:
                            uom_value = uom_map.get(str(explicit_item_code).upper())
                        if uom_value:
                            row[uom_index] = uom_value

                    continue
               

                row[product_index] = description

                if (
                    specification_index is not None
                    and specification_index < len(row)
                ):
                    row[specification_index] = description

                if (
                    uom_index is not None
                    and uom_index < len(row)
                    and not row[uom_index]
                ):
                    uom_value = uom_map.get(str(part_number or "").upper())
                    if not uom_value and explicit_item_code:
                        uom_value = uom_map.get(str(explicit_item_code).upper())
                    if uom_value:
                        row[uom_index] = uom_value

    def _enrich_products_with_table_material(self, sheets, material_map):
        if not sheets or not material_map:
            return

        for sheet in sheets:
            rows = sheet.get("rows") or []
            if len(rows) < 2:
                continue

            headers = rows[0]
            if "product_name" not in headers:
                continue

            product_index = headers.index("product_name")
            material_index = headers.index("material") if "material" in headers else None

            for row in rows[1:]:
                if product_index >= len(row):
                    continue

                product_name = str(row[product_index] or "").strip()
                if not product_name:
                    continue

                material = material_map.get(_norm(product_name))
                if not material:
                    continue

                if _norm(material) not in _norm(product_name):
                    row[product_index] = f"{product_name} | {material}".strip(" |")

                if material_index is not None and material_index < len(row) and not row[material_index]:
                    row[material_index] = material

    def _invoice_item_description_map_from_lines(self, lines):
        result = {}
        if not lines:
            return result

        for index, line in enumerate(lines):
            match = INVOICE_ITEM_ROW_PATTERN.match(line)
            if not match:
                continue

            body = self._clean_product_description(match.group("body"))
            code = match.group("code").strip()
            extras = []
            for extra in lines[index + 1:index + 30]:
                if INVOICE_ITEM_ROW_PATTERN.match(extra) or is_section_stop_line(extra):
                    break
                extra = strip_section_continuation(extra)
                normalized_extra = _norm(extra)
                if not normalized_extra or normalized_extra in {"brand preetemp"}:
                    continue
                extras.append(extra)

            description = self._clean_product_description(strip_section_continuation(" ".join([body, *extras])))
            description = re.sub(r"\s+", " ", description).strip(" |:-")
            if not description:
                continue

            result[code.upper()] = description
            leading = description.split()[0] if description.split() else ""
            if leading and re.search(r"[A-Za-z]", leading):
                result.setdefault(leading.upper(), description)

        return result
    def _invoice_item_uom_map_from_lines(self, lines):
        result = {}

        for line in lines:
            match = INVOICE_ITEM_ROW_PATTERN.match(line)
            if not match:
                continue
            uom = match.group("uom").replace(".", "")
            if uom.lower() == "nos":
                uom = "Nos"
            code = match.group("code").upper()
            result[code] = uom

            body_first = match.group("body").split()[0].upper()
            if body_first:
                result.setdefault(body_first, uom)

        return result
    def _cleanup_extracted_line_item_rows(self, sheets):
        if not sheets:
            return

        commercial_tail_pattern = re.compile(
            r"\s+\d+(?:\.\d+)?\s+"
            r"(?:no\.?s|nos|pcs?|ea|each|unit|set|lot|ls|pkt|roll|mtr|meter|kg|tin)\s+"
            r"\d[\d,]*(?:\.\d+)?\s+"
            r"\d[\d,]*(?:\.\d+)?\s+"
            r"\d[\d,]*(?:\.\d+)?\b.*$",
            re.I,
        )
        terms_pattern = re.compile(
            r"\b(?:product specifications|prices and validity|order confirmation|"
            r"delivery timeline|payment terms|shipping and risk|packing charges|"
            r"cancellation and modifications|returns and replacements|warranty|"
            r"ownership of goods|intellectual property|confidentiality|force majeure|"
            r"technical support|governing law|acceptance of terms|customs clearance|"
            r"demurrage|non-refundable|orders once confirmed|return requests)\b",
            re.I,
        )

        for sheet in sheets:
            rows = sheet.get("rows") or []
            if len(rows) < 2:
                continue
            headers = rows[0]
            if "product_name" not in headers:
                continue

            product_index = headers.index("product_name")
            specification_index = headers.index("specification") if "specification" in headers else None
            unit_price_index = headers.index("unit_price") if "unit_price" in headers else None
            line_total_index = headers.index("line_total") if "line_total" in headers else None

            kept_rows = [headers]
            kept_meta = []
            row_meta = sheet.get("row_meta") or []

            for offset, row in enumerate(rows[1:]):
                product_name = str(row[product_index] or "") if product_index < len(row) else ""
                normalized_name = _norm(product_name)
                unit_price = _decimal(row[unit_price_index]) if unit_price_index is not None and unit_price_index < len(row) else None
                line_total = _decimal(row[line_total_index]) if line_total_index is not None and line_total_index < len(row) else None

                if terms_pattern.search(product_name) and (unit_price in (None, Decimal("0")) or line_total in (None, Decimal("0"))):
                    continue
                if normalized_name.startswith(("net amount", "net total", "total amount")):
                    continue

                cleaned_name = commercial_tail_pattern.sub("", product_name).strip(" |:-")
                cleaned_name = re.sub(
                    r"\s+(?:Remark|Remarks|Note|Notes)\s*:\s*.*$",
                    "",
                    cleaned_name,
                    flags=re.I,
                ).strip(" |:-")
                cleaned_name = re.sub(
                    r"\s+\d+\.\s*(?:Shipyard|Export packing|Purchase Order|Offer is valid|Other spares|Payment term|All other terms)\b.*$",
                    "",
                    cleaned_name,
                    flags=re.I,
                ).strip(" |:-")
                cleaned_name = re.sub(
                    r"\s+Amount\s+Chargeable\b.*$",
                    "",
                    cleaned_name,
                    flags=re.I,
                ).strip(" |:-")
                cleaned_name = re.sub(r"\s+", " ", cleaned_name).strip()
                if cleaned_name:
                    row[product_index] = cleaned_name

                if specification_index is not None and specification_index < len(row) and row[specification_index]:
                    cleaned_spec = commercial_tail_pattern.sub("", str(row[specification_index])).strip(" |:-")
                    cleaned_spec = re.sub(
                        r"\s+(?:Remark|Remarks|Note|Notes)\s*:\s*.*$",
                        "",
                        cleaned_spec,
                        flags=re.I,
                    ).strip(" |:-")
                    cleaned_spec = re.sub(
                        r"\s+\d+\.\s*(?:Shipyard|Export packing|Purchase Order|Offer is valid|Other spares|Payment term|All other terms)\b.*$",
                        "",
                        cleaned_spec,
                        flags=re.I,
                    ).strip(" |:-")
                    cleaned_spec = re.sub(
                        r"\s+Amount\s+Chargeable\b.*$",
                        "",
                        cleaned_spec,
                        flags=re.I,
                    ).strip(" |:-")
                    row[specification_index] = re.sub(r"\s+", " ", cleaned_spec).strip()

                kept_rows.append(row)
                if offset < len(row_meta):
                    kept_meta.append(row_meta[offset])

            sheet["rows"] = kept_rows
            if row_meta:
                sheet["row_meta"] = kept_meta


    def _hisea_goods_table_to_line_items(
        self,
        table,
        source,
        allow_continuation=False,
    ):
        """
        Parse Hi-Sea quotation product tables.

        Page 1 contains headers. Following pages may contain only continuation
        rows, so allow_continuation=True handles those pages without affecting
        unrelated PDF layouts.
        """

        if not table:
            return None

        cleaned = [
            [
                re.sub(
                    r"\s+",
                    " ",
                    str(cell or "").replace("\n", " "),
                ).strip()
                for cell in row
            ]
            for row in table
            if row
        ]

        if not cleaned:
            return None

        first_rows_text = _norm(
            " ".join(
                " ".join(row)
                for row in cleaned[:8]
            )
        )

        has_hisea_header = all(
            signature in first_rows_text
            for signature in {
                "description of goods",
                "certificate",
                "qty pcs",
                "unit price",
                "sum usd",
            }
        )

        if not has_hisea_header and not allow_continuation:
            return None

        rows = [VIRTUAL_HEADERS]
        row_meta = []

        for row_number, cells in enumerate(cleaned, start=1):
            # Ensure consistent column count.
            cells = list(cells) + [""] * max(0, 7 - len(cells))

            serial = str(cells[0] or "").strip()

            if not re.fullmatch(r"\d{1,3}", serial):
                continue

            name = str(cells[1] or "").strip()
            details = str(cells[2] or "").strip()
            certificate = str(cells[3] or "").strip()
            quantity_text = str(cells[4] or "").strip()
            unit_price_text = str(cells[5] or "").strip()
            line_total_text = str(cells[6] or "").strip()

            # Some package rows are merged across Name/Details/Certificate columns.
            combined_description = " ".join(
                value
                for value in [name, details, certificate]
                if value
            ).strip()

            normalized_description = _norm(combined_description)

            is_charge = any(
                term in normalized_description
                for term in {
                    "wooden case package cost",
                    "package cost",
                    "packing cost",
                    "freight",
                    "delivery charge",
                    "handling charge",
                }
            )

            quantity = _decimal(quantity_text)
            unit_price = _decimal(unit_price_text)
            line_total = _decimal(line_total_text)

            # Handle merged charge rows where amount may appear in another cell.
            if is_charge:
                available_numbers = [
                    _decimal(value)
                    for value in cells[1:]
                    if _decimal(value) is not None
                ]

                if line_total is None and available_numbers:
                    line_total = available_numbers[-1]

                if line_total is None:
                    continue

                values = {
                    "row_type": "CHARGE",
                    "product_name": combined_description,
                    "specification": None,
                    "material": None,
                    "quantity": "1",
                    "unit_of_measurement": "LOT",
                    "unit_price": str(line_total),
                    "currency": "USD",
                    "line_total": str(line_total),
                    "remarks": combined_description,
                }

            else:
                if not name:
                    continue

                if quantity is None or unit_price is None:
                    continue

                if line_total is None:
                    line_total = quantity * unit_price

                clean_name = self._clean_product_description(name)
                clean_details = self._clean_product_description(details)

                # Required format: Name | Details
                product_name = clean_name

                if clean_details:
                    product_name = f"{clean_name} | {clean_details}"

                material_match = re.search(
                    r"\bMaterial\s*:\s*(.+?)(?="
                    r"\s+(?:Enclosure|Output|Connection|Pressure|"
                    r"Measure|Accuracy|Supply|Cable|Protection|$)"
                    r")",
                    details,
                    re.I,
                )

                material = (
                    material_match.group(1).strip()
                    if material_match
                    else None
                )

                remarks = (
                    f"Certificate: {certificate}"
                    if certificate
                    else None
                )

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_name,
                    "specification": details or None,
                    "material": material,
                    "quantity": str(quantity),
                    "unit_of_measurement": "PCS",
                    "unit_price": str(unit_price),
                    "currency": "USD",
                    "line_total": str(line_total),
                    "remarks": remarks,
                }

            rows.append([
                values.get(header)
                for header in VIRTUAL_HEADERS
            ])

            row_meta.append({
                field: {
                    "confidence": 96,
                    "source": {
                        "type": "hisea_goods_table",
                        **source,
                        "row": row_number,
                    },
                }
                for field, value in values.items()
                if value not in (None, "")
            })

        return (
            rows,
            row_meta,
        ) if len(rows) > 1 else None
    def parse(self, file_obj, filename):
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ImportServiceError("Unsupported file type.", {"file": ["Excel, CSV, PDF, image and email files are supported."]})
        if suffix == ".xlsx":
            return self._with_meta(self._parse_excel(file_obj), filename, "excel", suffix)
        if suffix == ".xls":
            return self._with_meta(self._parse_xls(file_obj, filename), filename, "excel", suffix)
        if suffix == ".csv":
            return self._with_meta(self._parse_csv(file_obj), filename, "csv", suffix)
        if suffix == ".pdf":
            return self._with_meta(self._parse_pdf(file_obj), filename, "pdf", suffix)
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
            return self._with_meta(self._parse_image(file_obj), filename, "image", suffix)
        if suffix in {".eml", ".msg", ".txt"}:
            source = "email" if suffix in {".eml", ".msg"} else "text"
            return self._with_meta(self._parse_email_or_text(file_obj, filename), filename, source, suffix)
        raise ImportServiceError("Unsupported file type.")

    def _with_meta(self, result, filename, source_type, suffix):
        text = result.get("extracted_text", "")
        result["source_type"] = source_type
        result["document_type"] = _document_type(filename, suffix, text)
        if re.search(r"\b(?:pricing|price\s*list|rrp|distributor\s+discount)\b", str(text or ""), re.I):
            result["document_type"] = "QUOTATION"
        result.setdefault("document_fields", {})
        filename_supplier = self._supplier_from_quote_filename(filename)
        existing_supplier = (
            result["document_fields"].get("supplier")
            or result["document_fields"].get("supplier_name")
        )
        if existing_supplier:
            beneficiary_supplier = re.sub(
                r"^\s*BENEFICIARY[’']?S\s+NAME\s*[:：]\s*",
                "",
                str(existing_supplier),
                flags=re.I,
            ).strip()
            if beneficiary_supplier and beneficiary_supplier != existing_supplier:
                result["document_fields"]["supplier"] = beneficiary_supplier
                result["document_fields"]["supplier_name"] = beneficiary_supplier
                result.setdefault("document_field_meta", {})
                result["document_field_meta"]["supplier"] = {
                    "confidence": 90,
                    "source": {"type": "beneficiary_supplier_cleanup"},
                }
                result["document_field_meta"]["supplier_name"] = result["document_field_meta"]["supplier"]
                existing_supplier = beneficiary_supplier
        bad_supplier_terms = {
            "mfr",
            "brand",
            "assembled",
            "manufacturer",
            "proforma invoice",
            "invoice",
            "quotation",
            "quote",
            "commercial offer",
            "purchase order",
            "sales quotation",
            "tax invoice",
            "customer",
            "buyer",
            "bill to",
            "ship to",
            "aries trading",
            "aries marine",
            "aries group",
            "arteq",
        }

        existing_supplier_is_bad = (
            not existing_supplier
            or any(term in _norm(existing_supplier) for term in bad_supplier_terms)
            or self._is_buyer_or_delivery_line(existing_supplier)
        )

        if filename_supplier and existing_supplier_is_bad:
            result["document_fields"]["supplier"] = filename_supplier
            result["document_fields"]["supplier_name"] = filename_supplier
            result.setdefault("document_field_meta", {})
            result["document_field_meta"]["supplier"] = {
                "confidence": 82,
                "source": {"type": "filename_supplier"},
            }
            result["document_field_meta"]["supplier_name"] = result["document_field_meta"]["supplier"]
        result["document_fields"]["category"] = result["document_type"]
        result.setdefault("document_field_meta", {})
        result.setdefault("extraction_summary", {})
        structured = self._build_structured_document(result)
        result["extraction_summary"].update({
            "source_type": source_type,
            "document_type": result["document_type"],
            "document_field_count": len(result["document_fields"]),
            "sheet_count": len(result.get("sheets", [])),
            "structured_document": structured,
        })
        return result

    def _supplier_from_quote_filename(self, filename):
        stem = Path(str(filename or "")).stem
        match = re.search(
            r"\s+-\s+L\d+\s+-\s+(?P<supplier>.+?)(?:_\d{8}(?:_\d{6})?)?$",
            stem,
            re.I,
        )
        if not match:
            match = re.search(
                r"^L\d+\s*[-_]\s*(?P<supplier>[A-Z][A-Z0-9 &.,()]+?)(?:_\d{8}(?:_\d{6})?)?$",
                stem,
                re.I,
            )
        if not match:
            return None

        supplier = re.sub(r"[_-]+", " ", match.group("supplier"))
        supplier = re.sub(r"\s+", " ", supplier).strip(" -_")

        if not supplier or any(term in _norm(supplier) for term in {"quote", "rfq", "enquiry"}):
            return None

        return supplier.upper()
    def _build_structured_document(self, result):
        fields = result.get("document_fields") or {}
        meta = result.get("document_field_meta") or {}
        header = {
            "supplier_name": fields.get("supplier") or fields.get("supplier_name"),
            "supplier_address": fields.get("supplier_address"),
            "supplier_email": fields.get("supplier_email"),
            "supplier_phone": fields.get("supplier_phone"),
            "buyer_name": fields.get("buyer_name") or fields.get("customer_to"),
            "buyer_company": fields.get("buyer_company"),
            "buyer_address": fields.get("buyer_address"),
            "quotation_number": fields.get("quotation_number"),
            "purchase_order_number": fields.get("purchase_order_number"),
            "invoice_number": fields.get("invoice_number"),
            "lpo_number": fields.get("lpo_number"),
            "grn_number": fields.get("grn_number"),
            "reference_number": fields.get("reference_number") or fields.get("document_number"),
            "document_date": fields.get("document_date"),
            "delivery_date": fields.get("delivery_date"),
            "quotation_validity": fields.get("quotation_validity"),
            "currency": fields.get("currency"),
            "subtotal": self._number_or_none(fields.get("subtotal")),
            "vat": self._number_or_none(fields.get("vat") or fields.get("tax_amount")),
            "discount": self._number_or_none(fields.get("discount")),
            "freight": self._number_or_none(fields.get("freight")),
            "other_charges": self._number_or_none(fields.get("other_charges")),
            "grand_total": self._number_or_none(fields.get("grand_total")),
            "payment_terms": fields.get("payment_terms"),
            "country": fields.get("country"),
        }
        confidence = {
            "supplier_name": self._confidence(meta, "supplier", "supplier_name"),
            "document_date": self._confidence(meta, "document_date"),
            "currency": self._confidence(meta, "currency"),
            "document_number": self._confidence(meta, "document_number", "quotation_number", "purchase_order_number", "invoice_number"),
            "grand_total": self._confidence(meta, "grand_total"),
        }
        items = []
        warnings = []
        for sheet in result.get("sheets", []):
            rows = sheet.get("rows") or []
            row_meta = sheet.get("row_meta") or []
            if not rows:
                continue
            headers = rows[0]
            for index, row in enumerate(rows[1:], start=1):
                data = dict(zip(headers, row))
                item = self._structured_item(data, index, row_meta[index - 1] if index - 1 < len(row_meta) else {})
                if item:
                    items.append(item)
        if not items and result.get("source_type") not in {"excel", "csv"}:
            warnings.append("No reliable product table was detected.")
        return {
            "document_type": result.get("document_type") or "OTHER",
            "confidence": confidence,
            "header": header,
            "items": items,
            "warnings": warnings,
        }

    def _structured_item(self, data, line_no, meta):
        description = data.get("product_name")
        if not description:
            return None
        quantity = self._number_or_none(data.get("quantity"))
        unit_price = self._number_or_none(data.get("unit_price"))
        line_total = self._number_or_none(data.get("line_total"))
        row_valid = False
        if quantity is not None and unit_price is not None and line_total is not None:
            expected = Decimal(str(quantity)) * Decimal(str(unit_price))
            actual = Decimal(str(line_total))
            row_valid = abs(expected - actual) <= max(Decimal("0.05"), abs(actual) * Decimal("0.02"))
        confidence_values = [details.get("confidence", 0) for details in meta.values() if isinstance(details, dict)]
        row_confidence = round(sum(confidence_values) / len(confidence_values)) if confidence_values else 75
        return {
            "line_no": line_no,
            "item_code": data.get("item_code"),
            "part_number": data.get("part_number"),
            "manufacturer": data.get("manufacturer"),
            "brand": data.get("brand"),
            "product_name": description,
            "description": description,
            "quantity": quantity,
            "unit": self._display_uom(data.get("unit_of_measurement")),
            "unit_of_measurement": self._display_uom(data.get("unit_of_measurement")),
            "unit_price": unit_price,
            "currency": data.get("currency"),
            "line_total": line_total,
            "row_valid": row_valid,
            "confidence": row_confidence,
        }

    def _confidence(self, meta, *keys):
        for key in keys:
            if key in meta:
                return round(meta[key].get("confidence", 0))
        return 0

    def _display_uom(self, value):
        if value in (None, ""):
            return None
        text = str(value).strip().replace(".", "")
        if text.lower() in {"no", "nos"}:
            return "Nos"
        return text.upper()

    def _number_or_none(self, value):
        number = _decimal(value)
        return float(number) if number is not None else None

    def _parse_excel(self, file_obj):
        file_obj.seek(0)
        workbook = openpyxl.load_workbook(file_obj, data_only=True, read_only=False)
        sheets = []
        for index, worksheet in enumerate(workbook.worksheets):
            rows = [[_clean(cell) for cell in row] for row in worksheet.iter_rows(values_only=True)]
            sheets.append({"sheet_name": worksheet.title, "sheet_index": index, "rows": rows, "is_virtual": False, "row_meta": []})
        return {"sheets": sheets, "extraction_summary": {"method": "spreadsheet"}}
    
    def _spreadsheet_quote_to_line_items(self, rows, source, original_file_name=None):
        if not rows:
            return None

        fields = {}
        text_lines = []
        for row in rows:
            line_cells = []
            for cell in row:
                value = str(cell or "").strip()
                if not value:
                    continue
                if any(_norm(value) == _norm(existing) for existing in line_cells):
                    continue
                line_cells.append(value)
            text_lines.append(" ".join(line_cells).strip())

        supplier_stop_terms = {
            "to",
            "date",
            "ref no",
            "currency",
            "vessel",
            "no item",
            "item description",
            "description",
            "qty",
            "unit",
            "price",
            "amount",
        }

        for line in text_lines[:12]:
            normalized_line = _norm(line)
            if not normalized_line:
                continue

            cleaned_supplier = self._clean_supplier_name(line)
            if re.match(r"^\s*(company|customer|buyer|client)\s*[:：]", line, re.I):
                continue
            if (
                cleaned_supplier
                and self._is_valid_document_supplier(cleaned_supplier)
                and any(term in normalized_line.split() for term in {"llc", "ltd", "limited", "trading", "factory", "company", "co"})
            ):
                fields["supplier"] = cleaned_supplier
                fields["supplier_name"] = cleaned_supplier
                break

            if any(word in normalized_line for word in supplier_stop_terms):
                continue

            if any(word in normalized_line for word in {"address", "adress", "contact", "email", "tel", "fax", "phone", "website"}):
                continue

            if cleaned_supplier and self._is_valid_document_supplier(cleaned_supplier):
                fields["supplier"] = cleaned_supplier
                fields["supplier_name"] = cleaned_supplier
                break

        for row in rows[:15]:
            for cell in row:
                text = str(cell or "").strip()
                normalized = _norm(text)

                match = re.search(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", text)
                if match and ("document_date" not in fields or "date" in normalized or "报价" in text):
                    fields["document_date"] = self._normalize_document_value("document_date", match.group(1))

                currency = self._currency_from_text(text)
                if currency:
                    fields["currency"] = currency

        fields.setdefault("currency", None)
        fields.setdefault("category", "QUOTATION")

        header_index = None
        header_map = {}

        for index, row in enumerate(rows[:30]):
            normalized_cells = [_norm(cell) for cell in row]
            if not any(cell in {"no", "sl no", "sr no"} for cell in normalized_cells):
                continue
            if not any("description" in cell or "item" in cell or "name" in cell for cell in normalized_cells):
                continue
            if not any("qty" in cell or "quantity" in cell or "q t t" in cell or "qtt" in cell for cell in normalized_cells):
                continue
            if not any("prc" in cell or "price" in cell or "rate" in cell for cell in normalized_cells):
                continue

            for col_index, header in enumerate(normalized_cells):
                if header in {"no", "sl no", "sr no"}:
                    header_map[col_index] = "serial"
                elif "description" in header or header == "item" or "name" in header:
                    header_map[col_index] = "product_name"
                elif "qty" in header or "quantity" in header or "q t t" in header or "qtt" in header:
                    header_map[col_index] = "quantity"
                elif "packing" in header and "surcharge" in header:
                    header_map[col_index] = "packing_surcharge"
                elif "prc" in header or "price" in header or "rate" in header:
                    header_map[col_index] = "unit_price"
                elif header in {"unit", "uom"}:
                    header_map[col_index] = "unit_of_measurement"
                elif header in {"amt", "amount", "total"} or "amount" in header or "total" in header:
                    header_map[col_index] = "line_total"
                elif "remark" in header or "note" in header or "comment" in header:
                    header_map[col_index] = "remarks"
                elif header in {"type", "model"}:
                    header_map[col_index] = "type"
                elif header == "size":
                    header_map[col_index] = "size"
                elif "voltage" in header:
                    header_map[col_index] = "voltage"
            header_index = index
            break

        if header_index is None:
            return None
        target_refs = {
            match.group(0).upper()
            for match in re.finditer(
                r"\b[A-Z]{2,}\d{3,}\b",
                str(original_file_name or ""),
                flags=re.I,
            )
        }

        active_target_ref = None
        if target_refs:
            for row in rows:
                row_text = " ".join(str(cell or "") for cell in row).upper()
                for ref in target_refs:
                    if ref in row_text:
                        active_target_ref = ref
                        break
                if active_target_ref:
                    break
        output_rows = [VIRTUAL_HEADERS]
        row_meta = []

        in_target_section = active_target_ref is None

        for row_number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
            row_text = " ".join(str(cell or "") for cell in row).upper()
            first_cell = str(row[0] if row else "").strip()

            if row_text.startswith("TOTAL"):
                break

            is_project_header = bool(
                re.search(r"\bPROJECT\b", row_text, flags=re.I)
            )

            project_ref_match = re.search(
                r"\b([A-Z]{2,}\d{3,})\b",
                row_text,
                flags=re.I,
            )

            if is_project_header:
                section_ref = (
                    project_ref_match.group(1).upper()
                    if project_ref_match
                    else None
                )

                if active_target_ref:
                    if section_ref == active_target_ref:
                        in_target_section = True
                        continue

                    if in_target_section:
                        break

                    in_target_section = False
                    continue

                continue

            if not in_target_section:
                continue

            mapped = {}
            for col_index, value in enumerate(row):
                field = header_map.get(col_index)
                if field:
                    mapped[field] = str(value).strip() if value not in (None, "") else None

            description = mapped.get("product_name")
            if not description:
                continue

            if _norm(description).startswith(("ttl", "total", "subtotal", "notes")):
                continue

            unit_price = _decimal(mapped.get("unit_price"))
            quantity = _decimal(mapped.get("quantity"))
            line_total = _decimal(mapped.get("line_total"))

            if unit_price is None and line_total is not None:
                unit_price = line_total

            if unit_price is None:
                continue

            if quantity is None:
                quantity = Decimal("1")

            if line_total is None:
                line_total = quantity * unit_price

            remark_text = str(mapped.get("remarks") or "").strip()
            product_name = self._clean_product_description(description)


            row_type = self._row_type(product_name)

            if any(term in _norm(product_name) for term in {"certificate", "certification", "freight", "charge", "fee"}):
                row_type = "CHARGE"

            values = {
                "row_type": row_type,
                "product_name": product_name,
                "remarks": remark_text,
                "specification": " | ".join(
                    str(value).strip()
                    for value in (
                        mapped.get("type"),
                        mapped.get("size"),
                        mapped.get("voltage"),
                    )
                    if str(value or "").strip()
                ) or None,
                "quantity": str(quantity),
                "unit_of_measurement": mapped.get("unit_of_measurement"),
                "unit_price": str(unit_price),
                "currency": fields.get("currency") or "",
                "line_total": str(line_total),
            }

            output_rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {"confidence": 90, "source": {"type": "xls_quote_table", **source, "row": row_number}}
                for field, value in values.items()
                if value not in (None, "")
            })
            packing_surcharge = _decimal(
                mapped.get("packing_surcharge")
            )

            if packing_surcharge is not None and packing_surcharge > 0:
                packing_name_parts = [
                    "Packing Surcharge",
                    product_name,
                    mapped.get("type"),
                    mapped.get("size"),
                ]

                packing_name = " - ".join(
                    str(value).strip()
                    for value in packing_name_parts
                    if str(value or "").strip()
                )

                packing_values = {
                    "row_type": "CHARGE",
                    "product_name": packing_name,
                    "remarks": "Packing surcharge for item row",
                    "quantity": "1",
                    "unit_of_measurement": None,
                    "unit_price": str(packing_surcharge),
                    "currency": fields.get("currency") or "",
                    "line_total": str(packing_surcharge),
                }

                output_rows.append([
                    packing_values.get(header)
                    for header in VIRTUAL_HEADERS
                ])
                row_meta.append({
                    field: {"confidence": 85, "source": {"type": "xls_packing_surcharge", **source, "row": row_number}}
                    for field, value in packing_values.items()
                    if value not in (None, "")
                })

        return (output_rows, row_meta, fields) if len(output_rows) > 1 else None
    def _parse_xls(self, file_obj, original_file_name=None):
        try:
            import xlrd
        except ImportError as exc:
            raise ImportServiceError(
                "Old Excel .xls support is not configured.",
                {"file": ["Install xlrd to upload old .xls files: pip install xlrd"]},
            ) from exc

        file_obj.seek(0)
        data = file_obj.read()
        workbook = xlrd.open_workbook(file_contents=data, formatting_info=True)

        sheets = []
        for index in range(workbook.nsheets):
            worksheet = workbook.sheet_by_index(index)
            rows = []
            merged_values = {}
            for row_start, row_end, col_start, col_end in getattr(worksheet, "merged_cells", []):
                value = worksheet.cell_value(row_start, col_start)
                for merged_row in range(row_start, row_end):
                    for merged_col in range(col_start, col_end):
                        merged_values[(merged_row, merged_col)] = value
            for row_index in range(worksheet.nrows):
                row = []
                for col_index in range(worksheet.ncols):
                    cell = worksheet.cell(row_index, col_index)
                    value = merged_values.get((row_index, col_index), cell.value)

                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate_as_datetime(value, workbook.datemode).date().isoformat()
                        except Exception:
                            value = cell.value

                    if isinstance(value, float) and value.is_integer():
                        value = int(value)

                    row.append(_clean(value))

                rows.append(row)

            converted = self._spreadsheet_quote_to_line_items(
                rows,
                {"sheet": worksheet.name},
                original_file_name=original_file_name,
            )
            if converted:
                quote_rows, row_meta, fields = converted
                sheets.append({
                    "sheet_name": f"{worksheet.name} Quote Items",
                    "sheet_index": index,
                    "rows": quote_rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                })
                return {
                    "sheets": sheets,
                    "document_fields": fields,
                    "document_field_meta": {
                        "supplier": {"confidence": 90, "source": {"type": "spreadsheet_header"}},
                        "supplier_name": {"confidence": 90, "source": {"type": "spreadsheet_header"}},
                        "document_date": {"confidence": 90, "source": {"type": "spreadsheet_header"}},
                        "currency": {"confidence": 90, "source": {"type": "spreadsheet_header"}},
                    },
                    "extracted_text": "\n".join(" ".join(str(cell or "") for cell in row) for row in rows),
                    "extraction_summary": {"method": "xls_quote_table"},
                }

            sheets.append({
                "sheet_name": worksheet.name,
                "sheet_index": index,
                "rows": rows,
                "is_virtual": False,
                "row_meta": [],
            })

        return {
            "sheets": sheets,
            "extraction_summary": {"method": "xls_spreadsheet"},
        }
    def _parse_csv(self, file_obj):
        file_obj.seek(0)
        raw = file_obj.read()
        text = raw if isinstance(raw, str) else raw.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [[_clean(cell) for cell in row] for row in csv.reader(io.StringIO(text), dialect)]
        return {"sheets": [{"sheet_name": "CSV", "sheet_index": 0, "rows": rows, "is_virtual": False, "row_meta": []}], "extraction_summary": {"method": "csv"}}

    def _parse_pdf(self, file_obj):
        file_obj.seek(0)
        sheets, text_chunks, table_sources, pages_words = [], [], [], []
        hisea_document_detected = False
        table_description_map = {}
        table_material_description_map = {}
        with pdfplumber.open(file_obj) as pdf:
            for page_index, page in enumerate(pdf.pages):
                page_text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                text_chunks.append(page_text)
                words = page.extract_words(x_tolerance=2, y_tolerance=3, use_text_flow=False) or []
                pages_words.append(words)
                tables = page.extract_tables() or []
                table_found = False

                for table_index, table in enumerate(tables):
                    # Capture full part-number/description pairs from the
                    # original table before any generic row conversion.
                    for part_number, description in (
                        self._description_map_from_pdf_table(table).items()
                    ):
                        table_description_map.setdefault(
                            part_number,
                            description,
                        )
                    for product_name, material_description in (
                        self._material_description_map_from_pdf_table(table).items()
                    ):
                        table_material_description_map.setdefault(
                            product_name,
                            material_description,
                        )

                    source = {
                        "page": page_index + 1,
                        "table": table_index + 1,
                    }

                    table_text = _norm(
                        " ".join(
                            " ".join(str(cell or "") for cell in row)
                            for row in table
                            if row
                        )
                    )

                    is_hisea_header = all(
                        signature in table_text
                        for signature in {
                            "description of goods",
                            "certificate",
                            "qty pcs",
                            "unit price",
                            "sum usd",
                        }
                    )

                    if is_hisea_header:
                        hisea_document_detected = True

                    converted = None

                    if hisea_document_detected:
                        converted = self._hisea_goods_table_to_line_items(
                            table,
                            source,
                            allow_continuation=not is_hisea_header,
                        )

                    if not converted:
                        converted = (
                            self._item_description_quote_table_to_line_items(
                                table,
                                source,
                            )
                            or self._mooring_description_model_table_to_line_items(
                                table,
                                source,
                            )
                            or self._structured_pdf_table_to_line_items(
                                table,
                                source,
                            )
                            or self._table_to_line_items(
                                table,
                                source,
                            )
                        )

                    if converted:
                        rows, row_meta = converted

                        sheets.append({
                            "sheet_name": (
                                f"Page {page_index + 1} "
                                f"Table {table_index + 1}"
                            ),
                            "sheet_index": len(sheets),
                            "rows": rows,
                            "is_virtual": True,
                            "row_meta": row_meta,
                        })

                        table_sources.append({
                            "page": page_index + 1,
                            "table": table_index + 1,
                            "rows": len(rows) - 1,
                        })

                        table_found = True

                if table_found:
                    continue

                source = {
                    "page": page_index + 1,
                    "table": 1,
                }

                converted = (
                    self._visual_offer_grid_to_line_items(
                        words,
                        source,
                    )
                    or self._words_to_line_items(
                        words,
                        source,
                    )
                )

                if converted:
                    rows, row_meta = converted
                    sheets.append({
                        "sheet_name": f"Page {page_index + 1} Visual Table",
                        "sheet_index": len(sheets),
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    })
                    table_sources.append({"page": page_index + 1, "table": "visual", "rows": len(rows) - 1})
        text = "\n".join(text_chunks).strip()
        fields, field_meta = self._document_fields_from_text(text)
        signature_supplier = re.search(
            r"\bFor\s+(?P<supplier>[A-Z0-9][A-Z0-9&.,'() /-]{2,}?\s+(?:L\.?L\.?C|LLC|LTD|LIMITED|FZE|FZCO|CO\.?|COMPANY))\b",
            text,
            re.I,
        )
        if signature_supplier:
            supplier_value = re.sub(r"\s+", " ", signature_supplier.group("supplier")).strip()
            if self._is_valid_document_supplier(supplier_value):
                fields["supplier"] = supplier_value
                fields["supplier_name"] = supplier_value
                field_meta["supplier"] = {
                    "confidence": 94,
                    "source": {"type": "signature_for_supplier"},
                }
                field_meta["supplier_name"] = field_meta["supplier"]
        header_supplier = self._header_supplier_before_title(text)
        current_supplier_norm = _norm(fields.get("supplier") or fields.get("supplier_name"))
        if header_supplier and (
            not current_supplier_norm
            or "aries" in current_supplier_norm
            or "eng services" in current_supplier_norm
            or self._is_buyer_or_delivery_line(fields.get("supplier") or fields.get("supplier_name"))
        ):
            fields["supplier"] = header_supplier
            fields["supplier_name"] = header_supplier
            field_meta["supplier"] = {
                "confidence": 94,
                "source": {"type": "header_company_before_document_title"},
            }
            field_meta["supplier_name"] = field_meta["supplier"]
        # Enrich part-number-based rows from native text.
        # Existing complete product names are left unchanged.
        self._enrich_products_from_native_text(
            sheets,
            text,
            table_description_map,
        )
        self._enrich_products_with_table_material(
            sheets,
            table_material_description_map,
        )
        self._cleanup_extracted_line_item_rows(sheets)
        # Remove generic document labels incorrectly captured as supplier names.
        current_detected_supplier = (
            fields.get("supplier")
            or fields.get("supplier_name")
        )

        invalid_supplier_labels = {
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
        }

        normalized_detected_supplier = _norm(
            current_detected_supplier
        )

        supplier_header_tokens = {
            "unit", "price", "total", "material", "category",
            "quantity", "qty", "description", "item", "product",
            "amount", "rate", "currency", "meter", "pcs",
            "prepared", "proposal", "project", "teamwork",
        }

        detected_tokens = normalized_detected_supplier.split()

        is_table_header_supplier = bool(
            detected_tokens
            and all(
                token in supplier_header_tokens
                for token in detected_tokens
            )
        )

        sentence_words = {
            "that", "which", "with", "through", "provide", "provides",
            "providing", "deliver", "delivers", "support", "supports",
            "ensure", "ensures", "create", "creates", "aim", "aims",
            "our", "your", "their", "this", "these", "those", "and",
            "for", "from", "into", "across", "within", "without",
        }

        is_sentence_supplier = bool(
            len(detected_tokens) >= 4
            and any(
                token in sentence_words
                for token in detected_tokens
            )
        )

        if (
            normalized_detected_supplier in invalid_supplier_labels
            or is_table_header_supplier
            or is_sentence_supplier
        ):
            fields.pop("supplier", None)
            fields.pop("supplier_name", None)
            field_meta.pop("supplier", None)
            field_meta.pop("supplier_name", None)
        # Prefer native PDF text only when it is not clearly buyer/customer text.
        # If native text finds the buyer, keep room for OCR/logo supplier.
        header_email_company_supplier = None
        for index, line in enumerate(str(text or "").splitlines()):
            if not re.search(r"sales@aqiqalahmar\.com", line, re.I):
                continue

            for previous_line in reversed(str(text or "").splitlines()[max(0, index - 8):index]):
                candidate = self._clean_supplier_name(previous_line)
                if not candidate:
                    continue
                candidate = re.sub(
                    r"\b(?:Voucher\s+No\.?|Dated|Mode/Terms\s+of\s+Payment|Buyer'?s\s+Ref\.?/Order\s+No\.?|Other\s+Reference\(s\)).*$",
                    "",
                    candidate,
                    flags=re.I,
                ).strip(" :-|")

                if (
                    candidate
                    and "aries" not in _norm(candidate)
                    and not self._is_buyer_or_delivery_line(candidate)
                    and self._is_valid_document_supplier(candidate)
                ):
                    header_email_company_supplier = candidate
                    break

            if header_email_company_supplier:
                break

        if header_email_company_supplier:
            fields["supplier"] = header_email_company_supplier
            fields["supplier_name"] = header_email_company_supplier
            field_meta["supplier"] = {"confidence": 98, "source": {"type": "header_company_near_supplier_email"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        header_email_domain_supplier = None
        header_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()[:35]
            if str(line or "").strip()
        ]

        header_domains = []
        for line in header_lines:
            for domain in re.findall(r"@([A-Z0-9.-]+\.[A-Z]{2,})", line, re.I):
                domain = domain.lower()
                if domain not in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "arteqworld.com", "ariesmar.com"}:
                    header_domains.append(domain)

        for domain in header_domains:
            domain_key = domain.split(".")[0]
            if len(domain_key) < 4:
                continue

            for candidate_line in header_lines:
                candidate = self._clean_supplier_name(candidate_line)
                if not candidate or self._is_buyer_or_delivery_line(candidate):
                    continue

                import unicodedata
                candidate_key = unicodedata.normalize(
                    "NFKD",
                    candidate,
                ).encode("ascii", "ignore").decode("ascii").lower()
                candidate_key = re.sub(r"[^a-z0-9]+", "", candidate_key)

                candidate_norm = _norm(candidate)

                if (
                    domain_key in candidate_key
                    and not any(term in candidate_norm for term in {
                        "quotation",
                        "customer",
                        "date",
                        "payment",
                        "vessel",
                        "readiness",
                        "terms",
                        "conditions",
                    })
                ):
                    header_email_domain_supplier = candidate
                    break

            if header_email_domain_supplier:
                break

        if header_email_domain_supplier:
            fields["supplier"] = header_email_domain_supplier
            fields["supplier_name"] = header_email_domain_supplier
            field_meta["supplier"] = {"confidence": 98, "source": {"type": "header_company_matching_email_domain"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        issued_by_supplier = None
        for match in re.finditer(
            r"Issued\s+By\s*:\s*(?:Bill\s+To\s*:)?\s*(?P<body>.+?)(?=\bTRN\s*:|\bAddress\s*:|\bTel\.?\s*:|\bE-?mail\s*:|$)",
            str(text or ""),
            re.I | re.S,
        ):
            body = re.sub(r"\s+", " ", match.group("body")).strip(" :-|")
            body = re.split(r"\bAries\s+Marine\b", body, maxsplit=1, flags=re.I)[0].strip(" :-|")
            candidate = self._clean_supplier_name(body)

            if (
                candidate
                and not self._is_buyer_or_delivery_line(candidate)
                and self._is_valid_document_supplier(candidate)
            ):
                issued_by_supplier = candidate
                break

        if issued_by_supplier:
            fields["supplier"] = issued_by_supplier
            fields["supplier_name"] = issued_by_supplier
            field_meta["supplier"] = {"confidence": 99, "source": {"type": "issued_by_block"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        supplier_from_text = self._supplier_from_pdf_text(text)

        if (
            supplier_from_text
            and self._is_valid_document_supplier(supplier_from_text)
            and not self._is_buyer_or_delivery_line(supplier_from_text)
            and "aries marine" not in _norm(supplier_from_text)  and "aries" not in _norm(supplier_from_text)
            and not issued_by_supplier
        ):
            supplier_from_text = re.sub(
                r"\b(?:Voucher\s+No\.?|Dated|Mode/Terms\s+of\s+Payment|Buyer'?s\s+Ref\.?/Order\s+No\.?|Other\s+Reference\(s\)).*$",
                "",
                self._clean_supplier_name(supplier_from_text),
                flags=re.I,
            ).strip(" :-|")
            fields["supplier"] = supplier_from_text
            fields["supplier_name"] = supplier_from_text

            field_meta["supplier"] = {
                "confidence": 96,
                "source": {
                    "type": "pdf_text_company",
                },
            }
            field_meta["supplier_name"] = field_meta["supplier"]

        # Generic quote header supplier fallback:
        # Example: "Taizhou Hongrun Marine Accessories Factory QUOTE # HR131"
        if not self._is_valid_document_supplier(fields.get("supplier") or fields.get("supplier_name")):
            quote_header_supplier = None

            for supplier_line in str(text or "").splitlines()[:20]:
                supplier_line = re.sub(r"\s+", " ", supplier_line).strip()
                match = re.search(
                    r"(?P<supplier>[A-Z][A-Za-z0-9&.,'() /-]+?\b(?:Factory|LLC|L\.L\.C|Ltd\.?|Limited|Trading|Company|Co\.?|Corporation|Corp))\s+(?:QUOTE|QUOTATION|QTN)\b",
                    supplier_line,
                    re.I,
                )
                if match:
                    candidate = self._clean_supplier_name(match.group("supplier"))
                    candidate_norm = _norm(candidate)
                    if (
                        candidate
                        and len(candidate.split()) >= 2
                        and not self._is_buyer_or_delivery_line(candidate)
                        and not any(term in candidate_norm for term in {"address", "contact", "phone", "email", "date", "quote", "quotation"})
                    ):
                        quote_header_supplier = candidate
                        break

            if quote_header_supplier:
                fields["supplier"] = quote_header_supplier
                fields["supplier_name"] = quote_header_supplier
                field_meta["supplier"] = {
                    "confidence": 91,
                    "source": {"type": "quote_header_company"},
                }
                field_meta["supplier_name"] = field_meta["supplier"]
        current_supplier_before_ocr = fields.get("supplier") or fields.get("supplier_name")
        needs_supplier_ocr = (
            not current_supplier_before_ocr
            or not self._is_valid_document_supplier(current_supplier_before_ocr)
            or self._is_buyer_or_delivery_line(current_supplier_before_ocr)
            or re.search(
                r"\b(?:information|certificate|sales contract|quotation|invoice|customer|buyer)\b",
                str(current_supplier_before_ocr or ""),
                re.I,
            )
        )

        supplier_from_ocr = None
        if needs_supplier_ocr:
            supplier_from_ocr = self._supplier_from_pdf_header_ocr(file_obj)

        if supplier_from_ocr:
            current_supplier = (
                fields.get("supplier")
                or fields.get("supplier_name")
            )

            current_supplier_is_valid = self._is_valid_document_supplier(
                current_supplier
            )
            current_supplier_is_terms = bool(
                re.search(
                    r"\b(?:once\s+the\s+order\s+is\s+placed|cancellation\s+is\s+not\s+allowed|unless\s+agreed|terms\s+and\s+conditions)\b",
                    str(current_supplier or ""),
                    re.I,
                )
            )
            current_supplier_is_buyer_tail = bool(
                re.search(
                    r"\b(?:aries|eng\.?\s+services|engineering\s+services|marine\s*&\s*eng)\b",
                    str(current_supplier or ""),
                    re.I,
                )
            )

            if not current_supplier_is_valid or current_supplier_is_terms or current_supplier_is_buyer_tail:
                cleaned_supplier = self._clean_supplier_name(
                    supplier_from_ocr
                )

                if self._is_valid_document_supplier(cleaned_supplier):
                    fields["supplier"] = cleaned_supplier
                    fields["supplier_name"] = cleaned_supplier

                    field_meta["supplier"] = {
                        "confidence": 88,
                        "source": {
                            "type": "header_logo_ocr",
                        },
                    }
                    field_meta["supplier_name"] = field_meta["supplier"]
        supplier_from_layout = self._supplier_from_order_issued_words(pages_words)
        current_supplier = fields.get("supplier") or fields.get("supplier_name")
        if supplier_from_layout and not self._is_valid_document_supplier(current_supplier):
            fields["supplier"] = supplier_from_layout
            fields["supplier_name"] = supplier_from_layout
            field_meta["supplier"] = {"confidence": 88, "source": {"type": "order_issued_to_layout"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        raw_footer_supplier = None
        raw_lines = [
            re.sub(r"\s+", " ", raw_line).strip()
            for raw_line in str(text or "").splitlines()
            if str(raw_line or "").strip()
        ]
        ignored_supplier_domains = {"arteqworld.com", "ariesmar.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}

        for index, raw_line in enumerate(raw_lines):
            domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", raw_line, re.I)
            if not domain_match:
                continue

            domain = domain_match.group(1).lower()
            if domain in ignored_supplier_domains:
                continue

            domain_key = domain.split(".")[0]
            if len(domain_key) < 4:
                continue

            for previous_line in reversed(raw_lines[max(0, index - 10):index]):
                candidate = self._clean_supplier_name(previous_line)
                if not candidate or self._is_buyer_or_delivery_line(candidate):
                    continue

                import unicodedata
                candidate_key = unicodedata.normalize(
                    "NFKD",
                    candidate,
                ).encode("ascii", "ignore").decode("ascii").lower()
                candidate_key = re.sub(r"[^a-z0-9]+", "", candidate_key)

                if domain_key in candidate_key:
                    raw_footer_supplier = candidate
                    break

            if raw_footer_supplier:
                break

        current_supplier_norm = _norm(fields.get("supplier") or "")
        if raw_footer_supplier and (
            "supplier" not in fields
            or "aries" in current_supplier_norm
            or "trading services" in current_supplier_norm
            or self._is_buyer_or_delivery_line(fields.get("supplier"))
        ):
            fields["supplier"] = raw_footer_supplier
            fields["supplier_name"] = raw_footer_supplier
            field_meta["supplier"] = {"confidence": 97, "source": {"type": "raw_footer_company_near_non_buyer_email"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        for_footer_supplier = None
        for raw_line in raw_lines:
            match = re.search(
                r"^\s*FOR\s+(?P<supplier>.+?\b(?:L\.?\s*L\.?\s*C\.?|LLC|LTD|LIMITED|TRADING|COMPANY|CO\.?))\s*$",
                raw_line,
                re.I,
            )
            if not match:
                continue

            candidate = self._clean_supplier_name(match.group("supplier"))
            if (
                candidate
                and not self._is_buyer_or_delivery_line(candidate)
                and self._is_valid_document_supplier(candidate)
            ):
                for_footer_supplier = candidate
                break

        current_supplier_norm = _norm(fields.get("supplier") or "")
        if for_footer_supplier and (
            "supplier" not in fields
            or "aries" in current_supplier_norm
            or "eng services" in current_supplier_norm
            or (
                len(current_supplier_norm.split()) <= 2
                and len(_norm(for_footer_supplier).split()) > len(current_supplier_norm.split())
            )
            or self._is_buyer_or_delivery_line(fields.get("supplier"))
        ):
            fields["supplier"] = for_footer_supplier
            fields["supplier_name"] = for_footer_supplier
            field_meta["supplier"] = {"confidence": 98, "source": {"type": "for_supplier_footer"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        current_supplier = fields.get("supplier") or fields.get("supplier_name")
        if current_supplier:
            cleaned_supplier = re.sub(
                r"\b(?:Voucher\s+No\.?|Dated|Mode/Terms\s+of\s+Payment|Buyer'?s\s+Ref\.?/Order\s+No\.?|Other\s+Reference\(s\)).*$",
                "",
                self._clean_supplier_name(current_supplier),
                flags=re.I,
            ).strip(" :-|")
            if cleaned_supplier and cleaned_supplier != current_supplier:
                fields["supplier"] = cleaned_supplier
                fields["supplier_name"] = cleaned_supplier
                field_meta["supplier"] = {"confidence": 97, "source": {"type": "cleaned_supplier_header_suffix"}}
                field_meta["supplier_name"] = field_meta["supplier"]
        current_supplier = fields.get("supplier") or fields.get("supplier_name")
        if current_supplier and "@" in str(current_supplier):
            email_domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", str(current_supplier), re.I)
            supplier_from_domain = self._company_name_from_email_domain(email_domain_match.group(1)) if email_domain_match else None
            if supplier_from_domain:
                fields["supplier"] = supplier_from_domain
                fields["supplier_name"] = supplier_from_domain
                field_meta["supplier"] = {"confidence": 88, "source": {"type": "pdf_supplier_email_domain_cleanup"}}
                field_meta["supplier_name"] = field_meta["supplier"]
        siemens_quote_converted = self._siemens_quote_table_to_items(text, fields)
        if siemens_quote_converted:
            rows, row_meta = siemens_quote_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Siemens Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_siemens_quote_table"},
            }
        siemens_offer_converted = self._siemens_bom_offer_to_items(text, fields)
        if siemens_offer_converted:
            rows, row_meta = siemens_offer_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Siemens BOM Offer Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_siemens_bom_offer"},
            }

        gulf_ocean_converted = self._gulf_ocean_sales_quote_to_items(text, fields)
        if gulf_ocean_converted:
            rows, row_meta = gulf_ocean_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Sales Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_sales_quote_qty_rate_amount"},
            }
        email_price_table_converted = self._email_product_price_table_to_items(text, fields)
        if email_price_table_converted:
            rows, row_meta = email_price_table_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Email Price Table Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_email_product_price_table"},
            }
        pricing_list_converted = self._pricing_list_to_items(text, fields)
        if pricing_list_converted:
            rows, row_meta = pricing_list_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Pricing List Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_pricing_list"},
            }
        model_spec_converted = self._model_spec_quote_to_items(text, fields)
        if model_spec_converted:
            rows, row_meta = model_spec_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Model Specification Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_model_spec_quote_table"},
            }
        china_harbor_converted = self._china_harbor_quote_to_items(text, fields)
        if china_harbor_converted:
            rows, row_meta = china_harbor_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF China Harbor Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_china_harbor_quote"},
            }
        simple_goods_converted = self._simple_goods_quote_table_to_items(text, fields)
        if simple_goods_converted:
            rows, row_meta = simple_goods_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Goods Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_simple_goods_quote_table"},
            }
        mooring_email_converted = self._mooring_email_price_table_to_items(text, fields)
        if mooring_email_converted:
            rows, row_meta = mooring_email_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Mooring Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_mooring_email_price_table"},
            }

        if sheets:
            boq_converted = self._boq_text_to_items(text, fields)
            if boq_converted:
                rows, row_meta = boq_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF BOQ Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_boq_text_items"},
                }
            multiline_quote_converted = self._multiline_sales_quote_to_items(text, fields)
            if multiline_quote_converted:
                rows, row_meta = multiline_quote_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Multiline Quote Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_multiline_sales_quote"},
                }
            crowcon_converted = self._crowcon_commercial_invoice_to_items(text, fields)
            if crowcon_converted:
                rows, row_meta = crowcon_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Commercial Invoice Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_commercial_invoice_items"},
                }

            text_converted = self._quote_text_to_table_items(text, fields)
            if text_converted:
                text_rows, text_row_meta = text_converted
                temp_text_sheet = {
                    "sheet_name": "PDF Quote Items",
                    "sheet_index": 0,
                    "rows": text_rows,
                    "is_virtual": True,
                    "row_meta": text_row_meta,
                }

                self._enrich_products_from_native_text(
                    [temp_text_sheet],
                    text,
                    table_description_map,
                )
                self._cleanup_extracted_line_item_rows([temp_text_sheet])
                text_rows = temp_text_sheet["rows"]
                text_row_meta = temp_text_sheet.get("row_meta", text_row_meta)
                table_row_count = sum(max(len(sheet["rows"]) - 1, 0) for sheet in sheets)
                text_row_count = max(len(text_rows) - 1, 0)

                table_headers = sheets[0]["rows"][0] if sheets and sheets[0].get("rows") else []
                text_headers = text_rows[0] if text_rows else []

                table_line_total_index = table_headers.index("line_total") if "line_total" in table_headers else -1
                text_line_total_index = text_headers.index("line_total") if "line_total" in text_headers else -1

                table_total_count = sum(
                    1
                    for sheet in sheets
                    for row in sheet.get("rows", [])[1:]
                    if table_line_total_index >= 0
                    and table_line_total_index < len(row)
                    and row[table_line_total_index] not in (None, "")
                )

                text_total_count = sum(
                    1
                    for row in text_rows[1:]
                    if text_line_total_index >= 0
                    and text_line_total_index < len(row)
                    and row[text_line_total_index] not in (None, "")
                )

                table_has_merged_values = any(
                    any(
                        isinstance(cell, str)
                        and re.search(r"\b\d+\s+(?:Nos|PCS|PC|EA)\s+\d+", cell, re.I)
                        and re.search(r"\s+\d+\s+(?:Nos|PCS|PC|EA)\b", cell, re.I)
                        for cell in row
                    )
                    for sheet in sheets
                    for row in sheet.get("rows", [])[1:]
                )

                if table_has_merged_values or (text_row_count >= table_row_count and text_total_count >= table_total_count):
                    self._apply_document_currency(text_rows, text_row_meta, fields)
                    return {
                        "sheets": [{
                            "sheet_name": "PDF Quote Items",
                            "sheet_index": 0,
                            "rows": text_rows,
                            "is_virtual": True,
                            "row_meta": text_row_meta,
                        }],
                        "document_fields": fields,
                        "document_field_meta": field_meta,
                        "extracted_text": text,
                        "extraction_summary": {"method": "pdf_quote_text_fallback_preferred"},
                    }

            calgaz_converted = self._calgaz_stock_quote_to_items(text, fields)
            if calgaz_converted:
                rows, row_meta = calgaz_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Calgaz Quote Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_calgaz_stock_quote"},
                }

            crowcon_converted = self._crowcon_commercial_invoice_to_items(text, fields)
            if crowcon_converted:
                rows, row_meta = crowcon_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Commercial Invoice Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_commercial_invoice_items"},
                }

            certificate_match = re.search(
                r"\b(?P<name>BV\s*Certificate\s*Fee|ABS\s*Certificate\s*Fee|Certificate\s*Fee)\s*:?\s*\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)",
                text,
                re.I,
            )

            if certificate_match and sheets:
                first_sheet = sheets[0]
                rows = first_sheet["rows"]
                row_meta = first_sheet["row_meta"]

                headers = rows[0]
                product_index = headers.index("product_name")
                existing_names = {
                    _norm(row[product_index])
                    for row in rows[1:]
                    if product_index < len(row)
                }

                charge_name = re.sub(r"\s+", " ", certificate_match.group("name")).strip()
                amount = certificate_match.group("amount").replace(",", "")

                if _norm(charge_name) not in existing_names:
                    values = {
                        "row_type": "CHARGE",
                        "product_name": charge_name,
                        "quantity": "1",
                        "unit_price": amount,
                        "currency": fields.get("currency") or "USD",
                        "line_total": amount,
                    }
                    rows.append([values.get(header) for header in headers])
                    row_meta.append({
                        field: {
                            "confidence": 88,
                            "source": {"type": "text_certificate_fee"},
                        }
                        for field, value in values.items()
                        if value not in (None, "")
                    })

            for sheet in sheets:
                self._apply_document_currency(sheet["rows"], sheet["row_meta"], fields)
            return {"sheets": sheets, "document_fields": fields, "document_field_meta": field_meta, "extracted_text": text, "extraction_summary": {"method": "pdf_visual_tables", "tables": table_sources}}
        calgaz_converted = self._calgaz_stock_quote_to_items(text, fields)
        if calgaz_converted:
            rows, row_meta = calgaz_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Calgaz Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_calgaz_stock_quote"},
            }

        crowcon_converted = self._crowcon_commercial_invoice_to_items(text, fields)
        if crowcon_converted:
            rows, row_meta = crowcon_converted
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Commercial Invoice Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_commercial_invoice_items"},
            }

        text_converted = self._quote_text_to_table_items(text, fields)
        if text_converted:
            rows, row_meta = text_converted
            temp_text_sheet = {
                "sheet_name": "PDF Quote Items",
                "sheet_index": 0,
                "rows": rows,
                "is_virtual": True,
                "row_meta": row_meta,
            }

            self._enrich_products_from_native_text(
                [temp_text_sheet],
                text,
                table_description_map,
            )
            self._cleanup_extracted_line_item_rows([temp_text_sheet])
            rows = temp_text_sheet["rows"]
            row_meta = temp_text_sheet.get("row_meta", row_meta)
            self._apply_document_currency(rows, row_meta, fields)
            return {
                "sheets": [{
                    "sheet_name": "PDF Quote Items",
                    "sheet_index": 0,
                    "rows": rows,
                    "is_virtual": True,
                    "row_meta": row_meta,
                }],
                "document_fields": fields,
                "document_field_meta": field_meta,
                "extracted_text": text,
                "extraction_summary": {"method": "pdf_quote_text_fallback"},
            }
        if len(text) >= OCR_TEXT_MINIMUM:
            prose_offer_converted = self._prose_unit_price_offer_to_items(text, fields)
            if prose_offer_converted:
                rows, row_meta = prose_offer_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Prose Offer Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_prose_unit_price_offer"},
                }

            email_offer_converted = self._email_offer_text_to_items(text, fields)
            if email_offer_converted:
                rows, row_meta = email_offer_converted
                self._apply_document_currency(rows, row_meta, fields)
                return {
                    "sheets": [{
                        "sheet_name": "PDF Email Offer Items",
                        "sheet_index": 0,
                        "rows": rows,
                        "is_virtual": True,
                        "row_meta": row_meta,
                    }],
                    "document_fields": fields,
                    "document_field_meta": field_meta,
                    "extracted_text": text,
                    "extraction_summary": {"method": "pdf_email_offer_fallback"},
                }

            return self._metadata_only_document(text, "PDF Review", fields, field_meta, "needs_review_no_visual_table")
        return self._parse_scanned_pdf(file_obj)

    def _parse_scanned_pdf(self, file_obj):
        try:
            import pypdfium2
            import pytesseract
            from pytesseract import Output
            
        except ImportError as exc:
            raise ImportServiceError("OCR is not configured.", {"file": ["Scanned PDFs require pytesseract and pypdfium2 OCR support."]}) from exc
        file_obj.seek(0)
        pdf = pypdfium2.PdfDocument(file_obj)
        sheets, text_chunks, table_sources, pages_words = [], [], [], []
        for page_index, page in enumerate(pdf):
            bitmap = page.render(scale=2).to_pil()
            text_chunks.append(pytesseract.image_to_string(bitmap))
            words = self._ocr_data_to_words(pytesseract.image_to_data(bitmap, output_type=Output.DICT))
            pages_words.append(words)
            converted = self._words_to_line_items(words, {"page": page_index + 1, "table": 1, "ocr": True})
            if converted:
                rows, row_meta = converted
                sheets.append({"sheet_name": f"Page {page_index + 1} OCR Table", "sheet_index": len(sheets), "rows": rows, "is_virtual": True, "row_meta": row_meta})
                table_sources.append({"page": page_index + 1, "table": "ocr_visual", "rows": len(rows) - 1})
        text = "\n".join(text_chunks).strip()
        fields, field_meta = self._document_fields_from_text(text)
        supplier_from_layout = self._supplier_from_order_issued_words(pages_words)
        current_supplier = fields.get("supplier") or fields.get("supplier_name")
        if supplier_from_layout and not self._is_valid_document_supplier(current_supplier):
            fields["supplier"] = supplier_from_layout
            fields["supplier_name"] = supplier_from_layout
            field_meta["supplier"] = {"confidence": 88, "source": {"type": "order_issued_to_layout"}}
            field_meta["supplier_name"] = field_meta["supplier"]
        if sheets:
            for sheet in sheets:
                self._apply_document_currency(sheet["rows"], sheet["row_meta"], fields)
            return {"sheets": sheets, "document_fields": fields, "document_field_meta": field_meta, "extracted_text": text, "extraction_summary": {"method": "ocr_visual_tables", "tables": table_sources}}
        return self._metadata_only_document(text, "OCR Review", fields, field_meta, "needs_review_no_ocr_table")

    def _parse_image(self, file_obj):
        try:
            import pytesseract
            from pytesseract import Output
        except ImportError as exc:
            raise ImportServiceError("OCR is not configured.", {"file": ["Image files require pytesseract OCR support."]}) from exc
        file_obj.seek(0)
        image = Image.open(file_obj)
        try:
            text = pytesseract.image_to_string(image)
            ocr_words = self._ocr_data_to_words(pytesseract.image_to_data(image, output_type=Output.DICT))
        except Exception as exc:
            fields, field_meta = {}, {}
            result = self._metadata_only_document("", "Image Review", fields, field_meta, "needs_review_ocr_unavailable")
            result["extraction_summary"]["ocr_error"] = exc.__class__.__name__
            return result
        fields, field_meta = self._document_fields_from_text(text)
        converted = self._words_to_line_items(ocr_words, {"page": 1, "table": 1, "ocr": True})
        if converted:
            rows, row_meta = converted
            self._apply_document_currency(rows, row_meta, fields)
            return {"sheets": [{"sheet_name": "Image OCR Table", "sheet_index": 0, "rows": rows, "is_virtual": True, "row_meta": row_meta}], "document_fields": fields, "document_field_meta": field_meta, "extracted_text": text, "extraction_summary": {"method": "ocr_visual_tables", "tables": [{"page": 1, "table": "ocr_visual", "rows": len(rows) - 1}]}}
        return self._metadata_only_document(text, "Image Review", fields, field_meta, "needs_review_no_ocr_table")

    def _parse_email_or_text(self, file_obj, filename):
        file_obj.seek(0)
        raw = file_obj.read()
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        if Path(filename).suffix.lower() == ".eml":
            message = email.message_from_string(text)
            body_parts, attachment_sheets = [], []
            attachment_meta = {"attachments": []}
            for part in message.walk():
                disposition = part.get_content_disposition()
                if disposition == "attachment":
                    attachment_name = part.get_filename() or "attachment"
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            parsed = self.parse(io.BytesIO(payload), attachment_name)
                            attachment_sheets.extend(parsed.get("sheets", []))
                            attachment_meta["attachments"].append({"filename": attachment_name, "status": "extracted"})
                        except ImportServiceError as exc:
                            attachment_meta["attachments"].append({"filename": attachment_name, "status": "skipped", "reason": str(exc)})
                elif part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    body_parts.append((payload or b"").decode(part.get_content_charset() or "utf-8", errors="replace"))
            body_text = "\n".join(body_parts)
            fields, field_meta = self._document_fields_from_text(body_text)
            parsed_body = self._metadata_only_document(body_text, "Email Body", fields, field_meta, "email_body_metadata_only")
            parsed_body["sheets"].extend(attachment_sheets)
            parsed_body["extraction_summary"] = {"method": "email_body_attachments", **attachment_meta}
            return parsed_body
        return self._virtual_from_labeled_text(text, "Text Body")

    def _metadata_only_document(self, text, sheet_name, fields=None, field_meta=None, method="needs_review_no_table"):
        if fields is None or field_meta is None:
            fields, field_meta = self._document_fields_from_text(text)
        return {
            "sheets": [{"sheet_name": sheet_name, "sheet_index": 0, "rows": [VIRTUAL_HEADERS], "is_virtual": True, "row_meta": []}],
            "document_fields": fields,
            "document_field_meta": field_meta,
            "extracted_text": text,
            "extraction_summary": {"method": method, "review_required": True},
        }

    def _ocr_data_to_words(self, data):
        words = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text or "").strip()
            if not text:
                continue
            try:
                confidence = float(data.get("conf", [0])[index])
            except (TypeError, ValueError, IndexError):
                confidence = 0
            if confidence < 25:
                continue
            left = float(data["left"][index])
            top = float(data["top"][index])
            width = float(data["width"][index])
            height = float(data["height"][index])
            words.append({"text": text, "x0": left, "x1": left + width, "top": top, "bottom": top + height})
        return words


    def _visual_offer_grid_to_line_items(self, words, source):
        """
        Parse a generic visual quotation grid whose headers include:

            MATERIAL / DESCRIPTION
            CATEGORY
            UNIT or QUANTITY
            UNIT PRICE
            TOTAL PRICE

        The parser is header-gated and does not use supplier names,
        filenames, or fixed product groups. Group headings are detected
        dynamically and carried into the product display value.
        """

        visual_lines = self._cluster_words_by_line(words)

        if len(visual_lines) < 4:
            return None

        header_words = [
            word
            for line in visual_lines[:20]
            for word in line.get("words", [])
        ]

        def first_word(*labels, min_x=None):
            for word in header_words:
                normalized = _norm(word.get("text"))
                if normalized not in labels:
                    continue
                if min_x is not None and float(word.get("x0", 0)) < min_x:
                    continue
                return word
            return None

        material_word = first_word(
            "material",
            "description",
            "product",
            "item",
        )
        category_word = first_word("category")
        total_word = first_word("total")

        price_words = [
            word
            for word in header_words
            if _norm(word.get("text")) == "price"
        ]

        unit_price_word = (
            min(price_words, key=lambda word: float(word.get("x0", 0)))
            if price_words
            else None
        )

        quantity_word = first_word("quantity", "qty")

        if quantity_word is None:
            unit_candidates = [
                word
                for word in header_words
                if _norm(word.get("text")) == "unit"
            ]

            if unit_candidates:
                quantity_word = min(
                    unit_candidates,
                    key=lambda word: float(word.get("x0", 0)),
                )

        if not all(
            (
                material_word,
                category_word,
                quantity_word,
                unit_price_word,
                total_word,
            )
        ):
            return None

        material_x = float(material_word["x0"])
        category_x = float(category_word["x0"])
        quantity_x = float(quantity_word["x0"])
        unit_price_x = float(unit_price_word["x0"])
        total_x = float(total_word["x0"])

        if not (
            material_x
            < category_x
            < quantity_x
            < unit_price_x
            < total_x
        ):
            return None

        material_category_boundary = (material_x + category_x) / 2
        category_quantity_boundary = (category_x + quantity_x) / 2
        quantity_price_boundary = (quantity_x + unit_price_x) / 2
        price_total_boundary = (unit_price_x + total_x) / 2

        header_bottom = max(
            float(material_word["bottom"]),
            float(category_word["bottom"]),
            float(quantity_word["bottom"]),
            float(unit_price_word["bottom"]),
            float(total_word["bottom"]),
        )

        def parse_local_decimal(value):
            raw = re.sub(
                r"[^0-9,.\-]",
                "",
                str(value or ""),
            ).strip()

            if not raw:
                return None

            try:
                if "," in raw and "." in raw:
                    if raw.rfind(",") > raw.rfind("."):
                        raw = raw.replace(".", "").replace(",", ".")
                    else:
                        raw = raw.replace(",", "")

                elif "," in raw:
                    tail = raw.rsplit(",", 1)[-1]

                    if len(tail) in {1, 2}:
                        raw = raw.replace(".", "").replace(",", ".")
                    else:
                        raw = raw.replace(",", "")

                return Decimal(raw)

            except (InvalidOperation, ValueError):
                return None

        def line_cells(line):
            cells = {
                "material": [],
                "category": [],
                "quantity": [],
                "unit_price": [],
                "line_total": [],
            }

            for word in line.get("words", []):
                center_x = (
                    float(word["x0"])
                    + float(word["x1"])
                ) / 2

                if center_x < material_category_boundary:
                    if center_x >= material_x - 25:
                        cells["material"].append(word["text"])

                elif center_x < category_quantity_boundary:
                    cells["category"].append(word["text"])

                elif center_x < quantity_price_boundary:
                    cells["quantity"].append(word["text"])

                elif center_x < price_total_boundary:
                    cells["unit_price"].append(word["text"])

                else:
                    cells["line_total"].append(word["text"])

            return {
                key: re.sub(
                    r"\s+",
                    " ",
                    " ".join(value),
                ).strip()
                for key, value in cells.items()
            }

        rows = [VIRTUAL_HEADERS]
        row_meta = []
        current_group = None

        for line_number, line in enumerate(visual_lines, start=1):
            if float(line.get("top", 0)) <= header_bottom:
                continue

            cells = line_cells(line)
            material = cells["material"].strip()
            category = cells["category"].strip()

            # A unit/specification token can visually cross the boundary
            # between Material and Category. Move it back generically.
            misplaced_prefix = re.match(
                r"^\s*((?:mm|cm|m|kg|g|ltr|l|pcs?)"
                r"(?:²|2|³|3)?)\s+(.+)$",
                category,
                re.I,
            )

            if misplaced_prefix:
                material = re.sub(
                    r"\s+",
                    " ",
                    f"{material} {misplaced_prefix.group(1)}",
                ).strip()
                category = misplaced_prefix.group(2).strip()

            quantity = parse_local_decimal(cells["quantity"])
            unit_price = parse_local_decimal(cells["unit_price"])
            line_total = parse_local_decimal(cells["line_total"])

            normalized_material = _norm(material)

            if normalized_material.startswith(
                (
                    "total",
                    "subtotal",
                    "grand total",
                    "notes",
                    "terms",
                )
            ):
                break

            # A short parenthesized line immediately below a product is
            # usually a continuation/specification, e.g. "(GI SHEET)".
            if (
                material
                and material.startswith("(")
                and quantity is None
                and unit_price is None
                and line_total is None
                and len(rows) > 1
            ):
                product_index = VIRTUAL_HEADERS.index("product_name")
                specification_index = VIRTUAL_HEADERS.index("specification")

                previous_product = str(
                    rows[-1][product_index] or ""
                ).strip()

                if material not in previous_product:
                    rows[-1][product_index] = (
                        f"{previous_product} | {material}"
                    )

                previous_specification = str(
                    rows[-1][specification_index] or ""
                ).strip()

                rows[-1][specification_index] = " | ".join(
                    value
                    for value in (
                        previous_specification,
                        material,
                    )
                    if value
                )
                continue

            # Dynamic group heading:
            # a material-only row such as "CABLES;", "VALVES:", etc.
            if (
                material
                and quantity is None
                and unit_price is None
                and line_total is None
                and (
                    material.rstrip().endswith((";", ":"))
                    or (
                        material.upper() == material
                        and len(material.split()) <= 5
                    )
                )
            ):
                current_group = re.sub(
                    r"^\s*\d{1,3}\s*[-.)]?\s*",
                    "",
                    material,
                ).strip(" ;:")
                continue

            if not material:
                continue

            row_type = "PRODUCT"
            charge_description = None

            # Rows with an amount but without a normal quantity/rate pair are
            # treated as document charges rather than products.
            if line_total is not None and (
                quantity is None or unit_price is None
            ):
                row_type = "CHARGE"
                quantity = quantity or Decimal("1")
                unit_price = unit_price or line_total

                # For charges, preserve the complete wording from the visual
                # source line instead of rebuilding it from shifted columns.
                charge_description = re.sub(
                    r"^\s*\d{1,3}\s*[-.)]?\s*",
                    "",
                    str(line.get("text") or ""),
                ).strip()

                amount_tokens = [
                    cells.get("line_total"),
                    cells.get("unit_price"),
                ]

                for amount_token in amount_tokens:
                    if amount_token:
                        charge_description = re.sub(
                            rf"\s*{re.escape(amount_token)}\s*\$?\s*$",
                            "",
                            charge_description,
                            flags=re.I,
                        ).strip()

                charge_description = re.sub(
                    r"\s+",
                    " ",
                    charge_description,
                ).strip(" |:-")

            if (
                quantity is None
                or unit_price is None
            ):
                continue

            if line_total is None:
                line_total = quantity * unit_price

            display_parts = [
                charge_description if row_type == "CHARGE" else material
            ]

            if row_type != "CHARGE":
                if (
                    current_group
                    and _norm(current_group) not in _norm(material)
                ):
                    display_parts.append(current_group)

                if (
                    category
                    and _norm(category) not in _norm(material)
                ):
                    display_parts.append(category)

            product_name = " | ".join(
                part
                for part in display_parts
                if part
            )

            values = {
                "row_type": row_type,
                "product_name": product_name,
                "specification": (
                    " | ".join(
                        part
                        for part in (current_group, category)
                        if part
                    )
                    or None
                ),
                "quantity": str(quantity),
                # Some documents label this column collectively as
                # "Meter & Pcs". Preserve the source meaning without
                # hardcoding group-specific UoM rules.
                "unit_of_measurement": "METER/PCS",
                "unit_price": str(unit_price),
                "currency": (
                    self._currency_from_text(
                        " ".join(
                            word.get("text", "")
                            for word in line.get("words", [])
                        )
                    )
                    or "USD"
                ),
                "line_total": str(line_total),
            }

            rows.append([
                values.get(header)
                for header in VIRTUAL_HEADERS
            ])

            row_meta.append({
                field: {
                    "confidence": 93,
                    "source": {
                        "type": "visual_offer_grid",
                        **source,
                        "line": line_number,
                    },
                }
                for field, value in values.items()
                if value not in (None, "")
            })

        return (
            rows,
            row_meta,
        ) if len(rows) > 1 else None


    def _words_to_line_items(self, words, source):
        visual_lines = self._cluster_words_by_line(words)
        if len(visual_lines) < 2:
            return None
        header_index, columns, header_currency = self._visual_header_columns(visual_lines)
        if header_index is None:
            return None
        rows, row_meta, pending = [VIRTUAL_HEADERS], [], None
        data_lines = visual_lines[header_index + 1:]
        previous_bottom = visual_lines[header_index]["bottom"]
        for line_number, line in enumerate(data_lines, start=header_index + 2):
            text = line["text"]
            normalized = _norm(text)
            if any(term in normalized for term in TOTAL_STOP_TERMS):
                break
            if any(term in normalized for term in NOISE_TERMS):
                continue
            values, field_meta, serial = self._cells_from_visual_line(line, columns, source, line_number)
            if pending and self._is_visual_description_continuation(line, values, serial):
                pending_values, pending_meta = pending
                pending_values["product_name"] = " ".join([str(pending_values.get("product_name") or "").strip(), line["text"].strip()]).strip()
                pending_meta["product_name"] = {"confidence": 84, "source": {"type": "visual_wrapped_cell", **source, "line": line_number}}
                previous_bottom = line["bottom"]
                continue
            if header_currency and not values.get("currency"):
                values["currency"] = header_currency
                field_meta["currency"] = {"confidence": 88, "source": {"type": "table_header", **source}}
            values = self._repair_price_columns(values)
            if serial and not any(values.get(field) for field in LINE_SCHEMA):
                if pending:
                    self._append_visual_row(rows, row_meta, pending[0], pending[1], source, line_number - 1)
                pending = ({name: None for name in VIRTUAL_HEADERS}, {})
                pending[0]["_serial"] = serial
                previous_bottom = line["bottom"]
                continue
            description = self._clean_visual_description(values.get("product_name"), serial or (pending[0].get("_serial") if pending else None))
            if description:
                values["product_name"] = description
            has_description = any(values.get(field) for field in ("product_name", "specification", "material"))
            has_numbers = any(values.get(field) for field in ("quantity", "unit_price", "line_total", "tax_amount"))
            line_gap = line["top"] - previous_bottom
            if pending and (line_gap <= max(18, line["height"] * 1.8) or not has_numbers):
                pending_values, pending_meta = pending
                for field, value in values.items():
                    if field == "_serial" or value in (None, ""):
                        continue
                    if field in {"product_name", "specification", "material"} and pending_values.get(field):
                        pending_values[field] = " ".join([str(pending_values[field]).strip(), str(value).strip()]).strip()
                    else:
                        pending_values[field] = value
                    pending_meta[field] = field_meta.get(field, pending_meta.get(field))
                values, field_meta = pending_values, pending_meta
                has_description = any(values.get(field) for field in ("product_name", "specification", "material"))
                has_numbers = any(values.get(field) for field in ("quantity", "unit_price", "line_total", "tax_amount"))
            elif pending:
                self._append_visual_row(rows, row_meta, pending[0], pending[1], source, line_number - 1)
            if has_description:
                pending = (values, field_meta)
            elif has_numbers:
                pending = (values, field_meta)
            previous_bottom = line["bottom"]
        if pending:
            self._append_visual_row(rows, row_meta, pending[0], pending[1], source, len(visual_lines))
        return (rows, row_meta) if len(rows) > 1 else None

    def _supplier_from_order_issued_words(self, pages_words):
        for words in pages_words or []:
            lines = self._cluster_words_by_line(words)
            for line in lines:
                if "order issued to" not in _norm(line["text"]):
                    continue
                next_label_x = self._next_label_x(line, "delivery address") or float("inf")
                left_x = max(line["x0"] - 5, 0)
                right_x = next_label_x - 8
                top = line["bottom"]
                bottom = top + 115
                left_words = [
                    word for word in words
                    if top < float(word.get("top", 0)) < bottom
                    and left_x <= float(word.get("x0", 0)) <= right_x
                    and float(word.get("x1", 0)) <= right_x + 15
                ]
                for candidate in self._cluster_words_by_line(left_words):
                    candidate_norm = _norm(candidate["text"])
                    if any(stop in candidate_norm for stop in {"currency", "delivery date", "description quantity", "s no description", "note please"}):
                        break
                    value = candidate["text"].strip(" :-")
                    if self._is_supplier_like(value):
                        return value
        return None

    def _next_label_x(self, line, label):
        words = line.get("words") or []
        label_tokens = _norm(label).split()
        for start in range(len(words)):
            group = words[start:start + len(label_tokens)]
            if _norm(" ".join(word["text"] for word in group)) == _norm(label):
                return group[0]["x0"]
        return None

    def _cluster_words_by_line(self, words):
        cleaned = []
        for word in words or []:
            try:
                x0, x1, top, bottom = float(word["x0"]), float(word["x1"]), float(word["top"]), float(word["bottom"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(word.get("text") or "").strip()
            if text:
                cleaned.append({"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom})
        cleaned.sort(key=lambda item: (item["top"], item["x0"]))
        lines = []
        for word in cleaned:
            center_y = (word["top"] + word["bottom"]) / 2
            target = next((line for line in lines if abs(center_y - line["center_y"]) <= max(4, line["height"] * 0.45)), None)
            if not target:
                target = {"words": [], "center_y": center_y, "height": word["bottom"] - word["top"]}
                lines.append(target)
            target["words"].append(word)
            target["center_y"] = sum((w["top"] + w["bottom"]) / 2 for w in target["words"]) / len(target["words"])
            target["height"] = max(target["height"], word["bottom"] - word["top"])
        visual_lines = []
        for line in lines:
            line_words = sorted(line["words"], key=lambda item: item["x0"])
            visual_lines.append({
                "words": line_words,
                "text": " ".join(word["text"] for word in line_words),
                "x0": min(word["x0"] for word in line_words),
                "x1": max(word["x1"] for word in line_words),
                "top": min(word["top"] for word in line_words),
                "bottom": max(word["bottom"] for word in line_words),
                "height": max(word["bottom"] - word["top"] for word in line_words),
            })
        return sorted(visual_lines, key=lambda item: (item["top"], item["x0"]))

    def _visual_header_columns(self, visual_lines):
        best = (None, [], None, 0)
        for index, line in enumerate(visual_lines[:35]):
            columns = self._header_columns_from_words(line["words"])
            fields = {column["field"] for column in columns if column["field"]}
            if {"product_name", "quantity"}.issubset(fields) and ({"unit_price", "line_total"} & fields):
                score = len(fields) * 20 + sum(column["confidence"] for column in columns) / max(len(columns), 1)
                if score > best[3]:
                    header_currency = next((self._currency_from_text(word["text"]) for word in line["words"] if self._currency_from_text(word["text"])), None)
                    best = (index, columns, header_currency, score)
        return best[:3] if best[0] is not None else (None, [], None)

    def _header_columns_from_words(self, words):
        candidates = []
        word_count = len(words)
        for start in range(word_count):
            for size in range(1, min(4, word_count - start) + 1):
                group = words[start:start + size]
                label = " ".join(word["text"] for word in group)
                if self._is_serial_header(label):
                    candidates.append({"field": None, "kind": "serial", "x0": group[0]["x0"], "x1": group[-1]["x1"], "confidence": 100})
                    continue
                field, score = _match_field(label, LINE_SCHEMA)
                if field and score >= 72:
                    candidates.append({"field": field, "kind": "data", "x0": group[0]["x0"], "x1": group[-1]["x1"], "confidence": score})
        selected = []
        used_fields = set()
        for candidate in sorted(candidates, key=lambda item: (-item["confidence"], item["x0"])):
            if candidate["field"] in used_fields:
                continue
            if any(not (candidate["x1"] < item["x0"] or candidate["x0"] > item["x1"]) for item in selected):
                continue
            if candidate["field"]:
                used_fields.add(candidate["field"])
            selected.append(candidate)
        return sorted(selected, key=lambda item: item["x0"])

    def _cells_from_visual_line(self, line, columns, source, line_number):
        values = {name: None for name in VIRTUAL_HEADERS}
        field_meta, serial_tokens = {}, []
        boundaries = self._column_boundaries(columns)
        for word in line["words"]:
            center_x = (word["x0"] + word["x1"]) / 2
            column = next((item["column"] for item in boundaries if item["left"] <= center_x < item["right"]), None)
            if not column:
                continue
            if column["kind"] == "serial":
                serial_tokens.append(word["text"])
                continue
            field = column["field"]
            values[field] = " ".join(filter(None, [str(values.get(field) or "").strip(), word["text"]])).strip()
            field_meta[field] = {"confidence": column.get("confidence", 75), "source": {"type": "visual_cell", **source, "line": line_number, "x0": round(word["x0"], 2), "top": round(word["top"], 2)}}
        return values, field_meta, " ".join(serial_tokens).strip() or None

    def _column_boundaries(self, columns):
        ordered = sorted(columns, key=lambda item: (item["x0"] + item["x1"]) / 2)
        boundaries = []
        for index, column in enumerate(ordered):
            previous_column = ordered[index - 1] if index else None
            next_column = ordered[index + 1] if index + 1 < len(ordered) else None
            left = previous_column["x1"] if previous_column is not None else float("-inf")
            right = next_column["x0"] if next_column is not None else float("inf")
            if column.get("kind") != "serial" and previous_column and previous_column.get("kind") != "serial":
                center = (column["x0"] + column["x1"]) / 2
                previous_center = (previous_column["x0"] + previous_column["x1"]) / 2
                left = (previous_center + center) / 2
            if column.get("field") != "product_name" and next_column:
                center = (column["x0"] + column["x1"]) / 2
                next_center = (next_column["x0"] + next_column["x1"]) / 2
                right = (center + next_center) / 2
            boundaries.append({"left": left, "right": right, "column": column})
        return boundaries

    def _is_visual_description_continuation(self, line, values, serial):
        if serial:
            return False
        if any(values.get(field) for field in ("unit_of_measurement", "unit_price", "tax", "tax_amount", "line_total", "currency")):
            return False
        text = str(line.get("text") or "").strip()
        if not text or any(term in _norm(text) for term in TOTAL_STOP_TERMS | NOISE_TERMS):
            return False
        return any(ch.isalpha() for ch in text)

    def _clean_visual_description(self, description, serial):
        value = str(description or "").strip()
        if not value:
            return None
        if serial and re.fullmatch(r"0*\d{1,4}", str(serial).strip()):
            value = re.sub(rf"^\s*{re.escape(str(serial).strip())}\s+", "", value).strip()
        if re.fullmatch(r"0*\d{1,4}", value):
            return None
        return self._strip_leading_serial(value)

    def _append_visual_row(self, rows, row_meta, values, field_meta, source, line_number):
        values = self._repair_price_columns(values)
        description = values.get("product_name") or values.get("specification") or values.get("material")
        has_amount_signal = any(values.get(field) for field in ("quantity", "unit_price", "line_total", "tax_amount"))
        if not description or not has_amount_signal or self._is_totals_or_noise_row([description]):
            return
        values["row_type"] = self._row_type(description)
        field_meta["row_type"] = {"confidence": 90 if values["row_type"] != "UNKNOWN" else 40, "source": {"type": "classifier", **source, "line": line_number}}
        rows.append([values.get(header) for header in VIRTUAL_HEADERS])
        row_meta.append(field_meta)

    def _goods_table_without_repeated_header(self, cleaned, source):
        """
        Handles continuation pages where the goods table continues
        without repeating the header.

        Expected layout:
        serial | description | part number | UoM | qty | total | unit rate | delivery
        """

        if not cleaned:
            return None

        rows = [VIRTUAL_HEADERS]
        row_meta = []

        known_uom_values = {
            "NO", "NOS", "PC", "PCS", "EA", "EACH",
            "M", "MTR", "METER", "METRE", "MM", "CM",
            "KG", "GM", "G", "LTR", "L", "LITRE",
            "ROLL", "TIN", "BOX", "PACK", "PKT",
            "SET", "PAIR", "LOT", "LS", "SHEET",
            "DRUM", "BAG", "COIL", "TUBE",
        }

        for row_offset, row in enumerate(cleaned, start=1):
            cells = [
                re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()
                for cell in row
            ]

            if not any(cells):
                continue

            normalized = _norm(" ".join(cells))

            if self._is_totals_or_noise_row(cells):
                continue

            if normalized.startswith(
                ("remarks", "terms", "total usd", "total aed", "grand total")
            ):
                break

            if len(cells) < 6:
                continue

            serial = cells[0]

            if not re.fullmatch(r"\d{1,4}", serial or ""):
                continue

            description = cells[1] if len(cells) > 1 else None
            part_number = cells[2] if len(cells) > 2 else None

            unit = None
            quantity = None
            unit_price = None
            line_total = None

            # Find UoM anywhere in the row, since PDF columns may shift.
            uom_index = None

            for index, cell in enumerate(cells):
                candidate = str(cell or "").upper().strip(" .:-")

                if candidate in known_uom_values:
                    unit = candidate
                    uom_index = index
                    break

            if uom_index is not None:
                if uom_index + 1 < len(cells):
                    quantity = _decimal(cells[uom_index + 1])

                # Florence PDF visually contains:
                # UoM | Qty | U.Rate | Total
                if uom_index + 2 < len(cells):
                    first_price = _decimal(cells[uom_index + 2])
                else:
                    first_price = None

                if uom_index + 3 < len(cells):
                    second_price = _decimal(cells[uom_index + 3])
                else:
                    second_price = None

                if quantity is not None and first_price is not None and second_price is not None:
                    # Determine which value is rate and which is total.
                    if abs((quantity * first_price) - second_price) <= max(
                        Decimal("0.05"),
                        abs(second_price) * Decimal("0.02"),
                    ):
                        unit_price = first_price
                        line_total = second_price

                    elif abs((quantity * second_price) - first_price) <= max(
                        Decimal("0.05"),
                        abs(first_price) * Decimal("0.02"),
                    ):
                        unit_price = second_price
                        line_total = first_price

                    else:
                        unit_price = first_price
                        line_total = second_price

                elif first_price is not None:
                    unit_price = first_price

            product_name = self._clean_product_description(description)

            if not product_name:
                continue

            if quantity is None or unit_price is None:
                continue

            if line_total is None:
                line_total = quantity * unit_price

            row_type = self._row_type(product_name)

            if row_type == "UNKNOWN":
                row_type = "PRODUCT"

            values = {
                "row_type": row_type,
                "product_name": product_name,
                "item_code": part_number or None,
                "part_number": part_number or None,
                "quantity": str(quantity),
                "unit_of_measurement": unit,
                "unit_price": str(unit_price),
                "currency": self._currency_from_text(" ".join(cells)),
                "line_total": str(line_total),
            }

            rows.append([
                values.get(header)
                for header in VIRTUAL_HEADERS
            ])

            row_meta.append({
                field: {
                    "confidence": 90,
                    "source": {
                        "type": "goods_continuation_table",
                        **source,
                        "row": row_offset,
                    },
                }
                for field, value in values.items()
                if value not in (None, "")
            })

        return (rows, row_meta) if len(rows) > 1 else None
    def _item_description_quote_table_to_line_items(self, table, source):
        cleaned = [
            [str(cell or "").strip() for cell in row]
            for row in table
            if row and any(str(cell or "").strip() for cell in row)
        ]

        if len(cleaned) < 2:
            return None

        header_index = None
        header_map = {}

        for row_index, row in enumerate(cleaned[:8]):
            normalized = [_norm(cell) for cell in row]

            if not any("item description" in cell or ("item" in cell and "description" in cell) for cell in normalized):
                continue
            if not any(cell in {"qty", "quantity"} or "qty" in cell for cell in normalized):
                continue
            if not any("rate" in cell or "price" in cell for cell in normalized):
                continue
            if not any("amount" in cell or "total" in cell for cell in normalized):
                continue

            header_index = row_index
            for index, header in enumerate(normalized):
                if "item" in header and "description" in header:
                    header_map[index] = "description"
                elif header in {"item code", "product code", "code"}:
                    header_map[index] = "item_code"
                elif header in {"unit", "uom", "unit of measure", "unit of measurement"}:
                    header_map[index] = "unit_of_measurement"
                elif header in {"qty", "quantity"} or "qty" in header:
                    header_map[index] = "quantity"
                elif "rate" in header or "price" in header:
                    header_map[index] = "unit_price"
                elif header == "tax" or header == "tax amount":
                    header_map[index] = "tax_amount"
                elif "amount" in header or "total" in header:
                    header_map[index] = "line_total"
            break

        if header_index is None:
            return None

        rows, row_meta = [VIRTUAL_HEADERS], []
        header_text = " ".join(" ".join(row) for row in cleaned[: header_index + 1])
        currency = self._currency_from_text(header_text) or ("USD" if "$" in header_text else None)

        def clean_cell(value):
            return clean_spaces(value)

        def split_qty_uom(value):
            text = clean_cell(value)
            match = re.match(
                r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>no\.?s|nos|pcs?|ea|each|unit|set|lot|ls|pkt|roll|mtr|meter|kg|tin)?\s*$",
                text,
                re.I,
            )
            if not match:
                return None, None
            uom = (match.group("uom") or "").replace(".", "")
            if uom.lower() in {"no", "nos"}:
                uom = "Nos"
            else:
                uom = uom.upper()
            return match.group("qty"), uom or None

        def split_description(value):
           

            raw_text = str(value or "").strip()

            if not raw_text:
                return None, None, None

            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in raw_text.splitlines()
                if line.strip()
            ]

            flattened = re.sub(
                r"\s+",
                " ",
                raw_text,
            ).strip()

            item_code = None
            part_number = None
            description_text = flattened

            # Extract one or two leading product identifiers.
            #
            # Examples:
            # TX820 ANN-3N5NI
            # ANN-3N5NI
            identity_match = re.match(
                r"^\s*"
                r"(?P<first>[A-Z0-9][A-Z0-9/._-]{2,})"
                r"(?:\s+"
                r"(?P<second>[A-Z0-9][A-Z0-9/._-]{2,})"
                r")?",
                flattened,
                re.I,
            )

            if identity_match:
                first_value = identity_match.group("first")
                second_value = identity_match.group("second")

                # When two identifiers exist:
                # TX820 = item code
                # ANN-3N5NI = part number
                if (
                    second_value
                    and re.search(r"[A-Za-z]", second_value)
                    and not re.fullmatch(
                        r"\d+(?:[,.]\d+)?",
                        second_value,
                    )
                ):
                    item_code = first_value
                    part_number = second_value

                else:
                    # A single leading identifier is treated as part number.
                    part_number = first_value

                description_text = flattened[
                    identity_match.end():
                ].strip(" |:-")

            # Remove an accidentally merged commercial value block appearing
            # immediately after the product identifiers.
            #
            
            commercial_prefix_pattern = re.compile(
                r"^\s*"
                r"\d+(?:[,.]\d+)?"
                r"(?:\s+(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS))?"
                r"\s+"
                r"\d[\d,]*(?:\.\d+)?"
                r"\s+"
                r"\d+(?:[,.]\d+)?"
                r"\s+"
                r"\d[\d,]*(?:\.\d+)?"
                r"\s+"
                r"\d[\d,]*(?:\.\d+)?"
                r"(?:\s+(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS))?"
                r"\s*",
                re.I,
            )

            description_text = commercial_prefix_pattern.sub(
                "",
                description_text,
                count=1,
            ).strip(" |:-")

            # Some PDF extractors place UoM immediately before description:
            # "pcs TAPFLO, 3 inch Pump..."
            description_text = re.sub(
                r"^\s*(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS)\s+",
                "",
                description_text,
                count=1,
                flags=re.I,
            ).strip()

            # When proper line breaks are available, use only the real
            # description lines and skip the identifier line.
            if len(lines) > 1:
                first_line_normalized = _norm(lines[0])

                if (
                    item_code
                    and _norm(item_code) in first_line_normalized
                ) or (
                    part_number
                    and _norm(part_number) in first_line_normalized
                ):
                    line_description = " ".join(lines[1:]).strip()

                    # Remove accidentally merged quantity/price block from the
                    # beginning of the multiline description too.
                    line_description = commercial_prefix_pattern.sub(
                        "",
                        line_description,
                        count=1,
                    ).strip(" |:-")

                    if line_description:
                        description_text = line_description

            # Keep readable product summary only.
            # Stop before the detailed technical specification section.
            light_detail_parts = []

            for detail in re.split(
                r"(?<=[.!?])\s+|\s+(?="
                r"Connections:|Air Connection:|Max Capacity:|"
                r"Displacement:|Max Discharge|Max Air Pressure|"
                r"Max Suction|Max Solids|Temperature Range:|"
                r"Pump Housing:|Inlet / Outlet|Diaphragms:|"
                r"Valve Balls:|Valve Seats|Centre Block:|"
                r"Air Valve:|ATEX Rating:|Footprint:|Weight:|"
                r"Dimensions:"
                r")",
                description_text,
                flags=re.I,
            ):
                detail = detail.strip()

                if not detail:
                    continue

                normalized = _norm(detail)

                if any(
                    normalized.startswith(term)
                    for term in {
                        "connections",
                        "air connection",
                        "max capacity",
                        "displacement",
                        "max discharge",
                        "max air pressure",
                        "max suction",
                        "max solids",
                        "temperature range",
                        "pump housing",
                        "inlet outlet",
                        "diaphragms",
                        "valve balls",
                        "valve seats",
                        "centre block",
                        "air valve",
                        "atex rating",
                        "footprint",
                        "weight",
                        "dimensions",
                    }
                ):
                    break

                light_detail_parts.append(detail)

            light_description = re.sub(
                r"\s+",
                " ",
                " ".join(light_detail_parts),
            ).strip(" |:-")
            # Final safety cleanup: product description must not start with
            # quantity, unit price, VAT, VAT amount and line total.
            light_description = commercial_prefix_pattern.sub(
                "",
                light_description,
                count=1,
            ).strip(" |:-")

            # Remove UoM accidentally left before the readable description.
            light_description = re.sub(
                r"^\s*(?:PCS?|PC|EA|EACH|NOS?|UNIT|SET|LOT|LS)\s+",
                "",
                light_description,
                count=1,
                flags=re.I,
            ).strip(" |:-")
            return item_code, part_number, light_description

        for row_offset, source_row in enumerate(cleaned[header_index + 1:], start=header_index + 2):
            raw = {}
            for index, field in header_map.items():
                raw[field] = source_row[index] if index < len(source_row) else ""

            description_raw = raw.get("description") or ""
            if not description_raw:
                continue

            normalized_description = _norm(description_raw)
            if any(term in normalized_description for term in {
                "sub total", "tax summary", "bank information",
                "additional notes", "terms conditions", "total aed",
                "account number", "iban", "total pi amount",
                "product specifications", "prices and validity",
                "order confirmation", "delivery timeline",
                "payment terms", "shipping and risk",
                "packing charges", "cancellation and modifications",
                "returns and replacements", "warranty",
                "ownership of goods", "intellectual property",
                "confidentiality", "force majeure",
                "technical support", "governing law",
                "acceptance of terms",
            }):
                continue

            quantity, uom = split_qty_uom(raw.get("quantity"))
            if not uom:
                explicit_uom = clean_cell(raw.get("unit_of_measurement")).upper().replace(".", "")
                if explicit_uom:
                    uom = explicit_uom
            if not uom:
                uom_match = re.search(
                    r"\b\d+(?:\.\d+)?\s+(no\.?s|nos|pcs?|ea|each|unit|set|lot|ls|pkt|roll|mtr|meter|kg|tin)\b",
                    description_raw,
                    re.I,
                )
                if uom_match:
                    uom = uom_match.group(1).upper().replace(".", "")
            unit_price = _decimal(raw.get("unit_price"))
            line_total = _decimal(raw.get("line_total"))
            tax_amount = _decimal(raw.get("tax_amount"))

            if not quantity or unit_price is None or line_total is None:
                continue

            item_code, part_number, light_description = split_description(description_raw)
            explicit_item_code = clean_cell(raw.get("item_code"))
            if explicit_item_code:
                item_code = explicit_item_code
                part_number = None
                light_description = clean_cell(description_raw)

            light_description = re.sub(
                r"\s+\d+(?:\.\d+)?\s+(?:no\.?s|nos|pcs?|ea|each|unit|set|lot|ls)\s+"
                r"\d[\d,]*(?:\.\d+)?\s+\d[\d,]*(?:\.\d+)?\s+\d[\d,]*(?:\.\d+)?\b.*$",
                "",
                light_description or "",
                flags=re.I,
            ).strip(" |:-")
            light_description = strip_section_continuation(light_description)

            if explicit_item_code:
                product_name = light_description or clean_cell(description_raw)
            else:
                product_identity = " | ".join(
                    part for part in [item_code, part_number]
                    if part
                )
                product_name = product_identity or clean_cell(description_raw)
                if light_description:
                    product_name = f"{product_name} | {light_description}"
            product_name = strip_section_continuation(product_name)
            normalized_product_name = _norm(product_name)
            values = {
                "row_type": "PRODUCT",
                "item_code": item_code,
                "part_number": part_number,
                "product_name": self._clean_product_description(product_name),
                "quantity": quantity,
                "unit_of_measurement": uom,
                "unit_price": str(unit_price),
                "currency": currency,
                "tax_amount": str(tax_amount) if tax_amount is not None else None,
                "line_total": str(line_total),
            }

            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {"confidence": 90, "source": {"type": "item_description_quote_table", **source, "row": row_offset}}
                for field, value in values.items()
                if value not in (None, "")
            })

        return (rows, row_meta) if len(rows) > 1 else None
    def _mooring_description_model_table_to_line_items(self, table, source):
        cleaned = [
            [str(cell or "").replace("\n", " ").strip() for cell in row]
            for row in table
            if row and any(str(cell or "").strip() for cell in row)
        ]
        if len(cleaned) < 2:
            return None

        header_index = None
        header_map = {}
        for row_index, row in enumerate(cleaned[:8]):
            normalized = [_norm(cell) for cell in row]
            header_text = " ".join(normalized)
            if not (
                "product description" in header_text
                and ("model specification" in header_text or "specification" in header_text)
                and "quantity" in header_text
                and "unit price" in header_text
                and "total price" in header_text
            ):
                continue

            header_index = row_index
            for index, header in enumerate(normalized):
                if header in {"no", "no."} or header.startswith("no "):
                    header_map[index] = "serial"
                elif "product description" in header:
                    header_map[index] = "description"
                elif "model" in header or "specification" in header:
                    header_map[index] = "specification"
                elif "material" in header:
                    header_map[index] = "material"
                elif "quantity" in header or header == "qty":
                    header_map[index] = "quantity"
                elif "unit price" in header:
                    header_map[index] = "unit_price"
                elif "total price" in header:
                    header_map[index] = "line_total"
            break

        if header_index is None:
            return None

        currency = "USD"
        rows = [VIRTUAL_HEADERS]
        row_meta = [{}]

        for row_offset, source_row in enumerate(cleaned[header_index + 1:], start=header_index + 2):
            if self._is_totals_or_noise_row(source_row):
                break

            raw = {field: source_row[index] for index, field in header_map.items() if index < len(source_row)}
            description = self._clean_product_description(raw.get("description"))
            specification = self._clean_product_description(raw.get("specification"))
            material = self._clean_product_description(raw.get("material"))
            quantity = _decimal(raw.get("quantity"))
            unit_price = _decimal(raw.get("unit_price"))
            line_total = _decimal(raw.get("line_total"))

            if not description:
                continue
            if not quantity and line_total is not None and unit_price is None:
                quantity = Decimal("1")
                unit_price = line_total
            if quantity is None or unit_price is None:
                continue
            if line_total is None:
                line_total = quantity * unit_price

            parts = [description]
            if specification and _norm(specification) != _norm(description):
                parts.append(specification)
            if material and _norm(material) not in {_norm(description), _norm(specification)}:
                parts.append(material)

            product_name = " | ".join(part for part in parts if part).strip()

            row_type = self._row_type(product_name)
            values = {
                "row_type": row_type,
                "product_name": product_name,
                "specification": specification,
                "material": material,
                "quantity": str(quantity),
                "unit_price": str(unit_price),
                "currency": currency,
                "line_total": str(line_total),
            }
            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {"confidence": 92, "source": {"type": "mooring_description_model_table", **source, "row": row_offset}}
                for field, value in values.items()
                if value not in (None, "")
            })

        return (rows, row_meta) if len(rows) > 1 else None
    def _structured_pdf_table_to_line_items(self, table, source):
        cleaned = [
            [str(cell or "").replace("\n", " ").strip() for cell in row]
            for row in table
            if any(str(cell or "").strip() for cell in row)
        ]

        if len(cleaned) < 2:
            return None

        has_repeated_header = any(
            any(
                term in _norm(cell)
                for term in {
                    "description",
                    "product",
                    "goods",
                    "uom",
                    "unit",
                    "u rate",
                    "unit rate",
                    "unit price",
                    "total price",
                    "quantity",
                    "qty",
                }
            )
            for row in cleaned[:4]
            for cell in row
        )

        if not has_repeated_header:
            continuation = self._goods_table_without_repeated_header(cleaned, source)
            if continuation:
                return continuation

        header_index = None
        final_header_map = {}

        for row_index, row in enumerate(cleaned[:25]):
            # Important: create a fresh header map for every candidate row.
            candidate_header_map = {}
            normalized_cells = [_norm(cell) for cell in row]

            has_description_style = any(
                "description" in cell
                or "goods" in cell
                or cell in {"name", "details", "item"}
                for cell in normalized_cells
            )

            has_price_style = any(
                "rate" in cell
                or "price" in cell
                or "amount" in cell
                or "total" in cell
                or "value" in cell
                or cell in {"sum", "u rate"}
                for cell in normalized_cells
            )

            if not has_description_style or not has_price_style:
                continue


            for index, header in enumerate(normalized_cells):
                compact_header = re.sub(r"[^a-z0-9]", "", header)
                stacked_header = header
                if row_index > 0 and index < len(cleaned[row_index - 1]):
                    previous_header = _norm(cleaned[row_index - 1][index])
                    if previous_header:
                        stacked_header = f"{previous_header} {header}".strip()
                if header in {
                    "item no",
                    "sr",
                    "sr no",
                    "sl no",
                    "serial no",
                    "serial number",
                    "s no",
                }:
                    candidate_header_map[index] = "serial"

                elif header == "item":
                    candidate_header_map[index] = "item_name"

                elif header in {
                    "part no",
                    "part number",
                    "p n",
                    "pn",
                }:
                    candidate_header_map[index] = "part_number"

                elif header in {
                    "code",
                    "item code",
                    "product code",
                }:
                    candidate_header_map[index] = "item_code"

                elif "model" in header or "specification" in header:
                    candidate_header_map[index] = "specification"
                elif (
                    header in {"material", "materials", "grade"}
                    or "material" in header
                    or "material description" in stacked_header
                ):
                    candidate_header_map[index] = "material"
                elif header in {
                    "item name",
                    "name",
                    "product",
                    "product name",
                }:
                    candidate_header_map[index] = "item_name"

                elif header == "details":
                    candidate_header_map[index] = "description"

                elif "description" in header:
                    candidate_header_map[index] = "description"

                elif "goods" in header:
                    candidate_header_map[index] = "item_name"

                # Robust UoM matching.
                elif (
                    compact_header in {
                        "uom",
                        "unit",
                        "unitofmeasure",
                        "unitofmeasurement",
                        "measure",
                    }
                    or re.fullmatch(r"u\s*o\s*m", header)
                ):
                    candidate_header_map[index] = "unit_of_measurement"

                elif (
                    header in {"qty", "quantity"}
                    or "qty" in header
                    or "quantity" in header
                ):
                    candidate_header_map[index] = "quantity"

                elif (
                    "unit price" in header
                    or "unit rate" in header
                    or "unit value" in header
                    or header in {"rate", "price", "value", "u rate"}
                ):
                    candidate_header_map[index] = "unit_price"

                elif (
                    "subtotal" in header
                    or "sub total" in header
                    or "total" in header
                    or "amount" in header
                    or ("value" in header and "unit" not in header)
                ):
                    candidate_header_map[index] = "line_total"

                elif "currency" in header:
                    candidate_header_map[index] = "currency"

            mapped_fields = set(candidate_header_map.values())

            valid_header = (
                {
                    "item_name",
                    "quantity",
                    "unit_price",
                    "line_total",
                }.issubset(mapped_fields)
                or {
                    "description",
                    "quantity",
                    "unit_price",
                    "line_total",
                }.issubset(mapped_fields)
                or {
                    "item_name",
                    "unit_price",
                    "line_total",
                }.issubset(mapped_fields)
                or {
                    "description",
                    "unit_price",
                    "line_total",
                }.issubset(mapped_fields)
            )

            if valid_header:
                header_index = row_index
                final_header_map = candidate_header_map
                break

        if header_index is None:
            return None

        header_map = final_header_map

        def clean_cell(value):
            return clean_spaces(str(value or "").replace("\n", " "))

        rows = [VIRTUAL_HEADERS]
        row_meta = []

        header_text = " ".join(cleaned[header_index])
        header_currency = self._currency_from_text(header_text)
        is_goods_description_table = "description of goods" in _norm(header_text)
        known_uom_values = {
            "NO",
            "NOS",
            "PC",
            "PCS",
            "EA",
            "EACH",
            "M",
            "MTR",
            "METER",
            "METRE",
            "MM",
            "CM",
            "KG",
            "GM",
            "G",
            "LTR",
            "L",
            "LITRE",
            "ROLL",
            "TIN",
            "BOX",
            "PACK",
            "PKT",
            "SET",
            "PAIR",
            "LOT",
            "LS",
            "SHEET",
            "DRUM",
            "BAG",
            "COIL",
        }

        for row_offset, source_row in enumerate(
            cleaned[header_index + 1:],
            start=header_index + 2,
        ):
            if self._is_totals_or_noise_row(source_row):
                break
            if is_goods_description_table and len(source_row) >= 7:
                product_name = self._clean_product_description(
                    " | ".join(
                        part for part in [
                            clean_cell(source_row[1]),
                            clean_cell(source_row[2]),
                            clean_cell(source_row[3]),
                        ]
                        if part
                    )
                )

                quantity = _decimal(source_row[4])
                unit_price = _decimal(source_row[5])
                line_total = _decimal(source_row[6])

                if product_name and unit_price is None and line_total is not None:
                    unit_price = line_total
                    quantity = quantity or Decimal("1")

                if product_name and unit_price is not None:
                    if quantity is None:
                        quantity = Decimal("1")
                    if line_total is None:
                        line_total = quantity * unit_price

                    row_type = self._row_type(product_name)
                    if "package cost" in _norm(product_name):
                        row_type = "CHARGE"

                    values = {
                        "row_type": row_type,
                        "product_name": product_name,
                        "quantity": str(quantity),
                        "unit_of_measurement": None,
                        "unit_price": str(unit_price),
                        "currency": header_currency,
                        "line_total": str(line_total),
                    }

                    rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                    row_meta.append({
                        field: {
                            "confidence": 92,
                            "source": {
                                "type": "goods_description_pdf_table",
                                **source,
                                "row": row_offset,
                            },
                        }
                        for field, value in values.items()
                        if value not in (None, "")
                    })
                    continue
            values = {name: None for name in VIRTUAL_HEADERS}
            field_meta = {}

            raw = {}

            for index, cell in enumerate(source_row):
                field = header_map.get(index)

                if not field:
                    continue

                raw[field] = clean_cell(cell)

            part_number = clean_cell(
                raw.get("part_number")
            )
            item_code = clean_cell(
                raw.get("item_code")
            )
            if not part_number:
                row_text = " ".join(clean_cell(cell) for cell in source_row)
                part_match = re.search(
                    r"\b[A-Z0-9]{2,}[A-Z0-9-]*-\d[A-Z0-9-]*\b",
                    row_text,
                    re.I,
                )
                if part_match:
                    part_number = part_match.group(0)
            item_name = clean_cell(
                raw.get("item_name")
            )

            description = clean_cell(
                raw.get("description")
            )
            if part_number and (not description or description == part_number):
                description_cells = []
                for cell in source_row:
                    cell_text = clean_cell(cell)
                    if not cell_text or cell_text == part_number:
                        continue
                    if _decimal(cell_text) is not None:
                        continue
                    if cell_text.upper() in known_uom_values:
                        continue
                    if _norm(cell_text) in {"ex stock", "exstock", "availability"}:
                        continue
                    if re.fullmatch(r"\d{1,3}", cell_text):
                        continue
                    if any(term in _norm(cell_text) for term in {"sl no", "part no", "description", "qty", "unit", "price"}):
                        continue
                    description_cells.append(cell_text)

                if description_cells:
                    description = self._clean_product_description(" ".join(description_cells))
            serial = clean_cell(
                raw.get("serial")
            )

            specification = clean_cell(
                raw.get("specification")
            )

            material = clean_cell(
                raw.get("material")
            )
            if not material:
                material_column_index = next(
                    (
                        index
                        for index, field in header_map.items()
                        if field == "material"
                    ),
                    None,
                )

                if material_column_index is not None:
                    for candidate_index in (
                        material_column_index,
                        material_column_index - 1,
                        material_column_index + 1,
                    ):
                        if candidate_index < 0 or candidate_index >= len(source_row):
                            continue

                        candidate_material = clean_cell(source_row[candidate_index])
                        candidate_norm = _norm(candidate_material)

                        if not candidate_material:
                            continue
                        if _decimal(candidate_material) is not None:
                            continue
                        if candidate_norm in known_uom_values:
                            continue
                        if any(
                            term in candidate_norm
                            for term in {
                                "material",
                                "description",
                                "quantity",
                                "price",
                                "total",
                                "amount",
                                "per unit",
                            }
                        ):
                            continue

                        material = candidate_material
                        break
            if not material:
                for candidate_cell in source_row:
                    candidate_material = clean_cell(candidate_cell)
                    candidate_norm = _norm(candidate_material)

                    if not candidate_material:
                        continue
                    if _decimal(candidate_material) is not None:
                        continue
                    if candidate_norm in known_uom_values:
                        continue
                    if candidate_material in {item_name, description, part_number, serial}:
                        continue
                    if not re.search(
                        r"\b(?:thickness|swl|base|cover|steel|ss\s*316|"
                        r"stainless|aluminium|aluminum|"
                        r"cast|iron|bronze|rubber|plastic|grade)\b",
                        candidate_norm,
                        re.I,
                    ):
                        continue

                    material = candidate_material
                    break

            clean_part_number = self._clean_product_description(part_number)
            clean_item_name = self._clean_product_description(item_name)
            clean_description = self._clean_product_description(description)
            clean_specification = self._clean_product_description(specification)
            clean_material = self._clean_product_description(material)

            description_parts = []
            for part in (
                clean_item_name,
                clean_description,
                clean_specification,
                clean_material,
            ):
                if not part:
                    continue
                if clean_part_number and _norm(part) == _norm(clean_part_number):
                    continue
                if any(_norm(existing) == _norm(part) for existing in description_parts):
                    continue
                description_parts.append(part)

            merged_description = " ".join(description_parts).strip()
            if serial and re.fullmatch(r"\d{1,4}", str(serial).strip()):
                merged_description = re.sub(
                    rf"^\s*{re.escape(str(serial).strip())}\s+",
                    "",
                    merged_description,
                ).strip()
            merged_description = self._strip_leading_serial(merged_description)

            if clean_part_number and merged_description:
                product_name = f"{clean_part_number} | {merged_description}"
            elif item_code and merged_description:
                product_name = merged_description
            elif clean_part_number:
                product_name = clean_part_number
            else:
                product_name = merged_description

            if product_name and source_row:
                row_text = " ".join(str(cell or "").strip() for cell in source_row if str(cell or "").strip())
                continuation_match = re.search(
                    r"\b(?:DAYS\s+UPON\s+)?(?:ORDER\s+)?(?P<tail>NPT\)\s+SS316\b.*)$",
                    row_text,
                    re.I,
                )
                if continuation_match and _norm(continuation_match.group("tail")) not in _norm(product_name):
                    product_name = re.sub(
                        r"\s+",
                        " ",
                        f"{product_name} {continuation_match.group('tail')}",
                    ).strip()

            if (
                not product_name
                and serial
                and not re.fullmatch(r"\d{1,4}", serial)
            ):
                product_name = serial.strip(" :-")

            if not product_name:
                ignored_description = self._description_from_ignored_cells(
                    source_row,
                    header_map,
                )

                if (
                    ignored_description
                    and self._row_type(ignored_description) == "CHARGE"
                ):
                    product_name = ignored_description

            unit_price = _decimal(raw.get("unit_price"))
            line_total = _decimal(raw.get("line_total"))
            quantity = _decimal(raw.get("quantity"))
            if line_total is None:
                numeric_after_unit_price = []
                unit_price_column_index = next(
                    (
                        index
                        for index, field in header_map.items()
                        if field == "unit_price"
                    ),
                    None,
                )

                if unit_price_column_index is not None:
                    for candidate_index in range(unit_price_column_index + 1, len(source_row)):
                        candidate_value = _decimal(source_row[candidate_index])
                        if candidate_value is not None:
                            numeric_after_unit_price.append(candidate_value)

                if numeric_after_unit_price:
                    line_total = numeric_after_unit_price[-1]
            if line_total is None:
                unit_price_column_index = next(
                    (
                        index
                        for index, field in header_map.items()
                        if field == "unit_price"
                    ),
                    None,
                )

                if unit_price_column_index is not None:
                    for candidate_index in range(unit_price_column_index + 1, len(source_row)):
                        candidate_total = _decimal(source_row[candidate_index])
                        if candidate_total is not None:
                            line_total = candidate_total
                            break
            unit = clean_cell(raw.get("unit_of_measurement"))

            if unit:
                unit = unit.upper().strip(" .:-")

            # Positional fallback:
            # if the UoM header exists, read directly from its original column.
            if not unit:
                uom_column_index = next(
                    (
                        index
                        for index, field in header_map.items()
                        if field == "unit_of_measurement"
                    ),
                    None,
                )

                if (
                    uom_column_index is not None
                    and uom_column_index < len(source_row)
                ):
                    candidate_unit = clean_cell(
                        source_row[uom_column_index]
                    ).upper().strip(" .:-")

                    if candidate_unit in known_uom_values:
                        unit = candidate_unit

            # Additional fallback:
            # inspect cells immediately before the quantity column.
            if not unit:
                quantity_column_index = next(
                    (
                        index
                        for index, field in header_map.items()
                        if field == "quantity"
                    ),
                    None,
                )

                if (
                    quantity_column_index is not None
                    and quantity_column_index > 0
                    and quantity_column_index - 1 < len(source_row)
                ):
                    candidate_unit = clean_cell(
                        source_row[quantity_column_index - 1]
                    ).upper().strip(" .:-")

                    if candidate_unit in known_uom_values:
                        unit = candidate_unit

            if not unit:
                header_text_for_unit = " ".join(cleaned[header_index])

                unit_match = re.search(
                    r"\bqty\.?\s*\(([^)]+)\)",
                    header_text_for_unit,
                    re.I,
                )

                if unit_match:
                    candidate_unit = unit_match.group(1).strip().upper()

                    if candidate_unit in known_uom_values:
                        unit = candidate_unit

            if (
                quantity is None
                and unit
                and _norm(raw.get("quantity")) == _norm(unit)
            ):
                quantity = Decimal("1")

            elif (
                quantity is None
                and self._row_type(product_name) == "CHARGE"
            ):
                quantity = Decimal("1")

            currency = (
                raw.get("currency")
                or self._currency_from_text(raw.get("unit_price"))
                or self._currency_from_text(raw.get("line_total"))
                or header_currency
            )

            if not product_name or unit_price is None:
                if (
                    product_name
                    and line_total is not None
                    and self._row_type(product_name) == "CHARGE"
                ):
                    unit_price = line_total
                    quantity = quantity or Decimal("1")
                else:
                    continue

            product_norm = _norm(product_name)
            if re.search(
                r"\b(should you have any enquiries|thank you for your business|above information is not an invoice|"
                r"payment will be collected|p\.?\s*o\.?\s*box|tel\.?|e-?mail)\b",
                product_norm,
                re.I,
            ):
                continue
            if (
                product_norm
                in {
                    "total usd",
                    "total aed",
                    "subtotal",
                    "sub total",
                }
                or product_norm.startswith(
                    (
                        "total",
                        "subtotal",
                        "sub total",
                    )
                )
            ):
                continue

            values["row_type"] = self._row_type(product_name)
            values["product_name"] = product_name
            values["item_code"] = item_code or part_number
            values["part_number"] = part_number
            values["specification"] = specification
            values["material"] = material
            values["unit_of_measurement"] = unit
            values["quantity"] = (
                str(quantity)
                if quantity is not None
                else None
            )
            values["unit_price"] = str(unit_price)
            values["line_total"] = (
                str(line_total)
                if line_total is not None
                else None
            )
            values["currency"] = currency

            for field, value in values.items():
                if value not in (None, ""):
                    field_meta[field] = {
                        "confidence": 92,
                        "source": {
                            "type": "structured_pdf_table",
                            **source,
                            "row": row_offset,
                        },
                    }

            rows.append(
                [
                    values.get(header)
                    for header in VIRTUAL_HEADERS
                ]
            )

            row_meta.append(field_meta)

        return (
            (rows, row_meta)
            if len(rows) > 1
            else None
        )
    def _table_to_line_items(self, table, source):
        cleaned = [[str(cell or "").replace("\n", " ").strip() for cell in row] for row in table if any(cell not in (None, "") for cell in row)]
        header_text = _norm(" ".join(" ".join(row) for row in cleaned[:3]))
        if (
            "item description" in header_text
            and "qty" in header_text
            and "rate" in header_text
            and ("tax amount" in header_text or "amount" in header_text)
            and len(cleaned) > 2
        ):
            return None
        if len(cleaned) < 2:
            return None
        header_index, header_map, header_confidence, header_currency = self._find_line_header(cleaned)
        if header_index is None:
            return None
        rows, row_meta, pending = [VIRTUAL_HEADERS], [], None
        for row_offset, source_row in enumerate(cleaned[header_index + 1:], start=header_index + 2):
            if self._is_totals_or_noise_row(source_row):
                break
            values = {name: None for name in VIRTUAL_HEADERS}
            field_meta = {}
            for index, cell in enumerate(source_row):
                field = header_map.get(index)
                if not field or not cell:
                    continue
                values[field] = cell
                field_meta[field] = {
                    "confidence": header_confidence.get(index, 0),
                    "source": {"type": "table_cell", **source, "row": row_offset, "column": index + 1},
                }
            if header_currency and not values.get("currency"):
                values["currency"] = header_currency
                field_meta["currency"] = {"confidence": 90, "source": {"type": "table_header", **source}}
            if not values.get("product_name"):
                fallback_description = self._description_from_ignored_cells(source_row, header_map)
                if fallback_description:
                    values["product_name"] = fallback_description
                    field_meta["product_name"] = {"confidence": 78, "source": {"type": "table_ignored_text", **source, "row": row_offset}}
            values = self._repair_price_columns(values)
            has_numbers = any(values.get(field) for field in ("quantity", "unit_price", "line_total"))
            has_description = any(values.get(field) for field in ("product_name", "specification", "material"))
            if has_description and not has_numbers and isinstance(pending, tuple) and len(pending) == 2:
                pending, pending_meta = pending
                for field in ("product_name", "specification", "material"):
                    if values.get(field):
                        pending[field] = " ".join([str(pending.get(field) or "").strip(), str(values[field]).strip()]).strip()
                        pending_meta[field] = field_meta.get(field, pending_meta.get(field))
                continue
            if not has_description and has_numbers and isinstance(pending, tuple) and len(pending) == 2:
                pending, pending_meta = pending
                for field, value in values.items():
                    if value not in (None, ""):
                        pending[field] = value
                        pending_meta[field] = field_meta.get(field, pending_meta.get(field))
                values, field_meta, pending = pending, pending_meta, None
            elif pending and not (isinstance(pending, tuple) and len(pending) == 2):
                pending = None
            elif has_description and not has_numbers:
                pending = (values, field_meta)
                continue
            description = values.get("product_name") or values.get("specification") or values.get("material")
            if not description:
                continue
            row_type = self._row_type(description)
            if row_type == "UNKNOWN" and not any(values.get(field) for field in ("product_name", "item_code", "part_number", "unit_price", "line_total")):
                continue
            values["row_type"] = row_type
            field_meta["row_type"] = {"confidence": 90 if row_type != "UNKNOWN" else 40, "source": {"type": "classifier", **source, "row": row_offset}}
            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append(field_meta)
        return (rows, row_meta) if len(rows) > 1 else None


    def _description_from_ignored_cells(self, row, header_map):
        ignored_text = []
        for index, cell in enumerate(row):
            if header_map.get(index):
                continue
            value = str(cell or "").strip()
            if not value or re.fullmatch(r"-+", value):
                continue
            if re.fullmatch(r"0*\d{1,4}", value):
                continue
            if _decimal(value) is not None or self._currency_from_text(value):
                continue
            cleaned = re.sub(r"(?<=[A-Z])(?=Certificate\b)", " ", value)
            cleaned = cleaned.strip(" :-")
            if cleaned and any(ch.isalpha() for ch in cleaned):
                ignored_text.append(cleaned)
        description = " ".join(ignored_text).strip()
        return description or None

    def _is_totals_or_noise_row(self, row):
        normalized = _norm(" ".join(str(cell or "") for cell in row))
        if any(term in normalized for term in TOTAL_STOP_TERMS):
            return True
        if any(term in normalized for term in NOISE_TERMS):
            return True
        return False

    def _find_line_header(self, rows):
        best = (None, {}, {}, {}, 0)
        for row_index, row in enumerate(rows[:8]):
            sample_rows = rows[row_index + 1: row_index + 8]
            header_map, confidence, currencies = {}, {}, {}
            used_fields = set()
            for index, cell in enumerate(row):
                sample_values = [sample[index] if index < len(sample) else None for sample in sample_rows]
                if self._is_serial_header(cell) or self._is_serial_sample(sample_values):
                    continue
                field, header_score = _match_field(cell, LINE_SCHEMA)
                currency = self._currency_from_text(cell)
                sample_score = self._sample_score(sample_values, field)
                score = int((header_score * 0.7) + (sample_score * 0.3)) if field else 0
                if field and score >= 62 and field not in used_fields:
                    header_map[index] = field
                    confidence[index] = score
                    used_fields.add(field)
                    if currency:
                        currencies[field] = currency
            line_score = len(set(header_map.values())) * 22 + sum(confidence.values()) / 25
            metadata_hits = sum(1 for cell in row if _norm(cell) in DOCUMENT_LABELS)
            if metadata_hits >= 2:
                line_score -= metadata_hits * 25
            if line_score > best[4]:
                best = (row_index, header_map, confidence, currencies, line_score)
        required_signal = {"product_name", "quantity", "unit_price", "line_total"}
        if best[4] >= 58 and len(required_signal.intersection(best[1].values())) >= 3:
            header_currency = best[3].get("unit_price") or best[3].get("line_total") or best[3].get("currency")
            return best[0], best[1], best[2], header_currency
        return None, {}, {}, None

    def _is_serial_header(self, value):
        return _norm(value) in {"s no", "sl no", "sr no", "serial no", "serial number", "no", "#", "item", "item no"}

    def _is_serial_sample(self, samples):
        values = [str(value).strip() for value in samples if value not in (None, "")]
        if len(values) < 2:
            return False
        numeric = []
        for value in values[:6]:
            if not re.fullmatch(r"0*\d+", value):
                return False
            numeric.append(int(value))
        return numeric == list(range(numeric[0], numeric[0] + len(numeric)))

    def _strip_leading_serial(self, line):
        text = str(line or "").strip()

        # Do not strip real size prefixes: "10 mm OD...", "38 mm Tube..."
        if re.match(r"^\s*\d+(?:\.\d+)?\s*(?:mm|cm|mtr|meter|inch|in\b|od\b)\b", text, re.I):
            return text

        if re.match(r"^\s*\d+(?:\.\d+)?\s+(?:nos?|pcs?|pc|mtr|m|kg|lot|set|pair|roll|sheet|ea|ltr)\b", text, re.I):
            return text

        return re.sub(
            r"^\s*0*\d{1,4}\s+(?=(?:[A-Za-z]|\d+[A-Za-z]))",
            "",
            text,
        ).strip()
    def _currency_from_text(self, value):
        text = str(value or "")
        normalized = _norm(text)

        if "uae dirham" in normalized or "uae dirhams" in normalized or "dirham" in normalized or "dirhams" in normalized:
            return "AED"
        if "us dollar" in normalized or "usdollar" in normalized:
            return "USD"

        match = re.search(r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b|[$\u20ac\u00a3\u00a5]", text, re.I)
        if not match:
            return None
        token = match.group(0).upper()
        return {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP", "\u00a5": "JPY"}.get(token, token)

    def _sample_score(self, samples, field):
        values = [str(value).strip() for value in samples if value not in (None, "")]
        if not values or not field:
            return 0
        numeric_ratio = sum(1 for value in values if _decimal(value) is not None) / len(values)
        text_ratio = sum(1 for value in values if len(_norm(value)) >= 4 and _decimal(value) is None) / len(values)
        uom_ratio = sum(
            1
            for value in values
            if re.fullmatch(
                r"(?i)"
                r"no|nos|pc|pcs|ea|each|"
                r"m|mtr|meter|metre|mm|cm|"
                r"kg|gm|g|ltr|l|litre|"
                r"lot|set|pair|roll|sheet|"
                r"tin|box|pack|pkt|drum|bag|coil|ls",
                str(value).strip(),
            )
        ) / len(values)
        currency_ratio = sum(1 for value in values if self._currency_from_text(value)) / len(values)
        sequential = all(str(index + 1) == value for index, value in enumerate(values[:5]))
        if field == "quantity":
            return 30 if sequential else int(numeric_ratio * 100)
        if field in {"unit_price", "line_total", "tax", "tax_amount"}:
            return int(numeric_ratio * 100)
        if field == "unit_of_measurement":
            return int(uom_ratio * 100)
        if field == "currency":
            return int(currency_ratio * 100)
        if field in {"product_name", "specification", "material"}:
            return int(text_ratio * 100)
        if field == "item_code" and sequential:
            return 0
        return int(max(text_ratio, numeric_ratio) * 70)

    def _repair_price_columns(self, values):
        product_text = _norm(values.get("product_name") or "")
        if (
            product_text
            and (
                re.fullmatch(r"\d+\s+of\s+\d+", product_text)
                or re.search(r"\b(?:quote total|tax value|in words|page \d+ of \d+|vat @|vat value)\b", product_text)
            )
        ):
            values["row_type"] = "CHARGE"
            values["product_name"] = ""
            values["unit_price"] = None
            values["line_total"] = None
            return values
        def normalize_amount_text(raw_value):
            raw_text = str(raw_value or "").strip()
            if not raw_text:
                return raw_value

            cleaned = re.sub(r"[^\d.,-]", "", raw_text)

            # European thousands style like 2.000,00
            if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{2,5}", cleaned):
                return cleaned.replace(".", "").replace(",", ".")

            if cleaned and cleaned != raw_text:
                return cleaned

            return raw_value

        def tolerance(amount):
            return max(Decimal("0.05"), abs(amount) * Decimal("0.02"))

        raw_amount_values = {field: values.get(field) for field in ("unit_price", "line_total", "tax_amount")}
        for field in ("unit_price", "line_total", "tax_amount"):
            raw_amount_value = values.get(field)
            currency = self._currency_from_text(raw_amount_value)
            values[field] = normalize_amount_text(raw_amount_value)
            if values.get(field) not in (None, ""):
                number = _decimal(values.get(field))
                if currency and not values.get("currency"):
                    values["currency"] = currency
                if number is not None:
                    values[field] = str(number)

        quantity_with_unit = re.match(
            r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>pcs?|nos?|no|sets?|set|ea|each|unit|lot|mtr|meter|kg|ltr|l|roll|tin)\s*$",
            str(values.get("quantity") or ""),
            re.I,
        )
        if quantity_with_unit:
            values["quantity"] = quantity_with_unit.group("qty")
            if not values.get("unit_of_measurement"):
                values["unit_of_measurement"] = quantity_with_unit.group("unit").upper()

        quantity = _decimal(values.get("quantity"))
        unit_price = _decimal(values.get("unit_price"))
        line_total = _decimal(values.get("line_total"))
        uom_as_price = _decimal(values.get("unit_of_measurement"))

        if quantity and unit_price is not None and line_total is not None:
            expected = quantity * unit_price
            if abs(expected - line_total) > tolerance(line_total):
                comma_unit_price = _decimal_comma_decimal(raw_amount_values.get("unit_price"))
                comma_line_total = _decimal_comma_decimal(raw_amount_values.get("line_total"))
                if comma_unit_price is not None and comma_line_total is not None:
                    comma_expected = quantity * comma_unit_price
                    if abs(comma_expected - comma_line_total) <= tolerance(comma_line_total):
                        values["unit_price"] = str(comma_unit_price)
                        values["line_total"] = str(comma_line_total)
                        unit_price = comma_unit_price
                        line_total = comma_line_total

        if quantity and uom_as_price and unit_price and not line_total:
            if abs((quantity * uom_as_price) - unit_price) <= tolerance(unit_price):
                values["line_total"] = values.get("unit_price")
                values["unit_price"] = str(uom_as_price)
                values["unit_of_measurement"] = None
                line_total = unit_price
                unit_price = uom_as_price
        # Repair rows where VAT percent/value was mapped as unit price and
        # the actual unit rate was mapped into line_total.
        # Example: qty=200, unit_price=5, line_total=0.20 should become
        # unit_price=0.20, line_total=40.00.
        if (
            quantity
            and quantity > 1
            and unit_price is not None
            and line_total is not None
            and line_total > 0
            and unit_price > line_total
        ):
            recalculated_total = quantity * line_total
            if recalculated_total > unit_price:
                values["unit_price"] = str(line_total)
                values["line_total"] = str(recalculated_total)
                return values
        if quantity and unit_price and line_total:
            if abs((quantity * unit_price) - line_total) <= tolerance(line_total):
                return values

            # Sometimes table text loses decimal point: 85000 should be 0.85000
            # only when row total mathematically proves it.
            raw_unit = str(values.get("unit_price") or "").strip()
            compact_unit = re.sub(r"[^\d-]", "", raw_unit)
            if re.fullmatch(r"-?\d{5,}", compact_unit):
                for scale in (
                    Decimal("10"),
                    Decimal("100"),
                    Decimal("1000"),
                    Decimal("10000"),
                    Decimal("100000"),
                ):
                    repaired_unit = Decimal(compact_unit) / scale
                    if abs((quantity * repaired_unit) - line_total) <= tolerance(line_total):
                        values["unit_price"] = str(repaired_unit)
                        return values

            if unit_price > 0 and abs((quantity * line_total) - unit_price) <= tolerance(unit_price):
                values["unit_price"], values["line_total"] = values["line_total"], values["unit_price"]

        return values
    def _virtual_from_labeled_text(self, text, sheet_name, allow_empty=False):
        fields, field_meta = self._document_fields_from_text(text)
        converted = self._lines_to_table_items(text)
        if converted:
            rows, row_meta = converted
            self._apply_document_currency(rows, row_meta, fields)
        else:
            rows, row_meta = [VIRTUAL_HEADERS], []
        if len(rows) == 1 and not allow_empty:
            raise ImportServiceError(
                "Needs Review",
                {"file": ["No reliable line-item table was detected. Header, totals and terms were not converted into products."]},
            )
        return {
            "sheets": [{"sheet_name": sheet_name, "sheet_index": 0, "rows": rows, "is_virtual": True, "row_meta": row_meta}],
            "document_fields": fields,
            "document_field_meta": field_meta,
            "extracted_text": text,
            "extraction_summary": {"method": "text_table_region" if len(rows) > 1 else "needs_review_no_table"},
        }
    def _calgaz_stock_quote_to_items(self, text, fields):
        header_norm = _norm(text)
        if "stockid description part no quantity price discount total" not in header_norm:
            return None

        currency = fields.get("currency") or self._currency_from_text(text) or "USD"
        supplier_match = re.search(
            r"^\s*(?P<supplier>[A-Z][A-Z0-9&.,'() /-]{2,}?\s+(?:LLC|L\.L\.C|LTD|LIMITED|FZE|FZCO|CO\.?|COMPANY|INC|GMBH|A\.S\.|AŞ))\b",
            str(text or ""),
            re.I | re.M,
        )
        if supplier_match:
            supplier_name = re.sub(r"\s+", " ", supplier_match.group("supplier")).strip(" :,-")
            if self._is_valid_document_supplier(supplier_name):
                fields["supplier"] = supplier_name.title().replace("Llc", "LLC").replace("L.l.c", "L.L.C")
                fields["supplier_name"] = fields["supplier"]

        rows = [VIRTUAL_HEADERS]
        row_meta = [{}]

        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        item_pattern = re.compile(
            r"^(?P<stock>[A-Z0-9][A-Z0-9-]{2,})\s+"
            r"(?P<body>.+?)\s+"
            r"(?P<part>[A-Z0-9][A-Z0-9._-]{3,})\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<price>\d[\d,]*(?:\.\d+)?)\s+"
            r"(?P<discount>\d[\d,]*(?:\.\d+)?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d+)?)$",
            re.I,
        )

        stop_terms = {
            "please send all",
            "cheques should",
            "for electronic payments",
            "swift code",
            "account #",
            "deliver to",
            "goods total",
            "quotation valid",
            "terms and conditions",
            "registered in singapore",
            "calgaz international llc terms",
            "for self collection",
            "self collection",
            "calgaz warehouse address",
            "warehouse address",
            "pre arrangement",
            "geylang bahru",
            "kallang distripark",
            "please send all",
            
        }

        pending = None

        def clean_desc(value):
            value = re.sub(r"\bSDS\s+\d+(?:\s*/\s*\d+)?\b", "", value, flags=re.I)
            value = re.sub(r"\bSTOCK\s+\d+\b", "", value, flags=re.I)
            value = re.sub(r"\bA\d{6,}\b", "", value, flags=re.I)
            value = re.sub(r"\*\*\s+", "** ", value)
            value = re.sub(r"\*\*+\s*$", "**", value)
            value = re.sub(r"\*\*\s+\*\*", "**", value)
            value = re.sub(r"\s+\*\*$", "**", value)
            value = re.sub(r"\s+", " ", value).strip(" -")
            return value

        def flush():
            nonlocal pending
            if not pending:
                return

            description_parts = [pending["description"]]
            for extra in pending["extra"]:
                norm_extra = _norm(extra)
                if any(term in norm_extra for term in stop_terms):
                    break
                if norm_extra.startswith("for self collection") or norm_extra.startswith("calgaz warehouse"):
                    break
                cleaned_extra = clean_desc(extra)
                if cleaned_extra and re.search(r"[A-Za-z0-9]", cleaned_extra):
                    description_parts.append(cleaned_extra)

            description = clean_desc(" ".join(description_parts))
            part_number = pending["part"]

            product_name = f"{description} | {part_number}" if description and part_number else description or part_number
            if not product_name:
                pending = None
                return

            row_type = "CHARGE" if (
                pending["stock"].upper().startswith("CA-")
                or "incoterm" in _norm(product_name)
                or "collection" in _norm(product_name)
            ) else "PRODUCT"

            values = {
                "row_type": row_type,
                "item_code": pending["stock"],
                "part_number": part_number,
                "product_name": product_name,
                "quantity": pending["qty"],
                "unit_of_measurement": "",
                "unit_price": pending["price"].replace(",", ""),
                "currency": currency,
                "line_total": pending["total"].replace(",", ""),
            }

            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {
                    "confidence": 88,
                    "source": {"type": "calgaz_stock_quote", "line": pending["line"]},
                }
                for field, value in values.items()
                if value not in (None, "")
            })
            pending = None

        for line_no, line in enumerate(lines, start=1):
            norm_line = _norm(line)
            if "stockid description part no quantity price discount total" in norm_line:
                continue

            match = item_pattern.match(line)
            if match:
                flush()
                description = match.group("body")
                part_number = match.group("part")
                if part_number and not re.search(r"\d", part_number):
                    description = f"{description} {part_number}"
                    part_number = ""

                pending = {
                    "line": line_no,
                    "stock": match.group("stock"),
                    "description": description,
                    "part": part_number,
                    "qty": match.group("qty"),
                    "price": match.group("price"),
                    "total": match.group("total"),
                    "extra": [],
                }
                continue

            if pending:
                pending["extra"].append(line)

        flush()

        return (rows, row_meta) if len(rows) > 1 else None
    def _crowcon_commercial_invoice_to_items(self, text, fields):
        normalized_text = _norm(text)
        if not (
            "commercial invoice continuation" in normalized_text
            and "item hs code description quantity unit price amount" in normalized_text
        ):
            return None

        currency = fields.get("currency") or self._currency_from_text(text) or "USD"
        rows = [VIRTUAL_HEADERS]
        row_meta = [{}]

        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        inline_item_pattern = re.compile(
            r"^(?P<item>\d{1,3})\s+"
            r"(?P<hs>[A-Z0-9]{2,10})\s+"
            r"(?P<description>.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d+)?)\s+"
            r"(?P<amount>\d[\d,]*(?:\.\d+)?)$",
            re.I,
        )
        ref_pattern = re.compile(
            r"^(?P<item>\d{1,3})\s+"
            r"(?P<hs>[A-Z0-9]{2,10})\s+"
            r"REF:\s*(?P<ref>.+)$",
            re.I,
        )
        priced_description_pattern = re.compile(
            r"^(?:(?:GB|UK|US)\s+)?"
            r"(?P<description>[A-Z0-9][A-Z0-9._/-]*\s+-\s+.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d+)?)\s+"
            r"(?P<amount>\d[\d,]*(?:\.\d+)?)$",
            re.I,
        )

        stop_terms = {
            "end of document",
            "page ",
            "commercial invoice",
            "seller name",
            "invoice number",
            "invoice date",
            "buyer reference",
            "consignee vat",
            "buyer vat",
            "payment should",
            "swift code",
            "sort code",
            "account no",
            "iban no",
            "name of signatory",
            "place and date",
            "signature",
        }

        pending = None

        def clean_description(value):
            value = re.sub(r"\bREF:\s*[^A-Z]*(?=[A-Z0-9-]+\s+-)", "", value, flags=re.I)
            value = re.sub(r"\bREF:\s*\d+(?:\s*/\s*[\w-]+)+", "", value, flags=re.I)
            value = re.sub(r"\s+", " ", value).strip(" -")
            return value

        def flush():
            nonlocal pending
            if not pending:
                return

            description_parts = [pending["description"]]
            for extra in pending["extra"]:
                norm_extra = _norm(extra)
                if any(term in norm_extra for term in stop_terms):
                    break
                if re.fullmatch(r"piece(?:\s+piece)?", norm_extra, re.I):
                    continue
                description_parts.append(extra)

            product_name = clean_description(" ".join(description_parts))
            if (
                not product_name
                or pending.get("quantity") in (None, "")
                or pending.get("unit_price") in (None, "")
                or pending.get("amount") in (None, "")
            ):
                pending = None
                return

            row_type = "CHARGE" if any(
                term in _norm(product_name)
                for term in {"freight", "shipping", "air freight", "forwarder"}
            ) else "PRODUCT"

            values = {
                "row_type": row_type,
                "item_code": pending["hs_code"],
                "product_name": self._clean_product_description(product_name),
                "quantity": pending["quantity"],
                "unit_of_measurement": "Piece",
                "unit_price": pending["unit_price"].replace(",", ""),
                "currency": currency,
                "line_total": pending["amount"].replace(",", ""),
            }

            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {
                    "confidence": 90,
                    "source": {"type": "commercial_invoice_text_items", "line": pending["line"]},
                }
                for field, value in values.items()
                if value not in (None, "")
            })
            pending = None

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)
            if "item hs code description quantity unit price amount" in normalized:
                continue
            if any(term in normalized for term in stop_terms):
                if pending:
                    flush()
                continue

            ref_match = ref_pattern.match(line)
            if ref_match:
                flush()
                pending = {
                    "line": line_no,
                    "hs_code": ref_match.group("hs"),
                    "description": "",
                    "quantity": None,
                    "unit_price": None,
                    "amount": None,
                    "extra": [],
                }
                continue

            priced_match = priced_description_pattern.match(line)
            if priced_match and pending and not pending.get("description"):
                pending["description"] = priced_match.group("description")
                pending["quantity"] = priced_match.group("qty")
                pending["unit_price"] = priced_match.group("unit_price")
                pending["amount"] = priced_match.group("amount")
                continue

            match = inline_item_pattern.match(line)
            if match:
                flush()
                pending = {
                    "line": line_no,
                    "hs_code": match.group("hs"),
                    "description": match.group("description"),
                    "quantity": match.group("qty"),
                    "unit_price": match.group("unit_price"),
                    "amount": match.group("amount"),
                    "extra": [],
                }
                continue

            if pending:
                pending["extra"].append(line)

        flush()

        return (rows, row_meta) if len(rows) > 1 else None
    def _multiline_sales_quote_to_items(self, text, fields):
        rows, row_meta = [VIRTUAL_HEADERS], []
        lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
        currency = fields.get("currency") or self._currency_from_text(text) or ""

        item_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+"
            r"(?P<pn>[A-Z0-9][A-Z0-9-]{2,})\s+"
            r"(?P<model>[A-Z0-9][A-Z0-9/._-]{2,})\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>PCS|PC|EA|EACH|NOS|NO|UNIT|SET)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<discount>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<taxable>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<vat_percent>\d+(?:\.\d+)?)\s+"
            r"(?P<vat_amount>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )

        stop_terms = {
            "customer", "offer details", "quotation no", "p o no",
            "document date", "quote validity", "prepared by",
            "sales quotation", "sl p n", "vat total", "discount amount",
            "this is a computer generated", "page ", "total ",
            "vat ", "net amount", "bank details", "sales terms",
            "delivery:", "sharjah", "united arab emirates",
            "contact person", "contact number", "contact email",
            "email", "tel", "trn", "incoterms", "payment terms",
            "ex warehouse", "works dubai", "collection",
        }

        pending = None

        def flush_pending():
            if not pending:
                return

            detail_lines = []
            for value in pending["details"]:
                normalized = _norm(value)
                if any(term in normalized for term in stop_terms):
                    continue
                if re.fullmatch(r"[A-Z]$", value.strip()):
                    continue
                if any(term in normalized for term in {
                    "100 atx certified", "100 atex certified", "liquid chambers",
                    "center block", "diaphragms", "valve seats", "inlet",
                    "air inlet", "max", "maximum", "weight", "dimensions",
                    "mfr", "assembled", "warranty", "https", "optional",
                }):
                    break
                detail_lines.append(value)

            light_description = " ".join(detail_lines).strip()
            product_name = pending["part_number"]
            if pending["model"]:
                product_name = f"{product_name} | {pending['model']}"
            if light_description:
                product_name = f"{product_name} | {light_description}"

            values = {
                "row_type": "PRODUCT",
                "item_code": pending["part_number"],
                "part_number": pending["part_number"],
                "product_name": self._clean_product_description(product_name),
                "quantity": pending["quantity"],
                "unit_of_measurement": pending["uom"],
                "unit_price": pending["rate"],
                "currency": currency,
                "tax": pending["vat_percent"],
                "tax_amount": pending["vat_amount"],
                "line_total": pending["taxable"],
            }

            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {"confidence": 88, "source": {"type": "multiline_sales_quote", "line": pending["line"]}}
                for field, value in values.items()
                if value not in (None, "")
            })

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            if any(term in normalized for term in stop_terms):
                continue

            item_match = item_pattern.match(line)
            if item_match:
                flush_pending()
                pending = {
                    "line": line_no,
                    "part_number": item_match.group("pn"),
                    "model": item_match.group("model"),
                    "quantity": item_match.group("qty"),
                    "uom": item_match.group("uom").upper(),
                    "rate": item_match.group("rate").replace(",", ""),
                    "taxable": item_match.group("taxable").replace(",", ""),
                    "vat_percent": item_match.group("vat_percent"),
                    "vat_amount": item_match.group("vat_amount").replace(",", ""),
                    "total": item_match.group("total").replace(",", ""),
                    "details": [],
                }
                continue

            if pending:
                if re.match(r"^\d{1,3}\s+[A-Z0-9][A-Z0-9-]{2,}\s+", line, re.I):
                    flush_pending()
                    pending = None
                    continue

                if not any(term in normalized for term in stop_terms):
                    pending["details"].append(line)

        flush_pending()

        return (rows, row_meta) if len(rows) > 1 else None
    def _prose_unit_price_offer_to_items(self, text, fields):
        rows, row_meta = [VIRTUAL_HEADERS], []
        lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
        currency = fields.get("currency") or self._currency_from_text(text) or ""

        item_pattern = re.compile(r"^(?P<sl>\d{1,3})[.)]\s+(?P<description>.+)$", re.I)
        price_pattern = re.compile(
            r"\bunit\s+price\s*(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD)?\.?\s*"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s*/?-?\s*(?P<uom>each|pc|pcs|no|nos|ea|unit|set)?",
            re.I,
        )

        pending = None

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            if normalized.startswith(("from ", "sent ", "to ", "cc ", "subject ", "attachments ", "categories ", "dear ")):
                continue

            if any(term in normalized for term in {
                "best regards", "kind regards", "payment", "inco term",
                "disclaimer", "phone", "mobile", "tel", "email", "web",
            }):
                pending = None
                continue

            item_match = item_pattern.match(line)
            if item_match:
                description = self._clean_product_description(item_match.group("description"))
                pending = {"description": description, "line": line_no}
                continue

            price_match = price_pattern.search(line)
            if price_match and pending:
                raw_description = pending["description"]
                part_number = None
                description = raw_description

                part_match = re.search(r"\s+-\s+([A-Z0-9][A-Z0-9._/-]{2,})\s*$", raw_description, re.I)
                if part_match:
                    part_number = part_match.group(1).strip()
                    description = raw_description[:part_match.start()].strip(" -")

                rate = price_match.group("rate").replace(",", "")
                uom = (price_match.group("uom") or "EACH").upper()

                product_name = (
                    f"{part_number} | {description}"
                    if part_number and description
                    else raw_description
                )

                values = {
                    "row_type": "PRODUCT",
                    "item_code": part_number,
                    "part_number": part_number,
                    "product_name": product_name,
                    "quantity": "1",
                    "unit_of_measurement": uom,
                    "unit_price": rate,
                    "currency": (price_match.group("currency") or currency or "").upper(),
                    "line_total": rate,
                }

                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 86, "source": {"type": "prose_unit_price_offer", "line": pending["line"]}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending = None

        return (rows, row_meta) if len(rows) > 1 else None
    def _email_offer_text_to_items(self, text, fields):
        rows, row_meta = [VIRTUAL_HEADERS], []
        lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
        currency = fields.get("currency") or self._currency_from_text(text) or ""

        code_pattern = re.compile(r"^[A-Z0-9][A-Z0-9-]{5,}$", re.I)
        price_pattern = re.compile(
            r"@\s*(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD)?\s*"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s*/\s*(?P<uom>pc|pcs|no|nos|ea|each|unit|set)",
            re.I,
        )

        pending_code = None
        pending_desc = []

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            if normalized.startswith(("from ", "sent ", "to ", "cc ", "subject ", "dear ")):
                continue

            if any(term in normalized for term in {
                "other commercial terms", "delivery period", "delivery terms",
                "payment terms", "validity", "warranty", "price and delivery",
                "await your reply", "kind regards", "disclaimer",
            }):
                pending_code = None
                pending_desc = []
                continue

            if code_pattern.fullmatch(line):
                pending_code = line
                pending_desc = []
                continue

            price_match = price_pattern.search(line)
            if price_match and pending_code:
                rate = price_match.group("rate").replace(",", "")
                description_text = self._clean_product_description(" ".join(pending_desc) or pending_code)
                description = (
                    f"{pending_code} | {description_text}"
                    if description_text and description_text != pending_code
                    else pending_code
                )
                values = {
                    "row_type": "PRODUCT",
                    "item_code": pending_code,
                    "part_number": pending_code,
                    "product_name": description,
                    "quantity": "1",
                    "unit_of_measurement": (price_match.group("uom") or "pc").upper(),
                    "unit_price": rate,
                    "currency": (price_match.group("currency") or currency or "").upper(),
                    "line_total": rate,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 88, "source": {"type": "email_offer_price_line", "line": line_no}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_code = None
                pending_desc = []
                continue

            if pending_code and line and not price_pattern.search(line):
                pending_desc.append(line)

        return (rows, row_meta) if len(rows) > 1 else None

    def _mooring_email_price_table_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if (
                    "equipment description" in _norm(line)
                    and "swl" in _norm(line)
                    and "unit price" in _norm(line)
                    and "total price" in _norm(line)
                )
            ),
            None,
        )
        if header_index is None:
            return None

        row_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+"
            r"(?P<description>.+?)\s+"
            r"(?P<swl>\d+(?:\.\d+)?)\s+"
            r"(?P<quantity>\d+(?:\.\d+)?)\s+"
            r"(?P<standard>ISO\s*\d+)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )
        charge_pattern = re.compile(
            r"^(?P<label>[A-Z]{2,}\s+Class\s+cert|Class\s+cert(?:ificate)?(?:\s+Fee)?)\s+"
            r"(?P<amount>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        in_price_table = False

        for line_no, line in enumerate(lines[header_index + 1:], start=header_index + 2):
            normalized = _norm(line)
            if normalized in {"p usd usd"}:
                in_price_table = True
                continue

            match = row_pattern.match(line)
            if match:
                in_price_table = True
                description = self._clean_product_description(match.group("description"))
                
                standard = re.sub(r"\s+", " ", match.group("standard").upper()).strip()
                swl = match.group("swl")
                product_name = self._clean_product_description(
                    f"{description} | {standard}"
                )
                unit_price = match.group("unit_price").replace(",", "")
                line_total = match.group("line_total").replace(",", "")

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_name,
                    "quantity": match.group("quantity"),
                    "unit_of_measurement": None,
                    "unit_price": unit_price,
                    "currency": fields.get("currency") or "USD",
                    "line_total": line_total,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {
                        "confidence": 94,
                        "source": {"type": "mooring_email_price_table", "line": line_no},
                    }
                    for field, value in values.items()
                    if value not in (None, "")
                })
                continue

            if not in_price_table:
                continue

            if normalized.startswith("total "):
                continue

            charge_match = charge_pattern.match(line)
            if charge_match:
                label = self._clean_product_description(charge_match.group("label"))
                amount = charge_match.group("amount").replace(",", "")
                values = {
                    "row_type": "CHARGE",
                    "product_name": label,
                    "quantity": "1",
                    "unit_of_measurement": None,
                    "unit_price": amount,
                    "currency": fields.get("currency") or "USD",
                    "line_total": amount,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {
                        "confidence": 90,
                        "source": {"type": "mooring_email_price_table_charge", "line": line_no},
                    }
                    for field, value in values.items()
                    if value not in (None, "")
                })
                continue

            if normalized.startswith(("purchase requisition", "line no", "description", "dear")):
                break

        product_count = sum(1 for row in rows[1:] if row[0] == "PRODUCT")
        return (rows, row_meta) if product_count >= 2 else None

    def _email_product_price_table_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any(
            "product quantity unit price total price" in _norm(" ".join(lines[index:index + 3]))
            for index in range(len(lines))
        ):
            return None

        row_pattern = re.compile(
            r"^(?P<no>\d{1,3})\s+"
            r"(?P<description>.*?)\s*"
            r"(?P<quantity>\d+(?:\.\d+)?)\s+"
            r"(?:€|EUR|Â€)\s*"
            r"(?P<unit_price>\d[\d,.]*)\s+"
            r"(?:€|EUR|Â€)\s*"
            r"(?P<line_total>\d[\d,.]*)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = []
        in_table = False
        index = 0

        while index < len(lines):
            line = lines[index]
            normalized = _norm(line)

            if "product quantity unit price total price" in _norm(" ".join(lines[index:index + 3])):
                in_table = True
                pending_description = []
                index += 1
                continue

            if not in_table:
                index += 1
                continue

            if normalized.startswith(("delivery time", "delivery place", "payment terms", "validity", "from ", "sent ", "to ", "cc ", "subject ")):
                break
            if normalized.startswith(("certificate", "manufacturer certificate", "additional cost", "welded", "swl to be marked")):
                pending_description = []
                index += 1
                continue
            if normalized in {"no"}:
                index += 1
                continue

            match = row_pattern.match(line)
            if match:
                description_parts = [*pending_description]
                inline_description = self._clean_product_description(match.group("description"))
                if inline_description:
                    description_parts.append(inline_description)

                lookahead = index + 1
                while lookahead < len(lines):
                    next_line = lines[lookahead]
                    next_norm = _norm(next_line)
                    if row_pattern.match(next_line):
                        break
                    if next_norm.startswith(("delivery time", "delivery place", "payment terms", "validity", "from ", "sent ", "to ", "cc ", "subject ")):
                        break
                    if next_norm.startswith(("certificate", "manufacturer certificate", "additional cost", "welded", "swl to be marked")):
                        break
                    if re.match(r"^H\s*=", next_line, re.I):
                        description_parts.append(next_line)
                        lookahead += 1
                        continue
                    break

                description = self._clean_product_description(" ".join(description_parts))
                if not description:
                    index += 1
                    pending_description = []
                    continue

                unit_price = self._normalize_euro_amount(match.group("unit_price"))
                line_total = self._normalize_euro_amount(match.group("line_total"))
                values = {
                    "row_type": "PRODUCT",
                    "product_name": description,
                    "quantity": match.group("quantity"),
                    "unit_of_measurement": None,
                    "unit_price": unit_price,
                    "currency": fields.get("currency") or "EUR",
                    "line_total": line_total,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {
                        "confidence": 92,
                        "source": {"type": "email_product_price_table", "line": index + 1},
                    }
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = []
                index = lookahead
                continue

            if re.search(r"\b(?:fairlead|roller|bollard|chock|seat|pedastal|pedestal)\b", line, re.I):
                pending_description = [line]

            index += 1

        return (rows, row_meta) if len(rows) > 1 else None

    def _normalize_euro_amount(self, value):
        raw = str(value or "").strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            return raw.replace(".", "")
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,4}", raw):
            return raw.replace(".", "").replace(",", ".")
        return raw.replace(",", "")

    def _pricing_list_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any("maritime pricing" in _norm(line) or "spares pricing" in _norm(line) for line in lines):
            return None

        row_pattern = re.compile(
            r"^(?P<code>[A-Z0-9][A-Z0-9-]{2,})\s+"
            r"(?P<description>.+?)\s+"
            r"(?:£|Â£|GBP)\s*(?P<price>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        last_row_index = None
        last_meta_index = None

        for line_no, line in enumerate(lines, start=1):
            match = row_pattern.match(line)

            if match:
                description = self._clean_product_description(match.group("description"))
                if not description or any(term in _norm(description) for term in {"part code", "description", "contact sales"}):
                    continue

                code = match.group("code").strip()
                price = match.group("price").replace(",", "")

                values = {
                    "row_type": "PRODUCT",
                    "product_name": f"{description} | {code}",
                    "item_code": code,
                    "part_number": code,
                    "quantity": "1",
                    "unit_of_measurement": None,
                    "unit_price": price,
                    "currency": "GBP",
                    "line_total": price,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 86, "source": {"type": "pricing_list", "line": line_no}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                last_row_index = len(rows) - 1
                last_meta_index = len(row_meta) - 1
                continue

            if last_row_index is not None and re.match(r"^[-–]\s+", line):
                product_index = VIRTUAL_HEADERS.index("product_name")
                continuation = self._clean_product_description(line)
                continuation = re.sub(r"^[-–]\s*", "", continuation).strip()
                if continuation:
                    current_name = rows[last_row_index][product_index] or ""
                    if " | " in current_name:
                        description, code = current_name.rsplit(" | ", 1)
                        rows[last_row_index][product_index] = f"{description} {continuation} | {code}"
                    else:
                        rows[last_row_index][product_index] = f"{current_name} {continuation}".strip()

                    if last_meta_index is not None:
                        row_meta[last_meta_index]["product_name"] = {
                            "confidence": 86,
                            "source": {"type": "pricing_list_continuation", "line": line_no},
                        }

        return (rows, row_meta) if len(rows) > 1 else None
    def _model_spec_quote_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any("model specifications" in _norm(line) for line in lines):
            return None
        if not any("unit price" in _norm(line) and "total price" in _norm(line) for line in lines):
            return None

        row_pattern = re.compile(
            r"^(?P<no>\d{1,3})\s+"
            r"(?P<uom>EACH|PCS|PC|NOS|NO|SET|UNIT)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = []
        index = 0

        while index < len(lines):
            line = lines[index]
            normalized = _norm(line)

            if normalized.startswith(("no model", "lig marine", "address", "web site", "tel", "email", "attn", "quotation")):
                index += 1
                continue
            if normalized.startswith(("total ", "delivery", "payment", "validity", "note", "for any", "best regards")):
                pending_description = []
                index += 1
                continue

            match = row_pattern.match(line)
            if match and pending_description:
                description_parts = list(pending_description)

                lookahead = index + 1
                if lookahead < len(lines):
                    next_line = lines[lookahead]
                    next_norm = _norm(next_line)
                    if (
                        re.search(r"\b(?:phase|hz)\b", next_line, re.I)
                        and not row_pattern.match(next_line)
                        and not next_norm.startswith(("total ", "delivery", "payment", "validity", "note", "for any", "best regards"))
                    ):
                        next_line = re.sub(
                            r"^\d+(?:\.\d+)?\s+\d[\d,]*(?:\.\d{1,4})?\s+\d[\d,]*(?:\.\d{1,4})?\s+",
                            "",
                            next_line,
                        ).strip()
                        description_parts.append(next_line)
                        index = lookahead

                description = self._clean_product_description(" ".join(description_parts))
                values = {
                    "row_type": "PRODUCT",
                    "product_name": description,
                    "quantity": match.group("qty"),
                    "unit_of_measurement": match.group("uom").upper(),
                    "unit_price": match.group("unit_price").replace(",", ""),
                    "currency": fields.get("currency") or "USD",
                    "line_total": match.group("line_total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {
                        "confidence": 94,
                        "source": {"type": "model_spec_quote_table", "line": index + 1},
                    }
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = []
                index += 1
                continue

            if re.match(
                r"^\d{1,3}\s+(?:EACH|PCS|PC|NOS|NO|SET|UNIT)\s+\d+(?:\.\d+)?\s+\d[\d,]*(?:\.\d{1,4})?\s+\d[\d,]*(?:\.\d{1,4})?\s*$",
                line,
                re.I,
            ):
                index += 1
                continue

            line = re.sub(
                r"^\d+(?:\.\d+)?\s+\d[\d,]*(?:\.\d{1,4})?\s+\d[\d,]*(?:\.\d{1,4})?\s+",
                "",
                line,
            ).strip()

            if re.search(r"\b(?:capacity|speed|input supply|phase|hz|capstan|winch|motor|pump)\b", line, re.I):
                pending_description.append(line)

            index += 1

        return (rows, row_meta) if len(rows) > 1 else None
    def _china_harbor_quote_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any("china harbor marine" in _norm(line) for line in lines):
            return None
        if not any("description standard qty" in _norm(line) for line in lines):
            return None

        row_pattern = re.compile(
            r"^(?P<no>\d{1,3})\s+"
            r"(?P<body>.+?)\s+"
            r"(?P<standard>ISO\s*\d+(?:-\d{4})?)\s+"
            r"(?P<model>[A-Z0-9-]+(?:-[A-Z0-9]+)*)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>NOS|PCS|PC|SET|EA|UNIT)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )
        cert_pattern = re.compile(
            r"^(?P<no>\d{1,3})\s+(?P<label>.+?\bCERTI(?:FICATE)?\b.*?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>NOS|PCS|PC|SET|EA|UNIT)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = ""

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            if normalized.startswith(("no description", "quotation", "address", "email", "to ", "p o box")):
                continue
            if normalized.startswith(("111", "total", "50 prepayment")) or "prepayment" in normalized:
                continue
            continued_row_match = re.match(
                r"^(?P<no>\d{1,3})\s+"
                r"(?P<standard>ISO\s*\d+(?:-\d{4})?)\s+"
                r"(?P<model>[A-Z0-9-]+(?:-[A-Z0-9]+)*)\s+"
                r"(?P<qty>\d+(?:\.\d+)?)\s+"
                r"(?P<uom>NOS|PCS|PC|SET|EA|UNIT)\s+"
                r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
                r"(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
                line,
                re.I,
            )
            if continued_row_match and pending_description:
                description = self._clean_product_description(pending_description)
                standard = re.sub(r"\s+", "", continued_row_match.group("standard").upper())
                model = continued_row_match.group("model").strip()
                product_name = self._clean_product_description(f"{description} | {standard} | {model}")

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_name,
                    "quantity": continued_row_match.group("qty"),
                    "unit_of_measurement": continued_row_match.group("uom"),
                    "unit_price": continued_row_match.group("unit_price").replace(",", ""),
                    "currency": fields.get("currency") or "USD",
                    "line_total": continued_row_match.group("line_total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 94, "source": {"type": "china_harbor_quote_continued_row", "line": line_no}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = ""
                continue
            cert_match = cert_pattern.match(line)
            if cert_match:
                label = self._clean_product_description(cert_match.group("label"))
                values = {
                    "row_type": "CHARGE",
                    "product_name": label,
                    "quantity": cert_match.group("qty"),
                    "unit_of_measurement": cert_match.group("uom"),
                    "unit_price": cert_match.group("unit_price").replace(",", ""),
                    "currency": fields.get("currency") or "USD",
                    "line_total": cert_match.group("line_total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 92, "source": {"type": "china_harbor_quote", "line": line_no}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = ""
                continue

            match = row_pattern.match(line)
            if match:
                description = self._clean_product_description(
                    " ".join(part for part in [pending_description, match.group("body")] if part)
                )
                standard = re.sub(r"\s+", "", match.group("standard").upper())
                model = match.group("model").strip()
                product_name = self._clean_product_description(f"{description} | {standard} | {model}")

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_name,
                    "quantity": match.group("qty"),
                    "unit_of_measurement": match.group("uom"),
                    "unit_price": match.group("unit_price").replace(",", ""),
                    "currency": fields.get("currency") or "USD",
                    "line_total": match.group("line_total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {"confidence": 94, "source": {"type": "china_harbor_quote", "line": line_no}}
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = ""
                continue

            if re.search(r"\b(?:fairlead|roller|bollard|chock|capstan)\b", line, re.I):
                pending_description = self._clean_product_description(line)

        return (rows, row_meta) if len(rows) > 1 else None
    def _simple_goods_quote_table_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any("description of goods" in _norm(line) for line in lines):
            return None
        if not any("unit price" in _norm(line) and "total amount" in _norm(line) for line in lines):
            return None

        price_row_pattern = re.compile(
            r"^(?P<item>\d{1,3})\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>[A-Z]{2,}(?:/[A-Z]{2,})?)\s+"
            r"(?:US\$|USD|\$)?\s*(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?:US\$|USD|\$)?\s*(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = None

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            if normalized.startswith(("add ", "e mail", "quotation", "to ", "attn", "re ", "dear")):
                continue
            if any(term in normalized for term in {"items description of goods", "unit price", "total amount"}):
                continue
            if normalized.startswith(("total", "note", "bank", "beneficiary", "swift")):
                pending_description = None
                continue

            match = price_row_pattern.match(line)
            if match and pending_description:
                description_parts = [pending_description]

                # Continuation line after the price row, before next row/total/note.
                next_index = line_no
                if next_index < len(lines):
                    next_line = lines[next_index]
                    next_norm = _norm(next_line)
                    if (
                        next_line
                        and not price_row_pattern.match(next_line)
                        and not next_norm.startswith(("total", "note", "bank", "beneficiary", "swift"))
                    ):
                        description_parts.append(next_line)

                description = self._clean_product_description(" ".join(description_parts))
                unit_price = match.group("unit_price").replace(",", "")
                line_total = match.group("line_total").replace(",", "")

                values = {
                    "row_type": "PRODUCT",
                    "product_name": description,
                    "quantity": match.group("qty"),
                    "unit_of_measurement": match.group("uom"),
                    "unit_price": unit_price,
                    "currency": fields.get("currency") or "USD",
                    "line_total": line_total,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({
                    field: {
                        "confidence": 94,
                        "source": {"type": "simple_goods_quote_table", "line": line_no},
                    }
                    for field, value in values.items()
                    if value not in (None, "")
                })
                pending_description = None
                continue

            if re.search(r"\b(?:capstan|winch|fairlead|roller|pump|motor|valve|certificate)\b", line, re.I):
                pending_description = line

        return (rows, row_meta) if len(rows) > 1 else None
    def _siemens_quote_table_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line and line.strip()
        ]

        if not any(
            re.search(r"\b(?:SIMATIC|SINAMICS|SCALANCE|LOGO!)\b", line, re.I)
            for line in lines
        ):
            return None

        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "part no" in _norm(line)
                and "description" in _norm(line)
                and "qty" in _norm(line)
                and "price" in _norm(line)
            ),
            -1,
        )

        part_pattern = re.compile(
            r"\b(?P<part>(?:6ES|6GK|6EP)[A-Z0-9./_-]{4,})\b",
            re.I,
        )

        price_tail_pattern = re.compile(
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>EA|EACH|PC|PCS|NOS|NO|UNIT|SET|LS)?\s*"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"(?:\s+(?:EX[-\s]?STOCK|AVAILABLE|READY|STOCK|"
            r"\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?))?\s*$",
            re.I,
        )

        stop_terms = {
            "subtotal", "sub total", "vat", "tax value", "quote total",
            "grand total", "total amount", "amount in words", "terms",
            "payment", "delivery", "bank details", "page",
        }

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = []

        for line_no, line in enumerate(lines[header_index + 1:], start=header_index + 2):
            normalized = _norm(line)

            if any(term in normalized for term in stop_terms):
                pending_description = []
                continue

            if re.search(r"\b(?:quotation|quote no|customer|supplier|date|trn|email|tel|phone|po box)\b", normalized):
                continue

            price_match = price_tail_pattern.search(line)
            part_match = part_pattern.search(line)

            if not price_match:
                if (
                    re.search(r"\b(?:SIMATIC|SINAMICS|SCALANCE|SITOP|LOGO!)\b", line, re.I)
                    or part_match
                    or pending_description
                ):
                    pending_description.append(line)
                continue

            before_price = line[:price_match.start()].strip()
            combined = " ".join([*pending_description, before_price]).strip()
            pending_description = []

            if not combined:
                continue

            part_match = part_pattern.search(combined)
            part_number = part_match.group("part").strip() if part_match else ""

            description = combined
            if part_number:
                description = re.sub(
                    rf"\b\d{{1,3}}\s+{re.escape(part_number)}\b",
                    part_number,
                    description,
                    count=1,
                    flags=re.I,
                )
                description = re.sub(
                    rf"\b{re.escape(part_number)}\s+\d{{1,3}}\s+",
                    f"{part_number} ",
                    description,
                    count=1,
                    flags=re.I,
                )
                description = re.sub(re.escape(part_number), " ", description, count=1, flags=re.I)

            description = re.sub(r"^\d{1,3}\s+", "", description).strip()
            description = re.sub(r"\s+\d{1,3}\s*$", "", description).strip()
            description = re.sub(
                r"\b(?:EXSTOCK|EX-STOCK|\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?)\b",
                " ",
                description,
                flags=re.I,
            )
            description = self._clean_product_description(description)
            continuation_parts = []
            for offset, next_line in enumerate(lines[line_no:], start=line_no):
                next_normalized = _norm(next_line)

                if any(term in next_normalized for term in stop_terms):
                    break

                if price_tail_pattern.search(next_line):
                    break

                if re.search(r"^\d{1,3}\s+(?:6ES|6GK|6EP)[A-Z0-9./_-]{4,}\b", next_line, re.I):
                    break

                upcoming_lines = lines[offset:offset + 4]
                if (
                    re.search(r"\b(?:SIMATIC|SINAMICS|SCALANCE|SITOP|LOGO!)\b", next_line, re.I)
                    and any(
                        price_tail_pattern.search(candidate)
                        and part_pattern.search(candidate)
                        for candidate in upcoming_lines
                    )
                ):
                    break

                if re.search(r"\b(?:from:|sent:|to:|cc:|subject:|dear|kind regards|commercial terms|delivery period|payment terms|validity|warranty|phone|mobile|address|web:|email)\b", next_normalized):
                    break

                if re.search(r"\b(?:total\s+aed|total\s+usd|sub\s*total|grand\s+total|\+\s*vat)\b", next_normalized):
                    break

                cleaned_next = self._clean_product_description(next_line)
                if cleaned_next:
                    continuation_parts.append(cleaned_next)

            if continuation_parts:
                description = self._clean_product_description(
                    " ".join([description, *continuation_parts])
                )
            if not description and not part_number:
                continue

            if part_number and description:
                product_name = f"{part_number} | {description}"
            else:
                product_name = description or part_number
            qty = price_match.group("qty")
            uom = price_match.group("uom") or None
            rate = price_match.group("rate").replace(",", "")
            total = price_match.group("total").replace(",", "")

            qty_decimal = _decimal(qty)
            rate_decimal = _decimal(rate)
            total_decimal = _decimal(total)

            if (
                qty_decimal is not None
                and rate_decimal is not None
                and total_decimal is not None
                and abs((qty_decimal * rate_decimal) - total_decimal) > max(Decimal("0.05"), abs(total_decimal) * Decimal("0.03"))
            ):
                continue

            values = {
                "row_type": "PRODUCT",
                "product_name": product_name,
                "item_code": part_number or None,
                "part_number": part_number or None,
                "quantity": qty,
                "unit_of_measurement": uom,
                "unit_price": rate,
                "currency": fields.get("currency") or "",
                "line_total": total,
            }

            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {
                    "confidence": 90,
                    "source": {"type": "siemens_quote_table", "line": line_no},
                }
                for field, value in values.items()
                if value not in (None, "")
            })

        return (rows, row_meta) if len(rows) > 1 else None
    def _siemens_bom_offer_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line and line.strip()
        ]

        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "bill of materials" in _norm(line)
            ),
            None,
        )
        if header_index is None:
            return None

        row_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+"
            r"(?P<inline_desc>.*?)\s*"
            r"(?P<part>[A-Z0-9./_-]*\d[A-Z0-9./_-]{4,})\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?:₹|INR|Rs\.?)\s*"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?:₹|INR|Rs\.?)\s*"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"(?:\s+(?P<lead>(?:EXSTOCK|\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?)))?\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = []
        index = header_index + 1

        while index < len(lines):
            line = lines[index]
            normalized = _norm(line)

            if normalized.startswith(("total cost", "general terms", "purchase order")):
                break
            if normalized in {"s n item model qty unit price total price lead time"}:
                index += 1
                continue
            if line == "`":
                index += 1
                continue
            if re.fullmatch(r"(?:EXSTOCK|\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?)", line, re.I):
                index += 1
                continue

            match = row_pattern.search(line)
            if not match:
                if not re.search(r"\b(?:₹|INR|Rs\.?)\s*\d", line, re.I):
                    pending_description.append(line)
                index += 1
                continue

            part_number = match.group("part")
            quantity = match.group("qty")
            rate = match.group("rate").replace(",", "")
            total = match.group("total").replace(",", "")

            before_part = line[:match.start()].strip()
            before_part = re.sub(r"^\s*\d{1,3}\s+", "", before_part).strip()
            description_parts = [part for part in pending_description if part and part != "`"]
            if before_part:
                description_parts.append(before_part)
            inline_description = self._clean_product_description(match.group("inline_desc"))
            if inline_description:
                description_parts.append(inline_description)

            lookahead = index + 1
            while lookahead < len(lines):
                next_line = lines[lookahead]
                next_norm = _norm(next_line)
                if row_pattern.search(next_line):
                    break
                if (
                    re.match(
                        r"^(?:SIMATIC|SINAMICS|SCALANCE|WinCC|LINE\s+REACTOR|LV\s+HRC)\b",
                        next_line,
                        re.I,
                    )
                    and any(
                        row_pattern.search(candidate_line)
                        for candidate_line in lines[lookahead + 1:lookahead + 4]
                    )
                ):
                    break
                if next_norm.startswith(("total cost", "general terms", "purchase order")):
                    break
                if next_line == "`":
                    lookahead += 1
                    continue
                if re.fullmatch(r"(?:EXSTOCK|\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?)", next_line, re.I):
                    lookahead += 1
                    continue
                if not re.search(r"\b(?:₹|INR|Rs\.?)\s*\d", next_line, re.I):
                    description_parts.append(next_line)
                lookahead += 1

            description = self._clean_product_description(" ".join(description_parts))
            description = re.sub(
                r"\b(?:EXSTOCK|\d+\s*-\s*\d+\s*WEEKS?|\d+\s*WEEKS?)\b",
                "",
                description,
                flags=re.I,
            )
            description = self._clean_product_description(description)
            if not description:
                description = part_number

            product_name = (
                f"{description} | {part_number}"
                if _norm(description) != _norm(part_number)
                else part_number
            )

            values = {
                "row_type": "PRODUCT",
                "product_name": product_name,
                "item_code": part_number,
                "part_number": part_number,
                "quantity": quantity,
                "unit_of_measurement": None,
                "unit_price": rate,
                "currency": fields.get("currency") or "INR",
                "line_total": total,
            }
            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {
                    "confidence": 94,
                    "source": {"type": "siemens_bom_offer", "line": index + 1},
                }
                for field in values
            })

            pending_description = []
            index = lookahead

        return (rows, row_meta) if len(rows) > 1 else None

    def _gulf_ocean_sales_quote_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line and line.strip()
        ]

        if not any("s no item code item name" in _norm(line) for line in lines):
            return None

        row_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\.?\s+"
            r"(?P<body>.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s*"
            r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|LEN|no\.s)\s+"
            r"(?P<rate>\.?\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<discount>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        current_quote_date = None
        in_items = False
        last_row_index = None

        stop_prefixes = (
            "gross",
            "vat",
            "expense",
            "round off",
            "net total",
            "amount in words",
            "remarks",
            "salesman",
            "prepared by",
            "print date",
            "page ",
        )

        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)

            date_match = re.search(r"\bDoc\.?\s*Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", line, re.I)
            if date_match:
                current_quote_date = self._normalize_document_value("document_date", date_match.group(1))

            if "s no item code item name" in normalized:
                in_items = True
                last_row_index = None
                continue

            if not in_items:
                continue

            if normalized.startswith(stop_prefixes):
                in_items = False
                last_row_index = None
                continue

            match = row_pattern.search(line)
            if not match:
                if last_row_index is not None and not re.search(r"\d[\d,]*(?:\.\d+)?\s+\d[\d,]*(?:\.\d+)?$", line):
                    product_index = VIRTUAL_HEADERS.index("product_name")
                    current_name = str(rows[last_row_index][product_index] or "").strip()
                    continuation = self._clean_product_description(line)
                    if continuation and not self._is_document_header_line(continuation):
                        rows[last_row_index][product_index] = self._clean_product_description(
                            f"{current_name} {continuation}"
                        )
                continue

            body = self._clean_product_description(match.group("body"))
            code = None
            code_match = re.match(r"^(?P<code>[A-Z0-9][A-Z0-9./_-]*\d[A-Z0-9./_-]*)\s+(?P<desc>.+)$", body, re.I)
            if code_match:
                code = code_match.group("code")
                product_name = self._clean_product_description(code_match.group("desc"))
            else:
                product_name = body

            rate = match.group("rate")
            if rate.startswith("."):
                rate = f"0{rate}"
            total = match.group("total").replace(",", "")

            values = {
                "row_type": "PRODUCT",
                "product_name": product_name,
                "item_code": code,
                "part_number": code,
                "quantity": match.group("qty"),
                "unit_of_measurement": match.group("uom").upper(),
                "unit_price": rate.replace(",", ""),
                "currency": fields.get("currency") or "",
                "tax": None,
                "tax_amount": None,
                "line_total": total,
            }
            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            last_row_index = len(rows) - 1
            meta = {
                field: {
                    "confidence": 94,
                    "source": {
                        "type": "sales_quote_qty_rate_amount",
                        "line": line_no,
                        "document_date": current_quote_date,
                    },
                }
                for field in values
            }
            row_meta.append(meta)

        return (rows, row_meta) if len(rows) > 1 else None

    def _boq_text_to_items(self, text, fields):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line and line.strip()
        ]

        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "description" in _norm(line)
                and "quantity" in _norm(line)
                and "unit price" in _norm(line)
                and "price" in _norm(line)
            ),
            None,
        )
        if header_index is None:
            return None

        row_pattern = re.compile(
            r"^(?P<before>.*?)\s*"
            r"(?P<uom>LS|L\.S\.|LOT|SET|NOS|NO|PCS|PC|EA|UNIT)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
            re.I,
        )

        rows, row_meta = [VIRTUAL_HEADERS], []
        pending_description = []

        stop_terms = {
            "total",
            "vat",
            "grand total",
            "notes",
            "exclusions",
            "attendances",
            "terms conditions",
            "terms and conditions",
            "thanks best regards",
        }

        for line_no, line in enumerate(lines[header_index + 1:], start=header_index + 2):
            normalized = _norm(line)
            if any(normalized.startswith(term) for term in stop_terms):
                break
            if normalized in {"measurement", "unit of measurement", "unit of", "row number", "price"}:
                continue
            if self._is_document_header_line(line):
                continue

            match = row_pattern.search(line)
            if not match:
                if not re.search(
                    r"\b(?:AED|USD|EUR|GBP|INR)\b|\b\d[\d,]*(?:\.\d{2,4})\s+\d[\d,]*(?:\.\d{2,4})\b",
                    line,
                    re.I,
                ):
                    pending_description.append(line)
                continue

            before = self._clean_product_description(match.group("before"))
            description_parts = [*pending_description]
            if before and not re.fullmatch(r"\d{1,3}", before):
                description_parts.append(before)

            lookahead = line_no
            continuation_lines = []
            while lookahead < len(lines):
                next_line = lines[lookahead]
                next_norm = _norm(next_line)

                if row_pattern.search(next_line):
                    break
                if any(next_norm.startswith(term) for term in stop_terms):
                    break
                if next_norm in {"measurement", "unit of measurement", "unit of", "row number", "price"}:
                    lookahead += 1
                    continue
                if self._is_document_header_line(next_line):
                    break
                if re.search(r"\b(?:program of work|terms of payment|validity|retention|best regards|confirmation)\b", next_norm):
                    break

                continuation_lines.append(next_line)
                lookahead += 1

            description_parts.extend(continuation_lines)
            product_name = self._clean_product_description(" ".join(description_parts))
            if not product_name:
                pending_description = []
                continue

            values = {
                "row_type": "SERVICE" if re.search(r"\b(?:installation|service|work|modification)\b", product_name, re.I) else "PRODUCT",
                "product_name": product_name,
                "quantity": match.group("qty"),
                "unit_of_measurement": match.group("uom").replace(".", "").upper(),
                "unit_price": match.group("rate").replace(",", ""),
                "currency": fields.get("currency") or self._currency_from_text(text) or "",
                "line_total": match.group("total").replace(",", ""),
            }
            rows.append([values.get(header) for header in VIRTUAL_HEADERS])
            row_meta.append({
                field: {
                    "confidence": 92,
                    "source": {"type": "boq_text_qty_rate_total", "line": line_no},
                }
                for field in values
            })
            pending_description = []

        return (rows, row_meta) if len(rows) > 1 else None
    def _quote_text_to_table_items(self, text, fields):
        rows, row_meta = [VIRTUAL_HEADERS], []
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
        currency = fields.get("currency") or self._currency_from_text(text) or ""
        isa_description_map = self._part_item_description_map_from_quote_lines(lines)

        numbered_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+(?P<desc>.+?)\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|no\.s)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"(?:\s|$)",
            re.I,
        )

        split_numbered_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|no\.s)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"(?:\s|$)",
            re.I,
        )
        split_currency_numbered_pattern = re.compile(
            r"^(?P<sl>\d{1,3}(?:\.\d{1,3})?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<currency1>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|[$\u20ac\u00a3\u00a5])\s*"
            r"(?P<rate>\d[\d,.]*(?:\.\d{1,4})?)\s+"
            r"(?P<currency2>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|[$\u20ac\u00a3\u00a5])\s*"
            r"(?P<total>\d[\d,.]*(?:\.\d{1,4})?)$",
            re.I,
        )
        currency_numbered_pattern = re.compile(
            r"^(?P<sl>\d{1,3}(?:\.\d{1,3})?)\s+(?P<desc>.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<currency1>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|[$\u20ac\u00a3\u00a5])\s*"
            r"(?P<rate>\d[\d,.]*(?:\.\d{1,4})?)\s+"
            r"(?P<currency2>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|[$\u20ac\u00a3\u00a5])\s*"
            r"(?P<total>\d[\d,.]*(?:\.\d{1,4})?)$",
            re.I,
        )
        due_date_qty_uom_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s*\|?\s*"
            r"(?P<desc>.+?)\s+"
            r"(?P<part>[A-Z0-9][A-Z0-9./_-]{2,})\s*\|?\s+"
            r"(?P<due>\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\s+"
            r"\|?\s*(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s*\|?\s*"
            r"(?:NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s*"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )
        qty_uom_numbered_pattern = re.compile(
            r"^(?P<sl>\d{1,3}(?:\.\d{1,3})?)\s+(?P<desc>.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"\s*$",
            re.I,
        )
        qty_uom_vat_amount_pattern = re.compile(
            r"^(?P<sl>\d{1,3}(?:\.\d{1,3})?)\s+(?P<desc>.+?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
            r"(?P<tax>\d+(?:\.\d+)?)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)"
            r"\s*$",
            re.I,
        )
        email_price_pattern = re.compile(
            r"@\s*(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)?\s*"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s*/?\s*(?P<uom>pc|pcs|nos|no|each|ea|unit|set|mtr|meter|kg)?",
            re.I,
        )
        part_number_amount_pattern = re.compile(
            r"^(?P<code>[A-Z0-9][A-Z0-9\-./]{2,})\s+"
            r"(?P<uom>NOS|NO|PCS|PC|EA|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?:(?P<currency1>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\s+)?"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?:(?P<currency2>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\s+)?"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )
        sr_price_qty_pattern = re.compile(
            r"^(?P<sl>\d{1,3})\s+"
            r"(?P<code>[A-Z0-9][A-Z0-9.\-\/]{2,})\s+"
            r"(?P<currency1>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\s+"
            r"(?P<list_price>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<currency2>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\s+"
            r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
            r"(?P<currency3>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\s+"
            r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )
        price_list_pattern = re.compile(
            r"^(?P<code>[A-Z0-9][A-Z0-9\-./]{2,})\s+(?P<desc>.+?)\s+(?P<rate>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )

        s_no_code_quote_header_index = next(
            (
                index
                for index, header_line in enumerate(lines)
                if "s no" in _norm(header_line)
                and "code" in _norm(header_line)
                and "description" in _norm(header_line)
                and "qty" in _norm(header_line)
                and "unit price" in _norm(header_line)
                and "total" in _norm(header_line)
            ),
            None,
        )
        if s_no_code_quote_header_index is not None:
            s_no_code_rows, s_no_code_meta = [VIRTUAL_HEADERS], []
            s_no_code_pattern = re.compile(
                r"^(?P<sl>\d{1,3})\s+(?P<body>.+?)\s+"
                r"(?P<qty>\d+(?:\.\d+)?)\s+"
                r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
                r"(?P<tax>\d+(?:\.\d+)?)\s+"
                r"(?P<rate>\d[\d,]*(?:\.\d{1,4})?)\s+"
                r"(?P<total>\d[\d,]*(?:\.\d{1,4})?)\s*$",
                re.I,
            )
            index = s_no_code_quote_header_index + 1
            while index < len(lines):
                line = lines[index]
                normalized_line = _norm(line)
                if any(term in normalized_line for term in {
                    "sub total",
                    "subtotal",
                    "grand total",
                    "quote total",
                    "tax value",
                    "amount in words",
                    "in words",
                    "discount",
                    "page ",
                }):
                    break

                match = s_no_code_pattern.search(line)
                if not match:
                    index += 1
                    continue

                description = self._clean_product_description(match.group("body"))
                description = re.sub(
                    r"^\s*(?:[A-Z0-9./_-]*\d[A-Z0-9./_-]*|\d{3,})\s+(?=[A-Z])",
                    "",
                    description,
                    flags=re.I,
                ).strip()

                lookahead = index + 1
                continuation_lines = []
                while lookahead < len(lines):
                    next_line = lines[lookahead]
                    next_norm = _norm(next_line)
                    if s_no_code_pattern.search(next_line):
                        break
                    if any(term in next_norm for term in {
                        "sub total",
                        "subtotal",
                        "grand total",
                        "quote total",
                        "tax value",
                        "amount in words",
                        "in words",
                        "discount",
                        "page ",
                    }):
                        break
                    if not self._is_document_header_line(next_line) and not any(term in next_norm for term in NOISE_TERMS):
                        continuation_lines.append(
                            re.sub(r"^\s*\d{1,3}\s+(?=[a-z])", "", next_line).strip()
                        )
                        lookahead += 1
                        continue
                    break

                if continuation_lines:
                    description = self._clean_product_description(
                        f"{description} {' '.join(continuation_lines)}"
                    )

                values = {
                    "row_type": "PRODUCT",
                    "product_name": description,
                    "quantity": match.group("qty"),
                    "unit_of_measurement": match.group("uom"),
                    "unit_price": match.group("rate").replace(",", ""),
                    "currency": currency,
                    "tax": match.group("tax"),
                    "line_total": match.group("total").replace(",", ""),
                }
                s_no_code_rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                s_no_code_meta.append({
                    field: {
                        "confidence": 94,
                        "source": {"type": "s_no_code_qty_uom_vat_rate_total", "line": index + 1},
                    }
                    for field in values
                })
                index = lookahead

            if len(s_no_code_rows) > 1:
                return s_no_code_rows, s_no_code_meta

        product_details_price_header = any(
            "product details" in _norm(_fix_pdf_text(line))
            and "price" in _norm(_fix_pdf_text(line))
            and "discount" in _norm(_fix_pdf_text(line))
            and "total" in _norm(_fix_pdf_text(line))
            for line in lines[:30]
        )
        if product_details_price_header:
            rows, row_meta = [VIRTUAL_HEADERS], []
            index = 0
            while index < len(lines):
                line = _fix_pdf_text(lines[index])
                match = sr_price_qty_pattern.search(line)
                if not match:
                    index += 1
                    continue

                code = match.group("code")
                description_lines = []
                lookahead = index + 1
                while lookahead < len(lines):
                    next_line = _fix_pdf_text(lines[lookahead])
                    next_norm = _norm(next_line)

                    if sr_price_qty_pattern.search(next_line):
                        break
                    if any(term in next_norm for term in {
                        "sub total",
                        "subtotal",
                        "grand total",
                        "vat",
                        "tax registration",
                        "terms conditions",
                        "payment terms",
                        "delivery terms",
                        "bank details",
                        "make pr electronics",
                        "no warranty",
                        "warranty",
                    }):
                        break

                    description_lines.append(next_line)
                    lookahead += 1

                description = self._clean_product_description(" ".join(description_lines))
                product_name = code if not description else f"{code} | {description}"
                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_name,
                    "quantity": match.group("qty"),
                    "unit_of_measurement": None,
                    "unit_price": match.group("rate").replace(",", ""),
                    "currency": (match.group("currency2") or match.group("currency1") or currency or "").upper(),
                    "line_total": match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({field: {"confidence": 92, "source": {"type": "product_details_price_quantity_discount_total", "line": index + 1}} for field in values})
                index = lookahead

            if len(rows) > 1:
                return rows, row_meta
    
        pending_code = None
        pending_desc = []
        pending_multiline_desc = []
        last_product_row_index = None
        pending_after_row_index = None
        in_item_table = False
        current_item_section = None

        def is_quote_tail_or_email_header(value):
            normalized_value = _norm(value)
            return (
                normalized_value.startswith((
                    "from ", "sent ", "to ", "cc ", "subject ", "dear ",
                    "phone ", "mobile ", "mob ", "tel ", "email ", "web ",
                    "address ", "kind regards", "regards",
                ))
                or normalized_value in {"note", "notes", "remark", "remarks"}
                or re.search(r"\b(?:phone|mobile|mob|tel|email|web)\b", normalized_value)
                or re.search(r"\+?\d{2,}\s*\d{5,}", normalized_value)
                or any(term in normalized_value for term in {
                    "delivery time", "delivery place", "payment terms", "validity",
                    "purchase order", "offer is valid", "other spares",
                    "all other terms", "certificate to be provided",
                    "manufacturer certificate", "additional cost",
                    "commercial terms", "terms conditions", "disclaimer",
                    "await your reply","industrial stock sales division",
                    "should you have any enquiries",
                    "thank you for your business",
                    "above information is not an invoice",
                    "payment will be collected",
                    "p o box",
                    "po box",
                    "items will be packed",
                    "standard packaging",
                    "additional packaging costs",
                    "prices quoted are based",
                    "prices do not include inspection",
                    "prices are subject to re confirmation",
                    "cable dimensions are nominal",
                    "kindly review the offer",
                    "orders cannot be canceled",
                    "orders cannot be amended",
                    "items not quoted",
                    "beyond our scope",
                    "not in stock",
                })
            )
        for line_no, line in enumerate(lines, start=1):
            normalized = _norm(line)
            if is_quote_tail_or_email_header(line):
                pending_code = None
                pending_desc = []
                pending_multiline_desc = []
                pending_after_row_index = None
                continue
            if re.search(
                r"\b(should you have any enquiries|thank you for your business|above information is not an invoice|"
                r"payment will be collected|p\.?\s*o\.?\s*box|tel\.?|e-?mail)\b",
                line,
                re.I,
            ):
                pending_code = None
                pending_desc = []
                pending_multiline_desc = []
                pending_after_row_index = None
                continue
            if re.match(
                r"^\s*(?:remark|remarks|note|notes)\s*:|^\s*\d+\.\s*(?:shipyard|export packing|purchase order|offer is valid|other spares|payment term|all other terms)\b",
                line,
                re.I,
            ):
                pending_code = None
                pending_desc = []
                pending_multiline_desc = []
                pending_after_row_index = None
                continue
            if (
                ("sl no" in normalized and "description" in normalized)
                or ("s no" in normalized and "description" in normalized and "qty" in normalized)
                or ("s no code description" in normalized and "unit price" in normalized and "total" in normalized)
                or ("sr" in normalized and "product details" in normalized and "price" in normalized)
                or ("description of goods" in normalized and "part no" in normalized and "quantity" in normalized)
                or ("item" in normalized and "description of goods" in normalized)
                or ("description of goods" in normalized and "unit price" in normalized)
                or ("item" in normalized and "description" in normalized and "u p" in normalized and "amt" in normalized)
            ):
                in_item_table = True
                pending_multiline_desc = []
                continue

            if not in_item_table:
                if (
                    currency_numbered_pattern.search(line)
                    or split_currency_numbered_pattern.search(line)
                    or qty_uom_vat_amount_pattern.search(line)
                    or qty_uom_numbered_pattern.search(line)
                ):
                    in_item_table = True
                else:
                    continue
            section_match = re.match(
                r"^\s*(?P<section_no>\d{1,3})\s+(?P<section>[A-Z][A-Z0-9 &/().,\-（）]+?)\s*$",
                line,
                re.I,
            )
            if (
                section_match
                and not re.search(r"\b\d+(?:\.\d+)?\s+(?:PC|PCS|EA|NOS?|UNIT|SET|LOT|LS)\s+\d", line, re.I)
                and not any(term in normalized for term in {"grand total", "total amount", "remark", "note"})
            ):
                current_item_section = self._clean_product_description(section_match.group("section"))
                continue
            if self._is_document_header_line(line) or is_quote_tail_or_email_header(line):
                pending_multiline_desc = []
                pending_after_row_index = None
                continue
            if any(term in normalized for term in {"sub total", "subtotal", "grand total", "quote total", "total aed", "total usd", "net amount", "net total", "vat", "tax value", "tax registration", "amount in words", "in words", "bank details", "terms conditions"}):
                continue
            if any(term in normalized for term in NOISE_TERMS):
                continue
            if re.fullmatch(r"\d+\s+\d+\s+working", normalized) or re.fullmatch(r"\d+-\d+\s+working", normalized) or re.fullmatch(r"\d+\s+of\s+\d+", normalized) or normalized == "order":
                continue
            if re.search(
                r"\b(net\s+amount|net\s+total|items\s+will\s+be\s+packed|standard\s+packaging|"
                r"additional\s+packaging\s+costs|prices\s+quoted\s+are\s+based|prices\s+do\s+not\s+include|"
                r"prices\s+are\s+subject|cable\s+dimensions\s+are\s+nominal|kindly\s+review\s+the\s+offer|"
                r"orders\s+cannot\s+be|items\s+not\s+quoted|beyond\s+our\s+scope|not\s+in\s+stock)\b",
                normalized,
                re.I,
            ):
                pending_code = None
                pending_desc = []
                pending_multiline_desc = []
                pending_after_row_index = None
                continue
            due_date_qty_uom_match = due_date_qty_uom_pattern.search(line)
            if due_date_qty_uom_match:
                product_description = self._clean_product_description(
                    f"{due_date_qty_uom_match.group('desc')} | {due_date_qty_uom_match.group('part')}"
                )
                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_description,
                    "item_code": due_date_qty_uom_match.group("part"),
                    "part_number": due_date_qty_uom_match.group("part"),
                    "quantity": due_date_qty_uom_match.group("qty"),
                    "unit_of_measurement": due_date_qty_uom_match.group("uom"),
                    "unit_price": due_date_qty_uom_match.group("rate").replace(",", ""),
                    "currency": currency,
                    "line_total": due_date_qty_uom_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 92, "source": {"type": "due_date_qty_uom_quote_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            qty_uom_vat_amount_match = qty_uom_vat_amount_pattern.search(line)
            if qty_uom_vat_amount_match:
                product_description = self._clean_product_description(
                    f"{current_item_section} | {qty_uom_vat_amount_match.group('desc')}"
                    if current_item_section and re.match(r"^\d+\.\d+\s+", line)
                    else qty_uom_vat_amount_match.group("desc")
                )
                product_description = re.sub(r"^\s*\d+\s+", "", product_description).strip()
                product_description = re.sub(
                    r"^\s*(?:[A-Z0-9./_-]*\d[A-Z0-9./_-]*|\d{3,})\s+(?=[A-Z])",
                    "",
                    product_description,
                    flags=re.I,
                ).strip()

                qty = _decimal(qty_uom_vat_amount_match.group("qty"))
                rate = _decimal(qty_uom_vat_amount_match.group("rate"))
                tax_percent = _decimal(qty_uom_vat_amount_match.group("tax"))
                tax_amount = _decimal(qty_uom_vat_amount_match.groupdict().get("tax_amount"))
                total = _decimal(qty_uom_vat_amount_match.group("total"))

                # Some quotation text order is:
                # qty uom VAT% unit_rate line_total
                # Example: 200 PCS 5 0.20 40.00
                if (
                    qty is not None
                    and rate is not None
                    and tax_percent is not None
                    and tax_amount is not None
                    and total is not None
                    and rate in {Decimal("0"), Decimal("5"), Decimal("5.0"), Decimal("5.00")}
                    and abs((qty * tax_amount) - total) <= max(Decimal("0.05"), abs(total) * Decimal("0.02"))
                ):
                    rate, tax_percent, tax_amount = tax_amount, rate, None

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_description,
                    "quantity": qty_uom_vat_amount_match.group("qty"),
                    "unit_of_measurement": qty_uom_vat_amount_match.group("uom"),
                    "unit_price": str(rate) if rate is not None else qty_uom_vat_amount_match.group("rate").replace(",", ""),
                    "currency": currency,
                    "tax": str(tax_percent) if tax_percent is not None else qty_uom_vat_amount_match.group("tax"),
                    "tax_amount": str(tax_amount) if tax_amount is not None else None,
                    "line_total": str(total) if total is not None else qty_uom_vat_amount_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 92, "source": {"type": "qty_uom_vat_amount_quote_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            split_currency_match = split_currency_numbered_pattern.search(line)
            if split_currency_match and pending_multiline_desc:
                detected_currency = self._currency_from_text(split_currency_match.group("currency1")) or currency
                description = self._clean_product_description(" ".join(pending_multiline_desc))

                values = {
                    "product_name": description,
                    "quantity": split_currency_match.group("qty"),
                    "unit_price": split_currency_match.group("rate"),
                    "line_total": split_currency_match.group("total"),
                    "currency": detected_currency,
                    "unit": None,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({"row_type": self._row_type(description)})
                pending_multiline_desc = []
                continue
            currency_numbered_match = (
                None
                if sr_price_qty_pattern.search(line)
                else currency_numbered_pattern.search(line)
            )
            if currency_numbered_match:
                detected_currency = self._currency_from_text(currency_numbered_match.group("currency1")) or currency
                values = {
                    "row_type": "PRODUCT",
                    "product_name": self._clean_product_description(currency_numbered_match.group("desc")),
                    "quantity": currency_numbered_match.group("qty"),
                    "unit_of_measurement": None,
                    "unit_price": currency_numbered_match.group("rate"),
                    "currency": detected_currency,
                    "line_total": currency_numbered_match.group("total"),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 86, "source": {"type": "currency_numbered_quote_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            qty_uom_match = qty_uom_numbered_pattern.search(line)
            if qty_uom_match:
                product_description = self._clean_product_description(
                    f"{current_item_section} | {qty_uom_match.group('desc')}"
                    if current_item_section and re.match(r"^\d+\.\d+\s+", line)
                    else qty_uom_match.group("desc")
                )
                product_description = re.sub(r"^\s*\d+\s+", "", product_description).strip()
                product_description = re.sub(
                    r"^\s*(?:[A-Z0-9./_-]*\d[A-Z0-9./_-]*|\d{3,})\s+(?=[A-Z])",
                    "",
                    product_description,
                    flags=re.I,
                ).strip()
                qty = _decimal(qty_uom_match.group("qty"))
                rate = _decimal(qty_uom_match.group("rate"))
                total = _decimal(qty_uom_match.group("total"))
                tax_percent = None

                # Some PDF text extraction drops the final amount column, leaving
                # qty uom VAT% unit_rate. Treat the VAT% as tax and calculate total.
                if (
                    qty is not None
                    and rate is not None
                    and total is not None
                    and rate in {Decimal("0"), Decimal("5"), Decimal("5.0"), Decimal("5.00")}
                    and total < rate
                ):
                    tax_percent = rate
                    rate = total
                    total = qty * rate

                values = {
                    "row_type": "PRODUCT",
                    "product_name": product_description,
                    "quantity": qty_uom_match.group("qty"),
                    "unit_of_measurement": qty_uom_match.group("uom"),
                    "unit_price": str(rate) if rate is not None else qty_uom_match.group("rate").replace(",", ""),
                    "currency": currency,
                    "tax": str(tax_percent) if tax_percent is not None else None,
                    "line_total": str(total) if total is not None else qty_uom_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 88, "source": {"type": "qty_uom_numbered_quote_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            sr_price_qty_match = sr_price_qty_pattern.search(line)
            if sr_price_qty_match:
                code = sr_price_qty_match.group("code")
                values = {
                    "row_type": "PRODUCT",
                    "item_code": code,
                    "part_number": code,
                    "product_name": code,
                    "quantity": sr_price_qty_match.group("qty"),
                    "unit_of_measurement": None,
                    "unit_price": sr_price_qty_match.group("rate").replace(",", ""),
                    "currency": (sr_price_qty_match.group("currency2") or sr_price_qty_match.group("currency1") or currency or "").upper(),
                    "line_total": sr_price_qty_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 90, "source": {"type": "sr_list_price_qty_discount_total_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            split_match = split_numbered_pattern.search(line)
            if split_match and pending_multiline_desc:
                description = self._clean_product_description(" ".join(pending_multiline_desc))
                values = {
                    "row_type": "PRODUCT",
                    "product_name": description,
                    "quantity": split_match.group("qty"),
                    "unit_of_measurement": split_match.group("uom"),
                    "unit_price": split_match.group("rate").replace(",", ""),
                    "currency": currency,
                    "line_total": split_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                row_meta.append({field: {"confidence": 86, "source": {"type": "split_numbered_quote_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            part_amount_match = part_number_amount_pattern.search(line)
            if part_amount_match:
                code = part_amount_match.group("code")

                description = self._clean_product_description(" ".join(pending_multiline_desc))
                description = re.sub(
                    r"\b\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?\b",
                    " ",
                    description,
                    flags=re.I,
                )
                description = re.sub(
                    r"\b(?:EA|PCS|PC|NOS|NO|LS|LOT|SET)\s+\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\b.*$",
                    " ",
                    description,
                    flags=re.I,
                )
                description = re.sub(
                    r"\b(?:ISA\s+)?Handling\s+fees\b.*$",
                    " ",
                    description,
                    flags=re.I,
                )
                description = self._clean_product_description(description)

                item_name = ""
                item_description = description

                split_item = re.match(
                    r"^(?P<item>.+?)\s+(?P<desc>(?:I\.?S\.?|Universal|Programming|Uo=|isolated|input|output|display|system)\b.*)$",
                    description,
                    re.I,
                )
                if split_item:
                    item_name = self._clean_product_description(split_item.group("item"))
                    item_description = self._clean_product_description(split_item.group("desc"))

                product_parts = [code]
                if item_name:
                    product_parts.append(item_name)
                if item_description and _norm(item_description) != _norm(item_name):
                    product_parts.append(item_description)

                values = {
                    "row_type": "PRODUCT",
                    "item_code": code,
                    "part_number": code,
                    "product_name": isa_description_map.get(code) or " | ".join(part for part in product_parts if part),
                    "quantity": part_amount_match.group("qty"),
                    "unit_of_measurement": part_amount_match.group("uom"),
                    "unit_price": part_amount_match.group("rate").replace(",", ""),
                    "currency": (part_amount_match.group("currency1") or part_amount_match.group("currency2") or currency or "").upper(),
                    "line_total": part_amount_match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                pending_after_row_index = last_product_row_index
                row_meta.append({field: {"confidence": 88, "source": {"type": "part_number_amount_line", "line": line_no}} for field in values})
                pending_multiline_desc = []
                continue
            match = numbered_pattern.search(line)
            if match:
                values = {
                    "row_type": "PRODUCT",
                    "product_name": self._clean_product_description(
                        f"{current_item_section} | {match.group('desc')}"
                        if current_item_section and re.match(r"^\d+\.\d+\s+", line)
                        else match.group("desc")
                    ),
                    "quantity": match.group("qty"),
                    "unit_of_measurement": match.group("uom"),
                    "unit_price": match.group("rate").replace(",", ""),
                    "currency": currency,
                    "line_total": match.group("total").replace(",", ""),
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                last_product_row_index = len(rows) - 1
                row_meta.append({field: {"confidence": 88, "source": {"type": "numbered_quote_line", "line": line_no}} for field in values})
                continue

            if re.fullmatch(r"[A-Z0-9][A-Z0-9\-./]{4,}", line, re.I):
                pending_code = line
                pending_desc = []
                continue

            email_match = email_price_pattern.search(line)
            if email_match and pending_code:
                description_text = self._clean_product_description(" ".join(pending_desc) or pending_code)
                description = (
                    f"{pending_code} | {description_text}"
                    if description_text and description_text != pending_code
                    else pending_code
                )
                rate = email_match.group("rate").replace(",", "")
                values = {
                    "row_type": "PRODUCT",
                    "item_code": pending_code,
                    "part_number": pending_code,
                    "product_name": description,
                    "quantity": "1",
                    "unit_of_measurement": email_match.group("uom") or "pc",
                    "unit_price": rate,
                    "currency": (email_match.group("currency") or currency or "").upper(),
                    "line_total": rate,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({field: {"confidence": 86, "source": {"type": "email_offer_line", "line": line_no}} for field in values})
                pending_code = None
                pending_desc = []
                continue

            price_match = price_list_pattern.search(line)
            if (
                price_match
                and not re.search(r"^\d{1,3}(?:\.\d{1,3})?\s+.+?\s+\d+(?:\.\d+)?\s+(?:NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+\d", line, re.I)
                and not any(word in normalized for word in {"shipped", "model description", "price list", "sub total", "subtotal", "grand total", "total", "vat", "tax", "discount", "in words", "quote total", "page"})
            ):
                rate = price_match.group("rate").replace(",", "")
                values = {
                    "row_type": "PRODUCT",
                    "item_code": price_match.group("code"),
                    "part_number": price_match.group("code"),
                    "product_name": self._clean_product_description(price_match.group("desc")),
                    "quantity": "1",
                    "unit_of_measurement": "Unit",
                    "unit_price": rate,
                    "currency": currency or "INR",
                    "line_total": rate,
                }
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append({field: {"confidence": 78, "source": {"type": "price_list_line", "line": line_no}} for field in values})
                continue

            if pending_code and not email_price_pattern.search(line):
                pending_desc.append(line)
            if not re.match(r"^\d{1,3}(?:\.\d{1,3})?\s+", line) and not price_list_pattern.search(line):
                if re.search(
                    r"\b(?:handling\s+fees|coc\s+and\s+handling|total\s*\(|grand\s+total|vat\s*\(|terms\s+and\s+conditions)\b",
                    normalized,
                    re.I,
                ):
                    pending_multiline_desc = []
                    pending_after_row_index = None
                    continue
                if is_quote_tail_or_email_header(line) or self._is_document_header_line(line):
                    pending_multiline_desc = []
                    pending_after_row_index = None
                    continue

                continuation_allowed = (
                    re.match(
                        r"^(?:NPT\)|BSP\)|SS316|SS304|MOC:|P-|H=|Incl\.?|Measure Range|Material:|Wire Type|Temperature Range|Diameter:|Cable Length|Cable Protection|Termination:)",
                        line,
                        re.I,
                    )
                    or re.search(
                        r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|mtr|meter|inch|in)\s*\([^)]*\)"
                        r"(?:\s*[xX]\s*\d+(?:\.\d+)?\s*(?:mm|cm|m|mtr|meter|inch|in)\s*\([^)]*\))+",
                        line,
                        re.I,
                    )
                    or (
                        pending_after_row_index == last_product_row_index
                        and not re.search(r"\b(?:AED|USD|EUR)\b\s*\d", line, re.I)
                        and not re.search(r"\b(?:handling\s+fees|total|grand\s+total|vat|terms)\b", normalized, re.I)
                        and len(line.split()) >= 3
                    )
                )

                if last_product_row_index is not None and pending_after_row_index == last_product_row_index and continuation_allowed:
                    product_name_index = VIRTUAL_HEADERS.index("product_name")
                    clean_tail = re.sub(
                        r"\b(?:EA|PCS|PC|NOS|NO|LS|LOT|SET)\s+\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\b.*$",
                        " ",
                        line,
                        flags=re.I,
                    )
                    clean_tail = re.sub(
                        r"\b(?:ISA\s+)?Handling\s+fees\b.*$",
                        " ",
                        clean_tail,
                        flags=re.I,
                    )
                    clean_tail = self._clean_product_description(clean_tail)

                    if clean_tail:
                        current_name = str(rows[last_product_row_index][product_name_index] or "").strip()
                        part_number_index = VIRTUAL_HEADERS.index("part_number")
                        item_code_index = VIRTUAL_HEADERS.index("item_code")
                        current_part = ""
                        for candidate_index in (part_number_index, item_code_index):
                            if candidate_index < len(rows[last_product_row_index]) and rows[last_product_row_index][candidate_index]:
                                current_part = str(rows[last_product_row_index][candidate_index]).strip()
                                break

                        separator = " | " if current_part and _norm(current_name) == _norm(current_part) else " "
                        rows[last_product_row_index][product_name_index] = self._clean_product_description(
                            f"{current_name}{separator}{clean_tail}"
                        )
                    pending_after_row_index = None
                else:
                    if pending_after_row_index == last_product_row_index:
                        pending_multiline_desc = [line]
                    else:
                        pending_multiline_desc.append(line)
        if last_product_row_index is not None and pending_multiline_desc:
            safe_continuation = [
                item for item in pending_multiline_desc
                if re.match(
                    r"^(?:NPT\)|BSP\)|SS316|SS304|MOC:|P-|H=|Incl\.?|Measure Range|Material:|Wire Type|Temperature Range|Diameter:|Cable Length|Cable Protection|Termination:)",
                    item,
                    re.I,
                )
            ]
            if safe_continuation:
                product_name_index = VIRTUAL_HEADERS.index("product_name")
                rows[last_product_row_index][product_name_index] = self._clean_product_description(
                    f"{rows[last_product_row_index][product_name_index]} {' '.join(safe_continuation)}"
                )
        if isa_description_map and len(rows) > 1:
            self._apply_part_item_description_map(rows, isa_description_map)
        return (rows, row_meta) if len(rows) > 1 else None

    def _part_item_description_map_from_quote_lines(self, lines):
        result = {}
        for index, line in enumerate(lines):
            code_match = re.match(
                r"^(?P<code>[A-Z0-9][A-Z0-9\-./]{2,})\s+"
                r"(?P<uom>NOS|NO|PCS|PC|EA|PCE|UNIT|ROLL|MTR|METER|SET|PKT|LOT|KG|TIN|TUBE|BOX|BAG|PAIR|LTR|LS|no\.s)\s+"
                r"\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?\s+"
                r"(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?(?:\s|$)",
                line,
                re.I,
            )
            if not code_match:
                continue

            before = lines[index - 1] if index > 0 else ""
            after = lines[index + 1] if index + 1 < len(lines) else ""
            if self._is_document_header_line(before) or self._is_document_header_line(after):
                continue
            if re.search(r"\b(?:handling\s+fees|total|grand\s+total|vat|terms)\b", _norm(before), re.I):
                continue
            if re.search(r"\b(?:handling\s+fees|total|grand\s+total|vat|terms)\b", _norm(after), re.I):
                after = ""

            combined = self._clean_product_description(f"{before} {after}".strip())
            combined = re.sub(
                r"\b(?:EA|PCS|PC|NOS|NO|LS|LOT|SET)\s+\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\b.*$",
                " ",
                combined,
                flags=re.I,
            )
            combined = re.sub(r"\b(?:ISA\s+)?Handling\s+fees\b.*$", " ", combined, flags=re.I)
            combined = self._clean_product_description(combined)
            if not combined:
                continue

            item_name = ""
            item_description = combined
            split_item = re.match(
                r"^(?P<item>.+?)\s+(?P<desc>(?:I\.?S\.?|Universal|Programming|Uo=|isolated|input|output|display|system)\b.*)$",
                combined,
                re.I,
            )
            if split_item:
                item_name = self._clean_product_description(split_item.group("item"))
                item_description = self._clean_product_description(split_item.group("desc"))

            parts = [code_match.group("code")]
            if item_name:
                parts.append(item_name)
            if item_description and _norm(item_description) != _norm(item_name):
                parts.append(item_description)
            result[code_match.group("code")] = " | ".join(part for part in parts if part)
        return result

    def _apply_part_item_description_map(self, rows, description_map):
        product_name_index = VIRTUAL_HEADERS.index("product_name")
        part_number_index = VIRTUAL_HEADERS.index("part_number")
        item_code_index = VIRTUAL_HEADERS.index("item_code")
        for row in rows[1:]:
            part_number = row[part_number_index] if part_number_index < len(row) else None
            item_code = row[item_code_index] if item_code_index < len(row) else None
            current_name = row[product_name_index] if product_name_index < len(row) else None
            key = part_number or item_code or str(current_name or "").split("|", 1)[0].strip()
            if key in description_map:
                row[product_name_index] = description_map[key]
            elif current_name:
                cleaned = re.sub(
                    r"\b\d+(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?\s+(?:AED|USD|EUR|GBP|INR)\s+\d[\d,]*(?:\.\d+)?\b.*$",
                    " ",
                    str(current_name),
                    flags=re.I,
                )
                cleaned = re.sub(r"\b(?:ISA\s+)?Handling\s+fees\b.*$", " ", cleaned, flags=re.I)
                row[product_name_index] = self._clean_product_description(cleaned)

    def _header_supplier_before_title(self, text):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line and line.strip()
        ]
        title_index = next(
            (
                index
                for index, line in enumerate(lines)
                if re.search(r"\b(?:sales quotation|quotation|proforma invoice|tax invoice|commercial invoice)\b", line, re.I)
            ),
            min(len(lines), 20),
        )
        for line in lines[:title_index]:
            legal_match = re.search(
                r"(?P<supplier>[A-Z][A-Z0-9&.,'() /-]{2,}?\s+(?:L\.?L\.?C|LLC|LTD|LIMITED|PVT\s+LTD|FZE|FZCO|TRADING|COMPANY|CO\.?))\b",
                line,
                re.I,
            )
            candidate = self._clean_supplier_name(legal_match.group("supplier") if legal_match else line)
            if not candidate:
                continue

            candidate = re.sub(
                r"\b(?:PO\s*Box|P\.?\s*O\.?\s*Box|Email|E-mail|Website|Tel|Phone)\b.*$",
                "",
                str(candidate),
                flags=re.I,
            ).strip(" :-,")
            if not candidate:
                continue
            if (
                candidate
                and re.search(r"\b(?:L\.?L\.?C|LLC|LTD|LIMITED|PVT\s+LTD|FZE|FZCO|TRADING|COMPANY|CO\.?)\b", candidate, re.I)
                and self._is_valid_document_supplier(candidate)
                and not self._is_buyer_or_delivery_line(candidate)
            ):
                return candidate
        return None
    
    def _is_document_header_line(self, line):
        normalized = _norm(line)
        if not normalized:
            return True
        header_prefixes = {
            "date",
            "valid until",
            "validity",
            "quote",
            "quote remarks",
            "project",
            "rfq",
            "customer",
            "attn",
            "payment",
            "delivery",
            "po box",
        }
        if any(normalized.startswith(prefix) for prefix in header_prefixes):
            return True
        if re.search(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}\b", line, re.I):
            return True
        return False
    
    def _clean_product_description(self, value):
        cleaned = str(value or "").strip(" :-")
        cleaned = re.sub(r"\s+(?:days upon order|ex-stock|subject to prior sales).*$", "", cleaned, flags=re.I)
        return cleaned.strip(" :-")
    
    def _lines_to_table_items(self, text):
        lines = [(index, re.sub(r"\s+", " ", line).strip()) for index, line in enumerate(text.splitlines(), start=1) if line.strip()]
        header_pos = None
        for pos, (_line_number, line) in enumerate(lines):
            normalized = _norm(line)
            hits = sum(1 for aliases in LINE_SCHEMA.values() for alias in aliases if _norm(alias) in normalized)
            if hits >= 3 and any(term in normalized for term in {"description", "item", "product"}) and any(term in normalized for term in {"qty", "quantity"}):
                header_pos = pos
                break
        if header_pos is None:
            return None
        rows, row_meta, pending_description = [VIRTUAL_HEADERS], [], []
        for line_number, line in lines[header_pos + 1:]:
            normalized = _norm(line)
            if any(term in normalized for term in TOTAL_STOP_TERMS):
                break
            if any(term in normalized for term in NOISE_TERMS):
                continue
            if re.fullmatch(r"0*\d{1,4}", line.strip()):
                continue
            line = self._strip_leading_serial(line)
            parsed = self._parse_item_text_row(" ".join([*pending_description, line]).strip(), line_number)
            if parsed:
                values, meta = parsed
                rows.append([values.get(header) for header in VIRTUAL_HEADERS])
                row_meta.append(meta)
                pending_description = []
            elif not re.search(r"\b\d[\d,]*(?:\.\d+)?\b", line):
                pending_description.append(line)
        return (rows, row_meta) if len(rows) > 1 else None

    def _parse_item_text_row(self, line, line_number):
        pattern = re.compile(
            r"^(?P<description>.+?)\s+"
            r"(?P<quantity>\d+(?:\.\d+)?)\s+"
            r"(?P<uom>nos?|pcs?|pc|mtr|m|meter|metre|pair|roll|sheet|set|ea|kg|ltr)\s+"
            r"(?P<unit_price>\d[\d,]*(?:\.\d{1,4})?)"
            r"(?:\s+(?P<currency>AED|USD|EUR|SAR|INR|GBP))?"
            r"(?:\s+(?P<vat_percent>\d+(?:\.\d+)?)%?)?"
            r"(?:\s+(?P<vat_amount>\d[\d,]*(?:\.\d{1,4})?))?"
            r"\s+(?P<line_total>\d[\d,]*(?:\.\d{1,4})?)$",
            re.I,
        )
        match = pattern.match(line)
        if not match:
            return None
        description = match.group("description").strip(" -")
        if not description or any(term in _norm(description) for term in NOISE_TERMS):
            return None
        product_name, specification, material = self._split_description_spec_material(description)
        currency = match.group("currency").upper() if match.group("currency") else None
        values = {
            "row_type": self._row_type(product_name),
            "product_name": product_name,
            "specification": specification,
            "material": material,
            "quantity": match.group("quantity"),
            "unit_of_measurement": match.group("uom"),
            "unit_price": match.group("unit_price").replace(",", ""),
            "currency": currency,
            "tax": match.group("vat_percent"),
            "line_total": match.group("line_total").replace(",", ""),
        }
        if match.group("vat_amount"):
            values["tax_amount"] = match.group("vat_amount").replace(",", "")
        meta = {
            field: {"confidence": 82, "source": {"type": "table_text_region", "line": line_number}}
            for field in values
        }
        return values, meta



    def _apply_document_currency(self, rows, row_meta, fields):
        currency = fields.get("currency") or ""
        if not rows:
            return

        headers = rows[0]
        product_index = headers.index("product_name") if "product_name" in headers else -1
        specification_index = headers.index("specification") if "specification" in headers else -1
        material_index = headers.index("material") if "material" in headers else -1
        currency_index = headers.index("currency") if "currency" in headers else -1
        quantity_index = headers.index("quantity") if "quantity" in headers else -1
        unit_price_index = headers.index("unit_price") if "unit_price" in headers else -1
        line_total_index = headers.index("line_total") if "line_total" in headers else -1

        for index, row in enumerate(rows[1:]):
            if product_index >= 0 and product_index < len(row):
                product_name = str(row[product_index] or "").strip()
                material_parts = []

                if specification_index >= 0 and specification_index < len(row):
                    specification = str(row[specification_index] or "").strip()
                    if specification and specification not in product_name:
                        material_parts.append(specification)

                if material_index >= 0 and material_index < len(row):
                    material = str(row[material_index] or "").strip()
                    if material and material not in product_name:
                        material_parts.append(material)

                if material_parts:
                    row[product_index] = (
                        product_name
                        + " | "
                        + " ".join(material_parts)
                    ).strip(" |")
            if currency_index >= 0 and currency_index < len(row) and not row[currency_index]:
                row[currency_index] = currency
                if index < len(row_meta):
                    row_meta[index]["currency"] = {
                        "confidence": 80,
                        "source": {"type": "document_metadata"},
                    }

            if (
                quantity_index >= 0
                and unit_price_index >= 0
                and line_total_index >= 0
                and quantity_index < len(row)
                and unit_price_index < len(row)
                and line_total_index < len(row)
                and row[line_total_index] in (None, "")
            ):
                quantity = _decimal(row[quantity_index])
                unit_price = _decimal(row[unit_price_index])

                if quantity is not None and unit_price is not None:
                    row[line_total_index] = str(quantity * unit_price)
                    if index < len(row_meta):
                        row_meta[index]["line_total"] = {
                            "confidence": 78,
                            "source": {"type": "quantity_unit_price_calculated"},
                        }
    def _split_description_spec_material(self, description):
        tokens = description.split()
        iso_index = next((index for index, token in enumerate(tokens) if token.upper().startswith("ISO")), None)
        if iso_index is not None and iso_index < len(tokens) - 2:
            product_name = " ".join(tokens[:iso_index]).strip()
            material = tokens[-1]
            specification = " ".join(tokens[iso_index:-1]).strip()
            return product_name or description, specification or None, material or None
        return description, None, None

    def _row_type(self, value):
        lowered = _norm(value)
        if not lowered:
            return "UNKNOWN"
        if any(term in lowered for term in CHARGE_TERMS):
            return "CHARGE"
        if any(term in lowered for term in SERVICE_TERMS):
            return "SERVICE"
        if len(lowered.split()) >= 1:
            return "PRODUCT"
        return "UNKNOWN"
    def _is_valid_document_supplier(self, value):
        """
        Return True only when the value looks like the document issuer/supplier.

        Reject customer names, bank details, totals, amount-in-words,
        product descriptions and document labels.
        """

        text = self._clean_supplier_name(value)
        if not text:
            return False

        text = re.sub(
            r"\b(?:Voucher\s+No\.?|Dated|Mode/Terms\s+of\s+Payment|Buyer'?s\s+Ref\.?/Order\s+No\.?|Other\s+Reference\(s\)).*$",
            "",
            text,
            flags=re.I,
        ).strip(" :-|")
        if not text:
            return False

        normalized = _norm(text)
        if re.search(r"[\\<>=]{1,}", text):
            return False
        if len(re.findall(r"[A-Za-z]", text)) < 3:
            return False
        if re.fullmatch(r"[\\/\W_]*[A-Za-z]{1,2}\s+(?:co|company)[\\/\W_]*", text, re.I):
            return False
        if normalized.startswith(("address", "phone", "fax", "website", "email", "contacts", "contact")):
            return False

        if normalized in {
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
            "teamwork",
            "our works",
            "seller",
            "company",
        }:
            return False
                
        

        rejected_terms = {
            "quotation",
            "commercial offer",
            "proposal",
            "project",
            "prepared",
            "prepared by",
            "proforma invoice",
            "purchase order",
            "invoice",
            "delivery location",
            "delivery address",
            "billing address",
            "shipping address",
            "bill to",
            "ship to",
            "customer",
            "buyer",
            "description",
            "item description",
            "unit price",
            "unit rate",
            "quantity",
            "payment",
            "validity",
            "terms and conditions",
            "once the order is placed",
            "cancellation is not allowed",
            "unless agreed",
            "bank details",
            "bank name",
            "branch",
            "account no",
            "account number",
            "ifsc",
            "swift code",
            "iban",
            "total pi amount",
            "amount in words",
            "us dollars",
            "grand total",
            "taxable",
            "authorised signatory",
            "authorized signatory",
            "aries marine engineering services",
            "aries trading services",
            "sarika",
            "mecommercial",
            "make",
            "origin",
            "country of origin",
            "coo",
            "hs code",
            "warranty",
            "al jerf",
            "ajman",
            "united arab emirates",
            "subject",
            "follow up",
            "flagged",
            "from",
            "sent",
            "to",
            "cc",
            "certificate",
            "certificates",
            "description of goods",
            "qty pcs",
            "sum usd",
            "teamwork",
            "our works",
            "cooperation",
        }

        if any(
            (
                re.search(rf"\b{re.escape(term)}\b", normalized)
                if len(term) <= 3
                else term in normalized
            )
            for term in rejected_terms
        ):
            return False

        if "@" in text:
            return False

        if re.search(
            r"\b(?:aed|usd|eur|inr|qty|uom|vat|total|rate|amount|certificate|certificates|information)\b",
            normalized,
            re.I,
        ):
            return False

        # Reject sentences/large extracted blocks.
        if len(text) > 160:
            return False

        # Reject values containing too many numbers.
        if len(re.findall(r"\d", text)) >= 5:
            return False

        words = normalized.split()

        if not 1 <= len(words) <= 14:
            return False

        supplier_noise_tokens = {
            "unit", "price", "total", "material", "category",
            "quantity", "qty", "description", "item", "product",
            "amount", "rate", "currency", "meter", "pcs",
            "prepared", "proposal", "project", "teamwork",
        }

        if words and all(
            token in supplier_noise_tokens
            for token in words
        ):
            return False

        sentence_words = {
            "that", "which", "with", "through", "provide", "provides",
            "providing", "deliver", "delivers", "support", "supports",
            "ensure", "ensures", "create", "creates", "aim", "aims",
            "our", "your", "their", "this", "these", "those", "and",
            "for", "from", "into", "across", "within", "without",
        }

        has_company_indicator = re.search(
            r"\b("
            r"llc|l l c|ltd|limited|company|co|corporation|corp|"
            r"trading|technical|technologies|technology|supplies|services|"
            r"industries|industry|industrial|enterprises|"
            r"establishment|est|fze|fzc|group|alliances|"
            r"marine|electronics|electrical|engineering|agencies|factory|automation|"
            r"carpentry|packaging|manufactures|manufacturing|manufacturer"
            r")\b",
            normalized,
            re.I,
        )

        if (
            len(words) >= 4
            and any(token in sentence_words for token in words)
            and not has_company_indicator
        ):
            return False

        company_indicators = has_company_indicator

        return bool(company_indicators)
    def _supplier_from_email_like_headers(self, text):
        ignored_domains = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "aol.com", "proton.me", "protonmail.com",
        }

        for match in re.finditer(
            r"^\s*(?:from|sender)\s*:\s*.*?<?[^@\s<>]+@(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})>?",
            str(text or ""),
            re.I | re.M,
        ):
            domain = match.group("domain").lower().strip(".")
            if domain in ignored_domains:
                continue

            parts = domain.split(".")
            company_part = (
                parts[-3]
                if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org"}
                else parts[-2]
            )

            if company_part in {"mail", "email", "info", "sales", "admin", "accounts"}:
                continue

            supplier = company_part.replace("-", " ").replace("_", " ").upper()
            if len(supplier) >= 3:
                return supplier

        return None
    def _supplier_from_pdf_text(self, text):
        """
        Extract a reliable issuing company from native PDF text.

        Supports:
        - complete company names on one line;
        - company names split across nearby lines, such as:
          "KAYA MARINE" + "Electronic Services Ltd."

        Generic document labels and buyer/customer sections are rejected.
        """

        lines = [
            re.sub(r"\s+", " ", _fix_pdf_text(line)).strip(" :-|")
            for line in str(text or "").splitlines()
            if str(line or "").strip()
        ]

        if not lines:
            return None
        from_display_supplier = self._supplier_from_sender_email_domain("\n".join(lines))
        if from_display_supplier:
            return from_display_supplier
        email_header_supplier = self._supplier_from_email_like_headers("\n".join(lines))
        if email_header_supplier:
            return email_header_supplier
        # Header supplier can be on the same line as DATE / QUOTE NO.
        # Example: "SealCorr Oilfield Supplies and Services LLC DATE : 17/Mar/2026"
        for line in lines[:12]:
            candidate_line = re.sub(
                r"\b(?:DATE|QUOTE\s*NO|QUOTATION\s*NO|QTN\s*NO|REF(?:ERENCE)?\s*NO|VALID\s+UNTIL)\b\s*[:#-]?.*$",
                "",
                line,
                flags=re.I,
            ).strip(" :-|")

            candidate = self._clean_supplier_name(candidate_line)

            if (
                candidate
                and not self._is_buyer_or_delivery_line(candidate)
                and self._is_valid_document_supplier(candidate)
            ):
                return candidate
        # Common quote header:
        # "Company Name Factory QUOTE # 123"
        # Supplier is the company text before QUOTE / QUOTATION.
        for line in lines[:20]:
            quote_company_match = re.search(
                r"(?P<supplier>[A-Z][A-Za-z0-9&.,'() /-]+?\b(?:Factory|LLC|L\.L\.C|Ltd\.?|Limited|Trading|Company|Co\.?|Corporation|Corp))\s+(?:QUOTE|QUOTATION|QTN)\b",
                line,
                re.I,
            )
            if quote_company_match:
                candidate = self._clean_supplier_name(quote_company_match.group("supplier"))
                candidate_norm = _norm(candidate)
                if (
                    candidate
                    and len(candidate.split()) >= 2
                    and not self._is_buyer_or_delivery_line(candidate)
                    and not any(term in candidate_norm for term in {"address", "contact", "phone", "email", "date", "quote", "quotation"})
                ):
                    return candidate
        for line in lines[:20]:
            if re.match(r"^\s*(subject|to|cc|bcc|sent|from|follow up flag|flag status)\s*:", line, re.I):
                continue
            if re.match(r"^\s*(?:cc|bcc|cec)\s*:", line, re.I):
                continue
            if re.search(r"\b(?:doc\.?\s*no|document\s*no|request\s*no|ref(?:erence)?\s*no|po\s*no|lpo\s*no)\b", line, re.I):
                continue
            supplier_line = re.sub(
                r"\b(?:QUOTE|QUOTATION|QTN|REF|DATE)\s*#?.*$",
                "",
                line,
                flags=re.I,
            ).strip(" :-#")

            if not supplier_line:
                continue

            for match in re.finditer(
                r"\b(?P<supplier>(?:[A-Z][A-Za-z&.,'()/-]+\s+){1,8}(?:Factory|LLC|L\.L\.C|Ltd\.?|Limited|Trading|Company|Co\.?))\b",
                supplier_line,
                re.I,
            ):
                candidate = self._clean_supplier_name(match.group("supplier"))
                candidate_norm = _norm(candidate)

                if any(term in candidate_norm for term in {"date", "quote", "quotation", "address", "contact", "phone", "email"}):
                    continue

                if candidate and self._is_valid_document_supplier(candidate):
                    return candidate

        rejected_terms = {
            "quotation",
            "commercial offer",
            "purchase order",
            "proforma invoice",
            "invoice",
            "proposal",
            "project",
            "prepared",
            "prepared by",
            "customer",
            "billing address",
            "shipping address",
            "bill to",
            "ship to",
            "bank name",
            "bank details",
            "account name",
            "amount in words",
            "grand total",
            "make",
            "origin",
            "country of origin",
            "coo",
            "hs code",
            "warranty",
            "authorised signatory",
            "authorized signatory",
            "certificate",
            "certificates",
            "description of goods",
            "qty pcs",
            "sum usd",
            "aries group",
            "aries marine",
            "subject",
            "follow up",
            "flagged",
            "teamwork",
            "our works",
            "cooperation",
        }

        company_pattern = re.compile(
            r"\b(?:"
            r"llc|l\.l\.c|ltd|limited|company|co\.?|corporation|corp|"
            r"trading|technologies|technical|supplies|services|"
            r"industries|industrial|enterprises|fze|fzc|"
            r"engineering|electronics|electrical|oilfield|marine"
            r")\b",
            re.I,
        )

        legal_suffix_pattern = re.compile(
            r"\b(?:llc|l\.l\.c|ltd|limited|fze|fzc)\b",
            re.I,
        )

        def valid_candidate(value):
            cleaned = self._clean_supplier_name(value)

            if not cleaned:
                return None

            normalized = _norm(cleaned)
            if re.match(r"^\s*(subject|to|cc|bcc|sent|from|follow up flag|flag status)\s*:", str(value or ""), re.I):
                return None
            if any(term in normalized for term in rejected_terms):
                return None

            if self._is_buyer_or_delivery_line(cleaned):
                return None

            if "@" in cleaned or "www" in normalized:
                return None

            if re.search(
                r"\b(?:phone|mobile|tel|trn|vat|po box|warehouse|"
                r"street|tower|date)\b",
                normalized,
                re.I,
            ):
                return None

            if len(cleaned) > 160:
                return None

            if len(re.findall(r"\d", cleaned)) >= 4:
                return None

            if not company_pattern.search(cleaned):
                return None

            if not self._is_valid_document_supplier(cleaned):
                return None

            return cleaned

        candidates = []

        # Normal single-line candidates.
        for index, line in enumerate(lines):
            cleaned = valid_candidate(line)

            if not cleaned:
                continue

            score = 60

            if index <= 20:
                score += 25

            if legal_suffix_pattern.search(cleaned):
                score += 30

            candidates.append((score, index, cleaned))

        # Generic multi-line company reconstruction.
        #
        # When a legal/company suffix appears on one line, look backwards
        # for a short company-name fragment. This handles layouts where a
        # logo/company name and "Electronic Services Ltd." are separated.
        for index, line in enumerate(lines):
            cleaned_tail = self._clean_supplier_name(line)

            if not cleaned_tail or not company_pattern.search(cleaned_tail):
                continue

            if any(term in _norm(cleaned_tail) for term in rejected_terms):
                continue

            if not (
                legal_suffix_pattern.search(cleaned_tail)
                or re.search(
                    r"\b(?:electronics|electrical|engineering|services)\b",
                    cleaned_tail,
                    re.I,
                )
            ):
                continue

            for previous_index in range(max(0, index - 14), index):
                prefix = self._clean_supplier_name(lines[previous_index])

                if not prefix:
                    continue

                prefix_normalized = _norm(prefix)
                if prefix_normalized.startswith(("brand ", "make ", "model ")):
                    continue
                if any(term in prefix_normalized for term in {"brand", "make", "origin", "india", "china", "usa", "uk"}):
                    continue

                if any(term in prefix_normalized for term in rejected_terms):
                    continue

                if self._is_buyer_or_delivery_line(prefix):
                    continue

                if "@" in prefix or re.search(r"\d", prefix):
                    continue

                # Prefer short uppercase/logo-like company fragments.
                if not (
                    1 <= len(prefix.split()) <= 6
                    and (
                        prefix.upper() == prefix
                        or re.search(
                            r"\b(?:marine|technical|technology|trading|"
                            r"industrial|industries|group)\b",
                            prefix,
                            re.I,
                        )
                    )
                ):
                    continue

                combined = re.sub(
                    r"\s+",
                    " ",
                    f"{prefix} {cleaned_tail}",
                ).strip()

                cleaned_combined = valid_candidate(combined)

                if not cleaned_combined:
                    continue

                score = 105

                if previous_index <= 20:
                    score += 20

                if legal_suffix_pattern.search(cleaned_combined):
                    score += 25

                candidates.append(
                    (
                        score,
                        previous_index,
                        cleaned_combined,
                    )
                )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item[0],
                -item[1],
                len(item[2]),
            ),
            reverse=True,
        )

        return candidates[0][2]


    def _supplier_from_pdf_header_ocr(self, file_obj):
        try:
            import pypdfium2
            import pytesseract
            from PIL import ImageOps, ImageEnhance, ImageFilter
        except ImportError:
            return None

        try:
            pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

            file_obj.seek(0)
            pdf = pypdfium2.PdfDocument(file_obj)
            if len(pdf) == 0:
                return None

            page = pdf[0]
            bitmap = page.render(scale=4).to_pil().convert("RGB")
            width, height = bitmap.size

            crops = [
                bitmap.crop((0, 0, width, int(height * 0.35))),
                bitmap.crop((0, 0, int(width * 0.55), int(height * 0.35))),
                bitmap.crop((0, 0, width, int(height * 0.50))),
            ]

            rejected = {
                "quotation", "quote", "date", "valid until", "customer", "rfq",
                "project", "aries marine", "aries group", "delivery location",
                "po box", "dubai", "payment", "validity", "tel", "phone",
                "email", "www", "certificate", "certificates", "information",
                "description", "description of goods", "qty", "pcs", "sum usd",
                "unit price", "total", "amount",
            }

            for crop in crops:
                gray = ImageOps.grayscale(crop)
                gray = ImageEnhance.Contrast(gray).enhance(2.2)
                gray = gray.filter(ImageFilter.SHARPEN)

                text = pytesseract.image_to_string(gray, config="--psm 6") or ""
              

                lines = [line.strip(" :-\t") for line in text.splitlines() if line.strip()]
                for line in lines[:12]:
                    normalized = _norm(line)
                    compact = re.sub(r"[^a-z]", "", normalized)
                    if any(term in compact for term in {"quotation", "commercialoffer", "purchaseorder", "invoice"}):
                        continue
                    legal_line_match = re.search(
                        r"(?P<supplier>[A-Z][A-Z0-9&.,'() /-]{2,}?\s+(?:L\.?L\.?C|LLC|LTD|LIMITED|PVT\s+LTD|FZE|FZCO|TRADING|COMPANY|CO\.?))\b",
                        line,
                        re.I,
                    )
                    if legal_line_match:
                        cleaned_candidate = self._clean_supplier_name(legal_line_match.group("supplier"))
                        cleaned_candidate = re.sub(
                            r"\b(?:PO\s*Box|P\.?\s*O\.?\s*Box|Email|E-mail|Website|Tel|Phone)\b.*$",
                            "",
                            cleaned_candidate,
                            flags=re.I,
                        ).strip(" :-,")
                        if self._is_valid_document_supplier(cleaned_candidate):
                            return cleaned_candidate
                    if not normalized or any(term in normalized for term in rejected):
                        continue
                    if re.search(r"\b\d{3,}\b", normalized):
                        continue
                    if len(normalized.split()) < 2:
                        continue
                    if not re.search(
                        r"\b("
                        r"llc|l\.l\.c|trading|technical|technologies|systems|"
                        r"supplies|services|company|co|fze|ltd|limited|"
                        r"est|establishment|oilfield|marine|solutions|"
                        r"carpentry|packaging|manufactures|manufacturing|manufacturer"
                        r")\b",
                        normalized,
                        re.I,
                    ):
                        continue

                    # Reject OCR lines where the company name was merged
                    # with a document heading.
                    if any(
                        heading in normalized
                        for heading in {
                            "quotation",
                            "commercial offer",
                            "purchase order",
                            "invoice",
                            "proforma invoice",
                        }
                    ):
                        continue

                    cleaned_candidate = self._clean_supplier_name(line)
                    cleaned_candidate = re.sub(
                        r"^\s*(?:[A-Za-z]{1,3}\s+){1,3}(?=[A-Z][A-Z&.,'() /-]{4,}$)",
                        "",
                        cleaned_candidate,
                    ).strip()
                    tail_company_match = re.search(
                        r"\b(?P<supplier>[A-Z][A-Z&.' -]{2,}?\s+"
                        r"(?:CARPENTRY|PACKAGING|MANUFACTURES|MANUFACTURING|"
                        r"MANUFACTURER|FACTORY|TRADING|LLC|L\.L\.C|LTD|LIMITED))\b",
                        cleaned_candidate,
                    )
                    if tail_company_match:
                        cleaned_candidate = tail_company_match.group("supplier").strip()
                       
                        cleaned_candidate = re.sub(
                            r"^\s*ALL\s+(?=AQRAB\b)",
                            "AL ",
                            cleaned_candidate,
                            flags=re.I,
                        ).strip()
                    if not self._is_valid_document_supplier(cleaned_candidate):
                        continue

                    return cleaned_candidate

            return None
        except Exception as exc:
        
            return None
        finally:
            try:
                file_obj.seek(0)
            except Exception:
                pass
    def _document_fields_from_text(self, text):
        fields, meta = {}, {}

        lines = [
            _fix_pdf_text(line).strip()
            for line in text.splitlines()
            if line.strip()
        ]

        category = _detect_category(text)

        supplier_candidates = []

        for line_number, line in enumerate(lines, start=1):
            if re.search(r"\b(?:doc\.?\s*no|document\s*no|request\s*no|ref(?:erence)?\s*no|po\s*no|lpo\s*no)\b", line, re.I):
                continue

            candidate = self._clean_supplier_name(line)

            if self._is_valid_document_supplier(candidate):
                score = 70

                normalized_candidate = _norm(candidate)

                if re.search(
                    r"\b(?:llc|l l c|ltd|limited|company|co|fze|fzc)\b",
                    normalized_candidate,
                    re.I,
                ):
                    score += 12

                if line_number <= 12:
                    score += 4

                # Footer company names are common in commercial offers.
                if line_number >= max(1, len(lines) - 8):
                    score += 8

                # Avoid selecting bank account names when another company
                # identity is available.
                previous_line = (
                    _norm(lines[line_number - 2])
                    if line_number > 1
                    else ""
                )

                if "account name" in previous_line:
                    score -= 15

                supplier_candidates.append(
                    (
                        score,
                        line_number,
                        candidate,
                    )
                )

        if supplier_candidates:
            supplier_candidates.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                ),
                reverse=True,
            )

            supplier_value = supplier_candidates[0][2]

            fields["supplier"] = supplier_value
            fields["supplier_name"] = supplier_value

            meta["supplier"] = {
                "confidence": supplier_candidates[0][0],
                "source": {
                    "type": "document_company_line",
                    "line": supplier_candidates[0][1],
                },
            }

            meta["supplier_name"] = meta["supplier"]
        supplier_email_match = re.search(
            r"\bE-?Mail\s*:\s*[^@\s<>]+@(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})",
            text,
            re.I,
        )
        if supplier_email_match:
            domain = supplier_email_match.group("domain").lower()
            parts = domain.split(".")
            company_part = parts[-3] if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org"} else parts[-2]
            if company_part not in {"mail", "email", "info", "sales", "admin", "accounts"}:
                supplier_from_email = company_part.replace("-", " ").replace("_", " ").upper()
                fields["supplier"] = supplier_from_email
                fields["supplier_name"] = supplier_from_email
                meta["supplier"] = {"confidence": 88, "source": {"type": "supplier_email_domain"}}
                meta["supplier_name"] = meta["supplier"]
        direct_date = re.search(
            r"\b(?:"
            r"Quote\s+Date|Quotation\s+Date|Invoice\s+Date|"
            r"Document\s+Date|Date"
            r")\s*:?\s*"
            r"("
            r"[A-Za-z]+\s+\d{1,2},?\s+\d{4}"
            r"|"
            r"\d{1,2}(?:st|nd|rd|th)?[,]?\s+[A-Za-z]+[,]?\s+\d{4}"
            r"|"
            r"\d{1,2}[-./][A-Za-z]{3,9}[-./]\d{2,4}"
            r"|"
            r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}"
            r")\b",
            text,
            re.I,
        )
        if direct_date:
            normalized_date = self._normalize_document_value("document_date", direct_date.group(1))
            if normalized_date:
                fields["document_date"] = normalized_date
                meta["document_date"] = {"confidence": 92, "source": {"type": "direct_date_label"}}
        header_email_supplier = self._supplier_from_email_like_headers(text)
        if header_email_supplier:
            fields["supplier"] = header_email_supplier
            fields["supplier_name"] = header_email_supplier
            meta["supplier"] = {"confidence": 92, "source": {"type": "email_header_domain"}}
            meta["supplier_name"] = meta["supplier"]
        sender_supplier = self._supplier_from_sender_email_domain(text)
        if sender_supplier and "supplier" not in fields:
            fields["supplier"] = sender_supplier
            fields["supplier_name"] = sender_supplier
            meta["supplier"] = {"confidence": 92, "source": {"type": "sender_email_domain"}}
            meta["supplier_name"] = meta["supplier"]
        footer_company_match = re.search(
            r"\b(?P<supplier>TECH\s+Automation)\b\s*\n\s*\+?\d",
            text,
            re.I,
        )
        if footer_company_match:
            supplier = self._clean_supplier_name(footer_company_match.group("supplier"))
            if supplier and self._is_valid_document_supplier(supplier):
                fields["supplier"] = supplier
                fields["supplier_name"] = supplier
                meta["supplier"] = {"confidence": 92, "source": {"type": "footer_company_before_contact"}}
                meta["supplier_name"] = meta["supplier"]
        if "supplier" not in fields:
            email_supplier = self._supplier_from_email_domain(text)
            if email_supplier:
                fields["supplier"] = email_supplier
                fields["supplier_name"] = email_supplier
                meta["supplier"] = {"confidence": 68, "source": {"type": "email_domain"}}
                meta["supplier_name"] = meta["supplier"]
        
        direct_currency = self._currency_from_text(text)
        if direct_currency:
            fields["currency"] = direct_currency
            meta["currency"] = {"confidence": 90, "source": {"type": "currency_text"}}
        for line_number, line in enumerate(lines, start=1):
            candidates = self._extract_key_value_candidates(line)
            for label, value in candidates:
                field, score = _match_field(label, DOCUMENT_SCHEMA)
                if not field or score < 70 or not value:
                    continue
                value = self._normalize_document_value(field, value)
                current_score = meta.get(field, {}).get(
                    "confidence",
                    0,
                )

                if field == "supplier":
                    value = self._clean_supplier_name(value)

                    if not self._is_valid_document_supplier(value):
                        continue

                if value and score > current_score:
                    fields[field] = value
                    meta[field] = {"confidence": score, "source": {"type": "label_value", "line": line_number, "label": label}}
                    if field == "buyer_name":
                        fields["customer_to"] = value
                        meta["customer_to"] = meta[field]
                    elif field == "supplier":
                        fields["supplier_name"] = value
                        meta["supplier_name"] = meta[field]
                    elif field in {"quotation_number", "purchase_order_number", "invoice_number", "lpo_number", "grn_number", "reference_number"} and "document_number" not in fields:
                        fields["document_number"] = value
                        meta["document_number"] = meta[field]
        supplier = fields.get("supplier")
        supplier_source_type = (meta.get("supplier", {}).get("source") or {}).get("type")
        if supplier and "@" in str(supplier):
            email_domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", str(supplier), re.I)
            supplier_from_domain = self._company_name_from_email_domain(email_domain_match.group(1)) if email_domain_match else None
            if supplier_from_domain:
                fields["supplier"] = supplier_from_domain
                fields["supplier_name"] = supplier_from_domain
                meta["supplier"] = {"confidence": 88, "source": {"type": "supplier_email_domain_cleanup"}}
                meta["supplier_name"] = meta["supplier"]
                supplier = supplier_from_domain
                supplier_source_type = "supplier_email_domain_cleanup"

        if (
            supplier
            and supplier_source_type not in {"email_header_domain", "sender_email_domain", "email_domain"}
            and (
                self._is_buyer_or_delivery_line(supplier)
                or not self._is_valid_document_supplier(supplier)
            )
        ):
            fields.pop("supplier", None)
            fields.pop("supplier_name", None)
            meta.pop("supplier", None)
            meta.pop("supplier_name", None)
        footer_legal_supplier = None
        for index, line in enumerate(lines):
            if not re.search(r"\b(?:E-?mail|E\s*:|Email)\b|@", line, re.I):
                continue

            for previous_line in reversed(lines[max(0, index - 8):index]):
                candidate = self._clean_supplier_name(previous_line)
                candidate_norm = _norm(candidate)

                if not candidate:
                    continue
                if self._is_buyer_or_delivery_line(candidate):
                    continue
                if any(term in candidate_norm for term in {"address", "subject", "total", "payment", "delivery", "phone", "fax"}):
                    continue

                if re.search(r"\b(?:A\.?\s*Ş\.?|AS|LLC|L\.L\.C|LTD|LIMITED|FZE|FZC|CO\.?)\b", candidate, re.I):
                    footer_legal_supplier = candidate
                    break

            if footer_legal_supplier:
                break

        current_supplier_norm = _norm(fields.get("supplier") or "")
        if footer_legal_supplier and (
            "supplier" not in fields
            or "aries" in current_supplier_norm
            or self._is_buyer_or_delivery_line(fields.get("supplier"))
        ):
            fields["supplier"] = footer_legal_supplier
            fields["supplier_name"] = footer_legal_supplier
            meta["supplier"] = {"confidence": 94, "source": {"type": "footer_legal_company_near_email"}}
            meta["supplier_name"] = meta["supplier"]

        supplier_email_domain = ""
        supplier_email = fields.get("supplier_email") or ""
        email_domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", supplier_email, re.I)
        if email_domain_match:
            supplier_email_domain = email_domain_match.group(1).lower()

        footer_domain_supplier = None
        for index, line in enumerate(lines):
            if not (
                supplier_email_domain
                and supplier_email_domain in str(line or "").lower()
            ):
                continue

            for previous_line in reversed(lines[max(0, index - 10):index]):
                candidate = self._clean_supplier_name(previous_line)
                candidate_norm = _norm(candidate)

                if not candidate:
                    continue
                if self._is_buyer_or_delivery_line(candidate):
                    continue
                if any(term in candidate_norm for term in {"address", "subject", "total", "payment", "delivery", "phone", "fax", "istanbul", "turkiye", "turkey"}):
                    continue

                candidate_domain_text = (
                    str(candidate or "")
                    .lower()
                    .replace("ı", "i")
                    .replace("İ", "i")
                    .replace("ğ", "g")
                    .replace("Ğ", "g")
                    .replace("ü", "u")
                    .replace("Ü", "u")
                    .replace("ş", "s")
                    .replace("Ş", "s")
                    .replace("ö", "o")
                    .replace("Ö", "o")
                    .replace("ç", "c")
                    .replace("Ç", "c")
                )
                candidate_domain_text = re.sub(r"[^a-z0-9]+", "", candidate_domain_text)
                import unicodedata
                candidate_domain_text = unicodedata.normalize(
                    "NFKD",
                    str(candidate or ""),
                ).encode("ascii", "ignore").decode("ascii").lower()
                candidate_domain_text = re.sub(r"[^a-z0-9]+", "", candidate_domain_text)

                domain_hint = any(
                    part
                    for part in supplier_email_domain.split(".")
                    if len(part) >= 4 and part in candidate_domain_text
                )
                legal_hint = re.search(r"\b(?:as|a s|llc|l l c|ltd|limited|fze|fzc|co)\b", candidate_norm, re.I)

                if domain_hint or legal_hint:
                    footer_domain_supplier = candidate
                    break

            if footer_domain_supplier:
                break

        current_supplier_norm = _norm(fields.get("supplier") or "")
        current_matches_email_domain = any(
            part
            for part in supplier_email_domain.split(".")
            if len(part) >= 4 and part in current_supplier_norm.replace(" ", "")
        )
        if footer_domain_supplier and (
            "supplier" not in fields
            or "aries" in current_supplier_norm
            or "trading services" in current_supplier_norm
            or self._is_buyer_or_delivery_line(fields.get("supplier"))
            or not current_matches_email_domain
        ):
            fields["supplier"] = footer_domain_supplier
            fields["supplier_name"] = footer_domain_supplier
            meta["supplier"] = {"confidence": 95, "source": {"type": "footer_company_near_supplier_email_domain"}}
            meta["supplier_name"] = meta["supplier"]
        for_supplier = None
        for line in lines:
            match = re.search(
                r"^\s*FOR\s+(?P<supplier>[A-Z0-9&.,'() /-]+?\b(?:L\.?L\.?C\.?|LLC|LTD|LIMITED|TRADING|COMPANY|CO\.?))\s*$",
                line,
                re.I,
            )
            if not match:
                continue

            candidate = self._clean_supplier_name(match.group("supplier"))
            if (
                candidate
                and not self._is_buyer_or_delivery_line(candidate)
                and self._is_valid_document_supplier(candidate)
            ):
                for_supplier = candidate
                break

        if for_supplier:
            fields["supplier"] = for_supplier
            fields["supplier_name"] = for_supplier
            meta["supplier"] = {"confidence": 95, "source": {"type": "for_supplier_footer"}}
            meta["supplier_name"] = meta["supplier"]
        supplier_fallbacks = [
            ("header_layout", self._fallback_supplier_from_header, 72),
            ("footer_company", self._fallback_supplier_from_footer, 76),
            ("signature_company", self._supplier_from_signature, 70),
        ]

        for source_type, finder, confidence in supplier_fallbacks:
            if "supplier" in fields:
                break

            supplier = finder(lines, category) if source_type == "header_layout" else finder(lines)
            if (
                supplier
                and not self._is_buyer_or_delivery_line(supplier)
                and self._is_valid_document_supplier(supplier)
            ):
                fields["supplier"] = supplier
                fields["supplier_name"] = supplier
                meta["supplier"] = {"confidence": confidence, "source": {"type": source_type}}
                meta["supplier_name"] = meta["supplier"]
        
        
        currency_match = re.search(r"\b(AED|USD|EUR|SAR|INR|GBP)\b", text, re.I)
        if currency_match and "currency" not in fields:
            fields["currency"] = currency_match.group(1).upper()
            meta["currency"] = {"confidence": 55, "source": {"type": "semantic_text"}}
        direct_compact_date = re.search(
            r"\bDATE\s*[:：]\s*(\d{4}[./-]\d{1,2}[./-]\d{1,2})\b",
            text,
            re.I,
        )
        if direct_compact_date:
            normalized_date = self._normalize_document_value("document_date", direct_compact_date.group(1))
            if normalized_date:
                fields["document_date"] = normalized_date
                meta["document_date"] = {
                    "confidence": 94,
                    "source": {"type": "direct_date_compact_label"},
                }
        ref_date_match = re.search(
            r"\bRe\s+your\s+Ref\s*[:：]\s*(\d{4}[./-]\d{1,2}[./-]\d{1,2})\b",
            text,
            re.I,
        )
        if ref_date_match:
            normalized_date = self._normalize_document_value("document_date", ref_date_match.group(1))
            if normalized_date:
                fields["document_date"] = normalized_date
                meta["document_date"] = {
                    "confidence": 90,
                    "source": {"type": "reference_date_label"},
                }
        pricing_title = re.search(
            r"^\s*(?P<supplier>.+?)\s*[–-]\s*ARIES\s+MARINE\s*$",
            text,
            re.I | re.M,
        )
        if pricing_title and not fields.get("supplier"):
            supplier = self._clean_supplier_name(pricing_title.group("supplier"))
            if supplier:
                fields["supplier"] = supplier
                fields["supplier_name"] = supplier
                meta["supplier"] = {"confidence": 94, "source": {"type": "pricing_title_supplier"}}
                meta["supplier_name"] = meta["supplier"]

        pricing_month = re.search(r"\b(?:Pricing|Price\s*List)\s+V[\d.]+\s*[–-]\s*([A-Za-z]{3})[-\s]*(\d{2,4})\b", text, re.I)
        if pricing_month and not fields.get("document_date"):
            month_map = {
                "jan": "01", "feb": "02", "mar": "03", "apr": "04",
                "may": "05", "jun": "06", "jul": "07", "aug": "08",
                "sep": "09", "oct": "10", "nov": "11", "dec": "12",
            }
            month = month_map.get(pricing_month.group(1).lower()[:3])
            year = pricing_month.group(2)
            if len(year) == 2:
                year = "20" + year
            if month:
                fields["document_date"] = f"{year}-{month}-01"
                meta["document_date"] = {"confidence": 80, "source": {"type": "pricing_month_title"}}
        if "document_date" not in fields:
            fallback_date = self._document_date_fallback(lines)
            if fallback_date:
                fields["document_date"] = fallback_date
                meta["document_date"] = {"confidence": 72, "source": {"type": "date_fallback"}}
        supplier_email = fields.get("supplier_email") or ""
        supplier_domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", supplier_email, re.I)
        if supplier_domain_match:
            supplier_domain = supplier_domain_match.group(1).lower()
            supplier_domain_key = supplier_domain.split(".")[0]

            raw_lines = [
                re.sub(r"\s+", " ", raw_line).strip()
                for raw_line in str(text or "").splitlines()
                if str(raw_line or "").strip()
            ]
            raw_footer_supplier = None

            for index, raw_line in enumerate(raw_lines):
                if supplier_domain not in raw_line.lower():
                    continue

                for previous_line in reversed(raw_lines[max(0, index - 10):index]):
                    candidate = self._clean_supplier_name(previous_line)
                    if not candidate or self._is_buyer_or_delivery_line(candidate):
                        continue

                    import unicodedata
                    candidate_key = unicodedata.normalize(
                        "NFKD",
                        candidate,
                    ).encode("ascii", "ignore").decode("ascii").lower()
                    candidate_key = re.sub(r"[^a-z0-9]+", "", candidate_key)

                    if supplier_domain_key and supplier_domain_key in candidate_key:
                        raw_footer_supplier = candidate
                        break

                if raw_footer_supplier:
                    break

            current_supplier_norm = _norm(fields.get("supplier") or "")
            if raw_footer_supplier and (
                "aries" in current_supplier_norm
                or "trading services" in current_supplier_norm
                or self._is_buyer_or_delivery_line(fields.get("supplier"))
            ):
                fields["supplier"] = raw_footer_supplier
                fields["supplier_name"] = raw_footer_supplier
                meta["supplier"] = {"confidence": 96, "source": {"type": "raw_footer_company_near_supplier_email"}}
                meta["supplier_name"] = meta["supplier"]
        raw_footer_supplier = None
        raw_lines = [
            re.sub(r"\s+", " ", raw_line).strip()
            for raw_line in str(text or "").splitlines()
            if str(raw_line or "").strip()
        ]
        ignored_supplier_domains = {"arteqworld.com", "ariesmar.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}

        for index, raw_line in enumerate(raw_lines):
            domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", raw_line, re.I)
            if not domain_match:
                continue

            domain = domain_match.group(1).lower()
            if domain in ignored_supplier_domains:
                continue

            domain_key = domain.split(".")[0]
            if len(domain_key) < 4:
                continue

            for previous_line in reversed(raw_lines[max(0, index - 10):index]):
                candidate = self._clean_supplier_name(previous_line)
                if not candidate or self._is_buyer_or_delivery_line(candidate):
                    continue

                import unicodedata
                candidate_key = unicodedata.normalize(
                    "NFKD",
                    candidate,
                ).encode("ascii", "ignore").decode("ascii").lower()
                candidate_key = re.sub(r"[^a-z0-9]+", "", candidate_key)

                if domain_key in candidate_key:
                    raw_footer_supplier = candidate
                    break

            if raw_footer_supplier:
                break

        current_supplier_norm = _norm(fields.get("supplier") or "")
        if raw_footer_supplier and (
            "supplier" not in fields
            or "aries" in current_supplier_norm
            or "trading services" in current_supplier_norm
            or self._is_buyer_or_delivery_line(fields.get("supplier"))
        ):
            fields["supplier"] = raw_footer_supplier
            fields["supplier_name"] = raw_footer_supplier
            meta["supplier"] = {"confidence": 97, "source": {"type": "raw_footer_company_near_non_buyer_email"}}
            meta["supplier_name"] = meta["supplier"]
        final_supplier = fields.get("supplier") or fields.get("supplier_name")
        if final_supplier and "@" in str(final_supplier):
            email_domain_match = re.search(r"@([A-Z0-9.-]+\.[A-Z]{2,})", str(final_supplier), re.I)
            supplier_from_domain = self._company_name_from_email_domain(email_domain_match.group(1)) if email_domain_match else None
            if supplier_from_domain:
                fields["supplier"] = supplier_from_domain
                fields["supplier_name"] = supplier_from_domain
                meta["supplier"] = {"confidence": 88, "source": {"type": "final_supplier_email_domain_cleanup"}}
                meta["supplier_name"] = meta["supplier"]
        return fields, meta

    def _document_date_fallback(self, lines):
        labels = ("quote date", "quotation date", "po date", "purchase order date", "document date", "delivery date", "date")
        pattern = re.compile(
            r"\b("
            r"\d{1,2}(?:st|nd|rd|th)?[,]?\s+[A-Za-z]+[,]?\s*\d{4}"
            r"|"
            r"[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s*\d{4}"
            r"|"
            r"\d{1,2}[-./]\s*[A-Za-z]{3,9}[,.\s-]+\d{2,4}"
            r"|"
            r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}"
            r")\b",
            re.I,
        )
        for line in lines:
            normalized = _norm(line)
            if any(label in normalized for label in labels):
                match = pattern.search(line)
                if match:
                    candidate = re.sub(
                        r"(\d{1,2})(st|nd|rd|th)\b",
                        r"\1",
                        match.group(1),
                        flags=re.I,
                    )
                    candidate = re.sub(r",(?=\d{4})", ", ", candidate)
                    normalized_date = self._normalize_document_value("document_date", candidate)
                    if normalized_date:
                        return normalized_date
        for line in lines:
            match = pattern.search(line)
            if match:
                candidate = re.sub(
                    r"(\d{1,2})(st|nd|rd|th)\b",
                    r"\1",
                    match.group(1),
                    flags=re.I,
                )
                candidate = re.sub(r",(?=\d{4})", ", ", candidate)
                normalized_date = self._normalize_document_value("document_date", candidate)
                if normalized_date:
                    return normalized_date
        return None
    def _supplier_from_sender_email_domain(self, text):
        match = re.search(
            r"^\s*From:\s*(?P<name>.*?)\s*<[^@\s<>]+@(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})>",
            str(text or ""),
            re.I | re.M,
        )
        if not match:
            return None

        display_name = re.sub(r"\s+", " ", match.group("name") or "").strip(" :-|")
        domain = match.group("domain").lower()

        ignored_domains = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "aol.com", "proton.me", "protonmail.com",
        }
        if domain in ignored_domains:
            return None

        company_from_display = None
        if "|" in display_name:
            company_from_display = display_name.split("|")[-1].strip(" :-;")
            company_from_display = re.sub(
                r"\b(?:llc|l\.l\.c)\b",
                "LLC",
                company_from_display,
                flags=re.I,
            )
            company_from_display = re.sub(r"\s+", " ", company_from_display).strip()
        elif re.search(r"\b(?:llc|l\.l\.c|ltd|limited|fze|fzc|trading|technical|services|supplies|company|co\.?)\b", display_name, re.I):
            company_from_display = display_name

        if company_from_display and self._is_valid_document_supplier(company_from_display):
            return self._clean_supplier_name(company_from_display)

        domain_parts = domain.split(".")
        if len(domain_parts) < 2:
            return None

        company_part = domain_parts[-3] if domain_parts[-2] in {"co", "com", "net", "org"} and len(domain_parts) >= 3 else domain_parts[-2]
        if company_part in {"mail", "email", "info", "sales", "admin", "accounts"}:
            return None

        return self._company_name_from_email_domain(domain)
    def _supplier_from_email_domain(self, text):
        domains = re.findall(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", str(text or ""), re.I)
        ignored_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}

        candidates = []
        for domain in domains:
            domain = domain.lower().strip()
            if domain in ignored_domains:
                continue

            name = domain.split(".")[0]
            if name in {"info", "sales", "mail", "admin", "accounts"}:
                continue

            candidates.append(self._company_name_from_email_domain(domain))

        return candidates[0] if candidates else None

    def _company_name_from_email_domain(self, domain):
        domain = str(domain or "").lower().strip()
        if not domain:
            return None
        domain_parts = domain.split(".")
        if len(domain_parts) < 2:
            return None
        company_part = (
            domain_parts[-3]
            if domain_parts[-2] in {"co", "com", "net", "org"} and len(domain_parts) >= 3
            else domain_parts[-2]
        )
        if company_part in {"mail", "email", "info", "sales", "admin", "accounts"}:
            return None
        legal_suffix = {
            "com.tr": " LTD",
            "co.uk": " LTD",
            "com.cn": " CO LTD",
        }
        suffix = ""
        for domain_suffix, company_suffix in legal_suffix.items():
            if domain.endswith("." + domain_suffix):
                suffix = company_suffix
                break
        return (company_part.replace("-", " ").replace("_", " ").upper() + suffix).strip()
    def _supplier_from_signature(self, lines):
        blocked_terms = {"bank", "account", "account name", "iban", "branch", "swift", "vat", "registration"}
        for line in lines:
            normalized = _norm(line)
            if any(term in normalized for term in blocked_terms):
                continue

            match = re.search(
                r"\bfor\s+(.+?\b(?:llc|l\.l\.c|trading|technical|technologies|systems|supplies|company|co\.?|ltd|limited|fze|fzco)\b.*)$",
                line,
                re.I,
            )
            if not match:
                continue

            value = match.group(1).strip(" :-")
            if self._is_supplier_like(value):
                return self._clean_supplier_name(value)

        return None
    def _fallback_supplier_from_footer(self, lines):
        footer_lines = lines[-16:]

        rejected_terms = {
            "bank",
            "branch",
            "account",
            "account name",
            "iban",
            "vat",
            "registration",
            "proposal",
            "issued",
            "signed",
            "email",
            "www",
            "po box",
            "p.o. box",
            "address",
            "tel",
            "phone",
            "fax",
            "make",
            "origin",
            "country of origin",
            "coo",
            "hs code",
            "warranty",
        }

        company_terms = {
            "llc",
            "l.l.c",
            "ltd",
            "limited",
            "fze",
            "fzco",
            "trading",
            "technical",
            "technologies",
            "systems",
            "supplies",
         
            
        }
        for index, line in enumerate(footer_lines):
            normalized = _norm(line)
            if "@" not in line and "www" not in normalized:
                continue

            for previous in reversed(footer_lines[max(0, index - 3):index]):
                value = previous.strip(" :-")
                previous_norm = _norm(value)
                if not previous_norm:
                    continue
                if self._is_buyer_or_delivery_line(value):
                    continue
                if any(term in previous_norm for term in rejected_terms):
                    continue
                if re.search(r"\b\d{3,}\b", previous_norm):
                    continue
                if len(previous_norm.split()) >= 2 and any(ch.isalpha() for ch in value):
                    return self._clean_supplier_name(value)
        candidates = []
        for line in footer_lines:
            value = line.strip(" :-")
            normalized = _norm(value)

            if not normalized:
                continue
            if any(term in normalized for term in rejected_terms):
                continue
            if self._is_buyer_or_delivery_line(value):
                continue
            if not any(term in normalized for term in company_terms):
                continue
            if re.search(r"\b\d{3,}\b", normalized):
                continue

            candidates.append(value)

        if candidates:
            return self._clean_supplier_name(candidates[-1])

        return None
    def _looks_like_line_item_header(self, value):
        normalized = _norm(value)
        hits = sum(
            1
            for term in {"description", "product", "item", "part no", "uom", "qty", "quantity", "unit rate", "unit price", "total"}
            if term in normalized
        )
        return hits >= 3
    def _fallback_supplier_from_header(self, lines, category="OTHER"):
        rejected = {"quotation", "quote", "commercial offer", "purchase order", "invoice", "tax invoice", "commercial invoice", "lpo", "grn", "bill to", "po number", "po date", "rfq", "rfq#", "validity", "valid until", "date"}
        if category == "PURCHASE_ORDER":
            supplier = self._value_after_section_label(lines, {"order issued to", "vendor", "supplier"})
            if supplier:
                return supplier
        for line in lines[:10]:
            normalized = _norm(line)
            if not normalized or normalized in rejected:
                continue
            if re.match(r"^(quote|quote no|quotation no|document no|invoice no|po no|ref|reference|trn|valid until)\b", normalized):
                continue
            if any(label in normalized for label in {"bill to", "ship to", "po number", "po date", "purchase order number", "purchase order date"}):
                continue
            if self._looks_like_line_item_header(line):
                continue
            if any(term in normalized for term in NOISE_TERMS | TOTAL_STOP_TERMS):
                continue
            if self._is_buyer_or_delivery_line(line):
                continue
            if normalized.startswith(("rfq", "valid", "date", "customer", "attn", "project", "payment", "delivery")):
                continue
            if re.search(r"\b\d{2,}\b", normalized) and not re.search(r"\b(llc|l\.l\.c|trading|technical|technologies|supplies|services|company|co)\b", normalized, re.I):
                continue
            if len(normalized.split()) <= 1:
                continue

            cleaned_supplier = self._clean_supplier_name(line.strip(" :-"))
            if not self._is_supplier_like(cleaned_supplier):
                continue

            return cleaned_supplier
        return None

    def _value_after_section_label(self, lines, labels):
        for index, line in enumerate(lines):
            candidate = self._clean_supplier_name(line)
            normalized = _norm(candidate)
            for label in labels:
                if normalized == label:
                    return self._first_supplier_like_line(lines[index + 1:index + 5])
                if normalized.startswith(label + " "):
                    value = line[len(label):].strip(" :-")
                    if self._is_supplier_like(value):
                        return value
        return None

    def _first_supplier_like_line(self, lines):
        for line in lines:
            value = line.strip(" :-")
            if self._is_supplier_like(value):
                return value
        return None

    def _is_supplier_like(self, value):
        normalized = _norm(value)
        if self._is_buyer_or_delivery_line(value):
            return False
        if not normalized or len(normalized.split()) <= 1:
            return False
        if any(label in normalized for label in {"bill to", "ship to", "to aries", "aries group", "account name", "bank", "iban", "po number", "po date", "date", "quote", "commercial offer", "purchase order", "rfq", "rfq#", "validity", "valid until", "customer", "attn", "payment", "delivery"}):
            return False
        if any(term in normalized for term in NOISE_TERMS | TOTAL_STOP_TERMS):
            return False
        company_terms = {
            "llc",
            "l.l.c",
            "ltd",
            "limited",
            "fze",
            "fzco",
            "trading",
            "technical",
            "technologies",
            "systems",
            "supplies",
            "services",
            "factory",
            "equipment",
            "marine",
            "company",
            "co",
        }
        if re.search(r"\b\d{2,}\b", normalized):
            return False

        if any(term in normalized for term in company_terms):
            return True

        # Some supplier names in sales contracts are clean company/header names
        # without LLC/LTD suffix. Accept them only if they look like a real name,
        # not document labels, buyer text, totals, or table headings.
        words = normalized.split()
        if 2 <= len(words) <= 8 and any(len(word) >= 4 for word in words):
            return bool(re.search(r"[A-Za-z]", str(value or "")))

        return False

    def _is_buyer_or_delivery_line(self, value):
        normalized = _norm(value)
        blocked_terms = {
            "aries marine",
            "aries group",
            "m s aries",
            "group of companies",
            "to aries",
            "lukson",
            "delivery location",
            "industrial zone",
            "jurf industrial",
            "ajman",
            "al jerf",
            "al jarf",
            "exhibition showroom",
            "customer",
            "quote remarks",
            "po box",
            "p.o.box",
            "p.o box",
            "tower",
            "street",
            "floor",
            "warehouse",
            "abu dhabi",
            "u.a.e",
            "united arab emirates",
            "business bay",
            "po box",
            "dubai uae",
            "attn",
            "payment",
            "validity",
            "rfq",
            "commercial offer",
        }
        return any(term in normalized for term in blocked_terms)
    def _clean_supplier_name(self, value):
        text = re.sub(
            r"\s+",
            " ",
            str(value or ""),
        ).strip(" :-|,")

        if not text:
            return None

        # Remove document/signature prefixes.
        text = re.sub(
            r"^\s*(?:"
            r"for|from|by|supplier|vendor|seller|"
            r"quotation from|issued by|offered by"
            r")\s*[,:\-]?\s+",
            "",
            text,
            flags=re.I,
        ).strip(" :-|,")

        # Never accept text continuing into bank/total sections.
        text = re.split(
            r"\b(?:"
            r"bank details|bank name|branch|account no|account number|"
            r"ifsc|swift code|iban|total pi amount|amount in words|"
            r"grand total|terms and conditions|authorised signatory|"
            r"authorized signatory"
            r")\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" :-|,")

        # Remove trailing document/contact labels.
        text = re.split(
            r"\b(?:"
            r"quote\s*#|quote\s+no|quotation\s+no|po\s+number|po\s+date|"
            r"date|valid\s+until|trn|attn|attention|contact person|prepared by|"
            r"phone|tel|email|website|gst"
            r")\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" :-|,")

        return text or None
    def _extract_key_value_candidates(self, line):
        candidates = []
        if ":" in line:
            label, value = line.split(":", 1)
            candidates.append((label.strip(), value.strip()))
        tokens = re.split(r"\s{2,}|\t+", line)
        if len(tokens) >= 2:
            for index in range(0, len(tokens) - 1, 2):
                candidates.append((tokens[index].strip(), tokens[index + 1].strip()))
        return candidates

    def _normalize_document_value(self, field, value):
        cleaned = str(value).strip(" -:\t")
        if field in {"subtotal", "vat", "discount", "freight", "other_charges", "tax_amount", "grand_total"}:
            amount = _decimal(cleaned)
            return str(amount) if amount is not None else None
        if field in {"document_date", "delivery_date", "quotation_validity"}:
            try:
                cleaned = re.sub(
                    r"(\d{1,2})(st|nd|rd|th)\b",
                    r"\1",
                    cleaned,
                    flags=re.I,
                )
                cleaned = re.sub(r"\s*,\s*", ", ", cleaned).strip(" ,.-")
                current_month = date.today().month

                year_first = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", cleaned)
                if year_first:
                    year, first, second = map(int, year_first.groups())

                    current_year = date.today().year

                    # Past/future year: trust normal yyyy-mm-dd.
                    # Current-month inference only applies to current-year documents.
                    if year != current_year:
                        return date(year, first, second).isoformat()

                    # First assume yyyy-mm-dd.
                    month = first
                    day = second

                    # If assumed month is greater than current month, treat as yyyy-dd-mm.
                    if month > current_month and second <= 12:
                        day = first
                        month = second

                    return date(year, month, day).isoformat()

                day_or_month_first = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})", cleaned)
                if day_or_month_first:
                    first, second, year = map(int, day_or_month_first.groups())
                    if year < 100:
                        year += 2000

                    # First assume dd-mm-yyyy.
                    day = first
                    month = second

                    # If assumed month is greater than current month, treat as mm-dd-yyyy.
                    if month > current_month and first <= 12:
                        month = first
                        day = second

                    return date(year, month, day).isoformat()

                has_month_name = bool(re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", cleaned, re.I))
                return date_parser.parse(cleaned, dayfirst=not has_month_name).date().isoformat()
            except (ValueError, TypeError, OverflowError):
                return None
        if field == "currency":
            match = re.search(r"\b([A-Z]{3})\b", cleaned, re.I)
            return match.group(1).upper() if match else None
        return cleaned or None
