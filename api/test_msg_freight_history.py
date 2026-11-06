import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from api.services.msg_commercial_history import extract_agreed_rate_history


class FreightHistoryTests(SimpleTestCase):
    def test_attribute_route_and_prefixed_shipment_are_same_physical_offer(self):
        from copy import deepcopy
        from api.services.msg_commercial_history import finalize_physical_freight_offers
        offer = 'Collection from\nOrigin\nDelivery to\nDestination\nFreight - USD 85\n'
        body = 'Shipment 1\n' + offer + 'Shipment 2\n' + offer + 'Regards\nSeller LLC'
        history = extract_agreed_rate_history(body, sent_date='2026-08-04')
        duplicate = deepcopy(history['items'][0])
        duplicate.pop('source_row_id', None)
        duplicate['product_name'] = 'Freight'
        duplicate['extra_attributes'] = [
            {'label': 'Collection from', 'value': 'Origin'},
            {'label': 'Delivery to', 'value': 'Destination'},
            {'label': 'Shipment', 'value': 'SHIPMENT 1'},
        ]
        result = finalize_physical_freight_offers(
            {'items': history['items'] + [duplicate, deepcopy(duplicate)]},
            body, sent_date='2026-08-04')
        self.assertEqual(len(result['items']), 2)
        self.assertIn({'label': 'Shipment', 'value': '2'}, result['items'][1]['extra_attributes'])

    def test_separate_equal_price_shipments_and_previous_dates(self):
        offer = ('Collection from\nNorthport\nDelivery to\nSouthport\n'
                 'Weight\n7 KG\nMode\nAIR\nTransit Time\n4 days\nFreight - USD 85 /-\n')
        body = ('SHIPMENT 1\n' + offer + 'SHIPMENT 2\n' + offer +
                'Important Notes:\nAdditional 900 USD if non-stackable\n'
                'Thanks & regards,\nSeller Freight Agency, Some City\n'
                'From: Sales <sales@seller.example>\nSent: July 2, 2026\n' +
                offer.replace('85', '79') + 'Regards\nSeller Freight Agency, Some City')
        result = extract_agreed_rate_history(body, 'sales@seller.example', '2026-07-03')
        self.assertEqual([r['unit_price'] for r in result['items']], ['85', '85', '79'])
        self.assertEqual(len({r['source_row_id'] for r in result['items']}), 3)
        self.assertTrue(all(r['supplier_name'] == 'Seller Freight Agency' for r in result['items']))
        self.assertEqual(result['items'][-1]['purchase_date'], '2026-07-02')

    def test_user_thread_through_production_pipeline_with_first_only_model_response(self):
        from api.services.gemini_extraction_service import GeminiExtractionService, gemini_result_to_virtual_sheet
        from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
        path = Path('C:/Users/admin/Downloads/RE_ MS3755 __ Shipment from UK to Singapore_Texas_20260820_045652.msg')
        if not path.exists():
            self.skipTest('Optional local source fixture unavailable')
        captured = {'document_type': 'email_quote', 'category': 'QUOTATION',
                    'supplier_name': 'Orient Cargo Agency', 'document_date': '2026-08-04',
                    'currency': 'AED', 'items': [{'product_name': 'Freight from UK to SINGAPORE',
                    'row_type': 'CHARGE', 'unit_price': '1810', 'line_total': '1810',
                    'quantity': '1', 'purchase_date': '2026-08-04', 'currency': 'AED'}]}
        # The real regression includes all four model offers, with the mode
        # appended to the route and separate equal-price numbered shipments.
        captured['items'] = [dict(captured['items'][0], product_name=name, unit_price=price, line_total=price)
                             for name, price in [
                                 ('Freight from UK to SINGAPORE (AIR - Door to Door – FEDEX)', '1810'),
                                 ('Freight for Shipment 1 from UK to SINGAPORE (AIR - Door to Door – FEDEX)', '1333'),
                                 ('Freight for Shipment 2 from UK to SINGAPORE (AIR - Door to Door – FEDEX)', '1333'),
                                 ('Freight for Shipment 3 from UK to USA (AIR - Door to Door – FEDEX)', '1359')]]
        service = object.__new__(GeminiExtractionService)
        service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
        service._log_usage = Mock()
        orchestrator = ExtractionOrchestrator()
        orchestrator.gemini = service
        result = orchestrator.extract(SimpleUploadedFile(path.name, path.read_bytes()), path.name)
        self.assertEqual(len(result['items']), 7)
        self.assertEqual([float(r['unit_price']) for r in result['items']],
                         [1810, 1333, 1333, 1359, 1357, 1325, 1930])
        self.assertTrue(all(r['supplier_name'] == 'Orient Cargo Agency' for r in result['items']))
        self.assertEqual([r['purchase_date'] for r in result['items']],
                         ['2026-08-04'] * 4 + ['2026-07-30'] * 2 + ['2026-07-29'])
        sheet = gemini_result_to_virtual_sheet(result)
        self.assertEqual(len(sheet['rows']), 8)
        rows = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertIn('Shipment: 1', rows[1]['display_name'])
        self.assertIn('Shipment: 2', rows[2]['display_name'])

    def test_finalizer_restores_corrupt_label_and_protects_other_quotes(self):
        from copy import deepcopy
        from api.services.msg_commercial_history import finalize_physical_freight_offers
        offer = 'Collection from\nOrigin\nDelivery to\nDestination\nFreight - USD 85\n'
        body = 'Shipment 1\n'+offer+'Shipment 2\n'+offer+'Regards\nSeller LLC'
        history = extract_agreed_rate_history(body, sent_date='2026-08-04')
        corrupted = deepcopy(history['items'])
        for a in corrupted[1]['extra_attributes']:
            if a['label'] == 'Shipment': a['value'] = '1'
        fallback = dict(corrupted[0], source_row_id=None, _msg_source_verified=False,
                        product_name='Freight for Shipment 2 from Origin to Destination (AIR)')
        other_date = dict(fallback, purchase_date='2026-08-05')
        attachment = dict(fallback, _attachment_name='quote.pdf')
        result = finalize_physical_freight_offers({'items':corrupted+[fallback,other_date,attachment]},body,sent_date='2026-08-04')
        self.assertEqual(len(result['items']),4)
        self.assertIn({'label':'Shipment','value':'2'},result['items'][1]['extra_attributes'])
        self.assertIn(other_date,result['items'])
        self.assertIn(attachment,result['items'])
