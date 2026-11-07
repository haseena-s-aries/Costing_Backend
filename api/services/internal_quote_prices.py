"""Prices our own organisation quoted to a client inside an email thread.

When a message in the thread is written by one of our own domains
(settings.INTERNAL_COMPANY_DOMAINS) and quotes a price to a client, the
price is ours, not a supplier offer:

- supplier_name is null unless the actual provider is named (never the
  email sender or recipient); the client is kept as buyer_name / Customer;
- each price keeps the date of the message that first quoted it; the same
  price re-sent for the same vessel/project is one row, while the same price
  for a different vessel/project stays a separate row;
- the description is the priced phrase without sentence filler
  ("The ... is USD 3,450" -> "Transportation charge (direct delivery ...)");
- quantity is only set when printed;
- vessel, subject and the original email are kept as metadata.

Model rows for the same price are replaced. A buyer confirming a supplier's
offer is handled by freight_quote_sections and is skipped here.
No supplier, file or route specific rules.
"""
import logging
import re
from decimal import Decimal

from django.conf import settings

from .msg_commercial_history import (
    AMOUNT, AMOUNT_AFTER, clean, number, currency, split_thread, signature_company,
)
from .freight_quote_sections import CONFIRMATION, _commercial_lines
from .price_calculation import calculation
from .commercial_consistency import TRANSPORT

logger = logging.getLogger(__name__)

TOTAL = re.compile(r"\b(?:total|sub\s*total|grand\s+total|invoice\s+value|order\s+value|cargo\s+value|"
                   r"CIF\s+value|insured\s+value|exchange\s+rate|budget)\b", re.I)
FILLER_START = re.compile(r"^(?:(?:kindly|please|pls)\s+note\s+(?:that\s+)?|note\s*:\s*|"
                          r"further\s+to\s+[^,]{0,40},\s*|as\s+discussed,?\s*|the\s+)", re.I)
FILLER_END = re.compile(r"\s*(?:\bis\b|\bare\b|\bwill\s+be\b|\bwould\s+be\b|\bshall\s+be\b|\bcomes?\s+to\b|"
                        r"\bamounts?\s+to\b|\bat\b|\bof\b|[=:–—-])\s*$", re.I)
DETAIL = re.compile(r"\s+(?:using|with|for|via)\s+|,\s*", re.I)
# A priced email line naming a movement of goods is a service.
MOVEMENT = re.compile(r"\b(?:transport\w*|shipping|shipment|freight|delivery|courier|logistics|trucking|haulage)\b", re.I)
CHARGE = re.compile(r"\b(?:charges?|fees?|costs?|transport\w*|freight|delivery|services?|hire|rental|handling)\b", re.I)


def _domains(text):
    return {d.lower() for d in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", str(text or ''))}


# Used when settings.INTERNAL_COMPANY_DOMAINS is not configured.
DEFAULT_INTERNAL_DOMAINS = ('arteqworld.com', 'ariesmar.com', 'ariesgroupglobal.com', 'ariesenergyglobal.com',
                            'ariesenergygroup.com', 'ariesofs.ae', 'aries-network.com')


def _internal(domain):
    internal = getattr(settings, 'INTERNAL_COMPANY_DOMAINS', None) or DEFAULT_INTERNAL_DOMAINS
    return bool(domain) and any(domain == d or domain.endswith('.' + d) for d in internal)


def _header(block, name):
    match = re.search(rf"^{name}\s*:\s*(.*)$", block['text'], re.I | re.M)
    return clean(match[1]) if match else ''


def _sender_domain(block):
    found = _domains(block.get('sender'))
    if not found:
        tail = re.split(r"(?i)regards", block['body'])
        found = _domains(tail[-1]) if len(tail) > 1 else set()
    return next(iter(found)) if len(found) == 1 else None


def _subject(block):
    return re.sub(r"^(?:(?:re|fw|fwd|aw|wg)\s*:\s*)+", "", _header(block, 'Subject'), flags=re.I).strip()


def _vessel(subject, body):
    explicit = re.search(r"\b(?:M\.?\s?V\.?|M/V|M\.?\s?T\.?|vessel(?:\s+name)?)\s*[:.-]?\s*"
                         r"([A-Z][A-Z0-9.-]*(?:\s+[A-Z0-9][A-Z0-9.-]*){0,3})\b", f"{subject}\n{body}")
    if explicit:
        return clean(explicit[1])
    words = subject.split()
    lead = []
    for word in words:
        if re.fullmatch(r"[A-Z][A-Z0-9.]*", word) and len(lead) < 4:
            lead.append(word)
        else:
            break
    # A leading upper-case name followed by ordinary words / " - " is the
    # vessel or project name ("ONE AQUILA Mooring fittings ...").
    rest = words[len(lead):]
    if 2 <= len(lead) <= 4 and rest and (rest[0] in {'-', '–', '/'} or not rest[0].isupper()):
        return ' '.join(lead)
    return None


def _customer(block, blocks):
    recipients = _domains(_header(block, 'To'))
    for other in blocks:
        if other is not block and _sender_domain(other) in recipients and not _internal(_sender_domain(other)):
            company = signature_company(other['body'])
            if company:
                return company
    return None


def _description(label, vessel=None):
    text = re.sub(r"\s*\bx\s*\d+(?:\.\d+)?\s*(?:nos?|pcs?|units?|sets?)?\.?\s*$", "", clean(label), flags=re.I)
    previous = None
    while previous != text:
        previous = text
        text = FILLER_START.sub('', text).strip()
        text = FILLER_END.sub('', text).strip()
    parts = DETAIL.split(text, maxsplit=1)
    main = parts[0].strip(' ,')
    detail = parts[1].strip(' ,') if len(parts) > 1 else ''
    name = main[:1].upper() + main[1:]
    # The vessel/project identifies which shipment this price belongs to.
    if vessel and vessel.casefold() not in text.casefold():
        name = f"{name} - {vessel}"
    if detail and len(detail.split()) <= 6:
        name = f"{name} ({detail})"
    return name


def _priced(line):
    first = min((m for m in (AMOUNT.search(line), AMOUNT_AFTER.search(line)) if m),
                key=lambda m: m.start(), default=None)
    if not first:
        return None
    calc = calculation(line)
    if calc:
        return line[:first.start()], calc['unit_price'], currency(first['currency']), calc['quantity'], calc['uom'], calc['line_total']
    qty = re.search(r"\b(?:qty|quantity)\s*[:=]?\s*(\d+(?:\.\d+)?)\b|\bx\s*(\d+(?:\.\d+)?)\s*(?:nos?|pcs?|units?|sets?)\b", line, re.I)
    return (line[:first.start()], number(first['amount']), currency(first['currency']),
            (qty[1] or qty[2]) if qty else None, None, None)


def internal_quote_rows(body, sender=None, sent_date=None):
    blocks = split_thread(body, sender, sent_date)
    rows = []
    for block in blocks:
        domain = _sender_domain(block)
        if not block['date'] or not _internal(domain) or CONFIRMATION.search(block['text']):
            continue
        subject = _subject(block)
        vessel = _vessel(subject, block['body'])
        customer = _customer(block, blocks)
        for index, line in enumerate(_commercial_lines(block)):
            priced = _priced(line)
            if not priced:
                continue
            label, unit_price, code, quantity, uom, line_total = priced
            if not unit_price or TOTAL.search(label) or not re.search(r"[A-Za-z]{3}", label):
                continue
            name = _description(label, vessel)
            if not name:
                continue
            # Customer / vessel / email are metadata, not item identity.
            reference = '; '.join(part for part in (
                f"Vessel: {vessel}" if vessel else '',
                f"Email {block['date']} from {clean(block.get('sender'))}: {subject}".strip(),
            ) if part)
            rows.append({
                'row_type': ('SERVICE' if TRANSPORT.search(name) or MOVEMENT.search(name)
                             else 'CHARGE' if CHARGE.search(name) else 'PRODUCT'),
                'product_name': name, 'display_name': name,
                'quantity': quantity, 'uom': uom, 'unit_price': unit_price, 'line_total': line_total,
                'currency': code, 'purchase_date': block['date'], 'quotation_date': block['date'],
                'supplier_name': None, 'buyer_name': customer, 'page_number': None,
                'extra_attributes': [],
                'source_block_id': block['id'], 'source_row_id': f"{block['id']}:internal:{index}",
                'source_reference': reference, 'source_context': line, 'vessel': vessel,
                '_source_email': {'from': clean(block.get('sender')), 'to': _header(block, 'To'),
                                  'sent': block['date'], 'subject': subject},
                '_internal_quote': True, '_msg_source_verified': True,
            })
    # The same price re-sent for the same vessel/project is one quotation,
    # dated when it was first quoted (the thread is newest first).
    unique = {}
    for row in sorted(rows, key=lambda r: r['purchase_date']):
        context = row['vessel'] or row['_source_email']['subject'].casefold()
        unique.setdefault((Decimal(row['unit_price']), row['currency'], context), row)
    return sorted(unique.values(), key=lambda r: r['purchase_date'], reverse=True)


def restore_internal_quotes(result, body, sender=None, sent_date=None):
    rows = internal_quote_rows(body, sender, sent_date)
    logger.info('INTERNAL_QUOTE_PRICES rows=%s internal_domains=%s', len(rows),
                ','.join(getattr(settings, 'INTERNAL_COMPANY_DOMAINS', None) or DEFAULT_INTERNAL_DOMAINS))
    if not rows:
        return result

    def amount(value):
        value = number(value)
        return Decimal(value) if value is not None else None

    prices = {amount(r['unit_price']) for r in rows} | {amount(r['line_total']) for r in rows if r['line_total']}
    codes = {r['currency'] for r in rows}
    kept = []
    for item in result.get('items') or []:
        verified = item.get('_attachment_name') or item.get('_freight_section_verified') or item.get('_explicit_email_rate')
        if not verified and amount(item.get('unit_price')) in prices and (item.get('currency') in codes or not item.get('currency')):
            continue
        kept.append(item)
    items = rows + kept
    result = dict(result, items=items)
    suppliers = {i.get('supplier_name') for i in items if i.get('supplier_name')}
    result['supplier_name'] = next(iter(suppliers)) if len(suppliers) == 1 else None
    customers = {r['buyer_name'] for r in rows if r.get('buyer_name')}
    if len(customers) == 1:
        result['buyer_name'] = next(iter(customers))
    result['document_date'] = max(r['purchase_date'] for r in rows)
    result['_internal_quote_count'] = len(rows)
    result['subtotal'] = result['grand_total'] = None
    return result
