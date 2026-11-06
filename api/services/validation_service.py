import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser

from api.constants import REQUIRED_PRODUCT_FIELDS

def parse_decimal(value):
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    cleaned = re.sub(
        r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b|[$\u20ac\u00a3\u00a5]",
        "",
        str(value),
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()

    if (
        "," not in cleaned
        and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned)
        and not re.fullmatch(r"\d+\.\d{1,4}", cleaned)
    ):
        cleaned = cleaned.replace(".", "")
    else:
        cleaned = cleaned.replace(",", "")

    return Decimal(cleaned)
def parse_quantity_decimal(value):
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value

    cleaned = re.sub(
        r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD|JPY|CNY|SGD|AUD|CAD)\b|[$\u20ac\u00a3\u00a5]",
        "",
        str(value),
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=\d)\s+(?=[,.\d])", "", cleaned).strip()

    # Quantity: 1.000 means 1.000, not 1000.
    if re.fullmatch(r"\d+\.\d{1,4}", cleaned):
        return Decimal(cleaned)

    # Quantity: 1,000 means 1000.
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
        return Decimal(cleaned.replace(",", ""))

    # Quantity: 1,50 means 1.50.
    if re.fullmatch(r"\d+,\d{1,4}", cleaned):
        return Decimal(cleaned.replace(",", "."))

    return Decimal(cleaned.replace(",", ""))
def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)

    has_month_name = bool(re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", text, re.I))
    return date_parser.parse(text, dayfirst=not has_month_name).date()

class ValidationService:
    def validate(self, mapped, default_currency="", row_type="PRODUCT"):
        errors = []
        warnings = []
        if not any(mapped.get(field) for field in REQUIRED_PRODUCT_FIELDS):
            errors.append("At least one product identifier is required.")
        try:
            price = parse_decimal(mapped.get("unit_price"))
            if price is None:
                errors.append("Unit price is required.")
            elif price < 0:
                errors.append("Unit price cannot be negative.")
            mapped["unit_price"] = str(price) if price is not None else None
        except (InvalidOperation, ValueError):
            errors.append("Unit price must be numeric.")
        quantity = None
        try:
            quantity = parse_quantity_decimal(mapped.get("quantity")) if mapped.get("quantity") not in (None, "") else (None if mapped.get('_physical_pdf_row') else Decimal("1"))
            if quantity is not None and quantity < 0:
                errors.append("Quantity cannot be negative.")
                
            mapped["quantity"] = str(quantity) if quantity is not None else None
        except (InvalidOperation, ValueError):
            errors.append("Quantity must be numeric.")
        try:
            exchange_rate = parse_decimal(mapped.get("exchange_rate")) if mapped.get("exchange_rate") not in (None, "") else Decimal("1")
            if exchange_rate <= 0:
                errors.append("Exchange rate must be greater than zero.")
            mapped["exchange_rate"] = str(exchange_rate)
        except (InvalidOperation, ValueError):
            errors.append("Exchange rate must be numeric.")
        if mapped.get("purchase_date"):
            try:
                mapped["purchase_date"] = parse_date(mapped.get("purchase_date")).isoformat()
            except (ValueError, TypeError, OverflowError):
                warnings.append("Purchase date could not be parsed.")
                mapped["purchase_date"] = None
        if mapped.get("line_total") not in (None, ""):
            try:
                line_total = parse_decimal(mapped.get("line_total"))
                mapped["line_total"] = str(line_total) if line_total is not None else None
                if not mapped.get('_comparison_matrix_price') and line_total is not None and price is not None and quantity is not None:
                    expected = price * quantity
                    tolerance = max(Decimal("0.05"), abs(expected) * Decimal("0.02"))
                    if abs(line_total - expected) > tolerance:
                        warnings.append("Line total does not match quantity x unit price.")
            except (InvalidOperation, ValueError):
                warnings.append("Line total must be numeric.")
                mapped["line_total"] = None
        # Costing base currency is not evidence of the quoted source currency.
        currency = (mapped.get("currency") or default_currency or "").strip().upper()
        if currency in {"DHS", "DH", "DIRHAM", "DIRHAMS"}:
            currency = "AED"
        if not currency:
            errors.append("Currency missing from source document.")
        elif not re.match(r"^[A-Z]{3}$", currency):
            errors.append("Currency must be a valid 3-letter code.")
        mapped["currency"] = currency
        status = "failed" if errors else ("warning" if warnings else "valid")
        return status, errors, warnings, mapped
