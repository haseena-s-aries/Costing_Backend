from django.test import SimpleTestCase

from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet, normalize_result


class CommercialOfferTableFinalizationTests(SimpleTestCase):
    def test_parent_name_and_specification_are_kept_in_hierarchy_order(self):
        result = normalize_result({
            "document_type": "offer",
            "category": "QUOTATION",
            "items": [{
                "row_type": "PRODUCT",
                "parent_category": "Comm Spare List",
                "product_name": "Pump Kits",
                "unit_price": "10",
                "currency": "USD",
                "extra_attributes": [{
                    "label": "Specification",
                    "value": "Dosing Pump Gasket and O Ring",
                }],
            }],
        })
        item = result["items"][0]
        self.assertEqual(item["parent_category"], "Comm Spare List")
        self.assertEqual(item["product_name"], "Pump Kits")
        self.assertIn({
            "label": "Specification",
            "value": "Dosing Pump Gasket and O Ring",
        }, item["extra_attributes"])
        self.assertEqual(
            item["display_name"],
            "Comm Spare List | Pump Kits | Specification: Dosing Pump Gasket and O Ring",
        )

    def test_canonical_display_name_handles_all_hierarchy_shapes(self):
        result = normalize_result({
            "document_type": "offer",
            "category": "QUOTATION",
            "items": [
                {"row_type": "PRODUCT", "parent_category": "Chemical Injection Package", "product_name": "DEMULSIFIER INJECTION SKID", "unit_price": "1", "currency": "USD"},
                {"row_type": "PRODUCT", "parent_category": "Comm Spare List", "product_name": "Pump Kits", "unit_price": "1", "currency": "USD", "extra_attributes": [{"label": "Specification", "value": "Dosing Pump Gasket and O Ring"}]},
                {"row_type": "PRODUCT", "product_name": "Process Ball Valve", "unit_price": "1", "currency": "USD", "extra_attributes": [{"label": "Specification", "value": "316SS, 600#"}]},
                {"row_type": "PRODUCT", "product_name": "Filter", "unit_price": "1", "currency": "USD"},
            ],
        })
        self.assertEqual(result["items"][0]["display_name"], "Chemical Injection Package | DEMULSIFIER INJECTION SKID")
        self.assertEqual(result["items"][1]["display_name"], "Comm Spare List | Pump Kits | Specification: Dosing Pump Gasket and O Ring")
        self.assertEqual(result["items"][2]["display_name"], "Process Ball Valve | Specification: 316SS, 600#")
        self.assertEqual(result["items"][3]["display_name"], "Filter")

    def test_source_verified_commercial_rows_use_semantic_types_and_clean_display(self):
        result = normalize_result({
            "document_type": "offer",
            "category": "QUOTATION",
            "items": [
                {"row_type": "PRODUCT", "product_name": "VFD", "unit_price": "100", "currency": "CNY", "_table_source_verified": True, "extra_attributes": [{"label": "Remarks", "value": "External Cost"}]},
                {"row_type": "CHARGE", "product_name": "Internal manhour cost 4 Hours", "unit_price": "10", "currency": "CNY", "_table_source_verified": True, "extra_attributes": [{"label": "Remarks", "value": "Internal Cost"}]},
                {"row_type": "PRODUCT", "product_name": "5% profit", "unit_price": "5,358.81", "currency": "CNY", "_table_source_verified": True, "extra_attributes": [{"label": "Remarks", "value": "Profit"}]},
                {"row_type": "CHARGE", "product_name": "Physical equipment with DNV certificate", "quantity": "2", "uom": "PC", "unit_price": "810.00", "line_total": "1587.60", "currency": "CNY", "_table_source_verified": True, "extra_attributes": [{"label": "DISC.%", "value": "2.00"}]},
            ],
        })
        items = result["items"]
        self.assertEqual([item["row_type"] for item in items], ["PRODUCT", "SERVICE", "CHARGE", "PRODUCT"])
        self.assertEqual([item["display_name"] for item in items], ["VFD", "Internal manhour cost 4 Hours", "5% profit", "Physical equipment with DNV certificate"])
        self.assertEqual(items[3]["unit_price"], "810.00")
        self.assertEqual(items[3]["line_total"], "1587.60")
        self.assertIn({"label": "DISC.%", "value": "2.00"}, items[3]["extra_attributes"])
        self.assertTrue(all("Total Price" not in item["display_name"] for item in items))

    def test_separate_source_columns_survive_final_normalization_and_sheet(self):
        result = normalize_result({
            "document_type": "offer",
            "category": "PRICE_LIST",
            "supplier_name": "Vendor Company Ltd.",
            "items": [{
                "row_type": "PRODUCT",
                "product_name": "Dosing Pump",
                "supplier_name": "Vendor Company Ltd.",
                "unit_price": "10",
                "line_total": "10",
                "currency": "USD",
                "extra_attributes": [
                    {"label": "Specification", "value": "Gasket and O Ring"},
                    {"label": "Type / Location", "value": "Chemical Injection Package"},
                ],
            }],
        })
        self.assertEqual(result["document_type"], "offer")
        self.assertEqual(result["category"], "QUOTATION")
        self.assertEqual(result["supplier_name"], "Vendor Company Ltd.")
        self.assertEqual(result["items"][0]["product_name"], "Dosing Pump")
        self.assertEqual(
            result["items"][0]["extra_attributes"],
            [
                {"label": "Specification", "value": "Gasket and O Ring"},
                {"label": "Type / Location", "value": "Chemical Injection Package"},
            ],
        )
        sheet = gemini_result_to_virtual_sheet(result)
        self.assertEqual(len(sheet["rows"]) - 1, 1)
        self.assertTrue(any("Specification: Gasket and O Ring" in str(cell) for cell in sheet["rows"][1]))
