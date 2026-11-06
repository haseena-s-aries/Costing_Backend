"""
Golden-set scorer for the Gemini extraction pipeline.

Compares an actual extraction result (dict) against an expected/golden
result (dict) and produces a structured score report.

Design goals:
- Item-level matching is NOT positional (index N vs index N). Gemini output
  order can legitimately shift between runs/prompt edits. Items are matched
  by best-fit (product_name + unit_price + quantity similarity), same as a
  human reviewer would match them.
- Distinguishes "wrong value" (partial credit / listed) vs "missing item"
  vs "extra hallucinated item" vs "field mismatch on document header".
- Produces one JSON report per file plus one rollup summary across the
  whole golden set, so you can track pass-rate over time as you change
  prompts/code.

Usage:
    from scorer import score_result
    report = score_result(actual, expected)
    print(report["overall_score"])
"""

from __future__ import annotations
import re
import json
from decimal import Decimal, InvalidOperation


# Fields on the document header that must match closely.
DOCUMENT_HEADER_FIELDS = [
    "document_type",
    "category",
    "supplier_name",
    "document_date",
    "currency",
    "document_number",
]

# Fields on each item that matter for correctness scoring.
ITEM_FIELDS = [
    "row_type",
    "product_name",
    "quantity",
    "unit_price",
    "line_total",
    "currency",
    "supplier_name",
    "purchase_date",
]

# Fields used purely to MATCH an actual item to its expected counterpart.
# (product_name similarity + unit_price/line_total closeness is usually
# enough — item_code/part_number help when present.)
MATCH_FIELDS = ["item_code", "part_number", "unit_price", "line_total"]


def _norm_text(value):
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return text


def _norm_number(value):
    if value in (None, ""):
        return None
    text = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _text_similarity(a, b):
    """Cheap token-overlap similarity, 0..1. No external deps."""
    a, b = _norm_text(a), _norm_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    tokens_a = set(re.findall(r"[a-z0-9]+", a))
    tokens_b = set(re.findall(r"[a-z0-9]+", b))
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return overlap / union if union else 0.0


def _values_match(a, b, is_numeric=False):
    if is_numeric:
        na, nb = _norm_number(a), _norm_number(b)
        if na is None and nb is None:
            return True
        if na is None or nb is None:
            return False
        tolerance = max(Decimal("0.05"), abs(nb) * Decimal("0.01"))
        return abs(na - nb) <= tolerance
    return _norm_text(a) == _norm_text(b)


NUMERIC_FIELDS = {"quantity", "unit_price", "line_total"}


def _item_match_score(actual_item, expected_item):
    """0..1 score of how likely `actual_item` IS `expected_item`."""
    name_sim = _text_similarity(
        actual_item.get("product_name"), expected_item.get("product_name")
    )
    price_match = 1.0 if _values_match(
        actual_item.get("unit_price") or actual_item.get("line_total"),
        expected_item.get("unit_price") or expected_item.get("line_total"),
        is_numeric=True,
    ) else 0.0
    code_match = 1.0 if (
        _norm_text(actual_item.get("item_code")) and
        _norm_text(actual_item.get("item_code")) == _norm_text(expected_item.get("item_code"))
    ) or (
        _norm_text(actual_item.get("part_number")) and
        _norm_text(actual_item.get("part_number")) == _norm_text(expected_item.get("part_number"))
    ) else 0.0

    # Weighted blend: name similarity carries most weight, price is a
    # strong secondary signal, exact code match is a tie-breaker bonus.
    return (name_sim * 0.6) + (price_match * 0.3) + (code_match * 0.1)


def _match_items(actual_items, expected_items, threshold=0.35):
    """
    Greedy best-match pairing between actual and expected items.
    Returns (pairs, unmatched_actual, unmatched_expected)
    pairs = list of (actual_index, expected_index, match_score)
    """
    candidates = []
    for ai, a_item in enumerate(actual_items):
        for ei, e_item in enumerate(expected_items):
            score = _item_match_score(a_item, e_item)
            if score >= threshold:
                candidates.append((score, ai, ei))

    candidates.sort(reverse=True)  # highest score first

    used_actual, used_expected = set(), set()
    pairs = []
    for score, ai, ei in candidates:
        if ai in used_actual or ei in used_expected:
            continue
        used_actual.add(ai)
        used_expected.add(ei)
        pairs.append((ai, ei, score))

    unmatched_actual = [i for i in range(len(actual_items)) if i not in used_actual]
    unmatched_expected = [i for i in range(len(expected_items)) if i not in used_expected]

    return pairs, unmatched_actual, unmatched_expected


def score_result(actual: dict, expected: dict) -> dict:
    actual = actual or {}
    expected = expected or {}

    # ---- Document header fields ----
    header_mismatches = []
    header_correct = 0
    for field in DOCUMENT_HEADER_FIELDS:
        is_num = False
        if _values_match(actual.get(field), expected.get(field), is_numeric=is_num):
            header_correct += 1
        else:
            header_mismatches.append({
                "field": field,
                "actual": actual.get(field),
                "expected": expected.get(field),
            })
    header_score = header_correct / len(DOCUMENT_HEADER_FIELDS)

    # ---- Items ----
    actual_items = actual.get("items") or []
    expected_items = expected.get("items") or []

    pairs, unmatched_actual, unmatched_expected = _match_items(actual_items, expected_items)

    item_field_mismatches = []
    correct_items = 0

    for ai, ei, match_score in pairs:
        a_item = actual_items[ai]
        e_item = expected_items[ei]
        field_issues = []
        for field in ITEM_FIELDS:
            is_num = field in NUMERIC_FIELDS
            if not _values_match(a_item.get(field), e_item.get(field), is_numeric=is_num):
                field_issues.append({
                    "field": field,
                    "actual": a_item.get(field),
                    "expected": e_item.get(field),
                })
        if not field_issues:
            correct_items += 1
        else:
            item_field_mismatches.append({
                "expected_index": ei,
                "actual_index": ai,
                "product_name": e_item.get("product_name"),
                "issues": field_issues,
            })

    missing_items = [
        {"index": ei, "product_name": expected_items[ei].get("product_name"),
         "unit_price": expected_items[ei].get("unit_price")}
        for ei in unmatched_expected
    ]
    extra_items = [
        {"index": ai, "product_name": actual_items[ai].get("product_name"),
         "unit_price": actual_items[ai].get("unit_price")}
        for ai in unmatched_actual
    ]

    total_expected_items = len(expected_items) or 1
    item_recall = (correct_items) / total_expected_items  # correct AND matched
    item_precision = correct_items / (len(actual_items) or 1)

    # Overall: item accuracy matters more than header (headers are easier).
    overall_score = round((header_score * 0.25) + (item_recall * 0.55) + (item_precision * 0.20), 4)

    passed = (
        overall_score >= 0.95
        and not missing_items
        and not extra_items
        and not header_mismatches
    )

    # Stable machine-readable failure categories.  These are intentionally
    # derived from the comparison only: a failing sample can be routed to a
    # deterministic repair, a bounded Gemini retry, or human review without
    # changing the prompt blindly.
    failure_categories = []
    if missing_items:
        failure_categories.append("missing_items")
    if extra_items:
        failure_categories.append("extra_items")
    if item_field_mismatches:
        failure_categories.append("wrong_item_fields")
    if header_mismatches:
        failure_categories.append("wrong_document_header")
    if not actual_items and expected_items:
        failure_categories.append("no_items_extracted")
    if not failure_categories and not passed:
        failure_categories.append("below_threshold")

    return {
        "passed": passed,
        "overall_score": overall_score,
        "header_score": round(header_score, 4),
        "header_mismatches": header_mismatches,
        "item_recall": round(item_recall, 4),
        "item_precision": round(item_precision, 4),
        "expected_item_count": len(expected_items),
        "actual_item_count": len(actual_items),
        "correct_item_count": correct_items,
        "missing_items": missing_items,       # in expected, never extracted
        "extra_items": extra_items,           # extracted but not real (hallucinated / dup)
        "item_field_mismatches": item_field_mismatches,  # matched but wrong field(s)
        "failure_categories": failure_categories,
    }


def summarize(reports: dict) -> dict:
    """reports = {filename: report_dict}. Returns rollup summary."""
    total = len(reports)
    passed = sum(1 for r in reports.values() if r["passed"])
    avg_score = round(sum(r["overall_score"] for r in reports.values()) / (total or 1), 4)
    worst = sorted(reports.items(), key=lambda kv: kv[1]["overall_score"])[:5]
    category_counts = {}
    for report in reports.values():
        for category in report.get("failure_categories", []):
            category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "total_files": total,
        "passed": passed,
        "pass_rate": round(passed / (total or 1), 4),
        "average_score": avg_score,
        "worst_5_files": [{"file": name, "score": r["overall_score"]} for name, r in worst],
        "failure_category_counts": category_counts,
    }


if __name__ == "__main__":
    # Tiny smoke test
    expected = {
        "document_type": "invoice", "category": "INVOICE", "supplier_name": "Acme Co",
        "document_date": "2026-01-15", "currency": "USD", "document_number": "INV-1",
        "items": [
            {"row_type": "PRODUCT", "product_name": "Steel Pipe 50mm", "quantity": "10",
             "unit_price": "5.00", "line_total": "50.00", "currency": "USD"},
        ],
    }
    actual = {
        "document_type": "invoice", "category": "INVOICE", "supplier_name": "Acme Co",
        "document_date": "2026-01-15", "currency": "USD", "document_number": "INV-1",
        "items": [
            {"row_type": "PRODUCT", "product_name": "Steel Pipe 50 mm", "quantity": "10",
             "unit_price": "5.00", "line_total": "50.00", "currency": "USD"},
        ],
    }
    print(json.dumps(score_result(actual, expected), indent=2))
