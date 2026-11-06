"""Conservative equivalence for extraction repeats, not product matching."""
import re
from decimal import Decimal, InvalidOperation


def _text(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().casefold()


def _number(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _commercial_name(value):
    text = _text(value)
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"\s*\(\s*(?:lumpsum|lump\s*sum|after\s+\d+\s*hrs?|per\s+hr)\s*\)", " ", text)
    text = re.sub(r"\s*-\s*(?:per\s+hr|after\s+\d+\s*hrs?)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:.,")
    return text


def is_true_duplicate(left, right):
    """Require equal commercial data AND a shared original-row locator.

    Missing provenance is not proof of duplication. Page alone, identical
    prices, similar names and recovery flags cannot establish equivalence.
    Callers operate within one document; source row IDs refer to original
    rows, not the page/pass on which a retry happened to extract them.
    """
    if not _text(left.get("supplier_name")) or _text(left.get("supplier_name")) != _text(right.get("supplier_name")):
        return False
    if not _text(left.get("currency")) or _text(left.get("currency")) != _text(right.get("currency")):
        return False
    if not _text(left.get("product_name")) or _text(left.get("product_name")) != _text(right.get("product_name")):
        return False
    for field in ("row_type", "purchase_date", "commercial_block_date", "uom",
                  "document_date", "source_document_number", "document_number",
                  "item_code", "part_number", "rate_basis", "duration",
                  "pricing_condition"):
        if _text(left.get(field)) != _text(right.get(field)):
            return False
    for field in ("quantity", "unit_price", "line_total"):
        a, b = left.get(field), right.get(field)
        if (a in (None, "")) != (b in (None, "")):
            return False
        if a not in (None, "") and (_number(a) is None or _number(a) != _number(b)):
            return False
    if _number(left.get("unit_price")) is None and _number(left.get("line_total")) is None:
        return False

    def attributes(row):
        attrs = row.get("extra_attributes") or []
        if isinstance(attrs, dict):
            attrs = [{"label": key, "value": value} for key, value in attrs.items()]
        return sorted((_text(a.get("label")), _text(a.get("value"))) for a in attrs if isinstance(a, dict))

    if attributes(left) != attributes(right):
        return False
    # Explicit distinctions always win, including line/row zero.
    locators = ("page_number", "source_page", "source_block", "source_block_id",
                "block_id", "email_block_id", "source_row_id", "source_row",
                "source_row_number", "row_number", "source_line", "source_reference",
                "source_position", "source_context")
    for field in locators:
        if _text(left.get(field)) != _text(right.get(field)):
            return False
    if _text(left.get("source_row_id")):
        return True
    has_scope = any(_text(left.get(f)) for f in (
        "page_number", "source_page", "source_block", "source_block_id", "block_id", "email_block_id"))
    has_row = any(_text(left.get(f)) for f in (
        "source_row", "source_row_number", "row_number", "source_line", "source_position"))
    # A free-text reference may name a document rather than a row.
    row_reference = re.search(r"\b(?:row|line)\s*[:#-]?\s*\d+\b", _text(left.get("source_reference")))
    return bool(has_scope and (has_row or row_reference))
