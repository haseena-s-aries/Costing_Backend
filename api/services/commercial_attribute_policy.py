"""Source column semantics shared by extraction, preview and import display."""
import re
import unicodedata


def commercial_field(label):
    # Compact only the header for matching, never the source value/product.
    text = unicodedata.normalize('NFKC', str(label or '')).casefold()
    text = re.sub(r'\b(?:aed|usd|eur|gbp|cny|rmb|jpy|inr|sar|qar|kwd|omr|bhd|sgd|aud|cad)\b', '', text)
    key = re.sub(r'[^a-z]', '', text)
    if re.fullmatch(r'(?:vat|tax|gst)(?:amount|rate|percentage|percent)?', key):
        return 'tax'
    if re.fullmatch(r'(?:unit)?(?:price|rate)(?:(?:excl|incl|excluding|including|netof)(?:vat|tax))?', key):
        return 'unit_price'
    if key in {'amount', 'amoun', 'total', 'totalprice', 'totalamount', 'linetotal', 'lineamount',
               'extendedprice', 'extendedamount', 'netamount', 'grossamount', 'subtotal', 'grandtotal'}:
        return 'line_total'
    if key in {'quantity', 'qty'}:
        return 'quantity'
    if key in {'uom', 'unit', 'units', 'unitofmeasurement', 'unitofmeasure', 'chargeper'}:
        return 'uom'
    if key in {'discount', 'disc', 'dis', 'discountpercentage', 'discountamount'}:
        return 'discount'
    if key == 'currency':
        return 'currency'
    return None


# Shipment / document reference fields (invoice headers, bills of lading)
# describe the whole document, not the item; keep them as data but out of
# the item description.
SHIPMENT_REFERENCE = re.compile(
    r'^(?:pcs|pieces|no\.? of (?:pcs|pieces|packages)|packages|weight|gross weight|net weight|volume|cbm|'
    r'pol|pod|fdc|port of (?:loading|discharge)|final destination|salesman(?: name)?|sales ?person|'
    r'carrier|voyage(?: ?/ ?flight)?(?: no\.?)?|flight(?: no\.?)?|obl|hbl|mbl|awb|bl no\.?|b ?/ ?l(?: no\.?)?|'
    r'commodity|roe|rate of exchange|exchange rate|job no\.?|container(?: no\.?| number)?|'
    r'shipper(?: name| ref(?:erence)? no\.?)?|consignee(?: name)?|vessel(?: name)?|'
    r'etd|eta|ets|t ?/ ?t|transit(?: time)?|free time|laycan|cut ?off(?: date)?)$')


def hidden_display_attribute(label):
    """Display policy only; historical prices must not become unit-price columns."""
    text = unicodedata.normalize('NFKC', str(label or '')).casefold()
    return bool(commercial_field(label) or re.search(
        r'\b(?:price|cost|budget|discount|hike|amount)\b', text
    ) or SHIPMENT_REFERENCE.match(re.sub(r'\s+', ' ', text).strip(' :')))


def clean_commercial_display(item):
    """Remove labelled commercial suffixes without touching same-cell prose."""
    for field in ('product_name', 'display_name', 'description'):
        value = item.get(field)
        if not isinstance(value, str):
            continue
        parts = re.split(r'\s*\|\s*', value)
        item[field] = ' | '.join(re.sub(r'\s+[-–—]+\s*$', '', p) for p in parts if not (
            ':' in p and hidden_display_attribute(p.split(':', 1)[0])
        ))
    return item
