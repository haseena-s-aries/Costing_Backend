from django.core.management.base import BaseCommand

from api.models import ImportBatch, Supplier
from api.services.normalization import normalize_supplier


class Command(BaseCommand):
    help = "Create/reuse Supplier records from ImportBatch.supplier_name_from_file and link old batches."

    def handle(self, *args, **options):
        created = 0
        linked = 0
        batches = ImportBatch.objects.filter(supplier__isnull=True).exclude(supplier_name_from_file__isnull=True).exclude(supplier_name_from_file="")
        for batch in batches:
            supplier_name = (batch.supplier_name_from_file or "").strip()
            if not supplier_name:
                continue
            normalized = normalize_supplier(supplier_name)
            supplier = Supplier.objects.filter(normalized_supplier_name=normalized, is_active=True).first()
            if supplier is None:
                supplier = Supplier.objects.create(
                    supplier_name=supplier_name,
                    normalized_supplier_name=normalized,
                    default_currency=batch.default_currency or "AED",
                    is_active=True,
                )
                created += 1
            batch.supplier = supplier
            batch.save(update_fields=["supplier", "updated_at"])
            linked += 1
        self.stdout.write(self.style.SUCCESS(f"Backfilled suppliers. created={created}, linked_batches={linked}"))
