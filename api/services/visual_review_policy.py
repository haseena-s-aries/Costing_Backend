"""Conservative review gate; never discard or rewrite extracted rows."""
import io
import logging


def batch_visual_review_reasons(batch):
    """Recheck source at the import boundary, including legacy cached results."""
    result = dict((batch.extraction_summary or {}).get('gemini_raw_result') or {})
    upload = batch.uploaded_file
    if str(upload.original_file_name or '').lower().endswith('.pdf'):
        try:
            with upload.file.open('rb') as source:
                data = source.read()
            result.pop('_visual_source_checked', None)
            if not data:
                raise ValueError('Empty PDF source')
            require_visual_review(result, data)
        except Exception:
            result.setdefault('_review_reasons', []).append('visual_source_check_failed')
    else:
        require_visual_review(result)
    labels = {
        'handwritten_content_requires_review': 'Handwritten content — verify all extracted fields',
        'scanned_content_requires_review': 'Scanned document — verify extracted text and amounts',
        'visual_source_check_failed': 'Source verification failed — manual review required',
    }
    return list(dict.fromkeys(labels[r] for r in result.get('_review_reasons', []) if r in labels))


def require_visual_review(result, file_bytes=None):
    reasons = list(result.get('_review_reasons') or [])
    records = [result, *(result.get('items') or [])]
    if any(r.get('contains_handwriting') is True for r in records):
        reasons.append('handwritten_content_requires_review')
    if file_bytes and not result.get('_visual_source_checked'):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                scanned = []
                for number, page in enumerate(pdf.pages, 1):
                    area = page.width * page.height
                    # Large raster page, not a small logo or signature. A scan
                    # alone does not prove handwriting; label this separately.
                    if len(page.chars) < 40 and any(
                        float(im.get('width', 0)) * float(im.get('height', 0)) > area * .45
                        for im in page.images
                    ):
                        scanned.append(number)
                if scanned:
                    result['_visual_review_pages'] = scanned
                    reasons.append('scanned_content_requires_review')
            result['_visual_source_checked'] = True
        except Exception:
            logging.getLogger(__name__).warning('Visual source review check unavailable', exc_info=True)
            reasons.append('visual_source_check_failed')
    result['_review_reasons'] = list(dict.fromkeys(reasons))
    if any(r in reasons for r in ('handwritten_content_requires_review',
                                  'scanned_content_requires_review', 'visual_source_check_failed')):
        result['_needs_review'] = result['_gemini_needs_review'] = True
    return result
