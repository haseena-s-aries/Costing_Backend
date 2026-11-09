"""Recover priced rows of clearly ruled PDF tables that the model skipped.

A table qualifies when its header row names a description column and a
price column (a currency code such as USD/AED, or Amount/Rate/Price/Charge).
Each data row with a description and a numeric price becomes a row with
that currency, its "Unit Type"/"Unit"/"Per" cell as the unit and its
comments/remarks as an attribute. A recovered row is only added when no
extracted row already has the same price, currency and a matching name, so
model rows are never duplicated or replaced.
"""
import io
import re
from decimal import Decimal, InvalidOperation

CURRENCY = re.compile(r"^(USD|AED|EUR|GBP|SGD|CNY|RMB|INR|SAR|QAR|KWD|OMR|BHD|JPY|AUD|CAD)$", re.I)
DESCRIPTION = re.compile(r"^(?:description|charge(?:s)?(?:\s+name)?|item(?:\s+description)?|particulars|service(?:s)?|details)$", re.I)
PRICE = re.compile(r"^(?:amount|rate|price|unit\s+price|charge|cost)(?:\s*\(?([A-Z]{3})\)?)?$", re.I)
UNIT = re.compile(r"^(?:unit(?:\s+type)?|uom|per|basis)$", re.I)
NOTE = re.compile(r"^(?:comments?|remarks?|notes?)$", re.I)
STOP = re.compile(r"^\s*(?:sub\s*total|grand\s+total|total|net\s+amount|vat|tax)\b", re.I)


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value):
    text = _clean(value).replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _unit(value):
    text = _clean(value)
    match = re.match(r"^per\s+(.+)$", text, re.I)
    return (match[1] if match else text).replace(" /PER ", "/").lower() or None


def table_rows(file_bytes):
    import pdfplumber
    rows = []
    try:
        pdf = pdfplumber.open(io.BytesIO(file_bytes))
    except Exception:
        return rows
    with pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables() or []:
                if not table:
                    continue
                header = [_clean(cell) for cell in table[0]]
                desc = next((i for i, h in enumerate(header) if DESCRIPTION.match(h)), None)
                price, code = None, None
                for i, h in enumerate(header):
                    if CURRENCY.match(h):
                        price, code = i, h.upper()
                        break
                    match = PRICE.match(h)
                    if match and price is None:
                        price, code = i, (match[1] or '').upper() or None
                if desc is None or price is None:
                    continue
                unit = next((i for i, h in enumerate(header) if UNIT.match(h)), None)
                note = next((i for i, h in enumerate(header) if NOTE.match(h)), None)
                if note is None:
                    # An unlabelled text column after the price holds comments.
                    note = next((i for i in range(price + 1, len(header)) if not header[i]
                                 and any(_clean(r[i]) for r in table[1:] if i < len(r))), None)
                for raw in table[1:]:
                    cells = [_clean(c) for c in raw]
                    name = cells[desc] if desc < len(cells) else ''
                    amount = _number(cells[price]) if price < len(cells) else None
                    if not name or amount is None or STOP.match(name):
                        continue
                    attrs = []
                    comment = cells[note] if note is not None and note < len(cells) else ''
                    if comment:
                        attrs.append({'label': 'Comments', 'value': comment})
                    rows.append({
                        'row_type': 'CHARGE', 'product_name': name, 'display_name': name,
                        'quantity': None, 'uom': _unit(cells[unit]) if unit is not None and unit < len(cells) else None,
                        'unit_price': str(amount), 'line_total': None, 'currency': code,
                        'page_number': page_number, 'extra_attributes': attrs,
                        'source_row_id': f"pdf-table:{page_number}:{len(rows)}",
                        '_pdf_table_row': True,
                    })
    return rows


def recover_table_rows(result, file_bytes):
    recovered = table_rows(file_bytes)
    if not recovered:
        return result
    items = list(result.get('items') or [])

    def tokens(value):
        return set(re.findall(r"[a-z0-9]{3,}", str(value or '').split('|')[0].casefold())) - {'charges', 'charge'}

    def amount(value):
        try:
            return Decimal(str(value).replace(',', ''))
        except (InvalidOperation, ValueError):
            return None

    added = []
    for row in recovered:
        present = any(amount(item.get('unit_price')) == Decimal(row['unit_price'])
                      and (not item.get('currency') or not row['currency'] or item.get('currency') == row['currency'])
                      and (tokens(item.get('product_name')) & tokens(row['product_name'])
                           or not tokens(row['product_name']))
                      for item in items)
        if present:
            continue
        row['supplier_name'] = result.get('supplier_name')
        row['purchase_date'] = result.get('document_date')
        row['currency'] = row['currency'] or result.get('currency')
        added.append(row)
    if added:
        result = dict(result, items=items + added)
        result['_pdf_table_rows_added'] = len(added)
    return result
