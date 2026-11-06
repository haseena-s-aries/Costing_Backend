import io
import zipfile
from unittest.mock import Mock

from django.test import SimpleTestCase

from .extraction_orchestrator import ExtractionOrchestrator
from api.services.gemini_extraction_service import _image_too_small_for_document, _prepare_image_for_gemini
from api.services.gemini_extraction_service import normalize_result, _supplier_column_order, _normalize_supplier_name, _repair_continuation_supplier_alignment, _normalize_item, _normalize_extra_attributes, _valid_date
from api.services.source_reconciliation import required_metadata


class ArchiveWorkflowTests(SimpleTestCase):
    def test_two_digit_document_date_uses_2000s_without_iso_year_swap(self):
        self.assertEqual(_valid_date("18-06-26"), "2026-06-18")

    def test_reconciles_legacy_swapped_short_date_against_document_date(self):
        result = normalize_result({
            "document_type": "quotation",
            "category": "QUOTATION",
            "supplier_name": "Supplier",
            "document_date": "2026-06-18",
            "currency": "CNY",
            "items": [{
                "row_type": "PRODUCT", "product_name": "Item",
                "purchase_date": "2018-06-26", "unit_price": "1",
                "currency": "CNY", "supplier_name": "Supplier",
            }],
        })
        self.assertEqual(result["items"][0]["purchase_date"], "2026-06-18")

    def test_multilingual_source_labels_are_canonicalized_without_inventing_attributes(self):
        self.assertEqual(
            _normalize_extra_attributes([
                {"label": "型号", "value": "R15IAD3S-MRA3FP"},
                {"label": "品牌", "value": "NI"},
            ]),
            [
                {"label": "Model", "value": "R15IAD3S-MRA3FP"},
                {"label": "Brand", "value": "NI"},
            ],
        )

    def _archive_bytes(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("quotation.pdf", b"pdf")
            archive.writestr("quotation.png", b"png")
        return stream.getvalue()

    def test_root_items_are_the_only_canonical_main_list(self):
        first = {
            "document_type": "quotation",
            "supplier_name": "Supplier A",
            "currency": "USD",
            "items": [
                {"product_name": "Motor A", "unit_price": "100"},
                {"product_name": "Motor B", "unit_price": "200"},
                {"product_name": "Motor C", "unit_price": "300"},
            ],
        }
        second = {
            "document_type": "quotation",
            "supplier_name": "Supplier B",
            "currency": "AED",
            "items": [
                {"product_name": "Motor A", "unit_price": "110"},
                {"product_name": "Motor B", "unit_price": "210"},
                {"product_name": "Motor C", "unit_price": "310"},
            ],
        }

        orchestrator = ExtractionOrchestrator()
        orchestrator.extract = Mock(side_effect=[first, second])
        result = orchestrator._extract_archive(
            self._archive_bytes(), "bundle.zip", None
        )

        self.assertEqual(len(result["items"]), 6)
        self.assertEqual(len(result["_archive_documents"]), 2)
        self.assertTrue(all("items" not in doc for doc in result["_archive_documents"]))
        self.assertEqual(
            [len(doc["document_items"]) for doc in result["_archive_documents"]],
            [3, 3],
        )
        self.assertEqual(
            {item["source_reference"] for item in result["items"]},
            {
                "bundle.zip:quotation.pdf",
                "bundle.zip:quotation.png",
            },
        )

    def test_archive_rows_are_not_reappended_on_repeated_root_consumption(self):
        result = {
            "items": [{"product_name": "A"}, {"product_name": "B"}],
            "_archive_documents": [
                {"document_items": [{"product_name": "A"}, {"product_name": "B"}]}
            ],
        }
        # The canonical importer contract reads only root items.
        self.assertEqual(len(result["items"]), 2)
        self.assertNotIn("items", result["_archive_documents"][0])

    def test_small_readable_image_is_upscaled_not_rejected(self):
        from PIL import Image

        source = io.BytesIO()
        Image.new("RGB", (510, 279), "white").save(source, format="JPEG")
        original = source.getvalue()

        self.assertTrue(_image_too_small_for_document(original))
        prepared, mime = _prepare_image_for_gemini(original, "image/jpeg")
        self.assertEqual(mime, "image/png")
        self.assertGreater(len(prepared), 0)
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertGreaterEqual(image.width, 1000)
            self.assertGreater(image.height, 279)

    def test_tax_rows_move_to_document_metadata_not_import_items(self):
        result = normalize_result({
            "document_type": "quotation",
            "supplier_name": "ISIC",
            "currency": "GBP",
            "grand_total": "2431.60",
            "items": [
                {"row_type": "CHARGE", "product_name": "ITEM COST", "line_total": "1920.00", "currency": "GBP"},
                {"row_type": "CHARGE", "product_name": "SHIPPING", "line_total": "225.00", "currency": "GBP"},
                {"row_type": "CHARGE", "product_name": "13% TAX", "line_total": "249.60", "currency": "GBP"},
            ],
        })
        self.assertEqual([item["product_name"] for item in result["items"]], ["ITEM COST", "SHIPPING"])
        self.assertEqual(result["tax_metadata"][0]["amount"], "249.60")
        self.assertEqual(result["tax_metadata"][0]["rate"], "13")
        self.assertEqual(result["grand_total"], "2431.60")

    def test_supplier_comparison_keeps_blank_supplier_and_quoted_amount(self):
        result = normalize_result({
            "document_type": "other",
            "category": "OTHER",
            "currency": "RMB",
            "items": [
                {"row_type": "PRODUCT", "product_name": "Wrench Set", "quantity": "2", "unit_price": None, "line_total": "600", "currency": "RMB", "supplier_name": "Wuxi Aierda"},
                {"row_type": "PRODUCT", "product_name": "Wrench Set", "quantity": "2", "unit_price": None, "line_total": "635", "currency": "RMB", "supplier_name": "Jiangyin Baoyuan"},
            ],
        })
        self.assertEqual(result["document_type"], "price_list")
        self.assertEqual(result["category"], "PRICE_LIST")
        self.assertIsNone(result["supplier_name"])
        self.assertEqual([item["line_total"] for item in result["items"]], ["600", "635"])
        self.assertEqual([item["currency"] for item in result["items"]], ["CNY", "CNY"])

    def test_supplier_column_order_is_carried_forward_without_blank_shift(self):
        page_one = [
            {"supplier_name": "JINAN MOTI"},
            {"supplier_name": "Wuxi Aierda"},
            {"supplier_name": "Jiangyin Baoyuan"},
            {"supplier_name": "Jiangyin Haofan"},
        ]
        order = _supplier_column_order(page_one)
        page_two_cells = [None, "600", "635", "630"]
        mapped = {
            supplier: value
            for supplier, value in zip(order, page_two_cells)
            if value is not None
        }
        self.assertNotIn("JINAN MOTI", mapped)
        self.assertEqual(mapped["Wuxi Aierda"], "600")
        self.assertEqual(mapped["Jiangyin Baoyuan"], "635")
        self.assertEqual(mapped["Jiangyin Haofan"], "630")

    def test_multi_supplier_document_does_not_require_document_supplier(self):
        result = required_metadata({
            "supplier_name": None,
            "document_date": None,
            "items": [
                {"supplier_name": "Wuxi Aierda", "line_total": "600", "currency": "CNY"},
                {"supplier_name": "Jiangyin Baoyuan", "line_total": "635", "currency": "CNY"},
            ],
        })
        self.assertNotIn("missing_supplier_name", result["_review_reasons"])
        self.assertIn("missing_document_date", result["_review_reasons"])

    def test_missing_supplier_on_one_priced_row_still_requires_review(self):
        result = required_metadata({
            "supplier_name": None,
            "document_date": "2026-09-22",
            "items": [
                {"supplier_name": "Wuxi Aierda", "line_total": "600", "currency": "CNY"},
                {"supplier_name": None, "line_total": "635", "currency": "CNY"},
            ],
        })
        self.assertIn("missing_supplier_name", result["_review_reasons"])

    def test_bilingual_supplier_and_comparison_category_normalize_consistently(self):
        self.assertEqual(
            _normalize_supplier_name("济南摩缇 JINAN MOTI INDUSTRIAL CO.,LTD"),
            "JINAN MOTI INDUSTRIAL CO.,LTD",
        )
        result = normalize_result({
            "document_type": "quotation",
            "category": "QUOTATION",
            "items": [
                {"product_name": "Wrench Set", "line_total": "600", "supplier_name": "Wuxi Aierda", "parent_category": "Tools", "source_category": "Tools"},
                {"product_name": "Wrench Set", "line_total": "635", "supplier_name": "Jiangyin Baoyuan", "parent_category": "Commercial", "source_category": "Commercial"},
            ],
        })
        self.assertEqual(result["document_type"], "price_list")
        self.assertEqual(result["category"], "PRICE_LIST")
        self.assertIsNone(result["supplier_name"])
        self.assertTrue(all(item["parent_category"] is None for item in result["items"]))
        self.assertTrue(all(item["source_category"] == "PRICE_LIST" for item in result["items"]))

    def test_wrench_prefix_shifts_only_that_row_group_not_megger(self):
        suppliers = ["JINAN MOTI", "Wuxi Aierda", "Jiangyin Baoyuan", "Jiangyin Haofan"]
        rows = [
            {"product_name": "Wrench Set", "supplier_name": "JINAN MOTI"},
            {"product_name": "Wrench Set", "supplier_name": "Wuxi Aierda"},
            {"product_name": "Wrench Set", "supplier_name": "Jiangyin Baoyuan"},
            {"product_name": "Megger 10kV", "supplier_name": "Wuxi Aierda"},
            {"product_name": "Megger 10kV", "supplier_name": "Jiangyin Baoyuan"},
            {"product_name": "Megger 10kV", "supplier_name": "Jiangyin Haofan"},
        ]
        _repair_continuation_supplier_alignment(rows, suppliers, 2)
        self.assertEqual(
            [row["supplier_name"] for row in rows[:3]],
            ["Wuxi Aierda", "Jiangyin Baoyuan", "Jiangyin Haofan"],
        )
        self.assertEqual(
            [row["supplier_name"] for row in rows[3:]],
            ["Wuxi Aierda", "Jiangyin Baoyuan", "Jiangyin Haofan"],
        )

    def test_informal_chat_price_without_product_name_is_retained_for_review(self):
        mapped, warning = _normalize_item({
            "row_type": "CHARGE",
            "product_name": None,
            "unit_price": "1325",
            "line_total": None,
            "uom": "PC",
            "currency": "AED",
            "extra_attributes": [{"label": "Availability", "value": "Available in ex-stock"}],
        }, "AED")
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped["unit_price"], "1325")
        self.assertIsNone(mapped["line_total"])
        self.assertEqual(mapped["unit_of_measurement"], "PC")
        self.assertIn("missing product_name", warning)
