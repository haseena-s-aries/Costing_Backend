import io
from email.message import EmailMessage
from decimal import Decimal
from unittest.mock import patch

import openpyxl
from PIL import Image
from django.test import SimpleTestCase, TestCase, override_settings

from api.exceptions import ImportServiceError
from api.models import CommodityPriceHistory
from api.services.import_service import ImportService
from api.services.gemini_extraction_service import (
    gemini_result_to_virtual_sheet,
    validate_gemini_extraction,
)
from api.services.market_price_service import MarketPriceError, SHORT_TON_TO_METRIC_TONNE, YahooHRCMarketPriceService
from api.services.parser_service import ParserService
from api.services.product_normalization_service import line_item_group_key
from api.views import costing_amount


class NamedBytesIO(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name
        self.size = len(data)


def yahoo_payload(timestamps, closes):
    return {
        "chart": {
            "result": [{
                "meta": {
                    "currency": "USD",
                    "symbol": "HRC=F",
                    "exchangeName": "CMX",
                    "instrumentType": "FUTURE",
                },
                "timestamp": timestamps,
                "indicators": {"quote": [{"close": closes}]},
            }],
            "error": None,
        }
    }


@override_settings(METALS_DEV_API_KEY="")
class YahooHRCMarketPriceServiceTests(TestCase):
    def setUp(self):
        self.service = YahooHRCMarketPriceService()

    def fetch_with(self, payload, rate=Decimal("3.67250000")):
        with patch.object(YahooHRCMarketPriceService, "_get_chart_json", return_value=payload), patch.object(
            YahooHRCMarketPriceService,
            "_usd_to_aed_rate",
            return_value=rate,
        ):
            return self.service.fetch_latest(force=True)

    def test_valid_yahoo_response_saves_delayed_hrc_benchmark(self):
        payload = yahoo_payload([1719878400], [Decimal("720.50")])

        result = self.fetch_with(payload)

        self.assertEqual(result["status"], "success")
        price = CommodityPriceHistory.objects.get(provider="yahoo_finance", provider_symbol="HRC=F")
        self.assertEqual(price.raw_price, Decimal("720.50"))
        self.assertEqual(price.raw_unit, "SHORT_TON")
        self.assertEqual(price.market_date.isoformat(), "2024-07-02")
        self.assertTrue(price.response_metadata["is_delayed"])

    def test_null_latest_close_uses_previous_valid_market_day(self):
        payload = yahoo_payload([1719792000, 1719878400], [Decimal("710.00"), None])

        self.fetch_with(payload)

        price = CommodityPriceHistory.objects.get(provider_symbol="HRC=F")
        self.assertEqual(price.raw_price, Decimal("710.00"))
        self.assertEqual(price.market_date.isoformat(), "2024-07-01")

    def test_weekend_or_holiday_response_keeps_latest_valid_close(self):
        payload = yahoo_payload([1719532800, 1719619200, 1719705600], [Decimal("700"), None, None])

        self.fetch_with(payload)

        price = CommodityPriceHistory.objects.get(provider_symbol="HRC=F")
        self.assertEqual(price.market_date.isoformat(), "2024-06-28")

    def test_timeout_returns_stale_saved_value_without_replacing_it(self):
        self.fetch_with(yahoo_payload([1719878400], [Decimal("720.50")]))

        with patch.object(YahooHRCMarketPriceService, "_get_chart_json", side_effect=MarketPriceError("Yahoo Finance request timed out")):
            result = self.service.fetch_latest(force=True)

        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["is_stale"])
        self.assertEqual(CommodityPriceHistory.objects.count(), 1)
        self.assertEqual(CommodityPriceHistory.objects.get().raw_price, Decimal("720.50"))

    def test_malformed_response_returns_stale_fallback(self):
        self.fetch_with(yahoo_payload([1719878400], [Decimal("720.50")]))

        with patch.object(YahooHRCMarketPriceService, "_get_chart_json", return_value={"chart": {"result": []}}):
            result = self.service.fetch_latest(force=True)

        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["is_stale"])

    def test_duplicate_market_date_updates_existing_row(self):
        self.fetch_with(yahoo_payload([1719878400], [Decimal("720.50")]))
        result = self.fetch_with(yahoo_payload([1719878400], [Decimal("730.25")]))

        self.assertEqual(result["updated"], 1)
        self.assertEqual(CommodityPriceHistory.objects.count(), 1)
        self.assertEqual(CommodityPriceHistory.objects.get().raw_price, Decimal("730.25"))

    def test_short_ton_to_metric_tonne_aed_conversion_uses_decimal(self):
        payload = yahoo_payload([1719878400], [Decimal("907.18474")])
        self.fetch_with(payload, rate=Decimal("3.6725"))

        price = CommodityPriceHistory.objects.get(provider_symbol="HRC=F")
        self.assertEqual(price.usd_per_metric_tonne, Decimal("1000.00000000"))
        self.assertEqual(price.aed_per_metric_tonne, Decimal("3672.50000000"))
        self.assertEqual(price.aed_per_kg, Decimal("3.67250000"))
        self.assertEqual(
            (price.raw_price / SHORT_TON_TO_METRIC_TONNE).quantize(Decimal("0.00000001")),
            price.usd_per_metric_tonne,
        )

    def test_invalid_close_does_not_replace_valid_stored_data(self):
        self.fetch_with(yahoo_payload([1719878400], [Decimal("720.50")]))

        result = self.fetch_with(yahoo_payload([1719964800], [Decimal("0")]))

        self.assertEqual(result["status"], "stale")
        self.assertEqual(CommodityPriceHistory.objects.count(), 1)
        self.assertEqual(CommodityPriceHistory.objects.get().market_date.isoformat(), "2024-07-02")


class GenericParserServiceTests(SimpleTestCase):
    def parse(self, data, filename):
        return ParserService().parse(NamedBytesIO(data, filename), filename)

    def test_excel_preserves_spreadsheet_mapping_flow(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Supplier Format A"
        sheet.append(["Item Code", "Description", "Qty", "UOM", "Rate", "Currency"])
        sheet.append(["CBL-001", "Copper Cable 6mm2", 10, "MTR", 4.85, "AED"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        parsed = self.parse(buffer.getvalue(), "supplier-format-a.xlsx")

        self.assertEqual(parsed["source_type"], "excel")
        self.assertFalse(parsed["sheets"][0]["is_virtual"])
        self.assertEqual(parsed["sheets"][0]["rows"][0][0], "Item Code")

    def test_csv_preserves_uploaded_columns(self):
        data = b"Code,Material Details,Qty Required,UOM,Quoted Rate,Curr.\nMCB-1,MCB 32A,3,PCS,28.5,AED\n"

        parsed = self.parse(data, "quotation.csv")

        self.assertEqual(parsed["source_type"], "csv")
        self.assertFalse(parsed["sheets"][0]["is_virtual"])
        self.assertEqual(parsed["sheets"][0]["rows"][0][1], "Material Details")

    def test_text_document_separates_metadata_from_line_items(self):
        text = """Quote No: QT-991
Date: 2026-02-06
Customer: SARIKA
Currency: AED
Grand Total: 48.50
Product Description  Qty UOM Unit Price Line Total
Copper Cable 6mm2 10 MTR 4.85 AED 48.50
"""

        parsed = self.parse(text.encode(), "generic-quote.txt")

        self.assertEqual(parsed["document_fields"]["document_number"], "QT-991")
        self.assertEqual(parsed["document_fields"]["customer_to"], "SARIKA")
        headers = parsed["sheets"][0]["rows"][0]
        self.assertNotIn("document_number", headers)
        self.assertIn("product_name", headers)
        self.assertIn("unit_price", headers)
        self.assertIn("document_number", parsed["document_field_meta"])

    def test_email_body_and_attachment_are_combined_without_document_tables(self):
        message = EmailMessage()
        message["From"] = "supplier@example.com"
        message["To"] = "buyer@example.com"
        message["Subject"] = "Quotation"
        message.set_content("Quote No: EM-45\nCurrency: AED\n")
        message.add_attachment(
            b"Description,Qty,UOM,Unit Price,Line Total\nTransportation,1,PCS,50,50\nXLPE Cable,2,MTR,30,60\n",
            maintype="text",
            subtype="csv",
            filename="lines.csv",
        )

        parsed = self.parse(message.as_bytes(), "mail.eml")

        self.assertEqual(parsed["source_type"], "email")
        self.assertEqual(parsed["document_fields"]["document_number"], "EM-45")
        attachment_sheet = parsed["sheets"][-1]
        self.assertEqual(attachment_sheet["rows"][0][0], "Description")
        self.assertEqual(attachment_sheet["rows"][1][0], "Transportation")
        self.assertEqual(attachment_sheet["rows"][2][0], "XLPE Cable")

    def test_purchase_order_table_extracts_one_product_row(self):
        text = """PURCHASE ORDER
Phone: +971 4 555 1234
TRN: 100234567800003
Description Qty UOM Unit Price VAT VAT Amount Total
CABLE MARINE CU/BRAIDED 0.6/1KV HALOGEN FREE FIRE RETARDENT IEC332 CAT A-BLACK O/S - SIZE 25MMX1C 40 Nos 23.00 5 46.00 966.00
Grand Total: 966.00
Amount in words: Nine Hundred Sixty Six
Terms and Conditions apply
"""
        parsed = self.parse(text.encode(), "purchase-order.txt")
        rows = parsed["sheets"][0]["rows"]

        self.assertEqual(len(rows), 2)
        item = dict(zip(rows[0], rows[1]))
        self.assertEqual(item["product_name"], "CABLE MARINE CU/BRAIDED 0.6/1KV HALOGEN FREE FIRE RETARDENT IEC332 CAT A-BLACK O/S - SIZE 25MMX1C")
        self.assertEqual(item["quantity"], "40")
        self.assertEqual(item["unit_of_measurement"], "Nos")
        self.assertEqual(item["unit_price"], "23.00")
        self.assertEqual(item["currency"], "AED")
        self.assertEqual(item["tax"], "5")
        self.assertEqual(item["line_total"], "966.00")

    def test_document_without_reliable_table_needs_review(self):
        text = """PURCHASE ORDER
Phone: +971 4 555 1234
TRN: 100234567800003
Grand Total: 966.00
Terms and Conditions apply
"""
        with self.assertRaises(ImportServiceError):
            self.parse(text.encode(), "no-table.txt")


    def test_table_aliases_and_relationships_map_price_columns(self):
        text = """QUOTATION
Currency: USD
Product Details Model/Specification Grade Order Qty Unit Price (USD) Total Amount
Chocks bulwark SWL6T ISO13729 B400*250*381 ZG230-450 2 Nos 345 690
Stand rollers SWL64T ISO13776 A350-H1000 ZG230-450+STEEL 2 Nos 945 1890
Grand Total: 2580
"""
        parsed = self.parse(text.encode(), "marine-quotation.txt")
        rows = parsed["sheets"][0]["rows"]
        first = dict(zip(rows[0], rows[1]))
        second = dict(zip(rows[0], rows[2]))

        self.assertEqual(first["product_name"], "Chocks bulwark SWL6T | ISO13729 B400*250*381 ZG230-450")
        self.assertEqual(first["specification"], "ISO13729 B400*250*381")
        self.assertEqual(first["material"], "ZG230-450")
        self.assertEqual(first["quantity"], "2")
        self.assertEqual(first["unit_price"], "345")
        self.assertEqual(first["line_total"], "690")
        self.assertEqual(first["currency"], "USD")
        self.assertEqual(second["unit_price"], "945")
        self.assertEqual(second["line_total"], "1890")

    def test_serial_number_column_is_not_item_code(self):
        table = [
            ["Sl No", "Item Description", "Qty", "UOM", "Rate", "Amount"],
            ["1", "Cable Tray", "2", "Nos", "100", "200"],
        ]
        converted = ParserService()._table_to_line_items(table, {"page": 1, "table": 1})
        rows, _meta = converted
        item = dict(zip(rows[0], rows[1]))

        self.assertIsNone(item.get("item_code"))
        self.assertEqual(item["product_name"], "Cable Tray")


    def test_serial_lines_are_not_used_as_descriptions(self):
        text = """TAX INVOICE
Description Qty UOM Unit Price VAT VAT Amount Total
001
AIRPORT CLEARANCE AND DELIVERY
1 Nos 1920.00 0 0.00 1920.00
002
TRANSPORTATION FROM AJMAN TO RAS AL KHOR
1 Nos 650.00 5 32.50 682.50
Grand Total: 2602.50
"""
        parsed = self.parse(text.encode(), "services-invoice.txt")
        rows = parsed["sheets"][0]["rows"]
        first = dict(zip(rows[0], rows[1]))
        second = dict(zip(rows[0], rows[2]))

        self.assertEqual(first["product_name"], "AIRPORT CLEARANCE AND DELIVERY")
        self.assertEqual(first["quantity"], "1")
        self.assertEqual(first["unit_of_measurement"], "Nos")
        self.assertEqual(first["unit_price"], "1920.00")
        self.assertEqual(first["tax"], "0")
        self.assertEqual(first["tax_amount"], "0.00")
        self.assertEqual(first["line_total"], "1920.00")
        self.assertEqual(second["product_name"], "TRANSPORTATION FROM AJMAN TO RAS AL KHOR")
        self.assertEqual(second["unit_price"], "650.00")
        self.assertEqual(second["tax"], "5")
        self.assertEqual(second["tax_amount"], "32.50")
        self.assertEqual(second["line_total"], "682.50")

    def test_charge_description_can_come_from_non_numeric_serial_column(self):
        table = [
            ["No.", "Product Description", "Quantity", "Unit Price (USD)", "Total Price (USD)"],
            ["BVCertificate Fee:", None, None, None, "$ 2,154.00"],
        ]
        rows, _meta = ParserService()._table_to_line_items(table, {"page": 1, "table": 1})
        item = dict(zip(rows[0], rows[1]))

        self.assertEqual(item["product_name"], "BV Certificate Fee")
        self.assertEqual(item["row_type"], "CHARGE")
        self.assertEqual(item["unit_price"], "2154.00")

    def test_vague_item_serial_column_is_ignored(self):
        table = [
            ["Item", "Description", "Qty", "UOM", "Unit Price", "VAT", "VAT Amount", "Total"],
            ["001", "AIRPORT CLEARANCE AND DELIVERY", "1", "Nos", "1920.00", "0", "0.00", "1920.00"],
            ["002", "TRANSPORTATION FROM AJMAN TO RAS AL KHOR", "1", "Nos", "650.00", "5", "32.50", "682.50"],
        ]
        rows, _meta = ParserService()._table_to_line_items(table, {"page": 1, "table": 1})
        first = dict(zip(rows[0], rows[1]))
        second = dict(zip(rows[0], rows[2]))

        self.assertIsNone(first.get("item_code"))
        self.assertEqual(first["product_name"], "AIRPORT CLEARANCE AND DELIVERY")
        self.assertEqual(second["product_name"], "TRANSPORTATION FROM AJMAN TO RAS AL KHOR")


    def test_structured_procurement_output_has_header_items_and_validity(self):
        text = """SUPPLIER QUOTATION
Quote No: SQ-500
Date: 23-Jul-2026
Supplier: Gulf Marine Trading
Currency: USD
Product Details Qty UOM Unit Price (USD) Total Amount
Marine Valve 2 Nos 345 690
Grand Total: 690
"""
        parsed = self.parse(text.encode(), "supplier-quotation.txt")
        structured = parsed["extraction_summary"]["structured_document"]

        self.assertEqual(structured["document_type"], "QUOTATION")
        self.assertEqual(structured["header"]["supplier_name"], "Gulf Marine Trading")
        self.assertEqual(structured["header"]["document_date"], "2026-07-23")
        self.assertEqual(structured["header"]["currency"], "USD")
        self.assertEqual(structured["header"]["grand_total"], 690.0)
        self.assertGreaterEqual(structured["confidence"]["supplier_name"], 70)
        self.assertEqual(len(structured["items"]), 1)
        item = structured["items"][0]
        self.assertEqual(item["description"], "Marine Valve")
        self.assertEqual(item["quantity"], 2.0)
        self.assertEqual(item["unit"], "Nos")
        self.assertEqual(item["unit_price"], 345.0)
        self.assertEqual(item["currency"], "USD")
        self.assertEqual(item["line_total"], 690.0)
        self.assertTrue(item["row_valid"])


    def test_visual_words_reconstruct_serial_wrapped_rows(self):
        def word(text, x0, top, width=30, height=8):
            return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + height}

        words = [
            word("Item", 20, 10),
            word("Description", 90, 10, 70),
            word("Qty", 330, 10),
            word("UOM", 380, 10),
            word("Unit", 455, 10),
            word("Price", 492, 10),
            word("VAT", 560, 10),
            word("VAT", 615, 10),
            word("Amount", 650, 10),
            word("Total", 735, 10),
            word("001", 22, 35),
            word("AIRPORT", 92, 47, 55),
            word("CLEARANCE", 150, 47, 78),
            word("AND", 232, 47, 25),
            word("DELIVERY", 260, 47, 62),
            word("1", 335, 47, 10),
            word("Nos", 382, 47),
            word("1920.00", 458, 47, 55),
            word("0", 563, 47, 10),
            word("0.00", 618, 47, 35),
            word("1920.00", 735, 47, 58),
            word("002", 22, 72),
            word("TRANSPORTATION", 92, 84, 105),
            word("FROM", 200, 84, 35),
            word("AJMAN", 240, 84, 50),
            word("TO", 294, 84, 18),
            word("RAS", 92, 96, 25),
            word("AL", 122, 96, 18),
            word("KHOR", 145, 96, 38),
            word("1", 335, 84, 10),
            word("Nos", 382, 84),
            word("650.00", 458, 84, 50),
            word("5", 563, 84, 10),
            word("32.50", 618, 84, 38),
            word("682.50", 735, 84, 50),
            word("Grand", 620, 125),
            word("Total", 660, 125),
        ]

        rows, _meta = ParserService()._words_to_line_items(words, {"page": 1, "table": "visual"})
        first = dict(zip(rows[0], rows[1]))
        second = dict(zip(rows[0], rows[2]))

        self.assertEqual(first["product_name"], "AIRPORT CLEARANCE AND DELIVERY")
        self.assertEqual(first["quantity"], "1")
        self.assertEqual(first["unit_of_measurement"], "Nos")
        self.assertEqual(first["unit_price"], "1920.00")
        self.assertEqual(first["tax"], "0")
        self.assertEqual(first["tax_amount"], "0.00")
        self.assertEqual(first["line_total"], "1920.00")
        self.assertEqual(second["product_name"], "TRANSPORTATION FROM AJMAN TO RAS AL KHOR")
        self.assertEqual(second["unit_price"], "650.00")
        self.assertEqual(second["tax"], "5")
        self.assertEqual(second["tax_amount"], "32.50")
        self.assertEqual(second["line_total"], "682.50")

    def test_money_values_with_currency_symbols_are_normalized(self):
        values = ParserService()._repair_price_columns({
            "quantity": "2",
            "unit_price": "$ 1,890.00",
            "line_total": "$ 3,780.00",
        })

        self.assertEqual(values["unit_price"], "1890.00")
        self.assertEqual(values["line_total"], "3780.00")
        self.assertEqual(values["currency"], "USD")

        shifted = ParserService()._repair_price_columns({
            "quantity": "2",
            "unit_of_measurement": "945",
            "unit_price": "$ 1 ,890.00",
            "currency": "USD",
        })
        self.assertIsNone(shifted["unit_of_measurement"])
        self.assertEqual(shifted["unit_price"], "945")
        self.assertEqual(shifted["line_total"], "1890.00")

    def test_european_decimal_comma_prices_are_repaired_by_reconciliation(self):
        values = ParserService()._repair_price_columns({
            "quantity": "105",
            "unit_price": "2,180",
            "line_total": "228,90",
            "currency": "EUR",
        })

        self.assertEqual(values["unit_price"], "2.180")
        self.assertEqual(values["line_total"], "228.90")

    def test_po_date_fallback_accepts_common_separators(self):
        fields, _meta = ParserService()._document_fields_from_text("""PURCHASE ORDER
PO Number: PO-42
PO Date 16.02.2026
Delivery Date: 13/03/2026
""")

        self.assertEqual(fields["document_date"], "2026-02-16")

    def test_delivery_date_fills_document_date_when_po_date_missing(self):
        fields, _meta = ParserService()._document_fields_from_text("""PURCHASE ORDER
Delivery Date: 13-03-2026
""")

        self.assertEqual(fields["document_date"], "2026-03-13")

    def test_costing_amount_uses_row_total_or_unit_plus_vat_amount(self):
        self.assertEqual(costing_amount({"quantity": "1", "unit_price": "650.00", "tax_amount": "32.50", "line_total": "682.50"}), "682.50")
        self.assertEqual(costing_amount({"quantity": "2", "unit_price": "100.00", "tax_amount": "10.00"}), "210.00")

    def test_line_item_group_key_separates_types_for_reuse(self):
        mapped = {"product_name": "Transportation from Ajman to Ras Al Khor"}

        self.assertEqual(
            line_item_group_key(mapped, "SERVICE"),
            "SERVICE:description:transportation from ajman to ras al khor",
        )
        self.assertEqual(
            line_item_group_key(mapped, "CHARGE"),
            "CHARGE:description:transportation from ajman to ras al khor",
        )

    def test_purchase_order_supplier_uses_order_issued_to(self):
        text = """PURCHASE ORDER
PO Number: PO-42
PO Date: 2026-02-06
Bill To: Buyer Company LLC
Order issued to:
Marine Cable Supplier LLC
Description Qty UOM Unit Price Total
Cable 1 Nos 10 10
"""

        fields, _meta = ParserService()._document_fields_from_text(text)

        self.assertEqual(fields["supplier"], "Marine Cable Supplier LLC")
        self.assertNotEqual(fields["supplier"], "Buyer Company LLC")



class ImportServiceRegressionTests(SimpleTestCase):
    def test_import_service_has_preview_row_serializer(self):
        self.assertTrue(callable(getattr(ImportService(), "_preview_row", None)))

    def test_gemini_result_maps_complete_document_item_schema(self):
        result = {
            "document_type": "quotation",
            "category": "QUOTATION",
            "supplier_name": "Valve Supplier LLC",
            "document_date": "2026-02-16",
            "currency": "AED",
            "document_number": "Q-1",
            "items": [
                {
                    "item_type": "PRODUCT",
                    "product_service_name": "SS316 Ball Valve",
                    "part_number": "BV-316-25",
                    "brand": "ACME",
                    "quantity": "20",
                    "uom": "pcs",
                    "unit_price": "AED 150",
                    "currency": "AED",
                    "total": "3000",
                    "purchase_date": "2026-04-10",
                    "source_reference": "07450",
                },
                {
                    "item_type": "SERVICE",
                    "product_service_name": "Door-to-Port",
                    "quantity": "1",
                    "unit_price": "USD 20",
                    "currency": "USD",
                    "total": "20",
                },
            ],
        }

        sheet = gemini_result_to_virtual_sheet(result)
        headers = sheet["rows"][0]
        product = dict(zip(headers, sheet["rows"][1]))
        service = dict(zip(headers, sheet["rows"][2]))

        self.assertEqual(product["row_type"], "PRODUCT")
        self.assertEqual(product["product_name"], "SS316 Ball Valve")
        self.assertEqual(product["part_number"], "BV-316-25")
        self.assertEqual(product["brand"], "ACME")
        self.assertEqual(product["unit_of_measurement"], "pcs")
        self.assertEqual(product["unit_price"], "150")
        self.assertEqual(product["line_total"], "3000")
        self.assertEqual(product["purchase_date"], "2026-04-10")
        self.assertEqual(product["source_reference"], "07450")
        self.assertEqual(service["row_type"], "SERVICE")
        self.assertEqual(service["currency"], "USD")

    def test_gemini_validation_flags_suspicious_records_only(self):
        validation = validate_gemini_extraction({
            "document_type": "quotation",
            "supplier_name": "Supplier LLC",
            "document_date": "2026-02-16",
            "currency": "AED",
            "items": [
                {
                    "item_type": "PRODUCT",
                    "product_service_name": "Pressure Gauge",
                    "quantity": "10",
                    "uom": "pcs",
                    "unit_price": "25",
                    "total": "250",
                    "currency": "AED",
                },
                {
                    "item_type": "PRODUCT",
                    "product_service_name": None,
                    "quantity": "10",
                    "unit_price": "25",
                    "total": "2000",
                    "currency": "AED",
                },
            ],
        })

        self.assertFalse(validation["is_valid"])
        self.assertEqual(len(validation["suspicious_items"]), 1)
        self.assertEqual(validation["suspicious_items"][0]["index"], 1)

    def test_gemini_validation_does_not_verify_routine_missing_currency(self):
        validation = validate_gemini_extraction({
            "document_type": "invoice",
            "supplier_name": "Arrow Transport LLC",
            "document_date": "2026-04-10",
            "currency": None,
            "items": [
                {
                    "item_type": "SERVICE",
                    "product_service_name": "AL HAUL TO KFKN PORT",
                    "total": "600.00",
                    "purchase_date": "2026-04-10",
                }
            ],
        })

        self.assertEqual(validation["suspicious_items"], [])

    def test_gemini_charge_rows_skip_vat_description(self):
        sheet = gemini_result_to_virtual_sheet({
            "document_type": "invoice",
            "supplier_name": "Arrow Transport LLC",
            "document_date": "2026-04-10",
            "currency": "AED",
            "items": [
                {
                    "item_type": "CHARGE",
                    "product_service_name": "VAT 5%",
                    "total": "45.00",
                    "currency": "AED",
                    "purchase_date": "2026-04-10",
                }
            ],
        })

        self.assertEqual(len(sheet["rows"]), 1)
        self.assertIn("tax_or_total_row_skipped", sheet["gemini_validation_warnings"])

    def test_gemini_scanned_service_amount_restores_missing_decimal(self):
        sheet = gemini_result_to_virtual_sheet({
            "document_type": "invoice",
            "supplier_name": "Arrow Transport LLC",
            "document_date": "2026-04-11",
            "currency": "AED",
            "items": [
                {
                    "item_type": "SERVICE",
                    "product_service_name": "AL HAUL W/H TO SHJ PORT KHUD",
                    "total": "45000",
                    "purchase_date": "2026-04-11",
                },
            ],
        })

        headers = sheet["rows"][0]
        service = dict(zip(headers, sheet["rows"][1]))

        self.assertEqual(service["line_total"], "450")

    def test_gemini_european_decimal_comma_prices_are_repaired_by_reconciliation(self):
        sheet = gemini_result_to_virtual_sheet({
            "document_type": "quotation",
            "supplier_name": "electrica",
            "document_date": "2026-07-13",
            "currency": "EUR",
            "items": [
                {
                    "item_type": "PRODUCT",
                    "product_service_name": "MARINE CABLE XTCUZ1-K (AS) 2X2,5 0,6/1KV, BLACK",
                    "quantity": "105",
                    "uom": "m",
                    "unit_price": "2,180",
                    "total": "228,90",
                    "currency": "EUR",
                },
            ],
        })

        headers = sheet["rows"][0]
        product = dict(zip(headers, sheet["rows"][1]))

        self.assertEqual(product["unit_price"], "2.180")
        self.assertEqual(product["line_total"], "228.90")

    def test_parser_quality_triggers_gemini_for_possible_first_page_only(self):
        quality = ImportService()._parser_quality({
            "sheets": [
                {
                    "rows": [
                        ["row_type", "product_name", "quantity", "unit_price", "line_total"],
                        ["PRODUCT", "Marine Cable", "1", "10", "10"],
                    ],
                }
            ],
            "document_fields": {"supplier": "Cable Supplier LLC", "document_date": "2026-02-16", "currency": "AED"},
            "extracted_text": "Page 1 of 3 continued on next page",
            "extraction_summary": {"method": "pdf_visual_tables", "tables": [{"page": 1, "rows": 1}]},
        })

        self.assertIn("possible_first_page_only_extraction", quality["reasons"])
        self.assertTrue(ImportService()._should_use_gemini(quality))

    def test_image_parser_degrades_when_ocr_binary_is_unavailable(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (80, 30), "white").save(image_bytes, format="PNG")
        file_obj = NamedBytesIO(image_bytes.getvalue(), "scan.png")

        with patch("pytesseract.image_to_string", side_effect=RuntimeError("missing tesseract")):
            parsed = ParserService().parse(file_obj, file_obj.name)

        self.assertEqual(parsed["source_type"], "image")
        self.assertEqual(parsed["extraction_summary"]["method"], "needs_review_ocr_unavailable")
        self.assertEqual(parsed["sheets"][0]["rows"][0], ["row_type", "product_name", "item_code", "part_number", "specification", "material", "brand", "quantity", "unit_of_measurement", "unit_price", "currency", "tax", "tax_amount", "line_total"])
