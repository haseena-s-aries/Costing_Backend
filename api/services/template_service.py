from django.db import transaction

from api.exceptions import ImportServiceError
from api.models import MappingTemplate, MappingTemplateField
from api.services.normalization import normalize_text

class TemplateService:
    def save_from_batch(self, batch, sheet, data, user=None):
        mappings = list(sheet.column_mappings.filter(is_ignored=False))
        if not mappings:
            raise ImportServiceError("No mappings saved for this sheet.")
        with transaction.atomic():
            template = MappingTemplate.objects.create(
                template_name=data["template_name"],
                supplier_id=data.get("supplier_id") or batch.supplier_id,
                import_category=data.get("import_category") or batch.category,
                description=data.get("description") or "",
                created_by=user,
            )
            MappingTemplateField.objects.bulk_create([
                MappingTemplateField(
                    template=template,
                    original_uploaded_column_name=mapping.uploaded_column_name,
                    normalized_uploaded_column_name=normalize_text(mapping.uploaded_column_name),
                    system_field_name=mapping.system_field_name,
                    original_column_position=mapping.uploaded_column_index,
                    is_required=mapping.is_required,
                )
                for mapping in mappings
            ])
        return template

    def apply(self, template, headers):
        normalized_headers = {normalize_text(header): (index, header) for index, header in enumerate(headers)}
        matched = []
        missing = []
        for field in template.fields.all():
            current = normalized_headers.get(field.normalized_uploaded_column_name)
            if current:
                index, header = current
                matched.append({"uploaded_column_index": index, "uploaded_column_name": header, "system_field_name": field.system_field_name})
            else:
                missing.append(field.original_uploaded_column_name)
        matched_names = {item["uploaded_column_name"] for item in matched}
        unmapped = [header for header in headers if header not in matched_names]
        return {"template_id": template.id, "template_name": template.template_name, "matched": matched, "missing_template_columns": missing, "unmapped_uploaded_columns": unmapped}
