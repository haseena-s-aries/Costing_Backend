from django.test import SimpleTestCase
from api.services.commercial_attribute_policy import clean_commercial_display
from api.services.gemini_extraction_service import (
    _normalize_item, _finalize_pdf_extraction_result, gemini_result_to_virtual_sheet,
)
from api.services.extraction.test_pdf_commercial_geometry import pdf_bytes, text


class QuotedPermitChargeTests(SimpleTestCase):
    def test_quoted_fee_is_not_a_support_document(self):
        for name in ['Gate Pass', 'Permit', 'Security pass']:
            row, _ = _normalize_item(dict(product_name=name, unit_price='82.25',
                                         source_document_type='email_quote', row_type='CHARGE'), 'EUR')
            self.assertIsNotNone(row)
            self.assertEqual(row['unit_price'], '82.25')
        row, warning = _normalize_item(dict(product_name='Daily Gate Pass', unit_price='20',
                                           source_document_type='gate_pass'), 'EUR')
        self.assertIsNone(row)
        self.assertEqual(warning, 'permit_non_commercial_row_skipped')

    def test_all_four_explicit_prices_survive_virtual_sheet(self):
        lines = ['From: Seller <sales@example.com>', 'Sent: Thursday, 1 October 2026 5:36 PM',
                 'To: Buyer LLC', 'Our quoted charges:', '- Clearance - EUR 115/-',
                 '- Documentation: EUR 260/-', '- Gate Pass: EUR 82.25/-',
                 '- Transportation: EUR 690/-', 'Custom Duty 5% of invoice value',
                 'Best Regards,', 'Example Shipping LLC']
        blob = pdf_bytes([[text(25, 30+i*20, line) for i, line in enumerate(lines)]])
        result = _finalize_pdf_extraction_result({'items': []}, blob)
        sheet = gemini_result_to_virtual_sheet(result)
        rows = [dict(zip(sheet['rows'][0], row)) for row in sheet['rows'][1:]]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r['unit_price'] for r in rows], ['115', '260', '82.25', '690'])
        self.assertEqual(rows[2]['product_name'], 'Gate Pass')

    def test_only_trailing_separator_is_removed(self):
        for name in ['BOE -', 'BOE –', 'BOE —']:
            self.assertEqual(clean_commercial_display({'product_name': name})['product_name'], 'BOE')
        self.assertEqual(clean_commercial_display({'product_name': 'AB-123 | Size: 4-6'})['product_name'],
                         'AB-123 | Size: 4-6')
