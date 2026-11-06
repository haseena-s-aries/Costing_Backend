from api.services.normalization import normalize_text

HEADER_TERMS = {
    "item", "code", "stock", "part", "product", "description", "material", "supplier", "vendor",
    "date", "qty", "quantity", "unit", "uom", "price", "rate", "cost", "currency", "curr", "fx",
    "po", "lpo", "grn", "quotation", "quote", "reference", "warehouse", "location", "remarks",
}

def _non_empty(row):
    return [cell for cell in row if cell not in (None, "")]

class HeaderDetectionService:
    def suggest_header_row(self, rows):
        best_row = 1
        best_score = -1
        for index, row in enumerate(rows[:20], start=1):
            cells = _non_empty(row)
            if len(cells) < 2:
                continue
            normalized = [normalize_text(cell) for cell in cells]
            text_cells = sum(1 for cell in cells if isinstance(cell, str))
            unique_cells = len(set(normalized))
            term_hits = sum(1 for value in normalized for token in value.split() if token in HEADER_TERMS)
            following = _non_empty(rows[index] if index < len(rows) else [])
            score = len(cells) * 2 + text_cells + unique_cells + term_hits * 4 + min(len(following), len(cells))
            if score > best_score:
                best_score = score
                best_row = index
        return best_row
