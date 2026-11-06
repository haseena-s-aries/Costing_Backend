from copy import deepcopy
from django.test import SimpleTestCase
from api.services.extraction.test_pdf_commercial_geometry import pdf_bytes, text
from api.services.source_reconciliation import finalize_pdf
from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet


class ConditionalTariffTests(SimpleTestCase):
    def fixture(self):
        pages = [
            ['From: ops@example.com', 'Sent: Tuesday, 28 April 2026 10:12 AM',
             'To: Buyer LLC', 'Please find our tariff for equipment at Harbour East.',
             'EQUIPMENT HIRE OPERATION - USD 2,400/- FOR 06 HRS (LUMPSUM).',
             'THERE AFTER USD 210/- PER HR AFTER 06 HRS.',
             'Best Regards,', 'Example Services LLC'],
            ['Equipment hire (5 hrs 20 min)', 'Equipment hire (210 USD /hr) 8814',
             'Crane rental 900', 'Total amount (AED) 9714']]
        return pdf_bytes([[text(30, 30+i*20, line) for i, line in enumerate(page)] for page in pages])

    def test_canonical_rates_replace_model_copies_and_keep_summary_currency(self):
        data = {'items': [dict(product_name=name, unit_price=price, page_number=page,
                               quantity='5.333333333', currency=currency)
                          for name, price, page, currency in [
                              ('Equipment Hire (Hourly)', '210', 1, 'USD'),
                              ('Equipment hire 8814', '210', 2, 'AED'),
                              ('Crane rental 900', '900', 2, 'AED')]]}
        result = finalize_pdf(data, self.fixture())
        self.assertEqual(len(result['items']), 4)
        self.assertEqual([(r['unit_price'], r['currency']) for r in result['items']],
                         [('2400', 'USD'), ('210', 'USD'), ('8814', 'AED'), ('900', 'AED')])
        self.assertTrue(all(r['row_type'] == 'SERVICE' for r in result['items']))
        self.assertTrue(all(r['quantity'] is None for r in result['items']))
        self.assertIn('First 6 hours', result['items'][0]['product_name'])
        self.assertIn('Harbour East', result['items'][0]['product_name'])
        self.assertEqual(result['items'][1]['uom'], 'HR')
        self.assertEqual(result['items'][2]['product_name'], 'Equipment hire')
        self.assertEqual(finalize_pdf(deepcopy(result), self.fixture())['items'], result['items'])
        sheet = gemini_result_to_virtual_sheet(normalize_result(deepcopy(result)))
        mapped = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertEqual(len(mapped), 4)
        self.assertEqual([str(r['unit_price']) for r in mapped], ['2400', '210', '8814', '900'])

    def test_unrelated_priced_item_survives(self):
        result = finalize_pdf({'items': [dict(product_name='Spare motor', unit_price='210',
                                            currency='USD', page_number=1)]}, self.fixture())
        self.assertEqual(len(result['items']), 5)
        self.assertTrue(any(r['product_name'] == 'Spare motor' for r in result['items']))
