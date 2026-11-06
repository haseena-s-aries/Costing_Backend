"""Recover serial-column identities and priced zero-quantity catalogue rows."""
import io
import re
import pdfplumber
from .msg_commercial_history import clean


def recover_catalog_rates(result, data):
    from .gemini_extraction_service import _decimal, build_canonical_display_name
    items = list(result.get('items') or [])
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pn, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                for hi, header in enumerate(table):
                    labels = [clean(h).lower() for h in header]
                    serial = next((i for i,h in enumerate(labels) if h in ('item','pos.','no.','sl no','s no')), None)
                    identity = next((i for i,h in enumerate(labels) if re.search(r'\b(description|name|model|type)\b',h)), None)
                    rate = next((i for i,h in enumerate(labels) if re.search(r'unit\s*price',h)), None)
                    if None in (serial, identity, rate):
                        continue
                    for row in table[hi+1:]:
                        if len(row) != len(header) or not re.fullmatch(r'\d+',clean(row[serial])) or not clean(row[identity]):
                            continue
                        matches = [item for item in items if clean(item.get('product_name')) == clean(row[serial])
                                   and _decimal(item.get('unit_price')) == _decimal(row[rate])
                                   and item.get('page_number') in (None,pn)]
                        if len(matches) == 1:
                            item = matches[0]
                            item['_source_serial_number'] = clean(row[serial])
                            item['product_name'] = clean(row[identity])
                            item['extra_attributes'] = [a for a in item.get('extra_attributes') or []
                                                        if clean(a.get('value')) != clean(row[identity])]
            lines = (page.extract_text() or '').splitlines()
            # Explicit printed header is required before interpreting numeric
            # columns. Rates, discount and total retain their own positions.
            if not any(re.search(r'Pos\..*Part number.*Description.*Qty\..*Price.*Disc\..*Total',line,re.I) for line in lines):
                continue
            row_pattern = re.compile(r'^(\d+)\s+(\S+)\s+(\d+(?:[.,]\d+)?)\s+([\d.,]+)\s+([\d.,]+)%\s+([\d.,]+)$')
            for index,line in enumerate(lines):
                m = row_pattern.match(line)
                if not m or _decimal(m[4]) is None or _decimal(m[4]) <= 0 or _decimal(m[6]) != 0:
                    continue
                description = []
                for continuation in lines[index+1:]:
                    if row_pattern.match(continuation) or re.match(r'(?:balance carried|trade register|total amount|terms and conditions)',continuation,re.I):
                        break
                    description.append(continuation)
                if not description:
                    continue
                existing = [i for i in items if m[2] in str(i.get('product_name') or '')
                            or any(clean(a.get('value')) == m[2] for a in i.get('extra_attributes') or []
                                   if re.search(r'part|article',str(a.get('label')),re.I))
                            or clean(description[0]) in clean(i.get('product_name'))]
                if len(existing) > 1:
                    continue
                suppliers = {i.get('supplier_name') for i in items if i.get('supplier_name')}
                dates = {i.get('purchase_date') for i in items if i.get('purchase_date')}
                codes = re.search(r'Total\s+([A-Z]{3})', '\n'.join(lines))
                recovered = dict(existing[0]) if existing else {}
                recovered.update(product_name=clean(' '.join(description)), row_type='PRODUCT',
                                 quantity=str(_decimal(m[3])), unit_price=str(_decimal(m[4])), line_total=str(_decimal(m[6])),
                                 page_number=pn, supplier_name=next(iter(suppliers)) if len(suppliers)==1 else result.get('supplier_name'),
                                 purchase_date=next(iter(dates)) if len(dates)==1 else result.get('document_date'),
                                 currency=codes[1] if codes else result.get('currency'),
                                 extra_attributes=[{'label':'Part Number','value':m[2]}],
                                 _msg_source_verified=True, source_row_id=f'pdf:{pn}:pos:{m[1]}')
                if existing:
                    items[items.index(existing[0])] = recovered
                else:
                    items.append(recovered)
    for item in items:
        item['display_name'] = build_canonical_display_name(item)
    result['items'] = items
    return result
