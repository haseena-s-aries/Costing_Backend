from django.test import SimpleTestCase
from api.services.currency_evidence_policy import require_image_currency_evidence
from api.services.source_reconciliation import required_metadata
from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet
from api.services.parser_service import ParserService
from api.services.validation_service import ValidationService
from api.services.import_service import ImportService
from api.views import _line_item_display_data


class CurrencyEvidenceTests(SimpleTestCase):
    def result(self):
        return dict(currency='AED', supplier_name='Example Ltd', document_date='2026-10-01',
                    items=[dict(product_name='Breaker AB-50', unit_price='170.50', quantity='1',
                                currency='AED', row_type='PRODUCT')])

    def test_inferred_currency_is_blank_through_virtual_sheet(self):
        result = required_metadata(require_image_currency_evidence(normalize_result(self.result())))
        self.assertIsNone(result['currency'])
        self.assertIsNone(result['items'][0]['currency'])
        self.assertTrue(result['_gemini_needs_review'])
        self.assertIn('missing_currency', result['_review_reasons'])
        sheet = gemini_result_to_virtual_sheet(result)
        row = dict(zip(sheet['rows'][0], sheet['rows'][1]))
        self.assertFalse(row['currency'])
        self.assertEqual(row['unit_price'], '170.50')
        # Preview previously reintroduced BASE_CURRENCY after this stage.
        for row_type in ['PRODUCT', 'SERVICE', 'CHARGE']:
            status, errors, _, mapped = ValidationService().validate(dict(row), None, row_type)
            self.assertEqual(status, 'failed')
            self.assertIn('Currency missing from source document.', errors)
            mapped = ImportService()._build_display_name(mapped)
            displayed = _line_item_display_data(mapped)
            self.assertFalse(displayed['currency'])
            self.assertEqual(displayed['unit_price'], '170.50')

    def test_no_implicit_default_but_explicit_currency_is_preserved(self):
        row = {'product_name': 'Example', 'unit_price': '10'}
        self.assertFalse(ValidationService().validate(dict(row))[3]['currency'])
        self.assertEqual(ValidationService().validate(dict(row), 'EUR')[3]['currency'], 'EUR')
        self.assertEqual(ValidationService().validate({**row, 'currency': 'USD'}, 'EUR')[3]['currency'], 'USD')

    def test_explicit_currency_and_mixed_currencies_survive(self):
        result = self.result()
        result['currency_evidence'] = 'Dhs'
        self.assertEqual(require_image_currency_evidence(result)['items'][0]['currency'], 'AED')
        result['currency_evidence'] = None
        result['items'][0]['currency_evidence'] = 'EUR'
        result['items'].append(dict(product_name='Other', currency_evidence='USD', unit_price='20'))
        self.assertEqual([i['currency'] for i in require_image_currency_evidence(result)['items']], ['EUR', 'USD'])

    def test_written_currency_name_is_evidence(self):
        from api.services.gemini_extraction_service import _valid_currency
        self.assertEqual(_valid_currency('euro'), 'EUR')
        self.assertEqual(_valid_currency('380euro/pcs'), 'EUR')
        self.assertEqual(_valid_currency('US$ 5'), 'USD')
        # Ambiguous or company text never becomes a currency.
        for text in ('BD SENSORS', 'Rs 50 per kg', 'USD and EUR', 'EUROPE', 'pcs'):
            self.assertIsNone(_valid_currency(text))
        result = {'currency': 'EUR', 'currency_evidence': None, 'items': [
            dict(product_name='Pressure transmitter A', unit_price='380', currency_evidence='euro'),
            dict(product_name='Pressure transmitter B', unit_price='350',
                 source_context='Pressure transmitter B: 350euro/pcs')]}
        result = require_image_currency_evidence(result)
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual([i['currency'] for i in result['items']], ['EUR', 'EUR'])

    def test_parser_does_not_fill_missing_currency_with_aed(self):
        rows = [['product_name', 'currency'], ['Breaker', '']]
        ParserService()._apply_document_currency(rows, [{}, {}], {})
        self.assertFalse(rows[1][1])
