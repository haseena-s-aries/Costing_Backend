from pathlib import Path
from unittest.mock import Mock
from decimal import Decimal
from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet, _normalize_item


class CatalogRateTests(SimpleTestCase):
    def extract(self, name, seed):
        path = Path('C:/Users/admin/Downloads') / name
        if not path.exists(): self.skipTest('Optional local source unavailable')
        o = ExtractionOrchestrator()
        o.gemini = Mock()
        o.gemini.extract_from_file.return_value = seed
        return o.extract(SimpleUploadedFile(name,path.read_bytes()),name)

    def test_serial_is_not_product_name(self):
        rows = [dict(product_name=str(i),unit_price=p,quantity=q,supplier_name='Seller',currency='USD',
                     purchase_date='2026-09-22',extra_attributes=[{'label':'Cable Type','value':n}])
                for i,p,q,n in [(1,'42','32','CJPF96/SC'),(2,'50.20','40','CBPJP86/SC (VFD)')]]
        result = self.extract('DE2316 CABLE OFFER - HONEST CABLE_20261013_052403.pdf',{'items':rows})
        self.assertEqual([i['product_name'] for i in result['items']],['CJPF96/SC','CBPJP86/SC (VFD)'])
        self.assertFalse(any(i['display_name'].startswith(('1 |','2 |')) for i in result['items']))

    def test_missing_positive_rate_zero_total_survives_upload_and_virtual_sheet(self):
        rows = [dict(product_name=n,unit_price=p,quantity='25',currency='EUR',supplier_name='SAMCON Prozessleittechnik GmbH',
                     purchase_date='2026-09-23') for n,p in [('ExCam IPP1488','4496'),('ExCam IPM1137','3596')]]
        result = self.extract('260923-AAN-108253-HH-Aries Marine and Engineering Services_26466_20261012_111956.pdf',{'items':rows})
        self.assertEqual(len(result['items']),3)
        recovered = result['items'][-1]
        self.assertIn('SKD02-T',recovered['product_name'])
        self.assertEqual(Decimal(recovered['unit_price']),Decimal('10'))
        self.assertEqual(Decimal(recovered['line_total']),0)
        self.assertEqual(Decimal(recovered['quantity']),0)
        sheet=gemini_result_to_virtual_sheet(result)
        self.assertEqual(len(sheet['rows']),4)
        last=dict(zip(sheet['rows'][0],sheet['rows'][-1]))
        self.assertEqual(Decimal(str(last['unit_price'])),10)
        self.assertEqual(Decimal(str(last['line_total'])),0)

    def test_positive_rate_not_free(self):
        row,_=_normalize_item({'product_name':'Generic component','unit_price':'10','line_total':'0','quantity':'0','currency':'EUR'}, 'EUR')
        self.assertIsNotNone(row)
