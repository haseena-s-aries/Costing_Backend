from django.test import SimpleTestCase

from api.services.yen_currency_hint import resolve_yen_currency, yen_hint

QUOTE = ("Dear Buyer,\nPlease find the quotation\n"
         "1. VFD - 75KW - FC-202P75 - 2 Nos. unit price ￥22362 (with VAT, 2 years warranty)\n"
         "2. VFD - 45KW - FC-202P45 - 3 Nos. unit price ￥16082 (with VAT, 2 years warranty)\n"
         "Lead time 4weeks\nBR 李明 Lorry\n")


def rows(*prices):
    return {'currency': None, 'items': [
        dict(product_name=f'Item {p}', unit_price=p, currency=None, source_context=f'unit price ￥{p}')
        for p in prices]}


class YenCurrencyHintTests(SimpleTestCase):
    def test_hint_tiers(self):
        self.assertEqual(yen_hint('Total RMB 500'), ('CNY', 'currency name/code'))
        self.assertEqual(yen_hint('Price 500円'), ('JPY', 'currency name/code'))
        self.assertEqual(yen_hint('Tel: +86 21 3360 0613'), ('CNY', 'phone country code'))
        self.assertEqual(yen_hint('Tel: +81 3 1234 5678'), ('JPY', 'phone country code'))
        self.assertEqual(yen_hint('よろしくお願いします 田中'), ('JPY', 'Japanese script'))
        self.assertEqual(yen_hint('BR 李明'), ('CNY', 'Chinese script'))
        self.assertEqual(yen_hint('Regards, Lorry'), (None, None))
        # Explicit code beats script; a tier naming both is skipped.
        self.assertEqual(yen_hint('李明 JPY 100'), ('JPY', 'currency name/code'))
        self.assertEqual(yen_hint('RMB or JPY, tel +86'), ('CNY', 'phone country code'))

    def test_bare_yen_rows_take_the_file_hint_and_are_flagged(self):
        result = resolve_yen_currency(rows('22362', '16082'), QUOTE)
        self.assertEqual([i['currency'] for i in result['items']], ['CNY', 'CNY'])
        self.assertEqual(result['currency'], 'CNY')
        self.assertIn('currency_from_document_hint', result['_review_reasons'])
        self.assertIn('Chinese script', result['items'][0]['_currency_hint'])

    def test_no_hint_or_no_yen_or_explicit_currency_is_untouched(self):
        self.assertIsNone(resolve_yen_currency(rows('22362'), QUOTE.replace('李明 ', ''))['items'][0]['currency'])
        self.assertIsNone(resolve_yen_currency(rows('100'), 'Price 100 (tel +86)'.replace('￥', ''))['items'][0]['currency'])
        result = {'items': [dict(product_name='A', unit_price='5', currency='USD', source_context='USD 5')]}
        self.assertEqual(resolve_yen_currency(result, QUOTE + 'USD 5')['items'][0]['currency'], 'USD')
        # A row with another printed currency never becomes CNY.
        mixed = {'items': [dict(product_name='B', unit_price='7', currency=None, source_context='USD 7')]}
        self.assertIsNone(resolve_yen_currency(mixed, QUOTE + 'B USD 7')['items'][0]['currency'])
