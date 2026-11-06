import re

CANONICAL_ALIASES = {
    "product_name": ["description", "item description", "product", "product description", "product details", "material", "material description", "item", "item name", "particulars", "details", "cable"],
    "quantity": ["qty", "quantity", "qnty", "quan"],
    "uom": ["unit", "uom", "u/m", "unit of measure", "unit of measurement"],
    "unit_price": ["rate", "price", "unit price", "unit rate", "rate each", "price each", "up", "u.p.", "u p", "u/prc", "u.prc", "u prc"],
    "line_total": ["amount", "total", "total amount", "line total", "value", "net value", "total price", "amt", "amt.", "tp"],
    "item_code": ["item code", "code", "product code", "material code"],
    "part_number": ["part number", "part no", "part no.", "p/n", "pn"],
    "remark": ["remark", "remarks", "notes", "note"],
}

def normalize_header(value):
    value = str(value or "").lower().replace("_", " ")
    # Currency decorations belong to commercial headers, not their identity.
    value = re.sub(r"\s*\([a-z]{3}\)\s*$", "", value) if re.search(r"\b(price|rate|amount|total|value)\b", value) else value
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()

def aliases_for(field, profile=None):
    values = list(CANONICAL_ALIASES.get(field, []))
    if profile:
        values.extend((profile.header_aliases or {}).get(field, []))
    return {normalize_header(value) for value in values}
