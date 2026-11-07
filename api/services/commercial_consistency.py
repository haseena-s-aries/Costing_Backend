"""Final consistency rules for extracted priced items (all document types).

1. Shipping / freight / transport costs are services: a PRODUCT row named as
   such becomes SERVICE.
2. A supplier name that is a shortened form of a fuller company name in the
   same document (e.g. a chat contact "Example Hongrun Qing" next to
   "Example Hongrun Marine Factory") is the same supplier.
3. A row that only restates another row's quotation is dropped: same
   supplier, price and currency, a name that is a strict subset of the
   other row's name, and taken from another page/date (a screenshot or an
   earlier conversation). Same-named rows on different dates (historical
   prices) and rows that add their own detail are kept.
4. Row totals are filled from quantity x unit price only when the result
   reconciles with the document's stated grand total; a row that merely
   repeats the grand total is dropped.
5. Item totals are checked against the grand total; a mismatch is flagged
   for review and never turned into another priced row.
"""
import re
from decimal import Decimal, InvalidOperation

TRANSPORT = re.compile(
    r"\bfreight\b|\b(?:shipping|shipment|transport(?:ation)?|courier|delivery|logistics|trucking|haulage|"
    r"cartage|forwarding)\s+(?:costs?|charges?|fees?|rates?|services?)\b|"
    r"\b(?:costs?|charges?|fees?)\s+(?:of|for)\s+(?:shipping|shipment|transport(?:ation)?|delivery|courier)\b",
    re.I)
TOTAL_ROW = re.compile(r"^\s*(?:grand\s+|sub\s*|net\s+)?total(?:ing|led)?\b|\btotal\s+(?:amount|value|price)\b", re.I)
BUSINESS = re.compile(r"\b(?:factory|co|company|corp(?:oration)?|ltd|limited|llc|l\.l\.c|inc|pte|gmbh|group|"
                      r"trading|industr(?:y|ies)|enterprises?|marine|shipping|logistics|services?|fze|fzco)\b", re.I)
STOP = {'for', 'of', 'the', 'to', 'and', 'a', 'an', 'per', 'in', 'on', 'at', 'with'}


def _amount(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def _tokens(name):
    words = re.findall(r"[a-z0-9]+", str(name or '').split('|')[0].casefold())
    return {w[:-1] if len(w) > 3 and w.endswith('s') else w for w in words if w not in STOP}


def classify_services(items):
    for item in items:
        if str(item.get('row_type') or 'PRODUCT').upper() == 'PRODUCT' and TRANSPORT.search(str(item.get('product_name') or '')):
            item['row_type'] = 'SERVICE'


def canonical_suppliers(items, result):
    names = {str(i.get('supplier_name')).strip() for i in items if i.get('supplier_name')}
    if result.get('supplier_name'):
        names.add(str(result['supplier_name']).strip())
    words = {name: name.casefold().split() for name in names}
    alias = {}
    for short in names:
        candidates = [full for full in names if full != short and BUSINESS.search(full)
                      and len(words[full]) > len(words[short])
                      and len(words[short]) >= 2 and words[full][:2] == words[short][:2]
                      and not BUSINESS.search(' '.join(words[short][2:]))]
        if len(candidates) == 1:
            alias[short] = candidates[0]
    for item in items:
        if item.get('supplier_name') in alias:
            item['supplier_name'] = alias[item['supplier_name']]
    if result.get('supplier_name') in alias:
        result['supplier_name'] = alias[result['supplier_name']]


SOURCE_VERIFIED = ('_msg_source_verified', '_explicit_email_rate', '_freight_section_verified',
                   '_internal_quote', '_source_fact_verified', '_attachment_name')


def drop_restatements(items):
    kept = []
    for item in items:
        mine = _tokens(item.get('product_name'))
        price = _amount(item.get('unit_price'))
        # A row read from its own dated source message is a quotation in
        # its own right (historical price), never a restatement.
        verified = any(item.get(flag) for flag in SOURCE_VERIFIED)
        restated = not verified and price is not None and mine and any(
            other is not item
            and _amount(other.get('unit_price')) == price
            and (other.get('currency') or None) == (item.get('currency') or None)
            and (other.get('supplier_name') or None) == (item.get('supplier_name') or None)
            and mine < _tokens(other.get('product_name'))
            and (other.get('page_number') != item.get('page_number')
                 or str(other.get('purchase_date') or '') != str(item.get('purchase_date') or ''))
            for other in items)
        if not restated:
            kept.append(item)
    return kept


def reconcile_totals(items, result):
    grand = _amount(result.get('grand_total'))
    priced = [i for i in items if _amount(i.get('unit_price')) is not None]
    codes = {i.get('currency') for i in priced}
    if grand is None or not priced or len(codes) != 1:
        return items
    # A row that only repeats the stated grand total is not an item.
    others = [i for i in priced if not (TOTAL_ROW.search(str(i.get('product_name') or ''))
                                       and _amount(i.get('unit_price')) == grand)]
    if len(others) < len(priced):
        items = [i for i in items if i in others or i not in priced]
        priced = others

    def total(item, computed):
        explicit = _amount(item.get('line_total'))
        if explicit is not None:
            return explicit
        qty, unit = _amount(item.get('quantity')), _amount(item.get('unit_price'))
        return qty * unit if computed and qty is not None and unit is not None else None

    computed = [total(i, True) for i in priced]
    if all(value is not None for value in computed) and sum(computed) == grand:
        for item, value in zip(priced, computed):
            if _amount(item.get('line_total')) is None:
                item['line_total'] = str(value.normalize() if value == value.to_integral() else value)
                item['_line_total_reconciled'] = True
        result['_grand_total_verified'] = True
    else:
        explicit = [total(i, False) for i in priced]
        if all(value is not None for value in explicit) and sum(explicit) != grand:
            reasons = list(result.get('_review_reasons') or [])
            result['_review_reasons'] = list(dict.fromkeys([*reasons, 'grand_total_mismatch']))
            result['_needs_review'] = True
    return items


def finalize(result):
    items = list(result.get('items') or [])
    if not items:
        return result
    classify_services(items)
    canonical_suppliers(items, result)
    items = drop_restatements(items)
    items = reconcile_totals(items, result)
    result = dict(result, items=items)
    return result
