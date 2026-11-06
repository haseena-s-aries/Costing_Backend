"""Reference identifiers are not evidence for document dates."""
import io
import re
from datetime import datetime
import pdfplumber


def reject_reference_dates(result, file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        source = '\n'.join(p.extract_text() or '' for p in pdf.pages)
    return reject_reference_dates_in_text(result, source)


def reject_reference_dates_in_text(result, source):
    from .gemini_extraction_service import _valid_date
    reference = re.compile(r'\b(?:ref(?:erence)?\.?\s*(?:no\.?|number|#)?|(?:quotation|quote|invoice|offer|po)\s*(?:no\.?|number|#))\s*[:#]\s*([^\s]+)', re.I)
    references = [m[1] for m in reference.finditer(source)]
    if not references:
        return result
    without_refs = reference.sub('', source)
    # Explicit document/email date labels, including values on the next line.
    date_values = re.findall(r'(?im)\b(?:date|dated|sent|issued\s+on)\s*[:：]\s*([^\n]+)', without_refs)
    date_values += re.findall(r'(?m)^\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\s*$', without_refs)
    supported = {_valid_date(value) for value in date_values}
    rejected = set(result.get('_rejected_reference_dates') or [])
    for record in [result, *(result.get('items') or [])]:
        for field in ('document_date', 'purchase_date'):
            value = _valid_date(record.get(field))
            if not value or value in supported:
                continue
            try:
                date = datetime.strptime(str(value), '%Y-%m-%d')
            except ValueError:
                continue
            keys = {date.strftime('%Y%m%d'), date.strftime('%d%m%Y'), date.strftime('%m%d%Y')}
            if any(key in re.sub(r'\D', '', ref) for key in keys for ref in references):
                record[field] = None
                rejected.add(str(value))
    if rejected:
        result['_rejected_reference_dates'] = sorted(rejected)
        result['_needs_review'] = result['_gemini_needs_review'] = True
        result['_review_reasons'] = list(dict.fromkeys([*(result.get('_review_reasons') or []), 'document_date_inferred_from_reference']))
    return result
