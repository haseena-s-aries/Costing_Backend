import re
import unicodedata
from decimal import Decimal, InvalidOperation

FINANCIAL_IDENTITY_LABELS = {"vat", "vat %", "vat amount", "tax", "tax %", "gst", "discount", "discount %", "subtotal", "total", "total amount", "grand total", "net amount", "line amount", "extended amount"}

def sanitize_product_identity(value):
    from api.services.commercial_attribute_policy import hidden_display_attribute as commercial_field
    financial = re.compile(r"^(?:VAT(?:\s*%|\s+Amount)?|Tax(?:\s*%|\s+Amount)?|GST|Discount(?:\s*%)?|Subtotal|Total|Grand\s+Total|Net\s+Amount|Line\s+Amount|Extended\s+Amount)\s*:", re.I)
    segments = []
    for segment in re.split(r"\s*\|\s*", str(value or "")):
        segment = re.sub(r"\s+", " ", segment).strip(" |:-")
        if segment and not financial.match(segment) and not (':' in segment and commercial_field(segment.split(':', 1)[0])):
            segments.append(segment)
    return " | ".join(segments)

def clean_display_identity(value):
    from api.services.commercial_attribute_policy import hidden_display_attribute as commercial_field
    text = str(value or "").strip()
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if not parts:
        return ""
    hidden_labels = {
        "sl no", "s no", "sr no", "serial no", "serial number",
        "line no", "line number", "row no", "row number", "vat",
        "vat amount", "tax", "tax amount", "gst", "discount", "discount percentage", "dis", "subtotal",
        "total", "total amount", "grand total", "net amount", "line amount",
        "extended amount", "dely period", "delivery period", "delivery",
        "delivery time", "lead time", "leadtime", "lead period",
    }
    output = [parts[0]]
    for part in parts[1:]:
        if ":" not in part:
            output.append(part)
            continue
        label, _ = part.split(":", 1)
        normalized_label = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        if not commercial_field(label) and normalized_label not in hidden_labels:
            output.append(part)
    return " | ".join(output)

def rate_matrix_display_name(item, siblings):
    """Return a tier-qualified display name only for repeated SERVICE tiers."""
    item = item or {}
    if str(item.get("row_type") or "").upper() != "SERVICE":
        return None
    uom = str(item.get("uom") or item.get("unit_of_measurement") or "").strip().upper()
    if uom not in {"TON", "T", "KG", "MT"}:
        return None
    name = str(item.get("product_name") or item.get("description") or "").strip()
    quantity_text = str(item.get("quantity") or "").strip()
    if not name or not quantity_text:
        return None
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:TON|TONS|T)\b", name, re.I):
        return name
    try:
        quantity = Decimal(quantity_text)
    except (InvalidOperation, ValueError):
        return None
    comparable = []
    for sibling in siblings or []:
        if str(sibling.get("row_type") or "").upper() != "SERVICE":
            continue
        sibling_uom = str(sibling.get("uom") or sibling.get("unit_of_measurement") or "").strip().upper()
        if sibling_uom != uom or normalize_product_text(sibling.get("product_name") or sibling.get("description")) != normalize_product_text(name):
            continue
        try:
            sibling_quantity = Decimal(str(sibling.get("quantity") or "").strip())
        except (InvalidOperation, ValueError):
            continue
        comparable.append((sibling, sibling_quantity))
    quantities = {value for _, value in comparable}
    is_transport_route = bool(re.search(r"\bto\b", name, re.I))
    if not is_transport_route and (len(comparable) < 2 or len(quantities) < 2):
        return None
    try:
        if uom == "TON" and quantity == quantity.to_integral_value():
            tier = f"{int(quantity):02d} TON" if quantity < 10 else f"{int(quantity)} TON"
        else:
            tier = f"{quantity_text} {uom}".strip()
    except (ValueError, TypeError):
        return None
    return f"{name} | {tier}"

def normalize_product_text(value):
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", sanitize_product_identity(value)).strip().lower()
    value = value.replace("–", "-").replace("—", "-").replace("/", " ")
    value = re.sub(r"\bmm\s*(2|²)\b", "sqmm", value)
    value = re.sub(r"\bsq\s*mm\b", "sqmm", value)
    value = re.sub(r"\bsqmm\b", "sqmm", value)
    value = re.sub(r"\b(\d+)\s*core\b", r"\1c", value)
    value = re.sub(r"\b(\d+)\s*-\s*core\b", r"\1c", value)
    value = re.sub(r"\barmou?red\b", "swa", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value

def normalize_code(value):
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).strip().lower()
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"[^a-z0-9-]+", "", value)
    return value

def group_key(mapped):
    item_code = normalize_code(mapped.get("item_code"))
    part_number = normalize_code(mapped.get("part_number"))
    brand = normalize_product_text(mapped.get("brand"))
    description = normalize_product_text(product_identity_text(mapped))
    if item_code:
        return f"item_code:{item_code}"
    if part_number:
        return f"part_number:{part_number}"
    if brand and part_number:
        return f"brand_part:{brand}:{part_number}"
    if description:
        return f"description:{description}"
    return ""

def canonical_name(rows):
    names = [
        product_identity_text(row)
        for row in rows
        if product_identity_text(row)
    ]
    if not names:
        return ""
    return sorted(names, key=lambda item: (len(normalize_product_text(item).split()), len(item)), reverse=True)[0]

def most_common(rows, field):
    values = [str(row.get(field) or "").strip() for row in rows if row.get(field)]
    if not values:
        return ""
    return max(set(values), key=values.count)


def line_item_group_key(mapped, row_type="PRODUCT"):
    normalized_type = str(row_type or mapped.get("row_type") or "PRODUCT").upper()
    key = group_key(mapped)
    if key:
        return f"{normalized_type}:{key}"
    description = normalize_product_text(product_identity_text(mapped) or mapped.get("description"))
    return f"{normalized_type}:description:{description}" if description else ""


def product_identity_text(mapped):
    mapped = mapped or {}
    parts = []
    product_name = sanitize_product_identity(mapped.get("product_name"))

    if product_name:
        parts.append(product_name)

    for field in ("brand", "model", "specification", "material"):
        value = sanitize_product_identity(mapped.get(field))

        if not value:
            continue

        if normalize_product_text(value) in normalize_product_text(" ".join(parts)):
            continue

        parts.append(value)

    for attr in mapped.get("extra_attributes") or []:
        if not isinstance(attr, dict):
            continue

        label = str(attr.get("label") or "").strip(" |:-")
        if normalize_product_text(label) in {normalize_product_text(x) for x in FINANCIAL_IDENTITY_LABELS}:
            continue
        value = str(attr.get("value") or "").strip(" |:-")

        if not value:
            continue

        attr_text = f"{label} {value}" if label else value

        if normalize_product_text(attr_text) in normalize_product_text(" ".join(parts)):
            continue

        parts.append(attr_text)

    return " | ".join(parts)
