from django.test import SimpleTestCase

from api.services.msg_attachment_recovery import recover_attachments


class AttachmentSupplierPrecedenceTests(SimpleTestCase):
    def test_attachment_supplier_replaces_body_supplier_for_same_quote_rows(self):
        # The parser is patched at the source boundary; this test exercises
        # only generic body-vs-attachment reconciliation.
        from unittest.mock import patch
        body = {
            'supplier_name': 'Email Sender LLC',
            'items': [{
                'product_name': 'Power Supply Cabinet', 'supplier_name': 'Email Sender LLC',
                'unit_price': '1120', 'line_total': '1120', 'quantity': '1',
            }],
        }
        attachment_row = {
            'product_name': 'Power Supply Cabinet', 'supplier_name': 'Actual Quotation Supplier Ltd',
            'unit_price': '1120.00', 'line_total': '1120.00', 'quantity': '1',
            'currency': 'CNY', 'purchase_date': '2026-09-29',
            'source_reference': 'quotation.pdf',
        }
        with patch('api.services.msg_attachment_recovery.parse_attachment', return_value=[attachment_row]):
            result = recover_attachments(body, [{'name': 'quotation.pdf', 'data': b'pdf'}])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['supplier_name'], 'Actual Quotation Supplier Ltd')
