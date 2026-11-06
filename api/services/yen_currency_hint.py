"""Resolve a bare yen sign (¥ / ￥) from hints printed in the same file.

The sign alone is shared by Chinese yuan (CNY) and Japanese yen (JPY), so a
price written as "￥22362" has no currency of its own. When the same source
file carries a hint, use it, strongest first:

1. a currency code or name: RMB, CNY, CN¥, 人民币, "<n> 元", yuan / JPY, JP¥,
   円, yen;
2. a telephone country code: +86 / 0086 (China), +81 / 0081 (Japan);
3. the writing script: Japanese kana means JPY; Chinese characters without
   any kana mean CNY.

A tier that points at both currencies is ignored. Rows resolved this way are
flagged for review ("currency_from_document_hint"); without any hint the
currency stays empty. Only rows that are still missing a currency and whose
price was written with the yen sign are touched.
"""
import re

YEN = re.compile(r"[¥￥]")
TIERS = (
    ("currency name/code",
     re.compile(r"\bRMB\b|\bCNY\b|CN¥|人民币|\d\s*元|\byuan\b", re.I),
     re.compile(r"\bJPY\b|JP¥|円|\byen\b", re.I)),
    ("phone country code",
     re.compile(r"\+\s*86\b|\(\s*\+?\s*86\s*\)|\b0086\b"),
     re.compile(r"\+\s*81\b|\(\s*\+?\s*81\s*\)|\b0081\b")),
)
KANA = re.compile(r"[぀-ヿ]")
HAN = re.compile(r"[一-鿿]")
# Any other printed currency means the yen sign is not the only candidate.
OTHER = re.compile(r"\b(?:USD|AED|EUR|GBP|SGD|INR|SAR|QAR|KWD|OMR|BHD|AUD|CAD)\b|US\$|€|£|₹")


def yen_hint(text):
    """Return (currency, reason) suggested by the file's own text."""
    text = str(text or "")
    for reason, cny, jpy in TIERS:
        is_cny, is_jpy = bool(cny.search(text)), bool(jpy.search(text))
        if is_cny != is_jpy:
            return ("CNY" if is_cny else "JPY"), reason
    if KANA.search(text):
        return "JPY", "Japanese script"
    if HAN.search(text):
        return "CNY", "Chinese script"
    return None, None


def resolve_yen_currency(result, source_text):
    source_text = str(source_text or "")
    items = list(result.get("items") or [])
    if not YEN.search(source_text) or not items:
        return result
    # Without row provenance, a document whose only printed currency is
    # the yen sign may still give it to its unqualified rows.
    yen_only_document = not OTHER.search(source_text)
    targets = []
    for item in items:
        if item.get("unit_price") in (None, "") or item.get("currency"):
            continue
        row_text = " ".join(str(item.get(field) or "") for field in
                            ("currency_evidence", "source_context", "unit_price"))
        if YEN.search(row_text) or (yen_only_document and not OTHER.search(row_text)):
            targets.append(item)
    if not targets:
        return result
    code, reason = yen_hint(source_text)
    if not code:
        return result
    for item in targets:
        item["currency"] = code
        item["_source_currency_missing"] = False
        item["_currency_hint"] = f"¥ resolved as {code} from {reason}"
    codes = {item.get("currency") for item in items if item.get("unit_price") not in (None, "")}
    if len(codes) == 1:
        result["currency"] = code
    reasons = [r for r in (result.get("_review_reasons") or [])
               if r not in {"ambiguous_currency_symbol", "ambiguous_segment_currency", "missing_currency"}]
    result["_review_reasons"] = list(dict.fromkeys([*reasons, "currency_from_document_hint"]))
    result["_needs_review"] = result["_gemini_needs_review"] = True
    return result
