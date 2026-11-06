"""Restore descriptions only when a priced Word table uniquely identifies a row."""
import re
from decimal import Decimal, InvalidOperation


def number(value):
    try:
        return Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        return None


def priced_rows(text):
    columns = None
    for line in text.splitlines():
        cells = [c.strip() for c in line.split('|')]
        labels = [re.sub(r'[^a-z]', '', c.lower()) for c in cells]
        description = next((i for i, c in enumerate(labels) if c in {'description', 'descriptions', 'productdescription'}), None)
        qty = next((i for i, c in enumerate(labels) if c.startswith('quantity')), None)
        price = next((i for i, c in enumerate(labels) if c.startswith('unitprice')), None)
        total = next((i for i, c in enumerate(labels) if c.startswith(('totalamount', 'linetotal'))), None)
        if description is not None:
            columns = (description, qty, price, total) if None not in (qty, price, total) else None
            continue
        if columns and len(cells) > max(columns):
            d, q, p, t = columns
            key = tuple(number(cells[i]) for i in (q, p, t))
            if cells[d] and None not in key:
                yield key, cells[d]


def restore_descriptions(result, documents):
    evidence = {}
    for document in documents:
        for key, description in priced_rows(document):
            evidence.setdefault(key, set()).add(description)
    for item in result.get('items') or []:
        key = tuple(number(item.get(k)) for k in ('quantity', 'unit_price', 'line_total'))
        candidates = evidence.get(key, set())
        if len(candidates) != 1:
            continue
        description = next(iter(candidates))
        # Require a shared model/code as well as all three numeric fields.
        codes = re.findall(r'\b[A-Za-z][A-Za-z0-9/-]*\d[A-Za-z0-9/-]*\b', description)
        existing = str(item.get('product_name') or '') + ' ' + str(item.get('extra_attributes') or '')
        if not any(code.casefold() in existing.casefold() for code in codes):
            continue
        item['product_name'] = description
        item['parent_category'] = None
    return result
