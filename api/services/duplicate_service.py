from decimal import Decimal, InvalidOperation

from api.models import PriceHistory
from api.services.normalization import normalize_text


class DuplicateService:
    def fingerprint(self, mapped, batch):
        parts = [
            mapped.get("item_code") or mapped.get("part_number") or mapped.get("product_name"),
            mapped.get("supplier"),
            batch.category,
            mapped.get("purchase_date"),
            mapped.get("quantity"),
            mapped.get("unit_price"),
            mapped.get("currency"),
            self.source_reference(mapped),
        ]
        return "|".join(normalize_text(part) for part in parts)

    def source_reference(self, mapped):
        return mapped.get("source_reference") or mapped.get("lpo_reference") or mapped.get("grn_reference") or mapped.get("quotation_reference")

    def existing_price_history(self, *, product, supplier, batch, mapped):
        if not product:
            return None
        try:
            quantity = Decimal(str(mapped.get("quantity") or "1"))
            unit_price = Decimal(str(mapped.get("unit_price")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        qs = PriceHistory.objects.filter(
            product=product,
            source_type=batch.category,
            purchase_date=mapped.get("purchase_date") or None,
            quantity=quantity,
            original_unit_price=unit_price,
            currency=(mapped.get("currency") or batch.default_currency or ""),
            is_active=True,
        )
        if supplier:
            qs = qs.filter(supplier=supplier)
        else:
            qs = qs.filter(supplier__isnull=True)
        source_reference = self.source_reference(mapped)
        if source_reference:
            qs = qs.filter(source_reference=source_reference)
        else:
            qs = qs.filter(source_reference__isnull=True)
        return qs.first()
