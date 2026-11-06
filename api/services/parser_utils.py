import re

SECTION_CONTINUATION_STOP_PATTERN = re.compile(
    r"\b(?:"
    r"no\.\s*item|bank\s*details|bank\s*name|account\s*no\.?|account\s*number|ifsc|swift\s*code|iban|"
    r"total\b|sub\s*total|subtotal|tax\s*summary|total\s+(?:pi\s+)?amount|amount\s+in\s+words|grand\s+total|"
    r"additional\s+notes|terms\s*&?\s*conditions|product\s+specifications|prices\s+and\s+validity|"
    r"order\s+confirmation|delivery\s+timeline|payment\s+terms|shipping\s+and\s+risk|packing\s+charges|"
    r"cancellation\s+and\s+modifications|returns\s+and\s+replacements|warranty|ownership\s+of\s+goods|"
    r"intellectual\s+property|confidentiality|force\s+majeure|technical\s+support|governing\s+law|"
    r"acceptance\s+of\s+terms"
    r")\b",
    re.I,
)

INVOICE_ITEM_ROW_PATTERN = re.compile(
    r"^\s*\d{1,4}\s+"
    r"(?P<body>.+?)\s+"
    r"(?P<code>[A-Z0-9][A-Z0-9./_-]{2,})\s+"
    r"\d+(?:\.\d+)?\s+"
    r"(?P<uom>no\.?s|nos|pcs?|ea|each|unit|set|lot|ls|pkt|roll|mtr|meter|kg|tin)\s+"
    r"\d[\d,]*(?:\.\d+)?\s+"
    r"\d[\d,]*(?:\.\d+)?\s+"
    r"\d[\d,]*(?:\.\d+)?\s*$",
    re.I,
)

def clean_spaces(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def strip_section_continuation(value):
    return SECTION_CONTINUATION_STOP_PATTERN.split(str(value or ""), maxsplit=1)[0].strip(" |:-")

def is_section_stop_line(value):
    return bool(SECTION_CONTINUATION_STOP_PATTERN.search(str(value or "")))