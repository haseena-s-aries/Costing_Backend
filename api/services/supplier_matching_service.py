from api.models import Supplier
from api.services.normalization import normalize_supplier


class SupplierMatchingService:
    def match(self, batch, mapped):
        supplier_text = (
            mapped.get("supplier")
            or mapped.get("supplier_name")
            or ""
        )

        supplier_text = str(supplier_text).strip()

        if not supplier_text or supplier_text.upper() == "UNKNOWN":
            if batch.supplier_id:
                return batch.supplier
            return None

        normalized = normalize_supplier(supplier_text)

        supplier = Supplier.objects.filter(
            supplier_code__iexact=supplier_text,
            is_active=True,
        ).first()

        if supplier:
            return supplier

        for supplier in Supplier.objects.filter(is_active=True)[:500]:
            if (
                normalize_supplier(supplier.supplier_name) == normalized
                or normalize_supplier(supplier.normalized_supplier_name) == normalized
            ):
                return supplier

        return Supplier.objects.create(
            supplier_name=supplier_text,
            normalized_supplier_name=normalized,
            default_currency=batch.default_currency or "AED",
            is_active=True,
        )