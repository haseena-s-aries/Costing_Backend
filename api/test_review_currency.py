from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from api.services.import_service import build_review_reasons
from api.services.gemini_extraction_service import validate_gemini_extraction


class ReviewCurrencyTests(SimpleTestCase):
    def test_final_rows_allow_mixed_currency_but_require_every_row(self):
        for currencies, missing in ((['USD', 'CNY'], False), (['USD', ''], True)):
            with self.subTest(currencies=currencies):
                rows = [SimpleNamespace(
                    raw_data={}, mapped_data={'currency': currency},
                    normalized_data={'product_name': 'Component'}, row_type='PRODUCT',
                ) for currency in currencies]
                batch = SimpleNamespace(rows=MagicMock())
                batch.rows.all.return_value = rows
                batch.rows.exists.return_value = True
                with patch('api.services.import_service.resolve_import_metadata', return_value={
                    'supplier_name': 'Example Ltd', 'document_date': '2026-01-01',
                    'category': 'QUOTATION', 'currency': None,
                }):
                    self.assertEqual('Currency missing' in build_review_reasons(batch), missing)

    def test_extraction_currency_validation_is_per_item(self):
        for currencies, missing in ((['USD', 'CNY'], False), (['USD', None], True)):
            with self.subTest(currencies=currencies):
                result = {
                    'supplier_name': 'Example Ltd', 'document_date': '2026-01-01',
                    'currency': None, 'items': [
                        {'product_name': f'Component {index}', 'row_type': 'PRODUCT',
                         'quantity': '1', 'unit_price': '10', 'line_total': '10',
                         'currency': currency}
                        for index, currency in enumerate(currencies)
                    ],
                }
                # Isolate validation from normalization's separate inference policies.
                with patch('api.services.gemini_extraction_service.normalize_result', side_effect=lambda x: x), patch(
                    'api.services.gemini_extraction_service._collapse_parallel_currency_rows', side_effect=lambda x: x
                ):
                    validation = validate_gemini_extraction(result)
                self.assertEqual('missing_currency' in validation['warnings'], missing)
