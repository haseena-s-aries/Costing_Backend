from pathlib import Path
from collections import Counter
from unittest.mock import patch
from django.test import SimpleTestCase
from api.services.pdf_email_quotes import recover_pdf_email_quotes
from api.services.gemini_extraction_service import _finalize_pdf_extraction_result, gemini_result_to_virtual_sheet


class PdfThreadTests(SimpleTestCase):
    def test_real_thread_finalization_and_virtual_rows(self):
        path = Path('C:/Users/admin/Downloads/Danfos China Quotation_20261013_150122.pdf')
        if not path.exists():
            self.skipTest('Optional user PDF unavailable')
        data = path.read_bytes()
        recovered = recover_pdf_email_quotes({'items': [{'currency': 'CNY'}]}, data)['items'][:-1]
        # Simulate missing latest offer and one older item, plus wrong buyer.
        seed = [dict(r, supplier_name='Buyer Group') for i, r in enumerate(recovered) if i >= 4 and i != 11]
        result = _finalize_pdf_extraction_result({'items': seed, 'currency': 'CNY'}, data)
        self.assertEqual(len(result['items']), 17)
        self.assertTrue(all(r['supplier_name'] == 'DANFOSS' for r in result['items']))
        self.assertEqual(Counter(r['purchase_date'] for r in result['items']),
                         {'2026-09-21': 4, '2026-09-18': 4, '2026-09-16': 4, '2026-09-10': 3, '2026-09-09': 2})
        self.assertEqual([r['unit_price'] for r in result['items'][:4]], ['64804','56566','13514','11359'])
        sheet = gemini_result_to_virtual_sheet(result)
        self.assertEqual(len(sheet['rows']), 18)
        mapped = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertTrue(all(r['supplier_name'] == 'DANFOSS' for r in mapped))

        # Actual upload handoff must recover even if Gemini returns the old,
        # incomplete result without running its internal PDF finalizer.
        from unittest.mock import Mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        orchestrator = ExtractionOrchestrator()
        orchestrator.gemini = Mock()
        orchestrator.gemini.extract_from_file.return_value = {'items': seed, 'currency': 'CNY'}
        uploaded = orchestrator.extract(SimpleUploadedFile(path.name, data), path.name)
        self.assertEqual(len(uploaded['items']), 17)
        self.assertTrue(all(r['supplier_name'] == 'DANFOSS' for r in uploaded['items']))
        self.assertEqual(Counter(r['purchase_date'] for r in uploaded['items']),
                         {'2026-09-21':4, '2026-09-18':4, '2026-09-16':4, '2026-09-10':3, '2026-09-09':2})
        self.assertEqual(len(gemini_result_to_virtual_sheet(uploaded)['rows']), 18)

    def test_uncertain_supplier_cannot_inherit_buyer_and_requires_review(self):
        from types import SimpleNamespace
        from api.services.source_reconciliation import required_metadata
        pages = [SimpleNamespace(extract_text=lambda: 'From: Sales <sales@unknown.example>\nSent: July 1, 2026\n1. Device MODEL123 - 2 Nos. unit price (2 years warranty)\nUnit price: USD 81\nFrom: Buyer <buy@buyer.example>\nSent: July 2, 2026\nPlease confirm\nRegards\nBuyer LLC')]
        with patch('api.services.pdf_email_quotes.pdfplumber.open') as opened:
            opened.return_value.__enter__.return_value.pages = pages
            result = recover_pdf_email_quotes({'items': [], 'supplier_name': 'Buyer LLC'}, b'pdf')
        result['supplier_name'] = 'Buyer LLC'
        result['items'][0]['supplier_name'] = 'Buyer LLC'
        result = required_metadata(result)
        self.assertIsNone(result['items'][0]['supplier_name'])
        self.assertTrue(result['_needs_review'])
        self.assertTrue(result['_gemini_needs_review'])
        self.assertIn('supplier_identity_uncertain', result['_review_reasons'])

    def test_page_continuation_and_buyer_reply_are_not_new_seller(self):
        from types import SimpleNamespace
        pages = [SimpleNamespace(extract_text=lambda: 'From: Sales <sales@maker.example>\nSent: July 1, 2026\nMaker has stock.\n1. Device MODEL123 - 2 Nos. unit price (2 years warranty)'),
                 SimpleNamespace(extract_text=lambda: 'Unit price: USD 81\nFrom: Buyer <buy@buyer.example>\nSent: July 2, 2026\nPlease confirm the price.\nRegards\nBuyer LLC')]
        with patch('api.services.pdf_email_quotes.pdfplumber.open') as opened:
            opened.return_value.__enter__.return_value.pages = pages
            result = recover_pdf_email_quotes({'items': []}, b'pdf')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['supplier_name'], 'MAKER')
        self.assertEqual(result['items'][0]['purchase_date'], '2026-07-01')
        self.assertEqual(result['items'][0]['unit_price'], '81')
