from copy import deepcopy
from django.test import SimpleTestCase
from api.services.row_metadata_policy import missing_row_metadata
from api.services.source_reconciliation import required_metadata


class RowMetadataTests(SimpleTestCase):
    def rows(self):
        return [dict(product_name='Item A', unit_price='10', purchase_date='2026-09-01', currency='USD'),
                dict(product_name='Item B', unit_price='20', document_date='2026-09-02', currency='AED')]

    def test_mixed_metadata_needs_no_document_default(self):
        rows = self.rows()
        self.assertEqual(missing_row_metadata(rows, {}), {'date': False, 'currency': False})
        result = required_metadata(dict(items=deepcopy(rows), supplier_name='Example Ltd'))
        self.assertNotIn('missing_document_date', result['_review_reasons'])
        self.assertNotIn('missing_currency', result['_review_reasons'])
        self.assertEqual(result['items'], rows)

    def test_one_missing_row_is_not_hidden_by_another_complete_row(self):
        rows = self.rows()
        rows[1].update(document_date=None, currency=None)
        self.assertEqual(missing_row_metadata(rows, {}), {'date': True, 'currency': True})
        self.assertEqual(missing_row_metadata(rows, dict(document_date='2026-09-03', currency='EUR')),
                         {'date': False, 'currency': False})

    def test_ignored_and_invalid_rows(self):
        rows = self.rows() + [dict(row_type='IGNORE')]
        self.assertFalse(any(missing_row_metadata(rows, {}).values()))
        rows[0]['purchase_date'] = 'not-a-date'
        self.assertTrue(missing_row_metadata(rows, {})['date'])
        self.assertTrue(all(missing_row_metadata([], {}).values()))
