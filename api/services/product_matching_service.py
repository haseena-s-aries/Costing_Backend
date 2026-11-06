from decimal import Decimal
import re
from rapidfuzz import fuzz

from api.models import Product, ProductMatchMethod, ProductMatchStatus, ProductSupplierAlias
from api.services.product_normalization_service import group_key, normalize_code, normalize_product_text, product_identity_text, sanitize_product_identity

class ProductMatchingService:
    def match(self, mapped, supplier=None):
        item_code = mapped.get("item_code")
        part_number = mapped.get("part_number")
        product_name = sanitize_product_identity(mapped.get("product_name"))
        mapped["product_name"] = product_name
        product_identity = product_identity_text(mapped) or product_name

        extracted_code = ""
        if not item_code and product_name:
            match = re.search(r"\|\s*([A-Z0-9][A-Z0-9_-]{2,})\s*$", str(product_name), re.I)
            if match:
                extracted_code = match.group(1)
                item_code = extracted_code
                product_name = re.sub(r"\s*\|\s*[A-Z0-9][A-Z0-9_-]{2,}\s*$", "", str(product_name), flags=re.I).strip()
                mapped["item_code"] = item_code
                mapped["product_name"] = product_name
        brand = mapped.get("brand")
        if not (item_code or part_number or product_name):
            return self._result(ProductMatchStatus.INVALID, None, ProductMatchMethod.INVALID, 0, [], "")
        key = group_key(mapped)
        alias = self._alias_match(mapped, supplier)
        if alias:
            return self._result(ProductMatchStatus.MATCHED, alias.product, ProductMatchMethod.SUPPLIER_ALIAS_EXACT, 100, [], key)
        for field, value, method in [
            ("item_code", item_code, ProductMatchMethod.ITEM_CODE_EXACT),
            ("part_number", part_number, ProductMatchMethod.PART_NUMBER_EXACT),
        ]:
            normalized = normalize_code(value)
            if normalized:
                product = self._match_code(field, normalized)
                if product:
                    return self._result(ProductMatchStatus.MATCHED, product, method, 100, [], key)
                other = "part_number" if field == "item_code" else "item_code"
                product = self._match_code(other, normalized)
                if product:
                    return self._result(ProductMatchStatus.MATCHED, product, method, 95, [], key)
        if brand and part_number:
            normalized_brand = normalize_product_text(brand)
            normalized_part = normalize_code(part_number)
            for product in Product.objects.filter(part_number__isnull=False, is_active=True)[:1000]:
                if normalize_code(product.part_number) == normalized_part and normalize_product_text(product.brand) == normalized_brand:
                    return self._result(ProductMatchStatus.MATCHED, product, ProductMatchMethod.BRAND_PART_NUMBER_EXACT, 100, [], key)
        normalized_description = normalize_product_text(product_identity)
        if normalized_description:
            exact = []
            products = list(Product.objects.filter(is_active=True)[:1000])
            for product in products:
                product_description = normalize_product_text(
                    sanitize_product_identity(product.normalized_description
                    or product.product_name)
                )

                if (
                    normalize_product_text(sanitize_product_identity(product.product_name)) == normalized_description
                    or product_description == normalized_description
                ):
                    exact.append(product)
            if len(exact) == 1:
                return self._result(ProductMatchStatus.MATCHED, exact[0], ProductMatchMethod.NORMALIZED_DESCRIPTION, 95, [], key)
            if len(exact) > 1:
                return self._result(ProductMatchStatus.AMBIGUOUS, None, ProductMatchMethod.AMBIGUOUS, 90, self._suggestions(exact, 90), key)
            scored = []
            for product in products:
                candidate_text = normalize_product_text(
                    sanitize_product_identity(product.normalized_description
                    or product.product_name)
                )
                score = fuzz.token_set_ratio(
                    normalized_description,
                    candidate_text,
                )
                if score >= 85:
                    scored.append((product, score))
            scored.sort(key=lambda item: item[1], reverse=True)
            if scored and scored[0][1] >= 95 and (len(scored) == 1 or scored[0][1] - scored[1][1] >= 5):
                return self._result(ProductMatchStatus.SUGGESTED, None, ProductMatchMethod.FUZZY_DESCRIPTION, scored[0][1], self._suggestions([item[0] for item in scored[:5]], None, scored), key)
            if len(scored) > 1:
                return self._result(ProductMatchStatus.AMBIGUOUS, None, ProductMatchMethod.AMBIGUOUS, scored[0][1], self._suggestions([item[0] for item in scored[:5]], None, scored), key)
        return self._result(ProductMatchStatus.NEW_PRODUCT, None, ProductMatchMethod.NEW_PRODUCT, 100, [], key)

    def _match_code(self, field, normalized):
        for product in Product.objects.filter(**{f"{field}__isnull": False}, is_active=True)[:1000]:
            if normalize_code(getattr(product, field)) == normalized:
                return product
        return None

    def _alias_match(self, mapped, supplier):
        if not supplier:
            return None
        item_code = normalize_code(mapped.get("item_code"))
        part_number = normalize_code(mapped.get("part_number"))
        description = normalize_product_text(product_identity_text(mapped) or mapped.get("product_name"))
        aliases = ProductSupplierAlias.objects.filter(supplier=supplier, is_active=True).select_related("product")
        if item_code:
            match = aliases.filter(normalized_supplier_item_code=item_code).first()
            if match:
                return match
        if part_number:
            match = aliases.filter(normalized_supplier_part_number=part_number).first()
            if match:
                return match
        if description:
            for alias in aliases[:500]:
                if alias.normalized_supplier_description == description:
                    return alias
        return None

    def _suggestions(self, products, default_score=None, scored=None):
        score_map = {product.id: score for product, score in scored or []}
        return [{"product_id": product.id, "product_name": product.product_name, "score": int(default_score or score_map.get(product.id, 0))} for product in products]

    def _result(self, status, product, method, confidence, suggestions, key):
        return {
            "status": status,
            "product": product,
            "product_id": product.id if product else None,
            "match_method": method,
            "confidence": Decimal(str(confidence)),
            "suggestions": suggestions,
            "group_key": key,
        }
