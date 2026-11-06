"""Exercise the real preview preparation, including its PDF branch."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.conf import settings as django_settings
from api.services.import_service import ImportService
from api.services.gemini_extraction_service import gemini_result_to_virtual_sheet


class Prepared(Exception):
    pass


class PreviewIdentityTests(SimpleTestCase):
    def test_finalized_pdf_identity_survives_cached_and_fresh_preview(self):
        item = dict(row_type="PRODUCT", product_name="Assembly (321-654)",
                    parent_category="Maintenance parts",
                    display_name="Maintenance parts | Assembly (321-654) | Material: Steel",
                    extra_attributes=[{"label": "Material", "value": "Steel"}],
                    quantity="2", uom="PC", unit_price="12", line_total="24",
                    currency="USD", supplier_name="Example Company Ltd",
                    purchase_date="2026-09-01")
        result = dict(document_type="email_quote", category="QUOTATION",
                      supplier_name=item["supplier_name"], document_date="2026-09-01",
                      currency="USD", items=[item])
        for cached in (True, False):
            with self.subTest(cached=cached):
                virtual = gemini_result_to_virtual_sheet(copy.deepcopy(result))
                sheet = SimpleNamespace(cached_rows=virtual["rows"] if cached else [],
                                        cached_headers=virtual["rows"][0] if cached else [])
                sheets = MagicMock()
                sheets.exists.return_value = True
                sheets.__iter__.side_effect = lambda: iter([sheet])
                batch = MagicMock()
                batch.sheets.filter.return_value = sheets
                batch.extraction_summary = {}
                batch.document_metadata = {k: v for k, v in result.items() if k != "items"}
                batch.category = "QUOTATION"
                batch.default_currency = "USD"
                batch.supplier_id = None
                batch.supplier_name_from_file = ""
                batch.uploaded_file.original_file_name = "generic.pdf"
                batch.uploaded_file.file.read.return_value = b"pdf source"
                batch.save.side_effect = Prepared
                service = ImportService()
                captured = []
                original = service._apply_gemini_result

                def capture(*args):
                    applied = original(*args)
                    captured.append(applied[0]["sheets"][0])
                    return applied

                damaged = dict(item, product_name="Assembly ( - )", display_name="Assembly ( - )", extra_attributes=[])
                with patch.object(django_settings, "GEMINI_API_KEY", "test"), \
                     patch("api.services.import_service.ImportRow") as rows, \
                     patch("api.services.import_service.ExtractionOrchestrator") as extractor, \
                     patch("api.services.import_service._extract_pdf_email_tariff_charge_items", return_value=[damaged]) as recovery, \
                     patch("api.services.import_service._extract_pdf_email_freight_charges", return_value=[]), \
                     patch.object(service, "_apply_gemini_result", side_effect=capture):
                    extractor.return_value.extract.return_value = copy.deepcopy(result)
                    with self.assertRaises(Prepared):
                        service.preview(batch, [1])
                    recovery.assert_not_called()
                    self.assertEqual(extractor.return_value.extract.call_count, 0 if cached else 1)
                output = dict(zip(captured[0]["rows"][0], captured[0]["rows"][1]))
                for field in ("parent_category", "product_name", "display_name", "extra_attributes"):
                    self.assertEqual(output[field], item[field])
