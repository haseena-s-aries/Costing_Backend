import re


class ConfidenceScorer:
    def score(self, result, structure_confidence=0.0, mapping_confidence=0.0, validation_issues=None, extraction_method=None):
        items = result.get("items") or []
        fields = {f: sum(1 for i in items if i.get(f) not in (None, "")) / (len(items) or 1) for f in ("product_name", "quantity", "unit_price", "line_total")}
        fields["amount"] = sum(
            1 for i in items
            if i.get("unit_price") not in (None, "")
            or i.get("line_total") not in (None, "")
        ) / (len(items) or 1)
        fields["item_currency"] = sum(
            1 for i in items
            if i.get("currency") not in (None, "") or result.get("currency")
        ) / (len(items) or 1)
        weak_names = []
        generic_names = {"motor", "cable", "service", "product", "item", "material"}
        for index, item in enumerate(items):
            name = str(item.get("product_name") or "").strip()
            normalized_name = name.lower()
            if re.search(r"\bdear\s+(?:mr|ms|mrs)\b|please\s+find|as\s+below", normalized_name):
                weak_names.append(f"weak_product_name:{index}")
            elif normalized_name.rstrip(":").strip() in generic_names:
                weak_names.append(f"generic_product_name:{index}")
        reasons = list(validation_issues or [])
        reasons.extend(weak_names)
        fields.update({"currency": float(bool(result.get("currency"))), "supplier_name": float(bool(result.get("supplier_name"))), "document_date": float(bool(result.get("document_date")))})
        if not items: reasons.append("no_items")
        # Spreadsheet rows can be commercially complete even when the file
        # has no supplier/date/currency header. Those fields are reported in
        # field_scores, but must not make a structurally sound table fall
        # back to Gemini by themselves.
        core_fields = ("product_name", "amount", "item_currency")
        data = sum(fields[field] for field in core_fields) / len(core_fields)
        final = data if extraction_method == "gemini" else structure_confidence * .30 + mapping_confidence * .30 + data * .40
        if validation_issues or weak_names: final *= .75
        level = "high" if final >= .95 else "medium" if final >= .80 else "low"
        return {"structure_confidence": round(structure_confidence, 4), "mapping_confidence": round(mapping_confidence, 4), "data_confidence": round(data, 4), "final_confidence": round(final, 4), "score": round(final, 4), "level": level, "reasons": reasons, "field_scores": fields}
