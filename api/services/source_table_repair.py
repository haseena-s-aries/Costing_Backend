"""Restore fields from visible PDF table cells using unique price anchors."""
import io
import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)


def number(value):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def text(value):
    value = str(value or '').replace('\u2010', '-').replace('\u00ad', '-')
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def restore_table_columns(result, file_bytes):
    """Never infer columns from product words; require visible cell geometry.

    Repair only a unique same-page price/quantity match. Leave ambiguous,
    scanned, or unsupported tables to the existing extraction pipeline.
    """
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            proposals = {}
            for page_no, page in enumerate(pdf.pages, 1):
                for table in page.find_tables():
                    rows = table.extract()
                    for h, header in enumerate(rows[:-1]):
                        labels = [text(v) for v in header]
                        # Some PDFs draw the header as one merged cell. Use
                        # the data-column boundaries to read printed labels.
                        if sum(bool(v) for v in labels) == 1:
                            y0, y1 = table.rows[h].bbox[1], table.rows[h].bbox[3]
                            labels = [text(page.crop((c[0], y0, c[2], y1)).extract_text()) if c else '' for c in table.rows[h+1].cells]
                        def find(pattern):
                            return next((i for i, v in enumerate(labels) if re.search(pattern, v, re.I)), None)
                        name_col = find(r'^(?:description|product(?: name)?|part name|item(?: name)?|type|品名)$')
                        price_col = find(r'^(?:unit price|u/p|unit rate|单价)')
                        if name_col is None or price_col is None:
                            continue
                        spec_col = find(r'^(?:specification|specifications|规格)$')
                        qty_col = find(r'^(?:quantity|qty)\b|^数量$')
                        unit_col = find(r'^(?:unit|uom)$|^单位$')
                        total_col = find(r'^(?:amount|line total|total price)\b|^金额$')
                        consumed = {name_col, price_col, spec_col, qty_col, unit_col, total_col}
                        for cells in rows[h+1:]:
                            def cell(col):
                                return text(cells[col]) if col is not None and col < len(cells) else None
                            price = number(cell(price_col))
                            name = cell(name_col)
                            if price is None or not name or re.match(r'^(?:total|subtotal|备注)', name, re.I):
                                continue
                            candidates = [(i, item) for i, item in enumerate(result.get('items') or [])
                                if str(item.get('page_number') or 1) == str(page_no)
                                and number(item.get('unit_price')) == price
                                and (qty_col is None or number(item.get('quantity')) == number(cell(qty_col)))]
                            if len(candidates) != 1:
                                continue
                            i, item = candidates[0]
                            spec = cell(spec_col)
                            attrs = []
                            for col, label in enumerate(labels):
                                if col in consumed or not label or re.search(r'^(?:no\.?|s/?n|serial|序号|delivery|lead time)$', label, re.I):
                                    continue
                                value = cell(col)
                                if re.fullmatch(r'model|型号|part number', label, re.I) and value:
                                    value = re.sub(r'(?<=-)\s+(?=\w)', '', value)
                                if value:
                                    attrs.append({'label': label, 'value': value})
                            update = {'row_type': 'PRODUCT', 'product_name': name + (' | ' + spec if spec else ''), 'extra_attributes': attrs}
                            if qty_col is None:
                                update['quantity'] = None
                            if unit_col is None and qty_col is None:
                                update['uom'] = None
                            if total_col is None:
                                update['line_total'] = None
                            currency = re.search(r'\b(USD|AED|RMB|CNY|EUR|GBP)\b', labels[price_col])
                            if currency:
                                update['currency'] = 'CNY' if currency[1] == 'RMB' else currency[1]
                            # Prefer a visible Latin company name when the
                            # same table also contains a translated company.
                            page_text = text(page.extract_text() or '')
                            english_company = re.search(
                                r'([A-Z][A-Za-z0-9 &.-]{2,}\s+(?:Automation|Electric|Marine|Trading|Industrial)[A-Za-z0-9 &().,\-]*?(?:Co\.?|Ltd\.?|LLC)\b)',
                                page_text,
                            )
                            if english_company:
                                update['_source_supplier_name'] = english_company.group(1).strip()
                            proposals.setdefault(i, []).append(update)
            for i, choices in proposals.items():
                if len(choices) == 1:
                    result['items'][i].update(choices[0])
                    result['items'][i]['display_name'] = choices[0]['product_name']
                    if choices[0].get('_source_supplier_name'):
                        result['items'][i]['supplier_name'] = choices[0]['_source_supplier_name']
                    result['items'][i].pop('_source_supplier_name', None)
    except Exception:
        logger.exception('PDF source table restoration failed')
    return result
