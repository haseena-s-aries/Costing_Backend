from django.test import SimpleTestCase

from api.services.extraction.confidence_scorer import ConfidenceScorer
from api.services.gemini_extraction_service import normalize_result


class ExtractionContractCleanupTests(SimpleTestCase):
    def test_gemini_complete_result_is_not_penalized_for_inapplicable_mapping_scores(self):
        result = {
            "currency": "AED",
            "items": [{
                "row_type": "PRODUCT",
                "product_name": "6MM PLYWOOD BOX",
                "line_total": "315",
                "currency": "AED",
            }],
        }
        score = ConfidenceScorer().score(
            result, 0.0, 0.0, extraction_method="gemini"
        )
        self.assertEqual(score["level"], "high")
        self.assertEqual(score["final_confidence"], 1.0)

    def test_deprecated_fields_are_compatibility_nulls_and_description_is_preserved(self):
        result = normalize_result({
            "items": [{
                "row_type": "PRODUCT",
                "product_name": "CABLE TIE 160 X 2.5 BLACK GIFFEX",
                "brand": "GIFFEX",
                "model": "160 X 2.5",
                "part_number": "1695",
                "extra_attributes": [{"label": "Material", "value": "Nylon"}],
                "line_total": "10",
                "currency": "AED",
            }],
        })
        item = result["items"][0]
        self.assertEqual(item["product_name"], "CABLE TIE 160 X 2.5 BLACK GIFFEX")
        self.assertIsNone(item["brand"])
        self.assertIsNone(item["model"])
        self.assertIsNone(item["part_number"])
        self.assertEqual(item["extra_attributes"], [{"label": "Material", "value": "Nylon"}])

    def test_weak_email_prose_product_name_is_not_high_confidence(self):
        score = ConfidenceScorer().score({
            "currency": "USD",
            "items": [{"product_name": "Dear Mr Arun: | MOTOR", "line_total": "100", "currency": "USD"}],
        }, extraction_method="gemini")
        self.assertEqual(score["level"], "low")
        self.assertIn("weak_product_name:0", score["reasons"])

    def test_document_metadata_attributes_are_removed(self):
        result = normalize_result({
            "items": [{
                "row_type": "SERVICE", "product_name": "DEMOSTIC TRANSPORTATION",
                "line_total": "235", "currency": "USD",
                "extra_attributes": [
                    {"label": "VESSLE", "value": "FSU TURA"},
                    {"label": "PO", "value": "AMES-SHJ-50607"},
                    {"label": "Material", "value": "Steel"},
                ],
            }],
        })
        self.assertEqual(result["items"][0]["extra_attributes"], [{"label": "Material", "value": "Steel"}])

    def test_unlabelled_parenthesized_identifier_is_not_promoted_to_part_number(self):
        result = normalize_result({
            "items": [{
                "row_type": "PRODUCT",
                "product_name": "ISO13733 300B 4R Fairlead with seat (1249-2001)",
                "part_number": "1249-2001",
                "line_total": "22000",
                "currency": "USD",
                "extra_attributes": [],
            }],
        })
        item = result["items"][0]
        self.assertEqual(item["product_name"], "ISO13733 300B 4R Fairlead with seat (1249-2001)")
        self.assertEqual(item["extra_attributes"], [])

    def test_explicit_part_number_label_is_preserved(self):
        result = normalize_result({
            "items": [{
                "row_type": "PRODUCT",
                "product_name": "Fairlead",
                "part_number": "1249-2001",
                "extra_attributes": [{"label": "P/N", "value": "1249-2001"}],
                "line_total": "10",
                "currency": "USD",
            }],
        })
        self.assertIn({"label": "P/N", "value": "1249-2001"}, result["items"][0]["extra_attributes"])
