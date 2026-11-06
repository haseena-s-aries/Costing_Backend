"""Do not accept a model-inferred currency for an image without evidence."""
def require_image_currency_evidence(result):
    from api.services.gemini_extraction_service import _valid_currency
    document_currency = _valid_currency(result.get('currency_evidence'))
    result['currency'] = document_currency
    for item in result.get('items') or []:
        currency = _valid_currency(item.get('currency_evidence')) or document_currency
        item['currency'] = currency
        item['_source_currency_missing'] = not bool(currency)
    return result
