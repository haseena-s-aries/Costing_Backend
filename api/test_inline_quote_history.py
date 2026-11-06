from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json

from api.services.msg_commercial_history import extract_inline_quote_history, reconcile_inline_quote_history
from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet


class InlineQuoteHistoryTests(SimpleTestCase):
    def test_real_msg_repairs_bad_model_rows_through_production(self):
        from api.services.gemini_extraction_service import GeminiExtractionService
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        path = Path('C:/Users/admin/Downloads/RE_ ARIES-DE2417_CABLES ENQ_20260924_105817.msg')
        if not path.exists():
            self.skipTest('User supplied MSG is not available on this machine')
        captured = {'document_type': 'email_quote', 'category': 'QUOTATION',
                    'document_date': '2026-08-03', 'currency': 'AED',
                    'supplier_name': 'Velumani D | Oceanic Global Marine Supply LLC',
                    'items': [{'product_name': 'Thank you for your enquiry. Please find our quotation below.',
                               'row_type': 'SERVICE', 'quantity': '260', 'unit_price': str(price),
                               'line_total': str(price * 260), 'currency': 'AED',
                               'purchase_date': '2026-08-03'} for price in (6, 12)]}
        service = object.__new__(GeminiExtractionService)
        service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
        service._log_usage = Mock()
        orchestrator = ExtractionOrchestrator()
        orchestrator.gemini = service
        result = orchestrator.extract(SimpleUploadedFile(path.name, path.read_bytes()), path.name)
        self.assertEqual(len(result['items']), 8)
        self.assertIsNone(result['items'][0]['currency'])
        self.assertEqual([i['purchase_date'] for i in result['items']], ['2026-09-24'] + ['2026-08-12'] * 5 + ['2026-08-03'] * 2)
        self.assertTrue(all(i['row_type'] == 'PRODUCT' for i in result['items']))
        self.assertTrue(all('Thank you' not in i['product_name'] for i in result['items']))
        self.assertEqual(len(gemini_result_to_virtual_sheet(result)['rows']) - 1, 8)

    def test_dated_quotes_preserve_identity_and_not_greetings(self):
        body = '''Greetings!
Thank you. Please find our quotation below.
From: Buyer <buyer@example.test>
Sent: 24 September 2026
Subject: Enquiry
Request to quote for Shielded cable -100 mtr @ 10/- PER MTR BLACK CLASS2 EX-STOCK
From: Sales Person | Example Marine LLC <seller@example.test>
Sent: 12 August 2026
Subject: Quotation
Please find our quotation below.
From: Buyer <buyer@example.test>
Sent: 12 August 2026
Subject: Enquiry
Request to quote for the below items also
Sl no
Description
Unit
Qty
1
Network cable
260
Mtr
12/- AED PER MTR SHF1 4x2x23AWG EX-STOCK
2
Panel wire
3
coil
110/- PER ROLL OF 100MTR BLACK
From: Sales Person | Example Marine LLC <seller@example.test>
Sent: 3 August 2026
Subject: Quotation
Greetings!
Thank you for your enquiry. Please find our quotation below.
Network cable - 260Mtr @ 12/- AED PER MTR SHF1 4x2x23AWG EX-STOCK
Regards
Sales Person
'''
        history = extract_inline_quote_history(body, 'Sales Person | Example Marine LLC <seller@example.test>', '2026-09-24')
        self.assertIsNotNone(history)
        self.assertEqual(len(history['items']), 4)
        self.assertIsNone(history['items'][0]['currency'])
        self.assertEqual(history['items'][2]['currency'], 'AED')
        self.assertIn('100MTR BLACK', history['items'][2]['product_name'])
        normalized = normalize_result(history)
        virtual = gemini_result_to_virtual_sheet(normalized)
        rows = [dict(zip(virtual['rows'][0], row)) for row in virtual['rows'][1:]]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r['purchase_date'] for r in rows], ['2026-09-24', '2026-08-12', '2026-08-12', '2026-08-03'])
        for row in rows:
            self.assertEqual(row['row_type'], 'PRODUCT')
            self.assertEqual(row['supplier_name'], 'Example Marine LLC')
            self.assertNotIn('Thank you', row['product_name'])
        bad = {'product_name': 'Thank you for your enquiry', 'purchase_date': '2026-08-03', 'quantity': '260', 'unit_price': '12', 'row_type': 'SERVICE'}
        attachment = dict(bad, _attachment_name='independent.pdf', product_name='Separate product')
        repaired = reconcile_inline_quote_history({'items': [bad, attachment]}, history)
        self.assertEqual(len(repaired['items']), 5)
        self.assertIn(attachment, repaired['items'])
        self.assertNotIn(bad, repaired['items'])

    def test_unendorsed_buyer_price_does_not_become_supplier_offer(self):
        result = extract_inline_quote_history('Request to quote for Cable -5 mtr @ 20/- AED PER MTR insulated',
                                              'Buyer LLC <buyer@example.test>', '2026-09-24')
        self.assertIsNone(result)

    def test_unknown_priced_structure_refuses_partial_replacement(self):
        body = 'Cable -5 mtr @ 20/- AED PER MTR insulated\nFrom: Sales | Example LLC <s@example.test>\nSent: 1 August 2026\nSubject: Offer\nOther goods price AED 40'
        self.assertIsNone(extract_inline_quote_history(body, 'Example LLC <s@example.test>', '2026-09-24'))
