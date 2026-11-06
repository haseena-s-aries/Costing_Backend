"""Physical commercial cells, independent of model order and translated names."""
import copy
import io
import re
from decimal import Decimal, InvalidOperation

import pdfplumber


PROTECTED_FIELDS = (
    "purchase_date", "source_document_type", "source_category", "_attachment_name", "_source_serial",
    "product_name", "supplier_name", "quantity", "uom", "unit_of_measurement",
    "unit_price", "line_total", "currency", "page_number", "extra_attributes",
    "display_name", "source_context", "source_reference", "parent_category",
    "_table_source_verified", "_comparison_matrix_price", "_comparison_cell_key",
    "_source_page", "_source_row_order", "_supplier_column_index", "_physical_pdf_row",
)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def amount(value):
    value = clean(value)
    if not re.fullmatch(r"(?:[A-Z]{3}\s*|[$€£]\s*)?\d[\d,]*(?:\.\d+)?", value):
        return None
    try:
        return format(Decimal(re.sub(r"[^\d.]", "", value)), ".2f")
    except InvalidOperation:
        return None


LEGAL = re.compile(r"\b(?:Co\.?\s*,?\s*Ltd\.?|Pvt\.?\s+Ltd\.?|Limited|Ltd\.?|LLC|LLP|FZE|FZC|Inc\.?|Corporation|Corp\.?|GmbH)(?!\w)", re.I)


def company(header):
    # Bilingual headers may wrap the English legal name over several lines.
    lines = [clean(v) for v in str(header or "").splitlines()]
    latin = [v for v in lines if re.search(r"[A-Za-z]", v) and not re.search(r"[\u3400-\u9fff]", v)]
    text = clean(" ".join(latin))
    match = LEGAL.search(text)
    if match:
        return text[:match.end()].strip(" ,")
    if re.fullmatch(r"Supplier\s+[\w -]+", text, re.I):
        return text
    return None


def cell(page, x, top, bottom):
    # Select glyph centers; touching grid lines and neighbouring cells do not
    # contribute text. Physical column bounds, not table array slots, own it.
    chars = [c for c in page.chars
             if x[0] <= (c['x0'] + c['x1']) / 2 < x[1]
             and top <= (c['top'] + c['bottom']) / 2 < bottom]
    return clean(pdfplumber.utils.extract_text(chars, x_tolerance=1, y_tolerance=3)) if chars else ""


def bounds(box):
    return (box[0], box[2]) if box else None


def schema_for(table):
    rows = table.extract()
    for h, labels in enumerate(rows[:8]):
        product = next((i for i, label in enumerate(labels)
                        if re.search(r"\b(?:Name|Description|Product)\b", str(label or ''), re.I)), None)
        if product is None or not table.rows[h].cells[product]:
            continue
        suppliers = []
        for i in range(len(labels)):
            for header in range(h + 1):
                name = company(rows[header][i]) if i < len(rows[header]) else None
                box = table.rows[header].cells[i] if i < len(table.rows[header].cells) else None
                if name and box:
                    suppliers.append({'supplier': name, 'x': bounds(box)})
                    break
        if len(suppliers) < 2:
            continue
        suppliers.sort(key=lambda entry: entry['x'][0])
        for i, supplier in enumerate(suppliers):
            supplier['index'] = i
        def column(pattern):
            index = next((i for i, label in enumerate(labels) if re.search(pattern, str(label or ''), re.I)), None)
            return bounds(table.rows[h].cells[index]) if index is not None else None
        qty = column(r'\b(?:Qty|Quantity)\b')
        uom = column(r'\b(?:Unit|UOM)\b')
        spec = column(r'\b(?:Specification|Model|Size)\b')
        # An unlabeled quantity column is accepted only if separated from
        # identity/specification/serial and consistently numeric in body rows.
        if qty is None and uom:
            candidates = []
            for i, box in enumerate(table.rows[h].cells):
                if box and box[0] >= uom[1] - 1 and box[2] <= suppliers[0]['x'][0]:
                    values = [clean(row[i]) for row in rows[h+1:] if i < len(row) and clean(row[i])]
                    if len(values) >= 2 and all(re.fullmatch(r'\d+(?:\.\d+)?', v) for v in values):
                        candidates.append(bounds(box))
            if len(candidates) == 1:
                qty = candidates[0]
        return dict(product=bounds(table.rows[h].cells[product]), spec=spec,
                    qty=qty, uom=uom, suppliers=suppliers, header=h, width=table.bbox[2]-table.bbox[0],
                    left=table.bbox[0], right=table.bbox[2])
    return None


def matrix_cells(pdf):
    schema = None
    items, regions = [], []
    previous_page = None
    for page_number, page in enumerate(pdf.pages, 1):
        found_on_page = False
        for table in page.find_tables():
            learned = schema_for(table)
            if learned:
                schema = learned
                start = schema['header'] + 1
            elif schema and previous_page == page_number - 1 and (
                abs(table.bbox[0]-schema['left']) < 3 and abs(table.bbox[2]-schema['right']) < 3
            ):
                # The actual grid must still contain the learned identity
                # boundary. No need for matching cell indexes or cell counts.
                edges = [c[0] for r in table.rows for c in r.cells if c]
                if not any(abs(x-schema['product'][0]) < 3 for x in edges):
                    continue
                start = 0
            else:
                continue
            page_items = []
            # Serial cells may span the last product plus several summary
            # rows. Only the description cell defines the commercial y-band.
            product_center = sum(schema['product']) / 2
            bands = sorted({(round(c[1], 3), round(c[3], 3))
                            for r in table.rows[start:] for c in r.cells
                            if c and c[0] <= product_center < c[2]})
            for top, bottom in bands:
                name = cell(page, schema['product'], top, bottom)
                if not name or re.fullmatch(r'\d+(?:\.\d+)?', name) or re.fullmatch(r'(?:final\s+)?(?:total|subtotal|discount|vat(?:\s+\d+%)?|tax)', name, re.I):
                    continue
                attrs = []
                if schema['spec']:
                    spec = cell(page, schema['spec'], top, bottom)
                    if spec:
                        attrs.append({'label': 'Specification', 'value': spec})
                quantity = cell(page, schema['qty'], top, bottom) if schema['qty'] else None
                uom = cell(page, schema['uom'], top, bottom) if schema['uom'] else None
                for supplier in schema['suppliers']:
                    price = amount(cell(page, supplier['x'], top, bottom))
                    if price is None:
                        continue
                    page_items.append(dict(
                        row_type='PRODUCT', product_name=name, quantity=quantity or None, uom=uom or None,
                        extra_attributes=copy.deepcopy(attrs), unit_price=price, line_total=price,
                        supplier_name=supplier['supplier'], page_number=page_number,
                        source_reference=f'pdf-matrix:{page_number}:{top}:{supplier["index"]}',
                        source_context=name, _source_page=page_number, _source_row_order=top,
                        _supplier_column_index=supplier['index'], _comparison_cell_key=[page_number, top, supplier['index']],
                        _table_source_verified=True, _comparison_matrix_price=True, _physical_pdf_row=True))
            if page_items:
                items.extend(page_items)
                regions.append((page_number, table.bbox))
                found_on_page = True
        if found_on_page:
            previous_page = page_number
        else:
            schema = None
    return items, regions


def text_lines(page):
    lines = []
    for word in sorted(page.extract_words(), key=lambda w: (w['top'], w['x0'])):
        match = next((line for line in reversed(lines[-3:]) if abs(line[0]-word['top']) < 3), None)
        if match is None:
            lines.append([word['top'], [word]])
        else:
            match[1].append(word)
    return [(y, sorted(words, key=lambda w: w['x0'])) for y, words in lines]


def sectional_quote_cells(pdf):
    """A numbered product block owns its following price table across pages."""
    active = None
    active_attrs = []
    items, regions = [], []
    for page_number, page in enumerate(pdf.pages, 1):
        lines = text_lines(page)
        headings = []
        for y, words in lines:
            text = clean(' '.join(w['text'] for w in words))
            match = re.fullmatch(r'(\d+)\s+([A-Za-z].*)', text)
            if (match and words[0]['x0'] < page.width * .15
                    and re.search(r'\b[A-Za-z]+\d+', match.group(2))
                    and not re.search(r'\b(?:total|discount|vat)\b', text, re.I)):
                headings.append((y, match.group(2)))
        tables = page.find_tables()
        events = sorted([(y, 'heading', name) for y, name in headings] +
                        [(t.bbox[1], 'table', t) for t in tables], key=lambda e: e[0])
        for event_index, (y, kind, value) in enumerate(events):
            if kind == 'heading':
                active = value
                active_attrs = []
                # All labelled specification lines belonging to this numbered
                # block are source attributes.  They may be on the page before
                # the price table (or before a page break), so retain them with
                # the active product until its price table is encountered.
                next_event_y = events[event_index + 1][0] if event_index + 1 < len(events) else float('inf')
                for detail_y, detail_words in lines:
                    if detail_y <= y or detail_y >= next_event_y:
                        continue
                    text_value = clean(' '.join(w['text'] for w in detail_words))
                    if not text_value or ':' not in text_value:
                        continue
                    label, value_text = text_value.split(':', 1)
                    label, value_text = clean(label), clean(value_text)
                    if (not label or not value_text or len(label) > 90
                            or re.search(r'^(?:currency|part number|price|quotation|page|due to|moq|incoterms|bank charge)', label, re.I)):
                        continue
                    if re.search(r'^(?:\*|no\.?\s*\d|\d{6}\b)', text_value, re.I):
                        continue
                    active_attrs.append({'label': label, 'value': value_text})
                continue
            table = value
            header = clean(' '.join(str(v or '') for v in (table.extract() or [[]])[0]))
            if not active or not re.search(r'\bPart\s*Number\b', header, re.I):
                continue
            if not re.search(r'Unit\s*Price|PCS|Pieces', header, re.I):
                continue
            table_items = []
            for top, words in lines:
                if not table.bbox[1] < top < table.bbox[3]:
                    continue
                prices = [w for w in words if re.fullmatch(r'[$€£]\d[\d,]*\.\d+', w['text'])]
                if not prices:
                    continue
                code = clean(' '.join(w['text'] for w in words if w['x1'] < prices[0]['x0']))
                models = re.findall(r'\b[A-Za-z]+\d+[A-Za-z\d-]*', active)
                if not code or not models or not any(code.casefold().startswith(m.casefold()) for m in models):
                    continue
                for index, price_word in enumerate(prices):
                    price = amount(price_word['text'])
                    attrs = copy.deepcopy(active_attrs)
                    attrs.append({'label': 'Part Number', 'value': code})
                    if len(prices) > 1:
                        header_words = [w for hy, ws in lines if table.bbox[1] <= hy < top for w in ws]
                        centers = [(w['x0']+w['x1'])/2 for w in prices]
                        left = (centers[index-1]+centers[index])/2 if index else prices[0]['x0']-35
                        right = (centers[index]+centers[index+1])/2 if index+1<len(prices) else table.bbox[2]
                        tier = clean(' '.join(w['text'] for w in header_words if left <= (w['x0']+w['x1'])/2 < right))
                        if tier:
                            attrs.append({'label': 'Quantity tier', 'value': tier})
                    table_items.append(dict(row_type='PRODUCT', product_name=active,
                        unit_price=price, line_total=None, quantity=None, uom=None,
                        extra_attributes=attrs, page_number=page_number,
                        source_reference=f'pdf-price-block:{page_number}:{top}:{index}', source_context=active+' '+code,
                        _table_source_verified=True, _physical_pdf_row=True,
                        _source_page=page_number, _source_row_order=top, _supplier_column_index=index))
            if table_items:
                items.extend(table_items)
                regions.append((page_number, table.bbox))
    return items, regions


def finalize_physical_pdf(result, file_bytes):
    """Return a complete verified layout, or leave candidates unchanged.

    Replacement is used only when every commercial table in the document is
    covered by the recognized layout. Mixed documents remain on the normal
    reconciliation path instead of losing unrelated rows.
    """
    from api.services.commercial_attribute_policy import commercial_field
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        items, regions = matrix_cells(pdf)
        matrix = bool(items)
        if not matrix:
            items, regions = sectional_quote_cells(pdf)
        if not items:
            return result
        covered = {(p, tuple(box)) for p, box in regions}
        for pn, page in enumerate(pdf.pages, 1):
            for table in page.find_tables():
                if (pn, tuple(table.bbox)) not in covered and re.search(r'\d+[.,]\d{2}\b', clean(str(table.extract()))):
                    return result
            if pn not in {p for p, _ in regions}:
                return result
        full_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
    currency_match = re.search(r'\b(USD|US\$|CNY|RMB|EUR|AED|GBP)\b', full_text)
    currency = {'RMB': 'CNY', 'US$': 'USD'}.get(currency_match[1], currency_match[1]) if currency_match else result.get('currency')
    if 'US$' in full_text:
        currency = 'USD'
    output = copy.deepcopy(result)
    source_supplier = next((candidate for line in full_text.splitlines()
                            if (candidate := company(line))), None)
    date_match = re.search(r'(?im)\b(?:Quotation|Offer|Document)\s+Date\s*:\s*([^\n]+)', full_text)
    source_date = clean(date_match.group(1)) if date_match else None
    if source_supplier:
        output['supplier_name'] = source_supplier
    if source_date:
        output['document_date'] = source_date
    for item in items:
        item['currency'] = currency
        item['purchase_date'] = source_date or result.get('document_date')
        item['supplier_name'] = item.get('supplier_name') or source_supplier or result.get('supplier_name')
        item['source_document_type'] = output.get('document_type')
        item['source_category'] = output.get('category')
        item['display_name'] = ' | '.join([item['product_name']] + [a['label']+': '+a['value'] for a in item['extra_attributes'] if not commercial_field(a['label'])])
    output['items'] = sorted(items, key=lambda i: (i['_source_page'], i['_source_row_order'], i['_supplier_column_index']))
    output['visible_priced_row_count'] = len(items)
    output['_physical_pdf_complete'] = True
    output['_authoritative_supplier_matrix'] = matrix
    output['currency'] = currency
    return output
