import re

def is_contact_only_supplier(value):
    """A telephone contact is not evidence of a seller's company name."""
    text = str(value or "").strip()
    text = re.sub(r"(?i)^(?:phone|telephone|tel|mobile|mob|fax|whatsapp|contact)(?:\s*(?:no\.?|number))?\s*[:.-]?\s*", "", text)
    text = re.sub(r"(?i)\s*(?:ext\.?|extension|x)\s*\d+\s*$", "", text)
    return bool(re.fullmatch(r"[+\d\s()./\-]+", text) and len(re.sub(r"\D", "", text)) >= 7)

def normalize_text(value):
    if value is None:
        return ""
    value = str(value).strip().lower()
    value = value.replace("_", " ").replace("/", " ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def normalize_supplier(value):
    value = normalize_text(value)
    value = re.sub(r"\b(and|co|company|corp|corporation)\b", "", value)
    value = re.sub(r"\b(llc|l l c|fze|fzco|fzc|ltd|limited|trading)\b", "", value)
    return re.sub(r"\s+", " ", value).strip()
