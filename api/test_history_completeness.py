from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
from api.views import ImportHistoryDetailView
from api.services.msg_commercial_history import restore_offer_context


class HistoryCompletenessTests(SimpleTestCase):
    def test_detail_returns_rows_beyond_100_for_history_and_review(self):
        source = [SimpleNamespace(id=i, raw_data={}, mapped_data={'product_name': f'Part {i}'},
                                  normalized_data={}, row_type='PRODUCT') for i in range(1, 110)]
        batch = MagicMock()
        batch.rows.all.return_value.order_by.return_value = source
        batch.rows.all.return_value.__getitem__.side_effect = lambda key: source[key]
        batch.id = 1
        batch.batch_number = 'TEST'
        batch.uploaded_file.original_file_name = 'generic.xlsx'
        batch.document_metadata = {}
        batch.category = 'QUOTATION'
        batch.status = 'preview_ready'
        batch.error_message = ''
        for field in ('total_rows', 'imported_rows', 'failed_rows', 'duplicate_rows', 'warning_rows'):
            setattr(batch, field, 109 if field == 'total_rows' else 0)
        batch.sheets.all.return_value = []
        batch.column_mappings.all.return_value = []
        with patch('api.views.get_object_or_404', return_value=batch), patch(
            'api.views.ImportService._preview_row', side_effect=lambda row: {
                'row_id': row.id, 'row_type': row.row_type, 'mapped_data': dict(row.mapped_data)}
        ), patch('api.views.ok', side_effect=lambda data: data):
            result = ImportHistoryDetailView().get(None, 1)
        self.assertEqual(len(result['rows']), 109)
        self.assertEqual(result['rows'][-1]['row_id'], 109)

    def test_offer_signature_and_route_are_bound_to_priced_block(self):
        body = ('Collection from\nOrigin\nDelivery to\nDestination\nFreight - USD 72\n'
                'Regards\nActual Seller LLC\n'
                'From: Buyer\nSent: August 3, 2026\nPlease quote\nRegards\nBuyer Inc.')
        item = {'product_name': 'Freight', 'unit_price': '72.00', 'currency': 'USD',
                'purchase_date': '2026-08-04', 'supplier_name': 'Wrong Seller'}
        result = restore_offer_context({'items': [item]}, body, sent_date='2026-08-04')
        self.assertEqual(result['items'][0]['supplier_name'], 'Actual Seller LLC')
        self.assertEqual(item['extra_attributes'], [
            {'label': 'Collection from', 'value': 'Origin'}, {'label': 'Delivery to', 'value': 'Destination'}])

    def test_attachment_supplier_is_not_replaced_by_mail_signature(self):
        item = {'_attachment_name': 'quote.pdf', 'supplier_name': 'Attachment Seller'}
        restore_offer_context({'items': [item]}, 'Regards\nEmail Seller LLC')
        self.assertEqual(item['supplier_name'], 'Attachment Seller')

    def test_final_offer_merge_preserves_source_details_and_separate_routes(self):
        body = ('Shipment from Northport to Southport\nAgreed Rates: USD: 72\n'
                'Collection Address: 12 Market Road\nRegards\nExample Freight LLC')
        base = {'unit_price': '72.00', 'currency': 'USD', 'purchase_date': '2026-08-04',
                'supplier_name': 'Example Freight LLC', 'extra_attributes': []}
        canonical = dict(base, product_name='Shipment from Northport to Southport', _msg_source_verified=True)
        fallback = dict(base, product_name='Shipment of equipment from Northport to Southport',
                        extra_attributes=[{'label': 'Collection Address', 'value': '12 Market Road'}])
        separate = dict(base, product_name='Shipment from Northport to Eastport')
        attachment = dict(fallback, _attachment_name='another-quote.pdf')
        result = restore_offer_context({'items': [canonical, fallback, separate, attachment]},
                                       body, sent_date='2026-08-04')
        self.assertEqual(len(result['items']), 3)
        self.assertIn({'label': 'Collection Address', 'value': '12 Market Road'}, canonical['extra_attributes'])
        self.assertIn(separate, result['items'])
        self.assertIn(attachment, result['items'])

    def test_supplied_msg_regressions_through_extraction(self):
        import json
        from pathlib import Path
        from unittest.mock import Mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        from api.services.gemini_extraction_service import GeminiExtractionService, gemini_result_to_virtual_sheet
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        fixtures = [
            ('RE_ MS3806 __ Shipment from UK - Pasadena_Gas detectors_Surya Veerya_20260820_100141.msg', '1503'),
            ('RE_ Shipment from UK - Pasadena_Gas detectors_Surya Veerya__EXW__UK TO USA__COR__CR0012634147_20260820_100140.msg', '1200'),
        ]
        for name, price in fixtures:
            with self.subTest(file=name):
                path = Path('C:/Users/admin/Downloads') / name
                if not path.exists():
                    self.skipTest('Optional user regression fixture unavailable')
                captured = {'document_type': 'email_quote', 'category': 'QUOTATION',
                            'supplier_name': 'Example Wrong Seller LLC', 'document_date': '2026-08-04',
                            'currency': 'AED', 'items': [{'product_name': 'Freight - Door to Door',
                            'row_type': 'CHARGE', 'unit_price': price, 'line_total': price,
                            'quantity': '1', 'purchase_date': '2026-08-04', 'currency': 'AED'}]}
                service = object.__new__(GeminiExtractionService)
                service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
                service._log_usage = Mock()
                orchestrator = ExtractionOrchestrator()
                orchestrator.gemini = service
                result = orchestrator.extract(SimpleUploadedFile(name, path.read_bytes()), name)
                self.assertEqual(len(result['items']), 1)
                if price == '1200':
                    self.assertEqual(result['items'][0]['supplier_name'], 'Abreco Freight LLC')
                else:
                    self.assertIn('from UK to USA', result['items'][0]['display_name'])
                    attrs = {a['label'].casefold(): a['value'] for a in result['items'][0]['extra_attributes']}
                    self.assertEqual(attrs['collection from'], 'UK')
                    self.assertEqual(attrs['delivery to'], 'USA')
                sheet = gemini_result_to_virtual_sheet(result)
                self.assertEqual(len(sheet['rows']), 2)
