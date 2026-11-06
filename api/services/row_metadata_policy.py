"""A document default is optional when every commercial row has metadata."""


def missing_row_metadata(items, defaults):
    from api.services.gemini_extraction_service import _valid_date, _valid_currency
    rows = [r for r in items if str(r.get('row_type') or '').upper() != 'IGNORE']
    date = _valid_date(defaults.get('document_date'))
    currency = _valid_currency(defaults.get('currency'))
    return {
        'date': not date and (not rows or any(not (
            _valid_date(r.get('purchase_date')) or _valid_date(r.get('document_date'))
        ) for r in rows)),
        'currency': not currency and (not rows or any(not _valid_currency(r.get('currency')) for r in rows)),
    }
