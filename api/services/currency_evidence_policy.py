"""Do not accept a model-inferred currency for an image without evidence."""
# Fields that hold text printed on the row itself (e.g. "380euro/pcs").
# Supplier, country and language are never currency evidence.
ROW_TEXT_FIELDS = ('source_context', 'unit_price', 'line_total', 'uom')


def require_image_currency_evidence(result):
    from api.services.gemini_extraction_service import _valid_currency
    document_currency = _valid_currency(result.get('currency_evidence'))
    item_currencies = []
    for item in result.get('items') or []:
        currency = _valid_currency(item.get('currency_evidence'))
        if not currency:
            row_text = ' '.join(str(item.get(field)) for field in ROW_TEXT_FIELDS
                                if isinstance(item.get(field), str))
            currency = _valid_currency(row_text)
        item_currencies.append(currency)
    if not document_currency:
        found = {c for c in item_currencies if c}
        # Every priced row printing the same currency is document evidence.
        if len(found) == 1 and all(item_currencies):
            document_currency = found.pop()
    result['currency'] = document_currency
    for item, currency in zip(result.get('items') or [], item_currencies):
        currency = currency or document_currency
        item['currency'] = currency
        item['_source_currency_missing'] = not bool(currency)
    return result
