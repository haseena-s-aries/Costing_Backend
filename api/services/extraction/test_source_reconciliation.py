from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock

import extract_msg
from django.test import SimpleTestCase
from api.services.source_reconciliation import finalize_pdf, finalize_msg_table, merge_recovery, required_metadata
from api.services.gemini_extraction_service import (
    GeminiExtractionService,
    _extract_company_from_email_body,
    _is_reliable_msg_supplier_candidate,
    _resolve_msg_supplier,
    _supplier_from_sender,
    gemini_result_to_virtual_sheet,
    normalize_result,
)
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator

ROOT = Path(__file__).resolve().parents[3] / "test_fixtures"


class SourceReconciliationTests(SimpleTestCase):
    def test_msg_supplier_agency_and_disclaimer_filtering(self):
        body = """
        Disclaimer: Orient Cargo Agency limited liability insurance terms apply.
        Regards,
        Orient Cargo Agency
        """
        self.assertEqual(_extract_company_from_email_body(body), "Orient Cargo Agency")
        self.assertFalse(_is_reliable_msg_supplier_candidate("This company has limited liability"))

    def test_msg_sender_display_name_company_is_used_without_domain_invention(self):
        self.assertEqual(_supplier_from_sender("Orient Cargo Agency"), "Orient Cargo Agency")
        self.assertIsNone(_supplier_from_sender("sender@example.com"))

    def test_msg_supplier_resolver_propagates_verified_company(self):
        result = {"supplier_name": "insurance liability disclaimer", "items": [{"supplier_name": "insurance liability disclaimer"}]}
        resolved = _resolve_msg_supplier(result, {"signature_company": "insurance liability disclaimer", "sender_company": "Orient Cargo Agency"})
        self.assertEqual(resolved["supplier_name"], "Orient Cargo Agency")
        self.assertEqual(resolved["items"][0]["supplier_name"], "Orient Cargo Agency")

    def test_msg_supplier_resolver_does_not_inherit_unrelated_supplier(self):
        result = {"supplier_name": None, "items": [{"supplier_name": None}]}
        resolved = _resolve_msg_supplier(result, {"signature_company": "Unrelated Earlier Supplier LLC", "sender_company": None})
        self.assertIsNone(resolved["supplier_name"])

    def test_pdf_production_postprocessing_with_saved_model_response(self):
        for prefix, count in (("Afnan", 4), ("Al falaq", 3), ("Al fuaad", 3)):
            for saved in (ROOT / "results/extraction").glob(prefix + "*.actual.json"):
                with self.subTest(file=saved.name):
                    name = saved.name.replace(".actual.json", ".pdf")
                    service = object.__new__(GeminiExtractionService)
                    service._extract_large_pdf_progressively = Mock(return_value=json.loads(saved.read_text(encoding="utf-8")))
                    service._verify_visible_document_date = Mock(return_value=None)
                    service._verify_visible_document_category = Mock(return_value=None)
                    service._generate_content_with_retry = Mock(side_effect=AssertionError("No live API calls allowed"))
                    result = service.extract_from_file(BytesIO((ROOT / "inputs" / name).read_bytes()), name)
                    self.assertEqual(len(result["items"]), count)
                    self.assertEqual(len(gemini_result_to_virtual_sheet(deepcopy(result))["rows"])-1, count)

    def test_six_saved_pdf_results_and_virtual_sheet(self):
        expected = {"Afnan": 4, "Al falaq": 3, "Al fuaad": 3}
        checked = 0
        for path in (ROOT / "results/extraction").glob("*.actual.json"):
            prefix = next((k for k in expected if path.name.startswith(k)), None)
            if not prefix:
                continue
            with self.subTest(file=path.name):
                pdf = (ROOT / "inputs" / path.name.replace(".actual.json", ".pdf")).read_bytes()
                result = finalize_pdf(json.loads(path.read_text(encoding="utf-8")), pdf)
                self.assertEqual(len(result["items"]), expected[prefix])
                self.assertEqual(finalize_pdf(deepcopy(result), pdf)["items"], result["items"])
                self.assertTrue(result["supplier_name"])
                self.assertEqual(result["document_date"], "2026-04-28")
                self.assertIsNone(result["currency"])
                self.assertTrue(result["grand_total"])
                for row in result["items"]:
                    if row.get("_amount_semantics") == "rate" and row.get("uom") == "HR":
                        self.assertIsNone(row["line_total"])
                        self.assertIsNone(row["quantity"])
                    if row.get("_amount_semantics") == "total":
                        if row.get("_unit_price_from_source_total"):
                            self.assertEqual(row["unit_price"], row["line_total"])
                        else:
                            self.assertIsNone(row["unit_price"])
                        self.assertIsNone(row["quantity"])
                sheet = gemini_result_to_virtual_sheet(deepcopy(result))
                self.assertEqual(len(sheet["rows"]) - 1, expected[prefix])
                checked += 1
        self.assertEqual(checked, 6)

    def test_gulf_table_preserves_cells_and_requires_currency(self):
        path = next((ROOT / "inputs").glob("RE_ DE2417*.msg"))
        with extract_msg.Message(str(path)) as msg:
            actual = json.loads((ROOT / "results/extraction" / (path.stem + ".actual.json")).read_text(encoding="utf-8"))
            result = finalize_msg_table(actual, msg.body, str(msg.date))
        self.assertEqual(len(result["items"]), 8)
        self.assertEqual(result["items"][0]["product_name"], "4sqmm terminal block single")
        self.assertEqual(result["items"][1]["product_name"], "4sqmm terminal block double")
        self.assertEqual([i["quantity"] for i in result["items"][-4:]], ["3", "3", "2", "2"])
        self.assertTrue(result["_needs_review"])
        self.assertIn("missing_currency", result["_review_reasons"])
        self.assertTrue(all(i["currency"] is None for i in result["items"]))
        self.assertEqual(len(gemini_result_to_virtual_sheet(result)["rows"])-1, 8)

    def test_distinct_rows_stay_distinct(self):
        a = dict(product_name="Equipment rental", purchase_date="2026-01-01", currency="USD", quantity="2", unit_price="10", line_total="20", page_number=1)
        b = dict(a, source_row_id="pdf-text-line:8", _source_fact_verified=True)
        for changes in ({"quantity": "3"}, {"purchase_date": "2026-01-02"}, {"unit_price": "11"}, {"line_total": "30"}, {"page_number": 2}, {"product_name": "Other equipment"}, {"source_row_id": "pdf-text-line:9"}):
            self.assertEqual(len(merge_recovery([dict(a, **changes)], [b])), 2)

    def test_supplier_and_rate_quantity_are_distinguishing(self):
        row = dict(product_name="Rental", supplier_name="Seller A", quantity="2", unit_price="10", currency="USD", page_number=1)
        fact = dict(row, source_row_id="pdf-text-line:2", _amount_semantics="rate")
        self.assertEqual(len(merge_recovery([dict(row, supplier_name="Seller B")], [fact])), 2)
        self.assertEqual(len(merge_recovery([dict(row, quantity="3")], [fact])), 2)

    def test_rate_copy_and_summary_total_merge_across_pass_block_ids(self):
        existing = dict(product_name="Launch hire", supplier_name="Seller", purchase_date="2026-04-28",
                        quantity=None, uom="hr", unit_price="160", line_total="10430", currency="USD",
                        page_number=2, source_block_id="gemini:page:2")
        recovered = dict(product_name="Launch hire", supplier_name="Seller", purchase_date="2026-04-28",
                         quantity=None, uom=None, unit_price=None, line_total="10430", currency="AED",
                         page_number=2, source_block_id="pdf:summary:2", _amount_semantics="total",
                         source_row_id="pdf-text-line:39", _source_fact_verified=True)
        merged = merge_recovery([existing], [recovered])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["currency"], "AED")

    def test_tura_saved_result_is_unchanged_by_pdf_recovery(self):
        path = ROOT / "inputs/TURA 26E23 shipment _20260831_125224.pdf"
        actual = json.loads((ROOT / "results/selected_4_check" / (path.stem + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(finalize_pdf(deepcopy(actual), path.read_bytes()), actual)

    def test_transport_capacity_tier_is_not_transaction_quantity(self):
        path = ROOT / "results/extraction/Arrow rate_20260815_042202.actual.json"
        result = normalize_result(json.loads(path.read_text(encoding="utf-8")))
        self.assertIsNone(result["items"][0]["quantity"])
        self.assertIsNone(result["items"][0]["uom"])
        self.assertEqual(result["items"][0]["unit_price"], "150")
        self.assertEqual(result["items"][0]["line_total"], "150")
        self.assertEqual(result["items"][0]["extra_attributes"][0]["label"], "Capacity")
        self.assertIn("missing_document_date", result.get("_review_reasons", []))

    def test_fixed_shipment_quote_does_not_use_chargeable_weight_as_quantity(self):
        result = normalize_result({"items": [{
            "row_type": "SERVICE", "product_name": "Shipment from UK to USA",
            "quantity": "5", "uom": "kg", "unit_price": None,
            "line_total": "1200", "currency": "AED",
            "extra_attributes": [{"label": "Chargeable weight", "value": "5 kg"}],
        }]})
        item = result["items"][0]
        self.assertIsNone(item["quantity"])
        self.assertIsNone(item["uom"])
        self.assertIsNone(item["unit_price"])
        self.assertEqual(item["line_total"], "1200")

    def test_fixed_freight_charge_does_not_use_unit_quantity(self):
        result = normalize_result({"items": [{
            "row_type": "CHARGE", "product_name": "Address correction fee",
            "quantity": "1", "uom": "charge", "unit_price": "50",
            "line_total": "50", "currency": "AED",
        }]})
        item = result["items"][0]
        self.assertIsNone(item["quantity"])
        self.assertIsNone(item["uom"])
        self.assertEqual(item["unit_price"], "50")
        self.assertIsNone(item["line_total"])

    def test_recovery_flag_does_not_protect_alternate_pass(self):
        a = dict(product_name="Equipment rental", purchase_date="2026-01-01", currency="USD", quantity=None, unit_price=None, line_total="20", page_number=1)
        b = dict(a, source_row_id="pdf-text-line:8", _source_fact_verified=True, _amount_semantics="total", _keep_duplicate_priced_row=True)
        self.assertEqual(len(merge_recovery([a], [b])), 1)

    def test_pdf_recovery_enriches_four_existing_commercial_rows_without_duplicates(self):
        existing = [
            dict(row_type="PRODUCT", product_name="ISO13733 300B 4R Fairlead with seat (1249-2001)",
                 quantity=None, uom="pc", unit_price="5500", line_total=None, currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 extra_attributes=[]),
            dict(row_type="PRODUCT", product_name="ISO13733 300B 4R Fairlead with seat (1249-2002)",
                 quantity=None, uom="pc", unit_price="5000", line_total=None, currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 extra_attributes=[]),
            dict(row_type="PRODUCT", product_name="ISO13729 B400x250x428 Bulwark chock",
                 quantity=None, uom="pc", unit_price="520", line_total=None, currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 extra_attributes=[{"label": "Material", "value": "ZG230-450"}]),
            dict(row_type="CHARGE", product_name="DNV certificate", quantity=None, uom=None,
                 unit_price="3700", line_total=None, currency="USD", supplier_name="Seller",
                 purchase_date="2026-09-05", page_number=1, extra_attributes=[]),
        ]
        recovered = [
            dict(row_type="SERVICE", product_name="ISO13733 300B 4R Fairlead with seat",
                 quantity="4", uom="pc", unit_price="5500", line_total="22000", currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 source_row_id="pdf-text-line:1", _source_fact_verified=True),
            dict(row_type="SERVICE", product_name="ISO13733 300B 4R Fairlead with seat",
                 quantity=None, uom="pc", unit_price="5000", line_total=None, currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 source_row_id="pdf-text-line:2", _source_fact_verified=True),
            dict(row_type="SERVICE", product_name="ISO13729 B400x250x428 Bulwark chock",
                 quantity="2", uom="pc", unit_price="520", line_total="1040", currency="USD",
                 supplier_name="Seller", purchase_date="2026-09-05", page_number=1,
                 source_row_id="pdf-text-line:3", _source_fact_verified=True),
            dict(row_type="SERVICE", product_name="DNV certificate", quantity=None, uom=None,
                 unit_price="3700", line_total=None, currency="USD", supplier_name="Seller",
                 purchase_date="2026-09-05", page_number=1, source_row_id="pdf-text-line:4",
                 _source_fact_verified=True),
        ]
        merged = merge_recovery(existing, recovered)
        self.assertEqual(len(merged), 4)
        self.assertEqual([row["row_type"] for row in merged], ["PRODUCT", "PRODUCT", "PRODUCT", "CHARGE"])
        self.assertEqual(merged[0]["quantity"], "4")
        self.assertEqual(merged[0]["line_total"], "22000")
        self.assertEqual(merged[2]["line_total"], "1040")
        self.assertEqual(merged[2]["extra_attributes"], [{"label": "Material", "value": "ZG230-450"}])

    def test_verified_pdf_source_does_not_infer_part_number_from_parentheses(self):
        from api.services.source_reconciliation import _clean_verified_pdf_row
        row = {
            "_source_fact_verified": True,
            "product_name": "ISO13733 300B 4R Fairlead with seat",
            "source_context": "1、ISO13733 300B 4R Fairlead with seat(1249-2001) USD5500/pcx4pcs=22000",
            "extra_attributes": [
                {"label": "Part Number", "value": "1249-2001"},
                {"label": "Material of frame", "value": "AH36"},
            ],
        }
        _clean_verified_pdf_row(row)
        self.assertEqual(row["product_name"], "ISO13733 300B 4R Fairlead with seat(1249-2001)")
        self.assertNotIn({"label": "Part Number", "value": "1249-2001"}, row["extra_attributes"])
        self.assertIn({"label": "Material of frame", "value": "AH36"}, row["extra_attributes"])

    def test_verified_pdf_source_keeps_explicit_part_number_label(self):
        from api.services.source_reconciliation import _clean_verified_pdf_row
        row = {
            "_source_fact_verified": True,
            "product_name": "Fairlead",
            "source_context": "Fairlead P/N: 1249-2001 USD5500/pc",
            "extra_attributes": [{"label": "Part Number", "value": "1249-2001"}],
        }
        _clean_verified_pdf_row(row)
        self.assertIn({"label": "Part Number", "value": "1249-2001"}, row["extra_attributes"])

    def test_metadata_and_mixed_currency(self):
        result = dict(supplier_name="Seller", document_date="2026-01-01", currency=None,
                      items=[{"unit_price": "10", "currency": "USD"}, {"line_total": "20", "currency": "AED"}])
        self.assertNotIn("missing_currency", required_metadata(deepcopy(result))["_review_reasons"])
        for field, reason in (("supplier_name", "missing_supplier_name"), ("document_date", "missing_document_date")):
            bad = dict(result, **{field: None})
            self.assertTrue(required_metadata(bad)["_needs_review"])
            self.assertIn(reason, bad["_review_reasons"])

    def test_orchestrator_high_confidence_cannot_clear_hard_review(self):
        service = ExtractionOrchestrator()
        service.preprocessor.prepare = Mock(return_value={"file_type": "other"})
        service.gemini = Mock()
        service.gemini.extract_from_file.return_value = dict(items=[dict(product_name="Rental", unit_price="10", currency="USD")])
        result = service.extract(Mock(), "sample.msg")
        self.assertTrue(result["_needs_review"])
        self.assertIn("missing_supplier_name", result["_review_reasons"])
