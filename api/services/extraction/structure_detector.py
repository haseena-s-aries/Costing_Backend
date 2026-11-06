from dataclasses import dataclass
from .field_aliases import aliases_for, normalize_header

@dataclass
class StructureMatch:
    structure: str
    confidence: float
    reasons: list

class StructureDetector:
    STANDARD_TABLE = "STANDARD_TABLE"
    PRICE_MATRIX = "PRICE_MATRIX"
    MULTI_SUPPLIER_TABLE = "MULTI_SUPPLIER_TABLE"
    KEY_VALUE = "KEY_VALUE"
    UNKNOWN = "UNKNOWN"

    def detect(self, document):
        rows = document.get("rows") or []
        if document.get("file_type") != "spreadsheet" or not rows:
            return StructureMatch(self.UNKNOWN, 0.0, ["not_a_spreadsheet"])
        for row in rows[:30]:
            headers = [normalize_header(v) for v in row["values"]]
            if any("route" in h for h in headers) and sum(any(x in h for x in ("ton", "kg", "hr", "hour")) for h in headers) >= 2:
                return StructureMatch(self.PRICE_MATRIX, 0.95, ["tier_or_route_columns"])
            matches = sum(header in aliases_for(field) for header in headers for field in ("product_name", "quantity", "uom", "unit_price", "line_total"))
            if matches >= 2:
                return StructureMatch(self.STANDARD_TABLE, min(0.95, 0.55 + matches * 0.08), ["semantic_headers"])
        return StructureMatch(self.UNKNOWN, 0.25, ["ambiguous_headers"])
