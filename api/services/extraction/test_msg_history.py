from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import extract_msg
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from api.services.msg_commercial_history import (
    split_thread, is_commercial, extract_history, number, email_date, signature_company, context_image_ids,
)
from api.services.gemini_extraction_service import (
    GeminiExtractionService, gemini_result_to_virtual_sheet, normalize_result,
)
from api.services.extraction.extraction_orchestrator import ExtractionOrchestrator
from api.services.regression_testing import compare_results


ROOT = Path(__file__).resolve().parents[3] / "test_fixtures"
HISTORY_FILES = (
    "SUPPLIER 1-AMPS-RE_ DU5111_BATTERY CAHRGING COST.msg",
    "Supplier 2_NGFS_RE_ SHIPMENT FROM CHINA TO SINGAPORE BY AIR FREIGHT -MOTORS_20260820_061014.msg",
    "SUPPLIER 2_RASHED HUSSEIN_e_ RFQ_SUPPLY OF UPS -_20260819_101052.msg",
)


def metadata(path):
    message = extract_msg.Message(str(path))
    try:
        return {"body": message.body, "sender": message.sender, "sent_date": str(message.date)}
    finally:
        message.close()


class MsgHistoryTests(SimpleTestCase):
    def test_outlook_cluster_keeps_sender_and_sent_date(self):
        body = "Latest text\nFrom: seller@example.com\nSent: Wednesday, 3 June 2026 12:09 pm\nTo: buyer\nSubject: Offer\nPrice: SGD 3,95"
        blocks = split_thread(body, "latest@example.com", "2026-08-20")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1]["date"], "2026-06-03")
        self.assertEqual(blocks[1]["sender"], "seller@example.com")

    def test_top_email_never_borrows_next_reply_date(self):
        blocks = split_thread("Dhs 100\nOn Wednesday, August 5, 2026 at 11:26 AM buyer@example.com wrote:\nPlease quote", "seller", "2026-08-18")
        self.assertEqual([b["date"] for b in blocks], ["2026-08-18", "2026-08-05"])

    def test_hyphen_price_is_commercial(self):
        block = split_thread("Please find revised price\n1) Assembly\nUnit Price - 20,250/- plus vat")[0]
        self.assertTrue(is_commercial(block))

    def test_buyer_copied_price_is_context(self):
        block = split_thread("Please advise the validity of the cost and availability.\n1) Assembly\nQTY - 1 No\nUnit Price - 15750/- plus vat")[0]
        self.assertFalse(is_commercial(block))

    def test_acknowledgement_and_signature_numbers_are_not_quotes(self):
        for body in ("We will check and revert with charges soon.\nRegards,\nTel: 6562261886", "Please quote your best price.\nRegards\nSample Ltd"):
            self.assertFalse(is_commercial(split_thread(body)[0]))

    def test_number_conventions(self):
        for raw, expected in (("3,70", "3.70"), ("1.576,20", "1576.20"), ("3,95", "3.95"),
                              ("1.599,75", "1599.75"), ("58,150", "58150"), ("20,250/-", None), ("1,23,45", None)):
            with self.subTest(raw=raw):
                self.assertEqual(number(raw), expected)

    def test_seller_signature_beats_brand_and_person_title(self):
        self.assertEqual(signature_company("FAMOUS MAKE\nRegards,\nAgent\nSeller Works L.L.C.\nhttps://brand.example/"), "Seller Works L.L.C.")
        self.assertEqual(signature_company("Agent\nOperational Supervisor / Cargo PTE LTD"), "Cargo PTE LTD")

    def test_supplier_inline_quotation_is_not_classified_as_buyer_context(self):
        html = '<p>From: Seller</p><p>Sent: 20 August 2026</p><p>Please find quotation attached</p><img src="cid:quote">'
        self.assertEqual(context_image_ids(html), set())

    def test_buyer_inline_request_image_is_context(self):
        html = '<p>From: Buyer</p><p>Sent: 20 August 2026</p><p>Please quote below equipment</p><img src="cid:request">'
        self.assertEqual(context_image_ids(html), {"request"})

    def test_different_supplier_currencies_are_not_inherited(self):
        body = "Please find price\n1) Pump\nUSD 400\nRegards\nSeller A Ltd\nFrom: other@example.com\nSent: 3 June 2026\nPlease find price\n1) Pump\nUnit Price - 450\nRegards\nSeller B Ltd"
        self.assertIsNone(extract_history(body, "seller@example.com", "2026-08-20"))

    def test_multiple_tiers_in_one_numbered_description_require_fallback(self):
        body = "Please find price\n1) Pump\nUSD 400\nUSD 450\nRegards\nSeller Ltd\nFrom: seller@example.com\nSent: 3 June 2026\nPlease find price\n1) Pump\nUSD 500\nRegards\nSeller Ltd"
        self.assertIsNone(extract_history(body, "seller@example.com", "2026-08-20"))

    def test_unsupported_block_does_not_replace_history_with_partial_rows(self):
        body = "Please find quotation USD 500 for unnamed details\nRegards\nSeller Ltd\nFrom: seller@example.com\nSent: 3 June 2026\nPlease find price\n1) Pump\nUSD 400\nRegards\nSeller Ltd"
        self.assertIsNone(extract_history(body, "seller@example.com", "2026-08-20"))

    def test_distinct_same_day_offers_survive(self):
        body = "Please find price\n1) Pump\nUSD 400\nRegards\nSeller Ltd\nFrom: seller@example.com\nSent: 20 August 2026\nPlease find price\n1) Pump\nUSD 450\nRegards\nSeller Ltd"
        result = extract_history(body, "seller@example.com", "2026-08-20")
        self.assertEqual([i["unit_price"] for i in result["items"]], ["400", "450"])
        self.assertNotEqual(*[i["source_block_id"] for i in result["items"]])

    def test_missing_date_does_not_use_newest_date_for_old_block(self):
        body = "Please find price\n1) Pump\nUSD 400\nRegards\nSeller Ltd\nFrom: seller@example.com\nPlease find price\n1) Pump\nUSD 450\nRegards\nSeller Ltd"
        self.assertIsNone(extract_history(body, "seller@example.com", "2026-08-20"))

    def test_fixture_source_history_and_existing_golden_contract(self):
        for name, count in zip(HISTORY_FILES, (3, 2, 3)):
            with self.subTest(file=name):
                path = ROOT / "inputs" / name
                result = normalize_result(extract_history(**metadata(path)))
                expected = json.loads((ROOT / "expected" / "extraction" / (path.stem + ".json")).read_text(encoding="utf-8"))
                comparison = compare_results(result, expected)
                self.assertTrue(comparison["passed"], comparison["differences"])
                self.assertEqual(len(result["items"]), count)
                for actual, want in zip(result["items"], expected["items"]):
                    self.assertEqual(actual["extra_attributes"], want["extra_attributes"])
                before = deepcopy(result)
                for _ in range(3):
                    result = normalize_result(result)
                self.assertEqual(result, before)

    def test_production_msg_service_orchestrator_and_virtual_sheet(self):
        """Actual MSG bytes, captured bad model output, real downstream code.

        Only model inference and its DB usage write are mocked. This proves
        the fix runs in production, independently of model wording/omissions.
        """
        for name, count in zip(HISTORY_FILES, (3, 2, 3)):
            with self.subTest(file=name):
                path = ROOT / "inputs" / name
                captured = json.loads((ROOT / "results" / "extraction" / (path.stem + ".actual.json")).read_text(encoding="utf-8"))
                service = object.__new__(GeminiExtractionService)
                service._generate_content_with_retry = Mock(return_value=SimpleNamespace(text=json.dumps(captured)))
                service._log_usage = Mock()
                orchestrator = ExtractionOrchestrator()
                orchestrator.gemini = service
                result = orchestrator.extract(SimpleUploadedFile(name, path.read_bytes()), name)
                service._generate_content_with_retry.assert_called_once()
                service._log_usage.assert_called_once()
                expected = json.loads((ROOT / "expected" / "extraction" / (path.stem + ".json")).read_text(encoding="utf-8"))
                self.assertTrue(compare_results(result, expected)["passed"], compare_results(result, expected)["differences"])
                self.assertEqual(result["_raw_extracted_count"], count)
                self.assertFalse(result["_gemini_needs_review"])
                sheet = gemini_result_to_virtual_sheet(deepcopy(result))
                self.assertEqual(len(sheet["rows"]) - 1, count)
                self.assertEqual(sheet["gemini_validation_warnings"], [])

    def test_existing_wuxi_source_history_is_complete_when_grammar_matches(self):
        for path in (ROOT / "inputs").glob("*Wuxi*.msg"):
            with self.subTest(file=path.name):
                history = extract_history(**metadata(path))
                if "26F1" in path.name:
                    self.assertIsNotNone(history)
                    self.assertEqual(len(history["items"]), 7)
                else:
                    self.assertIsNone(history)
