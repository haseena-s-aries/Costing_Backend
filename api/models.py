from __future__ import annotations

import hashlib
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.contrib.auth.models import AbstractUser, Group, Permission
from django.utils import timezone


# =========================================================
# COMMON BASE MODEL
# =========================================================


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# =========================================================
# CHOICES
# =========================================================


class ImportCategory(models.TextChoices):
    SUPPLIER_QUOTATION = "supplier_quotation", "Supplier Quotation"
    PURCHASE_ORDER = "purchase_order", "Purchase Order"
    HISTORICAL_PURCHASE = "historical_purchase", "Historical Purchase Data"
    LPO = "lpo", "LPO Data"
    GRN = "grn", "GRN Data"
    STOCK_PURCHASE = "stock_purchase", "Stock Purchase Data"
    OTHER = "other", "Other Price Data"


class ImportBatchStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    MAPPING_PENDING = "mapping_pending", "Mapping Pending"
    PREVIEW_READY = "preview_ready", "Preview Ready"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_ERRORS = (
        "completed_with_errors",
        "Completed with Errors",
    )
    FAILED = "failed", "Failed"
    DELETED = "deleted", "Deleted"


class ImportRowStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VALID = "valid", "Valid"
    WARNING = "warning", "Warning"
    FAILED = "failed", "Failed"
    DUPLICATE = "duplicate", "Duplicate"
    IMPORTED = "imported", "Imported"
    IGNORED = "ignored", "Ignored"


class ProductMatchStatus(models.TextChoices):
    EXACT_MATCH = "exact_match", "Exact Match"
    SUGGESTED = "suggested", "Suggested"
    MATCHED = "matched", "Matched"
    UNMATCHED = "unmatched", "Unmatched"
    NEW_PRODUCT = "new_product", "New Product"
    AMBIGUOUS = "ambiguous", "Ambiguous"
    INVALID = "invalid", "Invalid"
    IGNORED = "ignored", "Ignored"


class ProductMatchMethod(models.TextChoices):
    ITEM_CODE_EXACT = "item_code_exact", "Item Code Exact"
    PART_NUMBER_EXACT = "part_number_exact", "Part Number Exact"
    MANUFACTURER_PART_NUMBER = (
        "manufacturer_part_number",
        "Manufacturer and Part Number",
    )
    NORMALIZED_DESCRIPTION = (
        "normalized_description",
        "Normalized Description",
    )
    MANUAL = "manual", "Manual"
    SUPPLIER_ALIAS_EXACT = "supplier_alias_exact", "Supplier Alias Exact"
    BRAND_PART_NUMBER_EXACT = "brand_part_number_exact", "Brand and Part Number Exact"
    FUZZY_DESCRIPTION = "fuzzy_description", "Fuzzy Description"
    NEW_PRODUCT = "new_product", "New Product"
    AMBIGUOUS = "ambiguous", "Ambiguous"
    INVALID = "invalid", "Invalid"
    NONE = "none", "None"


class PriceSourceType(models.TextChoices):
    SUPPLIER_QUOTATION = "supplier_quotation", "Supplier Quotation"
    HISTORICAL_PURCHASE = "historical_purchase", "Historical Purchase"
    LPO = "lpo", "LPO"
    GRN = "grn", "GRN"
    STOCK_PURCHASE = "stock_purchase", "Stock Purchase"
    MANUAL = "manual", "Manual Price"
    MARKET_BENCHMARK = "market_benchmark", "Market Benchmark"


class CommodityBenchmark(TimeStampedModel):
    provider = models.CharField(max_length=50, default="metals_dev", db_index=True)
    provider_symbol = models.CharField(max_length=80, db_index=True)
    display_name = models.CharField(max_length=150)
    benchmark_name = models.CharField(max_length=255, null=True, blank=True)
    metal_type = models.CharField(max_length=150, null=True, blank=True)
    region = models.CharField(max_length=150, null=True, blank=True)
    default_unit = models.CharField(max_length=30, default="mt")
    is_supported = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "price_commodity_benchmarks"
        ordering = ["display_name"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_symbol"], name="unique_commodity_provider_symbol"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.provider_symbol})"


class CommodityPriceHistory(TimeStampedModel):
    benchmark = models.ForeignKey(
        CommodityBenchmark,
        on_delete=models.PROTECT,
        related_name="price_history",
    )
    market_date = models.DateField(db_index=True)
    source_timestamp = models.DateTimeField(null=True, blank=True)
    provider = models.CharField(max_length=50, default="metals_dev", db_index=True)
    provider_symbol = models.CharField(max_length=80, db_index=True)
    raw_price = models.DecimalField(max_digits=20, decimal_places=8)
    raw_currency = models.CharField(max_length=10, default="USD")
    raw_unit = models.CharField(max_length=30, default="mt")
    usd_per_metric_tonne = models.DecimalField(max_digits=20, decimal_places=8)
    usd_aed_exchange_rate = models.DecimalField(max_digits=18, decimal_places=8)
    aed_per_metric_tonne = models.DecimalField(max_digits=20, decimal_places=8)
    aed_per_kg = models.DecimalField(max_digits=20, decimal_places=8)
    response_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "price_commodity_price_history"
        ordering = ["-market_date", "benchmark__display_name"]
        constraints = [
            models.UniqueConstraint(fields=["benchmark", "market_date"], name="unique_commodity_daily_price"),
        ]

    def __str__(self) -> str:
        return f"{self.benchmark.display_name} {self.market_date}: AED/KG {self.aed_per_kg}"


class CommodityFetchLog(TimeStampedModel):
    provider = models.CharField(max_length=50, default="metals_dev", db_index=True)
    status = models.CharField(max_length=30, db_index=True)
    requested_symbols = models.JSONField(default=list, blank=True)
    response_summary = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "price_commodity_fetch_logs"
        ordering = ["-created_at"]


class BidCostingStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class UserRole(models.TextChoices):
    ADMIN = "admin", "Admin"
    MANAGER = "manager", "Manager"
    USER = "user", "User"


# =========================================================
# USERS
# =========================================================


class PriceUser(AbstractUser):
    """Application login user with approval and business identity fields."""
    is_rejected = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    full_name = models.CharField(max_length=255, blank=True)
    employee_code = models.CharField(max_length=100, unique=True, null=True, blank=True)
    requested_role = models.CharField(max_length=30, choices=UserRole.choices, default=UserRole.USER)
    role = models.CharField(max_length=30, choices=UserRole.choices, default=UserRole.USER)
    is_approved = models.BooleanField(default=False, db_index=True)
    groups = models.ManyToManyField(
        Group, blank=True, related_name="price_user_set", related_query_name="price_user",
    )
    user_permissions = models.ManyToManyField(
        Permission, blank=True, related_name="price_user_set", related_query_name="price_user",
    )

    def save(self, *args, **kwargs):
        # Django's createsuperuser command sets these standard flags. Keep the
        # application-specific authorization fields in sync so the same
        # account can sign in to both /admin/ and the Priceora frontend.
        if self.is_superuser and self._state.adding:
            self.is_staff = True
            self.is_active = True
            self.role = UserRole.ADMIN
            self.requested_role = UserRole.ADMIN
            self.is_approved = True
        if self.full_name and not self.first_name:
            self.first_name = self.full_name
        super().save(*args, **kwargs)


class AppUser(TimeStampedModel):
    """
    Local user reference.

    If the project already uses Django authentication,
    user can optionally be linked to AUTH_USER_MODEL.

    external_user_id can store the Central Auth or employee-system ID.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    auth_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="price_intelligence_profile",
    )

    external_user_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
    )

    employee_code = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
    )

    name = models.CharField(max_length=255)

    email = models.EmailField(
        null=True,
        blank=True,
        db_index=True,
    )

    role = models.CharField(
        max_length=30,
        choices=UserRole.choices,
        default=UserRole.USER,
        db_index=True,
    )

    is_active = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "price_app_users"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_user_id"],
                condition=models.Q(external_user_id__isnull=False),
                name="unique_non_null_external_user_id",
            ),
            models.UniqueConstraint(
                fields=["employee_code"],
                condition=models.Q(employee_code__isnull=False),
                name="unique_non_null_employee_code",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.employee_code or self.email or self.id})"


# =========================================================
# PRODUCT MASTER
# =========================================================


class Category(TimeStampedModel):
    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_categories"
        ordering = ["name"]
        verbose_name_plural = "Categories"

    def __str__(self) -> str:
        return self.name


class SubCategory(TimeStampedModel):
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="subcategories",
    )

    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_subcategories"
        ordering = ["category__name", "name"]
        verbose_name_plural = "Subcategories"
        constraints = [
            models.UniqueConstraint(
                fields=["category", "name"],
                name="unique_subcategory_per_category",
            )
        ]

    def __str__(self) -> str:
        return f"{self.category.name} - {self.name}"


class Product(TimeStampedModel):
    """
    Local synchronized product master.

    stock_product_id stores the corresponding ID from Stock Vault.
    """

    stock_product_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
    )

    item_code = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
    )

    part_number = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
    )

    product_name = models.TextField()

    normalized_description = models.TextField(
        null=True,
        blank=True,
    )

    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
        null=True,
        blank=True,
    )

    subcategory = models.ForeignKey(
        SubCategory,
        on_delete=models.PROTECT,
        related_name="products",
        null=True,
        blank=True,
    )

    manufacturer = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    brand = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    unit_of_measurement = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )

    commodity_benchmark = models.ForeignKey(
        CommodityBenchmark,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mapped_products",
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_products"
        ordering = ["product_name"]
        indexes = [
            models.Index(
                fields=["item_code"],
                name="idx_product_item_code",
            ),
            models.Index(
                fields=["part_number"],
                name="idx_product_part_no",
            ),
            models.Index(
                fields=["brand"],
                name="idx_product_brand",
            ),
            models.Index(
                fields=["category", "is_active"],
                name="idx_product_category_active",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["stock_product_id"],
                condition=models.Q(stock_product_id__isnull=False),
                name="unique_non_null_stock_product_id",
            ),
        ]

    def __str__(self) -> str:
        code = self.item_code or self.part_number
        return f"{code} - {self.product_name}" if code else self.product_name


class Supplier(TimeStampedModel):
    stock_supplier_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
    )

    supplier_code = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
    )

    supplier_name = models.CharField(
        max_length=255,
        db_index=True,
    )

    normalized_supplier_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    country = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    default_currency = models.CharField(
        max_length=10,
        default="AED",
    )

    contact_person = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    email = models.EmailField(
        null=True,
        blank=True,
    )

    phone = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_suppliers"
        ordering = ["supplier_name"]
        indexes = [
            models.Index(
                fields=["normalized_supplier_name"],
                name="idx_supplier_normalized",
            ),
            models.Index(
                fields=["supplier_code"],
                name="idx_supplier_code",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["stock_supplier_id"],
                condition=models.Q(stock_supplier_id__isnull=False),
                name="unique_non_null_stock_supplier_id",
            ),
        ]

    def __str__(self) -> str:
        return self.supplier_name




class ProductSupplierAlias(TimeStampedModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="supplier_aliases",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_aliases",
    )
    supplier_item_code = models.CharField(max_length=150, null=True, blank=True)
    normalized_supplier_item_code = models.CharField(max_length=150, null=True, blank=True, db_index=True)
    supplier_part_number = models.CharField(max_length=150, null=True, blank=True)
    normalized_supplier_part_number = models.CharField(max_length=150, null=True, blank=True, db_index=True)
    supplier_description = models.TextField(null=True, blank=True)
    normalized_supplier_description = models.TextField(null=True, blank=True)
    first_seen_import_batch = models.ForeignKey(
        "ImportBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_seen_product_aliases",
    )
    last_seen_import_batch = models.ForeignKey(
        "ImportBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_seen_product_aliases",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_product_supplier_aliases"
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["supplier", "normalized_supplier_item_code"], name="idx_alias_supplier_item"),
            models.Index(fields=["supplier", "normalized_supplier_part_number"], name="idx_alias_supplier_part"),
        ]

    def __str__(self) -> str:
        return self.supplier_item_code or self.supplier_part_number or self.supplier_description or str(self.id)


# =========================================================
# FILE UPLOAD AND IMPORT
# =========================================================


def uploaded_file_path(instance: "UploadedFile", filename: str) -> str:
    extension = os.path.splitext(filename)[1].lower()
    date_path = timezone.now().strftime("%Y/%m/%d")
    unique_name = f"{uuid.uuid4().hex}{extension}"

    return f"price-imports/{date_path}/{unique_name}"


class UploadedFile(TimeStampedModel):
    original_file_name = models.CharField(max_length=500)
    stored_file_name = models.CharField(
        max_length=500,
        null=True,
        blank=True,
    )

    file = models.FileField(
        upload_to=uploaded_file_path,
    )

    mime_type = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    file_size = models.PositiveBigIntegerField(default=0)

    file_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )

    source_type = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    document_type = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    extraction_status = models.CharField(max_length=50, default="pending", db_index=True)

    uploaded_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_price_files",
    )

    uploaded_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    is_deleted = models.BooleanField(default=False)

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_uploaded_files"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(
                fields=["file_hash"],
                name="idx_uploaded_file_hash",
            ),
            models.Index(
                fields=["uploaded_at", "is_deleted"],
                name="idx_uploaded_file_date",
            ),
        ]

    def __str__(self) -> str:
        return self.original_file_name

    def calculate_hash(self) -> str:
        """
        Calculate SHA-256 hash for duplicate-file detection.
        """

        sha256 = hashlib.sha256()

        self.file.open("rb")

        try:
            for chunk in self.file.chunks():
                sha256.update(chunk)
        finally:
            self.file.close()

        return sha256.hexdigest()

    def save(self, *args, **kwargs):
        if self.file:
            self.original_file_name = (
                self.original_file_name or os.path.basename(self.file.name)
            )

            self.stored_file_name = os.path.basename(self.file.name)

            try:
                self.file_size = self.file.size
            except (OSError, ValueError):
                pass

        super().save(*args, **kwargs)


class ImportBatch(TimeStampedModel):
    batch_number = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    uploaded_file = models.ForeignKey(
        UploadedFile,
        on_delete=models.PROTECT,
        related_name="import_batches",
    )

    category = models.CharField(
        max_length=50,
        choices=ImportCategory.choices,
        db_index=True,
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_batches",
    )

    supplier_name_from_file = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    default_currency = models.CharField(
        max_length=10,
        default="AED",
    )

    remarks = models.TextField(
        null=True,
        blank=True,
    )

    document_metadata = models.JSONField(default=dict, blank=True)
    extraction_summary = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=40,
        choices=ImportBatchStatus.choices,
        default=ImportBatchStatus.UPLOADED,
        db_index=True,
    )

    total_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    warning_rows = models.PositiveIntegerField(default=0)

    uploaded_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_import_batches",
    )

    uploaded_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    processing_started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    error_message = models.TextField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_import_batches"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(
                fields=["category", "status"],
                name="idx_import_category_status",
            ),
            models.Index(
                fields=["supplier", "uploaded_at"],
                name="idx_import_supplier_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.batch_number} - {self.uploaded_file.original_file_name}"

    def save(self, *args, **kwargs):
        if not self.batch_number:
            date_part = timezone.now().strftime("%Y%m%d")
            unique_part = uuid.uuid4().hex[:8].upper()
            self.batch_number = f"IMP-{date_part}-{unique_part}"

        super().save(*args, **kwargs)

class BulkImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed With Errors"
    FAILED = "failed", "Failed"


class BulkImportFileStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    IMPORTED = "imported", "Imported"
    REVIEW_REQUIRED = "review_required", "Review Required"
    DUPLICATE = "duplicate", "Duplicate"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


def bulk_import_file_path(instance, filename):
    extension = os.path.splitext(filename)[1].lower()
    date_path = timezone.now().strftime("%Y/%m/%d")
    unique_name = f"{uuid.uuid4().hex}{extension}"
    return f"bulk-imports/{date_path}/{unique_name}"


class BulkImportBatch(TimeStampedModel):
    batch_number = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    original_upload_name = models.CharField(
        max_length=500,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=40,
        choices=BulkImportStatus.choices,
        default=BulkImportStatus.PENDING,
        db_index=True,
    )

    total_files = models.PositiveIntegerField(default=0)
    processed_files = models.PositiveIntegerField(default=0)
    imported_files = models.PositiveIntegerField(default=0)
    review_files = models.PositiveIntegerField(default=0)
    duplicate_files = models.PositiveIntegerField(default=0)
    failed_files = models.PositiveIntegerField(default=0)

    category = models.CharField(
        max_length=50,
        default="auto",
    )

    default_currency = models.CharField(
        max_length=10,
        blank=True,
        default="",
    )

    supplier_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    remarks = models.TextField(
        null=True,
        blank=True,
    )

    uploaded_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bulk_import_batches",
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_bulk_import_batches"
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.batch_number:
            date_part = timezone.now().strftime("%Y%m%d")
            unique_part = uuid.uuid4().hex[:8].upper()
            self.batch_number = f"BULK-{date_part}-{unique_part}"

        super().save(*args, **kwargs)

class PurchaseQuoteDownload(TimeStampedModel):
    file_name = models.CharField(max_length=500, unique=True, db_index=True)
    quote_date = models.DateTimeField(null=True, blank=True, db_index=True)
    local_path = models.TextField()
    file_extension = models.CharField(max_length=20, db_index=True)
    file_size = models.PositiveBigIntegerField(default=0)
    source_page = models.PositiveIntegerField(null=True, blank=True)
    source_row = models.PositiveIntegerField(null=True, blank=True)
    downloaded_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=30, default="downloaded", db_index=True)
    error_message = models.TextField(null=True, blank=True)
    file_hash = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    extraction_batch_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    extraction_status = models.CharField(max_length=30, default="downloaded", db_index=True)
    extraction_completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "price_purchase_quote_downloads"
        ordering = ["-downloaded_at"]

    def __str__(self):
        return self.file_name
    
class BulkImportFile(TimeStampedModel):
    bulk_batch = models.ForeignKey(
        BulkImportBatch,
        on_delete=models.CASCADE,
        related_name="files",
    )

    original_file_name = models.CharField(
        max_length=500,
    )

    file = models.FileField(
        upload_to=bulk_import_file_path,
    )

    file_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )

    status = models.CharField(
        max_length=40,
        choices=BulkImportFileStatus.choices,
        default=BulkImportFileStatus.PENDING,
        db_index=True,
    )

    progress = models.PositiveSmallIntegerField(default=0)

    progress_message = models.CharField(
        max_length=500,
        null=True,
        blank=True,
    )

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bulk_jobs",
    )

    review_reasons = models.JSONField(
        default=list,
        blank=True,
    )

    error_message = models.TextField(
        null=True,
        blank=True,
    )

    retry_count = models.PositiveSmallIntegerField(default=0)

    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_bulk_import_files"
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="idx_bulk_file_queue",
            ),
            models.Index(
                fields=["bulk_batch", "status"],
                name="idx_bulk_batch_status",
            ),
        ]
        
class ImportSheet(TimeStampedModel):
    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="sheets",
    )

    sheet_name = models.CharField(max_length=255)
    sheet_index = models.PositiveIntegerField(default=0)

    header_row_number = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )

    data_start_row_number = models.PositiveIntegerField(
        default=2,
        validators=[MinValueValidator(1)],
    )

    total_rows = models.PositiveIntegerField(default=0)

    selected_for_import = models.BooleanField(default=True)
    cached_headers = models.JSONField(null=True, blank=True, default=list)
    cached_rows = models.JSONField(null=True, blank=True, default=list)

    class Meta:
        db_table = "price_import_sheets"
        ordering = ["sheet_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["import_batch", "sheet_index"],
                name="unique_sheet_index_per_batch",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.import_batch.batch_number} - {self.sheet_name}"


class ImportColumnMapping(TimeStampedModel):
    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="column_mappings",
    )

    import_sheet = models.ForeignKey(
        ImportSheet,
        on_delete=models.CASCADE,
        related_name="column_mappings",
    )

    uploaded_column_name = models.CharField(max_length=255)

    uploaded_column_index = models.PositiveIntegerField()

    user_defined_column_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    system_field_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    is_ignored = models.BooleanField(default=False)

    is_required = models.BooleanField(default=False)

    mapping_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
    )

    created_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_column_mappings",
    )

    class Meta:
        db_table = "price_import_column_mappings"
        ordering = ["uploaded_column_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["import_sheet", "uploaded_column_index"],
                name="unique_column_mapping_per_sheet",
            ),
        ]

    def __str__(self) -> str:
        mapped_to = (
            "Ignored"
            if self.is_ignored
            else self.system_field_name or "Not Mapped"
        )

        return f"{self.uploaded_column_name} → {mapped_to}"


class ImportRow(TimeStampedModel):
    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="rows",
    )

    import_sheet = models.ForeignKey(
        ImportSheet,
        on_delete=models.CASCADE,
        related_name="rows",
    )

    row_number = models.PositiveIntegerField()

    raw_data = models.JSONField(default=dict)

    mapped_data = models.JSONField(
        default=dict,
        blank=True,
    )

    normalized_data = models.JSONField(
        default=dict,
        blank=True,
    )

    row_type = models.CharField(
        max_length=30,
        default="PRODUCT",
        db_index=True,
    )

    resolved_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_import_rows",
    )

    validation_status = models.CharField(
        max_length=30,
        choices=ImportRowStatus.choices,
        default=ImportRowStatus.PENDING,
        db_index=True,
    )

    validation_message = models.TextField(
        null=True,
        blank=True,
    )

    validation_errors = models.JSONField(
        default=list,
        blank=True,
    )

    validation_warnings = models.JSONField(
        default=list,
        blank=True,
    )

    is_duplicate = models.BooleanField(default=False)
    duplicate_reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    is_imported = models.BooleanField(default=False)

    imported_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_import_rows"
        ordering = ["row_number"]
        indexes = [
            models.Index(
                fields=["import_batch", "validation_status"],
                name="idx_import_row_batch_status",
            ),
            models.Index(
                fields=["import_sheet", "row_number"],
                name="idx_import_row_sheet_number",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["import_sheet", "row_number"],
                name="unique_row_number_per_sheet",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.import_batch.batch_number} - "
            f"{self.import_sheet.sheet_name} - Row {self.row_number}"
        )


# =========================================================
# PRODUCT MATCHING
# =========================================================


class ProductMatchResult(TimeStampedModel):
    import_row = models.OneToOneField(
        ImportRow,
        on_delete=models.CASCADE,
        related_name="product_match",
    )

    imported_item_code = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    imported_part_number = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    imported_description = models.TextField(
        null=True,
        blank=True,
    )

    imported_manufacturer = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    imported_brand = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    suggested_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suggested_import_matches",
    )

    confirmed_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_import_matches",
    )

    match_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
    )

    match_method = models.CharField(
        max_length=50,
        choices=ProductMatchMethod.choices,
        default=ProductMatchMethod.NONE,
    )

    status = models.CharField(
        max_length=30,
        choices=ProductMatchStatus.choices,
        default=ProductMatchStatus.UNMATCHED,
        db_index=True,
    )

    confirmed_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_product_matches",
    )

    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    remarks = models.TextField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_product_match_results"
        ordering = ["-match_score"]
        indexes = [
            models.Index(
                fields=["status", "match_score"],
                name="idx_product_match_status_score",
            ),
        ]

    def __str__(self) -> str:
        return (
            self.imported_description
            or self.imported_item_code
            or f"Import row {self.import_row_id}"
        )


# =========================================================
# PRICE HISTORY
# =========================================================

class GeminiUsageLog(TimeStampedModel):
    import_batch = models.ForeignKey(
        "ImportBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gemini_usage_logs",
    )
    model_name = models.CharField(max_length=100)
    task_name = models.CharField(max_length=100, default="document_extraction")
    prompt_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    request_metadata = models.JSONField(default=dict, blank=True)
    response_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "price_gemini_usage_logs"
        ordering = ["-created_at"]


class ExtractionProfile(TimeStampedModel):
    """Reviewed, data-driven layout knowledge for deterministic extraction."""
    name = models.CharField(max_length=200)
    document_type = models.CharField(max_length=50, null=True, blank=True)
    supplier_pattern = models.CharField(max_length=500, null=True, blank=True)
    required_anchors = models.JSONField(default=list, blank=True)
    optional_anchors = models.JSONField(default=list, blank=True)
    header_aliases = models.JSONField(default=dict, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    parser_type = models.CharField(max_length=30, default="table")
    confidence_threshold = models.FloatField(default=0.90)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "price_extraction_profiles"
        ordering = ["name", "-updated_at"]
        indexes = [models.Index(fields=["active", "document_type"])]

    def __str__(self):
        return self.name
        
class PriceHistory(TimeStampedModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="price_history",
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="price_history",
    )

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="price_records",
    )

    import_row = models.OneToOneField(
        ImportRow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="price_record",
    )

    source_type = models.CharField(
        max_length=50,
        choices=PriceSourceType.choices,
        db_index=True,
    )

    source_reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    source_file_name = models.CharField(
        max_length=500,
        null=True,
        blank=True,
    )

    purchase_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
    )

    quantity = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        default=Decimal("1.0000"),
        validators=[MinValueValidator(Decimal("0.0001"))],
    )

    unit_of_measurement = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )

    original_unit_price = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001"))],
    )

    currency = models.CharField(
        max_length=10,
        default="AED",
        db_index=True,
    )

    exchange_rate = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("1.00000000"),
        validators=[MinValueValidator(Decimal("0.00000001"))],
    )

    base_currency = models.CharField(
        max_length=10,
        default="AED",
    )

    converted_unit_price = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001"))],
    )

    purchase_location = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    quotation_validity_date = models.DateField(
        null=True,
        blank=True,
    )

    lpo_reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    grn_reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    quotation_reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )

    remarks = models.TextField(
        null=True,
        blank=True,
    )

    is_latest = models.BooleanField(
        default=False,
        db_index=True,
    )

    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_price_records",
    )

    class Meta:
        db_table = "price_history"
        ordering = [
            "-purchase_date",
            "-created_at",
        ]
        indexes = [
            models.Index(
                fields=["product", "-purchase_date"],
                name="idx_price_product_date",
            ),
            models.Index(
                fields=["product", "is_latest"],
                name="idx_price_product_latest",
            ),
            models.Index(
                fields=["supplier", "-purchase_date"],
                name="idx_price_supplier_date",
            ),
            models.Index(
                fields=["source_type", "source_reference"],
                name="idx_price_source_reference",
            ),
            models.Index(
                fields=["product", "converted_unit_price"],
                name="idx_price_product_amount",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.product.product_name} - "
            f"{self.currency} {self.original_unit_price}"
        )

    def save(self, *args, **kwargs):
        if self.exchange_rate and self.original_unit_price:
            self.converted_unit_price = (
                self.original_unit_price * self.exchange_rate
            )

        super().save(*args, **kwargs)



# =========================================================
# COLUMN MAPPING TEMPLATES
# =========================================================


class MappingTemplate(TimeStampedModel):
    template_name = models.CharField(max_length=255)
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mapping_templates",
    )
    import_category = models.CharField(
        max_length=50,
        choices=ImportCategory.choices,
        db_index=True,
    )
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_mapping_templates",
    )

    class Meta:
        db_table = "price_mapping_templates"
        ordering = ["template_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["template_name", "supplier", "import_category"],
                name="unique_template_supplier_category",
            )
        ]

    def __str__(self) -> str:
        return self.template_name


class MappingTemplateField(TimeStampedModel):
    template = models.ForeignKey(
        MappingTemplate,
        on_delete=models.CASCADE,
        related_name="fields",
    )
    original_uploaded_column_name = models.CharField(max_length=255)
    normalized_uploaded_column_name = models.CharField(max_length=255, db_index=True)
    system_field_name = models.CharField(max_length=100)
    original_column_position = models.PositiveIntegerField()
    is_required = models.BooleanField(default=False)

    class Meta:
        db_table = "price_mapping_template_fields"
        ordering = ["original_column_position"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "original_column_position"],
                name="unique_template_column_position",
            )
        ]

    def __str__(self) -> str:
        return f"{self.original_uploaded_column_name} -> {self.system_field_name}"


# =========================================================
# BID COSTING
# =========================================================


class BidCosting(TimeStampedModel):
    bid_costing_number = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    title = models.CharField(max_length=255)

    project_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    customer_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    location = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    currency = models.CharField(
        max_length=10,
        default="AED",
    )

    remarks = models.TextField(
        null=True,
        blank=True,
    )


    status = models.CharField(
        max_length=30,
        choices=BidCostingStatus.choices,
        default=BidCostingStatus.DRAFT,
        db_index=True,
    )

    material_subtotal = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    freight = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    other_charges = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    tax_percentage = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        default=Decimal("0.0000"),
    )

    tax_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    grand_total = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    created_by = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_bid_costings",
    )

    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "price_bid_costings"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "-created_at"],
                name="idx_bid_status_date",
            ),
            models.Index(
                fields=["created_by", "-created_at"],
                name="idx_bid_creator_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.bid_costing_number} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.bid_costing_number:
            date_part = timezone.now().strftime("%Y%m%d")
            unique_part = uuid.uuid4().hex[:6].upper()
            self.bid_costing_number = f"BID-{date_part}-{unique_part}"

        self.calculate_totals(save=False)

        super().save(*args, **kwargs)

    def calculate_totals(self, save: bool = True) -> None:
        """
        Recalculate bid totals.

        For a newly created object without a primary key,
        material subtotal remains zero until items are added.
        """

        if self.pk:
            subtotal = self.items.aggregate(
                total=models.Sum("line_total")
            )["total"]

            self.material_subtotal = subtotal or Decimal("0.00")

        taxable_amount = (
            self.material_subtotal
            + self.freight
            + self.other_charges
        )

        self.tax_amount = (
            taxable_amount
            * self.tax_percentage
            / Decimal("100")
        ).quantize(Decimal("0.01"))

        self.grand_total = (
            taxable_amount + self.tax_amount
        ).quantize(Decimal("0.01"))

        if save and self.pk:
            BidCosting.objects.filter(pk=self.pk).update(
                material_subtotal=self.material_subtotal,
                tax_amount=self.tax_amount,
                grand_total=self.grand_total,
                updated_at=timezone.now(),
            )


class BidCostingItem(TimeStampedModel):
    bid_costing = models.ForeignKey(
        BidCosting,
        on_delete=models.CASCADE,
        related_name="items",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    commodity = models.ForeignKey(
        CommodityBenchmark,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    commodity_price_history = models.ForeignKey(
        CommodityPriceHistory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    selected_price = models.ForeignKey(
        PriceHistory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    source_import_row = models.ForeignKey(
        ImportRow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_costing_items",
    )

    # Snapshot fields preserve the original bid information.
    product_name_snapshot = models.TextField()

    item_code_snapshot = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    part_number_snapshot = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    supplier_name_snapshot = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    source_type_snapshot = models.CharField(max_length=50)

    source_reference_snapshot = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    source_file_name_snapshot = models.CharField(
        max_length=500,
        null=True,
        blank=True,
    )

    quantity = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
    )

    unit_of_measurement = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )

    unit_rate = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001"))],
    )

    currency = models.CharField(
        max_length=10,
        default="AED",
    )

    exchange_rate = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("1.00000000"),
        validators=[MinValueValidator(Decimal("0.00000001"))],
    )

    converted_unit_rate = models.DecimalField(
        max_digits=20,
        decimal_places=6,
    )

    line_total = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    pricing_source_type = models.CharField(max_length=50, null=True, blank=True)
    source_provider = models.CharField(max_length=50, null=True, blank=True)
    market_rate = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    market_unit = models.CharField(max_length=30, null=True, blank=True)
    market_currency = models.CharField(max_length=10, null=True, blank=True)
    market_date = models.DateField(null=True, blank=True)
    final_confirmed_rate = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    requires_review = models.BooleanField(default=False)

    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "price_bid_costing_items"
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(
                fields=["bid_costing", "sort_order"],
                name="idx_bid_item_sort",
            ),
            models.Index(
                fields=["product"],
                name="idx_bid_item_product",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.bid_costing.bid_costing_number} - "
            f"{self.product_name_snapshot}"
        )

    def save(self, *args, **kwargs):
        if self.product_id:
            self.product_name_snapshot = (
                self.product_name_snapshot
                or self.product.product_name
            )

            self.item_code_snapshot = (
                self.item_code_snapshot
                or self.product.item_code
            )

            self.part_number_snapshot = (
                self.part_number_snapshot
                or self.product.part_number
            )

            self.unit_of_measurement = (
                self.unit_of_measurement
                or self.product.unit_of_measurement
            )

        if self.supplier_id:
            self.supplier_name_snapshot = (
                self.supplier_name_snapshot
                or self.supplier.supplier_name
            )

        if self.selected_price_id:
            price = self.selected_price

            self.source_type_snapshot = (
                self.source_type_snapshot
                or price.source_type
            )

            self.source_reference_snapshot = (
                self.source_reference_snapshot
                or price.source_reference
            )

            self.source_file_name_snapshot = (
                self.source_file_name_snapshot
                or price.source_file_name
            )

        self.converted_unit_rate = (
            self.unit_rate * self.exchange_rate
        )

        self.line_total = (
            self.quantity * self.converted_unit_rate
        ).quantize(Decimal("0.01"))

        super().save(*args, **kwargs)

        self.bid_costing.calculate_totals()

    def delete(self, *args, **kwargs):
        bid_costing = self.bid_costing

        result = super().delete(*args, **kwargs)

        bid_costing.calculate_totals()

        return result
