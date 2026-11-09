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
# Quotation alternatives: "Option 1", "Collection 2", "Alternative 3", "Offer 1".
OPTION = re.compile(r"^\s*(option|collection|alternative|offer)\s*[-–:#.]?\s*(\d+)\s*[:.)-]?\s*$", re.I)
SIZE = re.compile(
    r"(?<![\d.])(20|40|45)\s*(?:(?P<ft>['’′]|ft\b|feet\b|foot\b)(?:\s*(?P<type2>GP|DV|HC|HQ|RF|RH|OT|FR|STD|DC)\b)?"
    r"|(?P<type>GP|DV|HC|HQ|RF|RH|OT|FR|STD|DC)\b)",
    re.I)
FIELDS = {
    'origin': r"POL|AOL|port\s+of\s+loading|loading\s+port|airport\s+of\s+(?:loading|departure)|"
              r"origin(?:\s+(?:port|airport))?|collection\s+from|pick[- ]?up\s+from|ship\s+from",
    'destination': r"POD|AOD|port\s+of\s+discharge|discharge\s+port|airport\s+of\s+(?:destination|discharge|arrival)|"
                   r"destination(?:\s+(?:port|airport))?|place\s+of\s+delivery|deliver(?:y)?\s+to|ship\s+to",
    'container': r"container(?:\s+(?:size|type))?|equipment",
    'transit': r"T\s*/\s*T|TT|transit\s+time",
    'carrier': r"carrier|shipping\s+line",
    'airline': r"airline",
    'term': r"term|incoterms?",
    'weight': r"chargeable\s+weight",
    'route': r"route",
    'mode': r"mode(?:\s+of\s+(?:shipment|transport))?|shipment\s+mode|service\s+type",
}
LABELS = {'origin': 'POL', 'destination': 'POD', 'container': 'Container size',
          'transit': 'Transit time', 'carrier': 'Carrier', 'airline': 'Airline', 'term': 'Term',
          'weight': 'Chargeable weight', 'route': 'Route'}
# A service term printed in brackets right after the price, e.g.
# "ALL IN CHARGES : AED 90800/- (DOOR TO DOOR)".
TRAILING_SERVICE = re.compile(r"^\s*/?\s*-?\s*\((?P<term>[^)]{3,40})\)")
# Local transport / delivery charges are routed between two places.
TRANSPORT_LINE = re.compile(r"\b(?:transport\w*|trucking|delivery|pick[- ]?up|haulage|cartage)\b", re.I)
PLACE_PAIR = re.compile(r"^\s*(?P<a>[A-Za-z][\w .&/]{1,40}?)\s*(?:–|—|->|>|\s-\s|\bto\b)\s*(?P<b>[A-Za-z][\w .&/]{1,40}?)\s*$", re.I)
FROM_TO = re.compile(r"\bfrom\s+(?P<a>[A-Za-z][\w .&/-]{1,40}?)(?:\s+to\s+(?P<b>[A-Za-z][\w .&/-]{1,40}?))?\s*(?:[.:;,(]|$)", re.I)
MODE = re.compile(r"\b(?P<mode>AIR|SEA|ROAD|OCEAN)\s+(?:IMPORT|EXPORT|FREIGHT|SHIPMENT|ESTIMATE)\b", re.I)
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
    # A bare location/airport code or charge code stays as printed (HKG,
    # AUH, DO, BOE).
    if text != text.upper() or re.fullmatch(r"[A-Z]{2,4}", text):
        return text
    words = text.lower().split()
    return ' '.join(w if i and w in SMALL_WORDS else w[:1].upper() + w[1:] for i, w in enumerate(words))


def _place(value):
    """Location value without hedges or notes ("Tianjin or nearby" ->
    "Tianjin", "AUH port ( AUH delivery )" -> "AUH port")."""
    value = re.sub(r"\s+or\s+(?:nearby|near\s*by|any|equivalent|similar)\b.*$", "", clean(value), flags=re.I)
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    return value.strip(" .,;:-–")


def _transport_route(block, lines, index, fields):
    """Route of a local transport/delivery charge: the nearest "from X (to
    Y)" heading above it, a field holding two places ("DXB – AUH"), or the
    email subject's "from X to Y"."""
    for line in reversed(lines[:index]):
        if _price(line) or len(line) > 60:
            continue
        match = FROM_TO.search(line)
        if match and match['a'][:1].isupper():
            return _place(match['a']), _place(match['b'] or '') or None
    for key in ('route', 'origin', 'destination'):
        pair = PLACE_PAIR.match(fields.get(key) or '')
        if pair:
            return _place(pair['a']), _place(pair['b'])
    subject = re.search(r"^Subject\s*:\s*(.*)$", block['text'], re.I | re.M)
    match = FROM_TO.search(subject[1]) if subject else None
    if match and match['b']:
        return _place(match['a']), _place(match['b'])
    return None, None


def _size(text):
    match = SIZE.search(text or '')
    if not match:
        return None
    # "40 ft OT" is a 40' open-top container: the type identifies it.
    kind = match['type'] or match['type2']
    return match[1] + (kind.upper() if kind else 'FT')


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
            # "LONESTAR SHIPPING LLC" and "Lonestar Shipping LLC" are one company.
            bucket = names.setdefault(key, set())
            if not any(existing.casefold() == name.casefold() for existing in bucket):
                bucket.add(name)
    return names


def _sender_name(block):
    return re.sub(r"\s*<.*$", "", clean(block.get('sender'))).strip(" '\"").casefold() or None


def _supplier(block, companies, blocks=()):
    if block.get('supplier'):
        return block['supplier']
    stem = _sender_domain(block)
    if not stem:
        # A forwarded/internal copy ("From: Seller Person") may omit the
        # address; the same sender's other messages name the company.
        name = _sender_name(block)
        known = {b['supplier'] for b in blocks if b is not block and b.get('supplier')
                 and name and _sender_name(b) == name}
        return next(iter(known)) if len(known) == 1 else None
    # Only a printed company whose name spells the sender's domain stem
    # (e.g. "Example Shipping LLC" for example-shipping.com) is accepted.
    names = companies.get(re.sub(r"[^a-z]", "", stem), set())
    if len(names) == 1:
        return next(iter(names))
    # The sender display name often carries the company: "Person - Example
    # Kingston" <person@examplekingston.com>.
    display = re.sub(r"\s*<.*$", "", clean(block.get('sender'))).strip(" '\"")
    key = re.sub(r"[^a-z]", "", stem)
    for part in re.split(r"\s+[-–/|]\s+|\s*\|\s*", display):
        if len(key) >= 4 and re.sub(r"[^a-z]", "", part.lower()) == key:
            return clean(part)
    return None


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


def calculation_like(text):
    return bool(re.search(r"[x×*]\s*\d+.*=", text))


def _sections(lines):
    """Split a message into quotation sections at "Option N" headings.
    Lines before the first heading are context shared by every section."""
    shared, sections, current = [], [], None
    for line in lines:
        option = OPTION.match(line)
        if option:
            current = {'option': option[2], 'kind': option[1].title(), 'lines': []}
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
                    else _supplier(block, companies, blocks))
        for section in sections:
            section_fields = {**shared_fields, **_field_values(section['lines'])}
            priced_lines = [i for i, l in enumerate(section['lines']) if _price(l)]
            for index, line in enumerate(section['lines']):
                price = _price(line)
                if not price:
                    continue
                label, unit_price, code, quantity, uom, line_total, remainder = price
                label = re.sub(r"\(\s+", "(", label).strip(' :–—-+*/')
                trailing = TRAILING_SERVICE.match(remainder)
                term = trailing['term'].strip() if trailing and SERVICE.search(trailing['term']) else None
                freight = bool(SERVICE.search(label) or term)
                transport = not freight and bool(TRANSPORT_LINE.search(label))
                if label and not (freight or transport) and supplier:
                    # A charge listed under a location heading ("Clearance and
                    # delivery from Jebel Ali port") is named with that place.
                    # A heading is a short line naming a place ("Clearance and
                    # delivery from Jebel Ali port"), never terms prose.
                    heading = next((FROM_TO.search(l) for l in reversed(section['lines'][:index])
                                    if not _price(l) and len(l) <= 60 and FROM_TO.search(l)
                                    and FROM_TO.search(l)['a'][:1].isupper()), None)
                    if heading and not heading['b']:
                        place = _place(heading['a'])
                        name = f"{_title(label)} - {place}"
                        condition = re.sub(r"^\s*/\s*[-=]*", "", remainder).strip(" -–")
                        rows.append({
                            'row_type': 'CHARGE', 'product_name': name, 'display_name': name,
                            'quantity': quantity, 'uom': uom, 'unit_price': unit_price, 'line_total': line_total,
                            'currency': code, 'purchase_date': block['date'], 'quotation_date': block['date'],
                            'supplier_name': supplier, 'page_number': None,
                            'extra_attributes': [{'label': 'Location', 'value': place}]
                            + ([{'label': 'Condition', 'value': condition}] if re.search(r"[A-Za-z]{2}", condition) else []),
                            'source_block_id': block['id'],
                            'source_row_id': f"{block['id']}:charge:{section['option'] or 0}:{index}",
                            'source_reference': block['id'], 'source_context': line,
                            '_msg_source_verified': True, '_freight_section_verified': True,
                        })
                    continue
                if not label or not (freight or transport) or NOT_SERVICE.search(label):
                    continue
                if not supplier:
                    continue
                fields = section_fields
                if not section['option'] and len(priced_lines) > 1:
                    # Several estimates in one message: each price uses the
                    # labelled lines of its own estimate (before it, else after).
                    position = priced_lines.index(index)
                    start = priced_lines[position - 1] + 1 if position else 0
                    end = priced_lines[position + 1] if position + 1 < len(priced_lines) else len(section['lines'])
                    before = _field_values(section['lines'][start:index])
                    after = _field_values(section['lines'][index + 1:end])
                    fields = {**shared_fields, **after, **before}
                    # A route labelled once for the whole quotation (POL/POD
                    # after the charges) applies to its freight line.
                    for key in ('origin', 'destination'):
                        if not fields.get(key) and section_fields.get(key):
                            fields[key] = section_fields[key]
                if transport:
                    origin, destination = _transport_route(block, section['lines'], index, fields)
                    if not origin:
                        continue
                    detail = re.search(r"\(\s*([^)]{1,30}?)\s*\)", remainder)
                    head = _title(label) + (f" ({clean(detail[1])})" if detail and not re.search(r"\brequired\b", detail[1], re.I) else '')
                    route = f"{origin} to {destination}" if destination else f"from {origin}"
                    name = f"{head} - {route}"
                    basis = re.search(r"\bper\s+(truck|trailer|trip|container|vehicle|load)\b", remainder, re.I)
                    count = re.search(r"\(\s*(\d+)\s*(?:trucks?|trailers?|trips?|vehicles?)\s+required", remainder, re.I)
                    attrs = [{'label': 'From', 'value': origin}] + ([{'label': 'To', 'value': destination}] if destination else [])
                    condition = re.sub(r"^\s*/\s*[-=]+", "", remainder).strip(" -–")
                    if detail and condition.strip("() ") == clean(detail[1]):
                        condition = ''
                    if condition:
                        attrs.append({'label': 'Condition', 'value': condition})
                    rows.append({
                        'row_type': 'SERVICE', 'product_name': name, 'display_name': name,
                        'quantity': quantity or (count[1] if count else None),
                        'uom': uom or (basis[1].lower() if basis else None),
                        'unit_price': unit_price, 'line_total': line_total, 'currency': code,
                        'purchase_date': block['date'], 'quotation_date': block['date'],
                        'supplier_name': supplier, 'page_number': None, 'extra_attributes': attrs,
                        'source_block_id': block['id'],
                        'source_row_id': f"{block['id']}:transport:{section['option'] or 0}:{index}",
                        'source_reference': block['id'], 'source_context': line,
                        '_msg_source_verified': True, '_freight_section_verified': True,
                    })
                    continue
                fields = {key: (_place(value) if key in ('origin', 'destination') else value)
                          for key, value in fields.items()}
                size = _size(remainder) or _size(fields.get('container')) or _size(label)
                # "for 7 x 40 ft OT": seven containers at the quoted per-container rate.
                count = re.search(r"\b(\d+)\s*[x×]\s*(?=(?:20|40|45)\s*(?:ft|feet|'|’|GP|HC|HQ|OT|RF|DV)\b)", label, re.I)
                if count and not quantity:
                    quantity, uom = count[1], uom or 'container'
                    label = re.sub(r"\s*\bfor\s+" + count[1] + r"\s*[x×]\s*", " ", label, flags=re.I)
                    label = re.sub(r"\b" + count[1] + r"\s*[x×]\s*", "", label, count=1)
                # "Mode: AIR - Door to Door – DHL" gives the mode, the service
                # term and the carrier of a bare "Freight" line.
                mode_field = fields.get('mode') or ''
                if mode_field:
                    if not term:
                        found_term = re.search(r"\b(?:door|port|airport|warehouse)\s+to\s+(?:door|port|airport|warehouse)\b",
                                               mode_field, re.I)
                        term = found_term.group(0) if found_term else None
                    carrier = [part.strip() for part in re.split(r"\s+[-–—]\s+", mode_field)
                               if part.strip() and not re.search(r"\b(?:air|sea|ocean|road|land|courier|express)\b", part, re.I)
                               and not SERVICE.search(part)]
                    if carrier and len(carrier[-1]) <= 20 and not fields.get('carrier'):
                        fields = dict(fields, carrier=carrier[-1])
                head = _title(label) + (f" ({_title(term)})" if term else '')
                field_mode = re.search(r"\b(air|sea|ocean|road|land|courier|express)\b", mode_field, re.I)
                if field_mode and not re.search(r"\b(?:air|sea|ocean|road|courier|express)\b", head, re.I):
                    head = f"{field_mode.group(1).title()} {head}" if re.search(r"\bfreight\b", head, re.I) \
                        else f"{field_mode.group(1).title()} Freight {head}"
                # Mode comes from the heading above the price only, never from
                # an inclusions list printed after it.
                mode = MODE.search('\n'.join(section['lines'][:index])) or (
                    MODE.search('\n'.join(shared)) if section['lines'] is not shared else None)
                if mode and not re.search(r"\bfreight\b", head, re.I):
                    head = f"{mode['mode'].title()} Freight {head}"
                parts = [head]
                if fields.get('origin') and fields.get('destination'):
                    parts.append(f"{_title(fields['origin'])} to {_title(fields['destination'])}")
                if size and size != _size(label) and size not in re.sub(r"\s+", "", label).upper().replace("'", 'FT'):
                    parts.append(size)
                elif not size and section['option']:
                    parts.append(f"{section.get('kind', 'Option')} {section['option']}")
                name = ' - '.join(parts)
                attrs = [{'label': LABELS[key], 'value': fields[key]}
                         for key in ('origin', 'destination', 'route', 'airline', 'carrier', 'term', 'weight', 'transit')
                         if fields.get(key)]
                if size:
                    attrs.append({'label': 'Container size', 'value': size})
                if section['option']:
                    attrs.append({'label': 'Quotation option', 'value': f"{section.get('kind', 'Option')} {section['option']}"})
                condition = re.sub(r"^\s*/\s*[-=]+", "", remainder)
                # "/ per Cntr" is the pricing basis, not a condition.
                condition = re.sub(r"^\s*/?\s*per\s+(?:cntr|container|unit|box|truck|trip|shipment)\b\.?", "", condition, flags=re.I)
                if trailing:
                    condition = condition.replace(trailing.group(0).strip(), '')
                condition = re.sub(SIZE, '', condition).strip(" ()-–,")
                condition = re.sub(r"^\s*/?\s*per\s+(?:cntr|container|unit|box|truck|trip|shipment)\b\.?", "", condition, flags=re.I).strip(" .")
                if re.search(r"[A-Za-z]{3}", condition) and not calculation_like(condition):
                    attrs.append({'label': 'Condition', 'value': condition})
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
        if item.get('_attachment_name'):
            kept.append(item)
            continue
        if verified_other:
            # A row read from the same priced line by another recovery step
            # (same date, price and currency) is the same offer.
            if not any(amount(item.get('unit_price')) == amount(row['unit_price'])
                       and item.get('currency') == row['currency'] and date == row['purchase_date']
                       for row in recovered):
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


# Outlook prints Calibri ligatures as unmapped glyphs ("Transporta(cid:415)on",
# "o(cid:427)er", "h(cid:425)ps"); restore the letters they stand for.
LIGATURES = {'(cid:415)': 'ti', '(cid:425)': 'tt', '(cid:427)': 'ff', '(cid:431)': 'ff'}


def _restore_ligatures(text):
    for glyph, letters in LIGATURES.items():
        text = text.replace(glyph, letters)
    return text


def pdf_text(file_bytes):
    """Visible PDF text without page-number lines ('' when unreadable)."""
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return _restore_ligatures('\n'.join('\n'.join(line for line in (page.extract_text() or '').splitlines()
                                       if not re.fullmatch(r'\s*\d+\s*', line)) for page in pdf.pages))
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
