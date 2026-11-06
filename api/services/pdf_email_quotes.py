"""Source-backed unit-price offers in PDFs containing printed email threads."""
import io
import re
import unicodedata
import pdfplumber
from .msg_commercial_history import split_thread, clean, number, currency, MONEY, NUMBER, _base, is_enquiry


def recover_pdf_email_quotes(result, file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        # Join before splitting messages: a signature or product may continue
        # on the next PDF page without a repeated email header.
        text = '\n'.join('\n'.join(line for line in (p.extract_text() or '').splitlines()
                                   if not re.fullmatch(r'\s*\d+\s*', line)) for p in pdf.pages)
    from .alternative_currency_quotes import collapse_currency_equivalents
    result = collapse_currency_equivalents(result, text)
    if len(re.findall(r'(?im)^From:', text)) < 2 or not re.search(r'(?im)^Sent:', text):
        return result
    text = unicodedata.normalize('NFKC', text)
    blocks = split_thread(text)
    suppliers = {}
    for block in blocks:
        address = re.search(r'@([\w-]+)\.[\w.]+', str(block.get('sender') or ''))
        if not address:
            continue
        domain = address[1]
        # Do not invent a company by title-casing an email address. Require
        # its corporate domain stem to be printed as a name in its own body.
        mention = re.search(r'\b' + re.escape(domain) + r'\b', block['body'], re.I)
        if mention and not re.search(r'https?://|www\.|@', block['body'][max(0, mention.start()-10):mention.start()]):
            suppliers.setdefault(domain.lower(), set()).add(mention[0].upper())
    codes = {i.get('currency') for i in result.get('items') or [] if i.get('currency')}
    default_code = next(iter(codes)) if len(codes) == 1 else None
    recovered = []
    for block in blocks:
        address = re.search(r'@([\w-]+)\.[\w.]+', str(block.get('sender') or ''))
        names = suppliers.get(address[1].lower(), set()) if address else set()
        if not block['date'] or is_enquiry(block):
            continue
        lines = block['body'].splitlines()
        for index, line in enumerate(lines):
            marker = re.search(r'\bunit\s+price\b', line, re.I)
            if not marker or not line[:marker.start()].strip():
                continue
            identity = re.sub(r'^\d+\s*[.,、)]\s*', '', line[:marker.start()]).strip(' ,-–')
            qty = re.search(r'\s*[-–—,]?\s*(\d+(?:\.\d+)?)\s*(Nos?\.?|PCS?|EA)\s*$', identity, re.I)
            quantity, uom = (qty[1], qty[2]) if qty else (None, None)
            if qty:
                identity = identity[:qty.start()].strip(' ,-–')
            if not identity or not re.search(r'[A-Za-z].*\d|\d.*[A-Za-z]', identity):
                continue
            tail = line[marker.end():]
            if index + 1 < len(lines) and re.match(r'^Unit\s+price\s*:', lines[index+1], re.I):
                price_text = lines[index+1]
            else:
                price_text = tail
            price = re.search(rf'(?P<currency>{MONEY})\s*(?P<amount>{NUMBER})', price_text, re.I)
            if not price:
                continue
            # An explanatory sentence quoting somebody else's prices is not
            # a product heading, even if it mentions a unit price.
            if re.search(r'\b(?:please|whether|whereas|indicated|confirm|recheck)\b', identity, re.I):
                continue
            attrs = []
            warranty = re.search(r'(\d+)\s*[- ]?\s*years?\s+warranty', tail, re.I)
            if warranty:
                attrs.append({'label': 'Warranty', 'value': warranty[1] + ' years'})
            supplier = next(iter(names)) if len(names) == 1 else None
            row = _base(dict(block, supplier=supplier), identity, number(price['amount']),
                        currency(price['currency']) or default_code, quantity, uom, attrs=attrs)
            row['source_row_id'] = f"{block['id']}:line:{index}"
            row['_supplier_identity_uncertain'] = supplier is None
            recovered.append(row)
    if not recovered:
        return result
    # Remove only model rows for identities recovered from the email source;
    # preserve other attachments/independent products in mixed documents.
    def identifiers(name):
        return {word.lower().strip('-') for word in re.findall(r'[A-Za-z0-9®][A-Za-z0-9®-]{7,}', str(name or '')) if re.search(r'\d', word)}
    def normalized(name):
        return re.sub(r'\W+', '', str(name or '').split('|')[0]).casefold()
    keys = set().union(*(identifiers(row['product_name']) for row in recovered))
    identities = {normalized(row['product_name']) for row in recovered}
    retained = [row for row in result.get('items') or []
                if row.get('_attachment_name') or (not identifiers(row.get('product_name')).intersection(keys)
                   and normalized(row.get('product_name')) not in identities)]
    result = dict(result, items=recovered + retained, supplier_name=None, document_date=None)
    result['_source_email_quote_count'] = len(recovered)
    if any(r.get('_supplier_identity_uncertain') for r in recovered):
        result['_needs_review'] = result['_gemini_needs_review'] = True
        result['_review_reasons'] = list(dict.fromkeys([*(result.get('_review_reasons') or []), 'supplier_identity_uncertain']))
    return result
