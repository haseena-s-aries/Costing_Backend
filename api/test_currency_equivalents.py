from copy import deepcopy
from django.test import SimpleTestCase
from api.services.alternative_currency_quotes import collapse_currency_equivalents
from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet


class CurrencyEquivalentTests(SimpleTestCase):
    def sample(self):
        return {'items': [dict(product_name=name, quantity=qty, supplier_name='Example Cable',
                               purchase_date='2026-10-08', currency=code, unit_price=price)
                          for name, qty, rates in [('Cable-A 3*70', '32', [('CNY','314'),('USD','47')]),
                                                   ('Cable-B 3*70', '40', [('CNY','339.5'),('USD','50.8')])]
                          for code, price in rates]}

    source = 'Cable-A 3*70 Unit price : 314yuan/m=$47\nCable-B 3*70 Unit price : 339.5yuan/m=$50.8'

    def test_first_printed_currency_and_virtual_sheet(self):
        result = collapse_currency_equivalents(self.sample(), self.source)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual([(i['currency'],i['unit_price']) for i in result['items']], [('CNY','314'),('CNY','339.5')])
        self.assertEqual([i['quantity'] for i in result['items']], ['32','40'])
        self.assertEqual(len(gemini_result_to_virtual_sheet(result)['rows']), 3)
        self.assertEqual(len(collapse_currency_equivalents(result, self.source)['items']), 2)

    def test_not_a_currency_preference_hardcode(self):
        result = collapse_currency_equivalents(self.sample(), 'Cable-A 3*70 Unit price : $47=314yuan/m')
        self.assertEqual(result['items'][0]['currency'], 'USD')

    def test_separate_quotes_dates_suppliers_and_no_equation_survive(self):
        sample = self.sample()
        self.assertEqual(collapse_currency_equivalents(deepcopy(sample), self.source.replace('=', ' or ')), sample)
        for field, value in [('supplier_name','Another Seller'), ('purchase_date','2026-10-09'), ('quantity','15')]:
            changed = deepcopy(sample)
            changed['items'][1][field] = value
            result = collapse_currency_equivalents(changed, self.source)
            self.assertEqual(len(result['items']), 3)
            self.assertEqual(len([i for i in result['items'] if i['product_name']=='Cable-A 3*70']), 2)
