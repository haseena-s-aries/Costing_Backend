import copy
import io
import os
from pathlib import Path
from unittest import skipUnless

from django.test import TestCase, override_settings

from api.services.commercial_attribute_policy import commercial_field
from api.services.msg_attachment_recovery import recover_attachments, parse_attachment
from api.services.source_reconciliation import required_metadata
from api.services.gemini_extraction_service import normalize_result, gemini_result_to_virtual_sheet, build_canonical_display_name
from api.services.import_service import ImportService
from . import test_pdf_commercial_geometry as fixtures
from .test_pdf_commercial_geometry import pdf_bytes, grid, text


@override_settings(GEMINI_API_KEY='test-no-network')
class AttachmentRecoveryTests(TestCase):
    def fixture(self):
        first = [text(25, 25, 'Example Instruments Ltd.'), text(25, 45, 'Quotation Date: 2026-04-10')]
        first += grid([25, 75, 450, 510, 570, 760], [70, 100, 140], [
            ['Item', 'Description', 'Qty', 'Unit', 'Amount (USD)'],
            ['1', 'Control Cabinet', '2', 'PC', '1,646']])
        second = grid([25, 75, 450, 510, 570], [70, 100, 160], [
            ['ITEM', 'DESCRIPTION', 'QTY', 'UNIT'],
            ['1', 'Cabinet: AB-456-IP65 700H*600W*300D RAL7000', '2', 'PC']])
        return pdf_bytes([first, second])

    def test_full_attachment_identity_and_own_metadata_survive_importrow(self):
        data = self.fixture()
        raw = dict(document_type='email_quote', category='QUOTATION',
                   supplier_name='Buyer LLC', document_date='2026-09-01', currency='JPY',
                   items=[dict(product_name='Control Cabinet', quantity='2', unit_price=None,
                               line_total='1.646', supplier_name=None)])
        result = recover_attachments(raw, [{'name': 'quoted.pdf', 'data': data}])
        result = required_metadata(normalize_result(result))
        self.assertEqual(len(result['items']), 1)
        row = result['items'][0]
        self.assertEqual(row['supplier_name'], 'Example Instruments Ltd.')
        self.assertEqual(row['purchase_date'], '2026-04-10')
        self.assertEqual(row['line_total'], '1646.00')
        self.assertIsNone(row['unit_price'])
        self.assertEqual(row['currency'], 'USD')
        self.assertIn('AB-456-IP65', row['display_name'])
        self.assertIn('700H*600W*300D', row['display_name'])
        self.assertIn('quoted.pdf', row['source_reference'])
        fixtures.PhysicalPdfPipelineTests.assert_persisted(self, result, data)

    def test_commercial_labels_never_leak_through_display_or_virtual_output(self):
        from api.views import _line_item_display_data
        from api.services.product_normalization_service import clean_display_identity, sanitize_product_identity
        labels = ['RATE (AED)', 'VAT (AED)', 'VAT (%)', 'AMOUNT (AED)', 'Price (Excl. VAT), (AED)',
                  'VAT Amount, (AED)', 'Amoun (AED', 'T o t a l P r i ce', 'Unit Price (EUR)']
        for label in labels:
            with self.subTest(label=label):
                self.assertIsNotNone(commercial_field(label))
                row = dict(product_name='Cable 50x10', display_name=f'Cable 50x10 | {label}: 300.00',
                           extra_attributes=[{'label': label, 'value': '300.00'}, {'label': 'Specification', 'value': 'IP65'}],
                           quantity='1', unit_price='300.00', line_total='315.00')
                self.assertNotIn('300.00', build_canonical_display_name(row))
                self.assertNotIn('300.00', ImportService()._build_display_name(row)['display_name'])
                self.assertNotIn('300.00', _line_item_display_data(row)['display_name'])
                self.assertNotIn('300.00', clean_display_identity(row['display_name']))
                self.assertNotIn('300.00', sanitize_product_identity(row['display_name']))
                result = required_metadata({'items': [row], '_physical_pdf_complete': True})
                sheet = gemini_result_to_virtual_sheet(result)
                mapped = dict(zip(sheet['rows'][0], sheet['rows'][1]))
                self.assertNotIn('300.00', mapped['display_name'])
                self.assertEqual(mapped['unit_price'], '300.00')
                self.assertEqual(mapped['extra_attributes'], row['extra_attributes'])
        for technical in ['Voltage Rating', 'Flow Rate', 'Material', 'Part Number', 'Quantity tier']:
            self.assertIsNone(commercial_field(technical))

    @skipUnless(os.environ.get('MSG_REGRESSION_DIR'), 'Optional source MSGs not supplied')
    def test_real_msg_attachments(self):
        import extract_msg
        root = Path(os.environ['MSG_REGRESSION_DIR'])
        expected = [('Supplier 2_*DG3367*.msg', 25), ('Supplier 3_*DG3367*.msg', 3)]
        for pattern, count in expected:
            with self.subTest(pattern=pattern):
                with extract_msg.Message(str(next(root.glob(pattern)))) as message:
                    attachments = [{'name': a.longFilename or a.shortFilename, 'data': a.data}
                                   for a in message.attachments if str(a.longFilename or a.shortFilename).lower().endswith('.pdf')]
                result = recover_attachments({'items': [], 'document_type': 'email_quote', 'category': 'QUOTATION'}, attachments)
                self.assertEqual(len(result['items']), count)
                self.assertTrue(all(i['supplier_name'] for i in result['items']))
                if count == 3:
                    self.assertTrue(all(i['purchase_date'] == '2026-04-10' for i in result['items']))
                    self.assertIn('NSX 250F TMD 200A 3P', result['items'][0]['display_name'])
                    self.assertIn('M20-M(6-12)', result['items'][1]['display_name'])
                    self.assertEqual(result['items'][0]['line_total'], '1646.00')
                else:
                    self.assertEqual(result['items'][0]['purchase_date'], '2026-04-14')
                    self.assertIn('NSX 250F TMD 200A 3P', result['items'][4]['product_name'])
                    self.assertIsNone(result['items'][4]['purchase_date'])
                fixtures.PhysicalPdfPipelineTests.assert_persisted(self, required_metadata(normalize_result(result)), self.fixture())

    @skipUnless(os.environ.get('MSG_REGRESSION_DIR'), 'Optional source PDFs not supplied')
    def test_real_pdf_commercial_columns_are_hidden_after_finalization(self):
        from api.services.gemini_extraction_service import _finalize_pdf_extraction_result
        root = Path(os.environ['MSG_REGRESSION_DIR'])
        for filename, count in [('ARIES QUOTATION 1275_20261012_122916.pdf', 2), ('PRE-QN-22748-1_20261012_082045.pdf', 1)]:
            data = (root / filename).read_bytes()
            rows = parse_attachment(data, filename)
            self.assertEqual(len(rows), count)
            for row in rows:
                row['extra_attributes'] += [{'label': 'RATE (AED)', 'value': row['unit_price']}, {'label': 'VAT Amount, (AED)', 'value': '15.00'}]
                row['display_name'] += ' | RATE (AED): 300.00 | VAT Amount, (AED): 15.00'
            result = _finalize_pdf_extraction_result(dict(document_type='offer', category='QUOTATION', items=rows), data)
            result = required_metadata(result)
            for row in result['items']:
                self.assertNotIn('RATE (AED):', row['display_name'])
                self.assertNotIn('VAT Amount', row['display_name'])
            fixtures.PhysicalPdfPipelineTests.assert_persisted(self, result, data)
