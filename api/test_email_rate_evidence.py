from pathlib import Path
from django.test import SimpleTestCase
from api.services.email_rate_evidence import rate_rows, restore_rates
from api.services.msg_commercial_history import plausible_company, signature_company


class EmailRateEvidenceTests(SimpleTestCase):
    def test_security_and_contract_text_are_not_suppliers(self):
        for text in ['This email is NOT originated from Seller LLC',
                     'points at the discretion of port authorities including but not limited']:
            self.assertFalse(plausible_company(text))
            self.assertIsNone(signature_company(text))

    def test_dates_conditions_and_totals(self):
        body = ('Ocean Freight + EXW: AED 500/20ft\nTotal: AED 500\nRegards\nSeller LLC\n'
                'From: Seller <sales@seller.example>\nSent: 27 October 2026\n'
                'Ocean Freight + EXW: USD 100/20ft\nSTORAGE CHARGE: USD 20/TEU if applicable\n'
                'Regards\nSeller LLC\nFrom: Buyer <buyer@buyer.example>\nSent: 26 October 2026\n'
                'Please quote freight\nInvoice value: USD 5000\nRegards\nBuyer LLC')
        rows = rate_rows(body, sent_date='2026-10-28')
        self.assertEqual([r['unit_price'] for r in rows], ['500', '100', '20'])
        self.assertEqual(rows[1]['purchase_date'], '2026-10-27')
        self.assertIn('if applicable', str(rows[2]['extra_attributes']))
        result = restore_rates({'items': rows[:1]}, body, sent_date='2026-10-28')
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(len(restore_rates(result, body, sent_date='2026-10-28')['items']), 3)

    def test_conditional_duplicate_and_unknown_quantity(self):
        body = ('Freight quotation\nSUBJECT Pick up container from far away yard: USD 50/20GP (if equipment released)\n'
                'INSPECTION\nAED 170/document +Service charge AED 100 if any\n'
                'If customs impose inspection, service fee AED 100\nRegards\nSeller LLC')
        original = {'product_name': 'Pick up container from far away yard (if equipment released)',
                    'unit_price': 50, 'currency': 'USD', 'quantity': 1, 'line_total': 50,
                    'purchase_date': '2026-10-29', 'row_type': 'SERVICE'}
        result = restore_rates({'items': [original], 'grand_total': 320}, body, sent_date='2026-10-29')
        self.assertEqual(len(result['items']), 3)
        self.assertTrue(all(r['quantity'] is None and r['line_total'] is None for r in result['items']))
        self.assertEqual({r['row_type'] for r in result['items']}, {'CHARGE'})
        self.assertEqual(result['items'][0]['uom'], '20GP')
        self.assertIsNone(result['grand_total'])

    def test_unsuffixed_signature_does_not_use_recipient(self):
        self.assertEqual(signature_company('To: Buyer LLC\nThank you\nExample Blue Services\n'
                                          'info@exampleblue.ae'), 'Example Blue Services')

    def test_local_worldzone_history(self):
        import extract_msg
        path = Path('C:/Users/admin/Downloads/WorldZone_20261103_081905.msg')
        if not path.exists():
            self.skipTest('Local source fixture unavailable')
        with extract_msg.Message(str(path)) as message:
            rows = rate_rows(message.body, message.sender, str(message.date))
        self.assertEqual(len(rows), 14)
        self.assertEqual({r['purchase_date'] for r in rows}, {'2026-10-27', '2026-10-28'})
        self.assertEqual({r['supplier_name'] for r in rows}, {'World Zone Group'})
        self.assertIn('1500', [r['unit_price'] for r in rows])

    def test_local_pdf_supplier(self):
        from api.services.gemini_extraction_service import _recover_email_supplier_from_pdf
        path = Path('C:/Users/admin/Downloads/pacific hire quote_20261103_044240.pdf')
        if not path.exists():
            self.skipTest('Local source fixture unavailable')
        self.assertEqual(_recover_email_supplier_from_pdf(path.read_bytes(), 'Aries Underwater Solutions LLC'),
                         'Pacific Blu Subsea Services')

    def test_local_attachment_supplier(self):
        import extract_msg
        from api.services.msg_attachment_recovery import recover_attachments
        path = Path('C:/Users/admin/Downloads/Lone star_Sea freight_20261103_081907.msg')
        if not path.exists():
            self.skipTest('Local source fixture unavailable')
        bad = 'This email is NOT originated from Lonestar Shipping LLC'
        result = {'supplier_name': bad, 'items': [
            {'product_name': 'DOOR TO DOOR FREIGHT CHARGE', 'unit_price': 10803, 'supplier_name': bad},
            {'product_name': 'INSURANCE', 'unit_price': 1317, 'supplier_name': bad}]}
        with extract_msg.Message(str(path)) as message:
            attachments = [{'name': a.longFilename, 'data': a.data} for a in message.attachments
                           if str(a.longFilename).lower().endswith('.pdf')]
            result = recover_attachments(result, attachments)
        self.assertEqual({r['supplier_name'] for r in result['items']}, {'LONESTAR SHIPPING LLC'})

    def test_local_abreco_keeps_dated_and_conditional_rates(self):
        import extract_msg
        path = next(Path('C:/Users/admin/Downloads').glob('Abreco L1RE_ RFQ_DU5995*.msg'), None)
        if path is None:
            self.skipTest('Local source fixture unavailable')
        with extract_msg.Message(str(path)) as message:
            rows = rate_rows(message.body, message.sender, str(message.date))
        self.assertEqual({r['supplier_name'] for r in rows}, {'Abreco Freight LLC'})
        prices = {(r['purchase_date'], float(r['unit_price'])) for r in rows}
        self.assertTrue({('2026-10-29', 9900), ('2026-10-27', 10250),
                         ('2026-10-27', 3000), ('2026-10-29', 170),
                         ('2026-10-29', 1300), ('2026-10-29', 50)} <= prices)
        self.assertTrue(any('subject to occur' in str(r['extra_attributes']).lower() for r in rows))

    def test_local_worldzone_production_pipeline_recovers_missing_history(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from api.services.gemini_extraction_service import GeminiExtractionService
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        path = Path('C:/Users/admin/Downloads/WorldZone_20261103_081905.msg')
        if not path.exists():
            self.skipTest('Local source fixture unavailable')
        response = {'document_type': 'email_quote', 'category': 'QUOTATION',
                    'document_date': '2026-10-28', 'currency': 'AED',
                    'supplier_name': 'points at the discretion of port authorities including but not limited',
                    'items': [{'product_name': name, 'unit_price': rate, 'currency': 'AED',
                               'purchase_date': '2026-10-28', 'row_type': 'CHARGE', 'quantity': 1}
                              for name, rate in [('Ocean Freight + EXW', 33405),
                                                 ('Destination Local Clearance & Delivery', 6449)]]}
        service = object.__new__(GeminiExtractionService)
        service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(response)))
        service._log_usage = Mock()
        orchestrator = ExtractionOrchestrator()
        orchestrator.gemini = service
        result = orchestrator.extract(SimpleUploadedFile(path.name, path.read_bytes()), path.name)
        self.assertEqual(len(result['items']), 14)
        self.assertEqual({r['supplier_name'] for r in result['items']}, {'World Zone Group'})
