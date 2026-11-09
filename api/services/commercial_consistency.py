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


# Moving goods between two named places ("Pick up of the materials from A
# to B") is a service.
MOVEMENT_ROUTE = re.compile(r"\b(?:pick[- ]?up|delivery|deliver|collection|shifting|transfer|haulage|trucking)\b"
                            r".*\bfrom\s+\S.*\bto\s+\S|^\s*from\s+\S.*\bto\s+\S", re.I)


# Time-based penalties after free time are charges, not products.
PENALTY = re.compile(r"\b(?:standby|stand-by|waiting|demurrage|detention)\b|\bstorage\s+(?:charges?|fees?|costs?)\b", re.I)


def classify_services(items):
    for item in items:
        name = str(item.get('product_name') or '')
        if str(item.get('row_type') or 'PRODUCT').upper() in {'PRODUCT', 'SERVICE'} and PENALTY.search(name):
            item['row_type'] = 'CHARGE'
            continue
        if str(item.get('row_type') or 'PRODUCT').upper() == 'PRODUCT' and (TRANSPORT.search(name) or MOVEMENT_ROUTE.search(name)):
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


TOTAL_LABEL = re.compile(r"\b(?:grand\s+)?total\b|\bnet\s+amount\b|\bamount\s+(?:due|payable)\b", re.I)
SHIPMENT_HINT = re.compile(r"\b(?:shipment|shipping|courier|freight|tracking|chargeable\s+weight|waybill|AWB|"
                           r"consignment|parcel|pickup)\b", re.I)


def single_total_service(result, items):
    """A document whose only price is its total (a booking confirmation,
    receipt or courier screenshot) keeps that price as ONE reviewable row
    instead of losing it as a 'total' line. With real line items, total
    rows are still dropped elsewhere."""
    priced = [i for i in items if _amount(i.get('unit_price') or i.get('line_total')) is not None]
    if any(not TOTAL_LABEL.search(str(i.get('product_name') or '')) for i in priced) or len(priced) > 1:
        return items
    source = priced[0] if priced else None
    amount = _amount((source or {}).get('unit_price') or (source or {}).get('line_total')) if source \
        else _amount(result.get('grand_total'))
    code = (source or {}).get('currency') or result.get('currency')
    if amount is None or not code:
        return items
    attrs = list((source or {}).get('extra_attributes') or [])
    context = ' '.join(str(v) for v in [result.get('document_type'), result.get('category'), result.get('title'),
                                       *(f"{a.get('label')} {a.get('value')}" for a in attrs if isinstance(a, dict)),
                                       (source or {}).get('product_name')] if v)
    # Never a 'total' name: the review sheet drops total lines.
    name = 'Shipment charges' if SHIPMENT_HINT.search(context) else 'Service charges'
    if source and source.get('product_name'):
        attrs.append({'label': 'Source label', 'value': str(source['product_name'])})
    row = dict(source or {}, product_name=name, display_name=name, unit_price=str(amount), line_total=None,
               quantity=(source or {}).get('quantity') if source and source.get('_quantity_printed') else None,
               currency=code, row_type='SERVICE', extra_attributes=attrs,
               supplier_name=(source or {}).get('supplier_name') or result.get('supplier_name'),
               purchase_date=(source or {}).get('purchase_date') or result.get('document_date'))
    reasons = list(result.get('_review_reasons') or [])
    result['_review_reasons'] = list(dict.fromkeys([*reasons, 'single_total_document']))
    result['_needs_review'] = True
    return [i for i in items if i is not source] + [row]


# Greeting / offer phrases that introduce a price but name nothing.
FILLER = re.compile(
    r"\b(?:dear\s+\w+|good\s+(?:day|morning|afternoon|evening)|hello|hi|greetings|"
    r"thanks?(?:\s+you)?(?:\s+(?:very\s+much|so\s+much))?(?:\s+for\s+(?:your|the)\s+(?:kind\s+)?(?:enquiry|inquiry|request|email|mail))?|"
    r"(?:we\s+are\s+|i\s+am\s+)?(?:pleased|happy|glad)\s+to\s+(?:offer|quote|inform|share)(?:\s+you)?|"
    r"(?:please|kindly|pls)\s+(?:find|note|see)(?:\s+(?:below|attached|herewith|our|the))*(?:\s+(?:best\s+)?(?:price|rate|offer|quote|quotation))?|"
    r"(?:our|the)\s+(?:best\s+)?(?:price|rate|offer|quote|quotation)(?:\s+(?:is|will\s+be|as\s+follows))?|"
    r"as\s+(?:discussed|requested|per\s+your\s+request)|we\s+can\s+offer|we\s+offer|offer(?:ing)?|"
    r"after\s*w[oa]rds?|there\s*after|after\s+that)\b", re.I)


def _is_filler(name):
    name = str(name or '').split('|')[0]
    # Only a name made of greeting/offer phrases is filler; part numbers and
    # codes such as "ZB5AD2C+ZB5AZ105C" never are.
    if not FILLER.search(name):
        return False
    rest = FILLER.sub(' ', name)
    return not re.search(r"[A-Za-z]{3}", re.sub(r"\b(?:and|the|for|your|our|you|is|are|to|of|at|a|an)\b", ' ', rest, flags=re.I))


def drop_filler_rows(items, result):
    """A row named only by greeting/offer text is the same price line as a
    properly named row with the same price, currency and date; drop it. A
    lone filler row is kept and flagged for review."""
    kept, flagged = [], False
    for item in items:
        if not _is_filler(item.get('product_name')):
            kept.append(item)
            continue
        price = _amount(item.get('unit_price'))
        twin = price is not None and any(
            other is not item and not _is_filler(other.get('product_name'))
            and _amount(other.get('unit_price')) == price
            and (other.get('currency') or None) == (item.get('currency') or None)
            and str(other.get('purchase_date') or '') == str(item.get('purchase_date') or '')
            for other in items)
        if not twin:
            kept.append(item)
            flagged = True
    if flagged:
        result['_review_reasons'] = list(dict.fromkeys([*(result.get('_review_reasons') or []), 'unnamed_price_line']))
        result['_needs_review'] = True
    return kept


def finalize(result):
    items = list(result.get('items') or [])
    items = single_total_service(result, items)
    if not items:
        return result
    classify_services(items)
    canonical_suppliers(items, result)
    items = drop_filler_rows(items, result)
    items = drop_restatements(items)
    items = reconcile_totals(items, result)
    result = dict(result, items=items)
    return result
