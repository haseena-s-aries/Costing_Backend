from django.test import SimpleTestCase

from api.services.commercial_consistency import finalize

FACTORY = 'Example Hongrun Marine Accessories Factory'


def offer():
    rows = [dict(row_type='PRODUCT', product_name=f'Shipping costs for {v}', quantity=1, unit_price='2984',
                 currency='USD', purchase_date='2026-10-28', supplier_name=FACTORY, page_number=1)
            for v in ('ONE AQUILA', 'ONE COLUMBA', 'ONE GRUS')]
    rows.append(dict(row_type='PRODUCT', product_name='Shipping cost', quantity=1, unit_price='2984',
                     currency='USD', purchase_date='2026-10-03', supplier_name='Example Hongrun Qing', page_number=2))
    return {'supplier_name': FACTORY, 'grand_total': '8952', 'currency': 'USD', 'items': rows}


class CommercialConsistencyTests(SimpleTestCase):
    def test_supplier_offer_with_chat_screenshot(self):
        result = finalize(offer())
        self.assertEqual([i['product_name'] for i in result['items']],
                         ['Shipping costs for ONE AQUILA', 'Shipping costs for ONE COLUMBA', 'Shipping costs for ONE GRUS'])
        self.assertEqual({i['row_type'] for i in result['items']}, {'SERVICE'})
        self.assertEqual({i['supplier_name'] for i in result['items']}, {FACTORY})
        self.assertEqual([i['line_total'] for i in result['items']], ['2984', '2984', '2984'])
        self.assertTrue(result['_grand_total_verified'])

    def test_chat_contact_alias_maps_to_identified_supplier(self):
        result = offer()
        result['items'][3]['unit_price'] = '3100'  # a different price stays, under the real supplier
        result = finalize(result)
        self.assertEqual(result['items'][3]['supplier_name'], FACTORY)
        self.assertNotIn('_grand_total_verified', result)

    def test_historical_same_name_rows_and_unreconciled_totals_are_kept(self):
        items = [dict(product_name='Door to Port Charges - A to B - 40FT', unit_price='9350', currency='USD',
                      purchase_date=d, supplier_name='Seller LLC', row_type='SERVICE') for d in ('2026-06-29', '2026-04-22')]
        result = finalize({'items': items})
        self.assertEqual(len(result['items']), 2)
        self.assertIsNone(result['items'][0].get('line_total'))

    def test_grand_total_row_dropped_and_mismatch_flagged(self):
        items = [dict(product_name='Valve', quantity='2', unit_price='50', line_total='100', currency='USD'),
                 dict(product_name='Gasket', quantity='1', unit_price='20', line_total='20', currency='USD'),
                 dict(product_name='Total amount', unit_price='120', currency='USD')]
        result = finalize({'items': items, 'grand_total': '120'})
        self.assertEqual([i['product_name'] for i in result['items']], ['Valve', 'Gasket'])
        result = finalize({'items': items[:2], 'grand_total': '150'})
        self.assertIn('grand_total_mismatch', result['_review_reasons'])
        self.assertEqual(len(result['items']), 2)

    def test_products_named_like_transport_parts_stay_products(self):
        result = finalize({'items': [dict(product_name='Transport bracket 40mm', unit_price='5', row_type='PRODUCT'),
                                     dict(product_name='Sea freight Shanghai to Dubai', unit_price='900', row_type='PRODUCT')]})
        self.assertEqual([i['row_type'] for i in result['items']], ['PRODUCT', 'SERVICE'])

    def test_source_verified_rows_from_separate_messages_are_kept(self):
        items = [dict(product_name='STORAGE CHARGE AT POD', unit_price='200', currency='USD', purchase_date='2026-10-29',
                      supplier_name='Seller LLC', _explicit_email_rate=True),
                 dict(product_name='STORAGE CHARGE AT POD BY CARRIER', unit_price='200', currency='USD',
                      purchase_date='2026-10-27', supplier_name='Seller LLC', _explicit_email_rate=True)]
        self.assertEqual(len(finalize({'items': items})['items']), 2)
