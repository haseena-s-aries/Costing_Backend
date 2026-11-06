from copy import deepcopy

from django.test import SimpleTestCase

import api.services.gemini_extraction_service as service


class EmailQuoteFinalInvariantTests(SimpleTestCase):
    def _result(self):
        return {
            "document_type": "email_quote",
            "category": "PRICE_LIST",
            "supplier_name": None,
            "currency": "JPY",
            "items": [
                {
                    "row_type": "PRODUCT",
                    "product_name": "VFD",
                    "display_name": "VFD | Warranty: 5years",
                    "supplier_name": "Danfoss",
                    "currency": "JPY",
                    "unit_price": "2778",
                    "quantity": "1",
                    "uom": "unit",
                    "line_total": "2778",
                    "extra_attributes": [{"label": "Warranty", "value": "5years"}],
                    "source_context": "From: Danfoss China\nSent: 10 Sep 2026\nVFD unit price ￥2778",
                },
                {
                    "row_type": "PRODUCT",
                    "product_name": "VFD",
                    "display_name": "VFD | Warranty: 2years",
                    "supplier_name": "Danfoss China",
                    "currency": "JPY",
                    "unit_price": "2500",
                    "quantity": None,
                    "uom": None,
                    "line_total": None,
                    "extra_attributes": [{"label": "Warranty", "value": "2years"}],
                    "source_context": "From: Danfoss China\nSent: 09 Sep 2026\nVFD unit price ￥2500",
                },
            ],
        }

    def test_virtual_sheet_reapplies_email_invariants(self):
        result = service.normalize_result(deepcopy(self._result()))
        result = service._finalize_email_quote_metadata(result)
        sheet = service.gemini_result_to_virtual_sheet(result)

        self.assertEqual(result["document_type"], "email_quote")
        self.assertEqual(result["category"], "QUOTATION")
        self.assertIsNone(result["currency"])
        self.assertTrue(result["_needs_review"])
        self.assertEqual(result["supplier_name"], "Danfoss China")
        self.assertEqual(len(sheet["rows"]) - 1, 2)
        self.assertEqual(result["items"][0]["source_document_type"], "email_quote")
        self.assertEqual(result["items"][0]["source_category"], "QUOTATION")
        self.assertIsNone(result["items"][0]["currency"])
        self.assertIsNone(result["items"][0]["line_total"])
        self.assertIsNone(result["items"][0]["quantity"])
        self.assertIsNone(result["items"][0]["uom"])
        self.assertIn("Warranty: 5years", result["items"][0]["display_name"])

    def test_bare_yen_is_not_a_currency(self):
        self.assertIsNone(service._valid_currency("¥"))
        self.assertIsNone(service._valid_currency("￥"))
        self.assertEqual(service._valid_currency("JP¥"), "JPY")
        self.assertEqual(service._valid_currency("RMB"), "CNY")
