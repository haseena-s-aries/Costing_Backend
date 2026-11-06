"""Collapse explicit currency equivalents, never unrelated priced offers."""
import re
import unicodedata
from decimal import Decimal
from .msg_commercial_history import number


def _identity(value):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def collapse_currency_equivalents(result, source):
    """Keep the first printed currency only when source explicitly says A=B.

    Prices are never converted or compared using exchange-rate estimates.
    Ambiguous identity, duplicate rows or quote ownership stays untouched.
    """
    code_pattern = r'(?:CNY|RMB|yuan|USD|US\$|AED|EUR|GBP|INR|SGD|JPY|\$|€|£)'
    amount_pattern = r'\d+(?:[,.]\d+)*'
    atom = re.compile(rf'^\s*(?:(?P<before>{code_pattern})\s*(?P<a>{amount_pattern})|(?P<b>{amount_pattern})\s*(?P<after>{code_pattern}))(?:\s*/\s*(?P<unit>[A-Za-z]+))?\s*$', re.I)
    aliases = {'YUAN': 'CNY', 'RMB': 'CNY', '$': 'USD', 'US$': 'USD', '€': 'EUR', '£': 'GBP'}
    items = list(result.get('items') or [])
    removed = set()
    groups = {}
    for index, item in enumerate(items):
        if item.get('_attachment_name') or item.get('attachment_name'):
            continue
        key = (_identity(item.get('product_name')), _identity(item.get('supplier_name')),
               str(item.get('purchase_date') or item.get('document_date') or ''),
               number(item.get('quantity')), _identity(item.get('uom') or item.get('unit_of_measurement')))
        groups.setdefault(key, []).append(index)
    for line in unicodedata.normalize('NFKC', str(source or '')).splitlines():
        heading = re.match(r'^\s*(.+?)\s+unit\s+(?:price|rate)\s*[:：]\s*(.+?)\s*$', line, re.I)
        if not heading or '=' not in heading[2]:
            continue
        equivalents = []
        for part in heading[2].split('='):
            match = atom.fullmatch(part)
            if not match:
                equivalents = []
                break
            code = (match['before'] or match['after']).upper()
            value = number(match['a'] or match['b'])
            if value is None:
                equivalents = []
                break
            equivalents.append((aliases.get(code, code), Decimal(value)))
        if len(equivalents) < 2 or len({c for c, _ in equivalents}) != len(equivalents):
            continue
        candidates = []
        for key, indexes in groups.items():
            if key[0] != _identity(heading[1]) or len(indexes) != len(equivalents):
                continue
            by_price = {}
            for index in indexes:
                item = items[index]
                value = number(item.get('unit_price'))
                if value is not None:
                    by_price.setdefault((str(item.get('currency') or '').upper(), Decimal(value)), []).append(index)
            if set(by_price) == set(equivalents) and all(len(v) == 1 for v in by_price.values()):
                candidates.append(by_price)
        if len(candidates) != 1:
            continue
        matched = candidates[0]
        keep = matched[equivalents[0]][0]
        items[keep]['_alternative_currency_prices'] = [
            {'currency': code, 'unit_price': str(value)} for code, value in equivalents[1:]]
        items[keep]['_currency_equivalence_source'] = line.strip()
        removed.update(matched[pair][0] for pair in equivalents[1:])
    if removed:
        result['items'] = [item for index, item in enumerate(items) if index not in removed]
        result['_currency_equivalent_rows_removed'] = len(removed)
        result['visible_priced_row_count'] = len(result['items'])
        codes = {i.get('currency') for i in result['items'] if i.get('currency')}
        result['currency'] = next(iter(codes)) if len(codes) == 1 else None
    return result
