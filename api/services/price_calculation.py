"""Printed "unit price x quantity = total" calculations.

A line such as "8950 USD / Per Container = 8950 USD x 4 Nos = USD 35,800.00"
carries one priced item: unit price 8950, quantity 4, total 35,800. The
total is not a second item. A calculation is accepted only when the printed
unit price times the printed quantity equals the printed total.
"""
import re
from decimal import Decimal, InvalidOperation

CUR = r"(?:USD|AED|EUR|GBP|SGD|CNY|RMB|INR|SAR|QAR|KWD|OMR|BHD|JPY|AUD|CAD|US\$|\$|€|£)"
NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
CALCULATION = re.compile(
    rf"(?:{CUR}\s*)?(?P<unit>{NUM})\s*(?:{CUR})?\s*(?:/\s*-?\s*)?[x×*]\s*(?P<qty>\d+(?:\.\d+)?)\s*"
    rf"(?P<uom>nos?\.?|pcs?\.?|pieces?|units?|containers?|cntrs?|sets?|ea|lots?|trips?)?\s*"
    rf"=\s*(?:{CUR}\s*)?(?P<total>{NUM})", re.I)
UNIT_BASIS = re.compile(r"\b(?:per|/)\s*(container|cntr|unit|set|trip|shipment|piece|pc|box)\b", re.I)


def _decimal(value):
    try:
        return Decimal(str(value).replace(',', ''))
    except (InvalidOperation, ValueError):
        return None


def calculation(line):
    """Return {unit_price, quantity, uom, line_total} for a verified
    calculation printed in the line, else None."""
    for match in CALCULATION.finditer(line or ''):
        unit, qty, total = _decimal(match['unit']), _decimal(match['qty']), _decimal(match['total'])
        if not unit or not qty or not total or qty <= 1 or unit * qty != total:
            continue
        basis = UNIT_BASIS.search(line[:match.end()])
        uom = basis[1].title() if basis else (match['uom'] or '').rstrip('.').upper() or None
        return {'unit_price': str(unit), 'quantity': str(qty.normalize()) if qty == qty.to_integral() else str(qty),
                'uom': uom, 'line_total': str(total), 'start': match.start()}
    return None
