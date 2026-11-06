"""Real PDF text/grid recovery, including persisted preview rows; no model calls."""
import copy
import io
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from api.models import UploadedFile, ImportBatch, ImportSheet, ImportRow
from api.services.gemini_extraction_service import (
    _finalize_pdf_extraction_result, normalize_result, gemini_result_to_virtual_sheet,
    GeminiExtractionService,
)
from api.services.import_service import ImportService


def pdf_bytes(pages):
    writer = PdfWriter()
    for commands in pages:
        page = writer.add_blank_page(width=800, height=700)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(commands).encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def text(x, top, value):
    value = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    return f'BT /F1 9 Tf {x} {700-top} Td ({value}) Tj ET'


def grid(xs, ys, rows):
    commands = [f'{x} {700-ys[0]} m {x} {700-ys[-1]} l S' for x in xs]
    commands += [f'{xs[0]} {700-y} m {xs[-1]} {700-y} l S' for y in ys]
    for row, values in enumerate(rows):
        commands += [text(xs[col]+4, ys[row]+18, value) for col, value in enumerate(values) if value]
    return commands


def matrix_fixture():
    xs = [30, 160, 265, 300, 340, 445, 550, 655, 760]
    header = ['Name', 'Specification', 'Unit', 'Qty', 'Alpha Ltd.', 'Beta LLC', 'Gamma Inc.', 'Delta GmbH']
    first = grid(xs, [60, 100, 140], [header, ['Tool A', 'Steel', 'PC', '2', '10', '20', '30', '40']])
    second = grid(xs, [60, 100, 140, 180], [
        ['Tool B', 'Small', 'PC', '2', '', '600', '635', '630'],
        ['Tool C', 'Large', 'PC', '1', '', '600', '4450', '4675'],
        ['Total', '', '', '', '', '1200', '5085', '5305'],
    ])
    return pdf_bytes([first, second])


def quote_fixture():
    xs = [30, 400, 560, 760]
    first = [text(30, 40, 'Prices in USD'), text(30, 80, '1 Pressure Sensor AB100 Fresh Water')]
    first += grid([30, 400, 760], [140, 170, 200], [['Part Number', 'Unit Price'], ['AB100-F', '$103.00']])
    first += [text(30, 250, '2 Pressure Sensor AB100 Sea Water')]
    second = grid([30, 400, 760], [40, 70, 100], [['Part Number', 'Unit Price'], ['AB100-S', '$165.00']])
    second += [text(30, 140, '3 Temperature Sensor CD200')]
    second += grid(xs, [180, 210, 240], [['Part Number', '1-9 PCS', '10+ PCS'], ['CD200-X', '$119.00', '$107.00']])
    return pdf_bytes([first, second])


@override_settings(GEMINI_API_KEY='test-no-network')
class PhysicalPdfPipelineTests(TestCase):
    def finalize(self, payload):
        raw = dict(document_type='quotation', category='QUOTATION', currency='USD',
                   supplier_name='Example Ltd.', document_date='2026-01-15',
                   items=[dict(product_name='Wrong fallback', unit_price='600', line_total='1200')])
        return normalize_result(_finalize_pdf_extraction_result(raw, payload))

    def assert_persisted(self, result, payload):
        virtual = gemini_result_to_virtual_sheet(copy.deepcopy(result))
        fields = ('product_name', 'supplier_name', 'quantity', 'unit_price', 'line_total', 'page_number', 'extra_attributes', 'purchase_date', 'source_reference')
        expected = [tuple(item.get(f) for f in fields) for item in result['items']]
        self.assertEqual(expected, [tuple(dict(zip(virtual['rows'][0], row)).get(f) for f in fields) for row in virtual['rows'][1:]])
        with override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}}):
            upload = UploadedFile.objects.create(original_file_name='generated.pdf', file=ContentFile(payload, name='generated.pdf'))
            batch = ImportBatch.objects.create(batch_number=f'geometry-test-{upload.pk}', uploaded_file=upload,
                category='QUOTATION', default_currency='USD',
                document_metadata={k: v for k, v in result.items() if k != 'items'})
            sheet = ImportSheet.objects.create(import_batch=batch, sheet_name='Gemini Extracted',
                cached_headers=virtual['rows'][0], cached_rows=virtual['rows'])
            service = ImportService()
            service.preview(batch, [sheet.pk], limit=100)
            rows = list(ImportRow.objects.filter(import_batch=batch).order_by('row_number'))
            self.assertEqual(expected, [tuple(row.normalized_data.get(f) for f in fields) for row in rows])
            self.assertEqual(expected, [tuple(service._row_import_data(row).get(f) for f in fields) for row in rows])

    def test_blank_column_and_repeated_price_use_xy_not_model_order(self):
        payload = matrix_fixture()
        result = self.finalize(payload)
        self.assertTrue(result.get('_physical_pdf_complete'))
        self.assertEqual(len(result['items']), 10)
        continuation = result['items'][4:]
        self.assertEqual([(i['product_name'], i['supplier_name'], i['unit_price']) for i in continuation], [
            ('Tool B', 'Beta LLC', '600.00'), ('Tool B', 'Gamma Inc.', '635.00'), ('Tool B', 'Delta GmbH', '630.00'),
            ('Tool C', 'Beta LLC', '600.00'), ('Tool C', 'Gamma Inc.', '4450.00'), ('Tool C', 'Delta GmbH', '4675.00')])
        self.assertTrue(all(i['unit_price'] == i['line_total'] for i in result['items']))
        self.assert_persisted(result, payload)

    def test_progressive_extraction_runs_physical_recovery_before_return(self):
        payload = matrix_fixture()
        service = object.__new__(GeminiExtractionService)
        model_result = dict(document_type='price_list', category='PRICE_LIST', currency='USD',
                            document_date='2026-01-15', supplier_name=None, items=[])
        with patch.object(service, '_extract_large_pdf_progressively', return_value=model_result), \
             patch.object(service, '_verify_visible_document_date', return_value='2026-01-15'), \
             patch.object(service, '_verify_visible_document_category', return_value='PRICE_LIST'):
            result = service.extract_from_file(io.BytesIO(payload), 'generated.pdf')
        self.assertTrue(result.get('_physical_pdf_complete'))
        self.assertEqual(len(result['items']), 10)
        self.assertTrue(all(i.get('supplier_name') for i in result['items']))

    def test_price_before_next_heading_belongs_to_previous_page_product(self):
        payload = quote_fixture()
        result = self.finalize(payload)
        self.assertEqual(len(result['items']), 4)
        self.assertEqual(result['items'][1]['product_name'], 'Pressure Sensor AB100 Sea Water')
        self.assertEqual(result['items'][1]['unit_price'], '165.00')
        self.assertEqual([i['unit_price'] for i in result['items'][2:]], ['119.00', '107.00'])
        self.assertTrue(all(i['quantity'] is None and i['line_total'] is None for i in result['items']))
        self.assert_persisted(result, payload)

    @skipUnless(os.environ.get('MATRIX_REGRESSION_PDF'), 'Optional customer PDF not supplied')
    def test_customer_matrix_final_preview(self):
        payload = Path(os.environ['MATRIX_REGRESSION_PDF']).read_bytes()
        result = self.finalize(payload)
        self.assertEqual(len(result['items']), 64)
        continuation = [i for i in result['items'] if i['page_number'] == 2]
        self.assertEqual([i['unit_price'] for i in continuation], ['600.00', '635.00', '630.00', '4300.00', '4450.00', '4675.00'])
        self.assertEqual([i['_supplier_column_index'] for i in continuation], [1, 2, 3, 1, 2, 3])
        self.assertTrue(all(i['supplier_name'] for i in result['items']))
        self.assertEqual(len({tuple(i['_comparison_cell_key']) for i in result['items']}), 64)
        self.assert_persisted(result, payload)

    @skipUnless(os.environ.get('QUOTE_REGRESSION_PDF'), 'Optional customer PDF not supplied')
    def test_customer_quote_final_preview(self):
        payload = Path(os.environ['QUOTE_REGRESSION_PDF']).read_bytes()
        result = self.finalize(payload)
        self.assertEqual(len(result['items']), 4)
        self.assertTrue(result['supplier_name'])
        self.assertTrue(result['document_date'])
        self.assertTrue(any(a['label'] for a in result['items'][0]['extra_attributes']))
        self.assertTrue(any(a['value'] for a in result['items'][1]['extra_attributes']))
        self.assertIn('Sea Water', result['items'][1]['product_name'])
        self.assertEqual([i['unit_price'] for i in result['items']], ['103.00', '165.00', '119.00', '107.00'])
        self.assert_persisted(result, payload)
