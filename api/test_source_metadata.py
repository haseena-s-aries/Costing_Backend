from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from api.services.extraction.deterministic_parser import DeterministicSpreadsheetParser
from api.services.extraction.structure_detector import StructureDetector
from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet, _normalize_supplier_name
from api.services.import_service import build_review_reasons
from api.services.source_reconciliation import required_metadata


class SourceMetadataTests(SimpleTestCase):
    def test_numbered_children_keep_parent_through_virtual_sheet(self):
        values = [
            ['No.', 'Description', 'Supply Model', 'Qty', 'Unit Price', 'Amount'],
            ['2', 'Installation accessories', '', '', '', ''],
            ['2.1', 'Small component', 'X-1', 2, 4, 8],
            ['2.2', 'Large component', 'X-2', 1, 9, 9],
            ['3', 'Standalone component', 'X-3', 1, 5, 5],
        ]
        document = {'file_type': 'spreadsheet', 'rows': [
            {'sheet': 'Quote', 'row_number': i + 1, 'values': row}
            for i, row in enumerate(values)
        ]}
        result = DeterministicSpreadsheetParser().extract(document, StructureDetector().detect(document))
        result = normalize_result(result)
        sheet = gemini_result_to_virtual_sheet(result)
        output = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertEqual(len(output), 3)
        for row in output[:2]:
            self.assertEqual(row['parent_category'], 'Installation accessories')
            self.assertTrue(row['display_name'].startswith('Installation accessories | '))
        self.assertIsNone(output[2]['parent_category'])

    def test_contact_number_is_not_a_supplier(self):
        for phone in ('+44 20 1234 5678', 'Tel: (020) 1234-5678', 'Mobile No. +91 98765 43210'):
            with self.subTest(phone=phone):
                self.assertIsNone(_normalize_supplier_name(phone))
                result = required_metadata(normalize_result({
                    'supplier_name': phone, 'document_date': '2026-09-12', 'currency': 'AED',
                    'items': [{'product_name': 'Transport work', 'row_type': 'SERVICE',
                               'supplier_name': phone, 'unit_price': '50', 'line_total': '50'}],
                }))
                self.assertIsNone(result['supplier_name'])
                self.assertIsNone(result['items'][0]['supplier_name'])
                self.assertIn('missing_supplier_name', result['_review_reasons'])
                self.assertTrue(result['_needs_review'])
        self.assertEqual(_normalize_supplier_name('3M Industrial Ltd'), '3M Industrial Ltd')

    def test_old_phone_supplier_rows_still_require_review(self):
        row = SimpleNamespace(normalized_data={'supplier_name': '+44 20 1234 5678'}, mapped_data={}, raw_data={}, row_type='SERVICE')
        batch = SimpleNamespace(rows=MagicMock())
        batch.rows.all.return_value = [row]
        with patch('api.services.import_service.resolve_import_metadata', return_value={}):
            self.assertIn('Supplier name missing', build_review_reasons(batch))

    def test_labelled_ordinal_date_and_adjacent_technical_column_survive_output(self):
        values = [
            ['Example Industrial Ltd', '', '', '', '', ''],
            ['Date:', '21st Sept. 2026', '', '', '', ''],
            ['Description', 'Supply Model', 'Qty', 'Unit', 'Unit Price\n(USD)', 'Amount\n(USD)'],
            ['Circuit component', 'ZX-20 with mounting base', 2, 'PC', 15, 30],
        ]
        document = {'file_type': 'spreadsheet', 'rows': [
            {'sheet': 'Quote', 'row_number': index + 1, 'values': row}
            for index, row in enumerate(values)
        ]}
        result = DeterministicSpreadsheetParser().extract(document, StructureDetector().detect(document))
        self.assertIsNotNone(result)
        result = required_metadata(normalize_result(result))
        self.assertEqual(result['document_date'], '2026-09-21')
        item = result['items'][0]
        self.assertEqual(item['purchase_date'], '2026-09-21')
        self.assertIn({'label': 'Supply Model', 'value': 'ZX-20 with mounting base'}, item['extra_attributes'])
        self.assertIn('Supply Model: ZX-20 with mounting base', item['display_name'])
        virtual = gemini_result_to_virtual_sheet(result)
        output = dict(zip(virtual['rows'][0], virtual['rows'][1]))
        self.assertEqual(output['purchase_date'], '2026-09-21')
        self.assertIn('Supply Model: ZX-20 with mounting base', output['display_name'])
