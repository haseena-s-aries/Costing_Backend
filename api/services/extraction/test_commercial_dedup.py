from copy import deepcopy
import json
from pathlib import Path
from django.test import SimpleTestCase

from api.services.commercial_row_identity import is_true_duplicate
from api.services.gemini_extraction_service import (
    _deduplicate_equivalent_commercial_items,
    _reconcile_duplicate_product_prices,
    _reconcile_duplicate_short_product_names,
    _reconcile_placeholder_commercial_items,
    _collapse_parallel_currency_rows,
)


class CommercialDedupTests(SimpleTestCase):
    def row(self, **changes):
        return dict({
            "row_type": "PRODUCT", "product_name": "Pump motor (IP55)",
            "supplier_name": "Example Motors", "purchase_date": "2026-06-05",
            "quantity": "1", "uom": "PC", "unit_price": "378.00",
            "line_total": "378.00", "currency": "USD", "page_number": 1,
            "source_row": 2, "extra_attributes": [],
        }, **changes)

    def test_same_original_row_repeat_removed_once_and_idempotent(self):
        a = self.row()
        b = self.row(unit_price="378", _keep_duplicate_priced_row=True)
        result = _deduplicate_equivalent_commercial_items({"items": [a, b]})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["_deduplicated_item_count"], 1)
        self.assertEqual(result["_normalization_actions"], ["duplicate_commercial_row_removed"])
        _deduplicate_equivalent_commercial_items(result)
        self.assertEqual(result["_deduplicated_item_count"], 1)

    def test_distinguishing_fields_preserved(self):
        changes = {
            "product_name": "Purifier motor (IP55)", "purchase_date": "2026-08-20",
            "supplier_name": "Other Motors", "unit_price": "379", "line_total": "756",
            "quantity": "2", "uom": "SET", "currency": "AED", "page_number": 2,
            "source_row": 3, "source_block_id": "next-email", "source_position": 100,
            "extra_attributes": [{"label": "Duration", "value": "8 HR"}],
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                rows = [self.row(), self.row(**{field: value})]
                self.assertFalse(is_true_duplicate(*rows))
                self.assertEqual(len(_deduplicate_equivalent_commercial_items({"items": rows})["items"]), 2)

    def test_parentheses_and_non_latin_identity_not_stripped(self):
        self.assertFalse(is_true_duplicate(self.row(), self.row(product_name="Pump motor (IP44)")))
        self.assertFalse(is_true_duplicate(self.row(product_name="电机"), self.row(product_name="泵")))

    def test_five_motors_including_two_equal_prices_and_two_freight_quotes(self):
        rows = [self.row(product_name=name, unit_price=price, line_total=price)
                for name, price in (("LO Purifier Motor", "229"),
                                    ("H.F.O Transfer Pump Motor", "378"),
                                    ("D.O Transfer Pump Motor", "166"),
                                    ("HFO Purifier Motor", "378"),
                                    ("Sludge Pump Motor", "230"))]
        rows += [self.row(row_type="SERVICE", product_name="Sea freight", unit_price="260", line_total="260"),
                 self.row(row_type="SERVICE", product_name="Air freight", unit_price="1000", line_total="1000")]
        result = _deduplicate_equivalent_commercial_items({"items": rows})
        self.assertEqual(len(result["items"]), 7)
        self.assertEqual(sum(row["row_type"] == "PRODUCT" for row in result["items"]), 5)

    def test_unknown_source_and_page_only_are_not_proof(self):
        a = self.row(source_row=None)
        self.assertFalse(is_true_duplicate(a, deepcopy(a)))
        a["page_number"] = None
        self.assertFalse(is_true_duplicate(a, deepcopy(a)))

    def test_same_price_in_two_email_blocks_remains(self):
        a = self.row(page_number=None, source_block_id="email-1")
        b = self.row(page_number=None, source_block_id="email-2")
        self.assertFalse(is_true_duplicate(a, b))

    def test_cross_unit_price_total_intersection_is_not_duplicate(self):
        self.assertFalse(is_true_duplicate(self.row(), self.row(unit_price="189", quantity="2")))

    def test_older_tariff_same_price_is_preserved(self):
        rows = [self.row(row_type="SERVICE", product_name="updated tariff for lifting"),
                self.row(row_type="SERVICE", product_name="lifting tariff", purchase_date="2026-08-20")]
        self.assertEqual(len(_deduplicate_equivalent_commercial_items({"items": rows})["items"]), 2)

    def test_different_price_mismatch_is_left_for_validation(self):
        entries = [{"mapped": self.row()}, {"mapped": self.row(unit_price="500")}]
        _reconcile_duplicate_product_prices(entries)
        self.assertTrue(all(entry["mapped"] for entry in entries))

    def test_code_only_history_not_removed(self):
        entries = [{"mapped": self.row(product_name="AX1", item_code="AX1")},
                   {"mapped": self.row(product_name="Long technical description for a different dated motor", item_code="AX1", purchase_date="2026-08-20")}]
        _reconcile_duplicate_short_product_names(entries)
        self.assertTrue(all(entry["mapped"] for entry in entries))

    def test_parallel_currencies_preserved(self):
        rows = [self.row(), self.row(currency="CNY")]
        self.assertEqual(len(_collapse_parallel_currency_rows({"items": rows})["items"]), 2)

    def test_placeholder_price_alone_never_removes_row(self):
        rows = [self.row(product_name="For reference", source_row=4), self.row()]
        self.assertEqual(len(_reconcile_placeholder_commercial_items({"items": rows})["items"]), 2)

    def test_placeholder_same_original_row_is_bound(self):
        rows = [self.row(product_name="For reference"), self.row()]
        result = _reconcile_placeholder_commercial_items({"items": rows})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["product_name"], "Pump motor (IP55)")

    def test_existing_golden_items_are_never_lost_by_dedup(self):
        root = Path(__file__).resolve().parents[3] / "test_fixtures" / "expected" / "extraction"
        fixtures = list(root.glob("*.json"))
        self.assertTrue(fixtures)
        for path in fixtures:
            with self.subTest(fixture=path.name):
                expected = json.loads(path.read_text(encoding="utf-8"))
                result = _deduplicate_equivalent_commercial_items(deepcopy(expected))
                self.assertEqual(result["items"], expected["items"])
