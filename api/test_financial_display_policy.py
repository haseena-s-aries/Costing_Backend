from django.test import SimpleTestCase

from api.services.commercial_attribute_policy import commercial_field, clean_commercial_display
from api.services.product_normalization_service import clean_display_identity, sanitize_product_identity
from api.services.gemini_extraction_service import build_canonical_display_name
from api.services.import_service import ImportService
from api.views import _line_item_display_data


class FinancialDisplayPolicyTests(SimpleTestCase):
    def test_stored_and_rebuilt_names_hide_commercial_attributes(self):
        technical = [{'label': 'Sensor', 'value': '155'},
                     {'label': 'Flow Rate', 'value': '20 l/min'},
                     {'label': 'DNV', 'value': '2,266'}]
        for label in ['Example PO Price', 'Buyer PO Unit price', 'Old PO price',
                      'Customer Budget', 'Discount', 'Price hike', 'Estimated cost']:
            with self.subTest(label=label):
                name = f'Drive ZX | {label}: 999 | Sensor: 155 | Flow Rate: 20 l/min | DNV: 2,266'
                expected = 'Drive ZX | Sensor: 155 | Flow Rate: 20 l/min | DNV: 2,266'
                row = dict(product_name='Drive ZX', display_name=name,
                           extra_attributes=[{'label': label, 'value': '999'}] + technical,
                           unit_price='10150', line_total='20300', currency='USD')
                self.assertEqual(clean_commercial_display(dict(row))['display_name'], expected)
                self.assertEqual(clean_display_identity(name), expected)
                self.assertEqual(sanitize_product_identity(name), expected)
                for builder in (_line_item_display_data, ImportService()._build_display_name):
                    for stored in (True, False):
                        source = dict(row)
                        if not stored:
                            source.pop('display_name')
                        result = builder(source)
                        self.assertEqual(result['display_name'], expected)
                        self.assertEqual(result['unit_price'], '10150')
                        self.assertEqual(result['line_total'], '20300')
                self.assertEqual(build_canonical_display_name(row), expected)

    def test_display_filter_does_not_change_source_column_semantics(self):
        self.assertIsNone(commercial_field('Buyer Old PO Price'))
        self.assertIsNone(commercial_field('Customer Budget'))
        for name in ['Freight DDP', 'Internal manhour cost', 'Price sensor assembly']:
            self.assertEqual(clean_commercial_display({'display_name': name})['display_name'], name)
