"""Bind final offer prices to physical HTML columns, not model price order."""
import re
from bs4 import BeautifulSoup
from .msg_commercial_history import clean, number, currency, MONEY, split_thread, _base


def restore_price_tables(result, html, body, sender=None, sent_date=None):
    if not html:
        return result
    blocks = split_thread(body, sender, sent_date)
    items = list(result.get('items') or [])
    for table_index, table in enumerate(BeautifulSoup(html, 'html.parser').find_all('table')):
        physical_rows = [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]
        headers = None
        for row_index, tr in enumerate(physical_rows):
            cells = tr.find_all(['td', 'th'], recursive=False)
            values = [clean(cell.get_text(' ', strip=True)) for cell in cells]
            labels = [re.sub(r'[^a-z0-9]+', ' ', v.lower()).strip() for v in values]
            qty = [i for i, v in enumerate(labels) if v in ('qty', 'quantity')]
            rates = [i for i, v in enumerate(labels) if re.fullmatch(r'unit (?:price|rate)(?: in)?(?: [a-z]{3})?', v)]
            totals = [i for i, v in enumerate(labels) if re.fullmatch(r'(?:total price|line total|row total|amount)(?: in)?(?: [a-z]{3})?', v)]
            names = [i for i, v in enumerate(labels) if v in ('model', 'description', 'item description', 'product')]
            if len(qty) == len(totals) == len(names) == 1 and rates:
                # Multiple rate columns: only the final explicitly labelled
                # unit rate directly beside quantity is unambiguous here.
                final = [i for i in rates if i + 1 == qty[0]] if len(rates) > 1 else rates
                if len(final) != 1:
                    headers = None
                    continue
                headers = (values, names[0], qty[0], final[0], totals[0])
                continue
            if not headers or len(values) != len(headers[0]) or any(int(c.get('colspan', 1)) != 1 or int(c.get('rowspan', 1)) != 1 for c in cells):
                continue
            labels, ni, qi, ri, ti = headers
            name, rate, total, quantity = values[ni], number(values[ri]), number(values[ti]), number(values[qi])
            if not name or rate is None or total is None or quantity is None:
                continue
            # Bind the table to its own dated email, not the top-level sender.
            owners = [b for b in blocks if name in clean(b['body']) and values[ri] in clean(b['body'])]
            if len(owners) != 1:
                continue
            owner = owners[0]
            codes = {currency(m.group()) for m in re.finditer(MONEY, labels[ri], re.I)}
            code = next(iter(codes)) if len(codes) == 1 else None
            attrs = []
            financial_headers = sum(bool(re.search(r'\b(?:price|cost|discount|hike|amount)\b', h, re.I)) for h in labels)
            first_financial = next((i for i, h in enumerate(labels) if re.search(r'\b(?:price|cost)\b', h, re.I)), ri)
            for i, value in enumerate(values):
                if i in (ni, qi, ri, ti) or not value:
                    continue
                monetary = bool(re.search(r'\b(?:price|cost|discount|hike|amount)\b', labels[i], re.I))
                # Unlabelled add-on amounts inside a price-build-up region
                # are financial, not specifications. Keep them in audit data.
                technical = bool(re.search(r'\b(?:part|model|size|dimension|length|width|height|voltage|power|quantity|qty)\b', labels[i], re.I))
                monetary |= financial_headers >= 2 and first_financial < i < ri and not technical and number(value.rstrip('%')) is not None
                if not monetary:
                    attrs.append({'label': labels[i], 'value': value})
            canonical = _base(owner, name, rate, code, quantity, total=total, attrs=attrs)
            canonical['source_row_id'] = f"{owner['id']}:html:{table_index}:{row_index}"
            canonical['_source_price_columns'] = [{'index': i, 'label': h, 'value': v}
                                                   for i, (h, v) in enumerate(zip(labels, values))]
            matches = [i for i, item in enumerate(items)
                       if not (item.get('_attachment_name') or item.get('attachment_name'))
                       and clean(item.get('product_name')).split(' | ')[0].casefold() == name.casefold()
                       and item.get('purchase_date') in (None, '', owner['date'])]
            if len(matches) == 1:
                previous = items[matches[0]]
                canonical['supplier_name'] = canonical.get('supplier_name') or previous.get('supplier_name')
                canonical['currency'] = canonical.get('currency') or previous.get('currency')
                items[matches[0]] = canonical
            elif not matches:
                items.append(canonical)
    result['items'] = items
    return result
