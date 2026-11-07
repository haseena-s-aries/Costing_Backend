"""Recover explicit freight rates from each dated seller message."""
import re
from decimal import Decimal
from .msg_commercial_history import (
    AMOUNT, clean, number, currency, split_thread, is_enquiry, _base,
)

CHARGE_WORDS = (r'\b(?:freight|EXW|transportation|delivery|door|fee|charges?|THC|PHC|DDO|seal|'
                r'inspection|clearance|premium|EEC|yard)\b')
# Words that open a new clause inside a sentence-style rate line.
CLAUSE_BREAK = r'[,;]|\b(?:then|after|thereafter|where|which|when|we|you|will|can|shall|is|are)\b'
# Conditions longer than this are kept as a source remark, not as part of
# the item identity shown in the display name.
MAX_CONDITION = 60


def _label(text):
    """Return the charge name only, never the surrounding sentence."""
    text = re.sub(r'^subject(?:\s+to)?\s+(?=\S)', '', clean(text), flags=re.I)
    if len(text.split()) > 6 or re.search(r'[,;]', text):
        parts = [p.strip(' :–—-') for p in re.split(CLAUSE_BREAK, text, flags=re.I)]
        tail = next((p for p in reversed(parts) if p and re.search(CHARGE_WORDS, p, re.I)), None)
        if tail:
            text = tail
    return text[:1].upper() + text[1:] if text.islower() else text


def _condition(text):
    # "/-" and "/=" after an amount are price-ending marks, not conditions.
    text = re.sub(r"^\s*/\s*[-=]+", "", clean(text)).strip(' ;,.:+–—-/')
    text = re.sub(r"^\((.*)\)$", r"\1", text).strip()
    if not re.search(r'[A-Za-z0-9]', text) or re.fullmatch(r'(?:will\s+)?(?:be\s+)?applicable', text, re.I):
        return ''
    return text


def _amount_label(line, matches, mi):
    start = matches[mi-1].end() if mi else 0
    label = line[start:matches[mi].start()].strip(' :–—-+*/0123456789.,')
    if mi and '+' in label:
        label = label.rsplit('+', 1)[-1].strip()
    return label


def rate_rows(body, sender=None, sent_date=None):
    rows = []
    for block in split_thread(body, sender, sent_date):
        text = block['body']
        if is_enquiry(block) or not block['date'] or not block['supplier']:
            continue
        if not re.search(r'\b(?:freight|shipping|container|POD|POL)\b', text, re.I):
            continue
        lines = [clean(s).lstrip('*•· ') for s in text.splitlines() if clean(s)]
        for index, line in enumerate(lines):
            matches = list(AMOUNT.finditer(line))
            for mi, match in enumerate(matches):
                label = _amount_label(line, matches, mi)
                if not label and index:
                    label = lines[index-1].strip(' :–—-')
                if not label or re.search(r'\b(?:total|invoice value|exchange|ex-rate|CIF value)\b', label, re.I):
                    continue
                if not re.search(CHARGE_WORDS, label, re.I):
                    continue
                end = matches[mi+1].start() if mi+1 < len(matches) else len(line)
                detail = line[match.end():end].strip()
                if mi+1 < len(matches):
                    # Text before the next amount names that next charge.
                    next_label = _amount_label(line, matches, mi+1)
                    if next_label and detail.endswith(next_label):
                        detail = detail[:-len(next_label)].strip()
                # Rate basis and conditional wording remain attached to this
                # quote, not silently turned into an unconditional total.
                basis = re.match(r'^/\s*(TEU|20GP|20FT|UNIT|hours?|documents?|containers?|cntr)\b', detail, re.I)
                uom = basis[1] if basis else None
                if uom:
                    uom = {'hours': 'hour', 'documents': 'document', 'containers': 'container', 'cntr': 'container'}.get(uom.lower(), uom)
                condition = re.sub(r'^/\s*' + re.escape(basis[1]), '', detail, count=1, flags=re.I).strip() if basis else detail
                condition = _condition(condition)
                conditional = bool(re.search(r'\b(?:if|subject|waiting|storage|inspection|approx)\b', line + ' ' + label, re.I))
                label = _label(label)
                attrs = [{'label': 'Rate basis', 'value': uom}] if uom else []
                if condition:
                    attrs.append({'label': 'Condition / inclusions' if len(condition) <= MAX_CONDITION else 'Remarks',
                                  'value': condition})
                if conditional:
                    attrs.append({'label': 'Applicability', 'value': 'Conditional'})
                if re.search(r'\bservice (?:charge|fee)\b', label, re.I) and (re.search(r'inspection', line, re.I) or (index and re.search(r'inspection', lines[index-1], re.I))):
                    label = 'Inspection service fee'
                    conditional = True
                qty_match = re.search(r'\b(?:qty|quantity)\s*[:=]\s*(\d+(?:\.\d+)?)\b', line, re.I)
                row = _base(block, label, number(match['amount']), currency(match['currency']),
                            qty=qty_match[1] if qty_match else None, uom=uom, row_type='CHARGE', attrs=attrs)
                row['is_conditional'] = conditional
                row['rate_basis'] = uom
                row['quotation_date'] = block['date']
                row['quotation_id'] = block['id'] + ':' + block['date']
                row['source_row_id'] = f"{block['id']}:rate:{index}:{mi}"
                row['_explicit_email_rate'] = True
                rows.append(row)
    return rows


def restore_rates(result, body, sender=None, sent_date=None):
    recovered = rate_rows(body, sender, sent_date)
    if not recovered:
        return result
    existing = list(result.get('items') or [])
    def amount(value):
        value = number(value)
        return Decimal(value) if value is not None else None
    def tokens(value):
        value = re.sub(r'\([^)]*\)', '', clean(value).lower()).split('|')[0]
        if re.search(r'\bservice (?:charge|fee)\b', value):
            return {'service', 'fee'}
        return set(re.findall(r'[a-z0-9]+', value)) - {'the', 'for', 'at', 'by', 'charge', 'charges', 'subject', 'to', 'sea', 'freight'}
    for row in recovered:
        matches = []
        for old in existing:
            # Compare numerically: "100" and "100.00" are the same rate.
            if amount(old.get('unit_price')) != amount(row['unit_price']) or old.get('currency') != row['currency']:
                continue
            if str(old.get('purchase_date') or result.get('document_date') or '')[:10] != row['purchase_date']:
                continue
            a, b = tokens(old.get('product_name')), tokens(row['product_name'])
            if a and b and (a <= b or b <= a):
                matches.append(old)
        if matches:
            old = matches[0]
            if not row['is_conditional'] and len(clean(old.get('product_name'))) > len(row['product_name']):
                row['product_name'] = old['product_name']
                row['display_name'] = old['product_name']
            attrs = list(old.get('extra_attributes') or [])
            for attr in row['extra_attributes']:
                if attr not in attrs:
                    attrs.append(attr)
            # Source evidence determines quantity, classification and totals;
            # preserve useful route, inclusion and exclusion attributes.
            row['extra_attributes'] = attrs
            existing = [item for item in existing if not any(item is m for m in matches)]
            existing.append(row)
        else:
            existing.append(row)
    result['items'] = existing
    suppliers = {r.get('supplier_name') for r in existing}
    if len(suppliers) == 1:
        result['supplier_name'] = next(iter(suppliers))
    result['visible_priced_row_count'] = len(existing)
    # Preserve the complete authored terms with their quotation, including
    # unpriced inclusions/exclusions; never sum across dates or currencies.
    owned = {r.get('source_block_id') for r in recovered}
    result['quotations'] = [
        {'quotation_id': b['id'] + ':' + b['date'], 'date': b['date'],
         'supplier_name': b['supplier'], 'terms_source': b['body'],
         'confirmed_total': None}
        for b in split_thread(body, sender, sent_date) if b['id'] in owned
    ]
    dates = {r.get('purchase_date') for r in existing}
    if len(dates) > 1 or any(r.get('is_conditional') for r in existing):
        result['subtotal'] = None
        result['grand_total'] = None
    return result
