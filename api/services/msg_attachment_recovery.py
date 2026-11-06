"""Source-backed PDF attachment rows; each attachment owns its metadata."""
import copy
import io
import logging
import re
from decimal import Decimal

import pdfplumber
from dateutil import parser as dates

from api.services.commercial_attribute_policy import commercial_field
from api.services.pdf_commercial_geometry import clean, cell, amount, company, text_lines

logger = logging.getLogger(__name__)


def metadata(text):
    candidates = []
    for line in text.splitlines():
        line = clean(line)
        if re.match(r'(?i)^(?:to\b|customer\b|client\b|attention\b|:)', line):
            continue
        # A bilingual legal name can follow its translated company name.
        english = re.sub(r'^.*[\u3400-\u9fff]\s*', '', line)
        explicit = bool(re.match(r'(?i)^from\s+', english))
        name = company(re.sub(r'(?i)^from\s+', '', english))
        if name:
            candidates.append((2 if explicit else 1, name))
    suppliers = {name for priority, name in candidates if priority == max((p for p, _ in candidates), default=0)}
    supplier = next(iter(suppliers)) if len(suppliers) == 1 else None
    match = re.search(r'(?im)(?:\b(?:quotation\s+|quote\s+|offer\s+)?date|日期)\s*[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})', text)
    date = None
    if match:
        raw = re.sub('[年月]', '-', match[1]).replace('日', '')
        try:
            date = dates.parse(raw, dayfirst=not bool(re.match(r'^\d{4}', raw))).date().isoformat()
        except ValueError:
            pass
    return supplier, date


def schemas(page):
    for table in page.find_tables():
        rows = table.extract()
        for h, row in enumerate(rows[:10]):
            columns = []
            for index, label in enumerate(row):
                box = table.rows[h].cells[index]
                if not label or not box:
                    continue
                # First physical header line retains original language/label.
                first = str(label).splitlines()[0]
                semantic = commercial_field(label) or commercial_field(first)
                key = re.sub(r'[^a-z]', '', first.casefold())
                if key in {'description', 'name', 'productdescription'}:
                    semantic = 'product_name'
                elif key in {'item', 'sl', 'slno', 'serial', 'srno', 'sno'} or first.strip() == '#':
                    semantic = 'serial'
                    if key == 'item' and any(c['field'] == 'serial' for c in columns):
                        semantic = None
                columns.append(dict(label=clean(label), field=semantic, x=(box[0], box[2]), bottom=box[3]))
            if any(c['field'] == 'product_name' for c in columns) and any(c['field'] == 'quantity' for c in columns):
                yield table, columns, max(c['bottom'] for c in columns)
                break


def currency(text):
    tokens = set(re.findall(r'\b(?:USD|CNY|RMB|AED|EUR|GBP|JPY)\b', text, re.I))
    tokens = {'CNY' if t.upper() == 'RMB' else t.upper() for t in tokens}
    return next(iter(tokens)) if len(tokens) == 1 else None


def serial_key(value):
    try:
        return str(Decimal(value).normalize())
    except Exception:
        return None


def parse_attachment(data, name):
    items, details = [], {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        supplier, date = metadata(full_text)
        source_type = 'invoice' if re.search(r'(?im)^\s*(?:tax\s+|commercial\s+)?invoice\b', full_text) else 'quotation'
        category = 'INVOICE' if source_type == 'invoice' else 'QUOTATION'
        for pn, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            for table, columns, top in schemas(page):
                fields = {c['field']: c for c in columns if c['field']}
                serial = fields.get('serial')
                description = fields['product_name']
                priced = any(f in fields for f in ('unit_price', 'line_total'))
                # Detail-only tables link by explicit source serial, never by
                # a model-generated product name or a matching price.
                if not priced:
                    if not serial:
                        continue
                    anchors = [w for w in words if top <= w['top'] < table.bbox[3]
                               and serial['x'][0] <= w['x0'] < serial['x'][1] and serial_key(w['text'])]
                    for j, anchor in enumerate(anchors):
                        end = anchors[j+1]['top']-2 if j+1 < len(anchors) else table.bbox[3]
                        value = cell(page, description['x'], anchor['top']-2, end)
                        if value:
                            details.setdefault(serial_key(anchor['text']), []).append(value)
                    continue
                # Use serial/quantity anchors even when many rows share one
                # enormous PDF cell; prices cannot shift past a blank cell.
                anchor_columns = [c for c in columns if c['field'] in {'serial', 'quantity', 'unit_price', 'line_total'}]
                ys = []
                for w in sorted(words, key=lambda w: w['top']):
                    if not top <= w['top'] < table.bbox[3] or not amount(w['text']):
                        continue
                    if not any(c['x'][0] <= (w['x0']+w['x1'])/2 < c['x'][1] for c in anchor_columns):
                        continue
                    if not ys or w['top']-ys[-1] > 8:
                        ys.append(w['top'])
                parent = None
                for j, y in enumerate(ys):
                    low = top if j == 0 else (ys[j-1]+y)/2 + 3
                    high = (y+ys[j+1])/2+3 if j+1 < len(ys) else table.bbox[3]
                    center = sum(description['x'])/2
                    boxes = {box for row in table.rows for box in row.cells
                             if box and box[0] <= center < box[2] and box[1] <= y+4 < box[3]}
                    exact = [box for box in boxes if sum(box[1] <= yy+4 < box[3] for yy in ys) == 1]
                    if len(exact) == 1:
                        low, high = exact[0][1], exact[0][3]
                    values = [(c, cell(page, c['x'], low, high)) for c in columns]
                    mapped = {c['field']: v for c, v in values if c['field']}
                    product = mapped.get('product_name', '').strip(' -')
                    if not product or re.match(r'(?i)^(?:(?:total|subtotal|grand total|amount in|vat|tax)\b|(?:in\s+)?words\s*:)', product):
                        continue
                    price, total = amount(mapped.get('unit_price')), amount(mapped.get('line_total'))
                    amount_only = price is None and total is not None
                    if amount_only:
                        price = total
                    if price is None and total is None:
                        if mapped.get('serial'):
                            parent = product
                        continue
                    attrs = [{'label': c['label'], 'value': v} for c, v in values
                             if not c['field'] and v]
                    if mapped.get('quantity') and not amount(mapped['quantity']):
                        combined = re.fullmatch(r'(\d[\d,.]*)\s+([^\d]+)', mapped['quantity'])
                        if combined and not mapped.get('uom'):
                            mapped['quantity'], mapped['uom'] = combined.groups()
                    row_type = 'CHARGE' if re.search(r'(?i)\b(?:packing|insurance|clearance|profit|freight|fee)\b', product) else 'PRODUCT'
                    remarks = ' '.join(a['value'] for a in attrs if a['label'].casefold() in {'remarks', 'remark'})
                    if re.fullmatch(r'(?i)(?:profit|markup|fee)', remarks):
                        row_type = 'CHARGE'
                    elif re.fullmatch(r'(?i)(?:internal cost|manhours?|labou?r)', remarks):
                        row_type = 'SERVICE'
                    item = dict(row_type=row_type, product_name=product, parent_category=parent,
                                quantity=mapped.get('quantity') or None, uom=mapped.get('uom') or None,
                                unit_price=price, line_total=total, extra_attributes=attrs,
                                currency=currency(' '.join(c['label'] for c in columns)) or currency(full_text),
                                supplier_name=supplier, purchase_date=date, page_number=pn,
                                source_document_type=source_type, source_category=category,
                                source_reference=f'{name}:page:{pn}:row:{y}', source_context=product,
                                _attachment_name=name, _source_serial=serial_key(mapped.get('serial')),
                                _physical_pdf_row=True, _table_source_verified=True,
                                _unit_price_from_amount_only=amount_only)
                    items.append(item)
            # Explicit extra charges below an amount table use its price
            # column and their own printed currency context (e.g. conversion).
            if items and any(i['page_number'] == pn for i in items):
                tables = list(schemas(page))
                priced_tables = [(t, cs) for t, cs, _ in tables if any(c['field'] == 'line_total' for c in cs)]
                if priced_tables:
                    table, cols = priced_tables[-1]
                    total_col = next(c for c in cols if c['field'] == 'line_total')
                    active_currency = None
                    for y, ws in text_lines(page):
                        if y < table.bbox[3]:
                            continue
                        line = clean(' '.join(w['text'] for w in ws))
                        if re.match(r'(?i)^amount\s+in\b', line):
                            active_currency = currency(line)
                            continue
                        if not active_currency:
                            continue
                        price_words = [w for w in ws if total_col['x'][0] <= (w['x0']+w['x1'])/2 < total_col['x'][1] and amount(w['text'])]
                        description = clean(' '.join(w['text'] for w in ws if w['x1'] < total_col['x'][0]))
                        if len(price_words) != 1 or not re.search(r'(?i)\b(?:cost|packing|transportation|customs?|clearance|clearence|fee)\b', description):
                            continue
                        item = copy.deepcopy(items[-1])
                        amount_value = amount(price_words[0]['text'])
                        item.update(product_name=description, parent_category=None, row_type='CHARGE',
                                    quantity=None, uom=None, unit_price=amount_value, line_total=amount_value,
                                    currency=active_currency, extra_attributes=[], _source_serial=None,
                                    _unit_price_from_amount_only=True,
                                    source_reference=f'{name}:page:{pn}:row:{y}', source_context=line)
                        items.append(item)
        for item in items:
            detail = details.get(item['_source_serial'])
            if detail:
                item['extra_attributes'].append({'label': 'Description', 'value': ' '.join(detail)})
            parts = [item['parent_category'], item['product_name']]
            parts += [a['label']+': '+a['value'] for a in item['extra_attributes'] if not commercial_field(a['label'])]
            item['display_name'] = ' | '.join(p for p in parts if p)
    return items


def recover_attachments(result, attachments, *, fallback_supplier=None, fallback_date=None):
    """Replace only confidently parsed attachment rows; preserve other sources."""
    if fallback_date:
        try:
            fallback_date = dates.parse(str(fallback_date), dayfirst=True).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            fallback_date = None
    recovered, parsed_names, unavailable = [], [], []
    existing_items = list(result.get('items') or [])
    for attachment in attachments:
        try:
            rows = parse_attachment(attachment['data'], attachment['name'])
            # An unsupported table layout still supplies seller evidence.
            # Require row evidence before replacing email-level attribution.
            from .msg_commercial_history import plausible_company, signature_company
            if not rows:
                with pdfplumber.open(io.BytesIO(attachment['data'])) as pdf:
                    source_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                supplier, _ = metadata(source_text)
                supplier = supplier or signature_company(source_text)
                if supplier and plausible_company(supplier):
                    for item in result.get('items') or []:
                        if (not plausible_company(item.get('supplier_name')) or len(attachments) == 1):
                            # Require a visible price and name overlap in
                            # this attachment before attributing its seller.
                            name_words = re.findall(r'[a-z]{3,}', str(item.get('product_name') or '').lower())
                            rate = amount(item.get('unit_price'))
                            printed = {amount(m) for m in re.findall(r'\d[\d,.]*', source_text)}
                            if rate and rate in printed and name_words and all(w in source_text.lower() for w in name_words):
                                item['supplier_name'] = supplier
                    names = {i.get('supplier_name') for i in result.get('items') or []}
                    if len(names) == 1 and supplier in names:
                        result['supplier_name'] = supplier
        except Exception:
            logger.warning('Attachment source recovery unavailable attachment=%s', attachment['name'], exc_info=True)
            unavailable.append(attachment['name'])
            continue
        if rows:
            recovered.extend(rows)
            parsed_names.append(attachment['name'])
    if not recovered:
        return result
    # If the attachment table does not repeat metadata such as currency,
    # preserve it from the same uniquely matched Gemini commercial row. Match
    # by visible amount and compatible product identity, never by position.
    def key_text(value):
        return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()

    for item in recovered:
        item_price = amount(item.get('unit_price') or item.get('line_total'))
        item_name = key_text(item.get('product_name'))
        candidates = [
            original for original in existing_items
            if item_price is not None
            and amount(original.get('unit_price') or original.get('line_total')) == item_price
            and item_name and key_text(original.get('product_name'))
            and (item_name in key_text(original.get('product_name'))
                 or key_text(original.get('product_name')) in item_name)
        ]
        if len(candidates) == 1:
            source = candidates[0]
            for field in ('currency', 'supplier_name', 'purchase_date'):
                if not item.get(field) and source.get(field):
                    item[field] = source[field]
    # All existing rows must be demonstrably covered before whole-result
    # replacement; unrelated body quotes or unsupported attachments survive.
    retained = []
    for item in result.get('items') or []:
        reference = str(item.get('source_reference') or '')
        if any(name in reference for name in parsed_names):
            continue
        visible = {amount(item.get(f)) for f in ('unit_price', 'line_total')} - {None}
        qty = amount(item.get('quantity'))
        if qty and amount(item.get('unit_price')):
            visible.add(format(Decimal(qty)*Decimal(amount(item['unit_price'])), '.2f'))
        # Text overlap only identifies an old candidate for replacement;
        # no prices, identities or supplier names are borrowed from it.
        tokens = set(re.findall(r'\w+', str(item.get('product_name') or '').casefold())) - {'the', 'of', 'and', 'for', 'with'}
        # A commercial attachment owns the row identity. The email sender may
        # be a buyer/forwarder and can therefore differ from the supplier
        # printed in the attached quotation. Once the attachment supplies a
        # supplier, do not require the body row's supplier to match it.
        matches = [r for r in recovered if visible & {r['unit_price'], r['line_total']}
                   and (r.get('supplier_name') or not item.get('supplier_name')
                        or clean(item['supplier_name']).casefold() == clean(r['supplier_name']).casefold())
                   and tokens & set(re.findall(r'\w+', (r['product_name']+' '+str(r.get('parent_category') or '')).casefold()))]
        if not matches and tokens:
            # A unique, exact source identity also repairs malformed OCR
            # amounts (e.g. a grouping separator mistaken for a decimal).
            exact = [r for r in recovered if tokens == (set(re.findall(r'\w+', r['product_name'].casefold())) - {'the', 'of', 'and', 'for', 'with'})
                     and (r.get('supplier_name') or not item.get('supplier_name')
                          or clean(item['supplier_name']).casefold() == clean(r['supplier_name']).casefold())
                     and (not qty or amount(r.get('quantity')) == qty)]
            if len(exact) == 1:
                matches = exact
        if not matches:
            retained.append(item)
    output = copy.deepcopy(result)
    output['items'] = retained + recovered
    output['_physical_pdf_complete'] = not retained
    output['_source_attachment_finalized'] = True
    if all(i.get('source_category') == 'QUOTATION' for i in recovered):
        output['document_type'], output['category'] = 'email_quote', 'QUOTATION'
    output['visible_priced_row_count'] = len(output['items'])
    for field, item_field in [('supplier_name', 'supplier_name'), ('document_date', 'purchase_date'), ('currency', 'currency')]:
        values = {i.get(item_field) for i in output['items']}
        output[field] = next(iter(values)) if len(values) == 1 else None
    output['_review_reasons'] = list(output.get('_review_reasons') or [])
    if unavailable:
        output['_unverified_attachments'] = unavailable
        # An auxiliary/certificate attachment may be unreadable while the
        # commercial quotation attachment was recovered successfully.  Keep
        # that diagnostic visible without downgrading every recovered row to
        # review.  If nothing commercial was recovered, review is required.
        if not recovered:
            output['_needs_review'] = True
            output['_review_reasons'].append('attachment_source_recovery_unavailable')
    for item in recovered:
        if not item.get('supplier_name') and fallback_supplier:
            item['supplier_name'] = fallback_supplier
        if not item.get('purchase_date') and fallback_date:
            item['purchase_date'] = fallback_date
        for field, reason in [('unit_price', 'source_unit_price_not_printed'), ('purchase_date', 'missing_attachment_date'), ('supplier_name', 'missing_attachment_supplier')]:
            if item.get(field) is None:
                output['_needs_review'] = True
                if reason not in output['_review_reasons']:
                    output['_review_reasons'].append(reason)
    if not output.get('document_date') and fallback_date:
        output['document_date'] = fallback_date
    elif output.get('document_date') is None and fallback_date:
        # A message can contain multiple attachments with different document
        # dates.  Item dates remain attachment-authoritative; the email date is
        # the safe document-level context in that mixed case.
        output['document_date'] = fallback_date
    if not output.get('supplier_name') and fallback_supplier:
        output['supplier_name'] = fallback_supplier
    return output
