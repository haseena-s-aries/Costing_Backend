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


class SingleTotalDocumentTests(SimpleTestCase):
    def test_only_a_total_becomes_one_reviewable_service_row(self):
        result = finalize({'currency': 'AED', 'grand_total': '1935.50', 'items': [
            dict(product_name='Estimated total cost', unit_price='1935.50', currency='AED',
                 extra_attributes=[{'label': 'Chargeable weight', 'value': '48.4 kg'}])]})
        self.assertEqual([(i['product_name'], i['unit_price'], i['quantity'], i['row_type']) for i in result['items']],
                         [('Shipment charges', '1935.50', None, 'SERVICE')])
        self.assertIn('single_total_document', result['_review_reasons'])
        self.assertIn({'label': 'Source label', 'value': 'Estimated total cost'}, result['items'][0]['extra_attributes'])

    def test_no_items_but_stated_total(self):
        result = finalize({'currency': 'AED', 'grand_total': '500', 'items': []})
        self.assertEqual([(i['product_name'], i['unit_price']) for i in result['items']], [('Service charges', '500')])

    def test_total_rows_next_to_real_items_are_not_turned_into_items(self):
        items = [dict(product_name='Valve', quantity='2', unit_price='50', line_total='100', currency='USD'),
                 dict(product_name='Total amount', unit_price='100', currency='USD')]
        result = finalize({'items': items, 'grand_total': '100'})
        self.assertEqual([i['product_name'] for i in result['items']], ['Valve'])


class FillerPriceLineTests(SimpleTestCase):
    def test_greeting_named_twin_is_dropped(self):
        items = [dict(product_name='Pick up of the materials from DMC to MBK Jetty', unit_price='1600', currency='AED',
                      purchase_date='2026-10-19', row_type='PRODUCT'),
                 dict(product_name='Thanks for your enquiry, pleased to offer', unit_price='1600', currency='AED',
                      purchase_date='2026-10-19', row_type='SERVICE')]
        result = finalize({'items': items})
        self.assertEqual([(i['product_name'], i['row_type']) for i in result['items']],
                         [('Pick up of the materials from DMC to MBK Jetty', 'SERVICE')])

    def test_lone_filler_row_is_kept_for_review_and_dates_matter(self):
        result = finalize({'items': [dict(product_name='Please find our best price', unit_price='90', currency='USD')]})
        self.assertEqual(len(result['items']), 1)
        self.assertIn('unnamed_price_line', result['_review_reasons'])
        items = [dict(product_name='Crane hire', unit_price='90', currency='USD', purchase_date='2026-01-01'),
                 dict(product_name='Thanks, pleased to offer', unit_price='90', currency='USD', purchase_date='2026-02-01')]
        self.assertEqual(len(finalize({'items': items})['items']), 2)

    def test_real_names_are_not_filler(self):
        from api.services.commercial_consistency import _is_filler
        for name in ('Offer for VFD supply', 'Transport offer DMC to MBK', 'Pick up of materials', 'ZB5AD2C+ZB5AZ105C'):
            self.assertFalse(_is_filler(name))


class PdfRecoveryNameTests(SimpleTestCase):
    def test_greeting_before_price_keeps_row_description(self):
        from api.services.source_reconciliation import _verified_pdf_identity
        line = 'Thanks for your enquiry, pleased to offer AED 1600/- per 7 ton from DMC to MBK Jetty.'
        self.assertEqual(_verified_pdf_identity(line, 'From DMC to MBK Jetty')[0], 'From DMC to MBK Jetty')
        self.assertEqual(_verified_pdf_identity('Launch hire AED 500/- per hour', 'x')[0], 'Launch hire')

    def test_recovered_filler_line_adopts_model_name(self):
        from api.services.gemini_extraction_service import _adopt_model_names
        recovered = [dict(product_name='From DMC to MBK Jetty', unit_price='1600', currency='AED', _filler_name=True)]
        model = [dict(product_name='Pick up of the materials from DMC to MBK Jetty', unit_price='1600', currency='AED')]
        self.assertEqual(_adopt_model_names(recovered, model)[0]['product_name'],
                         'Pick up of the materials from DMC to MBK Jetty')

    def test_default_quantity_one_does_not_block_or_survive_a_source_merge(self):
        from api.services.source_reconciliation import merge_recovery
        existing = [dict(product_name='Pick up of the materials from DMC to MBK Jetty', unit_price='1600', currency='AED',
                         quantity=1, page_number=1, purchase_date='2026-10-19')]
        fact = dict(product_name='DMC to MBK Jetty', unit_price='1600', currency='AED', quantity=None, page_number=1,
                    purchase_date='2026-10-19', _source_fact_verified=True)
        merged = merge_recovery(existing, [fact])
        self.assertEqual([(m['product_name'], m['quantity']) for m in merged],
                         [('Pick up of the materials from DMC to MBK Jetty', None)])


class DocumentReferenceDisplayTests(SimpleTestCase):
    def test_invoice_header_fields_stay_out_of_the_description(self):
        from api.services.gemini_extraction_service import build_canonical_display_name
        item = {'product_name': 'PORT STORAGE CHARGE FROM AUH PORT', 'extra_attributes': [
            {'label': label, 'value': value} for label, value in [
                ('Container', '42G0 FFAU6555040'), ('Pcs', '0'), ('Weight', '0.000'), ('POL', 'QINGDAO'),
                ('Salesman Name', 'X'), ('Voyage/Flight no', '1W1MA'), ('OBL', 'COSU1'), ('HBL', 'COSU1'),
                ('Commodity', '1'), ('ROE', '1.000000'), ('Size', '40HC')]]}
        self.assertEqual(build_canonical_display_name(item), 'PORT STORAGE CHARGE FROM AUH PORT | Size: 40HC')

    def test_unit_type_attribute_does_not_make_a_fee_a_service(self):
        from api.services.gemini_extraction_service import _canonical_row_type
        self.assertEqual(_canonical_row_type('CHARGE', {'product_name': 'DO CHARGES',
                                                        'display_name': 'DO CHARGES | Unit Type: PER SHIPMENT'}), 'CHARGE')


class PdfTableRecoveryTests(SimpleTestCase):
    def test_skipped_table_rows_are_added_once(self):
        from unittest.mock import patch
        from api.services import pdf_table_rows
        table = [['Description', 'Unit Type', 'AED', 'Comments'],
                 ['DO CHARGES', 'PER SHIPMENT', '525', 'or at actuals'],
                 ['E-TOKEN CHARGES', 'PER\nCONTAINER', '55', 'or at actuals'],
                 ['GAC SERVICE FEE', '', '750', ''],
                 ['TOTAL', '', '1330', '']]

        class Page:
            def extract_tables(self):
                return [table]

        class Pdf:
            pages = [Page()]
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch('pdfplumber.open', return_value=Pdf()):
            result = pdf_table_rows.recover_table_rows(
                {'supplier_name': 'Agent', 'document_date': '2025-12-19', 'currency': 'AED',
                 'items': [{'product_name': 'DO CHARGES', 'unit_price': '525', 'currency': 'AED'}]}, b'%PDF')
        added = [(i['product_name'], i['unit_price'], i['uom'], i['currency']) for i in result['items'][1:]]
        self.assertEqual(added, [('E-TOKEN CHARGES', '55', 'container', 'AED'), ('GAC SERVICE FEE', '750', None, 'AED')])
