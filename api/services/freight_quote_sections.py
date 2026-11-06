"""Source-backed freight/shipping offers from dated seller emails.

Each priced freight service line is read together with its own quotation
section (an "Option N" block, or the whole message when it has no options),
so the row description can identify the offer:

    Service name - Origin to Destination - Container size

When no container size is printed, the explicitly stated option is used
instead ("- Option 2"). Missing values are never guessed. Supplier, date,
price and currency stay in their own fields, and every dated seller message
keeps its own rows (historical prices are not merged).

No supplier, file or route specific rules.
"""
import io
import re
import unicodedata
from decimal import Decimal

from .msg_commercial_history import (
    AMOUNT, AMOUNT_AFTER, clean, number, currency, split_thread, is_enquiry, plausible_company,
)
from .price_calculation import calculation

# A priced line is a freight *service* (not a surcharge or fee) when its
# label names the movement itself.
SERVICE = re.compile(
    r"\b(?:freight|door\s+to\b|to\s+door\b|port\s+to\s+(?:port|door)|"
    r"FCL|LCL|ocean|sea\s+freight|air\s+freight|EXW|FOB|CIF|CFR|DAP|DDP|DDU)\b", re.I)
NOT_SERVICE = re.compile(
    r"\b(?:total|invoice value|CIF value|insurance|premium|storage|demurrage|detention|"
    r"inspection|waiting|surcharge|EEC|DTT|duty|VAT)\b", re.I)
OPTION = re.compile(r"^\s*option\s*[-–:#.]?\s*(\d+)\s*[:.)-]?\s*$", re.I)
SIZE = re.compile(
    r"(?<![\d.])(20|40|45)\s*(?:(?P<ft>['’′]|ft\b|feet\b|foot\b)|(?P<type>GP|DV|HC|HQ|RF|RH|OT|FR|STD|DC)\b)",
    re.I)
FIELDS = {
    'origin': r"POL|port\s+of\s+loading|loading\s+port|origin(?:\s+port)?",
    'destination': r"POD|port\s+of\s+discharge|discharge\s+port|destination(?:\s+port)?|place\s+of\s+delivery",
    'container': r"container(?:\s+(?:size|type))?|equipment",
    'transit': r"T\s*/\s*T|transit\s+time",
    'carrier': r"carrier|shipping\s+line",
}
LABELS = {'origin': 'POL', 'destination': 'POD', 'container': 'Container size',
          'transit': 'Transit time', 'carrier': 'Carrier'}
SMALL_WORDS = {'to', 'of', 'and', 'at', 'by', 'for', 'in', 'on', 'the', 'via'}
# A buyer's message accepting an offer: its prices are the supplier's
# agreed rate, and the supplier is the other party in the thread.
CONFIRMATION = re.compile(
    r"\b(?:(?:your|offer|quote|quotation|rate|price)s?\b[^.\n]{0,100}\b(?:is|are)\s+(?:hereby\s+)?confirmed"
    r"|job\s+confirmation|we\s+(?:hereby\s+)?confirm\s+(?:your|the)\s+(?:offer|quote|quotation|rate))\b", re.I)
BUSINESS = r"(?:Logistics|Shipping|Freight|Forwarding|Marine|Lines?|Cargo|Trading|Services|Transport)"
SUFFIX = r"(?:L\.?L\.?C\.?|LLC|PTE\.?\s+LTD\.?|CO\.?,?\s*LTD\.?|LTD\.?|LIMITED|INC\.?|FZE|FZCO|FZ-LLC)"


def _title(text):
    """Title-case ALL-CAPS source text for a readable description."""
    text = clean(text)
    if text != text.upper():
        return text
    words = text.lower().split()
    return ' '.join(w if i and w in SMALL_WORDS else w[:1].upper() + w[1:] for i, w in enumerate(words))


def _size(text):
    match = SIZE.search(text or '')
    if not match:
        return None
    return match[1] + ('FT' if match['ft'] else match['type'].upper())


def _field_values(lines):
    """Labelled values written as "POL : X" or as "POL" followed by "X"."""
    values = {}
    for index, line in enumerate(lines):
        for key, pattern in FIELDS.items():
            match = re.match(rf"^\s*(?:{pattern})\s*(?:[:：=-]\s*(?P<value>.*))?$", line, re.I)
            if not match:
                continue
            value = clean(match['value'] or '')
            if not value and index + 1 < len(lines) and not AMOUNT.search(lines[index + 1]):
                value = clean(lines[index + 1])
            if value and not AMOUNT.search(value):
                values.setdefault(key, value)
    return values


def _sender_domain(block):
    addresses = re.findall(r"[\w.+-]+@([\w-]+)\.[\w.]+", str(block.get('sender') or ''))
    if not addresses:
        tail = re.split(r"(?i)regards", block['body'])
        addresses = re.findall(r"[\w.+-]+@([\w-]+)\.[\w.]+", tail[-1]) if len(tail) > 1 else []
    stems = {a.lower() for a in addresses}
    return next(iter(stems)) if len(stems) == 1 else None


def _domain_companies(text):
    """Legal company names printed anywhere in the source, keyed by the
    letters of the name, so a sender domain can be matched to one."""
    names = {}
    for match in re.finditer(rf"\b([A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){{0,4}})\s+({SUFFIX})(?=\W|$)", text):
        name = clean(match[0]).rstrip('.')
        key = re.sub(r"[^a-z]", "", match[1].lower())
        if plausible_company(name) and len(key) >= 5:
            names.setdefault(key, set()).add(name)
    return names


def _supplier(block, companies):
    if block.get('supplier'):
        return block['supplier']
    stem = _sender_domain(block)
    if not stem:
        return None
    # Only a printed company whose name spells the sender's domain stem
    # (e.g. "Example Shipping LLC" for example-shipping.com) is accepted.
    names = companies.get(re.sub(r"[^a-z]", "", stem), set())
    return next(iter(names)) if len(names) == 1 else None


def _signature_company(stem, blocks):
    """Company name printed in the stem owner's own messages that starts
    with the domain stem, e.g. "Example Marine Logistics" for example.com."""
    names = set()
    for block in blocks:
        if _sender_domain(block) != stem:
            continue
        for match in re.finditer(rf"\b{re.escape(stem)}((?:\s+[A-Z][\w&.-]*){{0,4}}\s+{BUSINESS})\b",
                                 block['body'], re.I):
            if match[0][:1].isupper():
                names.add(clean(match[0]))
    return next(iter(names)) if len(names) == 1 else None


def _counterparty(block, blocks, companies):
    """Supplier for a buyer's confirmation: the single other party."""
    own = _sender_domain(block)
    others = {_sender_domain(b) for b in blocks if b is not block} - {own, None}
    if not own or len(others) != 1:
        return None
    stem = others.pop()
    names = companies.get(re.sub(r"[^a-z]", "", stem), set())
    if len(names) == 1:
        return next(iter(names))
    return _signature_company(stem, blocks)


def _price(line):
    """(label, unit_price, currency, quantity, uom, line_total) of a priced
    line. A printed unit x qty = total calculation is one item."""
    calc = calculation(line)
    first = min((m for m in (AMOUNT.search(line), AMOUNT_AFTER.search(line)) if m),
                key=lambda m: m.start(), default=None)
    if calc and first:
        code = first['currency']
        return (line[:first.start()], calc['unit_price'], currency(code), calc['quantity'],
                calc['uom'], calc['line_total'], line[first.end():])
    matches = list(AMOUNT.finditer(line))
    if len(matches) != 1:
        return None
    match = matches[0]
    return line[:match.start()], number(match['amount']), currency(match['currency']), None, None, None, line[match.end():]


def _sections(lines):
    """Split a message into quotation sections at "Option N" headings.
    Lines before the first heading are context shared by every section."""
    shared, sections, current = [], [], None
    for line in lines:
        option = OPTION.match(line)
        if option:
            current = {'option': option[1], 'lines': []}
            sections.append(current)
        elif current is None:
            shared.append(line)
        else:
            current['lines'].append(line)
    return shared, sections or [{'option': None, 'lines': shared}]


def _commercial_lines(block):
    lines = [clean(s).lstrip('*•·- ') for s in block['body'].splitlines() if clean(s)]
    end = next((i for i, line in enumerate(lines) if re.match(
        r"^(?:(?:thanks\s*(?:&|and)\s*)?(?:kind |best |warm )?regards\b|notes?\s*:|disclaimer\b)", line, re.I)), len(lines))
    return lines[:end]


def freight_offer_rows(body, sender=None, sent_date=None):
    blocks = split_thread(body, sender, sent_date)
    companies = _domain_companies(str(body or ''))
    rows = []
    for block in blocks:
        if not block['date'] or is_enquiry(block):
            continue
        lines = _commercial_lines(block)
        shared, sections = _sections(lines)
        shared_fields = _field_values(shared) if sections and sections[0]['lines'] is not shared else {}
        supplier = (_counterparty(block, blocks, companies) if CONFIRMATION.search(block['text'])
                    else _supplier(block, companies))
        for section in sections:
            fields = {**shared_fields, **_field_values(section['lines'])}
            for index, line in enumerate(section['lines']):
                price = _price(line)
                if not price:
                    continue
                label, unit_price, code, quantity, uom, line_total, remainder = price
                label = re.sub(r"\(\s+", "(", label).strip(' :–—-+*/')
                if not label or not SERVICE.search(label) or NOT_SERVICE.search(label):
                    continue
                if not supplier:
                    continue
                size = _size(remainder) or _size(fields.get('container')) or _size(label)
                parts = [_title(label)]
                if fields.get('origin') and fields.get('destination'):
                    parts.append(f"{_title(fields['origin'])} to {_title(fields['destination'])}")
                if size and size not in re.sub(r"\s+", "", label).upper().replace("'", 'FT'):
                    parts.append(size)
                elif not size and section['option']:
                    parts.append(f"Option {section['option']}")
                name = ' - '.join(parts)
                attrs = [{'label': LABELS[key], 'value': fields[key]}
                         for key in ('origin', 'destination', 'transit', 'carrier') if fields.get(key)]
                if size:
                    attrs.append({'label': 'Container size', 'value': size})
                if section['option']:
                    attrs.append({'label': 'Quotation option', 'value': f"Option {section['option']}"})
                rows.append({
                    'row_type': 'SERVICE', 'product_name': name, 'display_name': name,
                    'quantity': quantity, 'uom': uom or size, 'unit_price': unit_price,
                    'line_total': line_total, 'currency': code,
                    'purchase_date': block['date'], 'quotation_date': block['date'],
                    'supplier_name': supplier, 'page_number': None, 'extra_attributes': attrs,
                    'source_block_id': block['id'],
                    'source_row_id': f"{block['id']}:freight:{section['option'] or 0}:{index}",
                    'source_reference': block['id'], 'source_context': line,
                    '_msg_source_verified': True, '_freight_section_verified': True,
                })
    return rows


def restore_freight_offers(result, body, sender=None, sent_date=None):
    """Replace model rows for the same priced freight offer with the
    source-backed rows. A model row is the same offer when its price and
    currency match and it is dated on that quote, or on a message that
    quotes nothing (a buyer reply / the printed thread's top email)."""
    recovered = freight_offer_rows(body, sender, sent_date)
    if not recovered:
        return result

    def amount(value):
        value = number(value)
        return Decimal(value) if value is not None else None

    quote_dates = {row['purchase_date'] for row in recovered}
    kept = []
    for item in result.get('items') or []:
        date = str(item.get('purchase_date') or result.get('document_date') or '')[:10]
        name = str(item.get('product_name') or '').split('|')[0]
        verified_other = ((item.get('_explicit_email_rate') or item.get('_source_fact_verified'))
                          and not (SERVICE.search(name) and not NOT_SERVICE.search(name)))
        if item.get('_attachment_name') or verified_other:
            kept.append(item)
            continue
        duplicate = any(
            amount(item.get('unit_price')) in {amount(row['unit_price']), amount(row['line_total'])}
            and (item.get('currency') or row['currency']) == row['currency']
            and (date == row['purchase_date'] or date not in quote_dates)
            for row in recovered)
        if not duplicate:
            kept.append(item)
    result = dict(result, items=recovered + kept)
    suppliers = {row.get('supplier_name') for row in result['items'] if row.get('supplier_name')}
    if len(suppliers) == 1:
        result['supplier_name'] = next(iter(suppliers))
    result['document_date'] = max(quote_dates)
    result['_freight_section_count'] = len(recovered)
    # Offers from different dates/options are alternatives, never one total.
    result['subtotal'] = result['grand_total'] = None
    return result


def pdf_text(file_bytes):
    """Visible PDF text without page-number lines ('' when unreadable)."""
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return '\n'.join('\n'.join(line for line in (page.extract_text() or '').splitlines()
                                       if not re.fullmatch(r'\s*\d+\s*', line)) for page in pdf.pages)
    except Exception:
        return ''


def pdf_thread_text(file_bytes):
    """Text of a PDF that is a printed email thread, else None. Page-number
    lines are dropped so a quotation continuing on the next page stays in
    its own message."""
    text = pdf_text(file_bytes)
    if not text:
        return None
    if len(re.findall(r'(?im)^From\s*:', text)) < 2 or not re.search(r'(?im)^(?:Sent|Date)\s*:', text):
        return None
    return unicodedata.normalize('NFKC', text)


def collapse_calculated_totals(result):
    """Drop a row that only repeats another row's printed total (same date
    and currency), e.g. "... x 4 Nos =" or "Net amount confirmed is" next to
    the unit-price row of that calculation. Email-thread documents only."""
    items = list(result.get('items') or [])

    def amount(value):
        value = number(value)
        return Decimal(value) if value is not None else None

    def key(item):
        return str(item.get('purchase_date') or '')[:10], item.get('currency')

    totals = {}
    for item in items:
        unit, qty = amount(item.get('unit_price')), amount(item.get('quantity'))
        total = amount(item.get('line_total')) or (unit * qty if unit and qty and qty > 1 else None)
        if unit and total and total != unit:
            totals.setdefault(key(item), {})[total] = item
    kept = []
    for item in items:
        owner = totals.get(key(item), {}).get(amount(item.get('unit_price')))
        if owner is not None and owner is not item and not amount(item.get('quantity') or 0) > 1:
            owner['line_total'] = owner.get('line_total') or str(amount(item.get('unit_price')))
            continue
        kept.append(item)
    if len(kept) != len(items):
        result = dict(result, items=kept)
    return result
