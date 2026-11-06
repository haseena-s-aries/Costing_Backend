import copy
import json
import logging
import mimetypes
import os
from django.conf import settings
import re
import time
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime

from django.utils.dateparse import parse_date
from google import genai
from google.genai import types

from api.models import GeminiUsageLog
from api.services.parser_service import VIRTUAL_HEADERS
from api.services.product_normalization_service import rate_matrix_display_name
from api.services.source_table_repair import restore_table_columns
from api.services.commercial_row_identity import is_true_duplicate
from api.services.source_reconciliation import finalize_pdf, finalize_msg_table, required_metadata
from api.services.msg_commercial_history import (
    extract_history as extract_msg_history,
    extract_inline_quote_history,
    reconcile_inline_quote_history,
    split_thread as split_msg_thread,
    is_commercial as is_msg_commercial,
    is_enquiry as is_msg_enquiry,
    context_image_ids,
)

logger = logging.getLogger(__name__)

GEMINI_VIRTUAL_HEADERS = [
    *VIRTUAL_HEADERS,
    "purchase_date",
    "source_reference",
    "document_number",
    "supplier_name",
    "buyer_name",
    "source_document_type",
    "source_document_number",
    "source_category",
    "price_reference_quality",
     "display_name",
    "extra_attributes",
    "parent_category",
    "_source_currency_missing",
]

# Canonical extraction prompts. Edit these directly — do not add a
# second definition of the same constant name elsewhere in this file.
CORE_EXTRACTION_RULES = """
- Extract every real priced PRODUCT/SERVICE/CHARGE.
- Preserve visible source values exactly.
- Never guess unreadable text, digits, decimals, dates, supplier, currency or prices.
- Return null when uncertain.
- Every separately visible priced row/amount must remain a separate item.
- Never merge or add separate commercial amounts.
- Bind description, quantity, rate and amount only to their own visual row/block.
- SOURCE CELL BOUNDARY IS AUTHORITATIVE. If a Product Description, Description, or Item Description cell contains technical details, specifications, model/part numbers, dimensions, materials, standards, certification details, voltage, ratings, or other identity text inside that SAME physical source cell/block, preserve the COMPLETE cell text in product_name. Do not shorten product_name by moving text from that same cell into specification, model, part_number, brand, remark, note, or extra_attributes. Populate dedicated fields separately only when the source exposes them as separate labelled columns/fields.
- extra_attributes preserves only separate visible columns/fields outside those cells, with original labels (including Model/Remark). Exclude document headers. Never invent labels from prose.
- For PRODUCT rows, if product_name contains Chinese/CJK or another non-English human-readable description and its meaning is unambiguous, it MUST be returned in English for search/display. Do not leave the human-readable description in Chinese merely to preserve source wording. Preserve the original wording separately as extra_attribute {label: "Original Product Name", value: <original>}. Never translate or modify model/part numbers, brands, standards, certification codes, voltage, dimensions, quantities, units, prices or technical English text.
- Preserve separately labelled commercial terms such as delivery/lead time, VAT inclusion, payment terms, warranty and quotation validity as document/item metadata or extra_attributes; never append them to product_name and never create extra products from them.
- Missing quantity/UOM/line amount stays null; serial numbers are not quantities. Prefer the printed English vendor company name when a bilingual company header exists, and preserve the full English legal company name rather than translating or shortening it. RMB is CNY.
- Tonnage/capacity/size written inside description must remain description text, not quantity.
- Use explicit quantity/rate/amount/currency/date values only.
- Ignore subtotal, VAT/tax/GST, discount, grand total, FOC/free and non-commercial supporting documents.
- Supplier must be the company providing the commercial price, not buyer/recipient.
- A standalone company name or heading at the top of an image/table is valid supplier evidence when it is clearly separate from the priced rows; propagate it to that document's items only. Do not borrow supplier metadata from another page or archive member.
- For cost-breakdown tables, classify each priced cost line as CHARGE (including item cost, shipping, insurance, handling and tax) rather than PRODUCT. Keep grand totals and currency equivalents as document-level totals, not extra product rows.
- For informal chat screenshots, extract standalone price statements even when no formal table exists. Preserve indicators such as rough/indicative, ex-stock/availability, VAT applicability, customer-provided materials, supplier-provided manpower/equipment and similar conditions in extra_attributes or remarks. If the chat clearly describes a service but gives no formal item label, use a concise source-backed service description; otherwise keep product_name null and retain the priced row for Review.
- Commercial document date must come from the actual commercial document/source section.
- Never use filename/upload/current date to create a commercial date.
- A rate/price matrix — a table where one row description has multiple numeric columns, each column headed by a different quantity, capacity, weight, tonnage, duration, or tier value — must be split into one item per column/cell. For each cell: repeat that row's description in product_name, set quantity to that column's tier value, set uom to the column header's unit (e.g. TON, KG, HR), and set both unit_price and line_total to that cell's value. Never merge multiple tier columns into a single item and never return only one representative column — every cell in the matrix is a separate priced item.
- When a table separates route columns as FROM AREA, TO, and AREA, combine them as "{FROM AREA} TO {AREA}" in product_name. Keep the word "TO" even when it is shown as a separate table header column.
- For rate/price matrices, create items only from visible row-heading plus column-heading cells. Do not invent combined route rows such as "and return" unless that exact row is visibly printed in the matrix.
- For rate/price matrices, create items only from visible row-heading plus column-heading cells. Do not invent combined route rows such as "and return" unless that exact row is visibly printed in the matrix.
- Return schema JSON only.
""".strip()

SCHEMA_FIELD_PROMPT = """
Return document fields:
document_type, category, supplier_name, document_date, currency, subtotal, grand_total, visible_priced_row_count.
Return currency_evidence at document and item level: the exact visible currency
code/name/symbol (e.g. AED or Dhs). If absent, return null currency and null
currency_evidence. Never infer currency from supplier, country, tax, language,
amount formatting or application defaults. Keep all priced rows for Review.
Return contains_handwriting: true when commercial descriptions, dates, quantities
or amounts are handwritten (not merely a signature). Also set contains_handwriting
on affected items. Extract ALL commercial rows, including uncertain handwriting;
never omit a row because it needs review. Preserve readable text, use null for
unreadable fields, and do not guess spelling, dates or numbers. Human review is
mandatory for handwritten content even when you are confident.

Return each item with:
row_type, extra_attributes, product_name, purchase_date, page_number, quantity,
uom, unit_price, line_total, currency, supplier_name, source_category.
Optional source columns such as model, specification, material, brand,
part number and remarks must be returned only in extra_attributes using the
original source label. Apply the SOURCE CELL BOUNDARY rule: technical text
in the same physical Description cell stays in product_name; only separate
labelled columns/fields become extra_attributes.
For a non-English description, provide a confident English description in
product_name and preserve the original wording as an extra_attribute labelled
Original Product Name.
Preserve separately labelled delivery, VAT inclusion, payment, warranty and
validity terms in metadata or extra_attributes.
""".strip()

GEMINI_EXTRACTION_PROMPT = f"""
Read the complete commercial document visually from page 1 to the last page.

{SCHEMA_FIELD_PROMPT}

Rules:
{CORE_EXTRACTION_RULES}
""".strip()

SPREADSHEET_PROMPT = f"""
Extract all commercial rows from the spreadsheet.

{SCHEMA_FIELD_PROMPT}

Rules:
{CORE_EXTRACTION_RULES}
- Preserve spreadsheet row/sheet context in source_reference where available.
- Keep parent/project headings in parent_category for following priced rows only when the source visibly presents them as a section heading. Do not use the document title, filename, generic product category, or words such as Tools/Commercial as parent_category.
- If multiple suppliers quote the same item, return one item per supplier price.
- If one supplier has multiple price-stage columns for the same item, return only the current/final stage when explicitly labeled.
- In supplier-comparison tables, preserve the original supplier-column positions. A blank supplier cell is null; never shift a later supplier's amount left. Each visible supplier price cell is the quoted amount for that supplier. Do not multiply it by quantity unless the source explicitly labels the cell as a unit rate and separately provides a total.
- Leading serial numbers such as 1, 2 or 21 are source row numbers, not item codes or prices. Preserve the full description, specification/model and quantity from the row.
""".strip()

SPREADSHEET_EXTRACTION_PROMPT = SPREADSHEET_PROMPT

PDF_PAGE_PROMPT = f"""
Read this single PDF page visually. Current page rows only; adjacent pages are context only.

{SCHEMA_FIELD_PROMPT}

Rules:
{CORE_EXTRACTION_RULES}
- Set page_number to the current original page for every returned item.
- visible_priced_row_count must exclude subtotal, tax, discounts, totals, FOC/free and supporting/non-commercial amounts.
- Context pages must never contribute their own commercial rows to the current page result.
- Supplier/date/currency belong to the CURRENT commercial section; never copy an adjacent section's header.
""".strip()

PDF_PAGE_EXTRACTION_PROMPT = PDF_PAGE_PROMPT
GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "category": {"type": "string"},
        "supplier_name": {"type": "string", "nullable": True},
        "document_date": {"type": "string", "nullable": True},
        "currency": {"type": "string", "nullable": True},
        "currency_evidence": {"type": "string", "nullable": True},
        "document_number": {"type": "string", "nullable": True},
        "subtotal": {"type": "string", "nullable": True},
        "grand_total": {"type": "string", "nullable": True},
        "visible_priced_row_count": {
            "type": "integer",
            "nullable": True,
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_type": {"type": "string"},
                    "parent_category": {"type": "string", "nullable": True},
                    "extra_attributes": {
                        "type": "array",
                        "nullable": True,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["label", "value"],
                        },
                    },
                    "product_name": {"type": "string", "nullable": True},
                    "purchase_date": {"type": "string", "nullable": True},
                    "source_reference": {"type": "string", "nullable": True},
                    "source_context": {"type": "string", "nullable": True},
                    "quantity": {"type": "string", "nullable": True},
                    "uom": {"type": "string", "nullable": True},
                    "unit_price": {"type": "string", "nullable": True},
                    "line_total": {"type": "string", "nullable": True},
                    "currency": {"type": "string", "nullable": True},
                    "currency_evidence": {"type": "string", "nullable": True},
                    "page_number": {"type": "integer", "nullable": True},
                    "supplier_name": {"type": "string", "nullable": True},
                    "buyer_name": {"type": "string", "nullable": True},
                    "source_document_type": {"type": "string", "nullable": True},
                    "source_document_number": {"type": "string", "nullable": True},
                    "source_category": {"type": "string", "nullable": True},
                },
                "required": [
                    "row_type",
                    "extra_attributes",
                    "product_name",
                    "purchase_date",
                    "quantity",
                    "uom",
                    "unit_price",
                    "line_total",
                    "currency",
                    "page_number",
                    "supplier_name",
                    "source_category",
                ],
            },
        },
    },
    "required": ["document_type", "category", "supplier_name", "document_date", "currency", "items"],
}
GEMINI_VERIFICATION_PROMPT = """
Verify only these suspicious extracted commercial records.
Return JSON only: {"items":[{"index":0,"corrected_fields":{},"verification_notes":[]}]}
Correct only fields that are clearly wrong or missing from supplied context. Use null when unavailable. Never invent values.
"""

MULTILINGUAL_PRODUCT_NORMALIZATION_PROMPT = """
Normalize only the supplied PRODUCT identities. Return JSON only:
{"items":[{"index":0,"english_product_name":"...","original_product_name":"..."}]}

Rules:
- Translate only the human-readable non-English product identity into concise English.
- Preserve the original product_name exactly in original_product_name.
- Do not translate or alter model numbers, part numbers, brands, standards,
  certification codes, numeric specifications, dimensions, units or prices.
- Do not include the technical specification block in english_product_name.
- If the meaning is uncertain, return english_product_name as null.
""".strip()


def _contains_cjk(value):
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(value or "")))

SUPPORTED_DOCUMENT_TYPES = {
    "invoice", "quotation", "purchase_order", "proforma_invoice",
    "price_list", "offer", "email_quote", "shipping_quote",
    "service_quote", "other",
}


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None) if usage else None
        if value is not None:
            return value
    return 0


def _safe_json(text):
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text or "{}")


def _filename_year_hint(original_file_name):
    text = str(original_file_name or "")

    match = re.search(
        r"(?<!\d)(20\d{2})(?:[01]\d[0-3]\d)?(?!\d)",
        text,
    )

    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None

def _date_matches_filename_year(date_value, original_file_name):
    """
    Preserve the date extracted from the document.

    The filename may contain a different year, but a filename is not
    source evidence and must never modify a visible commercial date.
    """
    return _valid_date(date_value)

def _best_visible_date_candidate(date_values, original_file_name):
    filename_year = _filename_year_hint(original_file_name)
    parsed_dates = []

    for value in date_values or []:
        parsed = _valid_date(value)

        if parsed:
            parsed_dates.append(parsed)

    if not parsed_dates:
        return None

    if filename_year:
        same_year_dates = [
            value
            for value in parsed_dates
            if str(value).startswith(f"{filename_year}-")
        ]

        if same_year_dates:
            return same_year_dates[0]

    return _date_matches_filename_year(
        parsed_dates[0],
        original_file_name,
    )
def _image_too_small_for_document(file_bytes, min_width=300, min_height=300):
    if not file_bytes:
        return True

    try:
        import io
        from PIL import Image

        with Image.open(io.BytesIO(file_bytes)) as image:
            width, height = image.size
    except Exception:
        return False

    return width < min_width or height < min_height


def _prepare_image_for_gemini(file_bytes, mime_type):
    """Validate and gently upscale small readable images for vision extraction.

    File dimensions are not a document-validity test: compact screenshots and
    exported tables can contain a complete quotation. The original bytes are
    still retained by the caller; only the Gemini payload is normalized.
    """
    if not file_bytes or not str(mime_type or "").startswith("image/"):
        return file_bytes, mime_type
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(file_bytes)) as source:
            image = source.convert("RGB")
            width, height = image.size
            if width < 1000 or height < 1000:
                scale = min(3.0, max(1000 / max(width, 1), 1000 / max(height, 1)))
                image = image.resize(
                    (max(width, int(width * scale)), max(height, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buffer = BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
    except Exception:
        logger.warning("Image preprocessing skipped; sending original bytes", exc_info=True)
        return file_bytes, mime_type


def _empty_non_document_image_result(reason):
    return {
        "document_type": "other",
        "category": "OTHER",
        "supplier_name": None,
        "document_date": None,
        "currency": None,
        "document_number": None,
        "subtotal": None,
        "grand_total": None,
        "visible_priced_row_count": 0,
        "items": [],
        "_gemini_needs_review": True,
        "_gemini_missing_pages": [],
        "_gemini_quality_issues": [reason],
    }

def _extract_pdf_text(file_bytes, max_pages=None):
    if not file_bytes:
        return ""

    import io

    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = pdf.pages[:max_pages] if max_pages else pdf.pages
            return "\n".join(
                page.extract_text() or ""
                for page in pages
            )
    except Exception:
        pass

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        pages = reader.pages[:max_pages] if max_pages else reader.pages
        return "\n".join(
            page.extract_text() or ""
            for page in pages
        )
    except Exception:
        return ""
def _estimate_page_count(file_bytes, mime_type):
    if mime_type == "application/pdf":
        try:
            from pypdf import PdfReader
            import io

            return len(PdfReader(io.BytesIO(file_bytes)).pages)
        except Exception:
            try:
                import io
                import pdfplumber

                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    return len(pdf.pages)
            except Exception:
                return None
    if str(mime_type or "").startswith("image/"):
        return 1
    return None


def _split_pdf_pages(file_bytes):
    """Split a PDF into individual single-page PDF byte blobs so each page
    can be sent to Gemini as its own explicitly-labeled part. This is more
    reliable than a text instruction alone at getting the model to look at
    every page of a multi-invoice/multi-receipt bundle."""
    import io

    reader_writer = None
    try:
        from pypdf import PdfReader, PdfWriter
        reader_writer = (PdfReader, PdfWriter)
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter
            reader_writer = (PdfReader, PdfWriter)
        except ImportError:
            logger.warning(
                "Gemini PDF page split skipped: neither pypdf nor PyPDF2 is "
                "installed. Run `pip install pypdf` to enable per-page splitting "
                "for multi-invoice/multi-receipt bundles. Falling back to single blob."
            )
            return None

    PdfReader, PdfWriter = reader_writer
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            pages.append(buffer.getvalue())
        return pages
    except Exception as exc:
        logger.warning("Gemini PDF page split failed, falling back to single blob: %s", exc)
        return None
def _clean_text(value):
    if value in (None, ""):
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()

    if text.lower() in {"null", "none", "unknown", "n/a", "na", "-"}:
        return None

    return text or None


def _decimal(value):
    if value in (None, ""):
        return None
    raw = str(value)

    cleaned = re.sub(
        r"(?:"
        r"\b(?:AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b"
        r"|US\$|S\$|A\$|AU\$|C\$|CA\$|CN¥|JP¥"
        r"|[$€£¥₹元]"
        r"|د\.إ|د\.ك|د\.ب|ر\.س|ر\.ق|ر\.ع\.?"
        r"|\bDHS?\b|\bDIRHAMS?\b"
        r"|\bRS\.?\b|\bRUPEES?\b"
        r"|\bSR\b|\bQR\b|\bKD\b"
        r"|\bR\.?O\.?\b|\bBD\b"
        r"|\bRMB\b"
        r")",
        "",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", cleaned):
        cleaned = cleaned.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+,\d{1,3}", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d+,\d{1,3}", cleaned):
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    text = re.sub(r"[^\d.\-]", "", cleaned)
    if text in ("", "-", ".", "-."):
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _decimal_comma_decimal(value):
    if value in (None, ""):
        return None
    raw = str(value)

    cleaned = re.sub(
        r"(?:"
        r"\b(?:AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b"
        r"|US\$|S\$|A\$|AU\$|C\$|CA\$|CN¥|JP¥"
        r"|[$€£¥₹元]"
        r"|د\.إ|د\.ك|د\.ب|ر\.س|ر\.ق|ر\.ع\.?"
        r"|\bDHS?\b|\bDIRHAMS?\b"
        r"|\bRS\.?\b|\bRUPEES?\b"
        r"|\bSR\b|\bQR\b|\bKD\b"
        r"|\bR\.?O\.?\b|\bBD\b"
        r"|\bRMB\b"
        r")",
        "",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()
    if not re.fullmatch(r"-?\d+,\d{1,3}", cleaned):
        return None
    try:
        return Decimal(cleaned.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _money_decimal(value, row_type=None, product_name=None):
    """
    Parse the visible commercial amount exactly.

    Never change decimal position based on row type, number length,
    product name, or digit pattern.
    """
    return _decimal(value)


def _valid_currency(value):
    if not value:
        return None

    text = str(value).strip().upper()

    symbol_map = {
        # AED
        "د.إ": "AED",
        "DHS": "AED",
        "DH": "AED",
        "DIRHAM": "AED",
        "DIRHAMS": "AED",

        # USD
        "$": "USD",
        "US$": "USD",

        # EUR
        "€": "EUR",

        # GBP
        "£": "GBP",

        # INR
        "₹": "INR",
        "RS": "INR",
        "RS.": "INR",
        "RUPEE": "INR",
        "RUPEES": "INR",

        # SAR
        "ر.س": "SAR",
        "SR": "SAR",

        # QAR
        "ر.ق": "QAR",
        "QR": "QAR",

        # KWD
        "د.ك": "KWD",
        "KD": "KWD",

        # OMR
        "ر.ع.": "OMR",
        "ر.ع": "OMR",
        "RO": "OMR",
        "R.O.": "OMR",

        # BHD
        "د.ب": "BHD",
        "BD": "BHD",

        # JPY is only unambiguous when the country/code is explicit.  A bare
        # yen symbol is shared by multiple currencies and is handled as
        # unresolved below.
        "JP¥": "JPY",

        # CNY
        "CN¥": "CNY",
        "RMB": "CNY",
        "元": "CNY",

        # SGD
        "S$": "SGD",

        # AUD
        "A$": "AUD",
        "AU$": "AUD",

        # CAD
        "C$": "CAD",
        "CA$": "CAD",
    }

    if text in symbol_map:
        return symbol_map[text]

    # Never infer JPY/CNY from a bare yen glyph.  This guard belongs here so
    # callers cannot recreate a false currency during later normalization.
    if text in {"¥", "￥"}:
        return None

    # Standard ISO currency code
    supported_codes = {
        "AED",
        "USD",
        "EUR",
        "GBP",
        "INR",
        "SAR",
        "QAR",
        "KWD",
        "OMR",
        "BHD",
        "JPY",
        "CNY",
        "RMB",
        "SGD",
        "AUD",
        "CAD",
    }

    if text in supported_codes:
        return "CNY" if text == "RMB" else text

    return None
def _safe_business_date(parsed):
    if not parsed:
        return None

    if hasattr(parsed, "date"):
        parsed = parsed.date()

    current_year = datetime.now().year

    if parsed.year < 2000 or parsed.year > current_year + 2:
        return None

    return parsed.isoformat()
def _valid_date(value):
    if not value:
        return None

    text = str(value).strip()

    if not text:
        return None

    # -------------------------------------------------
    # BASIC NORMALIZATION
    # -------------------------------------------------

    # Normalize unicode dashes.
    text = (
        text.replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
    )

    # Jan. 15,2026 -> Jan 15, 2026
    text = re.sub(
        r"\b([A-Za-z]{3})\.\s*",
        r"\1 ",
        text,
    )

    # 18th / 21st / 22nd / 23rd -> 18 / 21 / 22 / 23
    text = re.sub(
        r"\b(\d{1,2})(?:st|nd|rd|th)\b",
        r"\1",
        text,
        flags=re.I,
    )

    # Ordinal dates such as 19th.Jan.2026 use a dot between day
    # and month as well as after the month abbreviation.
    text = re.sub(r'(?<=\d)\.(?=[A-Za-z])', ' ', text)

    # Normalize spaces after comma.
    text = re.sub(
        r",\s*",
        ", ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    # A two-digit leading component is not an ISO year.  Django's
    # parse_date() accepts values such as 18-06-26 as YYYY-MM-DD and turns
    # them into 2018-06-26, which is unsafe for commercial documents.
    # Handle DD-MM-YY/MM-DD-YY below before trying the generic ISO parser.
    has_short_numeric_date = bool(re.fullmatch(
        r"\d{1,2}[./-]\d{1,2}[./-]\d{2}", text
    ))

    # -------------------------------------------------
    # STANDARD ISO
    # -------------------------------------------------
    parsed = None if has_short_numeric_date else parse_date(text)

    if parsed:
        return _safe_business_date(parsed)

    # -------------------------------------------------
    # YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD
    # -------------------------------------------------
    year_first = re.fullmatch(
        r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})",
        text,
    )

    if year_first:
        year = int(year_first.group(1))
        month = int(year_first.group(2))
        day = int(year_first.group(3))

        try:
            return _safe_business_date(
                datetime(
                    year,
                    month,
                    day,
                )
            )
        except ValueError:
            return None

    # -------------------------------------------------
    # COMPACT YYYYMMDD
    #
    # 20260618
    # -------------------------------------------------
    compact_year_first = re.fullmatch(
        r"(\d{4})(\d{2})(\d{2})",
        text,
    )

    if compact_year_first:
        year = int(
            compact_year_first.group(1)
        )
        month = int(
            compact_year_first.group(2)
        )
        day = int(
            compact_year_first.group(3)
        )

        try:
            return _safe_business_date(
                datetime(
                    year,
                    month,
                    day,
                )
            )
        except ValueError:
            return None

    # -------------------------------------------------
    # NUMERIC DD/MM/YYYY OR MM/DD/YYYY
    #
    # 18/06/2026
    # 06/18/2026
    # -------------------------------------------------
    numeric_date = re.fullmatch(
        r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})",
        text,
    )

    if numeric_date:
        first = int(numeric_date.group(1))
        second = int(numeric_date.group(2))
        year = int(numeric_date.group(3))

        candidates = []

        if first <= 12 and second > 12:
            # MM/DD/YYYY
            candidates.append(
                (year, first, second)
            )

        elif first > 12 and second <= 12:
            # DD/MM/YYYY
            candidates.append(
                (year, second, first)
            )

        else:
            current_month = datetime.now().month
            current_year = datetime.now().year

            if year == current_year:
                if second <= current_month:
                    # DD/MM/YYYY
                    candidates.append(
                        (year, second, first)
                    )
                else:
                    # MM/DD/YYYY
                    candidates.append(
                        (year, first, second)
                    )
            else:
                # Different year: keep DD/MM as default.
                candidates.append(
                    (year, second, first)
                )

        for (
            candidate_year,
            candidate_month,
            candidate_day,
        ) in candidates:
            try:
                return _safe_business_date(
                    datetime(
                        candidate_year,
                        candidate_month,
                        candidate_day,
                    )
                )
            except ValueError:
                continue

        return None

    # -------------------------------------------------
    # NUMERIC WITH 2-DIGIT YEAR
    #
    # 18/06/26
    # 06/18/26
    # -------------------------------------------------
    numeric_short_year = re.fullmatch(
        r"(\d{1,2})[./-](\d{1,2})[./-](\d{2})",
        text,
    )

    if numeric_short_year:
        first = int(
            numeric_short_year.group(1)
        )

        second = int(
            numeric_short_year.group(2)
        )

        short_year = int(
            numeric_short_year.group(3)
        )

        # Commercial documents in this system are modern.
        year = 2000 + short_year

        candidates = []

        if first <= 12 and second > 12:
            candidates.append(
                (year, first, second)
            )

        elif first > 12 and second <= 12:
            candidates.append(
                (year, second, first)
            )

        else:
            current_month = datetime.now().month
            current_year = datetime.now().year

            if year == current_year:
                if second <= current_month:
                    # DD/MM/YYYY
                    candidates.append(
                        (year, second, first)
                    )
                else:
                    # MM/DD/YYYY
                    candidates.append(
                        (year, first, second)
                    )
            else:
                # Different year: keep DD/MM as default.
                candidates.append(
                    (year, second, first)
                )

        for (
            candidate_year,
            candidate_month,
            candidate_day,
        ) in candidates:
            try:
                return _safe_business_date(
                    datetime(
                        candidate_year,
                        candidate_month,
                        candidate_day,
                    )
                )
            except ValueError:
                continue

        return None

    # -------------------------------------------------
    # COMPACT 8-DIGIT DATE
    #
    # 18062026 -> DDMMYYYY
    # 06182026 -> MMDDYYYY when second part > 12
    # -------------------------------------------------
    compact_numeric = re.fullmatch(
        r"(\d{2})(\d{2})(\d{4})",
        text,
    )

    if compact_numeric:
        first = int(
            compact_numeric.group(1)
        )

        second = int(
            compact_numeric.group(2)
        )

        year = int(
            compact_numeric.group(3)
        )

        candidates = []

        if first <= 12 and second > 12:
            candidates.append(
                (year, first, second)
            )

        elif first > 12 and second <= 12:
            candidates.append(
                (year, second, first)
            )

        else:
            current_month = datetime.now().month
            current_year = datetime.now().year

            if year == current_year:
                if second <= current_month:
                    # DD/MM/YYYY
                    candidates.append(
                        (year, second, first)
                    )
                else:
                    # MM/DD/YYYY
                    candidates.append(
                        (year, first, second)
                    )
            else:
                # Different year: keep DD/MM as default.
                candidates.append(
                    (year, second, first)
                )

        for (
            candidate_year,
            candidate_month,
            candidate_day,
        ) in candidates:
            try:
                return _safe_business_date(
                    datetime(
                        candidate_year,
                        candidate_month,
                        candidate_day,
                    )
                )
            except ValueError:
                continue

        return None

    # -------------------------------------------------
    # TEXTUAL DATE FORMATS
    # -------------------------------------------------
    formats = (
        # Day month year
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%b/%Y",
        "%d/%B/%Y",

        # Month day year
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%b-%d-%Y",
        "%B-%d-%Y",

        # Year month day
        "%Y %b %d",
        "%Y %B %d",
        "%Y-%b-%d",
        "%Y-%B-%d",

        # 2 digit year textual
        "%d %b %y",
        "%d %B %y",
        "%d-%b-%y",
        "%d-%B-%y",
        "%b %d, %y",
        "%B %d, %y",
    )

    for fmt in formats:
        try:
            parsed = datetime.strptime(
                text,
                fmt,
            ).date()

            # Normalize 2-digit year interpretation.
            if parsed.year < 2000:
                parsed = parsed.replace(
                    year=parsed.year + 100
                )

            return _safe_business_date(parsed)

        except ValueError:
            continue

    return None
def _normalize_category(value):
    if not value:
        return None

    text = re.sub(
        r"[^A-Z0-9]+",
        "_",
        str(value).strip().upper(),
    ).strip("_")

    aliases = {
        "PO": "PURCHASE_ORDER",
        "PURCHASEORDER": "PURCHASE_ORDER",
        "PURCHASE_ORDER": "PURCHASE_ORDER",

        "QUOTE": "QUOTATION",
        "QUOTATION": "QUOTATION",
        "PRICE_QUOTE": "QUOTATION",
        "QUOTATION_OFFER": "QUOTATION",
        "QUOTATION_OFFER_QUOTE": "QUOTATION",
        "COMMERCIAL_QUOTATION": "QUOTATION",
        "SALES_QUOTATION": "QUOTATION",
        "COMMERCIAL_OFFER": "QUOTATION",
        "SALES_OFFER": "QUOTATION",
        "OFFER": "QUOTATION",
        "SALES_CONTRACT": "QUOTATION",

        "PROFORMA_INVOICE": "INVOICE",
        "COMMERCIAL_INVOICE": "INVOICE",
        "TAX_INVOICE": "INVOICE",
        "INVOICE": "INVOICE",

        "LOCAL_PURCHASE_ORDER": "LPO",
        "LPO": "LPO",

        "GOODS_RECEIPT_NOTE": "GRN",
        "GOODS_RECEIVED_NOTE": "GRN",
        "GRN": "GRN",

        "PRICE_LIST": "PRICE_LIST",
        "OTHER": "OTHER",
    }

    return aliases.get(text, text) if aliases.get(text, text) in {"QUOTATION", "PURCHASE_ORDER", "INVOICE", "GRN", "LPO", "PRICE_LIST", "OTHER"} else "OTHER"


def _prefer_specific_category(current, verified):
    """Prevent a weak OTHER verification from erasing commercial evidence."""
    current_value = _normalize_category(current) or "OTHER"
    verified_value = _normalize_category(verified) or "OTHER"
    if verified_value == "OTHER" and current_value != "OTHER":
        return current_value
    if current_value == "OTHER" and verified_value != "OTHER":
        return verified_value
    return verified_value if verified_value != "OTHER" else current_value

def _normalize_document_type(value):
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "po": "purchase_order",
        "quote": "quotation",
        "price_quote": "quotation",
        "commercial_offer": "offer",
        "sales_contract": "quotation",
        "freight_quote": "shipping_quote",
        "freight_quotation": "shipping_quote",
    }
    text = aliases.get(text, text)
    return text if text in SUPPORTED_DOCUMENT_TYPES else "other"
def _category_missing_or_other(value):
    return str(value or "").strip().upper() in {
        "",
        "OTHER",
        "UNKNOWN",
        "NONE",
        "NULL",
    }


def _promote_invoice_category_from_content(file_bytes, result):
    if not isinstance(result, dict):
        return result

    text = ""

    try:
        text = _extract_pdf_text(file_bytes, max_pages=3) or ""
    except Exception:
        text = ""

    combined = " ".join(
        str(value or "")
        for value in [
            text[:12000],
            result.get("document_type"),
            result.get("category"),
            result.get("document_number"),
            result.get("subtotal"),
            result.get("grand_total"),
        ]
    )

    has_invoice_text = re.search(
        r"\b(tax\s+invoice|invoice\s*(?:no|number|date)?|bill\s+to|billed\s+to|receipt)\b",
        combined,
        re.I,
    )

    has_quote_text = re.search(
        r"\b(quotation|quote|commercial\s+offer|purchase\s+order|rfq|enquiry)\b",
        combined,
        re.I,
    )

    has_invoice_shape = (
        bool(result.get("items"))
        and bool(result.get("document_number"))
        and bool(result.get("supplier_name"))
        and bool(result.get("currency"))
        and bool(result.get("subtotal") or result.get("grand_total"))
        and not has_quote_text
    )

    if not has_invoice_text and not has_invoice_shape:
        return result

    result["document_type"] = "invoice"
    result["category"] = "INVOICE"

    for item in result.get("items") or []:
        if _category_missing_or_other(item.get("source_document_type")):
            item["source_document_type"] = "invoice"

        if _category_missing_or_other(item.get("source_category")):
            item["source_category"] = "INVOICE"

    return result
def _remove_summary_total_rows(result):
    if not isinstance(result, dict):
        return result

    cleaned_items = []

    for item in result.get("items") or []:
        name = str(
            item.get("product_name")
            or item.get("description")
            or item.get("display_name")
            or ""
        ).strip()

        unit_price = str(item.get("unit_price") or "").strip()
        row_type = str(item.get("row_type") or "").strip().upper()

        is_summary_row = bool(
            re.search(
                r"\b("
                r"invoice\s+total|"
                r"grand\s+total|"
                r"net\s+total|"
                r"sub\s*total|"
                r"total\s+amount|"
                r"amount\s+due|"
                r"balance\s+due|"
                r"vat\s+total|"
                r"tax\s+total"
                r")\b",
                name,
                re.I,
            )
        )

        if is_summary_row:
            logger.warning(
                "Removed summary total row from Gemini result: %s",
                name,
            )
            continue

        if (
            row_type == "PRODUCT"
            and unit_price in {"", "-", "—", "NA", "N/A", "None", "null"}
            and re.search(r"\b(total|subtotal|amount\s+due|balance)\b", name, re.I)
        ):
            logger.warning(
                "Removed product-looking summary row from Gemini result: %s",
                name,
            )
            continue

        cleaned_items.append(item)

    result["items"] = cleaned_items
    return result
def _normalize_extra_attributes(value):
    if not value:
        return []

    if isinstance(value, dict):
        value = [
            {"label": key, "value": val}
            for key, val in value.items()
        ]

    if not isinstance(value, list):
        return []

    blocked_labels = {
        "supplier",
        "supplier name",
        "buyer",
        "buyer name",
        "quantity",
        "qty",
        "uom",
        "unit",
        "unit price",
        "price",
        "line total",
        "total",
        "amount",
        "currency",
        "date",
        "document date",
        "po",
        "purchase order",
        "vessel",
        "vessle",
        "our tec",
        "reference number",
        "document number",
        "quotation reference",
        "invoice reference",
        "rfq reference",
    }

    output = []
    seen = set()

    # Canonical English display labels for common multilingual source labels.
    # Values are preserved verbatim; this does not infer attributes from prose.
    label_aliases = {
        "型号": "Model",
        "型号名称": "Model",
        "品牌": "Brand",
        "规格": "Specification",
        "规格型号": "Specification",
        "备注": "Remark",
    }

    for entry in value:
        if not isinstance(entry, dict):
            continue

        label = re.sub(r"\s+", " ", str(entry.get("label") or "")).strip(" |:-")
        attr_value = re.sub(r"\s+", " ", str(entry.get("value") or "")).strip(" |:-")

        if not label or not attr_value:
            continue

        label = label_aliases.get(label, label)

        normalized_label = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        if normalized_label in blocked_labels:
            continue

        key = (label.lower(), attr_value.lower())
        if key in seen:
            continue

        seen.add(key)
        output.append({"label": label, "value": attr_value})

    return output


# Remarks values that name a cost-breakdown component (e.g. "External Cost",
# "Manhour", "Profit"). They distinguish otherwise identical description rows.
_COST_COMPONENT_REMARK_RE = re.compile(
    r"^(?:external|internal|material|labou?r|manufacturing|overhead|transport(?:ation)?)?\s*"
    r"(?:cost|costs|man[\s-]?hours?|man[\s-]?days?|profit|margin|mark[\s-]?up|overheads?)$",
    re.I,
)


def _is_cost_component_remark(value):
    return bool(_COST_COMPONENT_REMARK_RE.match(re.sub(r"\s+", " ", str(value or "")).strip(" .:-")))


def _has_cost_component_remark(item):
    for attr in (item or {}).get("extra_attributes") or []:
        if not isinstance(attr, dict):
            continue
        label = re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip()
        if label in {"remarks", "remark", "notes", "note"} and _is_cost_component_remark(attr.get("value")):
            return True
    return False


def build_canonical_display_name(item):
    """Build the only authoritative display name from semantic fields."""
    from api.services.commercial_attribute_policy import hidden_display_attribute as commercial_field
    clean = lambda value: re.sub(r"\s+", " ", str(value or "")).strip(" |:-")
    parent = clean(item.get("parent_category"))
    product = clean(item.get("product_name") or item.get("product_service_name"))
    parts = []
    if parent:
        parts.append(parent)
    if product:
        parts.append(product)
    hidden = {
        "quantity", "qty", "uom", "unit", "unit price", "unit rate", "rate",
        "price", "currency", "date", "document date", "supplier", "supplier name",
        "vat", "tax", "gst", "discount", "subtotal", "total", "amount",
        "total price", "total price usd", "unit price usd",
        "delivery", "delivery period", "delivery time", "lead time",
        "payment", "validity",
        "original product name",
    }
    product_key = re.sub(r"[^a-z0-9]+", " ", product.casefold()).strip()
    for attr in _normalize_extra_attributes(item.get("extra_attributes")):
        label = clean(attr.get("label"))
        value = clean(attr.get("value"))
        label_key = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
        if not value or attr.get('role') == 'financial' or commercial_field(label) or re.search(r'\b(?:price|cost|discount|hike|amount)\b', label_key) or label_key in hidden or value.casefold() in product_key:
            continue
        if re.search(r"\b(?:delivery|payment|valid(?:ity)?|vat|tax|freight|discount|lead\s*time|availability)\b", value, re.I):
            continue
        if label_key in {"model"}:
            parts.append(value)
        elif label_key in {"specification", "specifications", "model specification", "model / specification"}:
            parts.append(f"Specification: {value}")
        elif label_key in {"warranty"}:
            parts.append(f"Warranty: {value}")
        elif label_key in {"material", "type location", "type", "location", "brand", "make", "standard", "grade", "size"}:
            parts.append(f"{label}: {value}")
        elif label_key in {"remarks", "remark", "notes", "note"}:
            if _is_cost_component_remark(value) or re.search(r"(?:iso\s*\d+|iec\s*\d+|dnv|iacs|model|spec(?:ification)?|type|grade|class|swl|part\s*(?:no|number)|\b\d+\s*(?:mm|cm|m|v|kw|hz|t)\b|\b[a-z]*\d+[a-z]*\b)", value, re.I):
                parts.append(value)
        else:
            # Preserve source-labelled technical columns, including unfamiliar
            # model/specification headers, rather than silently dropping them.
            parts.append(f"{label}: {value}")
    return " | ".join(dict.fromkeys(parts))


def _inherit_cost_breakdown_quantity(items):
    """Cost-breakdown rows (Remarks: External Cost/Manhour/Profit) of one offer
    share a single Qty cell that is printed only on the first row. Copy it to
    sibling rows with the same description when exactly one quantity exists."""
    def blank(value):
        return str(value if value is not None else "").strip() in {"", "-"}

    groups = {}
    for item in items:
        if not _has_cost_component_remark(item):
            continue
        key = (
            str(item.get("product_name") or "").split("|", 1)[0].strip().lower(),
            str(item.get("supplier_name") or "").strip().lower(),
            str(item.get("page_number") or "").strip(),
            str(item.get("purchase_date") or "").strip(),
        )
        groups.setdefault(key, []).append(item)

    for group in groups.values():
        quantities = {str(i.get("quantity")).strip() for i in group if not blank(i.get("quantity"))}
        if len(quantities) != 1:
            continue
        quantity = next(iter(quantities))
        source = next(i for i in group if not blank(i.get("quantity")))
        for item in group:
            if blank(item.get("quantity")):
                item["quantity"] = quantity
                if blank(item.get("uom")) and not blank(source.get("uom")):
                    item["uom"] = source.get("uom")


def _apply_item_display_names(result):
    items = list(result.get("items") or [])

    if not items:
        return result

    _inherit_cost_breakdown_quantity(items)
    # extra_attributes are dropped by later stages, so keep the cost-component
    # remark in product_name (e.g. "MCU Panel Cost - External Cost").
    for item in items:
        name = re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip()
        if not name or not _has_cost_component_remark(item):
            continue
        for attr in item.get("extra_attributes") or []:
            label = re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip()
            remark = re.sub(r"\s+", " ", str(attr.get("value") or "")).strip(" .:-")
            if (
                label in {"remarks", "remark", "notes", "note"}
                and _is_cost_component_remark(remark)
                and remark.casefold() not in name.casefold()
            ):
                item["product_name"] = f"{name} - {remark}"
                break

    def clean(value):
        return re.sub(r"\s+", " ", str(value or "")).strip(" |:-")
    def route_ton_display_suffix(item, product_name):
        quantity = clean(item.get("quantity"))
        uom = clean(item.get("uom") or item.get("unit_of_measurement"))

        if not quantity or not uom:
            return None

        if not re.search(r"\btons?\b", uom, re.I):
            return None

        if not re.search(r"\bto\b", product_name, re.I):
            return None

        source_type = clean(
            item.get("source_document_type")
            or result.get("document_type")
        ).lower()
        source_category = clean(
            item.get("source_category")
            or result.get("category")
        ).lower()

        if source_type not in {"price_list", "quotation"} and source_category not in {"price_list", "quotation"}:
            return None

        qty_text = quantity
        if quantity.isdigit() and int(quantity) < 10:
            qty_text = quantity.zfill(2)

        return f"{qty_text} TON"
    def append_label(parts, label, value):
        label = clean(label)
        value = clean(value)

        if not label or not value:
            return

        text = f"{label}: {value}"
        existing = " | ".join(parts).lower()

        if text.lower() in existing:
            return

        parts.append(text)

    def duration_from_item(item):
        attrs = _normalize_extra_attributes(item.get("extra_attributes"))
        for attr in attrs:
            if re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip() == "duration":
                value = clean(attr.get("value"))
                if value:
                    return value
        quantity = clean(item.get("quantity"))
        match = re.search(
            r"\b\d+(?:\.\d+)?\s*(?:HR|HRS|HOUR|HOURS)\b"
            r"(?:\s*\d+(?:\.\d+)?\s*(?:MIN|MINS|MINUTE|MINUTES)\b)?",
            quantity,
            re.I,
        )
        return clean(match.group(0)) if match else None

    duration_sources = [
        (item, duration_from_item(item))
        for item in items
        if duration_from_item(item)
        and str(item.get("row_type") or "").upper() in {"SERVICE", "CHARGE"}
    ]
    for target in items:
        target_name = clean(target.get("product_name")).lower()
        if not re.search(r"\b(?:launch\s+hire|crane\s+charge)\b", target_name):
            continue
        if "tariff" in target_name or re.search(r"\bper\s+hr\b|\bper\s+hour\b", target_name):
            continue
        target_attrs = _normalize_extra_attributes(target.get("extra_attributes"))
        has_duration_attribute = any(
            re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip() == "duration"
            for attr in target_attrs
        )
        target_duration = duration_from_item(target)
        if target_duration and not has_duration_attribute:
            target_attrs.append({"label": "Duration", "value": target_duration})
            target["extra_attributes"] = target_attrs
            continue
        if has_duration_attribute:
            continue
        for source, duration in duration_sources:
            if source is target:
                continue
            if target.get("page_number") not in (None, "") and source.get("page_number") not in (None, "") and str(target.get("page_number")) != str(source.get("page_number")):
                continue
            if target.get("supplier_name") and source.get("supplier_name") and _supplier_identity_key(target.get("supplier_name")) != _supplier_identity_key(source.get("supplier_name")):
                continue
            if target.get("purchase_date") and source.get("purchase_date") and str(target.get("purchase_date")) != str(source.get("purchase_date")):
                continue
            attrs = _normalize_extra_attributes(target.get("extra_attributes"))
            attrs.append({"label": "Duration", "value": duration})
            target["extra_attributes"] = attrs
            break

    def normalize_attr_label(value):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    hidden_display_attribute_labels = {
        # These values already have dedicated core fields in the virtual
        # sheet/API. Keeping them in display_name would duplicate the same
        # information in the product identity.
        "charge per", "charge-per", "per", "uom", "unit", "unit of measurement",
        "quantity", "qty", "unit price", "unit rate", "rate", "price",
        "currency", "date", "document date", "supplier", "supplier name",
        "rate basis", "additional rate", "applies after",
        "vat", "vat amount", "tax", "tax amount", "gst",
        "discount", "discount percentage", "dis", "subtotal", "total", "total amount", "grand total",
        "net amount", "line amount", "extended amount",
        "sl no", "s no", "sr no", "serial no", "serial number",
        "line no", "line number", "row no", "row number",
        "total price", "total price usd", "unit price usd",
        "dely period", "delivery period", "delivery", "delivery time",
        "lead time", "leadtime", "lead period",
    }

    def is_identity_attribute(label, value="", product=""):
        normalized = normalize_attr_label(label)
        if re.search(r'\b(?:price|cost|discount|hike|amount)\b', normalized):
            return False
        value_text = normalize_attr_label(value)
        # REMARKS is a source column, not a semantic category.  Technical
        # standards/models/specifications in it identify the product, while
        # delivery/payment/logistics notes do not.
        if normalized in {"remarks", "remark", "notes", "note"}:
            if _is_cost_component_remark(value):
                return True
            if re.search(r"\b(?:delivery|warehouse|entrance\s+fee|payment|valid(?:ity)?|vat|tax|freight|availability|available|warranty|lead\s*time|discount|without\s+warehouse)\b", value_text, re.I):
                return False
            return bool(re.search(r"(?:iso\s*\d+|iec\s*\d+|dnv|iacs|model|spec(?:ification)?|type|grade|class|swl|part(?:\s+no|number)?|[a-z]*\d+[a-z]*|\d+\s*(?:mm|cm|m|v|kw|hz|t))", value_text, re.I))
        if re.search(
            r"\b(?:item\s*cost|shipping|freight|insurance|handling|tax|vat|gst|source\s*total|converted\s*total|grand\s*total|subtotal|amount|quote|quoted\s*price)\b",
            normalized,
            re.I,
        ):
            return False
        return normalized not in hidden_display_attribute_labels

    for item in items:
        product_name = clean(
            item.get("product_name")
            or item.get("product_service_name")
        )

        parent = clean(item.get("parent_category"))
        extra_attributes = _normalize_extra_attributes(
            item.get("extra_attributes")
        )

        parts = []

        if parent and product_name and parent.lower() not in product_name.lower():
            parts.append(parent)

        if product_name:
            parts.append(product_name)

        product_lower = product_name.lower()

        for attr in extra_attributes:
            label_text = clean(attr.get("label"))
            value_text = clean(attr.get("value"))
            if attr.get('role') == 'financial':
                continue

            if normalize_attr_label(label_text) in hidden_display_attribute_labels:
                continue

            # Original-language identity is retained for audit/search but is
            # not a second display identity. Model is a concise identity
            # suffix, so keep its value without adding a redundant label.
            if normalize_attr_label(label_text) == "original product name":
                continue
            if normalize_attr_label(label_text) == "model":
                if value_text and value_text.lower() not in product_lower:
                    parts.append(value_text)
                continue

            if not is_identity_attribute(label_text, value_text, product_name):
                continue

            if value_text and value_text.lower() in product_lower:
                continue

            if normalize_attr_label(label_text) in {"remarks", "remark", "notes", "note"}:
                parts.append(value_text)
            else:
                append_label(parts, label_text, value_text)

        matrix_display = rate_matrix_display_name(item, items)
        if matrix_display:
            parts = [matrix_display]

        item["extra_attributes"] = extra_attributes
        # Always rebuild from the finalized semantic fields.  A stale model
        # display_name must never override source-mapped parent/name/spec.
        item["display_name"] = build_canonical_display_name(item)

    result["items"] = items
    return result

def _reconcile_placeholder_commercial_items(result, original_file_name=""):
    result = _canonicalize_item_suppliers(result)
    result = _normalize_narrative_service_identities(result)
    # Final reconciliation also removes equivalent SERVICE recovery rows.
    # This runs after page merges and recovery helpers, immediately before
    # virtual-sheet/ImportRow generation.
    result = _deduplicate_equivalent_commercial_items(
        result,
        original_file_name,
    )
    items = list(result.get("items") or [])
    placeholders = {"for reference", "for your reference", "reference", "price reference", "as follows", "details below"}
    placeholder_indexes = [index for index, item in enumerate(items) if str(item.get("product_name") or "").strip().lower().rstrip(":") in placeholders]
    merged_items, matches, removed = list(items), [], []

    def amount(item):
        return str(item.get("unit_price") or item.get("line_total") or "").replace(",", "").strip()

    for placeholder_index in placeholder_indexes:
        placeholder = items[placeholder_index]
        price = amount(placeholder)
        candidates = [
            (index, item) for index, item in enumerate(items)
            if index != placeholder_index
            and str(item.get("row_type") or "PRODUCT").upper() in {"PRODUCT", "SERVICE"}
            and str(item.get("product_name") or "").strip().lower().rstrip(":") not in placeholders
            and price
            and amount(item) == price
            # A placeholder is not identity evidence. Require explicit same-row
            # provenance and compatible values before binding it to a name.
            and is_true_duplicate(
                {**placeholder, "product_name": item.get("product_name"),
                 "row_type": item.get("row_type")}, item,
            )
        ]
        if len(candidates) != 1:
            continue
        target_index, target = candidates[0]
        for field in ("quantity", "uom", "unit_price", "line_total", "currency", "supplier_name"):
            if placeholder.get(field) not in (None, ""):
                target[field] = placeholder[field]
        matches.append({"placeholder_index": placeholder_index, "target_index": target_index})
        removed.append(placeholder_index)

    result["items"] = [item for index, item in enumerate(merged_items) if index not in set(removed)]
    logger.info("FINAL commercial reconciliation file=%s pre_reconcile_items=%s placeholder_items_found=%s contextual_product_matches=%s merged_items=%s removed_placeholder_items=%s post_reconcile_items=%s",
                original_file_name, len(items), len(placeholder_indexes), len(matches), len(matches), len(removed), len(result["items"]))
    return result
def _source_section_key(item):
    source = str(item.get("source_reference") or "").strip().lower()
    source = re.sub(r"[:\- ]?r(?:ow)?\d+\b", "", source)
    source = re.sub(r"[:\- ]?\d+\b$", "", source)

    return (
        str(item.get("supplier_name") or "").strip().lower(),
        str(item.get("source_document_number") or "").strip().lower(),
        str(item.get("source_document_type") or "").strip().lower(),
        str(item.get("source_category") or "").strip().lower(),
        source,
        str(item.get("page_number") or "").strip(),
    )


def _harmonize_item_dates_by_source_section(result):
    """
    Fill missing item dates only when every known item in the same
    source section agrees on exactly one date.

    Never overwrite an already extracted valid date.
    """
    items = list((result or {}).get("items") or [])
    groups = {}

    for item in items:
        groups.setdefault(
            _source_section_key(item),
            [],
        ).append(item)

    for grouped_items in groups.values():
        known_dates = {
            _valid_date(item.get("purchase_date"))
            for item in grouped_items
            if _valid_date(item.get("purchase_date"))
        }

        # We can safely propagate only one unambiguous date.
        if len(known_dates) != 1:
            continue

        source_date = next(iter(known_dates))

        for item in grouped_items:
            # IMPORTANT: fill missing only.
            # Never replace a valid extracted date.
            if not _valid_date(item.get("purchase_date")):
                item["purchase_date"] = source_date

    return result

def _normalize_handwritten_transport_description(item):
    """Conservatively clean dates/tiers from scanned transport SERVICE names."""
    if str(item.get("row_type") or "").upper() != "SERVICE":
        return item
    text = str(item.get("product_name") or "").strip()
    if not text or not re.search(r"\bto\b", text, re.I):
        return item
    purchase_date = _valid_date(item.get("purchase_date"))
    leading_date = re.match(r"^\s*(\d{2}/\d{2}/\d{4}|\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})[.\s:-]+", text)
    if leading_date and purchase_date:
        token = leading_date.group(1)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            normalized_leading = token
        else:
            day, month, year = re.split(r"[-/]", token)
            normalized_leading = f"{year}-{month}-{day}"
        if normalized_leading == purchase_date:
            text = text[leading_date.end():].strip()

    quantity = _decimal(item.get("quantity"))
    uom = str(item.get("uom") or item.get("unit_of_measurement") or "").strip().upper()
    tier_match = re.search(r"\s+(\d+(?:\.\d+)?)\s*(TON|TONS)\s*$", text, re.I)
    if tier_match and quantity is not None and uom in {"TON", "TONS"}:
        try:
            if _decimal(tier_match.group(1)) == quantity:
                text = text[:tier_match.start()].strip(" -.,")
        except Exception:
            pass
    item["product_name"] = text
    if item.get("display_name"):
        item["display_name"] = text
    return item


def _reconcile_item_year(item_date, page_date_candidates, sibling_item_dates, document_candidates):
    """Correct only an OCR year when same-page evidence supports it."""
    item_date = _valid_date(item_date)
    if not item_date:
        return item_date, None
    try:
        item_year, item_month, item_day = item_date.split("-")
    except ValueError:
        return item_date, None

    candidates = []
    for value in list(page_date_candidates or []) + list(document_candidates or []):
        value = _valid_date(value)
        if value:
            candidates.append(value)
    sibling_dates = [_valid_date(value) for value in sibling_item_dates or []]
    sibling_dates = [value for value in sibling_dates if value]

    direct = [
        value for value in candidates
        if value[5:] == item_date[5:] and value[:4] != item_year
    ]
    supported_years = {}
    for value in sibling_dates:
        if value[5:] == item_date[5:] and value[:4] != item_year:
            supported_years[value[:4]] = supported_years.get(value[:4], 0) + 3
        elif value[5:7] == item_month and value[:4] != item_year:
            supported_years[value[:4]] = supported_years.get(value[:4], 0) + 1
    for value in candidates:
        if value[:4] != item_year and value[5:7] == item_month:
            supported_years[value[:4]] = supported_years.get(value[:4], 0) + 2

    if direct:
        supported_years[direct[0][:4]] = supported_years.get(direct[0][:4], 0) + 5
    if not supported_years:
        return item_date, None
    year, score = max(supported_years.items(), key=lambda pair: pair[1])
    if score < 3:
        return item_date, None
    new_date = f"{year}-{item_month}-{item_day}"
    return new_date, new_date if new_date != item_date else None


def normalize_result(result):
    normalized = dict(result or {})
    from api.services.visual_review_policy import require_visual_review
    require_visual_review(normalized)
    if normalized.get('_physical_pdf_complete') or normalized.get('_source_attachment_finalized'):
        return copy.deepcopy(normalized)
    normalized["document_type"] = _normalize_document_type(normalized.get("document_type") or normalized.get("category"))
    if normalized["document_type"] == "proforma_invoice":
        normalized["category"] = "INVOICE"

    elif normalized.get("category"):
        normalized["category"] = "QUOTATION" if normalized["document_type"] in {"quotation", "offer"} else (_normalize_category(normalized.get("category")) or "OTHER")

    else:
        category_map = {
            "invoice": "INVOICE",
            "quotation": "QUOTATION",
            "offer": "QUOTATION",
            "purchase_order": "PURCHASE_ORDER",
            "proforma_invoice": "INVOICE",
            "price_list": "PRICE_LIST",
        }

        normalized["category"] = category_map.get(
            normalized["document_type"],
            "OTHER",
        )
    normalized["supplier_name"] = _normalize_supplier_name(
        normalized.get("supplier_name")
    )
    supplier_name = normalized["supplier_name"]

    buyer_names = {
        _clean_text(normalized.get("buyer_name")),
    }

    for item in normalized.get("items") or []:
        buyer = _clean_text(item.get("buyer_name"))
        if buyer:
            buyer_names.add(buyer)

    buyer_names = {name.lower() for name in buyer_names if name}

    if supplier_name:
        supplier_lower = supplier_name.lower().strip()

        if any(
            supplier_lower == buyer
            or supplier_lower in buyer
            or buyer in supplier_lower
            for buyer in buyer_names
        ):
            normalized["supplier_name"] = None
    normalized["document_date"] = _valid_date(normalized.get("document_date"))
    normalized["currency"] = _valid_currency(normalized.get("currency"))
    normalized["document_number"] = _clean_text(normalized.get("document_number"))
    normalized["subtotal"] = _clean_text(normalized.get("subtotal"))
    normalized["grand_total"] = _clean_text(normalized.get("grand_total"))
    normalized["items"] = list(normalized.get("items") or [])
    for item in normalized["items"]:
        item_date = _valid_date(item.get("purchase_date"))
        # If an OCR/parser path already expanded a source DD-MM-YY value as
        # YYYY-MM-DD (for example 18-06-26 -> 2018-06-26), recover it from a
        # corroborated document date without changing unrelated historical
        # dates. The signature is: parsed year suffix = document day and
        # parsed day = document year suffix, with the same month.
        document_date = normalized.get("document_date")
        if item_date and document_date:
            try:
                item_dt = datetime.strptime(item_date, "%Y-%m-%d")
                document_dt = datetime.strptime(document_date, "%Y-%m-%d")
            except ValueError:
                item_dt = document_dt = None
            if (
                item_dt
                and document_dt
                and item_dt.year % 100 == document_dt.day
                and item_dt.day == document_dt.year % 100
                and item_dt.month == document_dt.month
            ):
                item_date = document_date
        item["purchase_date"] = item_date
        item["row_type"] = _canonical_row_type(item.get("row_type"), item)
        item["currency"] = _valid_currency(item.get("currency")) or normalized.get("currency")
        _normalize_handwritten_transport_description(item)
        item.update(_normalize_transport_service(item))

    # Tax/VAT/GST lines are document totals/metadata, not importable
    # products or charges. Keep their source amount and rate for audit while
    # excluding them from the virtual sheet, Product Master and PriceHistory.
    tax_rows = []
    commercial_items = []
    for item in normalized["items"]:
        description = str(
            item.get("product_name")
            or item.get("description")
            or ""
        ).strip()
        if re.search(r"\b(?:vat|gst|tax|sales\s+tax|excise\s+tax)\b", description, re.I):
            tax_rows.append({
                "label": description,
                "rate": re.search(r"(\d+(?:\.\d+)?)\s*%", description).group(1)
                if re.search(r"(\d+(?:\.\d+)?)\s*%", description)
                else None,
                "amount": item.get("line_total") or item.get("unit_price"),
                "currency": item.get("currency") or normalized.get("currency"),
                "source_reference": item.get("source_reference"),
            })
        else:
            commercial_items.append(item)
    if tax_rows:
        normalized["tax_metadata"] = list(normalized.get("tax_metadata") or []) + tax_rows
    normalized["items"] = commercial_items
    normalized["visible_priced_row_count"] = len(commercial_items)

    # Email/reply threads are quotations even when a model labels one of the
    # embedded tables as a price list. Keep the source classification uniform
    # for the document and every item.
    if normalized.get("document_type") == "email_quote":
        normalized["category"] = "QUOTATION"
        for item in normalized["items"]:
            item["source_document_type"] = "email_quote"
            item["source_category"] = "QUOTATION"
    elif normalized.get("document_type") == "offer":
        normalized["category"] = "QUOTATION"
        for item in normalized["items"]:
            item["source_document_type"] = "offer"
            item["source_category"] = "QUOTATION"

    # A thread-level currency that conflicts with the currencies explicitly
    # attached to its priced rows is ambiguous. Do not present the thread
    # value as authoritative or claim a high-confidence extraction.
    item_currencies = {
        _valid_currency(item.get("currency"))
        for item in normalized["items"]
        if _valid_currency(item.get("currency"))
    }
    document_currency = _valid_currency(normalized.get("currency"))
    if document_currency and item_currencies and document_currency not in item_currencies:
        normalized["currency"] = None
        reasons = list(normalized.get("_review_reasons") or [])
        reasons.append("conflicting_segment_currency")
        normalized["_review_reasons"] = list(dict.fromkeys(reasons))
        normalized["_needs_review"] = True
    
    # ---------------------------------------------------------
    # SUPPLIER NORMALIZATION
    #
    # Supports:
    # - normal single-supplier documents
    # - supplier comparison sheets
    # - multiple supplier price columns in one spreadsheet
    #
    # IMPORTANT:
    # Never overwrite an item's own supplier.
    # ---------------------------------------------------------

    document_supplier = _normalize_supplier_name(
        normalized.get("supplier_name")
    )

    if (
        not document_supplier
        or document_supplier.upper() == "UNKNOWN"
    ):
        document_supplier = None


    def is_metadata_supplier(value):
        text = str(value or "").strip()
        return bool(re.match(
            r"(?i)^(?:ref(?:erence)?\s*(?:no|number)?|date|currency|quotation\s*(?:no|number)?|quote\s*(?:no|number)?|po\s*(?:no|number)?|vessel|to|from)\b",
            text,
        ))

    item_suppliers = []

    for item in normalized["items"]:
        candidate = _normalize_supplier_name(
            item.get("supplier_name")
        )

        if is_metadata_supplier(candidate):
            item["supplier_name"] = None
            candidate = None

        if (
            candidate
            and candidate.upper() != "UNKNOWN"
        ):
            item_suppliers.append(candidate)


    unique_item_suppliers = {}

    for supplier in item_suppliers:
        supplier_key = _supplier_identity_key(supplier)

        if not supplier_key:
            continue

        unique_item_suppliers.setdefault(
            supplier_key,
            supplier,
        )


    # Multiple suppliers found:
    # keep supplier on each individual item.
    if len(unique_item_suppliers) > 1:

        normalized["supplier_name"] = None

        canonical_suppliers = {
            _supplier_identity_key(value): value
            for value in unique_item_suppliers.values()
            if _supplier_identity_key(value)
        }

        for item in normalized["items"]:
            candidate = _normalize_supplier_name(
                item.get("supplier_name")
            )

            if is_metadata_supplier(candidate):
                candidate = None

            if (
                not candidate
                or candidate.upper() == "UNKNOWN"
            ):
                item["supplier_name"] = None
                continue

            candidate_key = _supplier_identity_key(candidate)

            if candidate_key in canonical_suppliers:
                item["supplier_name"] = canonical_suppliers[candidate_key]


    # Exactly one supplier found:
    # use it as document supplier too.
    elif len(unique_item_suppliers) == 1:

        only_supplier = next(
            iter(unique_item_suppliers.values())
        )

        normalized["supplier_name"] = (
            document_supplier
            or only_supplier
        )

        for item in normalized["items"]:
            candidate = _normalize_supplier_name(
                item.get("supplier_name")
            )

            if is_metadata_supplier(candidate):
                candidate = None

            if (
                not candidate
                or candidate.upper() == "UNKNOWN"
            ):
                item["supplier_name"] = (
                    normalized["supplier_name"]
                )
            else:
                item["supplier_name"] = candidate


    # No item supplier found.
    else:

        normalized["supplier_name"] = document_supplier

        for item in normalized["items"]:
            candidate = _normalize_supplier_name(
                item.get("supplier_name")
            )

            if is_metadata_supplier(candidate):
                candidate = None

            if (
                not candidate
                or candidate.upper() == "UNKNOWN"
            ):
                item["supplier_name"] = (
                    normalized["supplier_name"]
                )

   
        # ---------------------------------------------------------
    # A comparison table with multiple named supplier columns is a price
    # list even when the model returned a generic/unknown category. Keep the
    # document supplier null because no single supplier owns the document.
    comparison_document = len(unique_item_suppliers) > 1
    if (
        comparison_document
        and normalized.get("document_type") not in {"email_quote", "offer"}
        and normalized.get("document_type") in {"other", "unknown", "", "quotation", "offer"}
    ):
        normalized["document_type"] = "price_list"
        normalized["category"] = "PRICE_LIST"

    if comparison_document:
        for item in normalized["items"]:
            # These are document/category labels, not product-section
            # headings. A real section heading is retained by the model.
            parent = str(item.get("parent_category") or "").strip().casefold()
            if parent in {"tools", "commercial", "price list", "price_list", "tools list comparison"}:
                item["parent_category"] = None
            item["source_document_type"] = "price_list"
            item["source_category"] = "PRICE_LIST"
            # The Specification column distinguishes comparison rows (e.g.
            # "Megger - 10kv"). extra_attributes are dropped by later
            # stages, so keep it in the product name.
            name = re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip()
            for attr in item.get("extra_attributes") or []:
                if not isinstance(attr, dict):
                    continue
                label = re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip()
                spec = re.sub(r"\s+", " ", str(attr.get("value") or "")).strip(" .:-|")
                if (
                    name
                    and label in {"specification", "specifications"}
                    and spec
                    and len(spec) <= 60
                    and spec.casefold() not in name.casefold()
                ):
                    item["product_name"] = f"{name} - {spec}"
                    break

        # ---------------------------------------------------------
    # DATE NORMALIZATION
    #
    # Priority:
    # 1. Keep an item's own explicit date.
    # 2. If a page has one clear date, use it for missing items
    #    from that same page.
    # 3. If the whole file has only one unique commercial date,
    #    use it for every remaining undated item.
    # 4. If multiple different dates exist, never force one
    #    document date onto unrelated rows.
    # ---------------------------------------------------------

    items = normalized["items"]

    # Collect explicit item dates.
    explicit_item_dates = []

    for item in items:
        item_date = _valid_date(item.get("purchase_date"))

        if item_date:
            item["purchase_date"] = item_date
            explicit_item_dates.append(item_date)


    # STEP 1:
    # Build page -> dates mapping from items that already have a date.
    page_dates = {}

    for item in items:
        item_date = _valid_date(item.get("purchase_date"))
        page_number = item.get("page_number")

        if item_date and page_number not in (None, ""):
            page_dates.setdefault(str(page_number), set()).add(item_date)


    # STEP 2:
    # If one page has exactly one known date, apply it only to
    # undated items from that same page.
    for item in items:
        if _valid_date(item.get("purchase_date")):
            continue

        page_number = item.get("page_number")

        if page_number in (None, ""):
            continue

        dates_for_page = page_dates.get(str(page_number), set())

        if len(dates_for_page) == 1:
            item["purchase_date"] = next(iter(dates_for_page))


    # Recalculate dates after page-level fallback.
    all_item_dates = [
        _valid_date(item.get("purchase_date"))
        for item in items
        if _valid_date(item.get("purchase_date"))
    ]

    unique_item_dates = set(all_item_dates)

    document_date = _valid_date(normalized.get("document_date"))

    # Include document-level date while deciding whether this
    # uploaded file really contains only one date.
    all_known_dates = set(unique_item_dates)

    if document_date:
        all_known_dates.add(document_date)


    # STEP 3:
    # If there is only ONE unique date in the whole file,
    # use that date for every remaining undated item.
    if len(all_known_dates) == 1:
        single_date = next(iter(all_known_dates))

        normalized["document_date"] = single_date

        for item in items:
            if not _valid_date(item.get("purchase_date")):
                item["purchase_date"] = single_date


    # If document_date itself was missing and all extracted items
    # agree on one date, use that as the document date.
    elif not document_date and len(unique_item_dates) == 1:
        normalized["document_date"] = next(iter(unique_item_dates))
    if not normalized.get("currency"):
        item_currencies = {
            _valid_currency(item.get("currency"))
            for item in normalized.get("items", [])
            if _valid_currency(item.get("currency"))
        }

        if len(item_currencies) == 1:
            normalized["currency"] = next(iter(item_currencies))
    normalized = _harmonize_item_dates_by_source_section(normalized)
    normalized = _canonicalize_item_suppliers(normalized)
  
    normalized = _apply_item_display_names(
        normalized
    )

    for item in normalized.get('items') or []:
        if item.get('_source_currency_missing'):
            item['currency'] = None
            normalized['currency'] = None
    return normalized


def _finalize_email_quote_metadata(result):
    """Apply email-thread classification after every recovery stage."""
    result = dict(result or {})
    if _normalize_document_type(result.get("document_type")) != "email_quote":
        return result
    result["document_type"] = "email_quote"
    result["category"] = "QUOTATION"
    items = list(result.get("items") or [])
    supplier_values = {
        _normalize_supplier_name(item.get("supplier_name"))
        for item in items
        if _normalize_supplier_name(item.get("supplier_name"))
    }
    if len(supplier_values) == 1 and not _clean_text(result.get("supplier_name")):
        result["supplier_name"] = next(iter(supplier_values))
    for item in items:
        item["source_document_type"] = "email_quote"
        item["source_category"] = "QUOTATION"
        source = str(item.get("source_context") or item.get("source_reference") or "")
        upper_source = source.upper()

        # A priced email row without segment provenance cannot safely inherit
        # commercial facts from another message block.  Keep the row for
        # review, but never expose an unsupported/derived total as source data.
        if not source.strip() and item.get("unit_price") not in (None, ""):
            review_reasons = list(result.get("_review_reasons") or [])
            review_reasons.append("missing_segment_provenance")
            result["_review_reasons"] = list(dict.fromkeys(review_reasons))
            item["line_total"] = None

        # A bare yen symbol has no country information. Keep the currency
        # unresolved unless the source explicitly qualifies it.
        if item.get("currency") in {"JPY", "CNY"} and re.search(r"[¥￥]", source):
            if not re.search(r"JP¥|\bJPY\b|CN¥|\bRMB\b|\bCNY\b|\b元\b", upper_source):
                item["currency"] = None

        # Preserve only totals printed in the same commercial segment.
        if source and item.get("line_total") and not re.search(
            r"(?:=|\btotal(?:\s+cost)?\b|\bamount\b|\bamt\b|\bcharges?\b)\s*[:=-]?\s*(?:[A-Z]{3}\s*)?[\d,]+(?:\.\d+)?",
            source,
            re.I,
        ):
            item["line_total"] = None

        # A unit-price-only source line does not inherit quantity/UOM from an
        # earlier enquiry segment.
        if source and re.search(r"(?:unit\s*price|rate|price)\b", source, re.I):
            if not re.search(r"\b(?:qty|quantity)\b|\b\d+(?:\.\d+)?\s*(?:pcs?|nos?|units?|set|each)\b", source, re.I):
                item["quantity"] = None
                item["uom"] = None
    result["items"] = items
    item_currencies = {
        _valid_currency(item.get("currency"))
        for item in items
        if item.get("unit_price") not in (None, "") and _valid_currency(item.get("currency"))
    }
    review_reasons = list(result.get("_review_reasons") or [])
    if any(item.get("currency") is None for item in items if item.get("unit_price") not in (None, "")):
        result["currency"] = None
        review_reasons.extend(["ambiguous_segment_currency", "ambiguous_currency_symbol"])
    # If the thread-level currency disagrees with JPY/CNY item guesses and the
    # source does not contain an explicit qualified code, those guesses are
    # unsupported bare-symbol interpretations. Clear them together.
    if result.get("currency") is None:
        for item in items:
            source = str(item.get("source_context") or item.get("source_reference") or "")
            if item.get("currency") in {"JPY", "CNY"} and (
                re.search(r"[¥￥]", source)
                and not re.search(r"JP¥|\bJPY\b|CN¥|\bRMB\b|\bCNY\b|\b元\b", source, re.I)
                or not source
            ):
                item["currency"] = None
                review_reasons.append("ambiguous_currency_symbol")
    if not _clean_text(result.get("supplier_name")) and not any(
        _clean_text(item.get("supplier_name")) for item in items
    ):
        review_reasons.append("ambiguous_segment_supplier")
    if not _valid_currency(result.get("currency")) and not all(
        _valid_currency(item.get("currency")) for item in items if item.get("unit_price")
    ):
        review_reasons.append("ambiguous_segment_currency")
    result["_review_reasons"] = list(dict.fromkeys(review_reasons))
    result["_needs_review"] = bool(result["_review_reasons"])
    if result["_needs_review"]:
        confidence = dict(result.get("_confidence") or {})
        confidence["level"] = "medium"
        confidence["final_confidence"] = min(float(confidence.get("final_confidence") or 1.0), 0.75)
        confidence["score"] = confidence["final_confidence"]
        result["_confidence"] = confidence
    return result


def _bad_product_name(value, row_type="PRODUCT"):
    text = str(value or "").strip()
    normalized = text.lower()
    if not text or re.fullmatch(r"\d+", text):
        return True
    if str(row_type or "").upper() == "CHARGE" and normalized in {"discount", "handling charge", "fuel surcharge"}:
        return False
    # These terms only mean "this is a summary/label row, not a real item"
    # when they make up the WHOLE name (or nearly all of it) — e.g. "Total",
    # "Grand Total", "VAT 5%", "Bank Details". A genuine item description
    # that happens to CONTAIN one of these words as part of a longer
    # sentence (e.g. "Total cost ex door Athens up to LPA airport...") is
    # real commercial data and must not be dropped just because "total"
    # appears in it. Only reject when the summary term dominates the name.
    summary_only_pattern = re.compile(
        r"^\s*(?:qty|quantity|vat|tax|subtotal|sub total|grand total|total|bank|iban|swift)"
        r"(?:\s*[:#]?\s*\d*(?:\.\d+)?\s*%?)?\s*$",
        re.I,
    )
    if summary_only_pattern.fullmatch(normalized):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s+(?:nos?|pcs?|kg|mtr|meter|unit|set)\s+\d", normalized):
        return True
    if _is_generic_product_label(text) or normalized in {"pc", "pcs", "per pc", "unit", "each"}:
        return True
    return False

def _is_generic_product_label(value):
    key = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return key in {"", "product", "service", "product service", "item", "description", "item description", "product description", "for reference", "for your reference", "reference", "price reference", "as follows", "details below"}

def _merge_focused_retry_result(original_result, retry_result, validation_issues):
    merged = dict(original_result or {})
    original_items = list(merged.get("items") or [])
    retry_items = list((retry_result or {}).get("items") or [])
    codes = {issue.get("code") for issue in validation_issues or []}
    allowed = set()
    if "SUPPLIER_UNCERTAIN" in codes: allowed.update({"supplier_name"})
    if "DATE_UNCERTAIN" in codes: allowed.update({"document_date", "purchase_date"})
    if "CURRENCY_UNCERTAIN" in codes: allowed.update({"currency"})
    if "MISSING_PRODUCT_NAME" in codes: allowed.update({"product_name", "display_name"})
    for field in ("supplier_name", "document_date", "currency"):
        if field in allowed and retry_result.get(field) not in (None, "", "UNKNOWN"):
            merged[field] = retry_result[field]
    used = set()
    for original in original_items:
        candidates = []
        for index, retry in enumerate(retry_items):
            if index in used: continue
            if original.get("page_number") and retry.get("page_number") and original.get("page_number") != retry.get("page_number"): continue
            price_match = _decimal(original.get("unit_price") or original.get("line_total")) == _decimal(retry.get("unit_price") or retry.get("line_total"))
            qty_match = str(original.get("quantity") or "") == str(retry.get("quantity") or "")
            uom_match = str(original.get("uom") or "").lower() == str(retry.get("uom") or "").lower()
            if price_match and (qty_match or uom_match): candidates.append((index, retry))
        if len(candidates) != 1: continue
        index, retry = candidates[0]; used.add(index)
        for field in allowed:
            if field in {"product_name", "display_name"} and not _is_generic_product_label(retry.get(field)) and retry.get(field):
                original[field] = retry[field]
            elif field not in {"product_name", "display_name"} and retry.get(field) not in (None, "", "UNKNOWN"):
                original[field] = retry[field]
    merged["items"] = original_items
    return merged

def _normalize_common_commercial_ocr(value):
    """
    Conservative OCR cleanup for common commercial/service words.

    Generic only:
    - no supplier names
    - no company-specific names
    - no location-specific replacements
    - only corrects close OCR variants of well-known commercial terms
    """
    text = str(value or "").strip()
    if not text:
        return text

    common_terms = {
        "transport",
        "transportation",
        "parking",
        "freight",
        "handling",
        "delivery",
        "clearance",
        "customs",
        "loading",
        "unloading",
        "waiting",
        "standby",
        "mobilization",
        "demobilization",
        "installation",
        "inspection",
        "testing",
        "commissioning",
        "repair",
        "maintenance",
        "service",
        "airport",
        "accommodation",
    }

    def edit_distance(a, b):
        a = a.lower()
        b = b.lower()

        previous = list(range(len(b) + 1))

        for i, ca in enumerate(a, 1):
            current = [i]

            for j, cb in enumerate(b, 1):
                current.append(
                    min(
                        current[-1] + 1,
                        previous[j] + 1,
                        previous[j - 1] + (ca != cb),
                    )
                )

            previous = current

        return previous[-1]

    def fix_word(match):
        original = match.group(0)
        lower = original.lower()

        # Already correct.
        if lower in common_terms:
            return original

        # Do not modify very short words/acronyms.
        if len(lower) < 5:
            return original

        best = None
        best_distance = 99

        for candidate in common_terms:
            # Prevent unrelated words being compared.
            if abs(len(candidate) - len(lower)) > 2:
                continue

            # Require similar beginning to keep this conservative.
            prefix_length = 3 if len(lower) < 7 else 4

            if lower[:prefix_length] != candidate[:prefix_length]:
                continue

            distance = edit_distance(lower, candidate)

            if distance < best_distance:
                best = candidate
                best_distance = distance

        # Maximum two OCR-character errors.
        if best is None or best_distance > 2:
            return original

        if original.isupper():
            return best.upper()

        if original[:1].isupper():
            return best.capitalize()

        return best

    return re.sub(
        r"\b[A-Za-z]{5,}\b",
        fix_word,
        text,
    )

def _clean_product_name(value, row_type):
    text = _clean_text(value)

    if not text:
        return None

    if str(row_type or "").upper() != "PRODUCT":
        text = _normalize_common_commercial_ocr(text)
    text = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", "", text).strip(" .:-")
    text = re.sub(r"\b(?:cancelled|canceled|cancellation|cancel|cansal)\b.*$", "", text, flags=re.I).strip(" .:-")
    text = re.sub(r"\b(?:job|status|invoice|tax invoice)\b[!.\s]*$", "", text, flags=re.I).strip(" .:-")
    if str(row_type or "").upper() != "CHARGE":
        # Only strip a TRAILING price-label suffix like "Widget XYZ Total: 500.00"
        # or "Item ABC Amount 250" — i.e. the word must be followed by a number
        # (with optional colon/currency in between), meaning it's a price tag
        # tacked onto the end of the description. Do NOT strip when the word
        # is not immediately followed by digits — that means it's part of the
        # actual sentence/description (e.g. "Total cost ex door Athens..." or
        # "Tax consultancy service"), which must be preserved intact.
        stripped = re.sub(
            r"\b(?:amount|total(?:\s+price)?|vat|tax)\b\s*[:\-]?\s*(?:[A-Z]{3}|[$\u20ac\u00a3\u00a5])?\s*\d[\d,.\s]*$",
            "",
            text,
            flags=re.I,
        ).strip(" .:-")
        if stripped:
            text = stripped
    text = re.sub(r"\s+", " ", text).strip()
    if str(row_type or "").upper() == "SERVICE":
        text = re.sub(r"\bpickyp\b", "pickup", text, flags=re.I)
    return text or None
def _canonical_row_type(value, item=None):
    raw = str(value or "").strip().upper()
    normalized = re.sub(r"[^A-Z0-9]+", "_", raw).strip("_")
    # Cost-breakdown lines (Remarks: External Cost / Manhour / Profit) are charges.
    if _has_cost_component_remark(item):
        return "CHARGE"
    if (item or {}).get("_table_source_verified"):
        source_text = str((item or {}).get("product_name") or "").lower()
        if re.search(r"\b(?:fee|freight|handling|insurance|clearance|surcharge|profit|markup|commission)\b", source_text) or re.search(r"\b(?:certificate|certification)\s*(?:fee|charge|cost|charges?)\b", source_text):
            return "CHARGE"
        if re.search(r"\b(?:day\s*rate|man[- ]?(?:day|hour|hours)|manhour|engineer|installation|commissioning|supervision|labou?r|service\s+work)\b", source_text):
            return "SERVICE"
        if re.search(r"\b(?:spare|spares|tools?|valve|pump|gasket|equipment|material|physical\s+item)\b", source_text):
            return "PRODUCT"
    # Source-verified MSG table headings determine the row type. Repeated
    # normalization must not reclassify it from appended cargo attributes.
    if ((item or {}).get("_msg_source_verified") or (item or {}).get("_source_fact_verified")) and normalized in {"PRODUCT", "SERVICE", "CHARGE"}:
        return normalized

    product_aliases = {
        "PRODUCT", "PRICED_PRODUCT", "COMMERCIAL_ROW", "LINE_ITEM",
        "ITEM", "MATERIAL", "MATERIAL_ROW", "GOODS", "GOODS_ITEM",
        "SPARE", "SPARES", "PART", "EQUIPMENT",
    }

    service_aliases = {
        "SERVICE", "PRICED_SERVICE", "LABOUR", "LABOR", "WORK",
        "TRANSPORT", "TRANSPORTATION", "INSTALLATION", "REPAIR",
        "INSPECTION", "TESTING",
    }

    charge_aliases = {
        "CHARGE", "PRICED_CHARGE", "FEE", "FREIGHT", "PACKING",
        "HANDLING", "CERT", "CERTIFICATION", "CERTIFICATE",
        "CERTIFICATE_FEE", "CLEARANCE", "SURCHARGE",
        "DELIVERY_CHARGE",
    }

    # Classify the commercial identity, not a following specification block
    # (e.g. a computer's Certification field is not a certification charge).
    text = str((item or {}).get("product_name") or "").split("|", 1)[0].lower()

    # Commercial charge words in the item name override Gemini's row_type.
    # Example: Gemini may say PRODUCT/SERVICE for "ABS Cert" or "CCS certification",
    # but those are separately billed charges.
    if re.search(
        r"\b(freight|packing|handling|cert|certificate|certification|clearance|fee|charge|inspection cost|surcharge|delivery charge)\b",
        text,
    ):
        return "CHARGE"

    name_text = " ".join(
        str((item or {}).get(key) or "")
        for key in (
            "product_name",
            "product_service_name",
            "description",
            "display_name",
            "parent_category",
            "supplier_name",
        )
    ).lower()

    route_like = bool(
        re.search(r"\b[A-Z0-9./&() -]{2,}\s+TO\s+[A-Z0-9./&() -]{2,}\b", name_text, re.I)
        or re.search(r"\bfrom\s+.+\s+to\s+.+\b", name_text, re.I)
    )

    transport_words = bool(
        re.search(
            r"\b(transport|transportation|delivery|truck|trucking|haulage|loading|offloading|shipment|freight|cartage|trip)\b",
            name_text,
            re.I,
        )
    )

    transport_supplier = bool(
        re.search(
            r"\b(transport|transportation|shipping|logistics|cargo|freight)\b",
            name_text,
            re.I,
        )
    )

    service_unit = bool(
        re.search(
            r"\b(ton|tons|trip|hour|hr|hrs|day|days|km|mile|miles)\b",
            name_text,
            re.I,
        )
    )
    if "social development dirham" in text:
        return "SERVICE"
    if route_like or transport_words or (transport_supplier and service_unit):
        return "SERVICE"

    if normalized in charge_aliases:
        return "CHARGE"
    if normalized in service_aliases:
        return "SERVICE"
    if normalized in product_aliases:
        # Generic Gemini labels like item/line_item need content-based routing.
        if normalized in {"ITEM", "LINE_ITEM", "COMMERCIAL_ROW"}:
            if re.search(
                r"\b(service|labou?r|transport|installation|repair|testing|work|hire|crane|loading|offloading|direct del|delivery)\b",
                text,
            ):
                return "SERVICE"
            return "PRODUCT"
        return "PRODUCT"

    if re.search(
        r"\b(service|labou?r|transport|installation|repair|testing|work|hire|crane|loading|offloading|direct del|delivery)\b",
        text,
    ):
        return "SERVICE"

    if (item or {}).get("product_name") and ((item or {}).get("unit_price") or (item or {}).get("line_total")):
        return "PRODUCT"
    return "UNKNOWN"
def _normalize_reusable_service_item(item):
    """
    Generic deterministic cleanup for service costing.

    - converts multi-person execution totals to a reusable per-person rate
      only when mathematically safe
    - removes aggregate summary totals
    - does not affect PRODUCT rows
    """

    cleaned = dict(item or {})

    row_type = str(
        cleaned.get("item_type")
        or cleaned.get("row_type")
        or "PRODUCT"
    ).upper()

    name = str(
        cleaned.get("product_service_name")
        or cleaned.get("product_name")
        or ""
    ).strip()

    normalized_name = re.sub(
        r"[^a-z0-9]+",
        " ",
        name.lower()
    ).strip()

    # Aggregate totals are not reusable price-history items.
    summary_patterns = (
        r"^grand total\b",
        r"^overall cost\b",
        r"^total cost\b",
        r"^total mark ?up cost\b",
        r"^total manpower cost\b",
        r"^total project cost\b",
        r"^total revised cost\b",
        r"^total for (?:above|the above)\b",
    )

    if any(
        re.search(pattern, normalized_name, re.I)
        for pattern in summary_patterns
    ):
        return None

    if row_type not in {"SERVICE", "CHARGE"}:
        return cleaned

    # FOC/free rows must not become reusable rates.
    if re.search(
        r"\b(?:foc|free of charge|no charge|stand\s*by\s*foc)\b",
        normalized_name,
        re.I,
    ):
        return None

    try:
        quantity = (
            Decimal(str(cleaned.get("quantity")).replace(",", ""))
            if cleaned.get("quantity") not in (None, "")
            else None
        )
    except Exception:
        quantity = None

    raw_total = (
        cleaned.get("total")
        if cleaned.get("total") not in (None, "")
        else cleaned.get("line_total")
    )

    try:
        total = (
            Decimal(str(raw_total).replace(",", ""))
            if raw_total not in (None, "")
            else None
        )
    except Exception:
        total = None

    try:
        unit_price = (
            Decimal(str(cleaned.get("unit_price")).replace(",", ""))
            if cleaned.get("unit_price") not in (None, "")
            else None
        )
    except Exception:
        unit_price = None

    # Safe reusable-rate normalization:
    # qty 2 + total 1400 => rate 700
        # Do not derive per-person/per-day rate from a single visible amount.
    # If source shows only one commercial amount, keep unit_price and line_total
    # as that same printed amount. Quantity remains informational.
    if (
        quantity is not None
        and quantity > 1
        and total is not None
        and (unit_price is None or unit_price == total)
    ):
        cleaned["unit_price"] = str(total)
        cleaned["total"] = str(total)
        cleaned["line_total"] = str(total)

    return cleaned
def _normalize_transport_service(item):
    """
    Normalize clear one-trip transport rows for costing.

    Safe rules only:
    - SERVICE only
    - route/transport-like description
    - total present
    - no explicit quantity/unit price
    => qty 1, UOM trip, unit price = total

    Does not touch PRODUCT rows or services with their own explicit basis.
    """

    cleaned = dict(item or {})

    row_type = str(
        cleaned.get("item_type")
        or cleaned.get("row_type")
        or "PRODUCT"
    ).upper()

    if row_type not in {"SERVICE", "CHARGE"}:
        return cleaned

    name = str(
        cleaned.get("product_service_name")
        or cleaned.get("product_name")
        or ""
    ).strip()

    normalized_name = re.sub(r"\s+", " ", name.lower())

    # Only apply to obvious transportation / route services.
    is_transport = (
        "transport" in normalized_name
        or "freight" in normalized_name
        or re.search(r"\bfrom\b.+\bto\b", normalized_name)
        or re.search(r"\b(?:to|via|w/h)\b", normalized_name)
        or "->" in normalized_name
        or "→" in normalized_name
    )

    charge_uom = str(cleaned.get("uom") or cleaned.get("unit_of_measurement") or "").strip().casefold()
    if cleaned.get("_fixed_shipment_quote"):
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        return cleaned
    if (
        not is_transport
        and re.search(r"\b(?:charges?|fee|surcharge)\b", name, re.I)
        and (cleaned.get("_fixed_charge") or charge_uom in {"unit", "charge", "fee"})
    ):
        cleaned["row_type"] = "CHARGE"
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["line_total"] = None
        cleaned["_fixed_charge"] = True
        return cleaned
    # Freight weight is shipment metadata unless the source explicitly
    # describes a per-weight rate.  A route/shipment row with a KG/T unit
    # and a fixed total must not become a weight-based transaction.
    if (
        is_transport
        and charge_uom in {"kg", "kgs", "kilogram", "kilograms", "ton", "tons"}
        and cleaned.get("line_total") not in (None, "")
        and not re.search(r"(?:per|/|each)\s*(?:kg|kgs|ton|tons)", name, re.I)
    ):
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["unit_price"] = None
        cleaned["_fixed_shipment_quote"] = True
        return cleaned
    if (
        not is_transport
        and charge_uom in {"unit", "charge", "fee"}
        and _decimal(cleaned.get("quantity")) == Decimal("1")
        and cleaned.get("line_total") not in (None, "")
        and re.search(r"\b(?:charges?|fee|surcharge)\b", name, re.I)
    ):
        cleaned["row_type"] = "CHARGE"
        if cleaned.get("unit_price") in (None, ""):
            cleaned["unit_price"] = cleaned.get("line_total")
        cleaned["line_total"] = None
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["_fixed_charge"] = True
        return cleaned
    if not is_transport:
        return cleaned
    attrs = [attr for attr in (cleaned.get("extra_attributes") or []) if isinstance(attr, dict)]
    weight_attr = next(
        (attr for attr in attrs
         if re.search(r"\b(?:chargeable\s+)?weight\b", str(attr.get("label") or ""), re.I)),
        None,
    )
    # A quoted shipment total accompanied by a chargeable-weight field is a
    # fixed offer, not a weight-based transaction. Keep the amount intact and
    # remove only the inferred quantity/UOM.
    if weight_attr and cleaned.get("line_total") not in (None, ""):
        # Preserve an explicitly quoted unit price, but do not invent one
        # from the fixed shipment total when the source only gives a total.
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["_fixed_shipment_quote"] = True
        return cleaned
    charge_uom = str(cleaned.get("uom") or cleaned.get("unit_of_measurement") or "").strip().casefold()
    if (
        charge_uom in {"unit", "charge", "fee"}
        and _decimal(cleaned.get("quantity")) == Decimal("1")
        and cleaned.get("line_total") not in (None, "")
        and re.search(r"\b(?:charges?|fee|surcharge)\b", name, re.I)
    ):
        cleaned["row_type"] = "CHARGE"
        if cleaned.get("unit_price") in (None, ""):
            cleaned["unit_price"] = cleaned.get("line_total")
        cleaned["line_total"] = None
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["_fixed_charge"] = True
        return cleaned
    if any(
        isinstance(attr, dict)
        and str(attr.get("label") or "").strip().casefold() == "capacity"
        for attr in (cleaned.get("extra_attributes") or [])
    ):
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["_capacity_tier"] = True
        return cleaned
    qty_text = str(cleaned.get("quantity") or "").strip()
    unit_price_text = str(cleaned.get("unit_price") or "").strip()
    line_total_text = str(
        cleaned.get("line_total")
        or cleaned.get("total")
        or ""
    ).strip()

    # In a transport tariff matrix, TON/tons is a vehicle-capacity tier,
    # not the purchased trip quantity. Preserve the tier as source metadata
    # and keep the printed tariff as the amount for that route/tier.
    if (
        str(cleaned.get("uom") or cleaned.get("unit_of_measurement") or "").strip().upper()
        in {"TON", "TONS"}
        and qty_text
        and unit_price_text
        and line_total_text
        and _decimal(unit_price_text) == _decimal(line_total_text)
    ):
        attrs = list(cleaned.get("extra_attributes") or [])
        if not any(str(a.get("label") or "").strip().casefold() == "capacity" for a in attrs if isinstance(a, dict)):
            attrs.append({"label": "Capacity", "value": f"{qty_text} TON"})
        cleaned["extra_attributes"] = attrs
        cleaned["quantity"] = None
        cleaned["uom"] = None
        cleaned["unit_of_measurement"] = None
        cleaned["_capacity_tier"] = True
        return cleaned

    # If ton/capacity was copied from description into quantity,
    # but source amount is a single route amount, keep it as one trip.
    if (
        re.search(r"\b\d+(?:\.\d+)?\s*tons?\b", normalized_name, re.I)
        and qty_text
        and unit_price_text
        and line_total_text
        and _decimal(unit_price_text) == _decimal(line_total_text)
    ):
        cleaned["quantity"] = "1"
        cleaned["uom"] = cleaned.get("uom") or "trip"
        cleaned["unit_of_measurement"] = cleaned.get("unit_of_measurement") or "trip"

    quantity = cleaned.get("quantity")
    unit_price = cleaned.get("unit_price")
    uom = cleaned.get("uom") or cleaned.get("unit_of_measurement")

    raw_total = (
        cleaned.get("total")
        if cleaned.get("total") not in (None, "")
        else cleaned.get("line_total")
    )

    # If the document only gives one transport amount,
    # treat it as one trip/service occurrence.
    if (
        quantity in (None, "")
        and unit_price in (None, "")
        and raw_total not in (None, "")
    ):
        cleaned["quantity"] = "1"
        cleaned["unit_price"] = raw_total
        cleaned["uom"] = "trip"
        cleaned["unit_of_measurement"] = "trip"

    elif (
        quantity in (None, "")
        and raw_total not in (None, "")
        and str(uom or "").strip() == ""
    ):
        cleaned["quantity"] = "1"
        cleaned["uom"] = "trip"
        cleaned["unit_of_measurement"] = "trip"

        if unit_price in (None, ""):
            cleaned["unit_price"] = raw_total

    return cleaned
def _is_real_transport_service_row(mapped):
    if not mapped:
        return False

    row_type = str(mapped.get("row_type") or "").upper()
    if row_type != "SERVICE":
        return False

    text = " ".join(
        str(mapped.get(key) or "")
        for key in (
            "product_name",
            "product_service_name",
            "description",
            "display_name",
            "parent_category",
            "uom",
            "unit_of_measurement",
            "supplier_name",
        )
    )

    normalized = re.sub(r"\s+", " ", text).strip().lower()

    has_route = bool(
        re.search(r"\bfrom\b.+\bto\b", normalized)
        or re.search(r"\b[a-z0-9 ./'&-]+\s+to\s+[a-z0-9 ./'&-]+", normalized)
    )

    has_transport_context = bool(
        re.search(
            r"\b(transport|transportation|trip|truck|lorry|haul|haulage|destination|port|freight|delivery)\b",
            normalized,
            re.I,
        )
    )

    has_amount = (
        mapped.get("unit_price") not in (None, "")
        or mapped.get("line_total") not in (None, "")
    )

    return has_amount and (has_route or has_transport_context)
def _flag_grand_total_merged_rows(mapped_items, doc_grand_total, doc_subtotal=None):
    """
    Prevent document summary amounts such as Subtotal / Taxable Amount /
    Grand Total from becoming product prices.
    """

    if len(mapped_items) < 2:
        return

    summary_amounts = {
        amount
        for amount in (doc_grand_total, doc_subtotal)
        if amount is not None
    }

    if not summary_amounts:
        return

    for entry in mapped_items:
        mapped = entry.get("mapped")

        if not mapped:
            continue

        if mapped.get("_keep_duplicate_priced_row"):
            continue

        if _is_real_transport_service_row(mapped):
            continue

        # A meaningful commercial description is not a summary merely
        # because its amount equals the document total.
        if not _bad_product_name(
            mapped.get("product_name") or mapped.get("display_name"),
            mapped.get("row_type"),
        ):
            continue

        if mapped.get("row_type") not in {"PRODUCT", "SERVICE", "CHARGE"}:
            continue

        line_total = _decimal(mapped.get("line_total"))
        unit_price = _decimal(mapped.get("unit_price"))

        candidate = line_total if line_total is not None else unit_price

        if candidate is None:
            continue

        is_summary_amount = any(
            abs(candidate - summary) <= max(
                Decimal("0.05"),
                abs(summary) * Decimal("0.001"),
            )
            for summary in summary_amounts
        )

        if not is_summary_amount:
            continue

        # If another PRODUCT with the same item/part code has a valid rate,
        # reuse that rate instead of the document summary amount.
        if mapped.get("row_type") == "PRODUCT":
            code = str(
                mapped.get("item_code")
                or mapped.get("part_number")
                or ""
            ).strip().lower()

            if code:
                for other_entry in mapped_items:
                    other = other_entry.get("mapped")

                    if not other or other is mapped:
                        continue

                    if other.get("row_type") != "PRODUCT":
                        continue

                    other_code = str(
                        other.get("item_code")
                        or other.get("part_number")
                        or ""
                    ).strip().lower()

                    if other_code != code:
                        continue

                    other_rate = _decimal(other.get("unit_price"))

                    if other_rate is None:
                        continue

                    # Never copy another summary amount.
                    if any(
                        abs(other_rate - summary) <= max(
                            Decimal("0.05"),
                            abs(summary) * Decimal("0.001"),
                        )
                        for summary in summary_amounts
                    ):
                        continue

                    quantity = _decimal(mapped.get("quantity"))

                    mapped["unit_price"] = str(other_rate)

                    if quantity is not None:
                        mapped["line_total"] = str(quantity * other_rate)
                    else:
                        mapped["line_total"] = str(other_rate)

                    entry["warning"] = (
                        "summary_amount_replaced_using_same_item_rate"
                    )
                    break
                else:
                    entry["mapped"] = None
                    entry["warning"] = "document_summary_amount_dropped"
            else:
                entry["mapped"] = None
                entry["warning"] = "document_summary_amount_dropped"

        else:
            entry["mapped"] = None
            entry["warning"] = "document_summary_amount_dropped"
def _reconcile_duplicate_short_product_names(mapped_items):
    """Code/name similarity cannot establish that two source rows are one."""
    return


def _reconcile_duplicate_product_prices(mapped_items):
    """Different prices must remain available for validation and history."""
    return
def _collapse_parallel_currency_rows(result):
    """Different currencies are distinct offers without explicit source proof."""
    return result


def _supplier_column_order(items):
    """Return supplier columns in their first-seen source order."""
    order = []
    seen = set()
    for item in items or []:
        supplier = _normalize_supplier_name(item.get("supplier_name"))
        key = _supplier_identity_key(supplier)
        if supplier and key and key not in seen:
            seen.add(key)
            order.append(supplier)
    return order


def _realign_suppliers_by_column_position(items, page_bytes, supplier_x, supplier_registry=None):
    """Fix supplier-comparison rows whose blank cells made prices shift left.

    Uses the PDF text layer: every supplier price column has a fixed x
    position. Rows that quote every supplier (the most complete rows) define
    each supplier's x; any other row whose printed price sits under a
    different supplier's column is reassigned to that supplier. `supplier_x`
    is carried across pages. Returns True when positions were verified.
    No-op for scans / PDFs without a text layer.
    """
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(page_bytes)) as pdf:
            page = pdf.pages[0]
            words = page.extract_words()
            tolerance = float(page.width) * 0.02
    except Exception:
        return False

    def amount_key(value):
        number = _decimal(value)
        return None if number is None else f"{number:.2f}"

    word_x = {}
    for word in words:
        key = amount_key(word.get("text"))
        if key is not None and re.search(r"\d[.,]\d", word.get("text", "")):
            word_x.setdefault(key, []).append((word["x0"] + word["x1"]) / 2)

    def item_x(item):
        for field in ("unit_price", "line_total"):
            key = amount_key(item.get(field))
            positions = word_x.get(key) if key else None
            if not positions:
                continue
            if max(positions) - min(positions) > tolerance:
                return None  # same amount printed in different columns
            return sum(positions) / len(positions)
        return None

    groups = {}
    group_names = []

    def comparison_name_key(item):
        name = re.sub(r"\s+", " ", str(item.get("product_name") or "").casefold()).strip()
        return re.sub(r"[^\w\u0080-\uffff]+", " ", name).strip()

    def group_key(item):
        name = comparison_name_key(item)
        if not name:
            return name
        # A continuation/specification suffix can be appended to the same
        # product identity when the source comparison table changes language
        # or repeats the item with a model/size suffix.  Only use this alias
        # when the suffix contains a concrete identifier (number/non-ASCII),
        # avoiding broad substring-based product matching.
        for existing in group_names:
            if name.startswith(existing + " "):
                suffix = name[len(existing):].strip(" -")
                if suffix and (re.search(r"\d", suffix) or re.search(r"[^\x00-\x7f]", suffix)):
                    return existing
            if existing.startswith(name + " "):
                suffix = existing[len(name):].strip(" -")
                if suffix and (re.search(r"\d", suffix) or re.search(r"[^\x00-\x7f]", suffix)):
                    return name
        group_names.append(name)
        return name

    for item in items or []:
        name = group_key(item)
        priced = _decimal(item.get("unit_price")) is not None or _decimal(item.get("line_total")) is not None
        # Blank supplier cells may still come back as price-less rows; they
        # carry no column evidence.
        if name and priced and _supplier_identity_key(item.get("supplier_name")):
            groups.setdefault(name, []).append(item)
    if not groups:
        return False

    # Learn each supplier's column x from the most complete rows.
    most = max(len({_supplier_identity_key(i.get("supplier_name")) for i in g}) for g in groups.values())
    if most >= 3:
        learned = {}
        for group in groups.values():
            if len({_supplier_identity_key(i.get("supplier_name")) for i in group}) != most:
                continue
            for item in group:
                x = item_x(item)
                if x is not None:
                    learned.setdefault(_supplier_identity_key(item.get("supplier_name")), []).append(x)
        for key, values in learned.items():
            if max(values) - min(values) <= tolerance:
                supplier_x.setdefault(key, sum(values) / len(values))

    if len(supplier_x) < 3:
        return False

    names = {
        _supplier_identity_key(value): value
        for value in (supplier_registry or [])
        if _supplier_identity_key(value)
    }
    for group in groups.values():
        for item in group:
            names.setdefault(_supplier_identity_key(item.get("supplier_name")), item.get("supplier_name"))

    columns = sorted(supplier_x, key=supplier_x.get)

    def candidate_columns(item):
        for field in ("unit_price", "line_total"):
            key = amount_key(item.get(field))
            found = set()
            for x in (word_x.get(key) or []) if key else []:
                index = min(range(len(columns)), key=lambda i: abs(supplier_x[columns[i]] - x))
                if abs(supplier_x[columns[index]] - x) <= tolerance:
                    found.add(index)
            if found:
                return sorted(found)
        return []

    checked = False
    duplicates = []
    for group in groups.values():
        previous = -1
        moved = False
        targets = []
        for item in group:
            candidates = candidate_columns(item)
            if len(candidates) > 1:
                # Same amount printed in several columns: suppliers are listed
                # left to right, so take the next column after the previous one.
                candidates = [c for c in candidates if c > previous][:1]
            if len(candidates) != 1:
                continue
            checked = True
            previous = candidates[0]
            nearest = columns[candidates[0]]
            own = _supplier_identity_key(item.get("supplier_name"))
            if nearest != own and nearest in names:
                logger.warning(
                    "SUPPLIER_COLUMN_REALIGNED product=%r from=%r to=%r amount=%r",
                    item.get("product_name"), item.get("supplier_name"), names[nearest], item.get("unit_price"),
                )
                item["supplier_name"] = names[nearest]
                moved = True
            targets.append(item)
        if not moved:
            continue
        # A shifted row can also carry a phantom trailing copy of the last
        # price (blank cell -> N+1 rows for N prices). Once realigned it
        # collides with the real row for the same supplier and amount.
        seen = set()
        for item in targets:
            key = (
                _supplier_identity_key(item.get("supplier_name")),
                amount_key(item.get("unit_price")),
                amount_key(item.get("line_total")),
            )
            if key in seen:
                duplicates.append(item)
            seen.add(key)
    if duplicates:
        items[:] = [item for item in items if not any(item is dup for dup in duplicates)]
        logger.warning("SUPPLIER_COLUMN_DUPLICATES_REMOVED count=%s", len(duplicates))
    return checked


def _item_has_current_page_commercial_evidence(item, page_bytes, page_number, supplier_x=None):
    """Reject rows copied from neighbouring context pages.

    Gemini may receive adjacent pages as context and attach a copied row to
    the current page.  Page identity in the JSON is not source evidence; the
    printed amount and its physical text position are.
    """
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(page_bytes)) as pdf:
            words = pdf.pages[0].extract_words(use_text_flow=False) or []
    except Exception:
        return True

    wanted = {_decimal(item.get("unit_price")), _decimal(item.get("line_total"))}
    wanted.discard(None)
    if not wanted:
        return False
    product_tokens = {
        token for token in re.findall(r"[\w\u0080-\uffff]+", str(item.get("product_name") or "").casefold())
        if len(token) > 2 and not token.isdigit()
    }
    for word in words:
        text = str(word.get("text") or "").strip()
        amount = _decimal(text)
        if amount not in wanted:
            continue
        center_x = (float(word.get("x0", 0)) + float(word.get("x1", 0))) / 2
        if supplier_x:
            nearest = min(supplier_x.values(), key=lambda x: abs(float(x) - center_x))
            tolerance = max(18.0, float(getattr(pdf.pages[0], "width", 0)) * 0.04)
            if abs(float(nearest) - center_x) > tolerance:
                continue
        top = float(word.get("top", 0))
        nearby = " ".join(
            str(candidate.get("text") or "")
            for candidate in words
            if abs(float(candidate.get("top", 0)) - top) <= 12
        ).casefold()
        if not product_tokens or any(token in nearby for token in product_tokens):
            return True
    return False


def _detect_supplier_comparison_layout(page_bytes, inherited_supplier_registry=None):
    """Detect matrix structure before Gemini count validation/retry."""
    if inherited_supplier_registry and len(inherited_supplier_registry) >= 2:
        return True
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(page_bytes)) as pdf:
            for table in pdf.pages[0].find_tables() or []:
                rows = table.extract() or []
                for header in rows[:8]:
                    nonempty = [str(value or "").strip() for value in header]
                    nonempty = [value for value in nonempty if value]
                    if len(nonempty) < 3:
                        continue
                    core = re.compile(
                        r"(?:description|product|item|name|qty|quantity|unit|uom|price|rate|amount|total|currency|remarks|specification|serial|no\.?)",
                        re.I,
                    )
                    supplier_like = [value for value in nonempty if not core.fullmatch(value)]
                    if len(supplier_like) < 2:
                        continue
                    header_index = rows.index(header)
                    for body in rows[header_index + 1:header_index + 5]:
                        numeric_cells = sum(
                            bool(re.search(r"\d", str(value or "")))
                            for value in body
                        )
                        if numeric_cells >= 2:
                            return True
    except Exception:
        return False
    return False


def _append_pdf_specifications(items, page_bytes, supplier_x, spec_column):
    """Add the printed Specification cell to product_name on comparison pages.

    The model returns the Specification column inconsistently, so it is read
    from the PDF text layer: the cell in the Specification column vertically
    aligned with the row's prices. `spec_column` carries the column bounds
    from the page that has the header to continuation pages. No-op without a
    text layer or a known column.
    """
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(page_bytes)) as pdf:
            page = pdf.pages[0]
            words = page.extract_words()
            vertical_edges = sorted({round(e["x0"]) for e in page.edges if e.get("orientation") == "v"})
            tolerance = float(page.width) * 0.02
    except Exception:
        return

    top_limit = 0.0
    for word in words:
        if re.sub(r"[^a-z]", "", word["text"].lower()) == "specification":
            center = (word["x0"] + word["x1"]) / 2
            left = max([x for x in vertical_edges if x < center] or [word["x0"] - 20])
            right = min([x for x in vertical_edges if x > center] or [word["x1"] + 20])
            spec_column["x0"], spec_column["x1"] = left, right
            top_limit = word["bottom"] + 5
            break
    if "x0" not in spec_column:
        return

    def amount_key(value):
        number = _decimal(value)
        return None if number is None else f"{number:.2f}"

    price_words = {}
    for word in words:
        key = amount_key(word.get("text"))
        if key is not None and re.search(r"\d[.,]\d", word.get("text", "")):
            price_words.setdefault(key, []).append(
                ((word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2)
            )

    groups = {}
    for item in items or []:
        name = re.sub(r"\s+", " ", str(item.get("product_name") or "").casefold()).strip()
        if name:
            groups.setdefault(name, []).append(item)

    row_y = {}
    for name, group in groups.items():
        for item in group:
            column_x = supplier_x.get(_supplier_identity_key(item.get("supplier_name")))
            key = amount_key(item.get("unit_price")) or amount_key(item.get("line_total"))
            if column_x is None or key is None:
                continue
            ys = [y for x, y in price_words.get(key, []) if abs(x - column_x) <= tolerance]
            if ys and max(ys) - min(ys) <= 8:
                row_y[name] = sum(ys) / len(ys)
                break
    if not row_y:
        return

    centers = sorted(row_y.values())
    gaps = [b - a for a, b in zip(centers, centers[1:])] or [60.0]
    typical_gap = sorted(gaps)[len(gaps) // 2]

    for name, y in row_y.items():
        index = centers.index(y)
        below = (centers[index] - centers[index - 1]) / 2 if index > 0 else typical_gap / 2
        above = (centers[index + 1] - centers[index]) / 2 if index + 1 < len(centers) else typical_gap / 2
        cell = [
            w for w in words
            if w["x0"] >= spec_column["x0"] - 1 and w["x1"] <= spec_column["x1"] + 1
            and w["top"] >= top_limit
            and y - below < (w["top"] + w["bottom"]) / 2 <= y + above
        ]
        cell.sort(key=lambda w: (round((w["top"] + w["bottom"]) / 2 / 6), w["x0"]))
        spec = re.sub(r"\s+", " ", " ".join(w["text"] for w in cell)).strip(" .:-|")
        if not spec or len(spec) > 60:
            continue
        def key(text):
            return re.sub(r"[\W_]+", "", str(text or "").casefold())

        for item in groups[name]:
            product = re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip()
            base, separator, tail = product.rpartition(" - ")
            # A specification the model already appended (spelled slightly
            # differently, e.g. "三合一 组合式") is replaced by the printed one.
            if separator and key(tail) and (key(tail) in key(spec) or key(spec) in key(tail)):
                product = base.strip()
            if key(spec) not in key(product):
                item["product_name"] = f"{product} - {spec}"
            else:
                item["product_name"] = product


def _repair_continuation_supplier_alignment(items, supplier_columns, page_number):
    """Repair a row-level leading blank on continuation comparison pages.

    This intentionally does not shift a whole page. A suffix-shaped row on
    the same page is evidence that the first supplier column is blank; only
    a peer row using the leading prefix is remapped to that suffix.
    """
    if int(page_number or 0) <= 1 or len(supplier_columns) < 3:
        return items
    keys = [_supplier_identity_key(value) for value in supplier_columns]
    suffix_rows = []
    for item in items or []:
        key = _supplier_identity_key(item.get("supplier_name"))
        if key in keys[1:]:
            suffix_rows.append(item)
    if not suffix_rows:
        return items

    prefix_mapping = dict(zip(keys[:-1], supplier_columns[1:]))
    groups = {}
    for item in items or []:
        group_key = re.sub(r"\s+", " ", str(item.get("product_name") or "").casefold()).strip()
        groups.setdefault(group_key, []).append(item)
    for group in groups.values():
        group_keys = {_supplier_identity_key(item.get("supplier_name")) for item in group}
        # Only rows occupying the leading prefix are shifted. A suffix-shaped
        # A peer row with a different supplier identity remains untouched.
        if not group_keys or not group_keys.issubset(set(keys[:-1])):
            continue
        for item in group:
            key = _supplier_identity_key(item.get("supplier_name"))
            if key in prefix_mapping:
                item["supplier_name"] = prefix_mapping[key]
    return items

def _price_reference_quality(mapped):
    quantity = _decimal(mapped.get("quantity"))
    unit_price = _decimal(mapped.get("unit_price"))
    line_total = _decimal(mapped.get("line_total"))

    if not mapped.get("_comparison_matrix_price") and quantity is not None and unit_price is not None and line_total is not None:
        return 4

    if unit_price is not None:
        return 3

    if quantity is not None and line_total is not None and quantity > 0:
        return 2

    if line_total is not None:
        return 1

    return 0


def _is_quoted_commercial_line(item):
    """A priced quote line is not a supporting permit document."""
    kind = str(item.get('source_document_type') or '').lower()
    if re.search(r'gate.?pass|permit|supporting|receipt', kind):
        return False
    context = ' '.join(str(item.get(k) or '') for k in ('source_document_type', 'source_category'))
    return bool(re.search(r'quotation|quote|offer|invoice', context, re.I)
                and (_decimal(item.get('unit_price')) is not None
                     or _decimal(item.get('line_total')) is not None))


def _normalize_item(item, default_currency):
    source_quantity = item.get('quantity')
    missing_source_rate = (item.get('unit_price') in (None, '')
                           and _decimal(source_quantity) is not None
                           and _decimal(item.get('line_total') or item.get('total')) is not None)
    if item.get('_physical_pdf_row'):
        mapped = copy.deepcopy(item)
        mapped['unit_of_measurement'] = mapped.get('uom')
        mapped['currency'] = mapped.get('currency') or default_currency
        return mapped, ''
    source_verified = bool(item.get("_table_source_verified"))
    item = dict(item or {})
    if not source_verified:
        item = _normalize_reusable_service_item(item)

    if not item:
        return None, "summary_or_non_reusable_service_skipped"

    if not source_verified:
        item = _normalize_transport_service(item)
    if missing_source_rate:
        item['unit_price'] = None
        item['quantity'] = source_quantity

    row_type = _canonical_row_type(
        item.get("item_type") or item.get("row_type"),
        item,
    )

    if row_type == "UNKNOWN":
        row_type = "PRODUCT"

    product_name = _clean_product_name(item.get("product_service_name") or item.get("product_name"), row_type)
    reference_values = [item.get("source_reference"), item.get("source_document_number"), item.get("document_number")]
    if product_name and any(_clean_text(v) and re.sub(r"\W+", "", product_name.lower()) == re.sub(r"\W+", "", _clean_text(v).lower()) for v in reference_values):
        product_name = None
    warnings = []
    normalized_name = str(product_name or item.get("product_service_name") or item.get("product_name") or "").strip().lower()
    if re.search(r"[\u0600-\u06FF]", normalized_name):
        return None, "non_commercial_arabic_template_row_skipped"

    if re.search(
        r"\b(tahseel|scientific research|service number|vat|trn|subtotal|total)\b",
        normalized_name,
        re.I,
    ):
        return None, "non_commercial_template_row_skipped"
    if re.search(
        r"\b(?:price|rate)\s+is\s+(?:valid|subject)\b.*\b(?:changes?|increase|decrease)\b"
        r"|\b(?:validity|valid for|valid until|copper price|price changes?)\b.*\b(?:usd|aed|rmb|cny)\b",
        normalized_name,
        re.I,
    ):
        return None, "pricing_condition_note_skipped"
    if not _is_quoted_commercial_line(item) and re.search(
        r"\b(gate\s*pass|daily\s+gate\s+pass|permit|security\s+pass)\b",
        normalized_name,
        re.I,
    ):
        return None, "permit_non_commercial_row_skipped"
    if re.search(
        r"\b("
        r"temporary\s+fuel\s+price\s+adjustment|"
        r"fuel\s+price\s+adjustment|"
        r"price\s+adjustment|"
        r"adjustment\s+charge"
        r")\b",
        normalized_name,
        re.I,
    ):
        if not item.get("_source_fact_verified"):
            return None, "adjustment_note_row_skipped"
    if re.match(
        r"^\s*(?:grand\s+total|sub\s*total|subtotal|total|net\s+amount|taxable\s+amount|discount|rebate|deduction)\b",
        normalized_name,
        re.I,
    ):
        return None, "tax_or_total_row_skipped"
    if row_type == "CHARGE":
        tax_name = re.sub(
            r"[^a-z0-9%]+",
            " ",
            normalized_name.lower(),
        ).strip()
        if re.search(r"\b(?:discount|rebate|deduction)\b", tax_name, re.I):
            return None, "discount_row_skipped"
        if re.fullmatch(
            r"(?:vat|tax|gst)(?:\s*(?:at)?\s*\d+(?:\.\d+)?\s*%?)?",
            tax_name,
            re.I,
        ):
            return None, "tax_or_total_row_skipped"

        if tax_name in {
            "subtotal",
            "sub total",
            "grand total",
            "total",
            "balance",
            "round off",
            "rounding off",
            "amount in words",
        }:
            return None, "tax_or_total_row_skipped"
    if _bad_product_name(product_name, row_type):
        if product_name:
            warnings.append("questionable product_name kept for review")
        else:
            # A readable commercial amount from an informal chat is still a
            # valid extraction row. Keep it for review instead of dropping
            # the price merely because the screenshot has no item heading.
            has_amount = item.get("unit_price") not in (None, "") or item.get("line_total") not in (None, "") or item.get("total") not in (None, "")
            if not has_amount:
                return None, "missing product_name"
            warnings.append("missing product_name")

    quantity = _decimal(item.get("quantity"))
    unit_price = _money_decimal(item.get("unit_price"), row_type, product_name)
    line_total = _money_decimal(item.get("total") if item.get("total") not in (None, "") else item.get("line_total"), row_type, product_name)
    raw_total = item.get("total") if item.get("total") not in (None, "") else item.get("line_total")
    # Zero-value / FOC rows are not reusable price-history items.
    if (
        unit_price is not None
        and line_total is not None
        and unit_price == Decimal("0")
        and line_total == Decimal("0")
    ):
        return None, "zero_value_row_skipped"

    if not item.get("_comparison_matrix_price") and quantity and unit_price is not None and line_total is not None:
        expected = quantity * unit_price
        tolerance = max(Decimal("0.05"), abs(line_total) * Decimal("0.03"))
        if abs(expected - line_total) > tolerance:
            comma_unit_price = _decimal_comma_decimal(item.get("unit_price"))
            comma_line_total = _decimal_comma_decimal(raw_total)
            if not source_verified and comma_unit_price is not None and comma_line_total is not None:
                comma_expected = quantity * comma_unit_price
                comma_tolerance = max(Decimal("0.05"), abs(comma_line_total) * Decimal("0.03"))
                if abs(comma_expected - comma_line_total) <= comma_tolerance:
                    unit_price = comma_unit_price
                    line_total = comma_line_total

    # Hard rule (not just a prompt request): a row with no legible amount at
    # all is never real commercial data — it is either a hallucinated total,
    # a label with no number next to it, or a misread cell. Drop it here so
    # this holds regardless of what the model actually output, since prompt
    # wording alone was not reliably followed.
    if unit_price is None and line_total is None:
        return None, "no_amount_dropped"
    if (
        quantity is None
        and unit_price is None
        and line_total is not None
    ):
        unit_price = line_total

        warnings.append("unit_price_defaulted_from_line_total")
    if row_type == "PRODUCT" and quantity is not None and quantity <= 0:
        warnings.append("quantity <= 0")
    if unit_price is not None and unit_price < 0:
        warnings.append("unit_price < 0")
    if line_total is not None and line_total < 0:
        warnings.append("line_total < 0")
    if not item.get("_comparison_matrix_price") and quantity and unit_price is not None and line_total is not None:
        expected = quantity * unit_price
        tolerance = max(Decimal("0.05"), abs(line_total) * Decimal("0.03"))

        if abs(expected - line_total) > tolerance:
            difference = line_total - expected
            tax_rate = (difference / expected) if expected else None

            if tax_rate is not None and Decimal("0.045") <= tax_rate <= Decimal("0.055"):
                warnings.append("line_total_includes_tax")
            else:
                warnings.append("Qty x Unit Price does not match Line Total")
                warnings.append("possible_vat_or_total_bound_as_line_total")
    if (
        not source_verified
        and
        quantity is not None
        and quantity > 1
        and unit_price is not None
        and line_total is not None
        and not item.get("_comparison_matrix_price")
    ):
        expected = quantity * unit_price
        tolerance = max(Decimal("0.05"), abs(line_total) * Decimal("0.001"))

        unit_price_text = str(item.get("unit_price") or "")

        looks_derived = bool(
            re.search(r"\.\d{3,}$", unit_price_text)
        )

        if (
            not source_verified and
            looks_derived
            and not item.get("_unit_price_verified_from_msg")
            and abs(expected - line_total) <= tolerance
        ):
            unit_price = line_total
            warnings.append("gemini_divided_single_visible_amount_corrected")
    try:
        page_number = int(item.get("page_number")) if item.get("page_number") not in (None, "") else None
    except (TypeError, ValueError):
        page_number = None

    currency = _valid_currency(
        item.get("currency")
    )

    if not currency:
        warnings.append("currency_missing_from_source")

    parent_category = _clean_text(item.get("parent_category"))
    display_name = _clean_text(item.get("display_name"))
    if parent_category and product_name and (not display_name or display_name == product_name):
        display_name = f"{parent_category} | {product_name}"

    attributes = _normalize_extra_attributes(item.get("extra_attributes"))
    core_attribute_labels = {
        "item code", "item no", "item number", "serial no", "sl no", "s no",
        "part number", "part no", "p n", "p/n", "model", "brand",
        "specification", "remark", "remarks", "note", "notes",
        "quantity", "qty", "uom", "unit", "unit of measurement",
        "charge per", "per", "unit price", "unit rate", "rate", "price",
        "line total", "total", "amount", "currency", "date", "document date",
        "supplier", "supplier name",
    }
    attributes = [
        attribute for attribute in attributes
        if re.sub(r"[^a-z0-9]+", " ", str(attribute.get("label") or "").casefold()).strip()
        not in core_attribute_labels
    ]
    optional_fields = (
        ("item_code", "ITEM CODE"), ("part_number", "PART NUMBER"),
        ("brand", "BRAND"), ("model", "MODEL"),
        ("specification", "SPECIFICATION"), ("remark", "REMARKS"),
        ("note", "NOTE"),
    )
    explicit_attribute_labels = {
        "item code", "item no", "item number", "part number", "part no",
        "p/n", "p n", "pn", "model", "model no", "model number", "brand",
        "specification", "material", "remarks", "remark", "notes", "note",
    }
    explicit_labels = {
        re.sub(r"\s+", " ", str(a.get("label") or "")).strip().casefold()
        for a in attributes if isinstance(a, dict)
    }
    existing_attributes = {(str(a.get("label") or "").casefold(), str(a.get("value") or "").strip())
                          for a in attributes if isinstance(a, dict)}
    for field, label in optional_fields:
        value = _clean_text(item.get(field))
        # Legacy dedicated fields are only promoted when the source already
        # carried an explicit labelled field.  A value-shaped identifier in a
        # description (especially a parenthesized suffix) is not evidence of
        # a Part Number/Model column and must remain part of product_name.
        if (value and label.casefold() in explicit_labels
                and (label.casefold(), value) not in existing_attributes):
            attributes.append({"label": label, "value": value})

    return {
        "row_type": row_type,
        "parent_category": parent_category,
        "_source_currency_missing": item.get('_source_currency_missing', False),
        "contains_handwriting": item.get('contains_handwriting') is True,
        "extra_attributes": attributes,
        "product_name": product_name,
        "display_name": display_name or product_name,
        "purchase_date": _valid_date(item.get("purchase_date")),
        "source_reference": _clean_text(item.get("source_reference")),
        "source_context": _clean_text(item.get("source_context")),
        "document_number": None,
        "quantity": str(quantity) if quantity is not None else None,
        "unit_of_measurement": _canonical_uom(item.get("uom") or item.get("unit_of_measurement")),
        "unit_price": str(unit_price) if unit_price is not None else None,
        "line_total": str(line_total) if line_total is not None else None,
        "currency": currency,
        "page_number": page_number,
        "supplier_name": _normalize_supplier_name(item.get("supplier_name")),
        "buyer_name": None,
        "source_document_type": _clean_text(item.get("source_document_type")),
        "source_category": (
            "INVOICE"
            if _normalize_document_type(
                item.get("source_document_type")
            ) == "proforma_invoice"
            else (
                _normalize_category(
                    item.get("source_category")
                    or item.get("source_document_type")
                )
                or "OTHER"
            )
        ),
        "source_document_number": None,
        "_unit_price_verified_from_msg": bool(
            item.get("_unit_price_verified_from_msg")
        ),
        "_keep_duplicate_priced_row": bool(
            item.get("_keep_duplicate_priced_row")
        ),
        "price_reference_quality": _price_reference_quality({
            "quantity": str(quantity) if quantity is not None else None,
            "unit_price": str(unit_price) if unit_price is not None else None,
            "line_total": str(line_total) if line_total is not None else None,
        }),
        "_table_source_verified": bool(item.get("_table_source_verified")),
        "_comparison_matrix_price": bool(item.get("_comparison_matrix_price")),
        "_comparison_cell_key": item.get("_comparison_cell_key"),
        "_source_page": item.get("_source_page"),
        "_source_row_order": item.get("_source_row_order"),
        "_supplier_column_index": item.get("_supplier_column_index"),
    }, "; ".join(warnings)


class ExtractionValidator:
    """
    Structured validation for raw Gemini extraction.

    This reports issues only. It does not change extracted values; focused
    retry is responsible for confirming corrections.
    """

    RETRY_CODES = {
        "NO_COMMERCIAL_ITEMS",
        "MISSING_PRODUCT_NAME",
        "MISSING_AMOUNT",
        "VISIBLE_ROW_COUNT_MISMATCH",
        "PRICE_BINDING_MISMATCH",
        "DATE_UNCERTAIN",
        "SUPPLIER_UNCERTAIN",
        "CURRENCY_UNCERTAIN",
        "SUBTOTAL_MISMATCH",
        "SUPPORTING_PAGE_ITEM",
        "DUPLICATE_SOURCE_ROW",
    }

    def validate(self, result, page_number=None):
        result = dict(result or {})
        items = list(result.get("items") or [])
        issues = []

        if not items:
            issues.append(self._issue(
                "NO_COMMERCIAL_ITEMS",
                page_number,
                [],
                "No commercial priced items were extracted.",
            ))

        expected_count = self._int_or_none(result.get("visible_priced_row_count"))

        if expected_count is not None and expected_count != len(items):
            issues.append(self._issue(
                "VISIBLE_ROW_COUNT_MISMATCH",
                page_number,
                [],
                f"Expected {expected_count} priced rows, got {len(items)}.",
            ))

        if not self._clean(result.get("supplier_name")) and not any(
            self._clean(item.get("supplier_name"))
            for item in items
        ):
            issues.append(self._issue(
                "SUPPLIER_UNCERTAIN",
                page_number,
                [],
                "Supplier was not extracted from the commercial source.",
            ))

        if not _valid_date(result.get("document_date")) and not any(
            _valid_date(item.get("purchase_date"))
            for item in items
        ):
            issues.append(self._issue(
                "DATE_UNCERTAIN",
                page_number,
                [],
                "Commercial document date was not extracted reliably.",
            ))

        if not _valid_currency(result.get("currency")) and not any(
            _valid_currency(item.get("currency"))
            for item in items
        ):
            issues.append(self._issue(
                "CURRENCY_UNCERTAIN",
                page_number,
                [],
                "Currency was not extracted reliably.",
            ))

        commercial_totals = []

        for index, item in enumerate(items):
            row_type = _canonical_row_type(item.get("row_type"), item)
            name = self._clean(
                item.get("product_name")
                or item.get("product_service_name")
                or item.get("description")
            )
            unit_price = _decimal(item.get("unit_price"))
            line_total = _decimal(
                item.get("line_total")
                if item.get("line_total") not in (None, "")
                else item.get("total")
            )
            quantity = _decimal(item.get("quantity"))

            if row_type in {"PRODUCT", "SERVICE", "CHARGE"} and not name:
                issues.append(self._issue(
                    "MISSING_PRODUCT_NAME",
                    page_number,
                    [index],
                    "Priced item has no product/service name.",
                ))

            if row_type in {"PRODUCT", "SERVICE", "CHARGE"} and unit_price is None and line_total is None:
                issues.append(self._issue(
                    "MISSING_AMOUNT",
                    page_number,
                    [index],
                    "Priced item has no unit_price or line_total.",
                ))

            if not item.get("_comparison_matrix_price") and quantity is not None and unit_price is not None and line_total is not None:
                expected = quantity * unit_price
                tolerance = max(Decimal("0.05"), abs(line_total) * Decimal("0.001"))
                if abs(expected - line_total) > tolerance:
                    issues.append(self._issue(
                        "PRICE_BINDING_MISMATCH",
                        page_number,
                        [index],
                        "Quantity x unit_price does not match line_total.",
                    ))

            if line_total is not None and row_type in {"PRODUCT", "SERVICE", "CHARGE"}:
                commercial_totals.append(line_total)

            supporting_text = " ".join(
                str(item.get(key) or "")
                for key in (
                    "product_name",
                    "parent_category",
                    "source_document_type",
                    "source_category",
                )
            )
            quote_context = {**item,
                'source_document_type': item.get('source_document_type') or result.get('document_type'),
                'source_category': item.get('source_category') or result.get('category')}
            if not _is_quoted_commercial_line(quote_context) and re.search(r"\b(gate pass|access fee|permit|portal fee)\b", supporting_text, re.I):
                issues.append(self._issue(
                    "SUPPORTING_PAGE_ITEM",
                    page_number,
                    [index],
                    "Supporting/non-commercial page item was extracted.",
                ))

        for index, item in enumerate(items):
            duplicate_index = next((
                earlier_index for earlier_index, earlier in enumerate(items[:index])
                if is_true_duplicate(earlier, item)
            ), None)
            if duplicate_index is not None:
                issues.append(self._issue(
                    "DUPLICATE_SOURCE_ROW", page_number, [duplicate_index, index],
                    "Duplicate source row extracted more than once.",
                ))

        subtotal = _decimal(result.get("subtotal"))
        if subtotal is not None and commercial_totals:
            total = sum(commercial_totals)
            tolerance = max(Decimal("0.10"), abs(subtotal) * Decimal("0.01"))
            if abs(total - subtotal) > tolerance:
                issues.append(self._issue(
                    "SUBTOTAL_MISMATCH",
                    page_number,
                    [],
                    "Subtotal does not match extracted commercial line totals.",
                ))

        retry_issues = [
            issue
            for issue in issues
            if issue["code"] in self.RETRY_CODES
        ]

        return {
            "is_valid": not issues,
            "requires_retry": bool(retry_issues),
            "issues": issues,
        }

    def _issue(self, code, page_number, item_indexes, message):
        return {
            "code": code,
            "page_number": page_number,
            "item_indexes": item_indexes,
            "message": message,
        }

    def _clean(self, value):
        text = str(value or "").strip()
        if text.upper() == "UNKNOWN":
            return ""
        return text

    def _int_or_none(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def build_retry_prompt(validation_issues, page_number=None):
    codes = {
        issue.get("code")
        for issue in validation_issues or []
    }

    rules = [
        "Return schema JSON only.",
        "Inspect only the supplied current page.",
        "Do not use filename, upload date or current date.",
        "Do not guess uncertain values; return null.",
    ]

    if "VISIBLE_ROW_COUNT_MISMATCH" in codes or "NO_COMMERCIAL_ITEMS" in codes:
        rules.append("Re-check every separate visible commercial row or amount and return each exactly once.")

    if "PRICE_BINDING_MISMATCH" in codes or "SUBTOTAL_MISMATCH" in codes:
        rules.append("Verify only row binding: description, quantity, rate and amount must come from the same visual row.")
        rules.append("Do not force item amounts to match subtotal; preserve visible row values.")

    if "DATE_UNCERTAIN" in codes:
        rules.append("Verify only the commercial document date visible for this page/source section.")

    if "SUPPLIER_UNCERTAIN" in codes:
        rules.append("Verify only the vendor/company providing the commercial price, not buyer/recipient.")

    if "CURRENCY_UNCERTAIN" in codes:
        rules.append("Verify only currency explicitly printed beside the relevant amount/header.")

    if "SUPPORTING_PAGE_ITEM" in codes:
        rules.append("If this is only a permit, gate pass, access fee, tax, total, or other support page, return no items.")

    if "DUPLICATE_SOURCE_ROW" in codes:
        rules.append("Remove duplicate extractions. Keep each visible source row once, unless the document visibly repeats it as a separate priced row.")

    page_text = (
        f" for original page {page_number}"
        if page_number not in (None, "")
        else ""
    )

    return (
        f"Focused retry{page_text}.\n"
        + SCHEMA_FIELD_PROMPT
        + "\nRules:\n- "
        + "\n- ".join(rules)
    )


def validate_gemini_extraction(result):
    result = normalize_result(result)
    result = _collapse_parallel_currency_rows(result)
    result = normalize_result(result)
    warnings = []
    suspicious_items = []
   
    if not result.get("document_date"):
        warnings.append("missing_document_date")
    if not _valid_currency(result.get("currency")) and (
        not result.get("items") or any(
            not _valid_currency(item.get("currency"))
            for item in result.get("items") or []
            if item.get("unit_price") not in (None, "") or item.get("line_total") not in (None, "")
        )
    ):
        warnings.append("missing_currency")
    item_suppliers = {
        str(item.get("supplier_name") or "").strip()
        for item in result.get("items") or []
        if (
            str(item.get("supplier_name") or "").strip()
            and str(
                item.get("supplier_name")
            ).strip().upper() != "UNKNOWN"
        )
    }

    if (
        not result.get("supplier_name")
        and not item_suppliers
    ):
        warnings.append("missing_supplier_name")
    if not result.get("items"):
        warnings.append("no_items")
    else:
        row_types_present = {
            str(item.get("item_type") or item.get("row_type") or "").upper()
            for item in result.get("items") or []
        }
        if row_types_present == {"CHARGE"}:
            # A document with ONLY charge/fee items and no PRODUCT or SERVICE
            # line is unusual — freight/shipping/goods documents almost
            # always have a main billable item alongside any extra fees.
            # This usually means the main item was missed during extraction.
            # Flag for review rather than silently accepting an incomplete
            # result.
            warnings.append("charges_only_no_main_item_possible_miss")

    all_normalized = [
        {"mapped": mapped, "warning": warning}
        for mapped, warning in (
            _normalize_item(item, result.get("currency"))
            for item in result.get("items") or []
        )
    ]
    _flag_grand_total_merged_rows(
        all_normalized,
        _decimal(result.get("grand_total")),
        _decimal(result.get("subtotal")),
    )
    _reconcile_duplicate_product_prices(all_normalized)

    for index, item in enumerate(result.get("items") or []):
        mapped, warning = all_normalized[index]["mapped"], all_normalized[index]["warning"]
        reasons = []
        needs_verification = False
        if not mapped:
            ignored_invalid_warnings = {
                "tax_or_total_row_skipped",
                "summary_or_non_reusable_service_skipped",
                "zero_value_row_skipped",
                "document_summary_amount_dropped",
                "unit_price_defaulted_from_line_total",
                "non_commercial_arabic_template_row_skipped",
                "non_commercial_template_row_skipped",
                "permit_non_commercial_row_skipped",
                "adjustment_note_row_skipped",
                "no_amount_dropped",
            }

            if warning not in ignored_invalid_warnings:
                reasons.append(warning or "invalid_item")
                needs_verification = True
        else:
            if not mapped.get("product_name"):
                reasons.append("missing_product_service_name")
                needs_verification = True
            if mapped["row_type"] == "PRODUCT" and not mapped.get("quantity"):
                reasons.append("missing_quantity")
            if mapped["row_type"] == "PRODUCT" and not (mapped.get("unit_price") or mapped.get("line_total")):
                reasons.append("missing_product_amount")
                needs_verification = True
            if warning:
                ignored_mapped_warnings = {
                    "unit_price_defaulted_from_line_total",
                    "line_total_includes_tax",
                    "gemini_divided_single_visible_amount_corrected",
                }

                if warning not in ignored_mapped_warnings:
                    reasons.append(warning)

                    if (
                        "does not match" in warning
                        or "questionable product_name" in warning
                        or "possible_summary_row_merge" in warning
                    ):
                        needs_verification = True
            
        non_review_reasons = {
            "unit_price_defaulted_from_line_total",
            "line_total_includes_tax",
            "gemini_divided_single_visible_amount_corrected",
        }

        reasons = [
            reason
            for reason in reasons
            if reason not in non_review_reasons
        ]

        if reasons and needs_verification:
            suspicious_items.append({"index": index, "reasons": reasons, "item": item})

    return {
        "warnings": warnings,
        "suspicious_items": suspicious_items,
        "is_valid": not warnings and not suspicious_items,
    }

def _extraction_quality_issues(result):
    issues = []
    items = list((result or {}).get("items") or [])

    def explicit_tax_amount(item):
        """Return an explicitly extracted VAT/tax amount, if present."""
        for key in ("vat_amount", "tax_amount", "gst_amount", "vat", "tax", "gst"):
            amount = _decimal(item.get(key))
            if amount is not None:
                return amount
        for attribute in item.get("extra_attributes") or []:
            if not isinstance(attribute, dict):
                continue
            label = re.sub(r"[^a-z0-9]+", " ", str(attribute.get("label") or "").lower()).strip()
            if label in {"vat", "vat amount", "tax", "tax amount", "gst", "gst amount"}:
                amount = _decimal(attribute.get("value"))
                if amount is not None:
                    return amount
        return None

    if not items:
        issues.append("No commercial priced items were extracted.")
        return issues

    for index, item in enumerate(items, start=1):
        row_type = _canonical_row_type(item.get("row_type"), item)
        product_name = _clean_text(item.get("product_name"))
        unit_price = _decimal(item.get("unit_price"))
        line_total = _decimal(item.get("line_total"))

        if row_type == "UNKNOWN":
            issues.append(f"Row {index}: unknown row_type.")

        if not product_name:
            issues.append(f"Row {index}: missing product/service description.")

        if unit_price is None and line_total is None:
            issues.append(f"Row {index}: missing price.")

        if not _valid_currency(item.get("currency")) and not _valid_currency((result or {}).get("currency")):
            issues.append(f"Row {index}: missing explicit currency.")

        quantity = _decimal(item.get("quantity"))
        if quantity is not None and unit_price is not None and line_total is not None and not item.get("_capacity_tier"):
            expected_total = quantity * unit_price
            tolerance = max(Decimal("0.05"), abs(expected_total) * Decimal("0.02"))
            if abs(expected_total - line_total) > tolerance:
                tax_amount = explicit_tax_amount(item)
                tax_inclusive_total = expected_total + tax_amount if tax_amount is not None else None
                if (
                    tax_inclusive_total is None
                    or abs(tax_inclusive_total - line_total) > tolerance
                ):
                    mismatch_is_valid_tax_total = False
                    if len(items) == 1:
                        document_subtotal = _decimal(result.get("subtotal"))
                        document_grand_total = _decimal(result.get("grand_total"))
                        if (
                            document_subtotal is not None
                            and document_grand_total is not None
                            and document_grand_total > document_subtotal
                            and abs(expected_total - document_subtotal) <= tolerance
                            and abs(line_total - document_grand_total) <= tolerance
                        ):
                            mismatch_is_valid_tax_total = True
                    if not mismatch_is_valid_tax_total:
                        issues.append(f"Row {index}: quantity x unit_price does not match line_total.")

    visible_count = result.get("visible_priced_row_count")
    try:
        visible_count = int(visible_count)
    except (TypeError, ValueError):
        visible_count = None

    # visible_priced_row_count is model-generated, so do not fail/review a
    # batch just because the count differs. It often counts VAT/subtotal/summary
    # rows in invoice PDFs. Only flag when Gemini says there are priced rows but
    # returned none.
    if visible_count is not None and visible_count > 0 and not items:
        issues.append(
            f"Gemini counted {visible_count} priced rows but returned 0 rows."
        )

    return issues
def gemini_result_to_virtual_sheet(result, fallback_currency=None):
    from api.services.commercial_attribute_policy import clean_commercial_display
    result = copy.deepcopy(result or {})
    for source in result.get('items') or []:
        clean_commercial_display(source)
    if ((result or {}).get('_physical_pdf_complete') or result.get('_source_attachment_finalized')
            or (result.get('items') and all(i.get('_physical_pdf_row') for i in result['items']))):
        from api.services.pdf_commercial_geometry import PROTECTED_FIELDS
        headers = list(dict.fromkeys([*GEMINI_VIRTUAL_HEADERS, *PROTECTED_FIELDS]))
        rows = [headers]
        row_meta = []
        for source in result.get('items') or []:
            mapped = copy.deepcopy(source)
            mapped['unit_of_measurement'] = source.get('uom')
            rows.append([mapped.get(header) for header in headers])
            row_meta.append({field: {'confidence': 100, 'source': {'type': 'gemini'}}
                             for field in mapped if mapped[field] not in (None, '')})
        return {'sheet_name': 'Gemini Extracted', 'sheet_index': 0,
                'is_virtual': True, 'rows': rows, 'row_meta': row_meta,
                'gemini_validation_warnings': [], 'validation_rejected_count': 0}
    # Keep the finalized extraction as the source of truth.  Some legacy
    # reconciliation helpers normalize the same nested dictionaries in place;
    # that must never replace a verified source-backed product identity before
    # the virtual sheet is built.
    source_result = copy.deepcopy(result or {})
    result = normalize_result(copy.deepcopy(source_result))
    result = _reconcile_placeholder_commercial_items(result)
    result = _collapse_parallel_currency_rows(result)
    result = normalize_result(result)
    result = _reconcile_placeholder_commercial_items(result)
    # These helpers may rebuild item metadata.  Email-thread invariants are
    # authoritative and must be reapplied immediately before row generation.
    result = _finalize_email_quote_metadata(result)
    rows = [GEMINI_VIRTUAL_HEADERS]
    row_meta = []
    warnings = []
 
    default_currency = _valid_currency(
        result.get("currency")
    )

    normalized_entries = [
        {"mapped": mapped, "warning": warning}
        for mapped, warning in (
            _normalize_item(item, default_currency)
            for item in result.get("items") or []
        )
    ]
    _flag_grand_total_merged_rows(
        normalized_entries,
        _decimal(result.get("grand_total")),
        _decimal(result.get("subtotal")),
    )
    _reconcile_duplicate_short_product_names(normalized_entries)
    _reconcile_duplicate_product_prices(normalized_entries)

    for entry in normalized_entries:
        mapped, warning = entry["mapped"], entry["warning"]
        logger.warning(
            "DISPLAY_TRACE virtual product=%r display=%r extra=%r quantity=%r",
            mapped.get("product_name") if mapped else None,
            mapped.get("display_name") if mapped else None,
            mapped.get("extra_attributes") if mapped else None,
            mapped.get("quantity") if mapped else None,
        )
        if not mapped:
            warnings.append(warning)
            continue


        rows.append([mapped.get(header) for header in GEMINI_VIRTUAL_HEADERS])
        meta = {field: {"confidence": 82, "source": {"type": "gemini"}} for field, value in mapped.items() if value not in (None, "")}
        if warning:
            meta["validation_warning"] = {"confidence": 60, "source": {"type": "gemini_validation"}, "message": warning}
            warnings.append(warning)
        row_meta.append(meta)

    safe_normalization_warnings = {
        "adjustment_note_row_skipped",
        "duplicate_commercial_row_removed",
        "unit_price_defaulted_from_line_total",
        "document_summary_row_removed",
    }
    return {
        "sheet_name": "Gemini Extracted",
        "sheet_index": 0,
        "is_virtual": True,
        "rows": rows,
        "row_meta": row_meta,
        "gemini_validation_warnings": warnings,
        "validation_rejected_count": sum(
            1 for entry in normalized_entries
            if not entry["mapped"]
            and entry["warning"] not in safe_normalization_warnings
        ),
    }
def _extract_pdf_email_freight_charges(file_bytes, supplier_name=None):
    """
    Extract freight/shipment charges from PDF email threads.

    Handles repeated forwarded/replied emails where each section has:
    Sent/From date
    route text like "from China to Muscat"
    charge line like "Door to Airport - USD 1742/- 206 kgs"
    POL/POD lines

    This is generic and only returns rows when a visible freight amount exists.
    """

    if not file_bytes:
        return []

    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return []

    if not text.strip():
        return []

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
    ]
    lines = [line for line in lines if line]

    items = []
    current_date = None
    current_route = None
    current_pol = None
    current_pod = None

    for index, line in enumerate(lines):
        sent_match = re.search(
            r"\b(?:Sent|Date)\s*:\s*"
            r"(?:[A-Za-z]+,\s*)?"
            r"(?P<date>\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},\s+\d{4})",
            line,
            re.I,
        )

        if sent_match:
            current_date = _valid_date(sent_match.group("date"))
            current_route = None
            current_pol = None
            current_pod = None
            continue

        route_match = re.search(
            r"\b(?:charges|shipment|freight).*?\bfrom\s+(?P<from>.+?)\s+to\s+(?P<to>.+?)(?:\.|$)",
            line,
            re.I,
        )

        if route_match:
            from_place = route_match.group("from").strip(" .:-")
            to_place = route_match.group("to").strip(" .:-")
            current_route = f"{from_place} to {to_place}"

        pol_match = re.search(r"\bPOL\s*:?\s*(?P<pol>[A-Za-z0-9 /.-]+)", line, re.I)
        if pol_match:
            current_pol = pol_match.group("pol").strip(" .:-")

        pod_match = re.search(r"\bPOD\s*:?\s*(?P<pod>[A-Za-z0-9 /.-]+)", line, re.I)
        if pod_match:
            current_pod = pod_match.group("pod").strip(" .:-")

        charge_match = re.search(
            r"(?P<service>door\s+to\s+airport|airport\s+to\s+door|door\s+to\s+door|air\s+freight|sea\s+freight|freight)"
            r"\s*[-:–]?\s*"
            r"(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)"
            r"\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)"
            r"(?:\s*/-)?"
            r"(?:\s*(?P<weight>\d+(?:\.\d+)?)\s*(?P<uom>kgs?|kg|tons?|cbm))?",
            line,
            re.I,
        )

        if not charge_match:
            continue

        amount = charge_match.group("amount").replace(",", "")
        currency = _valid_currency(charge_match.group("currency"))
        service = re.sub(r"\s+", " ", charge_match.group("service")).strip().title()
        weight = charge_match.group("weight")
        weight_uom = charge_match.group("uom")

        name_parts = ["Freight"]

        if current_route:
            name_parts.append(current_route)

        name_parts.append(service)

        if current_pol:
            name_parts.append(f"POL: {current_pol}")

        if current_pod:
            name_parts.append(f"POD: {current_pod}")

        product_name = " | ".join(name_parts)

        if weight and weight_uom:
            product_name = f"{product_name} - {weight} {weight_uom}"

        items.append({
            "row_type": "CHARGE",
            "product_name": product_name,
            "quantity": "1",
            "uom": "LS",
            "unit_price": amount,
            "line_total": amount,
            "currency": currency,
            "supplier_name": supplier_name,
            "purchase_date": current_date,
            "source_document_type": "QUOTATION",
            "source_category": "QUOTATION",
            "price_reference_quality": 5,
            "_keep_duplicate_priced_row": True,
            "source_row_id": f"pdf-text-line:{index}",
        })

    unique = []
    for item in items:
        if not _has_equivalent_commercial_item(unique, item):
            unique.append(item)
    return unique
def _tariff_recovery_key(value):
    text = str(value or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*h(?:r|rs|ours)?\b", " ", text)
    text = re.sub(
        r"\b(service|charges?|charge|hire|operation|for|at|the|same|remains|please|find|our|below|tariff|per|hr|hour|hours|base|to|ls|lumpsum|lump|sum)\b",
        " ",
        text,
    )
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()




def _has_equivalent_commercial_item(items, candidate):
    return any(is_true_duplicate(existing, candidate) for existing in items or [])


def _deduplicate_equivalent_commercial_items(result, original_file_name=""):
    """Remove only repeats of the same source row; preserve uncertain records."""
    items = list(result.get("items") or [])
    result.setdefault("_raw_extracted_count", len(items))
    kept = []

    def compatible_name(left, right):
        left = re.sub(r"[^\w\u0080-\uffff]+", " ", str(left or "").casefold()).strip()
        right = re.sub(r"[^\w\u0080-\uffff]+", " ", str(right or "").casefold()).strip()
        if not left or not right:
            return False
        if left == right:
            return True
        shorter, longer = sorted((left, right), key=len)
        if not longer.startswith(shorter + " "):
            return False
        suffix = longer[len(shorter):].strip(" -")
        return bool(suffix and (re.search(r"\d", suffix) or re.search(r"[^\x00-\x7f]", suffix)))

    def same_price_row(left, right):
        same_page = not left.get("page_number") or not right.get("page_number") or str(left.get("page_number")) == str(right.get("page_number"))
        return (
            same_page
            and compatible_name(left.get("product_name"), right.get("product_name"))
            and _valid_currency(left.get("currency")) == _valid_currency(right.get("currency"))
            and _decimal(left.get("quantity")) == _decimal(right.get("quantity"))
            and _decimal(left.get("unit_price")) == _decimal(right.get("unit_price"))
        )

    for item in items:
        # A comparison-table fallback can emit the same priced row twice:
        # once with the real supplier and once with an empty supplier after a
        # blank supplier cell shifted the model's column interpretation.  An
        # unassigned row is safe to discard only when an otherwise identical
        # commercial fact already has a named supplier.
        item_supplier = _clean_text(item.get("supplier_name"))
        matching_named = next(
            (
                existing for existing in kept
                if _clean_text(existing.get("supplier_name"))
                and same_price_row(existing, item)
                and _valid_currency(existing.get("currency")) == _valid_currency(item.get("currency"))
                and _decimal(existing.get("quantity")) == _decimal(item.get("quantity"))
                and _decimal(existing.get("unit_price")) == _decimal(item.get("unit_price"))
                and _decimal(existing.get("line_total")) == _decimal(item.get("line_total"))
            ),
            None,
        )
        if not item_supplier and matching_named:
            continue
        if item_supplier:
            kept = [
                existing for existing in kept
                if not (
                    not _clean_text(existing.get("supplier_name"))
                    and same_price_row(existing, item)
                    and _valid_currency(existing.get("currency")) == _valid_currency(item.get("currency"))
                    and _decimal(existing.get("quantity")) == _decimal(item.get("quantity"))
                    and _decimal(existing.get("unit_price")) == _decimal(item.get("unit_price"))
                    and _decimal(existing.get("line_total")) == _decimal(item.get("line_total"))
                )
            ]
        if not _has_equivalent_commercial_item(kept, item):
            kept.append(item)
    removed = len(items) - len(kept)
    if removed:
        result["_deduplicated_item_count"] = int(result.get("_deduplicated_item_count") or 0) + removed
        actions = list(result.get("_normalization_actions") or [])
        actions.extend(["duplicate_commercial_row_removed"] * removed)
        result["_normalization_actions"] = actions
        logger.info(
            "POSTPROCESS %s before=%s after=%s removed=%s",
            "_deduplicate_equivalent_commercial_items", len(items), len(kept), removed,
        )
    result["items"] = kept
    return result


def _normalize_narrative_service_identities(result):
    """Use explicit service context to clean narrative tariff labels only."""
    items = list(result.get("items") or [])
    contextual_names = []
    for item in items:
        name = str(item.get("product_name") or "").strip()
        lowered = name.lower()
        if (
            str(item.get("row_type") or "").upper() in {"SERVICE", "CHARGE"}
            and "launch hire" in lowered
            and "updated tariff" not in lowered
        ):
            contextual_names.append(name)

    context_name = contextual_names[0] if contextual_names else None
    for item in items:
        if item.get("_source_fact_verified"):
            continue
        name = str(item.get("product_name") or "").strip()
        lowered = name.lower()
        if re.fullmatch(r"(?:the\s+)?updated\s+tariff", lowered) and context_name:
            item["product_name"] = context_name
            item["display_name"] = context_name
        elif re.search(r"(?:charges?|costs?)?\s*for\s+diving\s+jobs|diving\s+jobs\s+at\b", lowered):
            item["product_name"] = "BOAT / LAUNCH HIRE FOR DIVING OPERATION"
            item["display_name"] = item["product_name"]
        elif re.search(r"\b(?:launch\s+hire|boat\s+charges?).*\blumpsum\b", lowered):
            item["product_name"] = re.sub(r"\s*\(\s*lumpsum\s*\)\s*", "", name, flags=re.I).strip()
            item["display_name"] = item["product_name"]
        elif re.search(r"\b(?:launch\s+hire|boat\s+charges?).*\bafter\s+\d+\s*hrs?\b", lowered):
            item["product_name"] = re.sub(r"\s*\(\s*", " - ", name, count=1).replace(")", "").strip()
            item["display_name"] = item["product_name"]
        elif re.search(r"\b(?:launch\s+hire|boat\s+charges?).*\b(?:\d+\s*hrs?|usd\s*/?\s*hr)\b", lowered):
            if item.get("currency") == "AED" and item.get("line_total"):
                item["product_name"] = "Launch hire"
                item["display_name"] = item["product_name"]
    result["items"] = items
    return result


def _summary_charge_context(lines, index, name):
    name_text = str(name or "").lower()

    if not re.search(r"\b(launch|hire|boat|diving|crane)\b", name_text, re.I):
        return []

    current_line = str(lines[index] or "")
    nearby = " ".join(str(line or "") for line in lines[max(index - 3, 0):index + 8])
    attributes = []

    inline_rate = re.search(
        r"\((?P<rate>[^)]*(?:USD|AED|EUR|GBP|INR|\$)[^)]*(?:hr|hour|hrs|hours|ls|lumpsum)[^)]*)\)",
        current_line,
        re.I,
    )
    if inline_rate:
        rate_value = re.sub(r"\s+", " ", inline_rate.group("rate")).strip()
        rate_value = rate_value.replace("$", "USD ")
        attributes.append({
            "label": "Rate Basis",
            "value": rate_value,
        })

    ls_rate = re.search(
        r"\bLS\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)\s*USD\s*till\s*(?P<hours>\d+(?:\.\d+)?)\s*h(?:r|rs|ours)?\b",
        nearby,
        re.I,
    )
    if ls_rate and not any(attr.get("label") == "Rate Basis" for attr in attributes):
        attributes.append({
            "label": "Rate Basis",
            "value": f"{ls_rate.group('amount')} USD LS till {ls_rate.group('hours')} hr",
        })

    hourly_after = re.search(
        r"\bthen\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)\s*USD\s*/?\s*hr\s*after\s*(?P<hours>\d+(?:\.\d+)?)\s*h(?:r|rs|ours)?\b",
        nearby,
        re.I,
    )
    if hourly_after:
        attributes.append({
            "label": "Additional Rate",
            "value": f"{hourly_after.group('amount')} USD/hr after {hourly_after.group('hours')} hr",
        })

    duration = re.search(
        r"\((?P<duration>\d+\s*hrs?\s*\d+\s*min)\)",
        nearby,
        re.I,
    )
    if duration:
        attributes.append({
            "label": "Duration",
            "value": re.sub(r"\s+", " ", duration.group("duration")).strip(),
        })
    else:
        till_hours = re.search(
            r"\btill\s*(?P<hours>\d+(?:\.\d+)?)\s*h(?:r|rs|ours)?\b",
            nearby,
            re.I,
        )
        if till_hours:
            attributes.append({
                "label": "Duration",
                "value": f"Till {till_hours.group('hours')} hr",
            })

    return attributes
def _extract_pdf_email_tariff_charge_items(file_bytes, supplier_name=None):
    """
    Recover visible email/PDF tariff lines that Gemini may misread, especially
    values like USD 1,600/- becoming 1.60.

    Generic rules:
    - Only uses visible PDF text.
    - Handles "USD 1,600/- FOR 08 HRS (LUMPSUM)" style tariff lines.
    - Handles simple AED summary lines like "Launch hire 5876" when nearby text
      says "Total amount (AED)".
    """

    if not file_bytes:
        return []

    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return []

    if not text.strip():
        return []

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
    ]
    lines = [line for line in lines if line]

    full_text = "\n".join(lines)
    default_date = _nearest_previous_email_date(full_text, len(full_text))
    items = []
    last_tariff_base_name = None

    currency_from_total = None
    total_currency_match = re.search(
        r"\bt\s*o\s*t\s*a\s*l\s+amount\s*\((?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\)",
        full_text,
        re.I,
    )
    if total_currency_match:
        currency_from_total = _valid_currency(total_currency_match.group("currency"))

    for index, line in enumerate(lines):
        line_date = _nearest_previous_email_date(full_text, full_text.find(line))
        item_date = line_date or default_date

        # Conditions containing currency thresholds are not quoted services.
        if re.search(r'\b(?:valid|validity|subject to|no more than|not exceeding|threshold)\b', line, re.I):
            continue

        tariff_match = re.search(
            r"(?P<name>.+?)\s*[-:]?\s*"
            r"(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|US\$|\$)"
            r"\s*(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
            r"\s*/-?"
            r"(?:\s*(?P<tail>.*))?$",
            line,
            re.I,
        )

        if tariff_match and not re.match(r"^\s*(?:there\s*after|thereafter)\b", line, re.I):
            name = re.sub(r"\s+", " ", tariff_match.group("name")).strip(" -:")
            name = re.sub(
                r"^(?:please\s+find\s+our\s+below\s+tariff\s+for|further\s+to\s+below\s+message,\s*)",
                "",
                name,
                flags=re.I,
            ).strip(" -:.,")
            name = re.sub(
                r"\bremains\s+the\s+same\b",
                "",
                name,
                flags=re.I,
            ).strip(" -:.,")
            last_tariff_base_name = name
            tail = re.sub(r"\s+", " ", tariff_match.group("tail") or "").strip(" -:.")
            amount = tariff_match.group("amount").replace(",", "")
            currency_text = tariff_match.group("currency")

            if currency_text in {"$", "US$"}:
                currency = "USD"
            else:
                currency = _valid_currency(currency_text)

            extra_attributes = []

            if tail:
                clean_tail = tail

                if re.search(r"\bper\s+hr\b|\bper\s+hour\b", tail, re.I):
                    extra_attributes.append({
                        "label": "Rate Basis",
                        "value": "Per hour",
                    })
                elif re.search(r"\blumpsum\b|\blump\s*sum\b|\bLS\b", tail, re.I):
                    extra_attributes.append({
                        "label": "Rate Basis",
                        "value": "Lump sum",
                    })

                name = re.sub(
                    r"\b(?:USD|AED|EUR|GBP|INR|\$)?\s*\d+(?:,\d{3})*(?:\.\d+)?\s*/?\s*(?:per\s*)?(?:hr|hour|hrs|hours)?\b",
                    " ",
                    name,
                    flags=re.I,
                )
                name = re.sub(r"\s+", " ", name).strip(" -:.,")
            name = re.sub(
                r"^\s*the\s+",
                "",
                name,
                flags=re.I,
            ).strip()
            uom = None
            line_total = amount
            explicit_quantity = re.search(r"[x×]\s*(\d+(?:\.\d+)?)\s*(?:pcs?|pieces?|units?)\b", tail, re.I)
            explicit_total = re.search(r"=\s*(?:USD|EUR|GBP|AED|CNY)?\s*([\d,]+(?:\.\d+)?)", tail, re.I)
            if explicit_quantity:
                quantity = explicit_quantity.group(1)
                uom = "PC"
                if explicit_total:
                    line_total = explicit_total.group(1).replace(",", "")
            else:
                quantity = None
                if re.search(r"/\s*(?:pc|pcs|piece|pieces|unit|units)\b", tail, re.I):
                    uom = "PC"
                    line_total = None
            if re.search(r"\bper\s+hr\b|\bper\s+hour\b", tail, re.I):
                uom = "HR"
                quantity = None
                line_total = None
                name = re.sub(
                    r"\bupdated\s+tariff\b",
                    "launch hire tariff",
                    name,
                    flags=re.I,
                ).strip(" -:.,")
            elif re.search(r"\blumpsum\b|\blump\s*sum\b|\bLS\b", tail, re.I):
                uom = "LS"
                quantity = "1"
                line_total = amount
            if re.search(r"\bfuel\s+price\s+adjustment\b", name, re.I):
                continue
            items.append({
                "row_type": (
                    "CHARGE" if re.search(r"\b(?:certificate|certification|fee|charge|freight|delivery|transport|tax|vat)\b", name, re.I)
                    else "SERVICE" if re.search(r"\b(?:service|hire|labor|labour|installation|repair|maintenance|rental)\b", name, re.I)
                    else "PRODUCT"
                ),
                "product_name": name,
                "quantity": quantity,
                "uom": uom,
                "unit_price": amount,
                "line_total": line_total,
                "currency": currency,
                "supplier_name": supplier_name,
                "purchase_date": item_date,
                "source_document_type": "QUOTATION",
                "source_category": "QUOTATION",
                "extra_attributes": extra_attributes,
                "price_reference_quality": 5,
                "_keep_duplicate_priced_row": True,
                "source_row_id": f"pdf-text-line:{index}",
            })

            continue
        thereafter_match = re.search(
            r"^\s*(?:there\s*after|thereafter)\s+"
            r"(?P<currency>AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)"
            r"\s*(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
            r"\s*/-?\s*(?P<tail>.*)$",
            line,
            re.I,
        )

        if thereafter_match:
            amount = thereafter_match.group("amount").replace(",", "")
            currency = _valid_currency(thereafter_match.group("currency"))
            tail = re.sub(r"\s+", " ", thereafter_match.group("tail") or "").strip(" -:.")
            base_name = last_tariff_base_name or "Additional charge"
            extra_attributes = []

            name = base_name
            if tail:
                clean_tail = re.sub(r"\s+", " ", tail).strip(" -:.")
                clean_tail = re.sub(r"\bper\s+hour\b", "PER HR", clean_tail, flags=re.I)
                clean_tail = re.sub(r"\bper\s+hr\b", "PER HR", clean_tail, flags=re.I)
                clean_tail = clean_tail.upper()
                name = f"{base_name} - {clean_tail}"

            uom = None
            if re.search(r"\bper\s+hr\b|\bper\s+hour\b", tail, re.I):
                uom = "HR"
                extra_attributes.append({
                    "label": "Rate Basis",
                    "value": "Per hour",
                })

            if tail:
                after_hours = re.search(
                    r"\bafter\s*(?P<hours>\d+(?:\.\d+)?)\s*h(?:r|rs|ours)?\b",
                    tail,
                    re.I,
                )
                if after_hours:
                    extra_attributes.append({
                        "label": "Applies After",
                        "value": f"{after_hours.group('hours')} hr",
                    })

            items.append({
                "row_type": "SERVICE",
                "product_name": name,
                "quantity": "1",
                "uom": uom,
                "unit_price": amount,
                "line_total": amount,
                "currency": currency,
                "supplier_name": supplier_name,
                "purchase_date": item_date,
                "source_document_type": "QUOTATION",
                "source_category": "QUOTATION",
                "extra_attributes": extra_attributes,
                "price_reference_quality": 5,
                "_keep_duplicate_priced_row": True,
                "source_row_id": f"pdf-text-line:{index}",
            })

            continue
        summary_match = re.search(
            r"^(?P<name>[A-Za-z][A-Za-z0-9 /().,&-]{2,}?)\s+"
            r"(?P<amount>\d{2,}(?:,\d{3})*(?:\.\d+)?)$",
            line,
            re.I,
        )

        if not summary_match and currency_from_total:
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            next_amount_match = re.match(
                r"^\s*(?P<amount>\d{2,}(?:,\d{3})*(?:\.\d+)?)\s*$",
                next_line,
                re.I,
            )

            if next_amount_match and re.search(
                r"\b(charge|charges|hire|freight|transport|delivery|crane|boat|launch)\b",
                line,
                re.I,
            ):
                summary_match = re.match(
                    r"^(?P<name>[A-Za-z][A-Za-z0-9 /().,&-]{2,}?)$",
                    line,
                    re.I,
                )
                if summary_match:
                    summary_match = {
                        "name": summary_match.group("name"),
                        "amount": next_amount_match.group("amount"),
                    }

        if summary_match and currency_from_total:
            if isinstance(summary_match, dict):
                raw_name = re.sub(r"\s+", " ", summary_match["name"]).strip(" -:")
                amount = summary_match["amount"].replace(",", "")
            else:
                raw_name = re.sub(r"\s+", " ", summary_match.group("name")).strip(" -:")
                amount = summary_match.group("amount").replace(",", "")
            extra_attributes = _summary_charge_context(lines, index, raw_name)

            name = re.sub(
                r"\s*\((?:AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD|US\$|\$)?\s*\d+(?:,\d{3})*(?:\.\d+)?\s*(?:/|per)?\s*(?:hr|hour|hrs|hours)?\s*\)\s*",
                " ",
                raw_name,
                flags=re.I,
            )
            name = re.sub(r"\s+", " ", name).strip(" -:")

            if not re.search(r"\b(charge|charges|hire|freight|transport|delivery|crane|boat|launch)\b", name, re.I):
                continue

            items.append({
                "row_type": "CHARGE",
                "product_name": name,
                "quantity": "1",
                "uom": None,
                "unit_price": amount,
                "line_total": amount,
                "currency": currency_from_total,
                "supplier_name": supplier_name,
                "purchase_date": item_date,
                "source_document_type": "QUOTATION",
                "source_category": "QUOTATION",
                "price_reference_quality": 5,
                "_keep_duplicate_priced_row": True,
                "source_row_id": f"pdf-text-line:{index}",
                "extra_attributes": extra_attributes,
            })

    unique = []
    for item in items:
        if not _has_equivalent_commercial_item(unique, item):
            unique.append(item)
    return unique

def _nearest_previous_email_date(text, position):
    before_text = (text or "")[:position]

    matches = []

    for match in re.finditer(r"\bDate:\s*([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})\b", before_text, re.I):
        year, month, day = match.groups()
        matches.append((match.start(), f"{year}-{int(month):02d}-{int(day):02d}"))

    month_names = {
        "jan": "01", "january": "01",
        "feb": "02", "february": "02",
        "mar": "03", "march": "03",
        "apr": "04", "april": "04",
        "may": "05",
        "jun": "06", "june": "06",
        "jul": "07", "july": "07",
        "aug": "08", "august": "08",
        "sep": "09", "sept": "09", "september": "09",
        "oct": "10", "october": "10",
        "nov": "11", "november": "11",
        "dec": "12", "december": "12",
    }

    sent_pattern = r"\bSent:\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})"
    for match in re.finditer(sent_pattern, before_text, re.I):
        month = month_names.get(match.group(1).lower())
        if month:
            day = int(match.group(2))
            year = match.group(3)
            matches.append((match.start(), f"{year}-{month}-{day:02d}"))

    if not matches:
        return None

    return sorted(matches, key=lambda item: item[0])[-1][1]
def _looks_like_internal_or_buyer_supplier(value):
    text = str(value or "").strip().lower()

    if not text:
        return True

    if "@" in text:
        return True

    if re.search(
        r"\b(to|cc|bcc|dear|regards|buyer|purchase|procurement|trading services)\b",
        text,
        re.I,
    ):
        return True

    return False
def _recover_nearest_commercial_supplier(text, position, current_supplier=None):
    source_text = text or ""

    # Email/reference PDFs can place request text on one page and the actual
    # quoted source/signature lower down or on the next page.
    nearby_text = source_text[max(0, position - 3000): position + 5000]

    patterns = [
        r"(?im)^\s*(?:supplier|vendor|quoted\s+by|seller|company)\s*[:\-]\s*(.+?)\s*$",
        r"(?im)^\s*From\s+([A-Za-z0-9 &.,'()/-]{3,120})(?:,\s*[A-Z][A-Za-z .]{1,40})?\s*$",
        r"(?im)^\s*From\s*:\s*([A-Za-z0-9 &.,'()/-]{3,120})\s*$",
        r"(?im)^\s*(?:Best\s+regards|Regards|Thanks\s+and\s+regards)[,\s]*\n\s*([A-Za-z0-9 &.,'()/-]{3,120})\s*$",
    ]

    candidates = []

    for pattern in patterns:
        for match in re.finditer(pattern, nearby_text):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,-:")

            # Remove trailing country/location after company name.
            value = re.sub(
                r",\s*(?:UAE|USA|UK|China|India|Singapore|Oman|Muscat|Dubai|Abu Dhabi)\s*$",
                "",
                value,
                flags=re.I,
            ).strip(" .,-:")

            if not value:
                continue
            if "@" in value:
                continue
            if _looks_like_internal_or_buyer_supplier(value):
                continue
            if re.search(r"\b(to|cc|bcc|subject|sent|date|dear|reply|forward)\b", value, re.I):
                continue
            if len(value) < 3 or len(value) > 120:
                continue

            candidates.append(value)

    if candidates:
        return candidates[-1]

    if _looks_like_internal_or_buyer_supplier(current_supplier):
        return ""

    return current_supplier or ""
def _recover_pdf_reference_pc_product_names(file_bytes, result):
    if not file_bytes or not result:
        return result

    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        return result

    request_match = re.search(
        r"\bfor\s*(?:for\s*)?(.{3,140}?)\s*[–-]\s*Qty\s*(\d+)",
        text,
        re.I,
    )

    if not request_match:
        return result

    request_name = re.sub(
        r"\s+",
        " ",
        request_match.group(1),
    ).strip(" .:-")

    request_qty = request_match.group(2)

    items = result.get("items") or []
    matched_reference_row = False

    pc_price_match = re.search(
        r"\b(?:USD|US\$|\$)\s*([0-9][0-9,]*(?:\.\d+)?)\s*/\s*(PC|PCS|EA|EACH)\b",
        text,
        re.I,
    )
    reference_unit_price = pc_price_match.group(1).replace(",", "") if pc_price_match else ""


    for item in items:
        product_name_raw = str(item.get("product_name") or "").strip()
        product_name_key = re.sub(r"[^a-z0-9]+", " ", product_name_raw.lower()).strip()
        uom = str(item.get("uom") or item.get("unit_of_measurement") or "").strip().lower()
        unit_price = str(item.get("unit_price") or "").replace(",", "").strip()

        name_is_missing_or_only_uom = _is_generic_product_label(product_name_raw) or product_name_key in {"pc", "pcs", "per pc", "unit", "each", "ea"}

        uom_is_pc = uom in {"pc", "pcs", "ea", "each"}

        price_matches_reference = (
            bool(unit_price)
            and bool(reference_unit_price)
            and _decimal(unit_price) == _decimal(reference_unit_price)
        )

        if (
            name_is_missing_or_only_uom
            and request_name
            and not _is_generic_product_label(request_name)
            and (
                price_matches_reference
                or (uom_is_pc and (unit_price or reference_unit_price))
            )
        ):
            item["product_name"] = request_name
            item["display_name"] = request_name

            item["quantity"] = (
                item.get("quantity")
                or request_qty
                or "1"
            )

            item["uom"] = (
                item.get("uom")
                or item.get("unit_of_measurement")
                or "PC"
            )

            item["unit_price"] = (
                item.get("unit_price")
                or reference_unit_price
            )

            item["line_total"] = (
                item.get("line_total")
                or item["unit_price"]
            )

            item["row_type"] = "PRODUCT"

            # IMPORTANT:
            # Keep supplier/date already extracted for this priced row.
            # Example: Aries Group must not be replaced by SARIKA.
            matched_reference_row = True

    if request_name and reference_unit_price and not matched_reference_row:
        reference_price = _decimal(reference_unit_price)

        unnamed_matches = []

        for item in items:
            row_type = str(
                item.get("row_type") or ""
            ).strip().upper()

            if row_type not in {"PRODUCT", "SERVICE"}:
                continue

            existing_name = _clean_text(
                item.get("product_name")
                or item.get("display_name")
            )

            if existing_name:
                continue

            item_price = (
                _decimal(item.get("unit_price"))
                or _decimal(item.get("line_total"))
            )

            if (
                reference_price is not None
                and item_price is not None
                and item_price == reference_price
            ):
                unnamed_matches.append(item)

        if len(unnamed_matches) == 1:
            target = unnamed_matches[0]

            target["product_name"] = request_name
            target["display_name"] = request_name
            target["row_type"] = "PRODUCT"

            target["quantity"] = (
                target.get("quantity")
                or request_qty
                or "1"
            )

            target["uom"] = (
                target.get("uom")
                or target.get("unit_of_measurement")
                or "PC"
            )

            target["unit_price"] = (
                target.get("unit_price")
                or reference_unit_price
            )

            target["line_total"] = (
                target.get("line_total")
                or target["unit_price"]
            )

            target["currency"] = (
                target.get("currency")
                or "USD"
            )

            # DO NOT overwrite supplier/date already bound
            # to the actual priced row.
            matched_reference_row = True

        else:
            logger.warning(
                "PDF reference recovery unresolved "
                "request_name=%s price=%s unnamed_matches=%s",
                request_name,
                reference_unit_price,
                len(unnamed_matches),
            )

    reference_supplier = _recover_nearest_commercial_supplier(
        text,
        request_match.start(),
        result.get("supplier_name"),
    )

    if reference_supplier:
        for item in items:
            current_supplier = str(item.get("supplier_name") or "").strip()

            if _looks_like_internal_or_buyer_supplier(current_supplier):
                item["supplier_name"] = reference_supplier
    for item in items:
        product_name_key = re.sub(
            r"[^a-z0-9]+",
            " ",
            str(item.get("product_name") or "").lower(),
        ).strip()

        if product_name_key in {"pc", "pcs", "per pc", "unit", "each", "ea"} and request_name:
            item["product_name"] = request_name
            item["display_name"] = request_name
            item["row_type"] = "PRODUCT"
            item["quantity"] = item.get("quantity") or request_qty or "1"
            item["uom"] = item.get("uom") or "PC"
    return result


def _recover_pdf_visible_table_columns(file_bytes, result):
    """Restore populated PDF table columns omitted by model extraction."""
    try:
        import io
        import pdfplumber

        def geometric_cell_text(page, bbox, fallback):
            """Rebuild a cell from glyph positions when PDF text lost spaces."""
            if not bbox:
                return re.sub(r"\s+", " ", str(fallback or "")).strip()
            x0, top, x1, bottom = bbox
            try:
                extracted = page.crop(bbox).extract_text(x_tolerance=1, y_tolerance=3) or ""
                extracted = re.sub(r"\s+", " ", extracted).strip()
                if extracted:
                    return extracted
            except Exception:
                pass
            chars = [c for c in page.chars if c.get("x1", 0) > x0 and c.get("x0", 0) < x1
                     and c.get("bottom", 0) > top and c.get("top", 0) < bottom]
            if not chars:
                return re.sub(r"\s+", " ", str(fallback or "")).strip()
            chars.sort(key=lambda c: (round(c.get("top", 0), 1), c.get("x0", 0)))
            lines = []
            for char in chars:
                if not lines or abs(char.get("top", 0) - lines[-1][-1].get("top", 0)) > 2:
                    lines.append([char])
                else:
                    lines[-1].append(char)
            rendered = []
            for line in lines:
                line.sort(key=lambda c: c.get("x0", 0))
                text, previous = "", None
                for char in line:
                    gap = (char.get("x0", 0) - previous.get("x1", 0)) if previous else 0
                    if previous and gap > 1.0 and not char.get("text", "").isspace():
                        text += " "
                    text += char.get("text", "")
                    previous = char
                rendered.append(text.strip())
            return re.sub(r"\s+", " ", " ".join(rendered)).strip()

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            rows = []
            continuation_schema = None
            for page in pdf.pages:
                for table_obj in page.find_tables() or []:
                    table = table_obj.extract()
                    geometric_rows = []
                    for row_obj, raw_row in zip(table_obj.rows, table):
                        geometric_rows.append([
                            geometric_cell_text(page, bbox, raw_row[index] if index < len(raw_row) else "")
                            for index, bbox in enumerate(row_obj.cells)
                        ])
                    header_index = next((i for i, row in enumerate(table[:8]) if sum(
                        bool(re.search(r"(?:product|description|item|name|model|specification|material|type|location)", str(cell or ""), re.I))
                        for cell in row
                    ) >= 1 and any(
                        re.search(r"(?:unit\s*price|price|rate|amount|extended|total)", str(cell or ""), re.I)
                        for cell in row
                    )), None)
                    if header_index is None and continuation_schema:
                        # Continuation tables may omit the repeated header.
                        # Reuse only the column schema, never section text.
                        header_index = -1
                        headers = continuation_schema["headers"]
                        header_labels = continuation_schema["labels"]
                        product_col = continuation_schema["product_col"]
                        context_col = continuation_schema.get("context_col")
                        parent_category = continuation_schema.get("parent_category")
                    elif header_index is None:
                        continue
                    else:
                        # A heading is only a parent when it is structurally
                        # outside the commercial header and occupies a
                        # heading-like single-cell row immediately before it.
                        # Ordinary unpriced product/reference rows are not
                        # promoted to parents.
                        parent_category = None
                        for candidate in reversed(geometric_rows[:header_index]):
                            populated = [str(cell or "").strip() for cell in candidate if str(cell or "").strip()]
                            if len(populated) != 1:
                                continue
                            text = populated[0]
                            if re.search(r"\d+\s*[x×/]\s*\d+|\b(?:mm|cm|kw|v|hz|swl|model|part)\b", text, re.I):
                                continue
                            if re.search(
                                r"\b(?:itb|date|dated|offer|quotation|quote|currency|manufacturer|country\s+of\s+origin|email|phone|tel|contact|ref(?:erence)?|no\.?\b)\b",
                                text,
                                re.I,
                            ) or re.search(r"\b(?:ltd|limited|llc|corp(?:oration)?|company|co\.?)\b", text, re.I):
                                continue
                            if len(text) >= 3:
                                parent_category = re.sub(r"\s+", " ", text).strip()
                                break
                    # Some PDFs draw the header baseline into the first body
                    # row's large cell rectangle. Remove only the leading
                    # glyphs that are absent from that row's raw cell text;
                    # this prevents header fragments (for example the second
                    # half of a slash header) leaking into values without
                    # guessing words or inserting product-specific text.
                    for row_index in range(header_index + 1, min(len(geometric_rows), len(table))):
                        for col_index, rendered in enumerate(geometric_rows[row_index]):
                            fallback = str(table[row_index][col_index] or "").strip() if col_index < len(table[row_index]) else ""
                            compact_rendered = re.sub(r"[^a-z0-9]+", "", rendered.casefold())
                            compact_fallback = re.sub(r"[^a-z0-9]+", "", fallback.casefold())
                            if compact_fallback and compact_rendered.endswith(compact_fallback) and compact_rendered != compact_fallback:
                                prefix_len = len(rendered)
                                remaining = len(compact_fallback)
                                compact_seen = 0
                                for position, character in enumerate(reversed(rendered)):
                                    if character.isalnum():
                                        compact_seen += 1
                                    if compact_seen >= remaining:
                                        prefix_len = len(rendered) - position - 1
                                        break
                                geometric_rows[row_index][col_index] = rendered[prefix_len:].strip()
                    if header_index >= 0:
                        headers = [re.sub(r"\s+", " ", str(v or "")).strip().lower() for v in geometric_rows[header_index]]
                        # Prefer the explicit identity column.  Specification,
                        # Type and Location are context/attribute columns and
                        # must never displace Name/Description.
                        name_col = next((i for i, h in enumerate(headers) if re.fullmatch(r"(?:name|product\s*name)", h)), None)
                        description_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:description|item\s*description)\b", h) and not re.search(r"specification|type|location|model", h)), None)
                        product_col = name_col if name_col is not None else description_col
                        if product_col is None:
                            product_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:description|name|product)\b", h) and not re.search(r"specification|type|location|model", h)), None)
                    if product_col is None:
                        continue
                    context_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:type\s*/\s*location|type|location|category|section|group)\b", h) and i != product_col), None)
                    quantity_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:qty|quantity)\b", h)), None)
                    uom_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:unit|uom)\b", h)), None)
                    total_col = next((i for i, h in enumerate(headers) if re.search(r"(?:extended|line|total|amount)", h) and not re.search(r"unit\s*price|rate", h)), None)
                    serial_col = next((i for i, h in enumerate(headers) if re.search(r"\b(?:no|sl\s*no|serial|row)\b", h)), None)
                    if header_index >= 0:
                        header_labels = list(geometric_rows[header_index])
                        continuation_schema = {"headers": headers, "labels": header_labels, "product_col": product_col, "context_col": context_col}
                        continuation_schema["parent_category"] = parent_category
                    for raw in geometric_rows[header_index + 1:]:
                        product = str(raw[product_col] or "").strip() if product_col < len(raw) else ""
                        if not product:
                            continue
                        values = {headers[i]: re.sub(r"\s+", " ", str(raw[i] or "")).replace("- ", "-").strip()
                                  for i in range(min(len(headers), len(raw))) if str(raw[i] or "").strip()}
                        price_text = next((values[h] for h in headers if re.search(r"(?:unit\s*price|price|rate|amount|extended|total)", h) and values.get(h)), "")
                        if not any(re.search(r"\d", price_text) for _ in [0]):
                            continue
                        row_parent = parent_category
                        if context_col is not None and context_col < len(raw):
                            context_value = re.sub(r"\s+", " ", str(raw[context_col] or "")).strip()
                            if context_value and context_value.casefold() != product.casefold():
                                row_parent = context_value
                        attrs = []
                        def is_core_header(label):
                            key = re.sub(r"[^a-z0-9]+", "", label.casefold())
                            return key in {"qty", "quantity", "unit", "uom", "unitprice", "rate", "price", "amount", "extendedprice", "total", "currency", "srno", "no", "serialno"} or key.startswith(("unitprice", "extendedprice"))
                        for column_index, label in enumerate(headers):
                            value = values.get(label)
                            if not value:
                                continue
                            if label == headers[product_col] or column_index == context_col or is_core_header(label) or label == "":
                                continue
                            header_value = header_labels[headers.index(label)] if header_index < 0 else geometric_rows[header_index][headers.index(label)]
                            attrs.append({"label": str(header_value or label).strip(), "value": value})
                        rows.append({
                            "product": re.sub(r"\s+", " ", product).strip(),
                            "attrs": attrs,
                            "price": price_text,
                            "parent_category": row_parent,
                            "quantity": str(raw[quantity_col] or "").strip() if quantity_col is not None and quantity_col < len(raw) else "",
                            "uom": str(raw[uom_col] or "").strip() if uom_col is not None and uom_col < len(raw) else "",
                            "line_total": str(raw[total_col] or "").strip() if total_col is not None and total_col < len(raw) else "",
                            "serial": str(raw[serial_col] or "").strip() if serial_col is not None and serial_col < len(raw) else "",
                            "page_number": pdf.pages.index(page) + 1 if page in pdf.pages else None,
                        })
    except Exception:
        return result

    if not rows:
        return result
    product_items = [item for item in result.get("items") or [] if str(item.get("row_type") or "").upper() == "PRODUCT"]
    used = set()
    for item in product_items:
        item_name = re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip().lower()
        item_name_key = re.sub(r"[^a-z0-9]+", "", item_name)
        item_price = re.sub(r"[^0-9.]", "", str(item.get("unit_price") or item.get("line_total") or ""))
        candidates = []
        for i, row in enumerate(rows):
            if i in used:
                continue
            row_price = re.sub(r"[^0-9.]", "", row.get("price") or "")
            if row_price and item_price and row_price != item_price:
                continue
            row_name = row["product"].lower()
            name_match = (
                item_name == row_name
                or item_name in row_name
                or row_name in item_name
                or item_name_key == re.sub(r"[^a-z0-9]+", "", row_name)
                or re.sub(r"[^a-z0-9]+", "", row_name) in item_name_key
            )
            evidence = 0
            if item.get("page_number") not in (None, "") and item.get("page_number") == row.get("page_number"):
                evidence += 1
            if row_price and item_price and row_price == item_price:
                evidence += 2
            if row.get("quantity") and str(item.get("quantity") or "").strip() == row["quantity"]:
                evidence += 1
            if row.get("uom") and str(item.get("uom") or "").strip().casefold() == row["uom"].casefold():
                evidence += 1
            row_total = re.sub(r"[^0-9.]", "", row.get("line_total") or "")
            item_total = re.sub(r"[^0-9.]", "", str(item.get("line_total") or ""))
            if row_total and item_total and row_total == item_total:
                evidence += 1
            # Name/specification is supporting evidence only. It is not a
            # gate because Gemini may have placed Specification in product_name.
            if name_match:
                evidence += 1
            if evidence < 3:
                continue
            item_attrs = " ".join(str(a.get("value") or "") for a in item.get("extra_attributes") or [])
            row_attrs = " ".join(str(a.get("value") or "") for a in row.get("attrs") or [])
            attr_overlap = int(bool(item_attrs and row_attrs and re.sub(r"\s+", " ", item_attrs).casefold() in re.sub(r"\s+", " ", row_attrs).casefold()))
            page_match = int(item.get("page_number") not in (None, "") and item.get("page_number") == row.get("page_number"))
            candidates.append((evidence, attr_overlap, page_match, i))
        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0]
            # A tied best match is not safe to repair from source.
            match_index = best[-1] if len(candidates) == 1 or candidates[1][:-1] != best[:-1] else None
        else:
            match_index = None
        if match_index is None:
            continue
        used.add(match_index)
        row = rows[match_index]
        item["_table_source_verified"] = True
        # The physical table hierarchy is authoritative.  A continuation may
        # inherit only the schema-bound section captured above.
        item["parent_category"] = row.get("parent_category")
        attrs = list(item.get("extra_attributes") or [])
        # A separate source column is authoritative: keep the chosen product
        # cell as product_name and carry every other populated non-core cell
        # under its original header.
        if row.get("attrs"):
            for attr in row["attrs"]:
                attr["label"] = re.sub(r"\s*/\s*", " / ", re.sub(r"\s+", " ", str(attr.get("label") or "")).strip())
                label_key = re.sub(r"[^a-z0-9]+", " ", attr["label"].casefold()).strip()
                existing = next((a for a in attrs if isinstance(a, dict) and re.sub(r"[^a-z0-9]+", " ", str(a.get("label") or "").casefold()).strip() == label_key), None)
                if existing is None:
                    attrs.append(attr)
                elif len(str(attr.get("value") or "")) < len(str(existing.get("value") or "")):
                    existing["value"] = attr.get("value")
            description_like = next((a["value"] for a in row["attrs"] if str(a.get("label") or "").casefold() in {"description", "item description"}), None)
            item["product_name"] = row["product"]
        seen = set()
        item["extra_attributes"] = [a for a in attrs if isinstance(a, dict) and (a.get("label"), a.get("value")) not in seen and not seen.add((a.get("label"), a.get("value")))]
    return _apply_item_display_names(result)


def finalize_structured_pdf_table(result, file_bytes):
    """Make confidently matched PDF table rows source-authoritative once."""
    result = _recover_pdf_supplier_matrix_rows(file_bytes, result)
    result = _recover_pdf_visible_table_columns(file_bytes, result)
    for item in result.get("items") or []:
        if item.get("_table_source_verified"):
            item["row_type"] = _canonical_row_type(item.get("row_type"), item)
    return result


def _recover_pdf_supplier_matrix_rows(file_bytes, result):
    """Use physical cells; model rows never define supplier column order."""
    from api.services.pdf_commercial_geometry import finalize_physical_pdf
    return finalize_physical_pdf(result, file_bytes)


def _finalize_pdf_extraction_result(result, file_bytes):
    """Run the single authoritative source-backed PDF finalization flow."""
    physical = _recover_pdf_supplier_matrix_rows(file_bytes, result)
    if physical.get('_physical_pdf_complete'):
        from api.services.visual_review_policy import require_visual_review
        return require_visual_review(physical, file_bytes)
    result = normalize_result(result)
    result = restore_table_columns(result, file_bytes)
    result = _recover_pdf_visible_table_columns(file_bytes, result)
    result = _recover_pdf_full_description_cells(file_bytes, result)
    result = finalize_pdf(result, file_bytes)
    result = normalize_result(result)
    result = finalize_structured_pdf_table(result, file_bytes)
    result = normalize_result(result)
    # This is intentionally the final comparison-table stage.  No later
    # identity or supplier recovery is allowed to overwrite physical cells.
    result = _recover_pdf_supplier_matrix_rows(file_bytes, result)
    if not result.get('_authoritative_supplier_matrix'):
        from api.services.pdf_email_quotes import recover_pdf_email_quotes
        result = recover_pdf_email_quotes(result, file_bytes)
        for item in result.get('items') or []:
            item['display_name'] = build_canonical_display_name(item)
    from api.services.visual_review_policy import require_visual_review
    return require_visual_review(result, file_bytes)


def _recover_pdf_full_description_cells(file_bytes, result):
    """Restore the complete source description when it is one table cell.

    A description cell may contain technical prose (including a
    ``Specifications:`` paragraph).  That text is part of the source
    product identity unless the PDF exposes a separate specification/model
    column.  This recovery deliberately only uses the table's description
    column and never parses labels inside that cell into other fields.
    """
    try:
        import io
        import pdfplumber

        source_rows = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                for physical_table in page.find_tables() or []:
                    table = physical_table.extract()
                    header_index = None
                    description_col = None
                    has_separate_identity_column = False
                    for index, raw_header in enumerate(table[:6]):
                        headers = [re.sub(r"\s+", " ", str(cell or "")).strip().lower() for cell in raw_header]
                        desc = next((i for i, header in enumerate(headers)
                                     if re.search(r"\b(?:description|product(?:\s+description)?|item\s*(?:&|and)\s*description|item description)\b", header)), None)
                        has_price = any(re.search(r"\b(?:unit\s*price|price|rate|amount|total)\b", header) for header in headers)
                        has_quantity = any(re.search(r"\b(?:qty|quantity|unit)\b", header) for header in headers)
                        if desc is not None and (has_price or has_quantity):
                            header_index = index
                            description_col = desc
                            has_separate_identity_column = any(
                                re.search(r"\b(?:model|specification|material|brand|part\s*(?:no|number))\b", header)
                                for header in headers
                                if header and header != headers[description_col]
                            )
                            break
                    if header_index is None or description_col is None or has_separate_identity_column:
                        continue

                    headers = [re.sub(r"\s+", " ", str(cell or "")).strip().lower() for cell in table[header_index]]
                    value_columns = [
                        i for i, header in enumerate(headers)
                        if re.search(r"\b(?:unit\s*price|price|rate|amount|total)\b", header)
                    ]
                    for raw_index, raw in enumerate(table[header_index + 1:], start=header_index + 1):
                        if description_col >= len(raw):
                            continue
                        description = re.sub(r"\s+", " ", str(raw[description_col] or "")).strip()
                        if not description:
                            continue
                        # Only commercial rows are candidates.  This avoids
                        # copying section headings or terms into products.
                        values = [str(raw[i] or "").strip() for i in value_columns if i < len(raw)]
                        if value_columns and not any(re.search(r"\d", value) for value in values):
                            continue
                        # With no horizontal rules, a PDF table cell can span
                        # many commercial rows. Never append that whole column
                        # to the first item. Split on physical price baselines.
                        repeated_columns = [i for i in value_columns if i < len(raw)
                                            and len(str(raw[i] or '').splitlines()) > 1]
                        if repeated_columns:
                            cells = physical_table.rows[raw_index].cells
                            desc_box = cells[description_col]
                            price_box = cells[repeated_columns[0]]
                            if not desc_box or not price_box:
                                continue
                            price_words = [w for w in page.crop(price_box).extract_words()
                                           if re.fullmatch(r'[-+]?\d[\d,.]*', w['text'])]
                            tops = sorted({round(w['top'], 1) for w in price_words})
                            if len(tops) < 2:
                                # Ambiguous multiline prices are not evidence
                                # that the whole description belongs to one row.
                                continue
                            desc_words = page.crop(desc_box).extract_words()
                            for band_index, top in enumerate(tops):
                                lower = desc_box[1] if band_index == 0 else top - 2
                                upper = tops[band_index + 1] - 2 if band_index + 1 < len(tops) else desc_box[3]
                                words = [w for w in desc_words if lower <= w['top'] < upper]
                                text = ' '.join(w['text'] for w in words).strip()
                                if text:
                                    source_rows.append({"page_number": page_number, "description": text})
                            continue
                        source_rows.append({"page_number": page_number, "description": description})
    except Exception:
        logger.debug("PDF full-description recovery unavailable", exc_info=True)
        return result

    if not source_rows:
        return result

    product_items = [item for item in result.get("items") or []
                     if str(item.get("row_type") or "").upper() == "PRODUCT"
                     and not item.get("_table_source_verified")]
    used = set()
    for item in product_items:
        current = re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip()
        current_key = current.casefold()
        match_index = next((i for i, row in enumerate(source_rows)
                            if i not in used and (not item.get("page_number") or item.get("page_number") == row["page_number"]) and (
                                current_key == row["description"].casefold()
                                or current_key and row["description"].casefold().startswith(current_key)
                            )), None)
        if match_index is None:
            continue
        used.add(match_index)
        item["product_name"] = source_rows[match_index]["description"]
        item["page_number"] = item.get("page_number") or source_rows[match_index]["page_number"]
        # No separate identity column was found in this table.  Any
        # technical fields produced by the model therefore came from the
        # same DESCRIPTION cell and must not make the canonical name shorter.
        for field in ("model", "part_number", "brand", "specification", "remark", "note"):
            item[field] = None
        item["extra_attributes"] = [
            attr for attr in (item.get("extra_attributes") or [])
            if isinstance(attr, dict) and str(attr.get("label") or "").strip().casefold()
            not in {"model", "specification", "material", "part number", "part no", "brand", "remark", "note"}
        ]
    return _apply_item_display_names(result)
def _extract_company_from_pdf_email_section(section_text):
    from api.services.msg_commercial_history import signature_company
    signature = signature_company(section_text)
    if signature:
        return signature
    company_pattern = re.compile(
        r"\b("
        r"[A-Z][A-Za-z0-9 &'.,/-]{2,120}?"
        r"(?:"
        r"CO\.?\s*,?\s*LTD\.?|"
        r"CO\.?\s*,?\s*LIMITED|"
        r"L\.L\.C\.?|LLC|"
        r"LTD\.?|LIMITED|"
        r"INC\.?|CORPORATION|CORP\.?|"
        r"FZE|FZC|FZCO|DMCC"
        r")"
        r")\b",
        re.I,
    )

    lines = [
        _clean_email_line(line)
        for line in str(section_text or "").splitlines()
    ]
    lines = [line for line in lines if line]

    for line in reversed(lines):
        if re.match(r"^(from|sent|to|cc|subject)\s*:", line, re.I):
            continue

        match = company_pattern.search(line)

        if not match:
            continue

        company = match.group(1).strip(" ,.-:;|")

        if 3 <= len(company) <= 150 and _is_reliable_msg_supplier_candidate(company):
            return company

    return None
def _recover_email_supplier_from_pdf(file_bytes, current_supplier):
    if not file_bytes:
        return current_supplier

    current = str(current_supplier or "").strip()

    def norm(value):
        return re.sub(
            r"[^a-z0-9]+",
            "",
            str(value or "").lower(),
        )

    def is_domain_like_supplier(value):
        text = str(value or "").strip()

        if not text:
            return True

        if "@" in text or "." in text:
            return True

        if re.fullmatch(r"[A-Z0-9_-]+", text, re.I) and not re.search(r"\s", text):
            return True

        return False

    def is_usable_company(value):
        text = str(value or "").strip()

        if not text or text.upper() == "UNKNOWN":
            return False

        if is_domain_like_supplier(text):
            return False

        return bool(
            re.search(
                r"\b(LLC|L\.L\.C|LTD|LIMITED|CO\.?|COMPANY|INC|FZE|FZC|FZCO|DMCC|MARINE|SHIPPING|TRADING|SERVICES)\b",
                text,
                re.I,
            )
        )

    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))

        text = "\n".join(
            page.extract_text() or ""
            for page in reader.pages[:3]
        )

        if not text.strip():
            return current_supplier

        from_match = re.search(
            r"(?im)^\s*From\s*:\s*(.+)$",
            text,
        )

        to_match = re.search(
            r"(?im)^\s*To\s*:\s*(.+)$",
            text,
        )

        to_value = (
            to_match.group(1).strip()
            if to_match
            else ""
        )

        supplier_is_recipient = bool(
            current
            and to_value
            and (
                norm(current) == norm(to_value)
                or norm(current) in norm(to_value)
                or norm(to_value) in norm(current)
            )
        )

        if is_usable_company(current) and not supplier_is_recipient:
            return current_supplier

        if not from_match:
            return current_supplier

        from_value = from_match.group(1).strip()
        current_email_text = text[from_match.start():]

        next_from_match = re.search(
            r"(?im)^\s*From\s*:",
            current_email_text[5:],
        )

        if next_from_match:
            current_email_text = current_email_text[: next_from_match.start() + 5]

        signature_supplier = _extract_company_from_pdf_email_section(
            current_email_text
        )

        if (
            signature_supplier
            and not _looks_like_internal_or_buyer_supplier(signature_supplier)
        ):
            return signature_supplier

        email_match = re.search(
            r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            from_value,
        )

        if not email_match:
            return current_supplier

        domain = email_match.group(1).lower()
        parts = domain.split(".")

        if len(parts) < 2:
            return current_supplier

        generic_domains = {
            "gmail",
            "yahoo",
            "hotmail",
            "outlook",
            "icloud",
            "protonmail",
        }

        if (
            len(parts) >= 3
            and parts[-2] in {"com", "co", "net", "org"}
        ):
            company = parts[-3]
        else:
            company = parts[-2]

        if company in generic_domains:
            return current_supplier

        domain_supplier = (
            company
            .replace("-", " ")
            .replace("_", " ")
            .strip()
            .upper()
        )

        if current and not is_domain_like_supplier(current):
            return current_supplier

        return domain_supplier or current_supplier

    except Exception:
        return current_supplier
def _clean_email_line(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip()


def _extract_company_from_email_body(body):
    """
    Find a likely supplier company from an email signature.

    Generic company suffix detection only.
    No company-specific hardcoding.
    """
    if not body:
        return None

    from api.services.msg_commercial_history import signature_company
    signature = signature_company(body)
    if signature:
        return signature

    company_pattern = re.compile(
        r"\b("
        r".{2,120}?"
        r"(?:"
        r"CO\.?\s*,?\s*LTD\.?|"
        r"CO\.?\s*,?\s*LIMITED|"
        r"L\.L\.C\.?|LLC|"
        r"LTD\.?|LIMITED|"
        r"INC\.?|CORPORATION|CORP\.?|"
        r"FZE|FZC|FZCO|DMCC|AGENCY"
        r")"
        r")\b",
        re.I,
    )

    raw_lines = str(body).splitlines()

    lines = [_clean_email_line(raw_line) for raw_line in raw_lines]
    lines = [line for line in lines if line]

    # Commercial email footers sometimes identify the trading company in a
    # standard-terms sentence while the signature contains only a website.
    # Use the sentence plus matching website as corroboration, not a domain
    # alone and not a legal disclaimer.
    transaction_match = re.search(
        r"\btransactions?\s+with\s+([A-Za-z][A-Za-z &.-]{2,90}?)\s+are\s+governed\b",
        " ".join(lines),
        re.I,
    )
    website_match = re.search(r"\bwww\.([a-z0-9-]+)\.[a-z]{2,}(?:\.[a-z]{2,})?\b", " ".join(lines), re.I)
    if transaction_match and website_match:
        base = _clean_text(transaction_match.group(1)).strip(" .,-")
        base_key = re.sub(r"[^a-z0-9]+", "", base.casefold())
        site_key = re.sub(r"[^a-z0-9]+", "", website_match.group(1).casefold())
        if base_key and site_key.startswith(base_key):
            suffix = site_key[len(base_key):]
            if suffix and len(suffix) >= 3:
                return f"{base} {suffix.title()}"
            return base

    for line in reversed(lines[-120:]):
        lower = line.casefold()
        if re.search(
            r"\b(?:disclaimer|liabilit(?:y|ies)|insurance|confidential|"
            r"not\s+responsible|intended\s+recipient|terms\s+and\s+conditions|"
            r"consignee|ship\s+to|delivery\s+address|buyer|vessel|c/o)\b",
            lower,
        ):
            continue
        if "@" in line or re.search(r"\b(?:https?://|www\.)", lower):
            continue
        match = company_pattern.search(line)

        if not match:
            continue

        company = match.group(1).strip(
            " ,.-:;|"
        )

        # Avoid huge sentences.
        if (
            3 <= len(company) <= 150
            and _is_reliable_msg_supplier_candidate(company)
        ):
            return company

    return None
def _supplier_from_sender(sender):
    """
    Conservative supplier fallback from From: address.

    Used only when no better company name is available.
    """

    text = str(sender or "").strip()

    if not text:
        return None

    display_name = re.sub(r"<[^>]+>", "", text).strip(" \"'")
    if (
        display_name
        and re.search(r"\b(?:agency|llc|l\.l\.c\.?|ltd|limited|fze|fzc|dmcc|inc\.?|corporation)\b", display_name, re.I)
        and _is_reliable_msg_supplier_candidate(display_name)
    ):
        return display_name

    match = re.search(
        r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        text,
    )

    if not match:
        return None

    domain = match.group(1).lower()

    parts = domain.split(".")

    if len(parts) < 2:
        return None

    generic = {
        "gmail",
        "yahoo",
        "hotmail",
        "outlook",
        "icloud",
        "protonmail",
    }

    if (
        len(parts) >= 3
        and parts[-2] in {
            "com",
            "co",
            "net",
            "org",
        }
    ):
        company = parts[-3]
    else:
        company = parts[-2]

    if company in generic:
        return None

    # A domain alone is corroborating evidence, not a company name.
    return None


def _is_reliable_msg_supplier_candidate(value):
    """Reject prose/legal text before accepting an MSG supplier candidate."""
    text = _clean_text(value)
    from api.services.msg_commercial_history import plausible_company
    if not plausible_company(text):
        return False
    if not text or len(text) > 150:
        return False
    lower = text.casefold()
    if re.search(
        r"\b(?:disclaimer|liabilit(?:y|ies)|liable|responsible\s+for|"
        r"confidential|intended\s+recipient|terms\s+and\s+conditions|"
        r"do\s+not\s+accept|not\s+responsible)\b",
        lower,
    ):
        return False
    if re.match(r"^(?:dear|from|to|cc|bcc|subject|regards|best regards)\b", lower):
        return False
    if len(re.findall(r"[A-Za-z]{2,}", text)) < 2:
        return False
    return True


def _resolve_msg_supplier(result, msg_meta):
    """Choose a company-backed MSG supplier and propagate it to all rows."""
    result = result or {}
    meta = msg_meta or {}
    signature = _normalize_supplier_name(meta.get("signature_company"))
    sender = _normalize_supplier_name(meta.get("sender_company"))
    current = _normalize_supplier_name(result.get("supplier_name"))

    # An older signature is usable only when it is corroborated by the
    # current sender identity or the current extracted supplier. This avoids
    # inheriting an unrelated vendor from quoted email history.
    candidates = []
    if signature and _is_reliable_msg_supplier_candidate(signature):
        signature_key = _supplier_identity_key(signature)
        corroborators = {_supplier_identity_key(value) for value in (sender, current) if value}
        if meta.get("signature_company_verified") or (corroborators and signature_key in corroborators):
            candidates.append(signature)
    candidates.extend(value for value in (sender, current) if value)
    supplier = next(
        (candidate for candidate in candidates
         if _is_reliable_msg_supplier_candidate(candidate)),
        None,
    )
    result["supplier_name"] = supplier
    for item in result.get("items") or []:
        item["supplier_name"] = supplier
    return result


def _commercial_msg_supplier(body, sender=None, sent_date=None):
    """Resolve supplier only from a priced MSG block, never whole-thread text."""
    blocks = split_msg_thread(body, sender, sent_date)
    fallback = []
    for block in reversed(blocks):
        if is_msg_enquiry(block):
            continue
        commercial = is_msg_commercial(block)
        if not commercial and not re.search(
            r"\b(?:quotation|quote|offer|rate|price|shipment|freight|invoice)\b",
            block.get("body") or "",
            re.I,
        ):
            continue
        signature = _extract_company_from_email_body(block.get("body"))
        block_sender = _supplier_from_sender(block.get("sender"))
        for candidate in (signature, block_sender):
            if _is_reliable_msg_supplier_candidate(candidate):
                return _normalize_supplier_name(candidate)
        fallback.append(block)
    # Attachment-backed quotations may have no price text in the email body.
    # Use only a non-enquiry sender/signature block as a fallback; do not scan
    # arbitrary historical text or legal disclaimers.
    for block in fallback:
        if is_msg_enquiry(block):
            continue
        for candidate in (
            _extract_company_from_email_body(block.get("body")),
            _supplier_from_sender(block.get("sender")),
        ):
            if _is_reliable_msg_supplier_candidate(candidate):
                return _normalize_supplier_name(candidate)
    sender_candidate = _supplier_from_sender(sender)
    if _is_reliable_msg_supplier_candidate(sender_candidate):
        return _normalize_supplier_name(sender_candidate)
    return None
def _normalize_supplier_name(value):
    raw_supplier = str(value or "")
    from api.services.normalization import is_contact_only_supplier
    if is_contact_only_supplier(raw_supplier):
        return None
    legal_suffix = re.compile(
        r"\b(?:CO\.?\s*,?\s*LTD\.?|LTD\.?|LIMITED|LLC|LLP|FZE|FZC|INC\.?|CORP\.?|CORPORATION|GMBH|PVT\.?\s*LTD\.?)\b",
        re.I,
    )
    # Legal names may contain City, Building or Manufacturer. These words
    # are not address delimiters inside an explicitly named legal entity.
    legal_end = legal_suffix.search(raw_supplier)
    if legal_end:
        end = legal_end.end()
        if raw_supplier[end:end + 1] == ".":
            end += 1
        raw_supplier = raw_supplier[:end]
    raw_lines = [
        re.sub(r"\s+", " ", line).strip(" ,;|")
        for line in raw_supplier.replace("\r", "").split("\n")
    ]
    raw_lines = [line for line in raw_lines if line]
    if raw_lines:
        collected = []
        for line in raw_lines:
            if re.search(
                r"(?:phone|tel|mobile|fax|email|www\.|https?://|manufacturer|contact\s*(?:person|name)?|\bL[123]\b)",
                line,
                re.I,
            ):
                break
            collected.append(line)
            if legal_suffix.search(line):
                break
        if collected:
            raw_supplier = " ".join(collected)
    raw_supplier = raw_supplier if legal_end else re.sub(
        r"\s*,?\s+(?:\d+(?:st|nd|rd|th)\b|no\.?|road|rd\.?|street|st\.?|floor|building|plaza|district|city|province|p\.?o\.?\s*box|post\s+box|phone|phone\s*no|tel|mobile|fax|email|www\.|https?://|manufacturer|contact\s*(?:person|name)?|\bL[123]\b).*?$",
        "",
        raw_supplier,
        flags=re.I,
    ).strip(" ,;|")
    # When Gemini returns a bilingual header, keep the explicitly printed
    # English legal/company name. This is normalization only; it does not
    # translate a Chinese-only name or invent a company from a domain.
    if re.search(r"[\u3400-\u9fff]", raw_supplier):
        english_candidates = re.findall(
            r"[A-Za-z][A-Za-z0-9&.,'() /-]{2,}", raw_supplier
        )
        legal_candidates = [
            candidate.strip(" -|()")
            for candidate in english_candidates
            if re.search(
                r"\b(?:CO\.?|LTD\.?|LIMITED|LLC|INC\.?|CORP\.?|CORPORATION|AGENCY|PTE\.?|PLC)\b",
                candidate,
                re.I,
            )
        ]
        if legal_candidates:
            raw_supplier = max(legal_candidates, key=len)
    supplier = _clean_text(raw_supplier)

    if not supplier:
        return None

    if supplier.upper() == "UNKNOWN":
        return None

    affiliation = re.search(r"(?:^|\\n)\\s*from\\s+([^\\n|]+)", str(value or ""), re.I)
    if affiliation:
        affiliated_company = _clean_text(affiliation.group(1))
        if affiliated_company and affiliated_company.upper() != "UNKNOWN":
            supplier = affiliated_company

    supplier = re.sub(
        r"^(?:M/S\.?|MESSRS\.?)\s+",
        "",
        supplier,
        flags=re.I,
    ).strip()

    email_match = re.search(
        r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        supplier,
    )

    if email_match:
        supplier = email_match.group(1)

    domain_match = re.fullmatch(
        r"(?:https?://)?(?:www\.)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?:/.*)?",
        supplier.strip(),
        flags=re.I,
    )

    if domain_match:
        domain = domain_match.group(1).lower()
        parts = domain.split(".")

        if (
            len(parts) >= 3
            and parts[-2] in {"com", "co", "net", "org"}
        ):
            company = parts[-3]
        else:
            company = parts[-2]

        if company in {
            "gmail",
            "yahoo",
            "hotmail",
            "outlook",
            "icloud",
            "protonmail",
        }:
            return None

        return re.sub(r"[-_]+", " ", company).strip().title() or None

    return supplier


def _canonical_uom(value):
    value = _clean_text(value)
    if not value:
        return None
    aliases = {"PCE": "PC", "PCS": "PC", "PIECE": "PC", "EACH": "EA"}
    return aliases.get(value.upper(), value)


def _supplier_identity_key(value):
    supplier = _normalize_supplier_name(value)

    if not supplier:
        return ""

    text = supplier.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(
        r"\b(llc|l l c|ltd|limited|fze|fzco|fzc|trading|co|company|and)\b",
        "",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _canonicalize_item_suppliers(result):
    """Propagate the strongest clearly matching supplier name document-wide."""
    result = result or {}
    items = list(result.get("items") or [])
    candidates = []
    document_supplier = _normalize_supplier_name(result.get("supplier_name"))
    if document_supplier:
        candidates.append(document_supplier)
    candidates.extend(
        supplier for supplier in (
            _normalize_supplier_name(item.get("supplier_name"))
            for item in items
        ) if supplier
    )

    def meaningful_tokens(value):
        ignored = {"llc", "ltd", "limited", "fze", "fzco", "fzc", "co", "company", "and"}
        return {
            token for token in _supplier_identity_key(value).split()
            if token not in ignored
        }

    qualifier_tokens = {
        "china", "prc", "uae", "usa", "uk", "india", "germany",
        "international", "trading", "technology", "technical", "services",
        "automation", "industrial", "marine", "electronics", "group",
    }

    def compatible(left, right):
        left_tokens = meaningful_tokens(left)
        right_tokens = meaningful_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        if left_tokens == right_tokens:
            return True
        # A short brand key may be expanded by geographic/legal qualifiers,
        # but arbitrary substring matches are intentionally not accepted.
        shorter, longer = sorted((left_tokens, right_tokens), key=len)
        return shorter < longer and shorter.issubset(longer) and all(
            token in qualifier_tokens for token in (longer - shorter)
        )

    unique = []
    for candidate in candidates:
        if not any(candidate.lower() == existing.lower() for existing in unique):
            unique.append(candidate)

    for item in items:
        current = _normalize_supplier_name(item.get("supplier_name"))
        if not current:
            if document_supplier:
                item["supplier_name"] = document_supplier
            continue
        matches = [candidate for candidate in unique if compatible(current, candidate)]
        if document_supplier and compatible(current, document_supplier):
            matches.append(document_supplier)
        if matches:
            item["supplier_name"] = max(matches, key=lambda value: len(str(value)))

    normalized_document = _normalize_supplier_name(result.get("supplier_name"))
    document_matches = [
        candidate for candidate in unique
        if normalized_document and compatible(normalized_document, candidate)
    ]
    if document_matches:
        result["supplier_name"] = max(document_matches, key=lambda value: len(str(value)))
    elif not normalized_document and len({
        _supplier_identity_key(item.get("supplier_name"))
        for item in items if item.get("supplier_name")
    }) == 1:
        result["supplier_name"] = next(
            (item.get("supplier_name") for item in items if item.get("supplier_name")),
            None,
        )
    result["items"] = items
    return result
def _extract_msg_request_items(email_body):
    body = str(email_body or "")
    lines = [_clean_email_line(line) for line in body.splitlines()]
    lines = [line for line in lines if line]

    headers = []
    values = []
    in_table = False

    for line in lines:
        lower = line.lower().strip()

        if re.fullmatch(r"sl\s*no", lower, re.I):
            in_table = True
            headers = ["sl_no"]
            values = []
            continue

        if not in_table:
            continue

        if re.match(r"^(from|regards|best regards|disclaimer)\b", lower, re.I):
            break

        if lower in {"item", "description", "model number", "model", "qty", "quantity", "unit", "uom"}:
            header = {
                "description": "item",
                "quantity": "qty",
                "uom": "unit",
            }.get(lower, lower.replace(" ", "_"))
            headers.append(header)
            continue

        values.append(line)

    request_items = []

    if not headers or not values:
        return request_items

    column_count = len(headers)

    for index in range(0, len(values), column_count):
        chunk = values[index:index + column_count]

        if len(chunk) < column_count:
            continue

        row = dict(zip(headers, chunk))

        item_name = _clean_text(row.get("item"))
        model = _clean_text(row.get("model_number") or row.get("model"))
        qty = _clean_text(row.get("qty"))
        unit = _clean_text(row.get("unit"))

        if not item_name or not model:
            continue

        request_items.append({
            "product_name": item_name,
            "model": model,
            "quantity": qty,
            "unit_of_measurement": unit,
        })

    return request_items


def _extract_msg_request_descriptions(email_body):
    return [
        item["product_name"]
        for item in _extract_msg_request_items(email_body)
        if item.get("product_name")
    ]


def _split_msg_commercial_blocks(email_body):
    """Compatibility wrapper retaining complete sender/date/body boundaries."""
    return [block["text"] for block in split_msg_thread(email_body)
            if is_msg_commercial(block)]


def _msg_block_date(block, fallback=None):
    blocks = split_msg_thread(block)
    return (blocks[0]["date"] if blocks else None) or fallback


def _extract_msg_inline_price_items(email_body, supplier_name=None, document_date=None):
    body = str(email_body or "")
    lines = [_clean_email_line(line) for line in body.splitlines()]
    lines = [line for line in lines if line]

    request_items = _extract_msg_request_items(body)

    if not request_items:
        return []

    price_lines = []

    for line in lines:
        if re.match(r"^(from|to|cc|date|subject|dear)\b", line, re.I):
            break

        match = re.search(
            r"(?P<model>[A-Z0-9][A-Z0-9._/-]{2,}(?:\s+Class\s+\d+)?)\s+"
            r"(?:USD\s*)?\$?\s*(?P<price>\d+(?:\.\d+)?)\b",
            line,
            re.I,
        )

        if not match:
            continue

        price_lines.append({
            "model": _clean_text(match.group("model")),
            "unit_price": match.group("price"),
            "line": line,
        })

    if not price_lines:
        return []

    used_indexes = set()
    items = []

    for price_line in price_lines:
        price_model = str(price_line.get("model") or "").lower()

        matched_index = None
        matched_request = None

        for index, request_item in enumerate(request_items):
            if index in used_indexes:
                continue

            request_model = str(request_item.get("model") or "").lower()

            if price_model == request_model or price_model in request_model or request_model in price_model:
                matched_index = index
                matched_request = request_item
                break

        if matched_request is None:
            continue

        used_indexes.add(matched_index)

        unit_price = _decimal(price_line.get("unit_price"))
        # The request/enquiry row is a different email segment. Its quantity
        # cannot be copied into the supplier's later price-only reply.
        # This parser has no explicit same-line total, so both remain absent.
        quantity = None
        line_total = None

        items.append({
            "row_type": "PRODUCT",
            "product_name": matched_request.get("product_name"),
            "model": matched_request.get("model"),
            "quantity": quantity,
            "unit_of_measurement": None,
            "unit_price": price_line.get("unit_price"),
            "_unit_price_verified_from_msg": True,
            "line_total": str(line_total) if line_total is not None else None,
            "currency": "USD",
            "supplier_name": supplier_name,
            "purchase_date": document_date,
            "remark": None,
            "price_reference_quality": 5,
        })

    return items


def _extract_msg_priced_table_items(email_body, supplier_name=None, document_date=None):
    body = str(email_body or "")
    lines = [_clean_email_line(line) for line in body.splitlines()]
    lines = [line for line in lines if line]

    request_descriptions = _extract_msg_request_descriptions(body)

    items = []
    currency = None
    table_started = False

    for line in lines:
        lower = line.lower()

        if "unit price" in lower and ("amount" in lower or "qua" in lower or "qty" in lower):
            table_started = True

            detected_currency = re.search(
                r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b",
                line,
                re.I,
            )

            if detected_currency:
                currency = _valid_currency(detected_currency.group(1))

            continue

        if not table_started:
            continue

        if re.search(r"^(hello|dear|best regards|regards|from)\b", line, re.I):
            break

        match = re.match(
            r"^\s*(?P<no>\d+)\s+"
            r"(?P<type>.+?)\s+"
            r"(?P<size>\d+(?:\*\d+(?:\.\d+)?)*)\s+"
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<unit_price>\d+(?:\.\d+)?)\s+"
            r"(?P<line_total>\d+(?:\.\d+)?)"
            r"(?:\s+(?P<tail>.*))?$",
            line,
            re.I,
        )

        if not match:
            continue

        product_type = _clean_text(match.group("type"))
        size = _clean_text(match.group("size"))

        fallback_name = " ".join(
            value for value in (product_type, size) if value
        ).strip()

        request_name = (
            request_descriptions[len(items)]
            if len(items) < len(request_descriptions)
            else None
        )

        items.append({
            "row_type": "PRODUCT",
            "product_name": request_name or fallback_name,
            "model": product_type if request_name else None,
            "specification": None if request_name else size,
            "quantity": match.group("qty"),
            "unit_price": match.group("unit_price"),
            "_unit_price_verified_from_msg": True,
            "line_total": match.group("line_total"),
            "currency": currency,
            "supplier_name": supplier_name,
            "purchase_date": document_date,
            "remark": _clean_text(match.group("tail")),
            "price_reference_quality": 5,
        })

    return items
def _recover_msg_product_name(
    product_name,
    email_body,
    item_code=None,
    part_number=None,
    unit_price=None,
    source_item=None,
):
    """
    Recover richer product name from earlier request/reply
    email history using model/item code.

    Generic:
    - no supplier-specific values
    - matches model/code exactly
    - prefers request-table description
    """

    current = _clean_email_line(
        product_name
    )

    body = str(email_body or "")

    if not body:
        return current or None

    # Bind an explicitly priced numbered block by its amount, never by
    # extraction array index. Ambiguous equal-price blocks are left alone.
    if _decimal(unit_price) is not None:
        matches = []
        block = []
        for raw in body.splitlines():
            line = raw.strip()
            start = re.match(r'^\d+[.,)]\s*([A-Za-z].*?:)\s*$', line)
            if start:
                block = [start.group(1)]
                continue
            price = re.search(r'\bprice\s*:.*?\b(?:USD|AED|EUR|GBP|CNY)\s*([\d,]+(?:\.\d+)?)', line, re.I)
            if price and block:
                if _decimal(price.group(1)) == _decimal(unit_price):
                    unit = re.search(r'(?:/|\.)(PC|PCS|PCE|EA|SET|KG|M)\b', line, re.I)
                    matches.append((' '.join(block), unit[1].upper() if unit else None))
                block = []
            elif block and line:
                if re.match(r'^(from|sent|date|subject|dear|regards)\b', line, re.I):
                    block = []
                else:
                    block.append(line)
        if len(set(matches)) == 1:
            if source_item is not None:
                source_item['extra_attributes'] = []
                if matches[0][1]:
                    source_item['uom'] = matches[0][1]
            return re.sub(r'\s+', ' ', matches[0][0]).strip(' ,')

    identifiers = {
        str(value).strip().lower()
        for value in (
            item_code,
            part_number,
        )
        if value not in (None, "")
    }

    # Only alphanumeric identifiers are row anchors. Plain words such as
    # MOTOR otherwise match email prose and contaminate every description.
    for token in re.findall(
        r"\b[A-Z0-9][A-Z0-9._/-]{3,}\b",
        str(current or ""),
        re.I,
    ):
        if re.search(r'\d', token) and re.search(r'[A-Za-z]', token):
            identifiers.add(token.lower())

    if not identifiers:
        return current or None

    raw_lines = body.splitlines()

    candidates = []

    for index, raw_line in enumerate(raw_lines):
        line = _clean_email_line(raw_line)

        if not line:
            continue

        line_lower = line.lower()

        matched_identifier = None

        for identifier in identifiers:
            if re.search(
                rf"(?<![A-Za-z0-9])"
                rf"{re.escape(identifier)}"
                rf"(?![A-Za-z0-9])",
                line_lower,
                re.I,
            ):
                matched_identifier = identifier
                break

        if not matched_identifier:
            continue

        # Request tables commonly have:
        #
        # Product Name
        # Model Number
        #
        # so inspect nearby previous lines.
        nearby = []

        for offset in (1, 2, 3):
            previous_index = index - offset

            if previous_index < 0:
                continue

            previous = _clean_email_line(
                raw_lines[previous_index]
            )

            if not previous:
                continue

            if re.match(r'^(dear|hello|hi|please|kindly|good morning|good day)\b', previous, re.I):
                continue

            if re.match(
                r"^(sl\s*no|item|model\s*number|unit|qty|quantity)$",
                previous,
                re.I,
            ):
                continue

            if re.match(
                r"^(from|to|cc|date|subject)\s*:",
                previous,
                re.I,
            ):
                continue

            # Do not use another code-only line as product description.
            if re.fullmatch(
                r"[A-Z0-9._/()-]{4,}",
                previous,
                re.I,
            ):
                continue

            nearby.append(previous)

        for candidate in nearby:
            score = 0

            if re.search(r"[A-Za-z]{3,}", candidate):
                score += 2

            if len(candidate) >= 8:
                score += 1

            # Product-like descriptions are preferred.
            if not re.search(
                r"\$|USD|AED|EUR|GBP|\bnew\b|\bdays?\b",
                candidate,
                re.I,
            ):
                score += 2

            candidates.append(
                (score, candidate, matched_identifier)
            )

    if not candidates:
        return current or None

    candidates.sort(
        key=lambda value: (
            value[0],
            len(value[1]),
        ),
        reverse=True,
    )

    _, best_name, identifier = candidates[0]

    identifier_display = next(
        (
            value
            for value in (
                item_code,
                part_number,
            )
            if value
            and str(value).lower() == identifier
        ),
        identifier.upper(),
    )

    if identifier.lower() in best_name.lower():
        return best_name

    return f"{best_name} | {identifier_display}"
def _recover_msg_decimal_price(
    current_price,
    email_body,
    currency=None,
):
    """
    Recover an explicit decimal price from MSG body only when
    Gemini appears to have lost the decimal point.

    Examples:
        source USD0.246, Gemini 246   -> 0.246
        source USD 1.25, Gemini 1250 -> no change unless exact
                                        100/1000 relation exists

    Generic and conservative:
    - uses only explicit currency amounts from source
    - never guesses a new price
    """

    if current_price in (None, ""):
        return current_price

    current = _decimal(current_price)

    if current is None:
        return current_price

    body = str(email_body or "")

    currency_pattern = (
        r"AED|USD|EUR|GBP|INR|SAR|QAR|KWD|"
        r"OMR|BHD|JPY|CNY|SGD|AUD|CAD"
    )

    matches = re.findall(
        rf"\b({currency_pattern})\s*"
        rf"(-?\d+(?:\.\d+)?)\b",
        body,
        re.I,
    )

    wanted_currency = _valid_currency(currency)

    for source_currency, source_value in matches:
        source_currency = _valid_currency(
            source_currency
        )

        # When item's currency is known, never use an
        # amount belonging to another currency.
        if (
            wanted_currency
            and source_currency
            and wanted_currency != source_currency
        ):
            continue

        source_amount = _decimal(source_value)

        if source_amount is None:
            continue

        # No correction required.
        if current == source_amount:
            return str(source_amount)

        # Correct only obvious decimal-loss relationships.
        for factor in (
            Decimal("10"),
            Decimal("100"),
            Decimal("1000"),
            Decimal("10000"),
        ):
            if current == source_amount * factor:
                logger.warning(
                    "MSG decimal price corrected "
                    "current=%s source=%s currency=%s factor=%s",
                    current,
                    source_amount,
                    source_currency,
                    factor,
                )

                return str(source_amount)

    return current_price
def _read_msg_attachment_bytes(attachment):
    """
    Safely return bytes from an extract-msg attachment.
    """
    try:
        data = attachment.data
    except Exception:
        return None

    if data is None:
        return None

    if isinstance(data, bytes):
        return data

    if isinstance(data, bytearray):
        return bytes(data)

    if hasattr(data, "read"):
        try:
            return data.read()
        except Exception:
            return None

    return None
def _extract_source_currency(file_bytes):
    """
    Return a document-level currency ONLY when the complete PDF
    contains exactly one explicit currency.

    If multiple currencies are present (example RMB + USD),
    return None so each extracted item keeps its own source currency.
    """

    if not file_bytes:
        return None

    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))

        text = "\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

        if not text.strip():
            return None

        detected = set()

        currency_patterns = [
            ("AED", [
                r"\bAED\b",
                r"\bDHS\b",
                r"\bDIRHAMS?\b",
                r"د\.إ",
            ]),
            ("USD", [
                r"\bUSD\b",
                r"US\$",
            ]),
            ("EUR", [
                r"\bEUR\b",
                r"€",
            ]),
            ("GBP", [
                r"\bGBP\b",
                r"£",
            ]),
            ("INR", [
                r"\bINR\b",
                r"₹",
                r"\bRS\.?\s*(?=\d)",
                r"\bRUPEES?\b",
            ]),
            ("SAR", [
                r"\bSAR\b",
                r"\bSR\s*(?=\d)",
                r"ر\.س",
            ]),
            ("QAR", [
                r"\bQAR\b",
                r"\bQR\s*(?=\d)",
                r"ر\.ق",
            ]),
            ("KWD", [
                r"\bKWD\b",
                r"\bKD\s*(?=\d)",
                r"د\.ك",
            ]),
            ("OMR", [
                r"\bOMR\b",
                r"\bR\.?O\.?\s*(?=\d)",
                r"ر\.ع\.?",
            ]),
            ("BHD", [
                r"\bBHD\b",
                r"\bBD\s*(?=\d)",
                r"د\.ب",
            ]),
            ("SGD", [
                r"\bSGD\b",
                r"S\$",
            ]),
            ("AUD", [
                r"\bAUD\b",
                r"(?:AU\$|A\$)",
            ]),
            ("CAD", [
                r"\bCAD\b",
                r"(?:CA\$|C\$)",
            ]),
            ("CNY", [
                r"\bCNY\b",
                r"\bRMB\b",
                r"CN¥",
                r"元",
            ]),
            ("JPY", [
                r"\bJPY\b",
                r"JP¥",
            ]),
        ]

        for currency, patterns in currency_patterns:
            if any(
                re.search(pattern, text, re.I)
                for pattern in patterns
            ):
                detected.add(currency)

        # One unambiguous currency only.
        if len(detected) == 1:
            return next(iter(detected))

        # Multiple currencies:
        # never force one document currency onto all rows.
        if len(detected) > 1:
            logger.info(
                "Multiple explicit source currencies detected=%s",
                sorted(detected),
            )
            return None

        # Plain symbols are ambiguous.
        return None

    except Exception as exc:
        logger.warning(
            "Source currency extraction failed error=%s",
            exc,
        )
        return None
def _extract_source_document_date(file_bytes):
    """
    Extract the best usable document date directly
    from PDF text.

    Supported:
    - yyyy.mm.dd
    - yyyy/mm/dd
    - yyyy-mm-dd
    - dd/mm/yyyy
    - mm/dd/yyyy when unambiguous
    - dd-mm-yyyy
    - dd.mm.yyyy
    - 24 November 2024
    - November 24, 2024
    - Jan. 15,2026
    - Month + year -> first day of month

    Important:
    This is only a deterministic fallback.
    A commercial date already correctly extracted by
    Gemini should take priority elsewhere in the flow.
    """

    if not file_bytes:
        return None

    try:
        import io

        extracted_texts = []

        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                plumber_text = "\n".join(
                    page.extract_text() or ""
                    for page in pdf.pages
                )
                if plumber_text.strip():
                    extracted_texts.append(plumber_text)
        except Exception:
            pass

        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(file_bytes))
            pypdf_text = "\n".join(
                page.extract_text() or ""
                for page in reader.pages
            )
            if pypdf_text.strip():
                extracted_texts.append(pypdf_text)
        except Exception:
            pass

        if not extracted_texts:
            return None

        text = max(
            extracted_texts,
            key=lambda value: (
                bool(re.search(r"(?im)^\s*date\s*[:\-]\s*[A-Za-z]", value)),
                len(value),
            ),
        )
        labelled_full_text_date = re.compile(
            r"(?im)^\s*date\s*[:\-]\s*"
            r"("
            r"[A-Za-z]{3,9}\.?\s+\d{1,2}"
            r"(?:st|nd|rd|th)?"
            r",?\s*\d{4}"
            r"|"
            r"\d{1,2}(?:st|nd|rd|th)?\s+"
            r"[A-Za-z]{3,9}\.?\s+\d{4}"
            r"|"
            r"\d{4}[./-]\d{1,2}[./-]\d{1,2}"
            r"|"
            r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
            r")"
        )

        for extracted_text in extracted_texts:
            labelled_match = labelled_full_text_date.search(extracted_text)
            if labelled_match:
                parsed = _valid_date(labelled_match.group(1))
                if parsed:
                    return parsed

        # -------------------------------------------------
        # 1. DATE NEAR AN EXPLICIT DOCUMENT-DATE LABEL
        #
        # Prefer:
        # Quotation Date
        # Invoice Date
        # PO Date
        # Document Date
        # Date:
        #
        # This prevents unrelated dates such as:
        # Expiration / delivery / bank dates
        # from taking priority.
        # -------------------------------------------------
        labelled_date_patterns = [
            (
                r"(?im)^\s*date\s*[:\-]\s*"
                r"([^\n\r]{1,60})"
            ),
            (
                r"(?i)\bquotation\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\binvoice\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\bproforma\s+invoice\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\bpo\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\bpurchase\s+order\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\border\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\bdocument\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\boffer\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
            (
                r"(?i)\bquote\s+date\b"
                r"\s*[:\-]?\s*"
                r"([^\n\r]{1,40})"
            ),
        ]

        for pattern in labelled_date_patterns:
            match = re.search(
                pattern,
                text,
            )

            if not match:
                continue

            candidate_text = (
                match.group(1)
                .strip()
            )

            # Try complete captured value first.
            parsed = _valid_date(
                candidate_text
            )

            if parsed:
                return parsed

            # Sometimes the label and another field are extracted
            # on the same PDF text line. Find a date substring.
            date_candidates = re.findall(
                r"\d{4}[./-]\d{1,2}[./-]\d{1,2}"
                r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
                r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{2,4}"
                r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?\s+\d{2,4}"
                r"|\d{8}",
                candidate_text,
                flags=re.I,
            )

            for date_candidate in date_candidates:
                parsed = _valid_date(
                    date_candidate
                )

                if parsed:
                    return parsed

            if parsed:
                return parsed

        # -------------------------------------------------
        # 2. YEAR-FIRST NUMERIC
        #
        # 2026.6.11
        # 2026/6/11
        # 2026-6-11
        # -------------------------------------------------
        year_first = re.search(
            r"\b(\d{4})[./-]"
            r"(\d{1,2})[./-]"
            r"(\d{1,2})\b",
            text,
        )

        if year_first:
            parsed = _valid_date(
                year_first.group(0)
            )

            if parsed:
                return parsed

        # -------------------------------------------------
        # 3. NUMERIC DAY/MONTH OR MONTH/DAY
        #
        # 18/06/2026
        # 06/18/2026
        # -------------------------------------------------
        numeric_matches = re.finditer(
            r"\b"
            r"(\d{1,2})[./-]"
            r"(\d{1,2})[./-]"
            r"(\d{4})"
            r"\b",
            text,
        )

        for match in numeric_matches:
            parsed = _valid_date(
                match.group(0)
            )

            if parsed:
                return parsed

        # -------------------------------------------------
        # 4. FULL TEXT DATE
        #
        # 24 November 2024
        # 24 Nov 2024
        # -------------------------------------------------
        day_month_year = re.search(
            r"\b"
            r"(\d{1,2})\s+"
            r"([A-Za-z]{3,9})\.?\s+"
            r"(\d{4})"
            r"\b",
            text,
            re.I,
        )

        if day_month_year:
            candidate = (
                f"{day_month_year.group(1)} "
                f"{day_month_year.group(2)} "
                f"{day_month_year.group(3)}"
            )

            parsed = _valid_date(
                candidate
            )

            if parsed:
                return parsed

        # -------------------------------------------------
        # 5. MONTH DAY YEAR
        #
        # November 24, 2024
        # Jan. 15,2026
        # -------------------------------------------------
        month_day_year = re.search(
            r"\b"
            r"([A-Za-z]{3,9})\.?\s+"
            r"(\d{1,2}),?\s*"
            r"(\d{4})"
            r"\b",
            text,
            re.I,
        )

        if month_day_year:
            candidate = (
                f"{month_day_year.group(1)} "
                f"{month_day_year.group(2)}, "
                f"{month_day_year.group(3)}"
            )

            parsed = _valid_date(
                candidate
            )

            if parsed:
                return parsed

        # -------------------------------------------------
        # 6. MONTH + FOUR-DIGIT YEAR
        #
        # November 2024
        # Nov 2024
        #
        # User rule:
        # when month + year is available,
        # use first day of that month.
        # -------------------------------------------------
        month_year = re.search(
            r"\b("
            r"Jan(?:uary)?|"
            r"Feb(?:ruary)?|"
            r"Mar(?:ch)?|"
            r"Apr(?:il)?|"
            r"May|"
            r"Jun(?:e)?|"
            r"Jul(?:y)?|"
            r"Aug(?:ust)?|"
            r"Sep(?:t(?:ember)?)?|"
            r"Oct(?:ober)?|"
            r"Nov(?:ember)?|"
            r"Dec(?:ember)?"
            r")\.?\s+"
            r"(\d{4})"
            r"\b",
            text,
            re.I,
        )

        if month_year:
            month_text = (
                month_year
                .group(1)
                .rstrip(".")
            )

            year = int(
                month_year.group(2)
            )

            for fmt in (
                "%b",
                "%B",
            ):
                try:
                    month = datetime.strptime(
                        month_text,
                        fmt,
                    ).month

                    return _safe_business_date(
                        datetime(
                            year,
                            month,
                            1,
                        )
                    )

                except ValueError:
                    continue

        # -------------------------------------------------
        # 7. MONTH + TWO-DIGIT YEAR
        #
        # November 24
        # Nov 24
        #
        # -> 2024-11-01
        # -------------------------------------------------
        short_month_year = re.search(
            r"\b("
            r"Jan(?:uary)?|"
            r"Feb(?:ruary)?|"
            r"Mar(?:ch)?|"
            r"Apr(?:il)?|"
            r"May|"
            r"Jun(?:e)?|"
            r"Jul(?:y)?|"
            r"Aug(?:ust)?|"
            r"Sep(?:t(?:ember)?)?|"
            r"Oct(?:ober)?|"
            r"Nov(?:ember)?|"
            r"Dec(?:ember)?"
            r")\.?\s+"
            r"(\d{2})"
            r"\b",
            text,
            re.I,
        )

        if short_month_year:
            month_text = (
                short_month_year
                .group(1)
                .rstrip(".")
            )

            year = (
                2000
                + int(
                    short_month_year.group(2)
                )
            )

            for fmt in (
                "%b",
                "%B",
            ):
                try:
                    month = datetime.strptime(
                        month_text,
                        fmt,
                    ).month

                    return _safe_business_date(
                        datetime(
                            year,
                            month,
                            1,
                        )
                    )

                except ValueError:
                    continue

        return None

    except Exception as exc:
        logger.warning(
            "Source document date extraction failed "
            "error=%s",
            exc,
        )

        return None
def _estimate_numbered_rows_from_pdf_page(page_bytes):
    """
    Estimate visible numbered commercial rows from one PDF page.

    Used only as an under-extraction safety check.
    Does not create items itself.
    """

    if not page_bytes:
        return 0

    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(
            io.BytesIO(page_bytes)
        )

        text = "\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

        numbers = []

        for raw_line in text.splitlines():
            line = re.sub(
                r"\s+",
                " ",
                raw_line,
            ).strip()

            match = re.match(
                r"^(\d{1,3})(?:\s+|$)",
                line,
            )

            if match:
                numbers.append(
                    int(match.group(1))
                )

        numbers = sorted(set(numbers))

        if len(numbers) < 2:
            return 0

        longest = 1
        current = 1

        for index in range(
            1,
            len(numbers),
        ):
            if (
                numbers[index]
                == numbers[index - 1] + 1
            ):
                current += 1
                longest = max(
                    longest,
                    current,
                )
            else:
                current = 1

        return (
            longest
            if longest >= 2
            else 0
        )

    except Exception:
        return 0
class GeminiExtractionService:
    def __init__(self):
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        self.model_name = settings.GEMINI_MODEL
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=90000),
        )
    def _generate_content_with_retry(
        self,
        *,
        contents,
        config,
        attempts=3,
    ):
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                return self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )

            except Exception as exc:
                last_error = exc

                message = str(exc).upper()

                retryable = any(
                    token in message
                    for token in (
                        "DEADLINE_EXCEEDED",
                        "504",
                        "TIMEOUT",
                        "READTIMEOUT",
                        "TIMED OUT",
                        "READ OPERATION TIMED OUT",
                        "503",
                        "UNAVAILABLE",
                    )
                )

                if not retryable or attempt >= attempts:
                    raise

                logger.warning(
                    "Gemini temporary failure attempt=%s/%s error=%s",
                    attempt,
                    attempts,
                    exc,
                )

                time.sleep(attempt * 2)

        raise last_error
    def _msg_to_gemini_contents(
        self,
        file_field,
        original_file_name,
    ):
        """
        Parse Outlook .msg locally.

        Gemini must never receive the raw .msg binary because
        application/octet-stream is not supported.

        Returns:
        - email metadata/body as text
        - PDF/image attachments as Gemini binary parts
        - TXT/CSV attachments as text
        - Excel attachments converted to tab-separated text
        """

        import tempfile

        try:
            import extract_msg
        except ImportError as exc:
            raise RuntimeError(
                "MSG support requires extract-msg. "
                "Run: pip install extract-msg"
            ) from exc

        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            file_field.open("rb")

        raw_msg = file_field.read()

        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            pass

        temp_path = None
        message = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".msg",
                delete=False,
            ) as temp_file:
                temp_file.write(raw_msg)
                temp_path = temp_file.name

            message = extract_msg.Message(temp_path)

            subject = str(
                getattr(message, "subject", "") or ""
            ).strip()

            sender = str(
                getattr(message, "sender", "") or ""
            ).strip()

            to_value = str(
                getattr(message, "to", "") or ""
            ).strip()

            cc_value = str(
                getattr(message, "cc", "") or ""
            ).strip()

            sent_date = str(
                getattr(message, "date", "") or ""
            ).strip()

            body = str(
                getattr(message, "body", "") or ""
            ).strip()

            email_text = f"""
OUTLOOK EMAIL SOURCE

File Name: {original_file_name}
Subject: {subject}
From: {sender}
To: {to_value}
CC: {cc_value}
Email Sent Date: {sent_date}

EMAIL BODY:
{body}
""".strip()

            contents = [
                GEMINI_EXTRACTION_PROMPT
                + """

EMAIL / MSG EXTRACTION RULES:
- This source was an Outlook .msg email.
- Read the email body and every supplied attachment.
- The sender/vendor company who supplied the commercial price is the supplier.
- Do not use the To/recipient company as supplier.
- An attached quotation/invoice/PO takes priority over the email body for its own document number, date, supplier, currency and item prices.
- If the email body itself contains the commercial quotation/prices, extract those rows too.
- Do not duplicate an item merely because the same quotation text appears both in the email body and an attachment.
- Email Sent Date is context only. Prefer the explicit quotation/invoice/PO/commercial document date when present.
- If no commercial document date exists but the email itself is the commercial offer, the email sent date may be used.
""",
                email_text,
            ]

            attachments = list(
                getattr(message, "attachments", []) or []
            )
            word_attachment_names = [
                str(getattr(attachment, "longFilename", None)
                    or getattr(attachment, "filename", None)
                    or "").strip()
                for attachment in attachments
                if Path(
                    str(getattr(attachment, "longFilename", None)
                        or getattr(attachment, "filename", None)
                        or "")
                ).suffix.lower() in {".docx", ".doc"}
            ]
            if word_attachment_names:
                contents[0] += """

IMPORTANT WORD-ATTACHMENT PRIORITY RULE:
The attached Word document is the authoritative commercial source when it
contains a quotation table. Extract product/service names exactly from the
visible DESCRIPTION cell in that Word table. Do not combine an email-body
shipment/project heading with the DESCRIPTION cell. Do not prepend or append
context such as "SHIP SPARES IN TRANSIT", COO, HS code, supplier name, or
other text unless that text is physically part of the same DESCRIPTION cell.
The email body may be used for metadata only when the Word document does not
provide it. Word attachment(s): """ + ", ".join(word_attachment_names)
            has_commercial_attachments = False
            source_pdf_attachments = []
            source_word_texts = []
            # Outlook may retain orphan copies of inline buyer-request images.
            # Identify them by HTML ownership and exact bytes, not filenames.
            import hashlib
            # Some Outlook MSG files contain malformed RTF charset metadata.
            # extract-msg/RTFDE may then raise UnicodeDecodeError while lazily
            # materializing htmlBody (for example a bad gbk/cp936 byte). HTML
            # is supplementary evidence only, so keep the readable plain body
            # and continue extraction when that optional representation fails.
            try:
                html_body = getattr(message, "htmlBody", b"") or b""
            except (UnicodeDecodeError, LookupError, ValueError) as exc:
                html_body = b""
                logger.warning(
                    "MSG htmlBody decoding failed; continuing with plain body "
                    "file=%s error=%s",
                    original_file_name,
                    exc,
                )
            context_ids = context_image_ids(html_body) if html_body else set()
            html_text = html_body.decode("utf-8", errors="replace") if isinstance(html_body, bytes) else html_body
            # Some Outlook MSG files expose the visible signature only in
            # htmlBody while message.body is empty. Keep this text only for
            # supplier evidence; it is not treated as a new commercial row.
            html_supplier_text = re.sub(
                r"<(?:br|/p|/div|/td|/tr|/li)\b[^>]*>",
                "\n",
                html_text or "",
                flags=re.I,
            )
            html_supplier_text = re.sub(r"<[^>]+>", " ", html_supplier_text)
            supplier_body = "\n".join(
                part for part in (body, html_supplier_text)
                if part
            )
            context_hashes = {
                hashlib.sha256(_read_msg_attachment_bytes(a) or b"").digest()
                for a in attachments if getattr(a, "cid", None) in context_ids
            }

            logger.info(
                "MSG parsed file=%s subject=%s attachments=%s",
                original_file_name,
                subject,
                len(attachments),
            )

            for index, attachment in enumerate(
                attachments,
                start=1,
            ):
                attachment_name = str(
                    getattr(
                        attachment,
                        "longFilename",
                        None,
                    )
                    or getattr(
                        attachment,
                        "shortFilename",
                        None,
                    )
                    or f"attachment_{index}"
                ).strip()

                attachment_bytes = (
                    _read_msg_attachment_bytes(
                        attachment
                    )
                )

                if not attachment_bytes:
                    logger.warning(
                        "MSG attachment skipped because data is empty "
                        "file=%s attachment=%s",
                        original_file_name,
                        attachment_name,
                    )
                    continue

                attachment_suffix = Path(
                    attachment_name
                ).suffix.lower()

                attachment_mime = (
                    mimetypes.guess_type(
                        attachment_name
                    )[0]
                )

                # ------------------------------------------
                # PDF
                # ------------------------------------------
                if attachment_suffix == ".pdf":
                    source_pdf_attachments.append({'name': attachment_name, 'data': attachment_bytes})
                    has_commercial_attachments = True
                    contents.append(
                        f"\n--- MSG ATTACHMENT {index}: "
                        f"{attachment_name} ---"
                    )

                    contents.append(
                        types.Part.from_bytes(
                            data=attachment_bytes,
                            mime_type="application/pdf",
                        )
                    )

                    continue

                # ------------------------------------------
                # IMAGE
                # ------------------------------------------
                if attachment_suffix in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".gif",
                    ".tif",
                    ".tiff",
                }:
                    image_mime_map = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".webp": "image/webp",
                        ".gif": "image/gif",
                        ".tif": "image/tiff",
                        ".tiff": "image/tiff",
                    }
                    image_bytes = attachment_bytes
                    image_mime = image_mime_map[attachment_suffix]
                    # Gemini image extraction is more reliable for inline
                    # Outlook GIF signatures when normalized to PNG.
                    if attachment_suffix == ".gif":
                        try:
                            from io import BytesIO
                            from PIL import Image

                            with Image.open(BytesIO(attachment_bytes)) as image:
                                image = image.convert("RGB")
                                buffer = BytesIO()
                                image.save(buffer, format="PNG")
                                image_bytes = buffer.getvalue()
                            image_mime = "image/png"
                        except Exception:
                            logger.warning(
                                "MSG GIF attachment could not be converted "
                                "file=%s attachment=%s",
                                original_file_name,
                                attachment_name,
                            )
                    cid = getattr(attachment, "cid", None)
                    context_only = cid in context_ids or (
                        cid and f"cid:{cid}" not in html_text
                        and hashlib.sha256(attachment_bytes).digest() in context_hashes
                    )
                    if not context_only and not _image_too_small_for_document(image_bytes):
                        has_commercial_attachments = True

                    contents.append(
                        f"\n--- MSG ATTACHMENT {index}: "
                        f"{attachment_name} ---"
                    )

                    contents.append(
                        types.Part.from_bytes(
                            data=image_bytes,
                            mime_type=image_mime,
                        )
                    )

                    continue

                # ------------------------------------------
                # TXT / CSV
                # ------------------------------------------
                if attachment_suffix in {
                    ".txt",
                    ".csv",
                }:
                    has_commercial_attachments = True
                    try:
                        attachment_text = (
                            attachment_bytes.decode(
                                "utf-8-sig",
                                errors="replace",
                            )
                        )

                        contents.append(
                            f"""
--- MSG ATTACHMENT {index}: {attachment_name} ---

{attachment_text}
"""
                        )
                    except Exception as exc:
                        logger.warning(
                            "MSG text attachment read failed "
                            "attachment=%s error=%s",
                            attachment_name,
                            exc,
                        )

                    continue

                # ------------------------------------------
                # EXCEL
                # ------------------------------------------
                if attachment_suffix in {
                    ".xlsx",
                    ".xls",
                }:
                    has_commercial_attachments = True
                    try:
                        from django.core.files.uploadedfile import (
                            SimpleUploadedFile,
                        )

                        spreadsheet_file = (
                            SimpleUploadedFile(
                                name=attachment_name,
                                content=attachment_bytes,
                                content_type=attachment_mime,
                            )
                        )

                        spreadsheet_text = (
                            self._spreadsheet_to_text(
                                spreadsheet_file,
                                attachment_name,
                            )
                        )

                        if spreadsheet_text.strip():
                            contents.append(
                                f"""
--- MSG EXCEL ATTACHMENT {index}: {attachment_name} ---

{spreadsheet_text}
"""
                            )

                    except Exception as exc:
                        logger.warning(
                            "MSG Excel attachment parsing failed "
                            "attachment=%s error=%s",
                            attachment_name,
                            exc,
                        )

                    continue

                # ------------------------------------------
                # WORD DOCUMENTS
                # ------------------------------------------
                # Word files are common quotation attachments.  Extract their
                # paragraphs and table cells as structured text so prices in
                # a DOCX are sent to the same commercial-row extraction flow
                # as PDF/Excel attachments.  This deliberately avoids treating
                # the document title or filename as an item.
                if attachment_suffix in {".docx", ".doc"}:
                    has_commercial_attachments = True
                    try:
                        document_text = self._word_document_to_text(
                            attachment_bytes,
                            attachment_name,
                        )
                        if document_text.strip():
                            source_word_texts.append(document_text)
                            contents.append(
                                f"""
--- MSG WORD ATTACHMENT {index}: {attachment_name} ---

The following content comes from the attached Word document. For rows from
tables in this attachment, use the visible DESCRIPTION cell as the complete
product/service name. Do not prepend email-body context, shipment labels,
parent headings, or inferred text that is not present in that cell. Keep
separate COO/HS CODE columns as attributes, not as a replacement description.

{document_text}
"""
                            )
                        else:
                            logger.warning(
                                "MSG Word attachment contained no readable text "
                                "file=%s attachment=%s",
                                original_file_name,
                                attachment_name,
                            )
                    except Exception as exc:
                        logger.warning(
                            "MSG Word attachment parsing failed "
                            "attachment=%s error=%s",
                            attachment_name,
                            exc,
                        )

                    continue

                logger.info(
                    "MSG attachment ignored unsupported_type "
                    "file=%s attachment=%s extension=%s",
                    original_file_name,
                    attachment_name,
                    attachment_suffix,
                )

            return contents, {
                "subject": subject,
                "has_commercial_attachments": has_commercial_attachments,
                "source_pdf_attachments": source_pdf_attachments,
                "source_word_texts": source_word_texts,
                "html_body": html_text,
                "sender": sender,
                "to": to_value,
                "cc": cc_value,
                "sent_date": sent_date,
                "body": body,
                "signature_company": (
                    (
                        _extract_company_from_email_body(supplier_body)
                        or _commercial_msg_supplier(supplier_body, sender, sent_date)
                    )
                ),
                "signature_company_verified": bool(
                    _extract_company_from_email_body(supplier_body)
                ),
                "sender_company": (
                    _supplier_from_sender(
                        sender
                    )
                ),
            }

        finally:
            if message is not None:
                try:
                    message.close()
                except Exception:
                    pass

            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    def _repair_suspicious_product_descriptions(
        self,
        result,
        split_pages,
        original_file_name,
        import_batch=None,
    ):
        """
        Repair PRODUCT descriptions only when the first extraction has
        obvious description-binding problems.

        IMPORTANT:
        - never changes quantity
        - never changes UOM
        - never changes unit_price
        - never changes line_total
        - never adds/removes items
        """

        if not split_pages:
            return result

        items = list(result.get("items") or [])
        if not items:
            return result

        suspicious_indexes = set()

        product_rows = []

        for index, item in enumerate(items):
            if str(item.get("row_type") or "").upper() != "PRODUCT":
                continue

            name = _clean_text(item.get("product_name")) or ""
            item_code = _clean_text(item.get("item_code")) or ""
            part_number = _clean_text(item.get("part_number")) or ""

            product_rows.append((index, item))

            normalized_name = name.strip().lower()
            normalized_code = item_code.strip().lower()
            normalized_part = part_number.strip().lower()

            if (
                re.search(r"\bdear\s+(?:mr|ms|mrs|sir)\b|please\s+find|as\s+below", normalized_name)
                or normalized_name.rstrip(":").strip() in {"motor", "cable", "service", "product", "item"}
            ):
                suspicious_indexes.add(index)

            semantic_attribute_count = sum(
                1 for attr in (item.get("extra_attributes") or [])
                if re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip()
                in {"cpu", "processor", "ram", "storage", "display", "os", "io ports", "i o ports", "power rating", "mounting", "feature", "voltage", "frequency", "rpm", "certification"}
            )
            if semantic_attribute_count >= 2:
                suspicious_indexes.add(index)

            # Obvious incomplete description:
            # product_name is only the model / item code / part number.
            if normalized_name and normalized_name in {
                normalized_code,
                normalized_part,
            }:
                suspicious_indexes.add(index)

        # Same long description assigned to different priced rows can indicate
        # a page-boundary continuation was attached to the wrong item.
        name_groups = {}

        for index, item in product_rows:
            name = re.sub(
                r"\s+",
                " ",
                str(item.get("product_name") or "").strip().lower(),
            )

            if not name:
                continue

            name_groups.setdefault(name, []).append((index, item))

        for grouped_rows in name_groups.values():
            if len(grouped_rows) < 2:
                continue

            anchors = {
                (
                    str(item.get("item_code") or "").strip().lower(),
                    str(item.get("part_number") or "").strip().lower(),
                    str(item.get("unit_price") or "").strip(),
                )
                for _, item in grouped_rows
            }

            # Same description attached to commercially different rows.
            if len(anchors) > 1:
                for index, _ in grouped_rows:
                    suspicious_indexes.add(index)

        if not suspicious_indexes:
            return result

        repair_payload = []

        for index in sorted(suspicious_indexes):
            item = items[index]

            repair_payload.append({
                "index": index,
                "row_type": item.get("row_type"),
                "item_code": item.get("item_code"),
                "part_number": item.get("part_number"),
                "page_number": item.get("page_number"),
                "quantity": item.get("quantity"),
                "uom": item.get("uom"),
                "unit_price": item.get("unit_price"),
                "line_total": item.get("line_total"),
                "current_product_name": item.get("product_name"),
            })

        prompt = """
    Review ONLY the product descriptions for the supplied extracted rows.

    The PDF pages below are the original source document.

    For each supplied row:
    - locate that exact priced row using row sequence, S/N, item code, part number,
    quantity, unit price and line total
    - determine the technical description belonging to THAT priced row
    - description text may continue onto the following page
    - text at the top of a page before the next priced row belongs to the previous
    priced row
    - never copy another row's description
    - repeated item codes or repeated prices are allowed and represent separate rows
    - stop before MFR/manufacturer metadata, country of origin, warranty, website,
    optional accessories, repair kits and recommendation/marketing text

    CRITICAL:
    Return ONLY corrected product_name values.
    Do not change quantity, UOM, unit price, line total, currency, supplier, date,
    category or number of items.

    Return JSON exactly in this form:
    {
    "items": [
        {
        "index": 0,
        "product_name": "correct full technical description"
        }
    ]
    }

    Rows needing description review:
    """ + json.dumps(repair_payload, default=str)

        contents = [prompt]

        for page_number, page_bytes in enumerate(split_pages, start=1):
            contents.append(
                f"--- PAGE {page_number} OF {len(split_pages)} ---"
            )
            contents.append(
                types.Part.from_bytes(
                    data=page_bytes,
                    mime_type="application/pdf",
                )
            )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=8192,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=1024
                    ),
                ),
            )

            repair_result = _safe_json(response.text)

            for correction in repair_result.get("items") or []:
                try:
                    index = int(correction.get("index"))
                except (TypeError, ValueError):
                    continue

                if index not in suspicious_indexes:
                    continue

                if index < 0 or index >= len(items):
                    continue

                repaired_name = _clean_product_name(
                    correction.get("product_name"),
                    "PRODUCT",
                )

                if not repaired_name:
                    continue

                # Description ONLY.
                items[index]["product_name"] = repaired_name

            result["items"] = items

            self._log_usage(
                response,
                import_batch,
                "product_description_repair",
                {
                    "file_name": original_file_name,
                    "repaired_indexes": sorted(suspicious_indexes),
                },
            )

        except Exception as exc:
            # Never fail the original extraction just because
            # optional description repair failed.
            logger.warning(
                "Gemini product description repair failed batch=%s file=%s error=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                exc,
            )

        return result
    def _reconcile_orphan_continuation_products(self, result):
        """
        Merge an uncoded continuation-only PRODUCT row into the previous
        coded PRODUCT row.

        Intended for multi-page tables where Gemini separates:
        - priced row on page N
        - technical description continuation on page N+1

        Only runs under strict conditions.
        """

        items = list(result.get("items") or [])
        if len(items) < 2:
            return result

        remove_indexes = set()

        for index in range(1, len(items)):
            current = items[index]
            previous = items[index - 1]

            if str(current.get("row_type") or "").upper() != "PRODUCT":
                continue

            if str(previous.get("row_type") or "").upper() != "PRODUCT":
                continue

            # Current row must have no commercial identity.
            if current.get("item_code") or current.get("part_number"):
                continue

            # A priced row is not an orphan description continuation.
            if current.get("unit_price") not in (None, "") or current.get("line_total") not in (None, ""):
                continue

            # Previous row must have a real code/part number.
            if not (previous.get("item_code") or previous.get("part_number")):
                continue

            try:
                current_page = int(current.get("page_number"))
                previous_page = int(previous.get("page_number"))
            except (TypeError, ValueError):
                continue

            # Continuation should normally be next page.
            if current_page != previous_page + 1:
                continue

            current_name = _clean_product_name(
                current.get("product_name"),
                "PRODUCT",
            )

            previous_name = _clean_product_name(
                previous.get("product_name"),
                "PRODUCT",
            )

            if not current_name:
                continue

            # Require substantial technical description.
            if len(current_name) < 40:
                continue

            # Strong signal:
            # previous coded row duplicates an earlier commercial identity,
            # while current uncoded row looks like continuation description.
            duplicate_previous_identity = False

            previous_code = str(
                previous.get("item_code")
                or previous.get("part_number")
                or ""
            ).strip().lower()

            previous_price = str(
                previous.get("unit_price")
                or previous.get("line_total")
                or ""
            ).strip()

            for earlier in items[:index - 1]:
                earlier_code = str(
                    earlier.get("item_code")
                    or earlier.get("part_number")
                    or ""
                ).strip().lower()

                earlier_price = str(
                    earlier.get("unit_price")
                    or earlier.get("line_total")
                    or ""
                ).strip()

                if (
                    is_true_duplicate(earlier, previous)
                ):
                    duplicate_previous_identity = True
                    break

            if not duplicate_previous_identity:
                continue

            # Keep commercial identity/price from the coded row.
            # Replace only its description using the continuation row.
            previous["product_name"] = current_name

            remove_indexes.add(index)

            logger.warning(
                "Merged orphan continuation PRODUCT index=%s into previous index=%s "
                "code=%s page=%s->%s",
                index,
                index - 1,
                previous_code,
                previous_page,
                current_page,
            )

        if remove_indexes:
            result["items"] = [
                item
                for index, item in enumerate(items)
                if index not in remove_indexes
            ]

        return result
    def _verify_repeated_product_prices(
        self,
        result,
        split_pages,
        original_file_name,
        import_batch=None,
    ):
        """
        Re-check commercial values only for repeated PRODUCT codes/part numbers
        whose extracted prices disagree.

        This does NOT:
        - add/remove items
        - change product_name
        - change supplier/date/category/currency
        """

        if not split_pages or len(split_pages) <= 1:
            return result

        items = list(result.get("items") or [])
        if not items:
            return result

        groups = {}

        for index, item in enumerate(items):
            if str(item.get("row_type") or "").upper() != "PRODUCT":
                continue

            code = str(
                item.get("item_code")
                or item.get("part_number")
                or ""
            ).strip().lower()

            if not code:
                continue

            groups.setdefault(code, []).append((index, item))

        suspicious_indexes = set()

        for grouped_rows in groups.values():
            if len(grouped_rows) < 2:
                continue

            prices = {
                str(
                    item.get("unit_price")
                    or item.get("line_total")
                    or ""
                ).strip()
                for _, item in grouped_rows
            }

            # Same code repeated but extracted prices disagree.
            # Do not guess which one is correct — verify against source PDF.
            if len(prices) > 1:
                for index, _ in grouped_rows:
                    suspicious_indexes.add(index)

        if not suspicious_indexes:
            return result

        review_rows = []

        for index in sorted(suspicious_indexes):
            item = items[index]

            review_rows.append({
                "index": index,
                "item_code": item.get("item_code"),
                "part_number": item.get("part_number"),
                "product_name": item.get("product_name"),
                "page_number": item.get("page_number"),
                "current_quantity": item.get("quantity"),
                "current_uom": item.get("uom"),
                "current_unit_price": item.get("unit_price"),
                "current_line_total": item.get("line_total"),
            })

        prompt = """
    Verify ONLY the printed commercial values of the PRODUCT rows listed below.

    Read the original PDF pages visually.

    IMPORTANT:
    - locate EACH exact printed priced row separately
    - use row order / S/N / item code / part number to distinguish repeated rows
    - the same item code or part number may legitimately appear more than once
    - never assume that a repeated code has the same price
    - never copy a price from another occurrence
    - quantity, UOM, unit_price and line_total must come from THAT exact row
    - description continuation on the following page must NEVER supply a price
    - Total, Subtotal, Taxable Amount, VAT/GST/Tax and Grand Total are not item prices
    - unit_price = the printed Unit Rate / Unit Price
    - line_total = that row's own pre-tax Amount / Taxable Amount
    - preserve the number of rows exactly
    - do not add or remove products

    Return JSON only:

    {
    "items": [
        {
        "index": 0,
        "quantity": "1",
        "uom": "PCS",
        "unit_price": "3400.00",
        "line_total": "3400.00"
        }
    ]
    }

    Rows to verify:
    """ + json.dumps(review_rows, default=str)

        contents = [prompt]

        for page_number, page_bytes in enumerate(split_pages, start=1):
            contents.append(
                f"--- PAGE {page_number} OF {len(split_pages)} ---"
            )
            contents.append(
                types.Part.from_bytes(
                    data=page_bytes,
                    mime_type="application/pdf",
                )
            )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=1024
                    ),
                ),
            )

            verified = _safe_json(response.text)

            for correction in verified.get("items") or []:
                try:
                    index = int(correction.get("index"))
                except (TypeError, ValueError):
                    continue

                if index not in suspicious_indexes:
                    continue

                if index < 0 or index >= len(items):
                    continue

                quantity = _decimal(correction.get("quantity"))
                unit_price = _decimal(correction.get("unit_price"))
                line_total = _decimal(correction.get("line_total"))
                uom = _clean_text(correction.get("uom"))

                # Require meaningful commercial values.
                if unit_price is None and line_total is None:
                    continue

                # If all three values exist, they must mathematically reconcile.
                if (
                    quantity is not None
                    and unit_price is not None
                    and line_total is not None
                ):
                    expected = quantity * unit_price

                    tolerance = max(
                        Decimal("0.05"),
                        abs(line_total) * Decimal("0.03"),
                    )

                    if abs(expected - line_total) > tolerance:
                        logger.warning(
                            "Repeated product price verification rejected "
                            "index=%s qty=%s rate=%s total=%s",
                            index,
                            quantity,
                            unit_price,
                            line_total,
                        )
                        continue

                if quantity is not None:
                    items[index]["quantity"] = str(quantity)

                if uom:
                    items[index]["uom"] = uom

                if unit_price is not None:
                    items[index]["unit_price"] = str(unit_price)

                if line_total is not None:
                    items[index]["line_total"] = str(line_total)

            result["items"] = items

            self._log_usage(
                response,
                import_batch,
                "repeated_product_price_verification",
                {
                    "file_name": original_file_name,
                    "verified_indexes": sorted(suspicious_indexes),
                },
            )

        except Exception as exc:
            logger.warning(
                "Gemini repeated product price verification failed "
                "batch=%s file=%s error=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                exc,
            )

        return result
    def _verify_visible_document_date(
        self,
        file_bytes,
        mime_type,
        original_file_name,
        import_batch=None,
    ):
        if not (
            str(mime_type or "").startswith("image/")
            or mime_type == "application/pdf"
        ):
            return None

        prompt = """
Read ONLY the visible email/document date from this file.

For scanned invoices/transport forms:
- Prefer the date written next to the printed "Date:" label in the invoice header.
- Read handwritten slash dates carefully. In forms from 2026, "08" may look like "03"; do not return March when the visible month is August.
- Ignore dates inside unrelated gate pass/access fee pages when the main commercial invoice page has its own Date label.
- For quotations/invoices, prefer explicit labels like "DATE:", "Quotation Date", "Invoice Date", or "Quote Date".

Return JSON only:
{
  "document_date": "YYYY-MM-DD",
  "date_candidates": ["YYYY-MM-DD"]
}

Rules:
- Prefer the visible email header date directly above the commercial item/cost table.
- For email screenshots, use the visible sent/received header date such as "Mon 22-Jun-26 12:37 PM".
- Interpret 2-digit years as 2000+YY. Example: 22-Jun-26 = 2026-06-22.
- Do not swap day and year.
- Do not use footer revision dates, form template dates, totals, due dates, or unrelated dates.
- If no reliable visible date exists, return {"document_date": null}.
"""

        try:
            response = self._generate_content_with_retry(
                contents=[
                    prompt,
                    types.Part.from_bytes(
                        data=file_bytes,
                        mime_type=mime_type,
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=512
                    ),
                ),
            )

            parsed = _safe_json(response.text)
            date_candidates = parsed.get("date_candidates") or []

            if parsed.get("document_date"):
                date_candidates.insert(0, parsed.get("document_date"))

            verified_date = _best_visible_date_candidate(
                date_candidates,
                original_file_name,
            )

            filename_year = _filename_year_hint(original_file_name)

            if verified_date and filename_year:
                try:
                    parsed_year = int(str(verified_date).split("-")[0])

                    if abs(parsed_year - filename_year) >= 5:
                        verified_date = (
                            f"{filename_year}-"
                            f"{str(verified_date).split('-')[1]}-"
                            f"{str(verified_date).split('-')[2]}"
                        )
                except Exception:
                    pass

            logger.warning(
                "GEMINI DATE VERIFY file=%s date=%s raw=%s",
                original_file_name,
                verified_date,
                response.text,
            )

            return verified_date

        except Exception as exc:
            logger.warning(
                "Gemini date verification failed batch=%s file=%s error=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                exc,
            )
            return None

    def _verify_visible_document_category(
        self,
        file_bytes,
        mime_type,
        original_file_name,
        import_batch=None,
    ):
        if not (
            str(mime_type or "").startswith("image/")
            or mime_type == "application/pdf"
        ):
            return None

        prompt = """
Read ONLY the visible document title/category from this file.

Return JSON only:
{
  "category": "INVOICE|QUOTATION|PURCHASE_ORDER|GRN|LPO|PRICE_LIST|OTHER"
}

Rules:
- If the visible heading says TAX INVOICE, INVOICE, or VAT INVOICE, return INVOICE.
- If it says QUOTATION, QUOTE, OFFER, or PRICE OFFER, return QUOTATION.
- Do not use product category or business category.
- If unclear, return OTHER.
"""

        try:
            response = self._generate_content_with_retry(
                contents=[
                    prompt,
                    types.Part.from_bytes(
                        data=file_bytes,
                        mime_type=mime_type,
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=256
                    ),
                ),
            )

            parsed = _safe_json(response.text)
            category = _normalize_category(parsed.get("category"))

            logger.warning(
                "GEMINI CATEGORY VERIFY file=%s category=%s raw=%s",
                original_file_name,
                category,
                response.text,
            )
            return category

        except Exception as exc:
            logger.warning(
                "Gemini category verification failed batch=%s file=%s error=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                exc,
            )
            return None


    def _extract_small_pdf_with_page_context(
        self,
        split_pages,
        original_file_name,
        import_batch=None,
    ):
        """
        Small PDF extraction (2-5 pages).

        Each original page owns only the priced rows that START on that page.
        The next page is supplied only as context so a long description can
        continue across the page boundary.

        This prevents:
        - next-page description being attached to the wrong item
        - the same row being extracted twice
        - summary totals being attached to the previous product
        """

        total_pages = len(split_pages)

        merged = {
            "document_type": "other",
            "category": "OTHER",
            "supplier_name": None,
            "document_date": None,
            "currency": None,
            "document_number": None,
            "subtotal": None,
            "grand_total": None,
            "items": [],
        }

        for primary_page in range(1, total_pages + 1):
            previous_page = (
                primary_page - 1
                if primary_page > 1
                else None
            )

            next_page = (
                primary_page + 1
                if primary_page < total_pages
                else None
            )

            prompt = (
                GEMINI_EXTRACTION_PROMPT
                + (
                    f"\n\nSMALL PDF PAGE-CONTEXT EXTRACTION.\n"
                    f"The original document has {total_pages} pages.\n"
                    f"PRIMARY PAGE: {primary_page}.\n"
                )
            )

            if previous_page:
                prompt += (
                    f"PREVIOUS CONTEXT PAGE: {previous_page}.\n"
                    f"Use PREVIOUS CONTEXT PAGE {previous_page} only to determine whether "
                    f"text appearing at the TOP of PRIMARY PAGE {primary_page} is a continuation "
                    f"of a priced row that started on the previous page. "
                    f"Never attach such previous-row continuation text to a new row that starts "
                    f"on PRIMARY PAGE {primary_page}.\n"
                )

            if next_page:
                prompt += (
                    f"NEXT CONTEXT PAGE: {next_page}.\n"
                    f"Extract ONLY priced PRODUCT, SERVICE or CHARGE rows whose "
                    f"S/N, item-code, part-number, quantity/rate or other priced-row "
                    f"anchor STARTS on PRIMARY PAGE {primary_page}.\n"
                    f"Use NEXT CONTEXT PAGE {next_page} only to complete the description "
                    f"of a row that started on PRIMARY PAGE {primary_page}.\n"
                    f"Do NOT output a new priced row that starts on NEXT CONTEXT PAGE "
                    f"{next_page}.\n"
                )
            else:
                prompt += (
                    f"Extract ONLY priced rows that start on PRIMARY PAGE "
                    f"{primary_page}.\n"
                )

            prompt += (
                f"Set page_number={primary_page} for every extracted item because "
                f"the priced row starts on that primary page.\n"
                f"Do not treat Total, Subtotal, Taxable Amount, VAT/GST/Tax or "
                f"Grand Total as an item.\n"
            )

            contents = [prompt]

            if previous_page:
                contents.extend([
                    f"--- PREVIOUS CONTEXT PAGE {previous_page} OF {total_pages} ---",
                    types.Part.from_bytes(
                        data=split_pages[previous_page - 1],
                        mime_type="application/pdf",
                    ),
                ])

            contents.extend([
                f"--- PRIMARY PAGE {primary_page} OF {total_pages} ---",
                types.Part.from_bytes(
                    data=split_pages[primary_page - 1],
                    mime_type="application/pdf",
                ),
            ])

            if next_page:
                contents.extend([
                    f"--- NEXT CONTEXT PAGE {next_page} OF {total_pages} ---",
                    types.Part.from_bytes(
                        data=split_pages[next_page - 1],
                        mime_type="application/pdf",
                    ),
                ])

            logger.info(
                "Gemini small PDF primary-context batch=%s previous=%s primary=%s next=%s",
                getattr(import_batch, "id", None),
                previous_page,
                primary_page,
                next_page,
            )

            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=16384,
                    response_mime_type="application/json",
                    response_schema=GEMINI_RESPONSE_SCHEMA,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=1024
                    ),
                ),
            )

            page_result = normalize_result(
                _safe_json(response.text)
            )

            page_category = (
                page_result.get("category")
                or page_result.get("document_type")
                or "OTHER"
            )

            page_document_type = page_result.get("document_type")
            page_document_number = page_result.get("document_number")
            page_document_date = _date_matches_filename_year(
                (
                    self._verify_visible_document_date(
                        split_pages[primary_page - 1],
                        "application/pdf",
                        original_file_name,
                        import_batch,
                    )
                    or page_result.get("document_date")
                ),
                original_file_name,
            )

            for item in page_result.get("items") or []:
                # PRIMARY PAGE owns this row.
                item["page_number"] = primary_page

                if not item.get("source_category"):
                    item["source_category"] = page_category

                if not item.get("source_document_type"):
                    item["source_document_type"] = page_document_type

                if not item.get("source_document_number"):
                    item["source_document_number"] = page_document_number

                if page_document_date:
                    item["purchase_date"] = page_document_date
                item_unit_price = _decimal(item.get("unit_price"))
                item_line_total = _decimal(
                    item.get("line_total")
                    if item.get("line_total") not in (None, "")
                    else item.get("total")
                )

                page_subtotal = _decimal(page_result.get("subtotal"))
                page_grand_total = _decimal(page_result.get("grand_total"))

                summary_amounts = {
                    value
                    for value in (page_subtotal, page_grand_total)
                    if value is not None
                }

                candidate_amount = (
                    item_line_total
                    if item_line_total is not None
                    else item_unit_price
                )

                if (
                    candidate_amount is not None
                    and any(
                        abs(candidate_amount - summary) <= max(
                            Decimal("0.05"),
                            abs(summary) * Decimal("0.001"),
                        )
                        for summary in summary_amounts
                    )
                ):
                    continue
                merged["items"].append(item)

            # Preserve useful document metadata.
            for field in (
                "supplier_name",
                "document_date",
                "currency",
                "document_number",
                "subtotal",
                "grand_total",
            ):
                value = page_result.get(field)

                if (
                    not merged.get(field)
                    and value not in (None, "", "UNKNOWN")
                ):
                    merged[field] = value

            if (
                merged.get("document_type") in (None, "", "other")
                and page_result.get("document_type")
            ):
                merged["document_type"] = page_result["document_type"]

            if (
                merged.get("category") in (None, "", "OTHER")
                and page_result.get("category")
            ):
                merged["category"] = page_result["category"]

            self._log_usage(
                response,
                import_batch,
                "small_pdf_primary_page_context",
                {
                    "file_name": original_file_name,
                    "primary_page": primary_page,
                    "previous_page": previous_page,
                    "next_page": next_page,
                    "items_found": len(page_result.get("items") or []),
                },
            )

        merged = normalize_result(merged)

        merged["_gemini_small_page_context"] = True
        merged["_gemini_processed_pages"] = list(
            range(1, total_pages + 1)
        )
        merged["_gemini_total_pages"] = total_pages
        merged["_gemini_stopped_early"] = False

        return merged
    def _extract_large_pdf_progressively(
        self,
        split_pages,
        original_file_name,
        import_batch=None,
    ):
        """
        Generic page-complete PDF extraction.

        One compact Gemini vision call per page.
        Retry only when Gemini itself reports more visible prices
        than the number of returned items.
        """

        total_pages = len(split_pages)

        merged = {
            "document_type": "other",
            "category": "OTHER",
            "supplier_name": None,
            "document_date": None,
            "currency": None,
            "document_number": None,
            "subtotal": None,
            "grand_total": None,
            "items": [],
        }

        processed_pages = []
        failed_pages = []
        incomplete_pages = []
        validator = ExtractionValidator()
        primary_supplier_key = None
        primary_document_key = None
        supporting_pages = []
        supplier_columns = []
        supplier_column_x = {}
        spec_column = {}

        for page_number, page_bytes in enumerate(
            split_pages,
            start=1,
        ):
            logger.info(
                "Gemini PDF page extraction "
                "batch=%s page=%s/%s file=%s",
                getattr(import_batch, "id", None),
                page_number,
                total_pages,
                original_file_name,
            )

            page_prompt = (
                PDF_PAGE_EXTRACTION_PROMPT
                + f"\nYou are extracting ORIGINAL PAGE {page_number} OF {total_pages}."
                + "\nPrevious/next pages may be supplied as CONTEXT ONLY."
                + "\nReturn an item only when that item's priced row or commercial amount belongs to the CURRENT PAGE."
                + "\nIf a product description starts on the previous page but its quantity/unit price/amount row is on the current page, use previous page context to complete product_name."
                + "\nIf a product description continues onto the next page, use next page context only to complete product_name."
                + "\nDo not create a separate item from continuation text."
                + "\nDo not create a separate item from a model/part number alone when the full description appears on an adjacent page."
                + "\nNever use subtotal, VAT, tax, grand total, final total, total amount or amount in words as item price."
                + "\nline_total must be the row's own pre-tax amount. If only VAT-inclusive total is visible, do not use it as line_total."
                + f"\nSet page_number={page_number} for every returned item."
            )
            if supplier_columns:
                page_prompt += (
                    "\nThis is a continuation page of a supplier-comparison table. "
                    "Carry forward the supplier columns from page 1 in this exact left-to-right order: "
                    + " | ".join(supplier_columns)
                    + ". Preserve empty cells at their original positions; an empty first supplier "
                    "cell means the first supplier has no quotation and must not cause later prices "
                    "to shift left. Return no item for that blank cell."
                )

            contents = [page_prompt]

            if page_number > 1:
                contents.append(
                    f"--- CONTEXT ONLY: ORIGINAL PAGE {page_number - 1} OF {total_pages} ---"
                )
                contents.append(
                    types.Part.from_bytes(
                        data=split_pages[page_number - 2],
                        mime_type="application/pdf",
                    )
                )

            contents.append(
                f"--- CURRENT PAGE TO EXTRACT: ORIGINAL PAGE {page_number} OF {total_pages} ---"
            )
            contents.append(
                types.Part.from_bytes(
                    data=page_bytes,
                    mime_type="application/pdf",
                )
            )

            if page_number < total_pages:
                contents.append(
                    f"--- CONTEXT ONLY: ORIGINAL PAGE {page_number + 1} OF {total_pages} ---"
                )
                contents.append(
                    types.Part.from_bytes(
                        data=split_pages[page_number],
                        mime_type="application/pdf",
                    )
                )

            page_result = None
            response = None

            try:
                response = self._generate_content_with_retry(
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=GEMINI_RESPONSE_SCHEMA,
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=512
                        ),
                    ),
                    attempts=2,
                )

                page_result = _safe_json(response.text)
                logger.warning(
                    "GEMINI PDF PAGE RAW OUTPUT file=%s page=%s/%s output=%s",
                    original_file_name,
                    page_number,
                    total_pages,
                    json.dumps(page_result, default=str, ensure_ascii=False, indent=2),
                )

                validation = validator.validate(
                    page_result,
                    page_number=page_number,
                )
                logger.warning(
                    "GEMINI PDF PAGE VALIDATION file=%s page=%s/%s validation=%s",
                    original_file_name,
                    page_number,
                    total_pages,
                    json.dumps(validation, default=str, ensure_ascii=False),
                )

                if validation.get("requires_retry"):
                    retry_prompt = build_retry_prompt(
                        validation.get("issues") or [],
                        page_number=page_number,
                    )
                    logger.warning(
                        "GEMINI PDF PAGE FOCUSED RETRY file=%s page=%s/%s issues=%s",
                        original_file_name,
                        page_number,
                        total_pages,
                        json.dumps(validation.get("issues") or [], default=str, ensure_ascii=False),
                    )
                    retry_response = self._generate_content_with_retry(
                        contents=[
                            retry_prompt,
                            types.Part.from_bytes(
                                data=page_bytes,
                                mime_type="application/pdf",
                            ),
                        ],
                        config=types.GenerateContentConfig(
                            temperature=0,
                            response_mime_type="application/json",
                            response_schema=GEMINI_RESPONSE_SCHEMA,
                            thinking_config=types.ThinkingConfig(
                                thinking_budget=512
                            ),
                        ),
                        attempts=2,
                    )
                    retry_result = _safe_json(retry_response.text)
                    retry_validation = validator.validate(
                        retry_result,
                        page_number=page_number,
                    )
                    logger.warning(
                        "GEMINI PDF PAGE RETRY OUTPUT file=%s page=%s/%s validation=%s output=%s",
                        original_file_name,
                        page_number,
                        total_pages,
                        json.dumps(retry_validation, default=str, ensure_ascii=False),
                        json.dumps(retry_result, default=str, ensure_ascii=False, indent=2),
                    )
                    self._log_usage(
                        retry_response,
                        import_batch,
                        "pdf_focused_validation_retry",
                        {
                            "file_name": original_file_name,
                            "page": page_number,
                            "issues": validation.get("issues") or [],
                            "retry_valid": retry_validation.get("is_valid"),
                        },
                    )
                    original_issue_codes = {
                        issue.get("code")
                        for issue in validation.get("issues") or []
                    }

                    if "SUPPORTING_PAGE_ITEM" in original_issue_codes and not retry_result.get("items"):
                        page_result = retry_result
                        validation = retry_validation
                    else:
                        page_result = _merge_focused_retry_result(
                            page_result,
                            retry_result,
                            validation.get("issues") or [],
                        )
                        validation = retry_validation if retry_validation.get("is_valid") else validation

                page_result = normalize_result(page_result)
                logger.warning(
                    "GEMINI PDF PAGE OUTPUT file=%s page=%s/%s output=%s",
                    original_file_name,
                    page_number,
                    total_pages,
                    json.dumps(page_result, default=str, ensure_ascii=False, indent=2),
                )
            except Exception as exc:
                logger.warning(
                    "Gemini PDF page failed "
                    "page=%s error=%s",
                    page_number,
                    exc,
                )
                # A transient/schema/context failure must not permanently
                # discard a commercial page. Retry with a compact page-only
                # request, without neighbouring context, before marking the
                # page failed.
                try:
                    recovery_response = self._generate_content_with_retry(
                        contents=[
                            (
                                "Extract every visible priced product row from this single PDF page. "
                                "Preserve the original supplier-column alignment, blank cells, quantity, "
                                "specification and amount. Do not return totals, discounts, VAT or tax "
                                "as product rows. Set page_number=%s. Return schema JSON only."
                                % page_number
                            ),
                            types.Part.from_bytes(data=page_bytes, mime_type="application/pdf"),
                        ],
                        config=types.GenerateContentConfig(
                            temperature=0,
                            response_mime_type="application/json",
                            response_schema=GEMINI_RESPONSE_SCHEMA,
                            thinking_config=types.ThinkingConfig(thinking_budget=512),
                        ),
                        attempts=2,
                    )
                    recovered_page = normalize_result(_safe_json(recovery_response.text))
                    if recovered_page.get("items"):
                        page_result = recovered_page
                        logger.warning(
                            "GEMINI PDF PAGE RECOVERED file=%s page=%s items=%s",
                            original_file_name,
                            page_number,
                            len(recovered_page.get("items") or []),
                        )
                    else:
                        failed_pages.append(page_number)
                        continue
                except Exception as recovery_exc:
                    logger.warning(
                        "PDF page recovery failed page=%s error=%s",
                        page_number,
                        recovery_exc,
                    )
                    failed_pages.append(page_number)
                    continue

            processed_pages.append(page_number)

            # The progressive loop owns page identity.  Gemini/retry output
            # is never allowed to retain a stale page number.
            for item in page_result.get("items") or []:
                item["page_number"] = page_number

            comparison_mode = (
                _detect_supplier_comparison_layout(page_bytes, supplier_columns)
                or
                len(supplier_column_x) >= 2
                or len({
                    _supplier_identity_key(item.get("supplier_name"))
                    for item in page_result.get("items") or []
                    if _supplier_identity_key(item.get("supplier_name"))
                }) >= 2
            )

            # Adjacent pages are context only.  Accept a Gemini row for this
            # page only when its printed commercial amount and product text
            # are physically grounded on the current page.
            # For supplier matrices, English Gemini translations may not be
            # present in the PDF text layer.  Do not destructively filter
            # candidates by product-text overlap; physical matrix recovery
            # below decides which price cells actually exist.
            if not comparison_mode:
                page_result["items"] = [
                    item for item in page_result.get("items") or []
                    if _item_has_current_page_commercial_evidence(
                        item,
                        page_bytes,
                        page_number,
                        supplier_column_x,
                    )
                ]

            positions_verified = _realign_suppliers_by_column_position(
                page_result.get("items") or [],
                page_bytes,
                supplier_column_x,
                supplier_registry=supplier_columns,
            )
            if positions_verified:
                _append_pdf_specifications(
                    page_result.get("items") or [], page_bytes, supplier_column_x, spec_column
                )

            discovered_suppliers = _supplier_column_order(page_result.get("items") or [])
            for supplier in discovered_suppliers:
                if _supplier_identity_key(supplier) not in {
                    _supplier_identity_key(value) for value in supplier_columns
                }:
                    supplier_columns.append(supplier)
            # Once physical supplier columns are known, never fall back to
            # sequence-based continuation repair: that logic cannot represent
            # a blank supplier cell and would shift later prices left.
            if not positions_verified and len(supplier_column_x) < 3:
                _repair_continuation_supplier_alignment(
                    page_result.get("items") or [], supplier_columns, page_number
                )

            page_items = list(
                page_result.get("items") or []
            )

            expected_count = page_result.get(
                "visible_priced_row_count"
            )

            try:
                expected_count = int(
                    expected_count
                )
            except (TypeError, ValueError):
                expected_count = None

            actual_count = len(page_items)

            # -----------------------------------------
            # Retry only when clearly under-extracted.
            # -----------------------------------------
            if (
                not comparison_mode
                and
                not positions_verified
                and
                expected_count is not None
                and expected_count > actual_count
            ):
                logger.warning(
                    "PDF page under-extracted "
                    "page=%s expected=%s actual=%s",
                    page_number,
                    expected_count,
                    actual_count,
                )

                retry_prompt = f"""
    Read this single PDF page again.

    You identified {expected_count} real current commercial
    price references but returned only {actual_count} items.

    Return ALL {expected_count} price references exactly once.

    A price reference may be:
    - table row
    - bullet
    - email sentence
    - paragraph
    - standalone quoted product/service/fee

    Do not require item numbers.
    Do not return subtotal, VAT/tax or grand total.
    Do not return genuinely free rows where both unit price and total are zero.
    Always return a row with a positive printed unit price, even when quantity
    and/or total are zero (optional/catalogue rates). Preserve the printed zeros.
    Do not invent amounts.

    Set page_number={page_number}.

    Return schema JSON only.
    """

                try:
                    retry_response = (
                        self._generate_content_with_retry(
                            contents=[
                                retry_prompt,
                                types.Part.from_bytes(
                                    data=page_bytes,
                                    mime_type="application/pdf",
                                ),
                            ],
                            config=types.GenerateContentConfig(
                                temperature=0,
                                response_mime_type="application/json",
                                response_schema=GEMINI_RESPONSE_SCHEMA,
                                thinking_config=types.ThinkingConfig(
                                    thinking_budget=512
                                ),
                            ),
                            attempts=2,
                        )
                    )

                    retry_result = normalize_result(
                        _safe_json(
                            retry_response.text
                        )
                    )

                    retry_items = list(
                        retry_result.get("items") or []
                    )

                    retry_expected = retry_result.get("visible_priced_row_count")
                    try:
                        retry_expected = int(retry_expected)
                    except (TypeError, ValueError):
                        retry_expected = None

                    if len(retry_items) >= actual_count:
                        page_result = retry_result
                        page_items = retry_items
                        actual_count = len(retry_items)
                        for item in page_result.get("items") or []:
                            item["page_number"] = page_number
                        # A retry may change supplier ordering.  Never accept
                        # it over a physically verified matrix page; when the
                        # retry was necessary, re-run x-position binding
                        # before using its rows.
                        positions_verified = _realign_suppliers_by_column_position(
                            page_result.get("items") or [],
                            page_bytes,
                            supplier_column_x,
                            supplier_registry=supplier_columns,
                        )
                        if retry_expected is not None:
                            expected_count = retry_expected

                    self._log_usage(
                        retry_response,
                        import_batch,
                        "pdf_under_extraction_retry",
                        {
                            "file_name": original_file_name,
                            "page": page_number,
                            "expected": expected_count,
                            "after": actual_count,
                        },
                    )

                except Exception as exc:
                    logger.warning(
                        "PDF page recovery failed "
                        "page=%s error=%s",
                        page_number,
                        exc,
                    )

            # Do not mark review only because Gemini's own visible_priced_row_count
            # is higher than returned items. That count is also model-generated and
            # can include subtotal/VAT/summary rows. Real validation happens on the
            # accepted commercial rows below.
            if (
                expected_count is not None
                and actual_count == 0
                and expected_count > 0
            ):
                incomplete_pages.append(
                    page_number
                )

            page_category = (
                page_result.get("category")
                or "OTHER"
            )

            page_document_type = (
                page_result.get(
                    "document_type"
                )
            )

            page_document_number = (
                page_result.get(
                    "document_number"
                )
            )

            page_supplier_key = _supplier_identity_key(page_result.get("supplier_name"))
            page_document_key = str(
                page_document_type or page_category or ""
            ).lower()
            supporting_document = bool(
                re.search(
                    r"\b(payment\s+receipt|receipt|gate\s+pass|permit|delivery\s+proof|customs\s+receipt|supporting\s+voucher|tahseel)\b",
                    " ".join(
                        str(page_result.get(field) or "")
                        for field in ("document_type", "category", "supplier_name")
                    ),
                    re.I,
                )
            )
            page_role = "PRIMARY_COMMERCIAL"
            if primary_supplier_key is None and page_items and not supporting_document:
                primary_supplier_key = page_supplier_key
                primary_document_key = page_document_key
            elif page_items and supporting_document:
                page_role = "SUPPORTING_DOCUMENT"
            # A supplier change can mark a new commercial section in a
            # multi-page quotation. Keep its priced rows; only explicitly
            # non-commercial document types are supporting pages.

            logger.info(
                "PDF_PAGE_ROLE file=%s page=%s role=%s supplier=%s document_type=%s items=%s",
                original_file_name,
                page_number,
                page_role,
                page_result.get("supplier_name"),
                page_document_type or page_category,
                len(page_items),
            )
            if page_role == "SUPPORTING_DOCUMENT":
                supporting_pages.append({
                    "page_number": page_number,
                    "supplier_name": page_result.get("supplier_name"),
                    "document_type": page_document_type or page_category,
                    "item_count": len(page_items),
                })
                continue

            page_document_date = _date_matches_filename_year(
                page_result.get(
                    "document_date"
                ),
                original_file_name,
            )

            page_item_dates = [
                item.get("purchase_date") for item in page_items
            ]
            page_date_candidates = [page_document_date]
            for item in page_items:
                item_date = _valid_date(item.get("purchase_date"))
                if not item_date:
                    continue
                reconciled_date, changed_date = _reconcile_item_year(
                    item_date=item_date,
                    page_date_candidates=page_date_candidates,
                    sibling_item_dates=page_item_dates,
                    document_candidates=[
                        merged.get("document_date"),
                        *[
                            existing.get("purchase_date")
                            for existing in merged.get("items") or []
                        ],
                    ],
                )
                if changed_date:
                    logger.warning(
                        "ITEM_YEAR_RECONCILE page=%s product=%r old=%r candidates=%r new=%r reason=%s",
                        page_number,
                        item.get("product_name"),
                        item_date,
                        [changed_date],
                        reconciled_date,
                        "same_day_month_year_corrected_from_page_context",
                    )
                    item["purchase_date"] = reconciled_date

            def reconcile_handwritten_item_date(item_date, page_date):
                if not item_date or not page_date or item_date == page_date:
                    return item_date
                try:
                    item_parts = item_date.split("-")
                    page_parts = page_date.split("-")
                    if (
                        len(item_parts) == 3
                        and len(page_parts) == 3
                        and item_parts[1:] == page_parts[1:]
                        and item_parts[0] != page_parts[0]
                    ):
                        return page_date
                except Exception:
                    pass
                return item_date

            for item in page_items:
                item["page_number"] = (
                    page_number
                )

                if not item.get(
                    "source_category"
                ):
                    item[
                        "source_category"
                    ] = page_category

                if not item.get(
                    "source_document_type"
                ):
                    item[
                        "source_document_type"
                    ] = page_document_type

                if not item.get(
                    "source_document_number"
                ):
                    item[
                        "source_document_number"
                    ] = page_document_number

                if page_document_date and not _valid_date(
                    item.get("purchase_date")
                ):
                    item[
                        "purchase_date"
                    ] = page_document_date
                elif page_document_date:
                    old_item_date = _valid_date(item.get("purchase_date"))
                    reconciled_item_date = reconcile_handwritten_item_date(
                        old_item_date,
                        page_document_date,
                    )
                    if reconciled_item_date != old_item_date:
                        item["purchase_date"] = reconciled_item_date
                        logger.warning(
                            "HANDWRITTEN_DATE_RECONCILE page=%s old=%s new=%s reason=%s",
                            page_number,
                            old_item_date,
                            reconciled_item_date,
                            "same_day_month_page_year_corroboration",
                        )

                merged["items"].append(
                    item
                )

            for field in (
                "supplier_name",
                "document_date",
                "currency",
                "document_number",
                "subtotal",
                "grand_total",
            ):
                value = page_result.get(
                    field
                )

                if (
                    not merged.get(field)
                    and value not in (
                        None,
                        "",
                        "UNKNOWN",
                    )
                ):
                    merged[field] = value

            if (
                merged.get("document_type")
                in (None, "", "other")
                and page_result.get(
                    "document_type"
                )
            ):
                merged[
                    "document_type"
                ] = page_result[
                    "document_type"
                ]

            if (
                merged.get("category")
                in (None, "", "OTHER")
                and page_result.get(
                    "category"
                )
            ):
                merged[
                    "category"
                ] = page_result[
                    "category"
                ]

            if response is not None:
                self._log_usage(
                    response,
                    import_batch,
                    "pdf_page_extraction",
                    {
                        "file_name": original_file_name,
                        "page": page_number,
                        "total_pages": total_pages,
                        "visible_prices": expected_count,
                        "items_found": actual_count,
                    },
                )

        merged = normalize_result(
            merged
        )

        merged_validation = validator.validate(merged)
        logger.warning(
            "GEMINI PDF FINAL VALIDATION file=%s pages=%s validation=%s",
            original_file_name,
            total_pages,
            json.dumps(merged_validation, default=str, ensure_ascii=False),
        )

        merged["_gemini_page_complete"] = True
        merged["_gemini_processed_pages"] = processed_pages
        merged["_gemini_total_pages"] = total_pages
        merged["_gemini_failed_pages"] = failed_pages
        merged["_gemini_incomplete_pages"] = incomplete_pages
        merged["_supporting_pages"] = supporting_pages

        merged["_gemini_needs_review"] = bool(
            failed_pages
            or incomplete_pages
        )
        logger.warning(
            "GEMINI PDF FINAL MERGED OUTPUT file=%s pages=%s item_count=%s output=%s",
            original_file_name,
            total_pages,
            len(merged.get("items") or []),
            json.dumps(merged, default=str, ensure_ascii=False, indent=2),
        )
        logger.info(
            "Gemini PDF extraction complete "
            "batch=%s pages=%s items=%s "
            "failed=%s incomplete=%s",
            getattr(
                import_batch,
                "id",
                None,
            ),
            total_pages,
            len(
                merged.get("items")
                or []
            ),
            failed_pages,
            incomplete_pages,
        )

        return merged
    def _extract_spreadsheet_in_chunks(
        self,
        sheet_text,
        original_file_name,
        import_batch=None,
        chunk_size=40,
    ):
        """
        Extract large spreadsheet text in smaller chunks.

        Important:
        - preserve source order
        - do not deduplicate items
        - keep supplier-specific price rows separate
        - merge metadata conservatively
        """

        raw_lines = [
            line
            for line in str(sheet_text or "").splitlines()
            if line.strip()
        ]

        if not raw_lines:
            return {
                "document_type": "other",
                "category": "OTHER",
                "supplier_name": None,
                "document_date": None,
                "currency": None,
                "document_number": None,
                "items": [],
            }

        header_lines = []
        row_lines = []

        # Repeat sheet/header context in every Gemini chunk. The old
        # ``startswith('M')`` test classified ordinary data rows beginning
        # with M as headers and, more importantly, allowed supplier headers to
        # disappear from later chunks. Keep the sheet marker, merged-cell
        # declarations and the first ten physical rows as context; only rows
        # after that are chunked as commercial data.
        for line in raw_lines:
            row_match = re.match(r"^R(\d+)\t", line)
            if line.startswith("--- SHEET:") or line.startswith("M") and re.match(r"^M\d+:", line):
                header_lines.append(line)
            elif row_match and int(row_match.group(1)) <= 10:
                header_lines.append(line)
            else:
                row_lines.append(line)

        chunks = [
            row_lines[index:index + chunk_size]
            for index in range(0, len(row_lines), chunk_size)
        ]

        merged = {
            "document_type": "other",
            "category": "OTHER",
            "supplier_name": None,
            "document_date": None,
            "currency": None,
            "document_number": None,
            "subtotal": None,
            "grand_total": None,
            "items": [],
        }

        total_chunks = len(chunks)

        for chunk_index, chunk_rows in enumerate(
            chunks,
            start=1,
        ):
            chunk_text = "\n".join(
                header_lines + chunk_rows
            )

            contents = [
                SPREADSHEET_EXTRACTION_PROMPT
                + (
                    f"\n\nSPREADSHEET CHUNK "
                    f"{chunk_index} OF {total_chunks}.\n"
                    f"Extract only the rows supplied in this chunk.\n"
                    f"Preserve source_reference exactly so rows remain traceable.\n"
                    f"Do not deduplicate rows across suppliers or source rows.\n"
                )
                + "\n\n--- SPREADSHEET DATA ---\n\n"
                + chunk_text
            ]

            logger.info(
                "Gemini spreadsheet chunk extraction "
                "batch=%s file=%s chunk=%s/%s rows=%s chars=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                chunk_index,
                total_chunks,
                len(chunk_rows),
                len(chunk_text),
            )

            try:
                response = self._generate_content_with_retry(
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=GEMINI_RESPONSE_SCHEMA,
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=1024
                        ),
                    ),
                )

            except Exception as exc:
                error_text = str(exc).upper()

                is_timeout = any(
                    token in error_text
                    for token in (
                        "DEADLINE_EXCEEDED",
                        "504",
                        "TIMEOUT",
                        "TIMED OUT",
                        "READTIMEOUT",
                    )
                )

                if is_timeout and len(chunk_rows) > 10:
                    logger.warning(
                        "Spreadsheet chunk timed out "
                        "file=%s chunk=%s/%s rows=%s. "
                        "Retrying smaller chunk.",
                        original_file_name,
                        chunk_index,
                        total_chunks,
                        len(chunk_rows),
                    )

                    smaller_text = "\n".join(
                        header_lines + chunk_rows
                    )

                    retry_result = (
                        self._extract_spreadsheet_in_chunks(
                            sheet_text=smaller_text,
                            original_file_name=original_file_name,
                            import_batch=import_batch,
                            chunk_size=max(
                                10,
                                len(chunk_rows) // 2,
                            ),
                        )
                    )

                    merged["items"].extend(
                        retry_result.get("items") or []
                    )

                    for field in (
                        "supplier_name",
                        "document_date",
                        "currency",
                        "document_number",
                        "subtotal",
                        "grand_total",
                    ):
                        if (
                            not merged.get(field)
                            and retry_result.get(field)
                            not in (None, "", "UNKNOWN")
                        ):
                            merged[field] = retry_result.get(field)

                    continue

                raise
            try:
                chunk_result = normalize_result(
                    _safe_json(response.text)
                )
            except json.JSONDecodeError as exc:
                finish_reason = None

                try:
                    finish_reason = str(
                        response.candidates[0].finish_reason
                    )
                except (
                    AttributeError,
                    IndexError,
                    TypeError,
                ):
                    pass

                logger.error(
                    "Spreadsheet chunk returned incomplete JSON "
                    "file=%s chunk=%s/%s finish_reason=%s "
                    "chars=%s error=%s",
                    original_file_name,
                    chunk_index,
                    total_chunks,
                    finish_reason,
                    len(response.text or ""),
                    exc,
                )

                # Retry this chunk with smaller pieces.
                if len(chunk_rows) > 20:
                    smaller_text = "\n".join(
                        header_lines + chunk_rows
                    )

                    retry_result = (
                        self._extract_spreadsheet_in_chunks(
                            sheet_text=smaller_text,
                            original_file_name=original_file_name,
                            import_batch=import_batch,
                            chunk_size=max(
                                20,
                                chunk_size // 2,
                            ),
                        )
                    )

                    merged["items"].extend(
                        retry_result.get("items") or []
                    )

                    for field in (
                        "supplier_name",
                        "document_date",
                        "currency",
                        "document_number",
                        "subtotal",
                        "grand_total",
                    ):
                        if (
                            not merged.get(field)
                            and retry_result.get(field)
                            not in (None, "", "UNKNOWN")
                        ):
                            merged[field] = retry_result.get(field)

                    continue

                raise ValueError(
                    "Gemini returned an incomplete spreadsheet extraction response."
                ) from exc

            merged["items"].extend(
                chunk_result.get("items") or []
            )

            for field in (
                "supplier_name",
                "document_date",
                "currency",
                "document_number",
                "subtotal",
                "grand_total",
            ):
                value = chunk_result.get(field)

                if (
                    not merged.get(field)
                    and value not in (
                        None,
                        "",
                        "UNKNOWN",
                    )
                ):
                    merged[field] = value

            if (
                merged.get("document_type")
                in (
                    None,
                    "",
                    "other",
                )
                and chunk_result.get("document_type")
            ):
                merged["document_type"] = (
                    chunk_result["document_type"]
                )

            if (
                merged.get("category")
                in (
                    None,
                    "",
                    "OTHER",
                )
                and chunk_result.get("category")
            ):
                merged["category"] = (
                    chunk_result["category"]
                )

            self._log_usage(
                response,
                import_batch,
                "spreadsheet_chunk_extraction",
                {
                    "file_name": original_file_name,
                    "chunk": chunk_index,
                    "total_chunks": total_chunks,
                    "source_rows": len(chunk_rows),
                    "items_found": len(
                        chunk_result.get("items") or []
                    ),
                },
            )

        merged = normalize_result(merged)

        logger.info(
            "Gemini spreadsheet chunk extraction finished "
            "batch=%s file=%s chunks=%s items=%s",
            getattr(import_batch, "id", None),
            original_file_name,
            total_chunks,
            len(merged.get("items") or []),
        )
        logger.warning(
            "GEMINI SPREADSHEET FINAL OUTPUT file=%s output=%s",
            original_file_name,
            json.dumps(merged, default=str, ensure_ascii=False, indent=2),
        )

        return merged
    def _spreadsheet_currency_from_formats(
        self,
        file_field,
        original_file_name,
    ):
        """
        Detect explicit spreadsheet currency from Excel number formats.

        Important:
        - Excel often stores 90 as the value and shows $90.00 only via
          number_format. Gemini may miss that after text conversion.
        - If exactly one currency appears in priced cells, use it as a
          deterministic source currency.
        """

        suffix = str(original_file_name or "").lower()

        if not suffix.endswith(".xlsx"):
            return None

        import io as io_module
        import openpyxl

        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            file_field.open("rb")

        raw = file_field.read()

        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            pass

        workbook = openpyxl.load_workbook(
            io_module.BytesIO(raw),
            data_only=True,
            read_only=False,
        )

        detected = []

        patterns = [
            ("USD", r"(?<![A-Z])US\$|(?<![A-Z])USD(?![A-Z])|\\\$|\$"),
            ("AED", r"(?<![A-Z])AED(?![A-Z])|د\.إ"),
            ("EUR", r"(?<![A-Z])EUR(?![A-Z])|€"),
            ("GBP", r"(?<![A-Z])GBP(?![A-Z])|£"),
            ("INR", r"(?<![A-Z])INR(?![A-Z])|₹"),
            ("SAR", r"(?<![A-Z])SAR(?![A-Z])|ر\.س"),
            ("QAR", r"(?<![A-Z])QAR(?![A-Z])|ر\.ق"),
            ("KWD", r"(?<![A-Z])KWD(?![A-Z])|د\.ك"),
            ("OMR", r"(?<![A-Z])OMR(?![A-Z])|ر\.ع"),
            ("BHD", r"(?<![A-Z])BHD(?![A-Z])|د\.ب"),
            ("SGD", r"(?<![A-Z])SGD(?![A-Z])|S\$"),
            ("AUD", r"(?<![A-Z])AUD(?![A-Z])|A\$|AU\$"),
            ("CAD", r"(?<![A-Z])CAD(?![A-Z])|C\$|CA\$"),
            ("CNY", r"(?<![A-Z])CNY(?![A-Z])|RMB|CN¥|¥"),
            ("JPY", r"(?<![A-Z])JPY(?![A-Z])|JP¥"),
        ]

        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.value in (None, ""):
                        continue

                    number_format = str(cell.number_format or "")

                    if not number_format or number_format.lower() == "general":
                        continue

                    for currency, pattern in patterns:
                        if re.search(pattern, number_format, re.I):
                            detected.append(currency)
                            break

        unique = list(dict.fromkeys(detected))

        if len(unique) == 1:
            return unique[0]

        return None
    def _word_document_to_text(self, raw, original_file_name):
        """Return readable paragraphs and tables from a Word attachment.

        DOCX is an Open XML zip, so this uses the standard library rather than
        requiring a desktop Word/LibreOffice installation on the API server.
        Legacy binary .doc files are reported as unsupported by this helper;
        they should be converted to .docx or PDF before upload.
        """
        import io
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET

        if str(original_file_name or "").lower().endswith(".doc"):
            raise ValueError(
                "Legacy .doc attachments require conversion to .docx or PDF"
            )

        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with ZipFile(io.BytesIO(raw)) as archive:
            document_xml = archive.read("word/document.xml")

        root = ET.fromstring(document_xml)
        output = []

        for block in root.findall(".//w:body/*", namespaces):
            tag = block.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = "".join(
                    node.text or ""
                    for node in block.findall(".//w:t", namespaces)
                ).strip()
                if text:
                    output.append(text)
            elif tag == "tbl":
                for row in block.findall("./w:tr", namespaces):
                    cells = []
                    for cell in row.findall("./w:tc", namespaces):
                        cell_text = " ".join(
                            (node.text or "").strip()
                            for node in cell.findall(".//w:t", namespaces)
                            if (node.text or "").strip()
                        )
                        cells.append(cell_text)
                    if any(cells):
                        output.append(" | ".join(cells))

        return "\n".join(output)

    def _spreadsheet_to_text(self, file_field, original_file_name):
        
        import csv as csv_module
        import io as io_module

        suffix = str(original_file_name or "").lower()
        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            file_field.open("rb")
        raw = file_field.read()
        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            pass

        lines = []

        def clean_header_label(value):
            text = str(value or "").strip()
            text = re.sub(r"\s+", " ", text)

            if not text:
                return None

            if _decimal(text) is not None:
                return None

            normalized = re.sub(
                r"[^a-z0-9]+",
                " ",
                text.lower(),
            ).strip()

            if normalized in {
                "",
                "yes",
                "no",
                "nil",
                "na",
                "n a",
                "project",
                "total",
                "subtotal",
            }:
                return None

            if normalized.startswith("project "):
                return None

            return text

        def header_label_for_cell(rows, row_index, col_index, value):
            labels = []
            seen = set()
            value_norm = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(value or "").lower(),
            ).strip()

            for previous_row_index in range(row_index):
                previous_row = rows[previous_row_index]

                first_visible_cell = next(
                    (
                        str(cell or "").strip()
                        for cell in previous_row
                        if str(cell or "").strip()
                    ),
                    "",
                )

                if _decimal(first_visible_cell) is not None:
                    continue

                if col_index >= len(rows[previous_row_index]):
                    continue

                label = clean_header_label(
                    rows[previous_row_index][col_index]
                )

                if not label:
                    continue

                label_norm = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    label.lower(),
                ).strip()

                if label_norm == value_norm or label_norm in seen:
                    continue

                seen.add(label_norm)
                labels.append(label)

            return " / ".join(labels[-2:]) if labels else None

        if suffix.endswith(".csv"):
            text = raw.decode("utf-8-sig", errors="replace")
            for row in csv_module.reader(io_module.StringIO(text)):
                lines.append("\t".join(str(cell or "") for cell in row))
        elif suffix.endswith(".xls"):
            # Old binary Excel format (.xls). openpyxl only supports the
            # newer zip-based .xlsx format and raises "File contains no
            # valid workbook part" on .xls, so use xlrd here instead.
            import xlrd

            workbook = xlrd.open_workbook(file_contents=raw)
            for sheet_index in range(workbook.nsheets):
                worksheet = workbook.sheet_by_index(sheet_index)
                lines.append(f"--- SHEET: {worksheet.name} ---")
                sheet_rows = [
                    worksheet.row_values(row_index)
                    for row_index in range(worksheet.nrows)
                ]
                for row_index in range(worksheet.nrows):
                    row_values = sheet_rows[row_index]
                    if any(str(cell or "").strip() for cell in row_values):
                        rendered_cells = []

                        for col_index, cell in enumerate(row_values):
                            if cell in (None, ""):
                                rendered_cells.append("")
                                continue

                            value_text = str(cell).strip()
                            label = header_label_for_cell(
                                sheet_rows,
                                row_index,
                                col_index,
                                value_text,
                            )

                            if label:
                                value_text = f"{label}: {value_text}"
                            elif col_index <= 2:
                                value_text = f"Item descriptor: {value_text}"

                            rendered_cells.append(value_text)

                        lines.append("\t".join(rendered_cells))
        else:
            import openpyxl
            from openpyxl.utils import get_column_letter

            workbook = openpyxl.load_workbook(
                io_module.BytesIO(raw),
                data_only=True,
                read_only=False,
            )

            for worksheet in workbook.worksheets:
                lines.append(
                    f"--- SHEET: {worksheet.title} ---"
                )

                sheet_rows = [
                    [
                        cell.value
                        for cell in row
                    ]
                    for row in worksheet.iter_rows()
                ]

                merged_values = {}

                for merged_range in worksheet.merged_cells.ranges:
                    min_col, min_row, max_col, max_row = (
                        merged_range.bounds
                    )

                    top_left = worksheet.cell(
                        row=min_row,
                        column=min_col,
                    ).value

                    if top_left in (None, ""):
                        continue

                    merged_values[(min_row, min_col)] = top_left

                    lines.append(
                        f"M{min_row}:"
                        f"{get_column_letter(min_col)}-"
                        f"{get_column_letter(max_col)}="
                        f"{str(top_left).strip()}"
                    )

                for row_number in range(
                    1,
                    worksheet.max_row + 1,
                ):
                    rendered_cells = []

                    for col_number in range(
                        1,
                        worksheet.max_column + 1,
                    ):
                        cell = worksheet.cell(
                            row=row_number,
                            column=col_number,
                        )

                        value = merged_values.get(
                            (row_number, col_number),
                            cell.value,
                        )

                        if value in (None, ""):
                            continue

                        value_text = str(value).strip()

                        if not value_text:
                            continue

                        number_format = str(
                            cell.number_format
                            or ""
                        )

                        currency_hint = None

                        explicit_currency_patterns = [
                            ("AED", r"\bAED\b|د\.إ"),
                            ("USD", r"\bUSD\b|US\$|\\\$|\$"),
                            ("EUR", r"\bEUR\b|€"),
                            ("GBP", r"\bGBP\b|£"),
                            ("INR", r"\bINR\b|₹"),
                            ("SAR", r"\bSAR\b|ر\.س"),
                            ("QAR", r"\bQAR\b|ر\.ق"),
                            ("KWD", r"\bKWD\b|د\.ك"),
                            ("OMR", r"\bOMR\b|ر\.ع"),
                            ("BHD", r"\bBHD\b|د\.ب"),
                            ("SGD", r"\bSGD\b|S\$"),
                            ("AUD", r"\bAUD\b|A\$|AU\$"),
                            ("CAD", r"\bCAD\b|C\$|CA\$"),
                            ("CNY", r"\bCNY\b|RMB|CN¥"),
                            ("JPY", r"\bJPY\b|JP¥"),
                        ]

                        for code, pattern in (
                            explicit_currency_patterns
                        ):
                            if re.search(
                                pattern,
                                number_format,
                                re.I,
                            ):
                                currency_hint = code
                                break

                        if currency_hint:
                            value_text = (
                                f"{value_text} "
                                f"[CURRENCY={currency_hint}]"
                            )

                        label = header_label_for_cell(
                            sheet_rows,
                            row_number - 1,
                            col_number - 1,
                            value_text,
                        )

                        if label:
                            value_text = f"{label}: {value_text}"
                        elif col_number <= 3:
                            value_text = f"Item descriptor: {value_text}"

                        column_letter = get_column_letter(
                            col_number
                        )

                        rendered_cells.append(
                            f"{column_letter}:{value_text}"
                        )

                    if rendered_cells:
                        lines.append(
                            f"R{row_number}\t"
                            + "\t".join(rendered_cells)
                        )

        result = "\n".join(lines)

        logger.info(
            "Spreadsheet text prepared file=%s "
            "lines=%s chars=%s",
            original_file_name,
            len(lines),
            len(result),
        )

        return result

    def extract_from_file(self, file_field, original_file_name, import_batch=None):
        start = time.monotonic()

        mime_type = (
            mimetypes.guess_type(
                original_file_name
            )[0]
            or "application/octet-stream"
        )

        suffix = str(
            original_file_name or ""
        ).lower()

        is_spreadsheet = suffix.endswith(
            (".xlsx", ".xls", ".csv")
        )

        is_msg = suffix.endswith(".msg")

        msg_meta = {}
        split_pages = None

        # Keep the original bytes available to the shared post-processing
        # path. Spreadsheet extraction has its own parser, but later
        # normalization/reconciliation still expects ``file_bytes`` to be
        # defined (as it is for PDF/image inputs).
        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            file_field.open("rb")
        file_bytes = file_field.read()
        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            pass

        # -------------------------------------------------
        # OUTLOOK MSG
        # Never send raw .msg binary to Gemini.
        # -------------------------------------------------
        if is_msg:
            contents, msg_meta = (
                self._msg_to_gemini_contents(
                    file_field,
                    original_file_name,
                )
            )

            page_count = None

            # Use a descriptive internal MIME value only
            # for logging. No raw MSG part is sent.
            mime_type = "application/vnd.ms-outlook"

            logger.info(
                "Gemini MSG extraction started "
                "batch=%s file=%s parts=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                len(contents),
            )

        elif is_spreadsheet:
            sheet_text = self._spreadsheet_to_text(
                file_field,
                original_file_name,
            )
            spreadsheet_format_currency = (
                self._spreadsheet_currency_from_formats(
                    file_field,
                    original_file_name,
                )
            )
            page_count = None

            logger.info(
                "Gemini spreadsheet extraction started "
                "batch=%s file=%s size=%s",
                getattr(import_batch, "id", None),
                original_file_name,
                len(sheet_text),
            )

            # Large spreadsheets are extracted in chunks so
            # one huge JSON response cannot fail with MAX_TOKENS.
            if len(sheet_text) > 30000:
                result = self._extract_spreadsheet_in_chunks(
                    sheet_text=sheet_text,
                    original_file_name=original_file_name,
                    import_batch=import_batch,
                    chunk_size=40,
                )

                if spreadsheet_format_currency:
                    if not _valid_currency(result.get("currency")):
                        result["currency"] = spreadsheet_format_currency

                    for item in result.get("items") or []:
                        if not _valid_currency(item.get("currency")):
                            item["currency"] = spreadsheet_format_currency

                    result = normalize_result(result)

                return result

            contents = [
                SPREADSHEET_EXTRACTION_PROMPT
                + "\n\n--- SPREADSHEET DATA ---\n\n"
                + sheet_text
            ]
        else:
           
            try:
                file_field.seek(0)
            except (AttributeError, ValueError):
                file_field.open("rb")
            file_bytes = file_field.read()
            try:
                file_field.seek(0)
            except (AttributeError, ValueError):
                pass

            # Do not reject readable screenshots/tables based only on pixel
            # dimensions. Upscale compact images for Gemini while retaining
            # the original bytes for source/reference processing.
            if str(mime_type or "").startswith("image/"):
                gemini_image_bytes, gemini_image_mime = _prepare_image_for_gemini(
                    file_bytes, mime_type
                )
            else:
                gemini_image_bytes, gemini_image_mime = file_bytes, mime_type

            page_count = _estimate_page_count(file_bytes, mime_type)
            estimated_pages = _estimate_page_count(file_bytes, mime_type)
     
            split_pages = (
                _split_pdf_pages(file_bytes)
                if mime_type == "application/pdf"
                and (estimated_pages or 0) > 1
                else None
            )
            # Small multi-page PDFs:
            # one primary page + next page as description context.
            # if split_pages and 1 < len(split_pages) <= 5:
            #     return self._extract_small_pdf_with_page_context(
            #         split_pages=split_pages,
            #         original_file_name=original_file_name,
            #         import_batch=import_batch,
            #     )

            # Large PDFs keep the existing progressive extraction.
            if split_pages and len(split_pages) > 1:
                result = self._extract_large_pdf_progressively(
                    split_pages=split_pages,
                    original_file_name=original_file_name,
                    import_batch=import_batch,
                )

                source_date = _date_matches_filename_year(
                    _extract_source_document_date(file_bytes),
                    original_file_name,
                )

                # Never overwrite a commercial date already
                # extracted by Gemini with a generic first-date scan.
                existing_document_date = _date_matches_filename_year(
                    result.get("document_date"),
                    original_file_name,
                )

                visual_document_date = _date_matches_filename_year(
                    self._verify_visible_document_date(
                        file_bytes=file_bytes,
                        mime_type=mime_type,
                        original_file_name=original_file_name,
                        import_batch=import_batch,
                    ),
                    original_file_name,
                )

                old_document_date = existing_document_date
                final_document_date = (
                    visual_document_date
                    or source_date
                    or existing_document_date
                )

                if final_document_date:
                    result["document_date"] = (
                        final_document_date
                    )
                    logger.warning(
                        "DATE_RECONCILE old_document_date=%r verified_date=%r new_document_date=%r",
                        old_document_date,
                        visual_document_date,
                        result.get("document_date"),
                    )

                    for item in result.get("items") or []:
                        old_item_date = _valid_date(item.get("purchase_date"))
                        if not old_item_date:
                            item["purchase_date"] = final_document_date
                            logger.warning(
                                "DATE_RECONCILE_ITEM product=%r old_purchase_date=%r new_purchase_date=%r",
                                item.get("product_name"),
                                old_item_date,
                                item.get("purchase_date"),
                            )
                else:
                    result["document_date"] = None

                # -----------------------------------------
                # SOURCE CURRENCY RECOVERY
                # For multi-page PDFs before early return.
                # -----------------------------------------
                source_currency = _extract_source_currency(
                    file_bytes
                )

                if source_currency:
                    result["currency"] = source_currency

                    for item in result.get("items") or []:
                        if not _valid_currency(
                            item.get("currency")
                        ):
                            item["currency"] = source_currency

                # Recover supplier before multi-page early return.
                previous_supplier = result.get("supplier_name")
                corrected_supplier = _recover_email_supplier_from_pdf(
                    file_bytes,
                    result.get("supplier_name"),
                )

                if (
                    corrected_supplier
                    and str(corrected_supplier).strip().upper()
                    != "UNKNOWN"
                ):
                    result["supplier_name"] = corrected_supplier

                    for item in result.get("items") or []:
                        if (not _clean_text(item.get("supplier_name"))
                                or item.get("supplier_name") == previous_supplier):
                            item["supplier_name"] = corrected_supplier
                freight_items = _extract_pdf_email_freight_charges(
                    file_bytes,
                    result.get("supplier_name"),
                )
                tariff_items = _extract_pdf_email_tariff_charge_items(
                    file_bytes,
                    result.get("supplier_name"),
                )

                if tariff_items:
                    existing_items = result.get("items") or []
                    logger.warning(
                        "POST_MERGE_TRACE helper=%s before=%s after=%s",
                        "_extract_pdf_email_tariff_charge_items",
                        len(existing_items),
                        len(existing_items) + len(tariff_items),
                    )

                    result = finalize_pdf(result, file_bytes, recovery_items=tariff_items)
                    logger.warning(
                        "POST_MERGE_TRACE helper=%s before=%s after=%s",
                        "tariff_recovery_assignment",
                        len(existing_items),
                        len(result.get("items") or []),
                    )
                    result = normalize_result(result)

                if freight_items:
                    existing_items = result.get("items") or []

                    result["items"] = existing_items + [
                        item for item in freight_items
                        if not _has_equivalent_commercial_item(existing_items, item)
                    ]
                result = _recover_pdf_reference_pc_product_names(
                    file_bytes,
                    result,
                )
                result = _promote_invoice_category_from_content(
                    file_bytes,
                    result,
                )

                verified_category = self._verify_visible_document_category(
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    original_file_name=original_file_name,
                    import_batch=import_batch,
                )

                preferred_category = _prefer_specific_category(
                    result.get("category"), verified_category
                )
                if preferred_category != result.get("category"):
                    result["category"] = preferred_category
                    result["document_type"] = preferred_category.lower()

                    for item in result.get("items") or []:
                        item["source_category"] = verified_category
                        item["source_document_type"] = verified_category.lower()

                result = _remove_summary_total_rows(result)
                result = normalize_result(result)

                for item in result.get("items") or []:
                    corrected_item_date = _date_matches_filename_year(
                        item.get("purchase_date"),
                        original_file_name,
                    )

                    if corrected_item_date:
                        item["purchase_date"] = corrected_item_date

                preferred_category = _prefer_specific_category(
                    result.get("category"), verified_category
                )
                if preferred_category != result.get("category"):
                    result["category"] = preferred_category
                    result["document_type"] = preferred_category.lower()

                    for item in result.get("items") or []:
                        item["source_category"] = verified_category
                        item["source_document_type"] = verified_category.lower()

                result = normalize_result(restore_table_columns(result, file_bytes))
                result = _recover_pdf_visible_table_columns(file_bytes, result)
                section_contexts = {
                    (_supplier_identity_key(i.get("supplier_name")), i.get("purchase_date"))
                    for i in result.get("items") or []
                }
                if len(section_contexts) > 1:
                    # A bundle has section totals/dates, not one shared header.
                    for field in ("supplier_name", "document_date", "document_number", "subtotal", "grand_total"):
                        result[field] = None
                result = _finalize_pdf_extraction_result(result, file_bytes)
                return required_metadata(result)
            if split_pages and len(split_pages) > 1:
                # Send each page as its own explicitly-labeled part instead of one
                # big multi-page blob. This forces the model to see page
                # boundaries directly rather than relying on it to infer them
                # from a text instruction, which proved unreliable for bundles
                # containing several unrelated invoices/receipts/gate passes.
                contents = [
                    GEMINI_EXTRACTION_PROMPT
                    + f"\n\nThis document has {len(split_pages)} pages, supplied below with PAGE markers. "
                    f"Read all pages as one continuous commercial document unless a clearly new "
                    f"quotation/invoice/PO/document starts on a later page. "
                    f"For multi-page item tables, preserve row sequence across page boundaries. "
                    f"A description appearing at the top of a page may be the continuation of the "
                    f"last priced row from the previous page. Attach that continuation only to that "
                    f"previous row, up to the point where the next numbered/item-code priced row begins. "
                    f"Never attach a continuation block to the next row merely because it is on the same page. "
                    f"Do not skip any page and never invent amounts."
                  
                ]
                for index, page_bytes in enumerate(split_pages, start=1):
                    contents.append(f"--- PAGE {index} OF {len(split_pages)} ---")
                    contents.append(types.Part.from_bytes(data=page_bytes, mime_type="application/pdf"))
                contents.append(
                    f"Reminder: extract items from ALL {len(split_pages)} pages shown above. "
                    f"Before returning JSON, verify each item's description against its own "
                    f"S/N/item-code/part-number/quantity/rate row. For a description crossing a "
                    f"page boundary, keep it attached to the priced row that started before the "
                    f"continuation. Return one combined items array."
                )
            else:
                page_note = (
                    f"THIS DOCUMENT HAS EXACTLY {page_count} PAGES. Go through them ONE BY ONE, "
                    f"page 1 to page {page_count}. Bundled scans commonly contain MULTIPLE unrelated "
                    f"invoices/receipts, one per page or every few pages, each with its own document "
                    f"number and date. Extract items from EVERY page independently. Never invent an "
                    f"amount that is not clearly visible."
                    if page_count else ""
                )
          
                contents = [GEMINI_EXTRACTION_PROMPT]
                if page_note:
                    contents.append(page_note)
                contents.append(
                    types.Part.from_bytes(
                        data=gemini_image_bytes,
                        mime_type=gemini_image_mime,
                    )
                )
            logger.info(
                "Gemini extraction started batch=%s mime=%s size=%s pages=%s split=%s",
                getattr(import_batch, "id", None), mime_type, len(file_bytes), page_count,
                bool(split_pages and len(split_pages) > 1),
            )
   
  
        try:
            response = self._generate_content_with_retry(
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=GEMINI_RESPONSE_SCHEMA,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=1024
                    ),
                ),
            )

        except Exception as exc:
            error_text = str(exc).upper()

            is_timeout = any(
                token in error_text
                for token in (
                    "DEADLINE_EXCEEDED",
                    "504",
                    "TIMEOUT",
                    "TIMED OUT",
                    "READTIMEOUT",
                )
            )

            if is_spreadsheet and is_timeout:
                logger.warning(
                    "Spreadsheet main extraction timed out. "
                    "Retrying in smaller chunks file=%s error=%s",
                    original_file_name,
                    exc,
                )

                return self._extract_spreadsheet_in_chunks(
                    sheet_text=sheet_text,
                    original_file_name=original_file_name,
                    import_batch=import_batch,
                    chunk_size=30,
                )

            raise

        try:
            result = normalize_result(
                _safe_json(response.text)
            )
            if is_spreadsheet and spreadsheet_format_currency:
                if not _valid_currency(result.get("currency")):
                    result["currency"] = spreadsheet_format_currency

                for item in result.get("items") or []:
                    if not _valid_currency(item.get("currency")):
                        item["currency"] = spreadsheet_format_currency

                result = _recover_pdf_reference_pc_product_names(
                    file_bytes,
                    result,
                )
                result = _promote_invoice_category_from_content(
                    file_bytes,
                    result,
                )
                result = _remove_summary_total_rows(result)
                result = normalize_result(result)

        except json.JSONDecodeError as exc:
            finish_reason = None

            try:
                finish_reason = str(
                    response.candidates[0].finish_reason
                )
            except (
                AttributeError,
                IndexError,
                TypeError,
            ):
                pass

            if (
                is_spreadsheet
                and "MAX_TOKENS" in str(
                    finish_reason
                ).upper()
            ):
                logger.warning(
                    "Spreadsheet main extraction hit MAX_TOKENS. "
                    "Retrying in chunks file=%s",
                    original_file_name,
                )

                return self._extract_spreadsheet_in_chunks(
                    sheet_text=sheet_text,
                    original_file_name=original_file_name,
                    import_batch=import_batch,
                    chunk_size=40,
                )

            logger.error(
                "Gemini main extraction returned invalid/truncated JSON "
                "file=%s chars=%s finish_reason=%s error=%s tail=%s",
                original_file_name,
                len(response.text or ""),
                finish_reason,
                exc,
                (response.text or "")[-1500:],
            )

            raise ValueError(
                "Gemini returned an incomplete extraction response. "
                "Please retry the file upload."
            ) from exc
        if mime_type == "application/pdf" and not is_spreadsheet:
            source_date = _extract_source_document_date(
                file_bytes
            )

            existing_document_date = _valid_date(
                    result.get("document_date")
            )

            final_document_date = (
                    source_date
                    or existing_document_date
            )

            if final_document_date:
                result["document_date"] = final_document_date

                for item in result.get("items") or []:
                    if not _valid_date(
                        item.get("purchase_date")
                    ):
                        item["purchase_date"] = (
                            final_document_date
                        )
            else:
                result["document_date"] = None

            # -------------------------------------------------
            # SOURCE CURRENCY RECOVERY
            # Read only explicit currency visible in PDF text.
            # Never infer from country/supplier/default settings.
            # -------------------------------------------------
            source_currency = _extract_source_currency(
                file_bytes
            )

            if source_currency:
                result["currency"] = source_currency

                for item in result.get("items") or []:
                    if not _valid_currency(
                        item.get("currency")
                    ):
                        item["currency"] = source_currency
        if mime_type == "application/pdf" and not is_spreadsheet:
            # ---------------------------------------------------------
            # EMAIL SUPPLIER RECOVERY
            # Prevent To/recipient name from becoming supplier.
            # ---------------------------------------------------------
            previous_supplier = result.get("supplier_name")
            corrected_supplier = _recover_email_supplier_from_pdf(
                file_bytes,
                result.get("supplier_name"),
            )

            if corrected_supplier:
                result["supplier_name"] = corrected_supplier

                for item in result.get("items") or []:
                    if (not _clean_text(item.get("supplier_name"))
                            or item.get("supplier_name") == previous_supplier):
                        item["supplier_name"] = corrected_supplier
            result = _recover_pdf_reference_pc_product_names(
                file_bytes,
                result,
            )
            result = _promote_invoice_category_from_content(
                file_bytes,
                result,
            )
            result = _remove_summary_total_rows(result)
            result = normalize_result(result)

            freight_items = _extract_pdf_email_freight_charges(
                file_bytes,
                result.get("supplier_name"),
            )
            tariff_items = _extract_pdf_email_tariff_charge_items(
                    file_bytes,
                    result.get("supplier_name"),
            )

            if tariff_items:
                existing_items = result.get("items") or []
                logger.warning(
                    "POST_MERGE_TRACE helper=%s before=%s after=%s",
                    "_extract_pdf_email_tariff_charge_items",
                    len(existing_items),
                    len(existing_items) + len(tariff_items),
                )

                result = finalize_pdf(result, file_bytes, recovery_items=tariff_items)
                logger.warning(
                    "POST_MERGE_TRACE helper=%s before=%s after=%s",
                    "tariff_recovery_assignment",
                    len(existing_items),
                    len(result.get("items") or []),
                )
                result = normalize_result(result)

            if freight_items:
                existing_items = result.get("items") or []

                result["items"] = existing_items + [
                    item for item in freight_items
                    if not _has_equivalent_commercial_item(existing_items, item)
                ]
                result = normalize_result(result)

        if (
            not is_spreadsheet
            and not is_msg
            and (
                str(mime_type or "").startswith("image/")
                or mime_type == "application/pdf"
            )
        ):
            verified_document_date = None

            if mime_type == "application/pdf":
                verified_document_date = _date_matches_filename_year(
                    _extract_source_document_date(file_bytes),
                    original_file_name,
                )

            if not verified_document_date:
                verified_document_date = _date_matches_filename_year(
                    self._verify_visible_document_date(
                        file_bytes=file_bytes,
                        mime_type=mime_type,
                        original_file_name=original_file_name,
                        import_batch=import_batch,
                    ),
                    original_file_name,
                )
            verified_category = self._verify_visible_document_category(
                file_bytes=file_bytes,
                mime_type=mime_type,
                original_file_name=original_file_name,
                import_batch=import_batch,
            )

            preferred_category = _prefer_specific_category(
                result.get("category"), verified_category
            )
            if preferred_category != result.get("category"):
                result["category"] = preferred_category
                result["document_type"] = preferred_category.lower()

                for item in result.get("items") or []:
                    item["source_category"] = verified_category
                    item["source_document_type"] = verified_category.lower()

            if verified_document_date:
                current_document_date = _valid_date(
                    result.get("document_date")
                )

                if current_document_date != verified_document_date:
                    logger.warning(
                        "Correcting Gemini document date file=%s from=%s to=%s",
                        original_file_name,
                        current_document_date,
                        verified_document_date,
                    )

                result["document_date"] = verified_document_date
                logger.warning(
                    "DATE_RECONCILE old_document_date=%r verified_date=%r new_document_date=%r",
                    current_document_date,
                    verified_document_date,
                    result.get("document_date"),
                )

                for item in result.get("items") or []:
                    old_item_date = _valid_date(item.get("purchase_date"))
                    if old_item_date:
                        continue
                    item["purchase_date"] = verified_document_date
                    logger.warning(
                        "DATE_RECONCILE_ITEM product=%r old_purchase_date=%r new_purchase_date=%r",
                        item.get("product_name"),
                        old_item_date,
                        item.get("purchase_date"),
                    )

                result = normalize_result(result)

                preferred_category = _prefer_specific_category(
                    result.get("category"), verified_category
                )
                if preferred_category != result.get("category"):
                    result["category"] = preferred_category
                    result["document_type"] = preferred_category.lower()

                    for item in result.get("items") or []:
                        item["source_category"] = verified_category
                        item["source_document_type"] = verified_category.lower()
        if is_msg:
            history = None
            try:
                from api.services.msg_commercial_history import extract_agreed_rate_history, extract_labelled_table_history
                agreed_history = extract_agreed_rate_history(
                    msg_meta.get("body"), msg_meta.get("sender"), msg_meta.get("sent_date"))
                inline_history = extract_inline_quote_history(
                    msg_meta.get("body"), msg_meta.get("sender"), msg_meta.get("sent_date"))
                table_history = extract_labelled_table_history(
                    msg_meta.get("body"), msg_meta.get("sender"), msg_meta.get("sent_date"))
                if table_history:
                    if agreed_history:
                        agreed_history['items'].extend(table_history['items'])
                        agreed_history['_msg_commercial_blocks'].extend(table_history['_msg_commercial_blocks'])
                    else:
                        agreed_history = table_history
                if agreed_history:
                    history = reconcile_inline_quote_history(result, agreed_history, source_body=msg_meta.get('body'))
                elif inline_history:
                    history = (reconcile_inline_quote_history(result, inline_history)
                               if msg_meta.get("has_commercial_attachments") else inline_history)
                elif not msg_meta.get("has_commercial_attachments"):
                    history = extract_msg_history(msg_meta.get("body"), msg_meta.get("sender"), msg_meta.get("sent_date"))
            except Exception:
                logger.exception("MSG source history parsing failed file=%s", original_file_name)
            if history is not None:
                primary_count = len(result.get("items") or [])
                result.update(history)
                result["_msg_primary_item_count"] = primary_count
                result = normalize_result(result)
                logger.info(
                    "MSG source history complete file=%s primary_items=%s final_items=%s blocks=%s",
                    original_file_name, primary_count, len(result["items"]),
                    len(result["_msg_commercial_blocks"]),
                )
            else:
                msg_body = str(
                    msg_meta.get("body")
                    or ""
                )
    
                current_supplier = _clean_text(
                    result.get("supplier_name")
                )
    
                signature_supplier = _clean_text(
                    msg_meta.get("signature_company")
                )
    
                sender_supplier = _clean_text(
                    msg_meta.get("sender_company")
                )
    
                # -------------------------------------------------
                # SUPPLIER
                #
                # 1. Company explicitly found in sender's email body
                # 2. Gemini supplier
                # 3. Sender email-domain company fallback
                # -------------------------------------------------
                corrected_supplier = None
    
                if signature_supplier:
                    corrected_supplier = signature_supplier
    
                elif (
                    current_supplier
                    and current_supplier.upper() != "UNKNOWN"
                ):
                    corrected_supplier = current_supplier
    
                elif sender_supplier:
                    corrected_supplier = sender_supplier
    
                # Signatures may contain a person's title before the seller
                # company (for example "Operational Supervisor / COMPANY").
                if corrected_supplier and "/" in corrected_supplier:
                    suffix = corrected_supplier.rsplit("/", 1)[-1].strip()
                    if suffix and re.search(r"\b(?:PTE|LLC|L\.L\.C|LTD|LIMITED|CO\.?|CORP)\b", suffix, re.I):
                        corrected_supplier = suffix
    
                if corrected_supplier:
                    result["supplier_name"] = (
                        corrected_supplier
                    )
    
                    for item in result.get("items") or []:
                        item["supplier_name"] = (
                            corrected_supplier
                        )
                # Collect independently priced blocks from the complete thread.
                # The newest block must not replace historical supplier quotations.
                table_items = []
                msg_blocks = _split_msg_commercial_blocks(msg_body)
                for msg_block in msg_blocks:
                    block_date = _msg_block_date(msg_block, result.get("document_date"))
                    block_items = _extract_msg_priced_table_items(
                        msg_block, corrected_supplier, block_date,
                    )
                    if not block_items:
                        block_items = _extract_msg_inline_price_items(
                            msg_block, corrected_supplier, block_date,
                        )
                    table_items.extend(block_items)
                logger.info(
                    "MSG commercial blocks file=%s blocks=%s recovered_items=%s",
                    original_file_name, len(msg_blocks), len(table_items),
                )
    
                msg_table_items_used = False
    
                if table_items and not result.get("items"):
                    # Local recovery supplements Gemini rows; it must not discard
                    # historical rows already recovered above.
                    result["items"] = list(result.get("items") or []) + table_items
                    msg_table_items_used = True
    
                    if table_items[0].get("currency"):
                        result["currency"] = table_items[0]["currency"]
                # -------------------------------------------------
                # PRODUCT NAME + PRICE
                # Validate against original email source.
                # -------------------------------------------------
                for item in result.get("items") or []:
                    row_type = str(
                        item.get("row_type")
                        or ""
                    ).upper()
    
                    if row_type == "PRODUCT":
                        recovered_name = (
                            _recover_msg_product_name(
                                item.get("product_name"),
                                msg_body,
                                item.get("item_code"),
                                item.get("part_number"),
                                unit_price=item.get("unit_price"),
                                source_item=item,
                            )
                        )
    
                        if recovered_name:
                            item["product_name"] = (
                                recovered_name
                            )
                            if recovered_name != item.get("display_name"):
                                item["display_name"] = recovered_name
                            # A recovered numbered block is one description,
                            # including technical prose, not a set of columns.
                            if re.search(r'\bprice\s*:', msg_body, re.I) and recovered_name in re.sub(r'\s+', ' ', msg_body):
                                item["extra_attributes"] = []
    
                    old_unit_price = _decimal(
                        item.get("unit_price")
                    )
    
                    old_line_total = _decimal(
                        item.get("line_total")
                    )
    
                    corrected_price_raw = (
                        _recover_msg_decimal_price(
                            item.get("unit_price"),
                            msg_body,
                            item.get("currency")
                            or result.get("currency"),
                        )
                    )
    
                    corrected_price = _decimal(
                        corrected_price_raw
                    )
    
                    if (
                        corrected_price is not None
                        and old_unit_price is not None
                        and corrected_price != old_unit_price
                    ):
                        item["unit_price"] = str(
                            corrected_price
                        )
    
                        quantity = _decimal(
                            item.get("quantity")
                        )
    
                        # Correct total only when the existing total
                        # clearly has the same decimal-loss error.
                        if (
                            quantity is not None
                            and old_line_total is not None
                        ):
                            correct_total = (
                                quantity
                                * corrected_price
                            )
    
                            wrong_total = (
                                quantity
                                * old_unit_price
                            )
    
                            tolerance = max(
                                Decimal("0.0001"),
                                abs(wrong_total)
                                * Decimal("0.0001"),
                            )
    
                            if (
                                abs(
                                    old_line_total
                                    - wrong_total
                                )
                                <= tolerance
                            ):
                                item["line_total"] = str(
                                    correct_total
                                )
    
                # Run existing normalization once more after
                # deterministic MSG corrections.
                result = normalize_result(result)

                # Re-resolve after history/recovery so disclaimer text cannot
                # remain as the document supplier.
                result = _resolve_msg_supplier(result, msg_meta)

                logger.info(
                    "Gemini MSG extraction completed "
                    "file=%s supplier=%s date=%s "
                    "currency=%s items=%s",
                    original_file_name,
                    result.get("supplier_name"),
                    result.get("document_date"),
                    result.get("currency"),
                    len(result.get("items") or []),
                )
        logger.warning(
            "DEBUG MAIN GEMINI ITEMS count=%s items=%s",
            len(result.get("items") or []),
            json.dumps(result.get("items") or [], default=str),
        )

        item_count = len(result.get("items") or [])
        elapsed_ms = int((time.monotonic() - start) * 1000)
        finish_reason = None
        try:
            finish_reason = str(response.candidates[0].finish_reason)
        except (AttributeError, IndexError, TypeError):
            pass
        logger.info(
            "Gemini extraction finished batch=%s document_type=%s items=%s elapsed_ms=%s finish_reason=%s pages=%s",
            getattr(import_batch, "id", None),
            result.get("document_type"),
            item_count,
            elapsed_ms,
            finish_reason,
            page_count,
        )
        logger.warning(
            "GEMINI FINAL NORMALIZED OUTPUT file=%s output=%s",
            original_file_name,
            json.dumps(result, default=str, ensure_ascii=False, indent=2),
        )

        needs_review = False
        missing_pages = []
        if split_pages and page_count and page_count > 1:
            covered_pages = {
                item.get("page_number")
                for item in result.get("items") or []
                if item.get("page_number") not in (None, "")
            }
            missing_pages = [page for page in range(1, page_count + 1) if page not in covered_pages]

            # One automatic retry, targeted only at pages that produced nothing.
            # This recovers pages the model silently skipped (e.g. the gate-pass
            # page in a bundle) without re-sending the whole document again.
            if missing_pages:
                logger.warning(
                    "Gemini extraction missing pages batch=%s missing=%s — retrying those pages only",
                    getattr(import_batch, "id", None), missing_pages,
                )
                retry_contents = [
                    GEMINI_EXTRACTION_PROMPT
                    + f"\n\nMISSING PAGE RECOVERY. "
                    f"Only original pages {missing_pages} are supplied below. "

                    f"Read every supplied page completely from top to bottom. "
                    f"A supplied page may be a completely NEW quotation/invoice/PO/document, "
                    f"not a continuation of the previous page. "

                    f"If the page has its own document heading, document number, date and priced table, "
                    f"treat it as a separate commercial document section and extract ALL priced rows. "

                    f"Do not stop after the first few rows. "
                    f"Count all visibly numbered/priced rows before returning JSON. "
                    f"Return one item for every separately printed priced row. "

                    f"For each item preserve that page's own supplier, date, document number, "
                    f"category, currency, quantity, UOM, unit price and row amount. "

                    f"Set page_number to the ORIGINAL page number shown in "
                    f"'--- PAGE N OF {page_count} ---'."
                ]
                for page in missing_pages:
                    retry_contents.append(f"--- PAGE {page} OF {page_count} ---")
                    retry_contents.append(types.Part.from_bytes(data=split_pages[page - 1], mime_type="application/pdf"))

                try:
                    retry_response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=retry_contents,
                        config=types.GenerateContentConfig(
                            temperature=0,
                            max_output_tokens=16384,
                            response_mime_type="application/json",
                            response_schema=GEMINI_RESPONSE_SCHEMA,
                            thinking_config=types.ThinkingConfig(thinking_budget=1024),
                        ),
                    )
                    logger.warning(
                        "DEBUG MISSING PAGE RAW RESPONSE chars=%s text=%s",
                        len(retry_response.text or ""),
                        retry_response.text,
                    )
                    retry_result = normalize_result(_safe_json(retry_response.text))
                    retry_items = retry_result.get("items") or []
                    result["items"] = list(result.get("items") or []) + retry_items
                    item_count = len(result["items"])
                    self._log_usage(retry_response, import_batch, "missing_page_retry", {
                        "file_name": original_file_name,
                        "missing_pages": missing_pages,
                        "recovered_items": len(retry_items),
                    })
                    recovered_pages = {
                        item.get("page_number") for item in retry_items
                        if item.get("page_number") not in (None, "")
                    }
                    missing_pages = [page for page in missing_pages if page not in recovered_pages]
                except Exception as exc:
                    logger.warning(
                        "Gemini missing-page retry failed batch=%s error=%s",
                        getattr(import_batch, "id", None), exc,
                    )

            needs_review = bool(missing_pages)

        result["_gemini_needs_review"] = needs_review
        result["_gemini_missing_pages"] = missing_pages
        logger.warning(
            "GEMINI FINAL EXTRACTION file=%s needs_review=%s missing_pages=%s item_count=%s output=%s",
            original_file_name,
            result.get("_gemini_needs_review"),
            result.get("_gemini_missing_pages"),
            len(result.get("items") or []),
            json.dumps(result, default=str, ensure_ascii=False, indent=2),
        )
        quality_issues = _extraction_quality_issues(result)

        if quality_issues:
            needs_review = True
            result["_gemini_needs_review"] = True
            result["_gemini_quality_issues"] = quality_issues
        else:
            needs_review = bool(missing_pages)
            result["_gemini_needs_review"] = needs_review
            result["_gemini_quality_issues"] = []

        if needs_review:
            logger.warning(
                "Gemini extraction still incomplete after retry batch=%s missing_pages=%s — "
                "flagged for review",
                getattr(import_batch, "id", None), missing_pages,
            )

        self._log_usage(response, import_batch, "file_extraction", {
            "file_name": original_file_name,
            "mime_type": mime_type,
            "elapsed_ms": elapsed_ms,
            "item_count": item_count,
            "document_type": result.get("document_type"),
            "page_count": page_count,
            "needs_review": needs_review,
            "missing_pages": missing_pages,
        })

        # Repair only suspicious multi-page PRODUCT descriptions.
        # Commercial values from the main extraction remain untouched.
        if (
            mime_type == "application/pdf"
            and split_pages
            and len(split_pages) > 1
        ):
            # First repair wrongly-bound/incomplete descriptions.
            result = self._repair_suspicious_product_descriptions(
                result=result,
                split_pages=split_pages,
                original_file_name=original_file_name,
                import_batch=import_batch,
            )
            logger.warning(
                "DEBUG AFTER DESCRIPTION REPAIR count=%s items=%s",
                len(result.get("items") or []),
                json.dumps(result.get("items") or [], default=str),
            )
            result = self._reconcile_orphan_continuation_products(result)

            logger.warning(
                "DEBUG AFTER ORPHAN RECONCILE count=%s items=%s",
                len(result.get("items") or []),
                json.dumps(result.get("items") or [], default=str),
            )

            # Then verify prices only when the same product code/part number
            # appears multiple times with conflicting extracted prices.
            result = self._verify_repeated_product_prices(
                result=result,
                split_pages=split_pages,
                original_file_name=original_file_name,
                import_batch=import_batch,
            )

            logger.warning(
                "DEBUG AFTER PRICE VERIFY count=%s items=%s",
                len(result.get("items") or []),
                json.dumps(result.get("items") or [], default=str),
            )

        if mime_type == "application/pdf":
            result = restore_table_columns(result, file_bytes)
            result = _recover_pdf_visible_table_columns(file_bytes, result)
            result = _recover_pdf_full_description_cells(file_bytes, result)
            result = finalize_pdf(result, file_bytes)
        if is_msg:
            result = finalize_msg_table(result, msg_meta.get("body"), msg_meta.get("sent_date"))
        result = _reconcile_placeholder_commercial_items(result, original_file_name)
        result = self._normalize_multilingual_product_names(result, import_batch)
        result = normalize_result(result)
        if mime_type == "application/pdf":
            result = _finalize_pdf_extraction_result(result, file_bytes)

        result = _finalize_email_quote_metadata(result)

        if is_msg and msg_meta.get('source_pdf_attachments'):
            from api.services.msg_attachment_recovery import recover_attachments
            result = recover_attachments(
                result,
                msg_meta['source_pdf_attachments'],
                fallback_supplier=(msg_meta.get('signature_company') or msg_meta.get('sender_company')),
                fallback_date=msg_meta.get('sent_date'),
            )

        if is_msg:
            from api.services.msg_commercial_history import restore_offer_context
            result = restore_offer_context(result, msg_meta.get('body'), msg_meta.get('sender'), msg_meta.get('sent_date'))
            from api.services.msg_price_table import restore_price_tables
            result = restore_price_tables(result, msg_meta.get('html_body'), msg_meta.get('body'),
                                          msg_meta.get('sender'), msg_meta.get('sent_date'))
            from api.services.alternative_currency_quotes import collapse_currency_equivalents
            result = collapse_currency_equivalents(result, msg_meta.get('body'))
            from api.services.msg_commercial_history import finalize_physical_freight_offers
            result = finalize_physical_freight_offers(result, msg_meta.get('body'),
                                                      msg_meta.get('sender'), msg_meta.get('sent_date'))
            from api.services.email_rate_evidence import restore_rates
            result = restore_rates(result, msg_meta.get('body'), msg_meta.get('sender'), msg_meta.get('sent_date'))
            from api.services.word_description_evidence import restore_descriptions
            result = restore_descriptions(result, msg_meta.get('source_word_texts') or [])
            for item in result.get('items') or []:
                item['display_name'] = build_canonical_display_name(item)
        if str(mime_type or '').startswith('image/'):
            from api.services.currency_evidence_policy import require_image_currency_evidence
            result = require_image_currency_evidence(result)
        return required_metadata(result)

    def _normalize_multilingual_product_names(self, result, import_batch=None):
        items = list(result.get("items") or [])
        candidates = []
        for index, item in enumerate(items):
            name = str(item.get("product_name") or "").strip()
            if (
                str(item.get("row_type") or "").upper() == "PRODUCT"
                and name
                and _contains_cjk(name)
            ):
                candidates.append({"index": index, "product_name": name})

        if not candidates:
            return result

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[MULTILINGUAL_PRODUCT_NORMALIZATION_PROMPT, json.dumps({"items": candidates}, ensure_ascii=False)],
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            )
            payload = _safe_json(response.text)
        except Exception:
            logger.exception("Multilingual product-name normalization failed")
            return result

        for correction in payload.get("items") or []:
            try:
                index = int(correction.get("index"))
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= len(items):
                continue
            english = str(correction.get("english_product_name") or "").strip()
            original = str(correction.get("original_product_name") or items[index].get("product_name") or "").strip()
            # The source field may include a specification block after the
            # human-readable identity. Preserve only that identity as the
            # original-name audit attribute; specifications remain source
            # metadata and must not pollute display_name.
            original = re.split(r"\s*\|\s*", original, maxsplit=1)[0].strip()
            if not english or _contains_cjk(english) or not original:
                continue
            attrs = _normalize_extra_attributes(items[index].get("extra_attributes"))
            if not any(
                str(attr.get("label") or "").strip().lower() == "original product name"
                for attr in attrs
            ):
                attrs.append({"label": "Original Product Name", "value": original})
            items[index]["product_name"] = english
            items[index]["extra_attributes"] = attrs

        self._log_usage(response, import_batch, "multilingual_product_normalization", {
            "candidate_count": len(candidates),
            "translated_count": sum(
                1 for item in items
                if item.get("product_name") and not _contains_cjk(item.get("product_name"))
            ),
        })
        result["items"] = items
        return result

    def verify_suspicious_items(self, result, suspicious_items, import_batch=None, context=None):
        if not suspicious_items:
            return result
        limited = suspicious_items[:10]
        payload = {
            "document": {
                "document_type": result.get("document_type"),
                "supplier_name": result.get("supplier_name"),
                "document_date": result.get("document_date"),
                "currency": result.get("currency"),
            },
            "suspicious_items": limited,
            "context": context or {},
        }
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[
                GEMINI_VERIFICATION_PROMPT,
                json.dumps(payload, default=str),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=2048,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        verification = _safe_json(response.text)
        merged = {**result, "items": list(result.get("items") or [])}
        for correction in verification.get("items") or []:
            try:
                index = int(correction.get("index"))
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= len(merged["items"]):
                continue
            corrected_fields = correction.get("corrected_fields") or {}
            allowed = {
                "item_type",
                "row_type",
                "product_service_name",
                "product_name",
                "item_code",
                "part_number",
                "brand",
                "quantity",
                "uom",
                "unit_of_measurement",
                "unit_price",
                "currency",
                "total",
                "line_total",
                "purchase_date",
                "source_reference",
                "document_number",

                # Per-item commercial context
                "supplier_name",
                "buyer_name",
                "source_document_type",
                "source_document_number",
            }
            merged["items"][index] = {
                **merged["items"][index],
                **{key: value for key, value in corrected_fields.items() if key in allowed},
            }
        self._log_usage(response, import_batch, "targeted_verification", {
            "suspicious_item_count": len(suspicious_items),
            "verified_item_count": len(verification.get("items") or []),
        })
        return normalize_result(merged)

    def _log_usage(self, response, import_batch, task_name, request_metadata):
        usage = getattr(response, "usage_metadata", None)
        GeminiUsageLog.objects.create(
            import_batch=import_batch,
            model_name=self.model_name,
            task_name=task_name,
            prompt_tokens=_usage_value(usage, "prompt_token_count", "input_token_count"),
            output_tokens=_usage_value(usage, "candidates_token_count", "output_token_count"),
            total_tokens=_usage_value(usage, "total_token_count"),
            request_metadata=request_metadata,
            response_metadata={"usage": usage.model_dump() if hasattr(usage, "model_dump") else {}},
        )
