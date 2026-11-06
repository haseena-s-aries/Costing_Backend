from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
import io
from django.test import SimpleTestCase
from api.services.visual_review_policy import require_visual_review
from api.services.source_reconciliation import required_metadata
from api.services.visual_review_policy import batch_visual_review_reasons
from api.services.import_service import ImportService
from api.exceptions import ImportServiceError


class VisualReviewPolicyTests(SimpleTestCase):
    def test_legacy_cache_cannot_bypass_source_check(self):
        batch = SimpleNamespace(extraction_summary={'gemini_raw_result': {'_visual_source_checked': True}},
                                uploaded_file=SimpleNamespace(original_file_name='unknown.pdf', file=Mock()))
        batch.uploaded_file.file.open.return_value = io.BytesIO(b'pdf')
        page = SimpleNamespace(width=600, height=800, chars=[], images=[dict(width=600, height=800)])
        with patch('pdfplumber.open') as opened:
            opened.return_value.__enter__.return_value.pages = [page]
            reasons = batch_visual_review_reasons(batch)
        self.assertIn('Scanned document', reasons[0])

    def test_final_import_boundary_blocks_before_any_database_write(self):
        with patch('api.services.visual_review_policy.batch_visual_review_reasons', return_value=['Scanned document']), \
             patch('api.services.import_service.DuplicateService') as duplicate:
            with self.assertRaises(ImportServiceError):
                ImportService().start(SimpleNamespace(), [])
            duplicate.assert_not_called()

    def test_explicit_review_confirmation_can_proceed(self):
        with patch('api.services.visual_review_policy.batch_visual_review_reasons') as check, \
             patch('api.services.import_service.DuplicateService', side_effect=RuntimeError('import reached')):
            with self.assertRaisesRegex(RuntimeError, 'import reached'):
                ImportService().start(SimpleNamespace(), [], review_confirmed=True)
            check.assert_not_called()

    def test_worker_preserves_visual_review_reasons(self):
        from api.management.commands.bulk_import_worker import Command
        self.assertTrue(Command._is_extraction_review_reason('Scanned document — verify extracted text'))
        self.assertTrue(Command._is_extraction_review_reason('Handwritten content — verify all fields'))

    def result(self):
        return dict(supplier_name='Example LLC', document_date='2026-09-30', currency='AED',
                    items=[dict(product_name='Uncertain handwritten words', unit_price='300'),
                           dict(product_name='Another row', unit_price=None, line_total='360')])

    def test_handwriting_requires_review_even_with_complete_metadata(self):
        result = self.result()
        before = deepcopy(result['items'])
        result['contains_handwriting'] = True
        result = required_metadata(result)
        self.assertTrue(result['_gemini_needs_review'])
        self.assertIn('handwritten_content_requires_review', result['_review_reasons'])
        self.assertEqual(result['items'], before)

    def test_scan_review_is_sticky_without_changing_rows(self):
        page = SimpleNamespace(width=600, height=800, chars=[], images=[dict(width=600, height=800)])
        with patch('pdfplumber.open') as opened:
            opened.return_value.__enter__.return_value.pages = [page]
            result = require_visual_review(self.result(), b'pdf')
        self.assertEqual(result['_visual_review_pages'], [1])
        result['_needs_review'] = result['_gemini_needs_review'] = False
        result = required_metadata(result)
        self.assertTrue(result['_gemini_needs_review'])
        self.assertEqual(result['items'], self.result()['items'])

    def test_small_logo_does_not_trigger_scan_review(self):
        page = SimpleNamespace(width=600, height=800, chars=[], images=[dict(width=30, height=30)])
        with patch('pdfplumber.open') as opened:
            opened.return_value.__enter__.return_value.pages = [page]
            result = require_visual_review(self.result(), b'pdf')
        self.assertNotIn('scanned_content_requires_review', result['_review_reasons'])
