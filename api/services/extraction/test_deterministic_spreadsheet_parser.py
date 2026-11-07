from django.test import SimpleTestCase
from unittest.mock import Mock

from .deterministic_parser import DeterministicSpreadsheetParser
from .extraction_orchestrator import ExtractionOrchestrator
from .structure_detector import StructureDetector
from api.services.gemini_extraction_service import normalize_result


class DeterministicSpreadsheetParserTests(SimpleTestCase):
    def test_supplier_columns_preserve_description_and_blank_positions(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": ["MTO", None, None, None, None, None, "MINGRI", None, "CVM", None]},
            {"sheet": "Sheet1", "row_number": 2, "values": ["SL NO", "DESCRIPTION", None, None, None, "QTY", "U.P.", "Amount", "UP", "AMT"]},
            {"sheet": "Sheet1", "row_number": 3, "values": [10, "150 x 150 x 70mm PVC JUNCTION BOX (WITHOUT GLAND HOLES), IP65 RATED", None, None, None, "1 NOS.", 9, 9, 5, 5]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        structure = StructureDetector().detect(document)
        result = DeterministicSpreadsheetParser().extract(document, structure)

        self.assertEqual(result["document_type"], "price_list")
        self.assertEqual(result["category"], "PRICE_LIST")
        self.assertEqual(result["supplier_name"], None)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["product_name"], rows[2]["values"][1])
        self.assertEqual(
            [(item["supplier_name"], item["unit_price"], item["line_total"]) for item in result["items"]],
            [("MINGRI", 9, 9), ("CVM", 5, 5)],
        )

    def test_orchestrator_keeps_valid_spreadsheet_items_without_gemini(self):
        document = {"file_type": "spreadsheet", "rows": []}
        structure = StructureDetector().detect({"file_type": "other", "rows": []})
        structure.structure = StructureDetector.UNKNOWN
        structure.confidence = 0.25
        candidate = {
            "document_type": "price_list",
            "category": "PRICE_LIST",
            "items": [
                {"product_name": "150 x 150 x 70mm PVC JUNCTION BOX (WITHOUT GLAND HOLES), IP65 RATED", "supplier_name": "MINGRI", "unit_price": "9", "line_total": "9"},
                {"product_name": "150 x 150 x 70mm PVC JUNCTION BOX (WITHOUT GLAND HOLES), IP65 RATED", "supplier_name": "CVM", "unit_price": "5", "line_total": "5"},
            ],
        }
        orchestrator = ExtractionOrchestrator()
        orchestrator.preprocessor.prepare = Mock(return_value=document)
        orchestrator.structure_detector.detect = Mock(return_value=structure)
        orchestrator.profile_detector.detect = Mock(return_value={"profile": None, "confidence": 0.0})
        orchestrator.parser.extract = Mock(return_value=candidate)
        orchestrator.gemini = Mock()
        orchestrator.gemini.extract_from_file.side_effect = AssertionError("Gemini must not be called")

        class File:
            def seek(self, *_args): pass
            def read(self): return b"xlsx-bytes"

        result = orchestrator.extract(File(), "comparison.xlsx")

        self.assertEqual(result["_extraction_method"], "deterministic")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual({item["supplier_name"] for item in result["items"]}, {"MINGRI", "CVM"})
        self.assertIn("150 x 150 x 70mm", result["items"][0]["product_name"])
        orchestrator.gemini.extract_from_file.assert_not_called()

    def test_section_heading_survives_parser_and_normalization(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": ["MTO", None, None, None, None, None, "MINGRI", None, "CVM", None]},
            {"sheet": "Sheet1", "row_number": 2, "values": ["SL NO", "DESCRIPTION", None, None, None, "QTY", "U.P.", "Amount", "UP", "AMT"]},
            {"sheet": "Sheet1", "row_number": 3, "values": [None, "MTO FOR INSULATION TAPE", None, None, None, None, None, 0, None, 0]},
            {"sheet": "Sheet1", "row_number": 4, "values": [84, "YELLOW", None, None, None, "05 NO.S", 0.5, 2.5, 1.5, 7.5]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        structure = StructureDetector().detect(document)
        parsed = DeterministicSpreadsheetParser().extract(document, structure)
        normalized = normalize_result(parsed)
        tape_rows = [item for item in normalized["items"] if item["product_name"] == "YELLOW"]

        self.assertEqual(len(tape_rows), 2)
        self.assertEqual({item["parent_category"] for item in tape_rows}, {"MTO FOR INSULATION TAPE"})
        self.assertEqual(
            {item["display_name"] for item in tape_rows},
            {"MTO FOR INSULATION TAPE | YELLOW"},
        )
        self.assertTrue(all(item["source_reference"] == "Sheet1 row 4" for item in tape_rows))

    def test_section_heading_is_structural_not_keyword_based(self):
        for heading_column in (0, 1, 2):
            values = [None, None, None, None, None, None, 0, 0, 0, 0]
            values[heading_column] = "Section Alpha"
            rows = [
                {"sheet": "Sheet1", "row_number": 1, "values": ["Title", None, None, None, None, None, "Supplier A", None, "Supplier B", None]},
                {"sheet": "Sheet1", "row_number": 2, "values": ["SL NO", "DESCRIPTION", None, None, None, "QTY", "U.P.", "Amount", "UP", "AMT"]},
                {"sheet": "Sheet1", "row_number": 3, "values": values},
                {"sheet": "Sheet1", "row_number": 4, "values": [1, "Item with 50mm2 * 8", None, None, None, "1 NOS", 2, 2, 3, 3]},
            ]
            document = {"file_type": "spreadsheet", "rows": rows}
            structure = StructureDetector().detect(document)
            result = DeterministicSpreadsheetParser().extract(document, structure)
            self.assertEqual(
                {item["parent_category"] for item in result["items"]},
                {"Section Alpha"},
            )

    def test_non_priced_reference_sheet_is_excluded(self):
        rows = [
            {"sheet": "Commercial", "row_number": 1, "values": ["Section Alpha", None, None, "Supplier A"]},
            {"sheet": "Commercial", "row_number": 2, "values": ["Description", "Qty", "Rate", "Amount"]},
            {"sheet": "Commercial", "row_number": 3, "values": ["Product 50mm2", 1, 12, 12]},
            {"sheet": "Reference", "row_number": 1, "values": ["Description", "Qty"]},
            {"sheet": "Reference", "row_number": 2, "values": ["Translated supporting description", 1]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        structure = StructureDetector().detect(document)
        result = DeterministicSpreadsheetParser().extract(document, structure)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["product_name"], "Product 50mm2")
        self.assertNotIn("Translated", result["items"][0].get("parent_category") or "")
        self.assertNotIn("Translated", result["items"][0].get("display_name") or "")

    def test_orchestrator_accepts_grouped_supplier_headers_deterministically(self):
        document = {"file_type": "spreadsheet", "rows": [
            {"sheet": "Sheet1", "row_number": 1, "values": ["Sl No", "Cable", "Cable Specifications", "Type", "Quantity in meters", None, "ELECTRICA", None, None, "Electricway", None, None]},
            {"sheet": "Sheet1", "row_number": 2, "values": [None, None, None, None, None, None, "UP", "TP", None, "up", "qty", "tp"]},
            {"sheet": "Sheet1", "row_number": 3, "values": [1, "CAT 6A", "Signal Shielded", "Non Marine", 65, "MARINE CABLE SPEC", 0.96, 62.4, "LS Cable", 3.0, 65, 195]},
        ]}
        orchestrator = ExtractionOrchestrator()
        orchestrator.preprocessor.prepare = Mock(return_value=document)
        orchestrator.profile_detector.detect = Mock(return_value={"profile": None, "confidence": 0.0})
        orchestrator.gemini = Mock()
        orchestrator.gemini.extract_from_file.side_effect = AssertionError("Gemini must not be called")

        class File:
            def seek(self, *_args): pass
            def read(self): return b"xlsx-bytes"

        result = orchestrator.extract(File(), "grouped.xlsx")
        self.assertEqual(result["_extraction_method"], "deterministic")
        self.assertEqual({item["supplier_name"] for item in result["items"]}, {"ELECTRICA", "Electricway"})
        self.assertTrue(all(item["product_name"] == "CAT 6A" for item in result["items"]))
        orchestrator.gemini.extract_from_file.assert_not_called()

    def test_unpriced_product_identity_does_not_become_parent_category(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": ["Description", "Qty", "Rate", "Amount"]},
            {"sheet": "Sheet1", "row_number": 2, "values": ["24x2x0.75MM Non Marine", None, None, None]},
            {"sheet": "Sheet1", "row_number": 3, "values": ["3x6MM", 20, 4.41, 88.2]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        structure = StructureDetector().detect(document)
        result = DeterministicSpreadsheetParser().extract(document, structure)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["product_name"], "3x6MM")
        self.assertIsNone(result["items"][0]["parent_category"])

    def test_supplier_group_fields_do_not_leak_between_groups(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": [
                "Sl No", "Product", "Specification", "Type", "Base Qty", None,
                "Supplier A", None, None, "Supplier B", None, None, None,
                "Supplier C", None, None,
            ]},
            {"sheet": "Sheet1", "row_number": 2, "values": [
                None, None, None, None, None, None,
                "Description", "UP", "Amount", "Description", "Qty", "UP", "Amount",
                "UP", "Amount", None,
            ]},
            {"sheet": "Sheet1", "row_number": 3, "values": [
                1, "Cable A", "Signal Shielded", "Marine", 65, None,
                "A quoted cable", 1.2, 78, "B quoted cable", "1 roll", 3, 3,
                4, 260, None,
            ]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        structure = StructureDetector().detect(document)
        result = DeterministicSpreadsheetParser().extract(document, structure)

        self.assertIsNotNone(result)
        items = {item["supplier_name"]: item for item in result["items"]}
        self.assertEqual(set(items), {"Supplier A", "Supplier B", "Supplier C"})
        self.assertEqual(str(items["Supplier A"]["quantity"]), "65")
        self.assertEqual(items["Supplier B"]["quantity"], "1 roll")
        self.assertEqual(str(items["Supplier C"]["quantity"]), "65")

        a_attrs = {a["label"]: a["value"] for a in items["Supplier A"]["extra_attributes"]}
        b_attrs = {a["label"]: a["value"] for a in items["Supplier B"]["extra_attributes"]}
        c_attrs = {a["label"]: a["value"] for a in items["Supplier C"]["extra_attributes"]}
        self.assertEqual(a_attrs.get("Supplier A Description"), "A quoted cable")
        self.assertNotIn("Supplier B Description", a_attrs)
        self.assertEqual(b_attrs.get("Supplier B Description"), "B quoted cable")
        self.assertNotIn("Supplier A Description", b_attrs)
        self.assertNotIn("Description", " ".join(c_attrs))
        for attrs in (a_attrs, b_attrs, c_attrs):
            self.assertIn("Specification", attrs)
            self.assertIn("Type", attrs)

    def test_each_supplier_uses_its_own_quantity_column_or_base_fallback(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": [
                "Sl No", "Product", "Base Qty", None, "Supplier A", None,
                "Supplier B", None, None, None, "Supplier C", None,
                "Supplier D", None, None, None,
            ]},
            {"sheet": "Sheet1", "row_number": 2, "values": [
                None, None, None, None, "UP", "Amount", "UP", "Qty", "Amount", None,
                "UP", "Amount", None, None, None, None,
            ]},
            {"sheet": "Sheet1", "row_number": 3, "values": [
                1, "Cable", 55, None, 1, 55, 2, 55, 110, None,
                3, 165, "D quoted cable", 4, 65, 260,
            ]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        result = DeterministicSpreadsheetParser().extract(document, StructureDetector().detect(document))
        items = {item["supplier_name"]: item for item in result["items"]}

        self.assertEqual(str(items["Supplier A"]["quantity"]), "55")
        self.assertEqual(str(items["Supplier B"]["quantity"]), "55")
        self.assertEqual(str(items["Supplier C"]["quantity"]), "55")
        self.assertEqual(str(items["Supplier D"]["quantity"]), "65")

    def test_supplier_names_as_price_headers_preserve_multiline_description(self):
        description = (
            "6AT TWO POLE MINIATURE CIRCUIT BREAKER\n"
            "AMPERE TRIP: 6A, CURVE CODE: C\n"
            "MOUNTING SUPPORT: DIN RAIL\n"
            "OPERATIONAL VOLTAGE: 220VAC, 50/60Hz & 24VDC\n"
            "(RECOMMENDED MODEL: SCHNEIDER, Acti9 iC60N, PART NO. M9F11206)"
        )
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": [
                "SL NO", "DESCRIPTION", "QTY", "UNIT", "Supplier A", "Amount",
                "Supplier B", "Amount", "Supplier C", "Amount",
            ]},
            {"sheet": "Sheet1", "row_number": 2, "values": [
                1, description, 2, "PC", 64, "", "", "", 0, "",
            ]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        result = DeterministicSpreadsheetParser().extract(document, StructureDetector().detect(document))

        self.assertIsNotNone(result)
        self.assertEqual({item["supplier_name"] for item in result["items"]}, {"Supplier A"})
        self.assertEqual(result["items"][0]["product_name"], description)
        self.assertEqual(str(result["items"][0]["unit_price"]), "64")

    def test_nearest_supplier_header_wins_over_upper_level_annotation(self):
        rows = [
            {"sheet": "Sheet1", "row_number": 1, "values": [None, None, None, "L3", None, "L2", None]},
            {"sheet": "Sheet1", "row_number": 2, "values": ["SL NO", "DESCRIPTION", "QTY", "CVM", None, "GOWIN MARINE", None]},
            {"sheet": "Sheet1", "row_number": 3, "values": [None, None, None, "UP", "TP", "UP", "TP"]},
            {"sheet": "Sheet1", "row_number": 4, "values": [1, "Hotwork item", 2, 10, 20, 12, 24]},
        ]
        document = {"file_type": "spreadsheet", "rows": rows}
        result = DeterministicSpreadsheetParser().extract(document, StructureDetector().detect(document))
        self.assertEqual({item["supplier_name"] for item in result["items"]}, {"CVM", "GOWIN MARINE"})

    def test_unit_decorated_headers_map_quantity_and_unit_price(self):
        rows = [
            {"sheet": "S", "row_number": 1, "values": ["Subject:", "RFQ cables", "", "", "Date:", "22 Sept 2026"]},
            {"sheet": "S", "row_number": 2, "values": ["No.", "Description", "Size", "Quantity\n(M)",
                                                       "Unit Price\n(USD/M)", "Amount\n(USD)"]},
            {"sheet": "S", "row_number": 3, "values": ["1.0", "SIGNAL", "11x0.5", "20.0", "3.7", "74.0"]},
            {"sheet": "S", "row_number": 4, "values": ["2.0", "POWER", "3x4", "30.0", "3.09", "92.7"]},
        ]
        structure = StructureDetector().detect({"file_type": "spreadsheet", "rows": rows})
        structure.structure = StructureDetector.STANDARD_TABLE
        result = DeterministicSpreadsheetParser().extract({"file_type": "spreadsheet", "rows": rows}, structure)
        self.assertEqual([(i["quantity"], i["uom"], i["unit_price"], i["line_total"]) for i in result["items"]],
                         [("20.0", "M", "3.7", "74.0"), ("30.0", "M", "3.09", "92.7")])

    def test_each_worksheet_uses_its_own_header(self):
        def row(sheet, number, values):
            return {"sheet": sheet, "row_number": number, "values": values}
        rows = [
            row("Comparison", 1, ["SL NO", "DESCRIPTION", "QTY", "UNIT", "SUPPLIER A", "", "SUPPLIER B", ""]),
            row("Comparison", 2, ["1", "SELECTOR SWITCH 22MM", "12", "", "4.5", "54", "5", "60"]),
            row("Comparison", 3, ["2", "RELAY 24V DC WITH BASE", "14", "", "4", "56", "7", "98"]),
            row("Shortage", 1, ["Sl no", "shortage", "QTY PER VESSEL", "6 VESSELS", "qty FOR 6", "unit", "Supplier A", "", "Supplier C", ""]),
            row("Shortage", 2, ["", "", "", "", "", "", "up", "tp", "UP", "TP"]),
            row("Shortage", 3, ["1", "ZB5AD2C+ZB5AZ105C", "4", "6", "24", "", "4.5", "18", "7.34", "29.36"]),
            row("Shortage", 4, ["2", "XB2BVM3LC,", "4", "6", "24", "", "2.4", "9.6", "4.37", "17.48"]),
        ]
        structure = StructureDetector().detect({"file_type": "spreadsheet", "rows": rows})
        structure.structure = StructureDetector.STANDARD_TABLE
        result = DeterministicSpreadsheetParser().extract({"file_type": "spreadsheet", "rows": rows}, structure)
        shortage = [(i["product_name"], i["supplier_name"], i["quantity"], i["unit_price"], i["line_total"])
                    for i in result["items"] if i["source_reference"].startswith("Shortage")]
        self.assertEqual(shortage, [
            ("ZB5AD2C+ZB5AZ105C", "Supplier A", "4", "4.5", "18"),
            ("ZB5AD2C+ZB5AZ105C", "Supplier C", "4", "7.34", "29.36"),
            ("XB2BVM3LC", "Supplier A", "4", "2.4", "9.6"),
            ("XB2BVM3LC", "Supplier C", "4", "4.37", "17.48"),
        ])
        self.assertEqual(len([i for i in result["items"] if i["source_reference"].startswith("Comparison")]), 4)

    def test_ampere_trip_is_a_product_attribute(self):
        from api.services.gemini_extraction_service import _canonical_row_type
        self.assertEqual(_canonical_row_type("PRODUCT", {"product_name": "MCB 2P AMPERE TRIP: 6A CURVE C"}), "PRODUCT")
        self.assertEqual(_canonical_row_type("PRODUCT", {"product_name": "Crane hire per trip"}), "SERVICE")
