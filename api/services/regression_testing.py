import json
import re
from decimal import Decimal, InvalidOperation

DOCUMENT_FIELDS = (
    "supplier_name", "document_date", "document_number", "category",
    "currency", "subtotal", "grand_total", "review_status",
)
ITEM_FIELDS = (
    "row_type", "product_name", "display_name", "purchase_date",
    "quantity", "uom", "unit_price", "line_total", "currency",
    "supplier_name", "source_document_number",
)


def _number(value):
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _product_identity(value):
    text = _text(value)
    text = text.replace("d,o", "d.o")
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"[,.;]+", lambda m: "," if "," in m.group() else "", text)
    return re.sub(r"\s*([,:])\s*", r"\1", text)


def _equal(field, actual, expected):
    if field in {"product_name", "display_name"}:
        return _product_identity(actual) == _product_identity(expected)
    if field in {"quantity", "unit_price", "line_total", "subtotal", "grand_total"}:
        left, right = _number(actual), _number(expected)
        return left == right if left is not None and right is not None else left == right
    return _text(actual) == _text(expected)


def _item_score(actual, expected):
    names = {_text(actual.get("product_name")), _text(actual.get("display_name"))}
    expected_name = _product_identity(expected.get("product_name") or expected.get("display_name"))
    actual_names = {_product_identity(actual.get("product_name")), _product_identity(actual.get("display_name"))}
    name_score = 1.0 if expected_name in actual_names else 0.0
    price_score = 1.0 if _equal("unit_price", actual.get("unit_price"), expected.get("unit_price")) else 0.0
    code_score = 1.0 if _text(actual.get("item_code")) and _text(actual.get("item_code")) == _text(expected.get("item_code")) else 0.0
    return name_score * 0.6 + price_score * 0.3 + code_score * 0.1


def compare_results(actual, expected):
    actual, expected = actual or {}, expected or {}
    differences = []
    for field in DOCUMENT_FIELDS:
        if field == "review_status":
            actual_value = actual.get("_gemini_needs_review", actual.get("review_status"))
            expected_value = expected.get("_gemini_needs_review", expected.get("review_status"))
        else:
            actual_value, expected_value = actual.get(field), expected.get(field)
        if not _equal(field, actual_value, expected_value):
            differences.append({"scope": "document", "field": field, "actual": actual_value, "expected": expected_value})

    actual_items, expected_items = list(actual.get("items") or []), list(expected.get("items") or [])
    unmatched = set(range(len(actual_items)))
    pairs = []
    for expected_item in expected_items:
        candidates = [( _item_score(actual_items[index], expected_item), index) for index in unmatched]
        if not candidates:
            differences.append({"scope": "item", "kind": "missing_item", "expected": expected_item})
            continue
        score, index = max(candidates)
        if score < 0.35:
            differences.append({"scope": "item", "kind": "missing_item", "expected": expected_item})
            continue
        unmatched.remove(index)
        pairs.append((actual_items[index], expected_item))
    for index in sorted(unmatched):
        differences.append({"scope": "item", "kind": "unexpected_item", "actual": actual_items[index]})
    for actual_item, expected_item in pairs:
        for field in ITEM_FIELDS:
            if not _equal(field, actual_item.get(field), expected_item.get(field)):
                differences.append({"scope": "item", "field": field, "actual": actual_item.get(field), "expected": expected_item.get(field), "product": expected_item.get("product_name")})
    return {"passed": not differences, "differences": differences, "expected_count": len(expected_items), "actual_count": len(actual_items)}


def json_default(value):
    return str(value)
