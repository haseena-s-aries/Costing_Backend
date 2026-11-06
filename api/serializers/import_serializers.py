from rest_framework import serializers

from api.constants import SYSTEM_FIELD_NAMES
from api.models import ImportBatch, MappingTemplate

class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    category = serializers.CharField()
    supplier_id = serializers.IntegerField(required=False, allow_null=True)
    supplier_name = serializers.CharField(required=False, allow_blank=True)
    default_currency = serializers.CharField(
        required=False,
        allow_blank=True,
        default=""
    )
    remarks = serializers.CharField(required=False, allow_blank=True)

class HeaderSerializer(serializers.Serializer):
    sheet_id = serializers.IntegerField()
    header_row_number = serializers.IntegerField(min_value=1)

class MappingItemSerializer(serializers.Serializer):
    uploaded_column_index = serializers.IntegerField(min_value=0)
    uploaded_column_name = serializers.CharField()
    system_field_name = serializers.ChoiceField(choices=sorted(SYSTEM_FIELD_NAMES))
    is_ignored = serializers.BooleanField(default=False)

class MappingSerializer(serializers.Serializer):
    sheet_id = serializers.IntegerField()
    mappings = MappingItemSerializer(many=True)

class PreviewSerializer(serializers.Serializer):
    sheet_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
    )
    limit = serializers.IntegerField(
        min_value=1,
        max_value=5000,
        default=1000,
        required=False,
    )

class TemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MappingTemplate
        fields = ["id", "template_name", "supplier", "import_category", "description", "is_active", "created_at"]

class SaveTemplateSerializer(serializers.Serializer):
    sheet_id = serializers.IntegerField()
    template_name = serializers.CharField()
    supplier_id = serializers.IntegerField(required=False, allow_null=True)
    import_category = serializers.CharField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)


class MetadataUpdateSerializer(serializers.Serializer):
    document_fields = serializers.DictField(required=True)

class ImportRowUpdateSerializer(serializers.Serializer):
    row_type = serializers.ChoiceField(choices=["PRODUCT", "SERVICE", "CHARGE", "UNKNOWN", "IGNORE"], required=False)
    mapped_data = serializers.DictField(required=False)
