from django.test import SimpleTestCase

from api.services.gemini_extraction_service import _normalize_item, _normalize_supplier_name
from api.services.source_reconciliation import required_metadata
from api.services.msg_commercial_history import extract_agreed_rate_history, reconcile_inline_quote_history


class CommercialSourceRulesTests(SimpleTestCase):
    def test_total_without_rate_keeps_quantity_and_requires_review(self):
        for kind in ('PRODUCT', 'SERVICE', 'CHARGE'):
            for qty in ('1', '7'):
                item, _ = _normalize_item(dict(product_name='Cable assembly', row_type=kind,
                                              quantity=qty, line_total='91', unit_price=None), 'USD')
                self.assertIsNotNone(item)
                self.assertIsNone(item['unit_price'])
                result = required_metadata({'items': [item]})
                self.assertIn('missing_unit_price', result['_review_reasons'])

    def test_total_only_fallback_and_printed_price(self):
        item, _ = _normalize_item(dict(product_name='Cable assembly', row_type='PRODUCT',
                                      line_total='91'), 'USD')
        self.assertEqual(float(item['unit_price']), 91)
        item, _ = _normalize_item(dict(product_name='Cable assembly', row_type='PRODUCT',
                                      quantity='2', unit_price='50', line_total='91'), 'USD')
        self.assertEqual(float(item['unit_price']), 50)

    def test_company_words_are_not_address_boundaries(self):
        name = 'Example City Building Machinery Manufacture Co.,Ltd.'
        self.assertEqual(_normalize_supplier_name(name + '\nAddress: Industrial Road\nTel: 12345'), name)

    def test_dated_rates_and_forwarded_copy(self):
        def offer(value):
            return ('Charges for your shipment from ORIGIN to DESTINATION excluding duties.\n'
                    f'Agreed Rates : USD: {value}\nCarrier # Express\n'
                    'Transit Time: 3-5 days\nRate Validity: this week\n'
                    'Conditional surcharge USD 80 if required\nRegards\nExample Freight LLC\n')
        body = offer('92')
        body += '\nFrom: Agent\nSent: September 8, 2026\n' + offer('104')
        body += '\nFrom: Agent\nSent: September 8, 2026\n' + offer('104')
        history = extract_agreed_rate_history(body, sent_date='2026-09-09')
        self.assertEqual([(r['purchase_date'], r['unit_price']) for r in history['items']],
                         [('2026-09-09', '92'), ('2026-09-08', '104')])
        merged = reconcile_inline_quote_history({'items': [dict(history['items'][0])]}, history)
        self.assertEqual(len(merged['items']), 2)

    def test_history_merge_uses_numeric_values_not_decimal_formatting(self):
        canonical = dict(product_name='Shipment from A to B', purchase_date='2026-09-09',
                         supplier_name='Example Freight LLC', unit_price='92.00', quantity='1.00')
        history = {'items': [canonical], '_msg_commercial_blocks': []}
        model = dict(canonical, unit_price='92', quantity=1)
        unrelated_date = dict(model, purchase_date='2026-09-08')
        attachment = dict(model, _attachment_name='separate-quote.pdf')
        merged = reconcile_inline_quote_history({'items': [model, unrelated_date, attachment]}, history)
        self.assertEqual(merged['items'], [canonical, unrelated_date, attachment])

    def test_real_freight_thread_through_extraction_and_virtual_sheet(self):
        import json
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from api.services.gemini_extraction_service import GeminiExtractionService, gemini_result_to_virtual_sheet
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        import extract_msg

        path = Path('C:/Users/admin/Downloads/l2-third set_20260910_072952.msg')
        if not path.exists():
            self.skipTest('Optional user source fixture unavailable')
        with extract_msg.Message(str(path)) as message:
            history = extract_agreed_rate_history(message.body, message.sender, str(message.date))
        latest = dict(history['items'][0])
        latest['unit_price'] = str(int(float(latest['unit_price'])))
        latest['quantity'] = '1'
        captured = dict(document_type='email_quote', category='QUOTATION', items=[latest],
                        supplier_name=latest['supplier_name'], currency=latest['currency'],
                        document_date=latest['purchase_date'])
        service = object.__new__(GeminiExtractionService)
        service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
        service._log_usage = Mock()
        orchestrator = ExtractionOrchestrator()
        orchestrator.gemini = service
        result = orchestrator.extract(SimpleUploadedFile(path.name, path.read_bytes()), path.name)
        self.assertEqual(len(result['items']), len(history['items']))
        self.assertEqual([r['purchase_date'] for r in result['items']],
                         [r['purchase_date'] for r in history['items']])
        self.assertEqual(len(gemini_result_to_virtual_sheet(result)['rows']) - 1, len(history['items']))
