from rapidfuzz import fuzz

from api.constants import REQUIRED_PRODUCT_FIELDS, SYSTEM_FIELD_NAMES
from api.exceptions import ImportServiceError
from api.models import ImportBatchStatus, ImportColumnMapping
from api.services.normalization import normalize_text

SYNONYMS = {
    "item_code": ["item code", "itemcode", "stock no", "stock number", "material code", "product code", "itemcd"],
    "part_number": ["part number", "part no", "part no", "p n", "partno", "partno"],
    "product_name": ["product", "product name", "description", "item", "item description", "name type", "name", "material details", "material description", "material service description", "product description", "product_description"],
    "supplier": ["supplier", "supplier name", "vendor", "vendor name"],
    "purchase_date": ["purchase date", "po date", "quotation date", "invoice date", "received on", "date", "po dt"],
    "quantity": ["quantity", "qty", "q t t", "qtt", "q.t.t", "purchased qty", "accepted qty", "qty required"],
    "row_type": ["row type", "row_type", "type"],
    "specification": ["specification", "spec"],
    "material": ["material"],
    "tax": ["tax", "vat", "tax %", "vat %"],
    "line_total": ["line total", "total", "amount", "net amount", "extended price"],
    "document_number": ["document number", "document no", "doc no"],
    "document_date": ["document date", "doc date"],
    "payment_terms": ["payment terms"],
    "delivery_terms": ["delivery terms"],
    "grand_total": ["grand total"],
    "unit_of_measurement": ["unit", "uom", "unit of measurement"],
    "unit_price": ["unit price", "unit price usd", "unit price aed", "rate", "po rate", "quoted rate", "unit cost", "invoice unit price", "unitcost"],
    "currency": ["currency", "curr", "curr"],
    "exchange_rate": ["exchange rate", "fx", "fx rate"],
    "lpo_reference": ["lpo reference", "po reference", "po no", "po number", "po no"],
    "grn_reference": ["grn reference", "grn number"],
    "quotation_reference": ["quotation reference", "quotation ref", "quote number", "quote ref"],
    "purchase_location": ["location", "warehouse", "origin", "purchase location"],
    "remarks": ["remarks", "notes", "note", "comments"],
    "brand": ["brand", "make", "brand make"],
}
NORMALIZED_SYNONYMS = {field: {normalize_text(item) for item in values} for field, values in SYNONYMS.items()}
UNWANTED_MAPPING_FIELDS = {
    "tax",
    "tax_amount",
   
    "document_number",
    "payment_terms",
    "delivery_terms",
    "grand_total",
    "subtotal",
}

UNWANTED_HEADER_WORDS = {
    "tax",
    "vat",
    "si no",
    "sino",
    "sl no",
    "sr no",
    "serial",
    "serial no",
    "no",
}
class MappingService:
    def suggest(self, headers):
        return [self.suggest_one(index, header) for index, header in enumerate(headers)]

    def suggest_one(self, index, header):
        normalized = normalize_text(header)
        if normalized in UNWANTED_HEADER_WORDS:
            return self._result(index, header, "ignore", 100, "ignored_unwanted")
        for field, synonyms in NORMALIZED_SYNONYMS.items():
            if normalized in synonyms:
                return self._result(index, header, field, 100, "synonym")
        best = ("ignore", 0)
        for field, synonyms in NORMALIZED_SYNONYMS.items():
            score = max([fuzz.token_set_ratio(normalized, synonym) for synonym in synonyms] or [0])
            if score > best[1]:
                best = (field, score)
        if best[1] >= 85:
            return self._result(index, header, best[0], 80, "strong_token")
        if best[1] >= 65:
            return self._result(index, header, best[0], 60, "weak_token")
        return self._result(index, header, "ignore", 0, "unmatched")

    def _result(self, index, header, field, confidence, method):
        return {
            "uploaded_column_index": index,
            "uploaded_column_name": header,
            "suggested_system_field": field,
            "confidence": confidence,
            "match_method": method,
        }

    def save_mappings(self, batch, sheet, mappings, user=None):
        headers = [str(cell or "") for cell in sheet.cached_headers]
        seen = set()
        has_product = False
        has_price = False
        prepared = []
        for item in mappings:
            index = int(item["uploaded_column_index"])
            if index < 0 or index >= len(headers):
                raise ImportServiceError("Invalid column mapping.", {"uploaded_column_index": ["Column index does not exist."]})
            uploaded_name = item.get("uploaded_column_name") or ""
            if normalize_text(uploaded_name) != normalize_text(headers[index]):
                raise ImportServiceError("Invalid column mapping.", {"uploaded_column_name": ["Uploaded column name does not match the selected header."]})
            field = item.get("system_field_name") or "ignore"
            normalized_uploaded_name = normalize_text(uploaded_name)

            if field in UNWANTED_MAPPING_FIELDS or normalized_uploaded_name in UNWANTED_HEADER_WORDS:
                field = "ignore"

            ignored = bool(item.get("is_ignored")) or field == "ignore"
            if field not in SYSTEM_FIELD_NAMES:
                raise ImportServiceError("Invalid system field.", {"system_field_name": [field]})
            if not ignored:
                if field in seen:
                    raise ImportServiceError("Duplicate system field mapping.", {"system_field_name": [field]})
                seen.add(field)
                has_product = has_product or field in REQUIRED_PRODUCT_FIELDS
                has_price = has_price or field == "unit_price"
            prepared.append((index, uploaded_name, field, ignored))
        if not has_product:
            raise ImportServiceError("Product identifier mapping is required.", {"mappings": ["Map item_code, part_number or product_name."]})
        if not has_price:
            raise ImportServiceError("Unit price mapping is required.", {"unit_price": ["Map unit_price before preview."]})
        ImportColumnMapping.objects.filter(import_sheet=sheet).delete()
        objects = [
            ImportColumnMapping(
                import_batch=batch,
                import_sheet=sheet,
                uploaded_column_index=index,
                uploaded_column_name=name,
                system_field_name=field,
                is_ignored=ignored,
                is_required=field in REQUIRED_PRODUCT_FIELDS or field == "unit_price",
                created_by=user,
            )
            for index, name, field, ignored in prepared
        ]
        ImportColumnMapping.objects.bulk_create(objects)
        batch.status = ImportBatchStatus.MAPPING_PENDING
        batch.save(update_fields=["status", "updated_at"])
        return objects
