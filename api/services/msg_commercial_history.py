"""Source-backed email history parsing. No filename or supplier-specific rules."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


MONEY = r"(?:JP¥|CN¥|RMB|元|¥|￥|AED|USD|SGD|EUR|GBP|CNY|INR|SAR|QAR|Dhs|US\$)"
NUMBER = r"\d[\d,.]*\d|\d"
AMOUNT = re.compile(rf"\b(?P<currency>{MONEY})\s*(?P<amount>{NUMBER})", re.I)
AMOUNT_AFTER = re.compile(rf"\b(?P<amount>{NUMBER})\s*(?P<currency>{MONEY})\b", re.I)
PRICE = re.compile(rf"\b(?:unit\s+price|price|rate|total\s+cost)\s*[:=-]?\s*(?:{MONEY}\s*)?(?P<amount>{NUMBER})", re.I)
LEGAL_COMPANY = re.compile(r"\b(?:L\.?L\.?C\.?|PTE\s+LTD\.?|CO\s*[;,.]?\s*LTD\.?|LIMITED|LTD\.?|INC\.?|CORPORATION)\s*$", re.I)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_source_description(value):
    """Join wrapped source description lines without creating fake columns."""
    text = clean(value)
    text = re.sub(r"\s*:\s*\|\s*", ": ", text)
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r",\.+", ",", text)
    text = re.sub(r",(?=[a-z])", ", ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text.rstrip(" ,.;")


def email_date(value):
    """Parse email headers without borrowing a date from the following body."""
    text = clean(value)
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if iso:
        try:
            return datetime.strptime(iso.group(), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    for token in re.findall(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b", text):
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(token.replace(",", ""), fmt).date().isoformat()
            except ValueError:
                pass
    return None


def number(value):
    """Accept unambiguous comma decimals and ordinary comma thousands."""
    text = clean(value).rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        return str(Decimal(text))
    except InvalidOperation:
        return None


def currency(value):
    value = clean(value).upper()
    return {
        "DHS": "AED", "US$": "USD", "JP¥": "JPY", "JPY": "JPY",
        "CN¥": "CNY", "RMB": "CNY", "元": "CNY", "CNY": "CNY",
        "¥": None, "￥": None,
    }.get(value, value) or None


def plausible_company(value):
    """A company label must not be a fragment of security/contract prose."""
    text = clean(value)
    return bool(text and len(text) < 140
                and not re.match(r'^(?:from|to|cc|bcc|subject|sent|date|customer|buyer)\s*:', text, re.I)
                and not re.search(
        r'\b(?:this email|originated|including but not limited|discretion|'
        r'additional costs|arising|confidential|disclaimer|recipient|'
        r'shall|liability|liable|not responsible|do not click|subject to)\b', text, re.I))


def signature_company(body):
    lines = [clean(line) for line in body.splitlines() if clean(line)]
    # Only signatures count: ignore brands, links, addresses and request cargo.
    # Abbreviated sign-offs ("Tks&B.rgds!", "B.Rgds", "Rgds") are sign-offs too.
    signature_start = next((i for i, line in enumerate(lines) if re.fullmatch(
        r"(?:(?:thanks|tks|thx|many\s+thanks)\s*(?:&|and)\s*)?(?:(?:kind|best|warm|b)[\s.]*)?(?:regards|rgds)[,!.]*"
        r"|(?:yours )?sincerely[,!.]?", line, re.I)), None)
    signature = lines[signature_start + 1:] if signature_start is not None else lines
    # A signature ends at the next quoted message header; a company printed
    # in the following (quoted) email is not this author's company.
    if signature_start is not None:
        end = next((i for i, line in enumerate(signature)
                    if re.match(r"^(?:From|Sent|Date|To)\s*:", line, re.I)), len(signature))
        signature = signature[:end]
    for line in signature:
        if not plausible_company(line):
            continue
        if re.match(r"^From\s+(?!:).+", line, re.I):
            return re.sub(r"^From\s+", "", line, flags=re.I).strip()
    for line in signature:
        if not plausible_company(line):
            continue
        if any(token in line.lower() for token in ("http", "www.", "mailto:", "@")):
            continue
        if LEGAL_COMPANY.search(line) and len(line) < 140:
            return line.rsplit(" / ", 1)[-1].strip()
        # Non-incorporated trading names in a signature may end in Agency.
        # Require the signature boundary; never treat cargo/body text as seller.
        company = re.match(r"^([A-Za-z][\w &'().-]*\bAgency)(?:,|$)", line, re.I)
        if signature_start is not None and company:
            return company[1].strip()
    # A signature may contain a logo rather than a plain company-name line.
    # Use a company-profile slug only when its spelling is independently
    # corroborated by the corporate website in that same signature.
    if signature_start is not None:
        text = '\n'.join(signature)
        slugs = re.findall(r'https?://(?:www\.)?linkedin\.com/company/([a-z0-9-]+)/', text, re.I)
        hosts = re.findall(r'(?:https?://|www\.)(?:www\.)?([a-z0-9-]+)\.[a-z.]+', text, re.I)
        names = {slug.replace('-', ' ').title() for slug in slugs
                 if '-' in slug and slug.replace('-', '').lower() in
                 {host.replace('-', '').lower() for host in hosts}}
        if len(names) == 1:
            return names.pop()
    # Unsuffixed trading names are acceptable only if printed in the body
    # and independently corroborated by a corporate domain in that block.
    hosts = re.findall(r'(?:@|https?://(?:www\.)?|www\.)([a-z0-9-]+)\.[a-z.]+', body, re.I)
    for line in signature:
        if not plausible_company(line) or not re.fullmatch(r"[A-Za-z][A-Za-z &.-]{2,80}", line):
            continue
        words = line.split()
        for size in range(2, min(4, len(words)) + 1):
            prefix = re.sub(r'[^a-z]', '', ''.join(words[:size]).lower())
            if len(prefix) >= 6 and prefix in {h.lower().replace('-', '') for h in hosts}:
                return line
    # Company profile is explicit naming evidence, unlike inventing a name
    # from a domain. Require the brand prefix to match a corporate domain.
    for slug in re.findall(r'linkedin\.com/company/([a-z0-9-]+)/', body, re.I):
        words = slug.split('-')
        prefix = ''.join(w for w in words if w.lower() not in {'group', 'company', 'llc', 'ltd'})
        if len(prefix) >= 6 and any(h.replace('-', '').lower().startswith(prefix.lower()) for h in hosts):
            return ' '.join(words).title()
    return None


def split_thread(body, sender=None, sent_date=None):
    """Keep a complete From/Sent/To/Subject cluster with its own body."""
    lines = [line.strip().lstrip("> ") for line in str(body or "").splitlines() if line.strip()]
    starts = [0]
    for i, line in enumerate(lines):
        if i == 0:
            continue
        is_from = bool(re.match(r"^From\s*:", line, re.I))
        is_reply = bool(re.match(r"^On\s+.+\bwrote\s*:\s*$", line, re.I))
        is_date = bool(re.match(r"^(?:Sent|Date)\s*:", line, re.I))
        # A Date/Sent belongs to the adjacent header cluster, never its own
        # block when immediately preceded by another header.
        preceding_header = bool(re.match(r"^(?:From|To|Cc|Sent|Date|Subject)\s*:", lines[i - 1], re.I))
        if is_from or is_reply or (is_date and not preceding_header):
            starts.append(i)
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        segment = lines[start:end]
        headers, offset = {}, 0
        reply = re.match(r"^On\s+(.+?)\s+wrote\s*:\s*$", segment[0], re.I)
        if reply:
            headers["date"] = reply.group(1)
            headers["from"] = reply.group(1)
            offset = 1
        while offset < len(segment):
            header = re.match(r"^(From|To|Cc|Sent|Date|Subject)\s*:\s*(.*)", segment[offset], re.I)
            if not header:
                break
            headers[header.group(1).lower()] = header.group(2)
            offset += 1
        content = "\n".join(segment[offset:])
        company = signature_company(content)
        blocks.append({
            "id": f"email:{index}", "text": "\n".join(segment), "body": content,
            "sender": headers.get("from") or (sender if index == 0 else None),
            "date": email_date(headers.get("sent") or headers.get("date") or (sent_date if index == 0 else None)),
            "supplier": company,
        })
    # A missing signature name can inherit an unambiguous company only from
    # the same sender domain, not from a customer elsewhere in the thread.
    def domain(block):
        addresses = re.findall(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})", str(block.get('sender') or ''))
        if not addresses:
            signature = re.split(r"(?i)(?:thanks\s*(?:&|and)\s*)?regards", block['body'])
            if len(signature) > 1:
                addresses = re.findall(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})", signature[-1])
        domains = {value.lower() for value in addresses}
        return next(iter(domains)) if len(domains) == 1 else None

    companies = {}
    for block in blocks:
        key = domain(block)
        if key and block['supplier']:
            companies.setdefault(key, set()).add(block['supplier'])
    for block in blocks:
        names = companies.get(domain(block), set())
        if not block['supplier'] and len(names) == 1:
            block['supplier'] = next(iter(names))
    return blocks


def commercial_body(block):
    """Exclude signatures/disclaimers from commercial evidence."""
    lines = block["body"].splitlines()
    end = next((i for i, line in enumerate(lines) if re.match(
        r"^(?:(?:kind |best |warm )?regards\b|disclaimer\b|all business is transacted\b)", line, re.I)), len(lines))
    return "\n".join(lines[:end])


def is_enquiry(block):
    text = commercial_body(block)
    # Ownership is determined from the author's opening request, not currency
    # copied below it. A buyer quoting a previous price is still an enquiry.
    opening = re.split(r"(?m)^\d+[).]\s|\bCARGO DETAILS\b", text, maxsplit=1)[0]
    if re.search(
        r"\b(?:please|kindly)\s+(?:quote|share|advise|confirm|provide|send|proceed)\b"
        r"|\bcan you\s+(?:share|quote|send)\b"
        r"|\b(?:proceed with|arrange)\s+(?:the\s+)?shipment\b"
        r"|\bneeds?\s+to\s+be\s+delivered\b",
        opening,
        re.I,
    ):
        return True
    return False


def context_image_ids(html_body):
    """Identify inline images belonging to a buyer's enquiry in the thread.

    Other attachments remain protected, including supplier quotation images.
    The HTML is read as text only; it is never executed or fetched.
    """
    class SourceHTML(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_starttag(self, tag, attrs):
            if tag == "br":
                self.parts.append("\n")
            if tag == "img":
                src = dict(attrs).get("src", "")
                if src.lower().startswith("cid:"):
                    self.parts.append(f" [[INLINE:{src[4:]}]] ")

        def handle_endtag(self, tag):
            if tag in {"p", "div", "tr", "li"}:
                self.parts.append("\n")

    parser = SourceHTML()
    parser.feed(html_body.decode("utf-8", errors="replace") if isinstance(html_body, bytes) else str(html_body or ""))
    result = set()
    for block in split_thread("".join(parser.parts)):
        if is_enquiry(block):
            result.update(re.findall(r"\[\[INLINE:([^\]]+)\]\]", block["body"]))
    return result


def is_commercial(block):
    text = commercial_body(block)
    opening = re.split(r"(?m)^\d+[).]\s|\bCARGO DETAILS\b", text, maxsplit=1)[0]
    if is_enquiry(block):
        if not re.search(r"\b(?:proceed with|arrange)\s+(?:the\s+)?shipment\b|\bproceed\s+with\b", text, re.I):
            return False
    if re.search(r"\b(?:still working|will (?:check|revert)|revert .*soon)\b", opening, re.I) and not AMOUNT.search(text):
        return False
    return bool(AMOUNT.search(text) or AMOUNT_AFTER.search(text) or PRICE.search(text))


def _base(block, name, rate, code, qty=None, uom=None, total=None, row_type="PRODUCT", attrs=None):
    return {"row_type": row_type, "product_name": name, "display_name": name,
            "quantity": qty, "uom": uom, "unit_price": rate, "line_total": total,
            "currency": code, "purchase_date": block["date"], "supplier_name": block["supplier"],
            "page_number": None, "extra_attributes": attrs or [],
            "source_block_id": block["id"], "source_row_id": f'{block["id"]}:row:0',
            "source_reference": block["id"], "source_context": block["text"],
            "_msg_source_verified": True}


def _numbered_items(block, code):
    text = commercial_body(block)
    starts = list(re.finditer(r"(?m)^\s*\d+[).]\s+", text))
    rows = []
    for index, start in enumerate(starts):
        section = text[start.end():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        lines = section.splitlines()
        price_index = next((i for i, line in enumerate(lines) if AMOUNT.search(line) or PRICE.search(line)), None)
        if price_index is None:
            return None
        price_line = lines[price_index]
        # More than one explicit quote in this numbered item requires the
        # general extractor; selecting the first would lose a price tier.
        quoted_lines = [line for line in lines if AMOUNT.search(line) or PRICE.search(line)]
        if len(quoted_lines) != 1:
            return None
        match = AMOUNT.search(price_line) or PRICE.search(price_line)
        rate = number(match.group("amount"))
        local_currency = currency(match.groupdict().get("currency")) or code
        qty = re.search(r"\b(?:QTY|Quantity)\s*[:=-]?\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)?", section, re.I)
        rate_uom = re.search(r"/\s*(PC|PCS?|EA|NO)\b", price_line, re.I)
        description = [clean(line) for line in lines[:price_index]
                       if clean(line) and not re.match(r"^(?:QTY|Quantity)\b", line, re.I)]
        name = clean_source_description(" | ".join(description))
        # Do not reinterpret inline prices or broad technical paragraphs here.
        if not name or not rate or not local_currency:
            return None
        # A slash UOM (e.g. /pc) describes the rate basis, not a purchase
        # quantity.  Quantity is populated only from an explicit QTY field.
        quantity = qty.group(1) if qty else None
        uom = qty.group(2) if qty else (rate_uom.group(1).upper() if rate_uom else None)
        explicit_total = re.search(
            rf"(?:=|\b(?:total|amount|amt)\b)\s*(?:{MONEY}\s*)?({NUMBER})",
            section,
            re.I,
        )
        total = number(explicit_total.group(1)) if explicit_total else None
        row = _base(block, name, rate, local_currency, quantity, uom, total)
        row["source_row_id"] = f'{block["id"]}:row:{index}'
        rows.append(row)
    return rows or None


def _vertical_table(block, code):
    lines = [clean(line) for line in commercial_body(block).splitlines() if clean(line)]
    labels = [line.upper() for line in lines]
    # This is a structural schema: description/rate/qty/charges columns.
    try:
        start = next(i for i in range(len(labels) - 3) if labels[i:i + 4] == ["DESCRIPTION", "RATE", "QTY", "CHARGES"])
    except StopIteration:
        return None
    attrs = []
    if "CARGO DETAILS" in labels[:start]:
        metadata = lines[labels.index("CARGO DETAILS") + 1:start - 1]
        if len(metadata) % 2:
            return None
        attrs = [{"label": metadata[i], "value": metadata[i + 1]} for i in range(0, len(metadata), 2)]
    rows, i = [], start + 4
    while i < len(lines) and re.fullmatch(r"\d+", lines[i]):
        if i + 4 >= len(lines):
            return None
        name, rate_text, qty_text, total_text = lines[i + 1:i + 5]
        rate_match, total_match = AMOUNT.search(rate_text), AMOUNT.search(total_text)
        if not rate_match or not total_match or number(qty_text) is None:
            return None
        local_currency = currency(rate_match.group("currency"))
        if local_currency != currency(total_match.group("currency")):
            return None
        # Unit derives from an explicitly labelled weight, never a guess.
        weight = next((a for a in attrs if re.fullmatch(r"WEIGHT IN (KG|KGS)", a["label"], re.I)), None)
        uom = "KG" if weight and number(weight["value"]) == number(qty_text) else None
        row = _base(block, name, number(rate_match.group("amount")), local_currency,
                    number(qty_text), uom, number(total_match.group("amount")), "CHARGE", attrs)
        row["source_row_id"] = f'{block["id"]}:row:{lines[i]}'
        row["source_category"] = "Freight" if attrs else "CHARGE"
        rows.append(row)
        i += 5
    return rows or None


def _service_offer(block, code, preceding):
    text = commercial_body(block)
    heading = re.search(r"\b(?:offer|quotation)\s+for\s+(?:the\s+)?([^:\n]+):", text, re.I)
    fixed = re.search(rf"\b(?:total\s+cost\s*[:=-]?|revised\s+price\s+is)\s*(?P<currency>{MONEY})\s*(?P<amount>{NUMBER})", text, re.I)
    if not fixed:
        fixed = re.search(rf"\b(?P<description>[^\n:]+?)\s*:\s*(?:delivery\s+cost|cost)\s*[:=-]?\s*(?P<currency>{MONEY})\s*(?P<amount>{NUMBER})", text, re.I)
    if not fixed:
        fixed = re.search(rf"\bdelivery\s+cost\s*[:=-]?\s*(?P<currency>{MONEY})\s*(?P<amount>{NUMBER})", text, re.I)
    if not fixed:
        fixed = re.search(rf"\b(?:airfreight|seafreight|freight)\b[^\n]*?\bfor\s+(?P<amount>{NUMBER})\s*(?P<currency>{MONEY})", text, re.I)
    if not fixed:
        return None
    name = clean(heading.group(1)) if heading else clean(fixed.group("description")) if fixed and fixed.groupdict().get("description") else None
    if not name and fixed and re.search(r"\bdelivery\s+cost\b", text, re.I):
        before_cost = re.search(r"(?:and\s+)?([^\n]+?)\s*\n\s*delivery\s+cost", text, re.I)
        name = clean_source_description(before_cost.group(1)) if before_cost else None
    if not name and fixed:
        service = re.search(r"\bvia\s+(?:the\s+)?([A-Za-z]+)\s+for\s+\d", text, re.I)
        if service:
            name = f"{clean(service.group(1))} for motors"
    if not name:
        # A revised offer may refer back to one previous service by this seller.
        names = {row["product_name"] for row in preceding if row["row_type"] == "SERVICE" and row["supplier_name"] == block["supplier"]}
        if len(names) != 1:
            return None
        name = names.pop()
    name = clean_source_description(name[:1].upper() + name[1:])
    duration = re.search(r"\bmaximum duration of\s+\d+\s+\w+", text, re.I)
    base_name = name
    if duration:
        name += " for a " + duration.group().lower()
    local_currency = currency(fixed.group("currency")) or code
    rate = number(fixed.group("amount"))
    rows = [_base(block, name, rate, local_currency, total=rate, row_type="SERVICE")]
    transit = re.search(r"\btransit\s+time\s*[:=-]?\s*([^\n]+)", text, re.I)
    if transit:
        rows[0]["extra_attributes"] = [{"label": "transit time", "value": clean(transit.group(1).rstrip(".;"))}]
        rows[0]["display_name"] = f"{rows[0]['product_name']} | transit time: {rows[0]['extra_attributes'][0]['value']}"
    # Only an explicit future additional-period rate; no quantity or total.
    extra = re.search(rf"\badditional\s+(\w+)\s+will\s+be\s+charged\s+at\s+({MONEY})\s*({NUMBER})\s+per\s+(\w+)", text, re.I)
    if extra:
        row = _base(block, f"Additional {extra.group(1).lower()} for {base_name.lower()}",
                    number(extra.group(3)), currency(extra.group(2)), uom=extra.group(4).lower(), row_type="SERVICE")
        row["source_row_id"] = f'{block["id"]}:row:1'
        rows.append(row)
    # Refuse partial recovery when another unexplained quoted amount exists.
    quoted = {number(m.group("amount")) for m in AMOUNT.finditer(text)}
    if quoted - {row["unit_price"] for row in rows}:
        return None
    for row in rows:
        row["source_category"] = "Service"
    return rows


def extract_history(body, sender=None, sent_date=None):
    """Return a complete, auditable multi-offer history, or None for fallback.

    Narrow grammars preserve explicit table/numbered/prose values. An unknown
    block makes the entire result fall back; partial parsing never replaces
    already extracted rows.
    """
    blocks = split_thread(body, sender, sent_date)
    candidates = [block for block in blocks if is_commercial(block)]
    supplier_counts = {}
    for block in candidates:
        if block["supplier"]:
            supplier_counts[block["supplier"]] = supplier_counts.get(block["supplier"], 0) + 1
    if supplier_counts:
        primary_supplier = max(supplier_counts, key=supplier_counts.get)
        for block in candidates:
            if not block["supplier"] or (is_enquiry(block) and re.search(r"\bproceed with\b", block["body"], re.I)):
                block["supplier"] = primary_supplier
    if len(candidates) < 2 or any(not b["date"] or not b["supplier"] for b in candidates):
        return None
    supplier_currencies = {}
    for block in candidates:
        codes = {currency(m.group("currency")) for m in AMOUNT.finditer(commercial_body(block))}
        supplier_currencies.setdefault(block["supplier"], set()).update(codes)
    rows, evidence = [], []
    for block in sorted(candidates, key=lambda b: b["date"]):
        codes = supplier_currencies[block["supplier"]]
        code = next(iter(codes)) if len(codes) == 1 else None
        parsed = _vertical_table(block, code) or _numbered_items(block, code) or _service_offer(block, code, rows)
        if not parsed:
            # A standalone discount/adjustment note is not a commercial row.
            # Keep it out of the complete history without weakening parsing of
            # genuine priced products or services.
            if re.search(r"\b(?:discount|adjustment|reduction)\b", commercial_body(block), re.I):
                continue
            return None
        for row in parsed:
            if row["row_type"] == "PRODUCT" and re.search(r"\bmotor\b", row["product_name"], re.I):
                row["source_category"] = "Motors"
            elif row["row_type"] == "SERVICE" and re.search(r"freight|shipping|airfreight|seafreight", row["product_name"], re.I):
                row["source_category"] = "Freight"
            else:
                row.setdefault("source_category", "QUOTATION")
        rows.extend(parsed)
        evidence.append({"source_block_id": block["id"], "date": block["date"],
                         "supplier": block["supplier"], "sender": block["sender"], "item_count": len(parsed)})
    suppliers, currencies = {r["supplier_name"] for r in rows}, {r["currency"] for r in rows}
    return {"document_type": "email_quote", "category": "QUOTATION",
            "document_date": email_date(sent_date) or max(b["date"] for b in candidates),
            "supplier_name": next(iter(suppliers)) if len(suppliers) == 1 else None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "subtotal": None, "grand_total": None, "document_number": None,
            "visible_priced_row_count": len(rows), "_raw_extracted_count": len(rows), "items": rows,
            "_msg_commercial_blocks": evidence}


def extract_inline_quote_history(body, sender=None, sent_date=None):
    """Recover explicit per-unit quotes without using greetings as identity.

    Unknown priced structures cause fallback, never a partial authoritative
    replacement. A supplier's 'quotation below' may annotate the immediately
    following quoted enquiry; it is not a licence to borrow other senders.
    """
    blocks = split_thread(body, sender, sent_date)
    rate_pattern = re.compile(
        rf"(?P<amount>{NUMBER})\s*/-\s*(?:(?P<currency>{MONEY})\s+)?PER\s+(?P<basis>\w+)(?P<detail>.*)$", re.I)

    def company(block):
        display = str(block.get('sender') or '').split('<', 1)[0].strip()
        display = display.rsplit('|', 1)[-1].strip(' \"\'')
        return display if LEGAL_COMPANY.search(display) else block.get('supplier')

    rows, evidence = [], []
    for index, block in enumerate(blocks):
        text = commercial_body(block)
        lines = [clean(line) for line in text.splitlines() if clean(line)]
        matches = [(i, rate_pattern.search(line)) for i, line in enumerate(lines)]
        matches = [(i, m) for i, m in matches if m]
        if not matches:
            if is_commercial(block):
                return None
            continue
        owner = block
        if re.search(r'\brequest\s+to\s+quote\b', text, re.I):
            previous = blocks[index - 1] if index else None
            if not previous or not company(previous) or not re.search(r'\b(?:quotation|offer)\s+below\b', previous['body'], re.I):
                return None
            owner = previous
        supplier = company(owner)
        if not supplier or not owner.get('date'):
            return None
        codes = {currency(m.group('currency')) for _, m in matches if m.group('currency')}
        # Only a single explicitly printed currency within this quote block
        # may fill its other rows. Missing/ambiguous currency stays missing.
        default_code = next(iter(codes)) if len(codes) == 1 else None
        local_rows = []
        for line_index, match in matches:
            prefix = lines[line_index][:match.start()].strip()
            detail = clean(match.group('detail'))
            quantity, uom = None, match.group('basis')
            attrs = [{'label': 'Price basis', 'value': 'PER ' + uom}]
            if '@' in prefix:
                identity = prefix.rsplit('@', 1)[0]
                q = re.search(r'(?:\s|[–—�-])(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>mtr|m|pcs?|rolls?|coils?)\s*$', identity, re.I)
                if q:
                    quantity, uom = q.group('qty'), q.group('uom')
                    identity = identity[:q.start()].strip(' -–—�')
                identity = re.sub(r'(?i)^request\s+to\s+quote\s+for\s+', '', identity)
            elif not prefix:
                serial_index = next((i for i in range(line_index - 1, -1, -1)
                                     if re.fullmatch(r'\d+', lines[i]) and i + 3 == line_index - 1), None)
                if serial_index is None:
                    return None
                identity, a, b = lines[serial_index + 1:line_index]
                if number(a) is not None and number(b) is None:
                    quantity, uom = number(a), b
                elif number(b) is not None and number(a) is None:
                    quantity, uom = number(b), a
                else:
                    return None
            else:
                return None
            if not identity or re.search(r'\b(?:thank you|greetings|regards)\b', identity, re.I):
                return None
            name = clean_source_description(identity + (' | ' + detail if detail else ''))
            row = _base(dict(owner, supplier=supplier), name, number(match.group('amount')),
                        currency(match.group('currency')) or default_code, quantity, uom, attrs=attrs)
            row['source_row_id'] = f"{block['id']}:line:{line_index}"
            row['source_context'] = block['text']
            row['_source_currency_missing'] = not bool(row['currency'])
            local_rows.append(row)
        rows.extend(local_rows)
        evidence.append({'source_block_id': block['id'], 'date': owner['date'],
                         'supplier': supplier, 'item_count': len(local_rows)})
    if not rows:
        return None
    suppliers = {row['supplier_name'] for row in rows}
    codes = {row['currency'] for row in rows}
    return {'document_type': 'email_quote', 'category': 'QUOTATION',
            'supplier_name': next(iter(suppliers)) if len(suppliers) == 1 else None,
            'document_date': email_date(sent_date),
            'currency': next(iter(codes)) if len(codes) == 1 else None,
            'items': rows, '_msg_commercial_blocks': evidence,
            'visible_priced_row_count': len(rows)}


def extract_labelled_table_history(body, sender=None, sent_date=None):
    """Recover complete text-layer cells from explicitly labelled email tables.

    Accept only a full rectangular run with serial/description/price columns;
    uncertain or wrapped cell segmentation stays with the general extractor.
    """
    aliases = {'sl no': 'serial', 's no': 'serial', 'serial no': 'serial',
               'description': 'description', 'item description': 'description',
               'part number': 'part', 'part no': 'part', 'qty': 'quantity',
               'quantity': 'quantity', 'unit price': 'rate', 'total price': 'total',
               'amount': 'total', 'availability': 'availability'}
    rows, evidence = [], []
    for block in split_thread(body, sender, sent_date):
        lines = [clean(s) for s in commercial_body(block).splitlines() if clean(s)]
        for start in range(len(lines)):
            columns, codes = [], set()
            cursor = start
            while cursor < len(lines):
                label = re.sub(r'\([^)]*\)', '', lines[cursor]).strip().lower().rstrip('.')
                if label not in aliases:
                    break
                columns.append(aliases[label])
                codes.update(currency(m.group()) for m in re.finditer(MONEY, lines[cursor], re.I))
                cursor += 1
            if not {'serial', 'description', 'rate', 'quantity'} <= set(columns) or len(columns) != len(set(columns)):
                continue
            local = []
            width = len(columns)
            while cursor + width <= len(lines):
                cells = dict(zip(columns, lines[cursor:cursor + width]))
                if not re.fullmatch(r'\d+', cells['serial']):
                    break
                if number(cells['quantity']) is None or number(cells['rate']) is None or not re.search(r'[A-Za-z\u0080-\uffff]', cells['description']):
                    local = []
                    break
                if 'total' in cells and number(cells['total']) is None:
                    local = []
                    break
                attrs = [{'label': label, 'value': cells[key]} for key, label in
                         [('part', 'Part Number'), ('availability', 'Availability')] if cells.get(key)]
                row = _base(block, cells['description'], number(cells['rate']),
                            next(iter(codes)) if len(codes) == 1 else None,
                            number(cells['quantity']), total=number(cells.get('total')), attrs=attrs)
                row['source_row_id'] = f"{block['id']}:table:{start}:row:{cells['serial']}"
                local.append(row)
                cursor += width
            if local:
                rows.extend(local)
                evidence.append({'source_block_id': block['id'], 'date': block['date'], 'item_count': len(local)})
                break
    return {'items': rows, '_msg_commercial_blocks': evidence} if rows else None


def extract_agreed_rate_history(body, sender=None, sent_date=None):
    """Recover explicitly labelled shipment offers, retaining dated revisions.

    This is additive recovery: unrelated extracted tables/attachments survive
    reconciliation. Conditional charges in terms are not agreed quote rows.
    """
    rows, evidence, seen = [], [], set()
    for block in split_thread(body, sender, sent_date):
        text = commercial_body(block)
        # Each collection/delivery group is a separate physical offer. In
        # particular, equally priced numbered shipments are not duplicates.
        if block['date'] and not is_enquiry(block):
            lines = text.splitlines()
            starts = [i for i, line in enumerate(lines)
                      if re.fullmatch(r'collection\s+from\s*[:\-]?', line, re.I)]
            for offer_index, start in enumerate(starts):
                end = starts[offer_index + 1] if offer_index + 1 < len(starts) else len(lines)
                section = lines[start:end]
                prices = list(re.finditer(
                    rf'(?im)^\s*freight\s*[:–—-]\s*(?P<currency>{MONEY})\s*:?\s*(?P<amount>{NUMBER})(?=\s|$)',
                    '\n'.join(section)))
                if len(prices) != 1:
                    continue
                fields = {}
                for i, line in enumerate(section[:-1]):
                    label = line.strip().rstrip(':').casefold()
                    if label in ('collection from', 'delivery to', 'weight', 'mode', 'transit time'):
                        fields[label] = clean(section[i + 1])
                if not fields.get('collection from') or not fields.get('delivery to'):
                    continue
                attrs = [{'label': key.title(), 'value': value} for key, value in fields.items()]
                marker = lines[start - 1] if start else ''
                if re.fullmatch(r'shipment\s+\d+', marker, re.I):
                    attrs.append({'label': 'Shipment', 'value': marker.split()[-1]})
                validity = re.search(r'(?im)^\s*\*?\s*quotation valid for\s+([^\n]+)', text)
                if validity:
                    attrs.append({'label': 'Quotation valid for', 'value': clean(validity[1])})
                price = prices[0]
                row = _base(block, f"Freight from {fields['collection from']} to {fields['delivery to']}",
                            number(price['amount']), currency(price['currency']),
                            total=number(price['amount']), row_type='CHARGE', attrs=attrs)
                row['source_row_id'] = f"{block['id']}:offer:{offer_index}"
                rows.append(row)
                evidence.append({'source_block_id': block['id'], 'date': block['date'], 'item_count': 1})
        rates = list(re.finditer(
            rf"(?im)^\s*agreed\s+rates?\s*[:=-]\s*(?P<currency>{MONEY})\s*:?\s*(?P<amount>{NUMBER})\s*$", text))
        route = re.search(r"\bshipment\s+from\s+([^\n]+?)(?=\s+excluding\b|[.;\n]|$)", text, re.I)
        if len(rates) != 1 or not route or not block['date'] or not block['supplier'] or is_enquiry(block):
            continue
        rate = rates[0]
        attrs = []
        for match in re.finditer(
            r"(?im)^\s*(carrier|transit time|rate validity|chargeable weight[^:#\n]*)\s*[:#]\s*([^\n]+)", text):
            attrs.append({'label': clean(match[1]), 'value': clean(match[2])})
        row = _base(block, 'Shipment from ' + clean(route[1]), number(rate['amount']),
                    currency(rate['currency']), total=number(rate['amount']), row_type='SERVICE', attrs=attrs)
        key = (row['supplier_name'], row['purchase_date'], row['product_name'],
               row['unit_price'], row['currency'], tuple((a['label'], a['value']) for a in attrs))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        evidence.append({'source_block_id': block['id'], 'date': block['date'], 'item_count': 1})
    return {'items': rows, '_msg_commercial_blocks': evidence} if rows else None


def reconcile_inline_quote_history(result, history, source_body=None):
    """Replace uniquely bound text rows; never discard independent attachments."""
    def same_number(left, right):
        left, right = number(left), number(right)
        return left is not None and right is not None and Decimal(left) == Decimal(right)

    canonical = history['items']
    retained = []
    for item in result.get('items') or []:
        if item.get('_attachment_name') or item.get('attachment_name'):
            retained.append(item)
            continue
        candidates = [row for row in canonical
                      if row['purchase_date'] == item.get('purchase_date')
                      and same_number(row['unit_price'], item.get('unit_price'))
                      and (not item.get('quantity') or not row.get('quantity')
                           or same_number(item['quantity'], row['quantity']))]
        if len(candidates) != 1:
            retained.append(item)
        elif source_body:
            # Keep additional details when they occur in the email source;
            # recovery must not discard them merely because wording differs.
            target = candidates[0]
            attrs = target.setdefault('extra_attributes', [])
            labels = {clean(a.get('label')).casefold() for a in attrs}
            evidence = re.sub(r'\W+', ' ', str(source_body).casefold()).strip()
            for attr in item.get('extra_attributes') or []:
                label = clean(attr.get('label')).casefold()
                value = re.sub(r'\W+', ' ', str(attr.get('value') or '').casefold()).strip()
                if label not in labels and value and value in evidence:
                    attrs.append(dict(attr))
                    labels.add(label)
    merged = dict(result)
    merged['items'] = canonical + retained
    merged['_msg_commercial_blocks'] = history['_msg_commercial_blocks']
    # Multiple attachment sellers/currencies do not have a single default.
    merged['supplier_name'] = None
    merged['currency'] = None
    return merged


def finalize_physical_freight_offers(result, body, sender=None, sent_date=None):
    """Replace duplicate interpretations of uniquely identified source offers.

    Re-read shipment identity from the physical block, never from attributes
    that an intervening price-only reconciliation may have copied.
    """
    history = extract_agreed_rate_history(body, sender, sent_date)
    canonical = [r for r in (history or {}).get('items', []) if ':offer:' in str(r.get('source_row_id'))]
    if not canonical:
        return result

    def route(item):
        match = re.search(r'\bfrom\s+(.+?)\s+to\s+([^|()]+)', str(item.get('product_name') or ''), re.I)
        if match:
            return tuple(clean(v).casefold().strip(' .') for v in match.groups())
        attrs = {clean(a.get('label')).casefold(): clean(a.get('value')).casefold().strip(' .')
                 for a in item.get('extra_attributes') or []}
        if attrs.get('collection from') and attrs.get('delivery to'):
            return attrs['collection from'], attrs['delivery to']
        return None

    def shipment(item):
        match = re.search(r'\bshipment\s+(\d+)\b', str(item.get('product_name') or ''), re.I)
        if match:
            return match[1]
        value = next((clean(a.get('value')) for a in item.get('extra_attributes') or []
                      if clean(a.get('label')).casefold() == 'shipment'), '')
        match = re.fullmatch(r'(?:shipment\s*)?(\d+)', value, re.I)
        return match[1] if match else value or None

    source_ids = {r['source_row_id'] for r in canonical}
    retained = []
    for item in result.get('items') or []:
        if item.get('_attachment_name') or item.get('attachment_name'):
            retained.append(item)
            continue
        if item.get('source_row_id') in source_ids:
            continue
        price = number(item.get('unit_price'))
        candidates = [r for r in canonical if price is not None
                      and Decimal(r['unit_price']) == Decimal(price)
                      and r['purchase_date'] == email_date(item.get('purchase_date'))
                      and r['currency'] == item.get('currency')
                      and route(r) == route(item) and route(item) is not None
                      and clean(r.get('supplier_name')).casefold() == clean(item.get('supplier_name')).casefold()
                      and (not shipment(item) or shipment(item) == shipment(r))]
        if len(candidates) != 1:
            retained.append(item)
    result['items'] = canonical + retained
    result['visible_priced_row_count'] = len(result['items'])
    return result


def restore_offer_context(result, body, sender=None, sent_date=None):
    """Restore context only from a uniquely bound, dated priced email block."""
    blocks = split_thread(body, sender, sent_date)
    bindings = {}
    for item in result.get('items') or []:
        if item.get('_attachment_name') or item.get('attachment_name'):
            continue
        price = number(item.get('unit_price'))
        date = email_date(item.get('purchase_date'))
        if price is None or not date:
            continue
        candidates = []
        for block in blocks:
            if block['date'] != date or is_enquiry(block):
                continue
            text = commercial_body(block)
            amounts = re.finditer(rf'\b(?P<currency>{MONEY})\s*:?\s*(?P<amount>{NUMBER})', text, re.I)
            if any(number(m['amount']) is not None and Decimal(number(m['amount'])) == Decimal(price)
                   and (not item.get('currency') or currency(m['currency']) == item['currency']) for m in amounts):
                candidates.append(block)
        if len(candidates) != 1:
            continue
        block = candidates[0]
        bindings[id(item)] = block['id']
        if block['supplier']:
            item['supplier_name'] = block['supplier']
        lines = [clean(line) for line in commercial_body(block).splitlines() if clean(line)]
        attrs = list(item.get('extra_attributes') or [])
        for index, line in enumerate(lines[:-1]):
            if re.fullmatch(r'collection\s+from\s*[:\-]?|delivery\s+to\s*[:\-]?', line, re.I):
                label, value = line.rstrip(':- '), lines[index + 1]
                if not any(clean(a.get('label')).casefold() == label.casefold() for a in attrs):
                    attrs.append({'label': label, 'value': value})
        item['extra_attributes'] = attrs
    # Metadata can be filled after the first history merge. Reconcile again
    # using the final dated source block and route, rather than display text.
    def route(item):
        match = re.search(r'\bfrom\s+(.+?)\s+to\s+([^|]+)', str(item.get('product_name') or ''), re.I)
        return tuple(clean(v).casefold().strip(' .') for v in match.groups()) if match else None

    items = result.get('items') or []
    retained = []
    source_text = re.sub(r'\W+', ' ', str(body or '').casefold()).strip()
    for item in items:
        if item.get('_msg_source_verified') or id(item) not in bindings or not route(item):
            retained.append(item)
            continue
        matches = [other for other in items if other.get('_msg_source_verified')
                   and id(other) in bindings and bindings[id(other)] == bindings[id(item)]
                   and route(other) == route(item)
                   and other.get('currency') == item.get('currency')
                   and other.get('supplier_name') == item.get('supplier_name')
                   and Decimal(number(other.get('unit_price'))) == Decimal(number(item.get('unit_price')))]
        if len(matches) != 1:
            retained.append(item)
            continue
        target = matches[0]
        attrs = target.setdefault('extra_attributes', [])
        labels = {clean(a.get('label')).casefold() for a in attrs}
        for attr in item.get('extra_attributes') or []:
            label = clean(attr.get('label')).casefold()
            value = re.sub(r'\W+', ' ', str(attr.get('value') or '').casefold()).strip()
            if label not in labels and value and value in source_text:
                attrs.append(dict(attr))
                labels.add(label)
    result['items'] = retained
    result['visible_priced_row_count'] = len(retained)
    return result
