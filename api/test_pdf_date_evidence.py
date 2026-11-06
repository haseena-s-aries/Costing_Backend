from pathlib import Path
from unittest.mock import Mock
from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from api.services.pdf_date_evidence import reject_reference_dates_in_text
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet


class ReferenceDateTests(SimpleTestCase):
    def test_reference_number_is_not_date(self):
        result = {'document_date':'2025-03-17','document_number':'ABC-20250317',
                  'items':[{'purchase_date':'2025-03-17'}]}
        result = reject_reference_dates_in_text(result,'Ref. No.: ABC-20250317')
        self.assertIsNone(result['document_date'])
        self.assertIsNone(result['items'][0]['purchase_date'])
        self.assertEqual(result['document_number'],'ABC-20250317')
        self.assertTrue(result['_needs_review'])

    def test_explicit_matching_date_is_kept(self):
        result = {'document_date':'2025-03-17'}
        result = reject_reference_dates_in_text(result,'Ref. No.: ABC-20250317\nDate: 2025-03-17')
        self.assertEqual(result['document_date'],'2025-03-17')
        self.assertNotIn('_rejected_reference_dates',result)

    def test_actual_pdf_upload_rejects_model_reference_date(self):
        path=Path('C:/Users/admin/Downloads/QUOTATION MCU BOM-_20261012_064040.pdf')
        if not path.exists(): self.skipTest('Optional local fixture unavailable')
        o=ExtractionOrchestrator()
        o.gemini=Mock()
        o.gemini.extract_from_file.return_value={'document_date':'2026-09-24','document_number':'MR-20260924',
            'supplier_name':'Mingri Electric Co.,Ltd.','currency':'CNY','items':[
                {'product_name':'Controller','unit_price':'1690','currency':'CNY','purchase_date':'2026-09-24'}]}
        result=o.extract(SimpleUploadedFile(path.name,path.read_bytes()),path.name)
        self.assertIsNone(result['document_date'])
        self.assertTrue(all(not i.get('purchase_date') for i in result['items']))
        self.assertTrue(result['_needs_review'])
        self.assertIn('missing_document_date',result['_review_reasons'])
        sheet=gemini_result_to_virtual_sheet(result)
        for row in sheet['rows'][1:]:
            mapped=dict(zip(sheet['rows'][0],row))
            self.assertFalse(mapped.get('purchase_date'))
