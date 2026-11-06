import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from api.services.msg_commercial_history import signature_company, extract_labelled_table_history


class SignatureTableTests(SimpleTestCase):
    def test_signature_profile_requires_matching_website(self):
        signature = 'Thanks & regards,\nSam\nhttps://www.linkedin.com/company/example-freight/\nwww.examplefreight.com'
        self.assertEqual(signature_company(signature), 'Example Freight')
        self.assertIsNone(signature_company(signature.replace('examplefreight.com', 'unrelated.com')))

    def test_labelled_table_keeps_specification_and_part_number(self):
        body = ('Description\nPart Number\nQty\nUnit Price (USD)\nTotal Price (USD)\nSl no\n'
                'Breaker 2P 6A 10KA\nABC123\n2\n10\n20\n1\nRegards\nSeller LLC')
        rows = extract_labelled_table_history(body, sent_date='2026-01-01')['items']
        self.assertEqual(rows[0]['product_name'], 'Breaker 2P 6A 10KA')
        self.assertIn({'label': 'Part Number', 'value': 'ABC123'}, rows[0]['extra_attributes'])

    def test_local_sources_through_extraction_and_virtual_sheet(self):
        import extract_msg
        from api.services.gemini_extraction_service import GeminiExtractionService, gemini_result_to_virtual_sheet
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        fixtures = [('L2-Orient_20260817_122236.msg', '2026-08-14', ['2593'], 'Orient Cargo Agency'),
                    ('RE_ DE2417_DE2471_CONSUMABLES_NEXA_20260814_111837.msg', '2026-08-07',
                     ['76.37', '99.02', '14.99', '41.22', '15.42', '8.27'], 'NEXA ELECTRICALS TRADING LLC')]
        for filename, date, prices, supplier in fixtures:
            with self.subTest(filename=filename):
                path = Path('C:/Users/admin/Downloads') / filename
                if not path.exists():
                    self.skipTest('Optional local regression source unavailable')
                captured = {'category': 'QUOTATION', 'document_type': 'email_quote', 'document_date': date,
                            'currency': 'AED', 'items': [{'product_name': 'Cut description', 'unit_price': price,
                            'purchase_date': date, 'currency': 'AED'} for price in prices]}
                service = object.__new__(GeminiExtractionService)
                service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
                service._log_usage = Mock()
                orchestrator = ExtractionOrchestrator()
                orchestrator.gemini = service
                result = orchestrator.extract(SimpleUploadedFile(filename, path.read_bytes()), filename)
                self.assertEqual(len(result['items']), len(prices))
                self.assertTrue(all(r['supplier_name'] == supplier for r in result['items']))
                if len(prices) > 1:
                    with extract_msg.Message(str(path)) as message:
                        source = extract_labelled_table_history(message.body, message.sender, message.date)['items']
                    self.assertEqual([r['product_name'] for r in result['items']], [r['product_name'] for r in source])
                    for row, original in zip(result['items'], source):
                        self.assertIn(original['extra_attributes'][0]['value'], row['display_name'])
                sheet = gemini_result_to_virtual_sheet(result)
                self.assertEqual(len(sheet['rows']), len(prices) + 1)
