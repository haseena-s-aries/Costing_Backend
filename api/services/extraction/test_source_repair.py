from pathlib import Path
from django.test import SimpleTestCase
from api.services.gemini_extraction_service import (
    _recover_msg_product_name, _valid_date, normalize_result,
    _extract_pdf_email_tariff_charge_items,
    _split_msg_commercial_blocks, _msg_block_date,
    _recover_pdf_full_description_cells,
)
from api.services.source_table_repair import restore_table_columns


class SourceRepairTests(SimpleTestCase):
    def test_msg_thread_keeps_each_dated_priced_block(self):
        body = (
            "Date: 2026-03-25\nE3SUPS10KH Price: AED 15750\n"
            "From: seller@example.com\nDate: 2026-07-28\n"
            "E3SUPS10KH Price: AED 20250"
        )
        blocks = _split_msg_commercial_blocks(body)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(_msg_block_date(blocks[0]), "2026-03-25")
        self.assertEqual(_msg_block_date(blocks[1]), "2026-07-28")

    def test_msg_block_date_accepts_outlook_weekday_and_time(self):
        self.assertEqual(
            _msg_block_date("Sent: Wednesday, 3 June 2026 12:09 pm"),
            "2026-06-03",
        )

    def test_numbered_blocks_bind_by_price_and_keep_details(self):
        body = 'Dear Sir:\n1. Pump assembly:\nAX-10, 460V, heater\nPrice: USD500/PC\n2. Fan assembly:\nBX-20, 220V\nPrice: USD200/PC'
        self.assertEqual(_recover_msg_product_name('MOTOR', body, unit_price='200'), 'Fan assembly: BX-20, 220V')
        self.assertEqual(_recover_msg_product_name('Dear Sir: | MOTOR', body, unit_price='500'), 'Pump assembly: AX-10, 460V, heater')

    def test_ambiguous_prices_do_not_copy_one_description(self):
        body = '1. Pump:\nAX-10\nPrice: USD200/PC\n2. Fan:\nBX-20\nPrice: USD200/PC'
        self.assertEqual(_recover_msg_product_name('Unresolved', body, unit_price='200'), 'Unresolved')

    def test_ordinal_page_date_is_not_lost(self):
        self.assertEqual(_valid_date('19th.Jan.2026'), '2026-01-19')
        result = normalize_result({'document_date': '2026-01-12', 'items': [{'product_name':'A', 'purchase_date':'2026-01-19'}]})
        self.assertEqual(result['items'][0]['purchase_date'], '2026-01-19')

    def test_product_certification_spec_is_not_a_charge(self):
        result = normalize_result({'items':[{'row_type':'PRODUCT','product_name':'Industrial Computer | Certification: IEC60945'}]})
        self.assertEqual(result['items'][0]['row_type'], 'PRODUCT')

    def test_actual_certification_charge_stays_charge(self):
        result = normalize_result({'items':[{'row_type':'CHARGE','product_name':'CCS certification'}]})
        self.assertEqual(result['items'][0]['row_type'], 'CHARGE')

    def test_separate_source_columns_are_not_removed_by_label_count(self):
        attrs = [{'label':'Voltage','value':'220V'}, {'label':'Power','value':'5KW'}]
        result = normalize_result({'items':[{'product_name':'Pump','extra_attributes':attrs}]})
        self.assertEqual(result['items'][0]['extra_attributes'], attrs)

    def test_pdf_source_structure(self):
        inputs = Path('test_fixtures/inputs')
        paths = list(inputs.glob('WinMate*20260814*.pdf'))
        if not paths:
            self.skipTest('Local source fixture unavailable')
        result = restore_table_columns({'items':[{'product_name':'Incomplete', 'page_number':1, 'unit_price':'22900', 'quantity':'1', 'uom':'unit', 'line_total':'22900'}]}, paths[0].read_bytes())
        item = result['items'][0]
        self.assertIn('i5-1335U', item['product_name'])
        self.assertEqual(item['extra_attributes'], [{'label':'型号','value':'R15IAD3S-MRA3FP'}])
        for field in ('quantity','uom','line_total'):
            self.assertIsNone(item[field])

    def test_single_description_cell_keeps_embedded_specifications(self):
        path = Path('test_fixtures/unique_100_input/4. SUPPLIER 1- QTN-3014 ARIES MARINE (5)_20260907_080136.pdf')
        if not path.exists():
            self.skipTest('Local source fixture unavailable')
        result = {
            'items': [{
                'row_type': 'PRODUCT',
                'product_name': 'Pilot Ladder 6Mtr Step Brace Type',
                'unit_price': '810.00',
            }]
        }
        recovered = _recover_pdf_full_description_cells(path.read_bytes(), result)
        item = normalize_result(recovered)['items'][0]
        self.assertIn('Mechanically Fastened With Aluminium Ferrules', item['product_name'])
        self.assertIn('SOLAS 1974', item['product_name'])
        self.assertIn('DNV certificate', item['product_name'])
        self.assertIn('Distance Between Steps', item['product_name'])
        self.assertIsNone(item.get('specification'))

    def test_pricing_condition_is_not_recovered_as_tariff(self):
        paths = list(Path('test_fixtures/inputs').glob('*Jiangsu*.pdf'))
        if not paths:
            self.skipTest('Local source fixture unavailable')
        rows = _extract_pdf_email_tariff_charge_items(paths[0].read_bytes())
        self.assertFalse(any(row.get('unit_price') == '200' for row in rows))

    def test_cable_and_second_supplier_columns(self):
        import json
        paths = list(Path('test_fixtures/inputs').glob('*Jiangsu*.pdf'))
        if not paths:
            self.skipTest('Local source fixture unavailable')
        expected = json.loads((Path('test_fixtures/expected/extraction') / (paths[0].stem + '.json')).read_text(encoding='utf-8'))
        for i, row in enumerate(expected['items']):
            row['page_number'] = 1 if i < 11 else 2
            row['extra_attributes'] = []
        repaired = normalize_result(restore_table_columns(expected, paths[0].read_bytes()))
        self.assertEqual(len(repaired['items']), 13)
        self.assertEqual(repaired['items'][0]['extra_attributes'], [{'label':'Size','value':'1x2x0.75'}])
        self.assertEqual(repaired['items'][-1]['extra_attributes'], [{'label':'Remark','value':'Genuine original'}])
        self.assertEqual(repaired['items'][-1]['purchase_date'], '2026-01-19')
