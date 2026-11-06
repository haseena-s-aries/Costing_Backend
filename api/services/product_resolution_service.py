from django.utils import timezone
import re
from api.models import Product, ProductSupplierAlias
from api.services.product_matching_service import ProductMatchingService
from api.services.product_normalization_service import canonical_name, group_key, most_common, normalize_code, normalize_product_text, product_identity_text, sanitize_product_identity

class ProductResolutionService:
    def resolve_preview(self, mapped, supplier=None):
        return ProductMatchingService().match(mapped, supplier=supplier)

    def get_or_create_for_group(self, rows, batch):
        mapped_rows = [row.mapped_data for row in rows]
        for mapped in mapped_rows:
            mapped["product_name"] = sanitize_product_identity(mapped.get("product_name"))
            product_name = mapped.get("product_name")
            if not mapped.get("item_code") and product_name:
                match = re.search(r"\|\s*([A-Z0-9][A-Z0-9_-]{2,})\s*$", str(product_name), re.I)
                if match:
                    mapped["item_code"] = match.group(1)
                    mapped["product_name"] = re.sub(r"\s*\|\s*[A-Z0-9][A-Z0-9_-]{2,}\s*$", "", str(product_name), flags=re.I).strip()
        first = mapped_rows[0]
        existing = ProductMatchingService().match(first, supplier=batch.supplier)
        if existing["product"]:
            product = existing["product"]
            clean_name = sanitize_product_identity(product.product_name)
            if clean_name and clean_name != product.product_name:
                product.product_name = clean_name
                product.normalized_description = normalize_product_text(clean_name)
                product.save(update_fields=["product_name", "normalized_description", "updated_at"])
            return existing["product"], False
        item_code = most_common(mapped_rows, "item_code")
        part_number = most_common(mapped_rows, "part_number")
        product_name = sanitize_product_identity(canonical_name(mapped_rows))
        normalized_description = normalize_product_text(product_name)
        product = self._find_existing(item_code, part_number, product_name)
        if product:
            return product, False
        product = Product.objects.create(
            item_code=item_code or None,
            part_number=part_number or None,
            product_name=product_name or item_code or part_number,
            normalized_description=normalized_description,
            brand=most_common(mapped_rows, "brand") or None,
            manufacturer=most_common(mapped_rows, "manufacturer") or None,
            unit_of_measurement=most_common(mapped_rows, "unit_of_measurement") or None,
            is_active=True,
        )
        return product, True

    def update_alias(self, product, supplier, mapped, batch):
        if not supplier:
            return None
        item_code = normalize_code(mapped.get("item_code"))
        part_number = normalize_code(mapped.get("part_number"))
        description = normalize_product_text(product_identity_text(mapped) or mapped.get("product_name"))
        alias = None
        qs = ProductSupplierAlias.objects.filter(product=product, supplier=supplier, is_active=True)
        if item_code:
            alias = qs.filter(normalized_supplier_item_code=item_code).first()
        if not alias and part_number:
            alias = qs.filter(normalized_supplier_part_number=part_number).first()
        if not alias and description:
            alias = qs.filter(normalized_supplier_description=description).first()
        now = timezone.now()
        if alias:
            alias.last_seen_import_batch = batch
            alias.last_seen_at = now
            alias.save(update_fields=["last_seen_import_batch", "last_seen_at", "updated_at"])
            return alias
        return ProductSupplierAlias.objects.create(
            product=product,
            supplier=supplier,
            supplier_item_code=mapped.get("item_code") or None,
            normalized_supplier_item_code=item_code or None,
            supplier_part_number=mapped.get("part_number") or None,
            normalized_supplier_part_number=part_number or None,
            supplier_description=mapped.get("product_name") or None,
            normalized_supplier_description=description or None,
            first_seen_import_batch=batch,
            last_seen_import_batch=batch,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )

    def _find_existing(self, item_code, part_number, product_name):
        normalized_item = normalize_code(item_code)
        normalized_part = normalize_code(part_number)
        normalized_name = normalize_product_text(product_identity_text({
            "product_name": product_name,
        }) or product_name)
        for product in Product.objects.filter(is_active=True)[:1000]:
            if normalized_item and normalize_code(product.item_code) == normalized_item:
                return product
            if normalized_part and normalize_code(product.part_number) == normalized_part:
                return product
            if normalized_name and normalize_product_text(sanitize_product_identity(product.product_name)) == normalized_name:
                clean_name = sanitize_product_identity(product.product_name)
                if clean_name != product.product_name:
                    product.product_name = clean_name
                    product.normalized_description = normalize_product_text(clean_name)
                    product.save(update_fields=["product_name", "normalized_description", "updated_at"])
                return product
        return None
