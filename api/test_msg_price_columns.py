from decimal import Decimal
from django.test import SimpleTestCase
from api.services.msg_price_table import restore_price_tables
from api.services.gemini_extraction_service import _decimal, build_canonical_display_name, gemini_result_to_virtual_sheet


class PriceColumnTests(SimpleTestCase):
    def test_numeric_separators(self):
        for value, expected in [('20,300', '20300'), ('18,001', '18001'),
                                ('4,809', '4809'), ('14,99', '14.99'),
                                ('1.234,56', '1234.56'), ('1,234.56', '1234.56')]:
            self.assertEqual(_decimal(value), Decimal(expected))

    def test_final_rate_not_total_and_financial_columns_not_identity(self):
        headers = ['Model', 'Previous PO price', 'Interface', 'Sensor', 'Price hike',
                   'Unit price in USD', 'Certification', 'Unit price in USD', 'Qty', 'Total Price in USD']
        data = [['MODEL-A', '7300', '250', '155', '5%', '7,884', '2,266', '10,150', '2', '20,300'],
                ['MODEL-B', '6295', '250', '155', '5%', '6,856', '2,145', '9,001', '2', '18,001']]
        html = '<table>' + ''.join('<tr>' + ''.join('<td>'+c+'</td>' for c in row) + '</tr>' for row in [headers]+data) + '</table>'
        body = '\n'.join(c for row in [headers]+data for c in row) + '\nRegards\nSeller Ltd'
        result = {'items': [{'product_name': row[0], 'unit_price': row[-1], 'line_total': row[-1],
                             'currency': 'USD', 'purchase_date': '2026-01-01'} for row in data]}
        result = restore_price_tables(result, html, body, sent_date='2026-01-01')
        self.assertEqual([r['unit_price'] for r in result['items']], ['10150', '9001'])
        self.assertEqual([r['line_total'] for r in result['items']], ['20300', '18001'])
        self.assertEqual([build_canonical_display_name(r) for r in result['items']], ['MODEL-A', 'MODEL-B'])
        sheet = gemini_result_to_virtual_sheet(result)
        self.assertEqual(len(sheet['rows']), 3)
        mapped = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertEqual([Decimal(str(r['unit_price'])) for r in mapped], [Decimal('10150'), Decimal('9001')])
        self.assertEqual([Decimal(str(r['line_total'])) for r in mapped], [Decimal('20300'), Decimal('18001')])

    def test_technical_sensor_spec_remains_but_price_labels_hidden(self):
        item = {'product_name': 'Device', 'extra_attributes': [
            {'label': 'Sensor', 'value': '4-20 mA'},
            {'label': 'Previous PO Price', 'value': '700'},
            {'label': 'Price hike', 'value': '5%'}]}
        self.assertEqual(build_canonical_display_name(item), 'Device | Sensor: 4-20 mA')
