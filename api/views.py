from django.db.models import Count, Max, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.core.paginator import Paginator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.encoding import force_bytes
from rest_framework.authtoken.models import Token
from rest_framework.viewsets import ModelViewSet
import re
import mimetypes
from pathlib import Path
from django.utils import timezone
from api.constants import SYSTEM_FIELDS
from api.exceptions import ImportServiceError
from api.models import PriceUser, UserRole, BidCosting, BidCostingItem, CommodityBenchmark, CommodityPriceHistory, ImportBatch, ImportRow, ImportRowStatus, ImportSheet, MappingTemplate, Product, ProductMatchStatus, Supplier,ProductMatchResult,PriceHistory,BulkImportBatch,BulkImportFile,BulkImportFileStatus,PurchaseQuoteDownload
from api.serializers.import_serializers import HeaderSerializer, ImportRowUpdateSerializer, MappingSerializer, MetadataUpdateSerializer, PreviewSerializer, SaveTemplateSerializer, TemplateSerializer, UploadSerializer
from api.services.import_service import (
    ImportService,
    resolve_import_metadata,
    build_review_reasons,
)
from api.services.mapping_service import MappingService
from api.services.parser_service import ParserService, VIRTUAL_HEADERS
from api.services.sheet_service import SheetService
from api.services.template_service import TemplateService
from api.services.upload_service import UploadService
from api.services.normalization import normalize_supplier
from api.services.market_price_service import HRC_SYMBOL, YAHOO_PROVIDER, YahooHRCMarketPriceService, fetch_current_market_prices
from api.services.product_normalization_service import clean_display_identity, line_item_group_key, normalize_product_text, rate_matrix_display_name, sanitize_product_identity
from decimal import Decimal, InvalidOperation
import logging
import time
from api.services.bulk_import_service import BulkImportService
from api.services.purchase_auto_extraction_service import PurchaseAutoExtractionService

logger = logging.getLogger(__name__)


def _history_document_label(value):
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return {
        "email quote": "EMAIL_QUOTE",
        "emailquote": "EMAIL_QUOTE",
        "quotation": "QUOTATION",
        "quote": "QUOTATION",
        "price list": "PRICE_LIST",
        "price list quotation": "PRICE_LIST",
        "invoice": "INVOICE",
        "purchase order": "PURCHASE_ORDER",
        "other": "OTHER",
    }.get(normalized, str(value or "").strip().upper())

def ok(data=None, message="OK", code=status.HTTP_200_OK):
    payload = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    return Response(payload, status=code)

def fail(exc):
    return Response({"success": False, "message": exc.message, "errors": exc.errors}, status=exc.status_code)


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        name = str(data.get("full_name") or "").strip()
        email = str(data.get("email") or "").strip().lower()
        employee_id = str(data.get("employee_id") or "").strip()
        role = str(data.get("request_role") or "user").lower()
        password = str(data.get("password") or "")
        if role == UserRole.ADMIN:
            return Response({"success": False, "message": "Admin registration is not available."}, status=400)
        if not name or not email or not employee_id or role not in UserRole.values or not password:
            return Response({"success": False, "message": "Full name, email, employee ID, role and password are required."}, status=400)
        if password != str(data.get("confirm_password") or ""):
            return Response({"success": False, "message": "Passwords do not match."}, status=400)
        User = get_user_model()
        try:
            validate_password(password, User(username=email, email=email, full_name=name))
        except ValidationError as exc:
            return Response({"success": False, "message": " ".join(exc.messages), "errors": {"password": exc.messages}}, status=400)
        if User.objects.filter(username=email).exists() or User.objects.filter(email__iexact=email).exists():
            return Response({"success": False, "message": "Email is already registered."}, status=400)
        if User.objects.filter(employee_code=employee_id).exists():
            return Response({"success": False, "message": "Employee ID is already registered."}, status=400)
        auth_user = User.objects.create_user(
            username=email, email=email, password=password, first_name=name,
            full_name=name, employee_code=employee_id,
            requested_role=role, role=role, is_active=False, is_approved=False,
        )
        return ok(message="Registration submitted. An authorised administrator must approve your account.", code=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        identifier = str(request.data.get("identifier") or request.data.get("email") or "").strip()
        password = request.data.get("password") or ""
        User = get_user_model()
        candidate = User.objects.filter(Q(email__iexact=identifier) | Q(employee_code__iexact=identifier)).first()
        if candidate and candidate.is_deleted:
            return Response({"success": False, "message": "This account is no longer available."}, status=403)
        if candidate and (not candidate.is_approved or not candidate.is_active) and candidate.check_password(password):
            return Response({"success": False, "message": "Your account is waiting for approval. Please contact an administrator."}, status=403)
        user = authenticate(request=request, login_identifier=identifier, password=password)
        profile = user if user else None
        if user and user.is_superuser:
            profile.role = UserRole.ADMIN
            profile.is_approved = True
            profile.save(update_fields=["role", "is_approved"])
        if not profile:
            return Response({"success": False, "message": "Invalid email/employee code or password."}, status=401)
        if not profile.is_approved or not user.is_active:
            return Response({"success": False, "message": "Your account is waiting for approval."}, status=403)
        token, _ = Token.objects.get_or_create(user=user)
        return ok({"token": token.key, "user": {"id": str(profile.id), "name": profile.full_name or profile.get_full_name() or profile.username, "email": profile.email, "employee_id": profile.employee_code, "role": profile.role}})


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        user = get_user_model().objects.filter(email__iexact=email, is_deleted=False).first()
        if user and user.is_active:
            from django.conf import settings
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            frontend = getattr(settings, "FRONTEND_URL", "http://localhost:5173").rstrip("/")
            reset_url = f"{frontend}/reset-password/{uid}/{token}"
            send_mail("Reset your Priceora password", f"Use this link to set a new password:\n\n{reset_url}\n\nThis link expires after use.", settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
        return ok(message="If an account exists for this email, a password reset link has been sent.")


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            user = get_user_model().objects.get(pk=urlsafe_base64_decode(request.data.get("uid")).decode())
        except Exception:
            user = None
        password = str(request.data.get("password") or "")
        if not user or not default_token_generator.check_token(user, request.data.get("token") or ""):
            return Response({"success": False, "message": "This reset link is invalid or expired."}, status=400)
        if password != str(request.data.get("confirm_password") or ""):
            return Response({"success": False, "message": "Passwords do not match."}, status=400)
        try:
            validate_password(password, user)
        except ValidationError as exc:
            return Response({"success": False, "message": " ".join(exc.messages), "errors": {"password": exc.messages}}, status=400)
        user.set_password(password)
        user.save(update_fields=["password"])
        return ok(message="Password reset successfully. You can now sign in.")


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if not user.check_password(request.data.get("current_password") or ""):
            return Response({"success": False, "message": "Current password is incorrect."}, status=400)
        password = str(request.data.get("password") or "")
        if password != str(request.data.get("confirm_password") or ""):
            return Response({"success": False, "message": "Passwords do not match."}, status=400)
        try:
            validate_password(password, user)
        except ValidationError as exc:
            return Response({"success": False, "message": " ".join(exc.messages), "errors": {"password": exc.messages}}, status=400)
        user.set_password(password)
        user.save(update_fields=["password"])
        Token.objects.filter(user=user).delete()
        return ok(message="Password changed successfully. Please sign in again.")


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = request.user
        from api.user_management import user_data
        return ok(user_data(profile))

    def patch(self, request):
        from api.user_management import user_data
        user = request.user
        name = str(request.data.get("full_name") or "").strip()
        email = str(request.data.get("email") or "").strip().lower()
        employee_code = str(request.data.get("employee_code") or "").strip()
        if not name or not email or not employee_code:
            return Response({"success": False, "message": "Name, email and employee code are required."}, status=400)
        if get_user_model().objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            return Response({"success": False, "message": "Email is already registered."}, status=400)
        if get_user_model().objects.filter(employee_code__iexact=employee_code).exclude(pk=user.pk).exists():
            return Response({"success": False, "message": "Employee code is already registered."}, status=400)
        user.full_name = name
        user.first_name = name
        user.email = email
        user.username = email
        user.employee_code = employee_code
        user.save(update_fields=["full_name", "first_name", "email", "username", "employee_code"])
        return ok(user_data(user), message="Profile updated.")


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return ok(message="Signed out.")


from api.user_management import UserManagementView


def _money(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
def _fix_divided_single_visible_amount(data):
    """
    Preserve extracted/mapped commercial values exactly.
    Display/API code must never repair prices heuristically.
    """
    return dict(data or {})
def _line_item_display_data(data):
    cleaned = _fix_divided_single_visible_amount(data)
    from api.services.commercial_attribute_policy import hidden_display_attribute as commercial_field, clean_commercial_display
    cleaned = clean_commercial_display(cleaned)

    def clean(value):
        return re.sub(r"\s+", " ", str(value or "")).strip(" |:-")

    def add(parts, label, value):
        label = clean(label)
        value = clean(value)

        if not label or not value:
            return

        text = f"{label}: {value}"
        existing = " | ".join(parts).lower()

        if text.lower() in existing:
            return

        parts.append(text)

    product_name = clean(
        cleaned.get("product_name")
        or cleaned.get("description")
        or ""
    )

    parent = clean(cleaned.get("parent_category"))
    extra_attributes = cleaned.get("extra_attributes") or []

    parts = []

    if parent and product_name and parent.lower() not in product_name.lower():
        parts.append(parent)

    if product_name:
        parts.append(product_name)

    standard_attributes = [
        ("Brand", cleaned.get("brand")),
        ("Model", cleaned.get("model")),
        ("Specification", cleaned.get("specification")),
        ("Remark", cleaned.get("remark")),
        ("Note", cleaned.get("note")),
    ]

    has_structured_context = bool(
        parent
        or extra_attributes
    )

    if not has_structured_context:
        standard_attributes = []

    supplier_values = {
        clean(cleaned.get("supplier")).lower(),
        clean(cleaned.get("supplier_name")).lower(),
    }

    product_lower = product_name.lower()

    for label, value in standard_attributes:
        value_text = clean(value)

        if not value_text:
            continue

        value_lower = value_text.lower()

        if label == "Brand" and value_lower in supplier_values:
            continue

        if value_lower and value_lower in product_lower:
            continue

        add(parts, label, value_text)

    hidden_display_attribute_labels = {
        "rate basis",
        "additional rate",
        "applies after",
    }

    for attr in extra_attributes:
        if not isinstance(attr, dict):
            continue

        label_text = clean(attr.get("label"))
        value_text = clean(attr.get("value"))

        if commercial_field(label_text) or label_text.lower() in hidden_display_attribute_labels:
            continue

        if value_text and value_text.lower() in product_lower:
            continue

        add(parts, label_text, value_text)

    # A persisted normalized display_name is authoritative.  Reconstructing
    # it here can resurrect stale/shortened product identities.
    stored_display_name = clean(cleaned.get("display_name"))
    display_name = stored_display_name or " | ".join(parts) or product_name

    cleaned["display_name"] = display_name
    cleaned["description"] = display_name

    return cleaned
def _merged_line_item_data(mapped_data, normalized_data):
    # normalized_data is the latest persisted extraction/edit state.  Older
    # mapped_data must never overwrite its source identity or attributes.
    return {**(mapped_data or {}), **(normalized_data or {})}

def _import_row_display_data(row):
    if not row:
        return {}

    return _line_item_display_data(
        ImportService()._row_import_data(row)
    )

def costing_amount(data):
    """
    Resolve commercial amount without inventing hidden per-unit rates.
    """

    unit_price = _money(data.get("unit_price"))
    line_total = _money(data.get("line_total"))

    if unit_price is not None:
        return str(unit_price)

    if line_total is not None:
        return str(line_total)

    return None

class SystemFieldsView(APIView):
    permission_classes = [AllowAny]

    def _save_items(self, bid, lines):
        BidCostingItem.objects.filter(bid_costing=bid).delete()
        objects = []
        for index, line in enumerate(lines):
            quantity = _to_decimal(line.get("quantity"), "1")
            unit_rate = _to_decimal(line.get("unitRate"))
            line_total = (quantity * unit_rate).quantize(Decimal("0.01"))
            source_row = None
            row_id = line.get("rowId")
            if row_id:
                source_row = ImportRow.objects.filter(id=row_id).first()
            commodity = None
            commodity_price = None
            if line.get("pricingSourceType") == "MARKET_BENCHMARK":
                commodity = CommodityBenchmark.objects.filter(id=line.get("commodityId"), is_active=True).first()
                commodity_price = CommodityPriceHistory.objects.filter(id=line.get("commodityPriceHistoryId"), benchmark=commodity).first() if commodity else None
            product = getattr(source_row, "resolved_product", None)
            if not product and commodity:
                product = Product.objects.filter(commodity_benchmark=commodity, is_active=True).first()
            objects.append(BidCostingItem(
                bid_costing=bid,
                product=product,
                commodity=commodity,
                commodity_price_history=commodity_price,
                source_import_row=source_row,
                supplier=getattr(getattr(source_row, "import_batch", None), "supplier", None),
                product_name_snapshot=line.get("product") or "Unnamed item",
                item_code_snapshot=line.get("itemCode") or "",
                part_number_snapshot=line.get("partNumber") or "",
                supplier_name_snapshot=line.get("supplier") or "",
                source_type_snapshot=line.get("source") or "",
                source_reference_snapshot=line.get("sourceReference") or "",
                source_file_name_snapshot=line.get("sourceFile") or "",
                quantity=quantity,
                unit_of_measurement=line.get("unit") or "",
                unit_rate=unit_rate,
                currency=line.get("currency") or (bid.currency if bid.currency != "MIXED" else "AED"),
                exchange_rate=Decimal("1.00000000"),
                converted_unit_rate=unit_rate,
                line_total=line_total,
                pricing_source_type=line.get("pricingSourceType") or line.get("source"),
                source_provider=line.get("sourceProvider"),
                market_rate=_to_decimal(line.get("marketRate")) if line.get("marketRate") not in (None, "") else None,
                market_unit=line.get("marketUnit"),
                market_currency=line.get("marketCurrency"),
                market_date=line.get("marketDate") or None,
                final_confirmed_rate=unit_rate,
                requires_review=bool(line.get("requiresReview")),
                sort_order=index,
            ))
        BidCostingItem.objects.bulk_create(objects)

    def _apply_totals(self, bid, charges):
        bid.calculate_totals()
        bid.freight = _to_decimal(charges.get("freight"))
        bid.other_charges = _to_decimal(charges.get("other"))
        bid.tax_amount = _to_decimal(charges.get("tax"))
        bid.grand_total = bid.material_subtotal + bid.freight + bid.other_charges + bid.tax_amount
        BidCosting.objects.filter(id=bid.id).update(
            material_subtotal=bid.material_subtotal,
            freight=bid.freight,
            other_charges=bid.other_charges,
            tax_amount=bid.tax_amount,
            grand_total=bid.grand_total,
        )

    def _currency_for_lines(self, lines, fallback="AED"):
        line_currencies = {str(line.get("currency") or "").upper() for line in lines if line.get("currency")}
        if len(line_currencies) == 1:
            return next(iter(line_currencies))
        if len(line_currencies) > 1:
            return "MIXED"
        return fallback or "AED"


    def get(self, request):
        return ok(SYSTEM_FIELDS)

class UploadView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        serializer = UploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        supplier = Supplier.objects.filter(id=data.get("supplier_id")).first() if data.get("supplier_id") else None
        user = getattr(request.user, "price_intelligence_profile", None) if request.user.is_authenticated else None
        try:
            result = UploadService().upload(file_obj=data["file"], category=data["category"], supplier=supplier, supplier_name=data.get("supplier_name", ""), default_currency=data["default_currency"], remarks=data.get("remarks", ""), user=user)
            return ok(result, "File uploaded.", status.HTTP_201_CREATED)
        except ImportServiceError as exc:
            return fail(exc)

class BulkUploadView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        active_batch = (
            BulkImportBatch.objects
            .filter(
                status__in=[
                    "pending",
                    "processing",
                ]
            )
            .order_by("-created_at")
            .first()
        )

        if active_batch:
            return Response(
                {
                    "success": False,
                    "message": (
                        "A bulk import is already processing. "
                        "Please wait until it finishes before uploading another batch."
                    ),
                    "data": {
                        "bulk_batch_id": active_batch.id,
                        "batch_number": active_batch.batch_number,
                        "status": active_batch.status,
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        files = request.FILES.getlist("files")

        if not files:
            single_file = request.FILES.get("file")
            if single_file:
                files = [single_file]

        if not files:
            return Response(
                {
                    "success": False,
                    "message": "No files uploaded.",
                    "errors": {
                        "files": [
                            "Select files or upload a ZIP file."
                        ]
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        category = (
            request.data.get("category")
            or "auto"
        )

        supplier_name = (
            request.data.get("supplier_name")
            or ""
        )

        default_currency = (
            request.data.get("default_currency")
            or ""
        )

        remarks = (
            request.data.get("remarks")
            or ""
        )

        user = (
            getattr(
                request.user,
                "price_intelligence_profile",
                None,
            )
            if request.user.is_authenticated
            else None
        )

        try:
            result = BulkImportService().create_bulk_batch(
                files=files,
                category=category,
                supplier_name=supplier_name,
                default_currency=default_currency,
                remarks=remarks,
                user=user,
            )

            return ok(
                result,
                "Bulk upload queued.",
                status.HTTP_202_ACCEPTED,
            )

        except ImportServiceError as exc:
            return fail(exc)


class BulkImportStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, bulk_batch_id):
        batch = get_object_or_404(
            BulkImportBatch,
            id=bulk_batch_id,
        )
        result = BulkImportService().status(batch)

        # Purchase Software archive uploads create child jobs for the files
        # inside a ZIP/RAR. The parent download has no matching child filename,
        # so close the parent when the batch reaches a terminal state. This
        # keeps the archive out of the next extraction candidate list even if
        # one child needs review.
        if batch.status in {
            "completed",
            "completed_with_errors",
            "failed",
        }:
            parent_status = "failed" if batch.status == "failed" else "extracted"
            PurchaseQuoteDownload.objects.filter(
                extraction_batch_id=batch.id,
                extraction_status="queued",
            ).update(
                extraction_status=parent_status,
                error_message=(
                    "Archive extraction failed."
                    if parent_status == "failed"
                    else None
                ),
                extraction_completed_at=timezone.now(),
                updated_at=timezone.now(),
            )

        return ok(result)


class BulkReviewListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        started_at = time.perf_counter()
        if request.query_params.get("count_only") == "1":
            return ok({"count": BulkImportFile.objects.filter(
                status=BulkImportFileStatus.REVIEW_REQUIRED,
            ).count()})
        jobs = (
            BulkImportFile.objects
            .filter(
                status=BulkImportFileStatus.REVIEW_REQUIRED
            )
            .select_related(
                "bulk_batch",
                "import_batch",
                "import_batch__uploaded_file",
                "import_batch__supplier",
            )
            .prefetch_related(
                "import_batch__rows",
            )
            .order_by("-created_at")
        )

        jobs = list(jobs)
        jobs_loaded_at = time.perf_counter()
        result = []

        for job in jobs:
            batch = job.import_batch

            metadata = (
                resolve_import_metadata(batch)
                if batch
                else {}
            )

            result.append({
                "id": job.id,
                "bulk_batch_id": job.bulk_batch_id,
                "bulk_batch_number": (
                    job.bulk_batch.batch_number
                ),
                "file_name": job.original_file_name,
                "import_batch_id": job.import_batch_id,
                "review_reasons": (
                    [reason for reason in (job.review_reasons or [])
                     if reason != "Currency missing" or not batch
                     or "Currency missing" in build_review_reasons(batch)]
                ),
                "created_at": job.created_at,

                "source_file": (
                    batch.uploaded_file.original_file_name
                    if batch
                    else job.original_file_name
                ),

                "supplier_name": (
                    metadata.get("supplier_name")
                    or ""
                ),

                "document_date": (
                    metadata.get("document_date")
                    or ""
                ),

                "category": (
                    metadata.get("category")
                    or ""
                ),

                "currency": (
                    metadata.get("currency")
                    or ""
                ),
            })

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "bulk_review_debug jobs=%s query_load_ms=%.1f total_ms=%.1f",
            len(result),
            (jobs_loaded_at - started_at) * 1000,
            elapsed_ms,
        )
        if elapsed_ms >= 1000:
            logger.warning(
                "SLOW_BULK_REVIEW jobs=%s total_ms=%.1f",
                len(result),
                elapsed_ms,
            )
        return ok(result)
class BulkReviewSkipView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, job_id):
        job = get_object_or_404(
            BulkImportFile,
            id=job_id,
            status=BulkImportFileStatus.REVIEW_REQUIRED,
        )

        job.status = BulkImportFileStatus.SKIPPED
        job.progress = 100
        job.progress_message = "Skipped by user"
        job.completed_at = timezone.now()

        job.save(
            update_fields=[
                "status",
                "progress",
                "progress_message",
                "completed_at",
                "updated_at",
            ]
        )

        bulk_batch = job.bulk_batch

        bulk_batch.review_files = bulk_batch.files.filter(
            status=BulkImportFileStatus.REVIEW_REQUIRED
        ).count()

        bulk_batch.processed_files = bulk_batch.files.exclude(
            status__in=[
                BulkImportFileStatus.PENDING,
                BulkImportFileStatus.PROCESSING,
            ]
        ).count()

        bulk_batch.save(
            update_fields=[
                "review_files",
                "processed_files",
                "updated_at",
            ]
        )

        return ok(
            {
                "job_id": job.id,
                "status": job.status,
            },
            "Review item skipped.",
        )
class BulkReviewImportView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, job_id):
        job = get_object_or_404(
            BulkImportFile.objects.select_related(
                "import_batch"
            ),
            id=job_id,
        )

        if not job.import_batch_id:
            return Response(
                {
                    "success": False,
                    "message": "This review item has no import batch.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = job.import_batch

        metadata = resolve_import_metadata(
            batch
        )

        row_payloads = [
            {
                **(row.raw_data or {}),
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }
            for row in batch.rows.all() if row.row_type != "IGNORE"
        ]
        from api.services.row_metadata_policy import missing_row_metadata
        missing_metadata = missing_row_metadata(row_payloads, metadata)
        from api.services.gemini_extraction_service import _valid_currency
        has_row_currency = bool(row_payloads) and all(_valid_currency(row.get("currency")) for row in row_payloads)

        if missing_metadata['date']:
            return Response(
                {
                    "success": False,
                    "message": "Document date is required before import.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if missing_metadata['currency']:
            return Response(
                {
                    "success": False,
                    "message": "Currency is required before import.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        sheet_ids = list(
            batch.sheets.values_list(
                "id",
                flat=True,
            )
        )

        try:
            result = ImportService().start(
                batch,
                sheet_ids,
                review_confirmed=True,
            )

            job.status = BulkImportFileStatus.IMPORTED
            job.progress = 100
            job.progress_message = "Imported after review"
            job.review_reasons = []
            job.completed_at = timezone.now()

            job.save(
                update_fields=[
                    "status",
                    "progress",
                    "progress_message",
                    "review_reasons",
                    "completed_at",
                    "updated_at",
                ]
            )
            bulk_batch = job.bulk_batch

            bulk_batch.imported_files = bulk_batch.files.filter(
                status=BulkImportFileStatus.IMPORTED
            ).count()

            bulk_batch.review_files = bulk_batch.files.filter(
                status=BulkImportFileStatus.REVIEW_REQUIRED
            ).count()

            bulk_batch.failed_files = bulk_batch.files.filter(
                status=BulkImportFileStatus.FAILED
            ).count()

            bulk_batch.duplicate_files = bulk_batch.files.filter(
                status=BulkImportFileStatus.DUPLICATE
            ).count()

            bulk_batch.processed_files = (
                bulk_batch.imported_files
                + bulk_batch.review_files
                + bulk_batch.failed_files
                + bulk_batch.duplicate_files
            )

            bulk_batch.save(
                update_fields=[
                    "imported_files",
                    "review_files",
                    "failed_files",
                    "duplicate_files",
                    "processed_files",
                    "updated_at",
                ]
            )

            return ok(
                result,
                "Review confirmed and imported.",
            )

        except ImportServiceError as exc:
            return fail(exc)
        
class BatchMetadataView(APIView):
    permission_classes = [AllowAny]
    def patch(self, request, batch_id):
        serializer = MetadataUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch.objects.select_related("supplier"), id=batch_id)
        metadata = {**(batch.document_metadata or {}), **serializer.validated_data["document_fields"]}
        supplier_name = (metadata.get("supplier_name") or metadata.get("supplier") or "").strip()
        if supplier_name:
            metadata["supplier"] = supplier_name
            metadata["supplier_name"] = supplier_name
            normalized = normalize_supplier(supplier_name)
            supplier = Supplier.objects.filter(normalized_supplier_name=normalized, is_active=True).first()
            if supplier is None:
                supplier = Supplier.objects.create(supplier_name=supplier_name, normalized_supplier_name=normalized, default_currency=(
                    metadata.get("currency")
                    or batch.default_currency
                    or ""
                ), is_active=True)
            batch.supplier = supplier
            batch.supplier_name_from_file = supplier_name
        batch.document_metadata = metadata
        batch.save(update_fields=["document_metadata", "supplier", "supplier_name_from_file", "updated_at"])
        row_updates = {}
        if supplier_name:
            row_updates["supplier"] = supplier_name
        if metadata.get("currency"):
            row_updates["currency"] = metadata.get("currency")
        if metadata.get("document_date"):
            row_updates["purchase_date"] = metadata.get("document_date")
        if metadata.get("category"):
            row_updates["category"] = metadata.get("category")
        if row_updates:
            for row in ImportRow.objects.filter(import_batch=batch):
                mapped = _line_item_display_data(
                    _merged_line_item_data(
                        row.mapped_data,
                        {**(row.normalized_data or {}), **row_updates},
                    )
                )
                normalized_data = _line_item_display_data(
                    _merged_line_item_data(
                        row.normalized_data,
                        {**(row.mapped_data or {}), **row_updates},
                    )
                )
                row.mapped_data = mapped
                row.normalized_data = normalized_data
                row.save(update_fields=["mapped_data", "normalized_data", "updated_at"])
        return ok({"batch_id": batch.id, "document_fields": metadata, "supplier": supplier_name or None}, "Document information updated.")

class ImportRowUpdateView(APIView):
    permission_classes = [AllowAny]
    def patch(self, request, batch_id, row_id):
        serializer = ImportRowUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = get_object_or_404(ImportRow, id=row_id, import_batch_id=batch_id)
        row_type = serializer.validated_data.get("row_type")
        mapped_patch = serializer.validated_data.get("mapped_data") or {}
        mapped_patch = _fix_divided_single_visible_amount(mapped_patch)
        if row_type:
            row.row_type = row_type
            mapped_patch["row_type"] = row_type
            mapped_patch["line_group_key"] = line_item_group_key({**(row.mapped_data or {}), **mapped_patch}, row_type)
            if row_type in {"SERVICE", "CHARGE"}:
                row.validation_status = "valid"
                row.validation_errors = []
                row.validation_warnings = [f"{row_type.title()} row will be stored in import history; no product price record will be created."]
            elif row_type == "IGNORE":
                row.validation_status = "ignored"
                row.validation_errors = []
                row.validation_warnings = ["Ignored row skipped."]
            elif row_type == "UNKNOWN":
                row.validation_status = "failed"
                row.validation_errors = ["Unknown row type requires review."]
        if mapped_patch:
            merged_data = _line_item_display_data(
                _merged_line_item_data(
                    row.mapped_data,
                    {**(row.normalized_data or {}), **mapped_patch},
                )
            )
            row.mapped_data = {**(row.mapped_data or {}), **merged_data}
            row.normalized_data = {**(row.normalized_data or {}), **merged_data}
            recovered_data = ImportService()._row_import_data(row)
            row.mapped_data = {**(row.mapped_data or {}), **recovered_data}
            row.normalized_data = {**(row.normalized_data or {}), **recovered_data}
        row.save(update_fields=["row_type", "mapped_data", "normalized_data", "validation_status", "validation_errors", "validation_warnings", "updated_at"])
        return ok(ImportService()._preview_row(row), "Import row updated.")


class ImportReviewRowCreateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, batch_id):
        batch = get_object_or_404(ImportBatch, id=batch_id)
        payload = request.data or {}
        row_type = payload.get("row_type") or "PRODUCT"
        mapped_data = dict(payload.get("mapped_data") or {})
        mapped_data["row_type"] = row_type
        next_number = (batch.rows.aggregate(max_number=Max("row_number")).get("max_number") or 0) + 1
        sheet = batch.sheets.order_by("id").first()
        if not sheet:
            return Response(
                {"success": False, "message": "Import batch has no sheet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        row = ImportRow.objects.create(
            import_batch=batch,
            import_sheet=sheet,
            row_number=next_number,
            raw_data={},
            mapped_data=mapped_data,
            normalized_data=mapped_data.copy(),
            row_type=row_type,
            validation_status="pending" if row_type not in {"IGNORE"} else "ignored",
        )
        return ok(ImportService()._preview_row(row), "Review row added.")


class ImportReviewRowDeleteView(APIView):
    permission_classes = [AllowAny]

    def delete(self, request, batch_id, row_id):
        row = get_object_or_404(ImportRow, id=row_id, import_batch_id=batch_id)
        row.delete()
        return ok({"row_id": row_id}, "Review row deleted.")

class HeaderView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        serializer = HeaderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch, id=batch_id)
        sheet = get_object_or_404(ImportSheet, id=serializer.validated_data["sheet_id"])
        try:
            return ok(SheetService().select_header(batch, sheet, serializer.validated_data["header_row_number"]))
        except ImportServiceError as exc:
            return fail(exc)

class MappingView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        serializer = MappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch, id=batch_id)
        sheet = get_object_or_404(ImportSheet, id=serializer.validated_data["sheet_id"], import_batch=batch)
        
        if not sheet.cached_headers:
            sheet.cached_headers = list(VIRTUAL_HEADERS)
            sheet.save(update_fields=["cached_headers", "updated_at"])
        user = getattr(request.user, "price_intelligence_profile", None) if request.user.is_authenticated else None
        try:
            mappings = MappingService().save_mappings(batch, sheet, serializer.validated_data["mappings"], user=user)
            return ok({"saved": len(mappings)}, "Mappings saved.")
        except ImportServiceError as exc:
            return fail(exc)

class PreviewView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        serializer = PreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch, id=batch_id)
        try:
            return ok(ImportService().preview(batch, serializer.validated_data["sheet_ids"], serializer.validated_data["limit"]), "Preview generated.")
        except ImportServiceError as exc:
            return fail(exc)

class StartImportView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        serializer = PreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch, id=batch_id)
        try:
            return ok(ImportService().start(batch, serializer.validated_data["sheet_ids"]), "Import completed.")
        except ImportServiceError as exc:
            return fail(exc)

class ImportHistoryView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        queryset = ImportBatch.objects.select_related("uploaded_file", "supplier").exclude(status="deleted")
        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(uploaded_file__original_file_name__icontains=search)
                | Q(batch_number__icontains=search)
                | Q(category__icontains=search)
                | Q(status__icontains=search)
                | Q(supplier__supplier_name__icontains=search)
                | Q(supplier_name_from_file__icontains=search)
            )
        category = (request.query_params.get("category") or "").strip()
        if category:
            queryset = queryset.filter(category=category)
        supplier = (request.query_params.get("supplier") or "").strip()
        if supplier:
            queryset = queryset.filter(Q(supplier__supplier_name=supplier) | Q(supplier_name_from_file=supplier))
        status_value = (request.query_params.get("status") or "").strip()
        if status_value:
            queryset = queryset.filter(status=status_value)
        uploaded_from = (request.query_params.get("uploaded_from") or "").strip()
        if uploaded_from:
            queryset = queryset.filter(uploaded_at__date=uploaded_from)
        data = [{
            "id": item.id,
            "batch_number": item.batch_number,
            "file_name": item.uploaded_file.original_file_name,
            "category": _history_document_label(
                (item.document_metadata or {}).get("document_type")
                or (item.document_metadata or {}).get("category")
                or item.category
            ),
            "document_type": _history_document_label(
                (item.document_metadata or {}).get("document_type")
                or (item.document_metadata or {}).get("category")
                or item.category
            ),
            "supplier": item.supplier.supplier_name if item.supplier else item.supplier_name_from_file,
            "uploaded_date": item.uploaded_at,
            "total_rows": item.total_rows,
            "imported_rows": item.imported_rows,
            "failed_rows": item.failed_rows,
            "duplicate_rows": item.duplicate_rows,
            "warning_rows": item.warning_rows,
            "status": item.status,
        } for item in queryset.order_by("-uploaded_at")]
        return ok(data)

class ImportHistoryDetailView(APIView):
    permission_classes = [AllowAny]
    def get(self, request, batch_id):
        batch = get_object_or_404(ImportBatch.objects.select_related("uploaded_file", "supplier").prefetch_related("sheets", "column_mappings", "rows"), id=batch_id)
        history_rows = []
        # This response also supplies the editable review rows. Truncating it
        # silently hides valid extracted items from both history and review.
        history_source_rows = list(batch.rows.all().order_by("id"))
        history_payloads = {
            row.id: {
                **(row.raw_data or {}),
                **(row.mapped_data or {}),
                **(row.normalized_data or {}),
            }
            for row in history_source_rows
        }
        for row in history_source_rows:
            row_data = ImportService()._preview_row(row)
            for field_data in (row_data.get("mapped_data"), row_data.get("normalized_data")):
                if not isinstance(field_data, dict):
                    continue
                display_source = _line_item_display_data(field_data)
                if display_source.get("display_name"):
                    field_data["display_name"] = display_source["display_name"]
                matrix_display = rate_matrix_display_name(
                    history_payloads.get(row.id) or field_data,
                    list(history_payloads.values()),
                )
                row_type = str(
                    field_data.get("row_type")
                    or row_data.get("row_type")
                    or row.row_type
                    or ""
                ).upper()
                # PRODUCT descriptions must remain the exact persisted/API
                # product_name. A derived display_name/ProductMaster label
                # may be shorter and must not overwrite it in Import History.
                raw_product_name = field_data.get("product_name") or ""
                clean_name = clean_display_identity(
                    raw_product_name
                    if row_type == "PRODUCT"
                    else (
                        matrix_display
                        or field_data.get("display_name")
                        or raw_product_name
                        or ""
                    )
                )
                if clean_name:
                    # Keep source product_name intact. The display field may
                    # include the preserved source attributes; never replace
                    # it by truncating or cleaning the product identity.
                    field_data["display_name"] = clean_display_identity(
                        field_data.get("display_name")
                        or clean_name
                    )
                    field_data["product_name"] = raw_product_name or clean_name
                    field_data["description"] = field_data["display_name"]
            history_rows.append(row_data)
        data = {
            "id": batch.id,
            "batch_number": batch.batch_number,
            "file_name": batch.uploaded_file.original_file_name,
            "category": _history_document_label(
                (batch.document_metadata or {}).get("document_type")
                or (batch.document_metadata or {}).get("category")
                or batch.category
            ),
            "document_type": _history_document_label(
                (batch.document_metadata or {}).get("document_type")
                or (batch.document_metadata or {}).get("category")
                or batch.category
            ),
            "status": batch.status,
            "total_rows": batch.total_rows,
            "imported_rows": batch.imported_rows,
            "failed_rows": batch.failed_rows,
            "duplicate_rows": batch.duplicate_rows,
            "warning_rows": batch.warning_rows,
            "sheets": [{"id": sheet.id, "sheet_name": sheet.sheet_name, "header_row_number": sheet.header_row_number, "total_rows": sheet.total_rows} for sheet in batch.sheets.all()],
            "mappings": [{"sheet_id": mapping.import_sheet_id, "uploaded_column_name": mapping.uploaded_column_name, "system_field_name": mapping.system_field_name, "is_ignored": mapping.is_ignored} for mapping in batch.column_mappings.all()],
            "rows": history_rows,
            "error_message": batch.error_message,
        }
        return ok(data)

    def delete(self, request, batch_id):
        batch = get_object_or_404(
            ImportBatch,
            id=batch_id,
        )

        # ---------------------------------------------------------
        # Remove ONLY the BulkImportFile linked to this ImportBatch.
        #
        # IMPORTANT:
        # Do NOT delete by bulk_batch_id because that would remove
        # all files uploaded together in the same bulk upload.
        # ---------------------------------------------------------
        bulk_job = (
            BulkImportFile.objects
            .filter(
                import_batch_id=batch.id,
            )
            .select_related("bulk_batch")
            .first()
        )

        bulk_batch = (
            bulk_job.bulk_batch
            if bulk_job
            else None
        )

        if bulk_job:
            BulkImportFile.objects.filter(
                import_batch_id=batch.id
            ).delete()

        # ---------------------------------------------------------
        # Soft-delete this import history record.
        # Existing product-search delete logic can continue to work
        # independently.
        # ---------------------------------------------------------
        batch.status = "deleted"

        batch.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

        # The uploaded source is the identity used by duplicate detection.
        # Mark it deleted together with its import batch; otherwise deleting
        # the history row still leaves the old file hash active and blocks a
        # legitimate re-upload of the corrected source file.
        uploaded_source = batch.uploaded_file
        if uploaded_source and not uploaded_source.is_deleted:
            uploaded_source.is_deleted = True
            uploaded_source.deleted_at = timezone.now()
            uploaded_source.save(
                update_fields=["is_deleted", "deleted_at", "updated_at"]
            )

        # ---------------------------------------------------------
        # Recalculate counters ONLY for the affected bulk batch.
        # Other files in the same bulk upload remain untouched.
        # ---------------------------------------------------------
        if bulk_batch:
            jobs = bulk_batch.files.all()

            bulk_batch.total_files = jobs.count()

            bulk_batch.imported_files = jobs.filter(
                status=BulkImportFileStatus.IMPORTED
            ).count()

            bulk_batch.review_files = jobs.filter(
                status=BulkImportFileStatus.REVIEW_REQUIRED
            ).count()

            bulk_batch.failed_files = jobs.filter(
                status=BulkImportFileStatus.FAILED
            ).count()

            bulk_batch.duplicate_files = jobs.filter(
                status=BulkImportFileStatus.DUPLICATE
            ).count()

            bulk_batch.processed_files = jobs.exclude(
                status__in=[
                    BulkImportFileStatus.PENDING,
                    BulkImportFileStatus.PROCESSING,
                ]
            ).count()

            bulk_batch.save(
                update_fields=[
                    "total_files",
                    "imported_files",
                    "review_files",
                    "failed_files",
                    "duplicate_files",
                    "processed_files",
                    "updated_at",
                ]
            )

        return ok(
            {
                "batch_id": batch.id,
                "bulk_file_tracking_deleted": bool(
                    bulk_job
                ),
            },
            "Import batch deleted. The same source file can now be uploaded again.",
        )

class DownloadView(APIView):
    permission_classes = [AllowAny]
    def get(self, request, batch_id):
        batch = get_object_or_404(ImportBatch.objects.select_related("uploaded_file"), id=batch_id)
        inline = request.query_params.get("disposition") == "inline"
        response = FileResponse(batch.uploaded_file.file.open("rb"), as_attachment=not inline, filename=batch.uploaded_file.original_file_name)
        if inline:
            response["Content-Disposition"] = f'inline; filename="{batch.uploaded_file.original_file_name}"'
        return response

class TemplateViewSet(ModelViewSet):
    permission_classes = [AllowAny]
    queryset = MappingTemplate.objects.filter(is_active=True).prefetch_related("fields")
    serializer_class = TemplateSerializer

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        template.is_active = False
        template.save(update_fields=["is_active", "updated_at"])
        return ok(message="Template deleted.")

class SaveTemplateView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        serializer = SaveTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = get_object_or_404(ImportBatch, id=batch_id)
        sheet = get_object_or_404(ImportSheet, id=serializer.validated_data["sheet_id"], import_batch=batch)
        user = getattr(request.user, "price_intelligence_profile", None) if request.user.is_authenticated else None
        try:
            template = TemplateService().save_from_batch(batch, sheet, serializer.validated_data, user=user)
            return ok(TemplateSerializer(template).data, "Template saved.", status.HTTP_201_CREATED)
        except ImportServiceError as exc:
            return fail(exc)

class ApplyTemplateView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        batch = get_object_or_404(ImportBatch, id=batch_id)
        sheet = get_object_or_404(ImportSheet, id=request.data.get("sheet_id"), import_batch=batch)
        template = get_object_or_404(MappingTemplate, id=request.data.get("template_id"), is_active=True)
       
        headers = list(getattr(sheet, "cached_headers", None) or VIRTUAL_HEADERS)
        return ok(TemplateService().apply(template, headers))



class LineItemGroupSearchView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        started_at = time.perf_counter()
        query = normalize_product_text(request.query_params.get("q", ""))
        supplier_query = (request.query_params.get("supplier") or "").strip()
        normalized_supplier_query = normalize_supplier(supplier_query) if supplier_query else ""
        row_types = [item.strip().upper() for item in request.query_params.get("row_type", "").split(",") if item.strip()]
        rows = ImportRow.objects.select_related(
            "import_batch__uploaded_file",
            "import_batch__supplier",
            "product_match__confirmed_product",
        ).filter(
            is_imported=True,
            row_type__in=["PRODUCT", "SERVICE", "CHARGE"],
        ).exclude(validation_status__in=[ImportRowStatus.FAILED, ImportRowStatus.IGNORED]).only(
            "id", "import_batch_id", "row_type", "validation_status", "is_imported",
            "imported_at", "raw_data", "mapped_data", "normalized_data",
            "import_batch__id", "import_batch__document_metadata",
            "import_batch__category", "import_batch__default_currency", "import_batch__supplier_id",
            "import_batch__supplier__id", "import_batch__supplier__supplier_name",
            "import_batch__uploaded_file_id", "import_batch__uploaded_file__id",
            "import_batch__uploaded_file__original_file_name",
            "product_match__id", "product_match__confirmed_product_id",
            "product_match__confirmed_product__id", "product_match__confirmed_product__product_name",
        )
        if row_types:
            rows = rows.filter(row_type__in=row_types)
        queryset_ready_at = time.perf_counter()
        rows = list(rows)
        rows_loaded_at = time.perf_counter()
        logger.info(
            "line_items_groups_debug phase=rows_loaded query=%r supplier=%r "
            "row_types=%s rows=%s queryset_setup_ms=%.1f db_load_ms=%.1f",
            query,
            supplier_query,
            row_types or "all",
            len(rows),
            (queryset_ready_at - started_at) * 1000,
            (rows_loaded_at - queryset_ready_at) * 1000,
        )
        groups = {}
        seen_import_row_ids = set()

        duration_re = re.compile(
            r"^\d+(?:\.\d+)?\s*(?:HR|HRS|HOUR|HOURS)"
            r"(?:\s+\d+(?:\.\d+)?\s*(?:MIN|MINS|MINUTE|MINUTES))?$",
            re.I,
        )

        def row_payload(row):
            mapped = row.mapped_data or {}
            normalized = row.normalized_data or {}
            raw = row.raw_data or {}
            return _merged_line_item_data({**raw, **mapped}, normalized)

        def duration_value(payload):
            for attr in payload.get("extra_attributes") or []:
                if not isinstance(attr, dict):
                    continue
                label = re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip()
                if label == "duration" and str(attr.get("value") or "").strip():
                    return str(attr["value"]).strip()
            quantity = str(payload.get("quantity") or "").strip()
            return quantity if duration_re.fullmatch(quantity) else None

        duration_context = {}
        for context_row in rows:
            payload = row_payload(context_row)
            duration = duration_value(payload)
            if not duration:
                continue
            context_key = (
                context_row.import_batch_id,
                payload.get("page_number"),
                normalize_supplier(payload.get("supplier_name") or payload.get("supplier") or ""),
                str(payload.get("purchase_date") or ""),
            )
            duration_context.setdefault(context_key, duration)

        def build_row_display_name(row, payload):
            from api.services.commercial_attribute_policy import clean_commercial_display
            payload = clean_commercial_display(dict(payload))
            stored_display = re.sub(r"\s+", " ", str(payload.get("display_name") or "")).strip()
            if stored_display:
                return clean_display_identity(stored_display)
            base_name = (
                payload.get("product_name")
                or payload.get("description")
                or (getattr(row.product_match.confirmed_product, "product_name", None) if row.product_match and row.product_match.confirmed_product_id else None)
                or ""
            )
            matrix_display = rate_matrix_display_name(payload, [row_payload(candidate) for candidate in rows])
            if matrix_display:
                return clean_display_identity(matrix_display)
            attrs = list(payload.get("extra_attributes") or [])
            has_duration = any(
                isinstance(attr, dict)
                and re.sub(r"[^a-z0-9]+", " ", str(attr.get("label") or "").lower()).strip() == "duration"
                for attr in attrs
            )
            name_lower = str(base_name).lower()
            if not has_duration and re.search(r"\b(?:launch\s+hire|crane\s+charge)\b", name_lower) and "tariff" not in name_lower:
                context_key = (
                    row.import_batch_id,
                    payload.get("page_number"),
                    normalize_supplier(payload.get("supplier_name") or payload.get("supplier") or ""),
                    str(payload.get("purchase_date") or ""),
                )
                duration = duration_context.get(context_key)
                if duration:
                    attrs.append({"label": "Duration", "value": duration})
            # Share History's builder for legacy rows without a stored display.
            # Keep technical attributes and parents, but not financial columns.
            return clean_display_identity(_line_item_display_data({
                **payload, "product_name": base_name, "extra_attributes": attrs,
            })["display_name"])

        def supplier_matches_query(*candidates):
            if not normalized_supplier_query:
                return True
            return any(
                normalize_supplier(candidate or "") == normalized_supplier_query
                for candidate in candidates
                if candidate
            )

        def normalize_document_label(value):
            normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
            return {
                "quotation": "QUOTATION",
                "quote": "QUOTATION",
                "price list": "PRICE LIST",
                "price list quotation": "PRICE LIST",
                "invoice": "INVOICE",
                "email quote": "EMAIL_QUOTE",
                "email_quote": "EMAIL_QUOTE",
                "emailquote": "EMAIL_QUOTE",
                "purchase order": "PURCHASE ORDER",
                "purchase_order": "PURCHASE ORDER",
                "other": "OTHER",
            }.get(normalized, str(value or "").strip().upper())

        def choose_source_document_label(row, data, metadata):
            candidates = [
                metadata.get("document_type"),
                metadata.get("category"),
                getattr(row.import_batch, "document_type", None),
                getattr(row.import_batch, "category", None),
                data.get("source_category"),
                data.get("source_document_type"),
            ]
            for value in candidates:
                label = normalize_document_label(value)
                if label and label != "OTHER":
                    return label
            return "OTHER"

        for row in rows:
            raw_row_data = row_payload(row)
            data = _line_item_display_data(raw_row_data)
            data["display_name"] = build_row_display_name(row, raw_row_data)
            logger.debug(
                "LINE_ITEM_DISPLAY_TRACE row_id=%s product_name=%r display_name=%r extra=%r quantity=%r product_master_name=%r final_item=%r",
                row.id,
                raw_row_data.get("product_name"),
                raw_row_data.get("display_name"),
                raw_row_data.get("extra_attributes"),
                raw_row_data.get("quantity"),
                getattr(getattr(row.product_match, "confirmed_product", None), "product_name", None),
                data.get("display_name"),
            )
            # Keep the persisted source identity verbatim in the response.
            # Normalized text is used below only for search/group keys.
            source_product_name = str(
                raw_row_data.get("product_name")
                or data.get("product_name")
                or ""
            ).strip()
            data["product_name"] = source_product_name
            description = (
                data.get("display_name")
                or data.get("product_name")
                or data.get("description")
                or ""
            )
            if query and query not in normalize_product_text(description):
                continue
            matched_product_id = (
                row.product_match.confirmed_product_id
                if hasattr(row, "product_match") and row.product_match and row.product_match.confirmed_product_id
                else None
            )
            display_identity = normalize_product_text(
                data.get("display_name")
                or data.get("product_name")
                or data.get("description")
                or ""
            )

            base_group_key = (
                f"{row.row_type}:product:{matched_product_id}:display:{display_identity}"
                if matched_product_id and row.row_type == "PRODUCT"
                else data.get("line_group_key") or line_item_group_key(data, row.row_type)
            )
            if not base_group_key:
                continue
            amount = costing_amount(data)
            batch_metadata = row.import_batch.document_metadata or {}
            source_document_label = choose_source_document_label(row, data, batch_metadata)
            batch_supplier = row.import_batch.supplier.supplier_name if row.import_batch.supplier_id else row.import_batch.supplier_name_from_file
            mapped_supplier = (
                data.get("supplier")
                or data.get("supplier_name")
            )
            metadata_supplier = batch_metadata.get("supplier") or batch_metadata.get("supplier_name")
            if not supplier_matches_query(mapped_supplier, batch_supplier, metadata_supplier):
                continue
            supplier = mapped_supplier or batch_supplier or metadata_supplier
            if normalized_supplier_query and not supplier_matches_query(supplier):
                supplier = supplier_query
            document_date = data.get("purchase_date") or data.get("document_date") or ""
            source_file_name = data.get("source_file_name") or row.import_batch.uploaded_file.original_file_name
            unit_price_key = str(data.get("unit_price") or "").strip()
            currency_key = str(data.get("currency") or "").strip().upper()
            is_product_row = str(row.row_type or "").upper() == "PRODUCT"
            response_product_name = (
                str(data.get("product_name") or data.get("description") or "").strip()
                if is_product_row
                else str(
                    data.get("display_name")
                    or data.get("product_name")
                    or data.get("description")
                    or ""
                ).strip()
            )

            group_key = f"{base_group_key}:price:{unit_price_key}:{currency_key}"
            record = {
                "row_id": row.id,
                "batch_id": row.import_batch_id,
                "description": response_product_name,

                "display_name": (
                    data.get("display_name")
                    or response_product_name
                    or description
                ),
                "product_name": (
                    data.get("product_name")
                    or response_product_name
                    or description
                ),
                "model": data.get("model"),
                "specification": data.get("specification"),
                "remark": data.get("remark"),
                "note": data.get("note"),
                "parent_category": data.get("parent_category"),
                "extra_attributes": data.get("extra_attributes") or [],

                "row_type": row.row_type,
                "supplier": supplier,
                "quantity": data.get("quantity"),
                "uom": data.get("unit_of_measurement"),
                "unit_price": data.get("unit_price"),
                "tax_amount": data.get("tax_amount"),
                "line_total": data.get("line_total"),
                "amount_for_costing": amount,
                "currency": data.get("currency"),
                "document_date": document_date,
                "source_file_name": source_file_name,
                "item_code": data.get("item_code"),
                "part_number": data.get("part_number"),
                "imported_at": row.imported_at.isoformat() if row.imported_at else None,
                "uploaded_at": row.import_batch.uploaded_file.uploaded_at.isoformat() if row.import_batch.uploaded_file_id else None,
                "import_batch_name": row.import_batch.uploaded_file.original_file_name,
                "category": source_document_label,

                "document_type": source_document_label,
            }
            group = groups.setdefault(group_key, {
                "group_key": group_key,
                "base_group_key": base_group_key,
                "row_type": row.row_type,
                "description": response_product_name,

                "display_name": (
                    data.get("display_name")
                    or response_product_name
                    or description
                ),
                "model": data.get("model"),
                "specification": data.get("specification"),
                "remark": data.get("remark"),
                "note": data.get("note"),
                "parent_category": data.get("parent_category"),
                "extra_attributes": data.get("extra_attributes") or [],

                "supplier": supplier,
                "document_date": document_date,
                "source_file_name": source_file_name,
                "currency": record.get("currency"),
                "record_count": 0,
                "latest_record": record,
                "records": [],
            })
            group["record_count"] += 1
            group["records"].append(record)
            seen_import_row_ids.add(row.id)
            if str(record.get("document_date") or "") >= str(group["latest_record"].get("document_date") or ""):
                group["latest_record"] = record
                group["description"] = record.get("display_name") or record.get("description")
                group["display_name"] = record.get("display_name") or record.get("description")
                group["product_name"] = record.get("product_name")
                group["parent_category"] = record.get("parent_category")
                group["model"] = record.get("model")
                group["specification"] = record.get("specification")
                group["remark"] = record.get("remark")
                group["note"] = record.get("note")
                group["extra_attributes"] = record.get("extra_attributes") or []
        price_history = PriceHistory.objects.none()
        if not row_types or "PRODUCT" in row_types:
            price_history = PriceHistory.objects.select_related(
                "product",
                "product__category",
                "product__subcategory",
                "supplier",
                "import_row",
                "import_batch__uploaded_file",
                "import_batch__supplier",
            ).filter(
                product__isnull=False,
                product__is_active=True,
                is_active=True,
            ).only(
                "id", "product_id", "supplier_id", "import_batch_id", "import_row_id",
                "source_type", "source_reference", "source_file_name", "purchase_date",
                "quantity", "unit_of_measurement", "original_unit_price", "currency",
                "converted_unit_price", "import_batch__id", "import_batch__document_metadata",
                "import_batch__category", "import_batch__supplier_id",
                "import_batch__supplier__id", "import_batch__supplier__supplier_name",
                "import_batch__uploaded_file_id", "import_batch__uploaded_file__id",
                "import_batch__uploaded_file__original_file_name",
                "product__id", "product__product_name", "product__item_code",
                "product__part_number", "product__manufacturer", "product__brand",
                "product__category_id", "product__subcategory_id", "product__category__id",
                "product__category__name", "product__subcategory__id", "product__subcategory__name",
                "import_row__id", "import_row__raw_data", "import_row__mapped_data",
                "import_row__normalized_data", "import_row__row_type", "import_row__imported_at",
            )
        price_history_started_at = time.perf_counter()
        price_history_rows = list(price_history)
        logger.info(
            "line_items_groups_debug phase=price_history_loaded rows=%s db_load_ms=%.1f",
            len(price_history_rows),
            (time.perf_counter() - price_history_started_at) * 1000,
        )
        for price in price_history_rows:
            if price.import_row_id and price.import_row_id in seen_import_row_ids:
                continue
            import_row_data = {}

            if price.import_row_id and price.import_row:
                import_row_data = _import_row_display_data(
                    price.import_row
                )
            import_row_data["product_name"] = str(
                import_row_data.get("product_name")
                or import_row_data.get("display_name")
                or ""
            ).strip()
            import_row_data["display_name"] = str(
                import_row_data.get("display_name")
                or import_row_data.get("product_name")
                or ""
            ).strip()

            description = (
                import_row_data.get("display_name")
                or import_row_data.get("product_name")
                or (
                    price.product.product_name
                    if price.product_id
                    else ""
                )
            )
            if query and query not in normalize_product_text(description):
                continue
            batch_metadata = price.import_batch.document_metadata if price.import_batch_id else {}
            batch_supplier = (
                price.import_batch.supplier.supplier_name
                if price.import_batch_id and price.import_batch.supplier_id
                else (price.import_batch.supplier_name_from_file if price.import_batch_id else "")
            )
            mapped_supplier = (
                import_row_data.get("supplier")
                or import_row_data.get("supplier_name")
            )
            price_supplier = price.supplier.supplier_name if price.supplier_id else ""
            metadata_supplier = (batch_metadata or {}).get("supplier") or (batch_metadata or {}).get("supplier_name")
            if not supplier_matches_query(mapped_supplier, price_supplier, batch_supplier, metadata_supplier):
                continue
            supplier = mapped_supplier or price_supplier or batch_supplier or metadata_supplier
            if normalized_supplier_query and not supplier_matches_query(supplier):
                supplier = supplier_query
            source_file_name = price.source_file_name or (
                price.import_batch.uploaded_file.original_file_name
                if price.import_batch_id and price.import_batch.uploaded_file_id
                else ""
            )
            document_date = price.purchase_date.isoformat() if price.purchase_date else ""
            record = {
                "row_id": price.import_row_id,
                "batch_id": price.import_batch_id,
                "price_history_id": price.id,
                "description": description,

                "display_name": (
                    import_row_data.get("display_name")
                    or description
                ),
                "product_name": (
                    import_row_data.get("product_name")
                    or description
                ),
                "model": import_row_data.get("model"),
                "specification": import_row_data.get(
                    "specification"
                ),
                "remark": import_row_data.get("remark"),
                "note": import_row_data.get("note"),
                "parent_category": import_row_data.get(
                    "parent_category"
                ),
                "extra_attributes": import_row_data.get("extra_attributes") or [],

                "row_type": "PRODUCT",
                "supplier": supplier,
                "quantity": str(price.quantity) if price.quantity is not None else None,
                "uom": price.unit_of_measurement,
                "unit_price": str(price.original_unit_price),
                "tax_amount": None,
                "line_total": (
                    import_row_data.get("line_total")
                    or str(price.original_unit_price)
                ),
                "amount_for_costing": str(price.converted_unit_price),
                "currency": price.currency,
                "document_date": document_date,
                "source_file_name": source_file_name,
                "item_code": price.product.item_code if price.product_id else None,
                "part_number": price.product.part_number if price.product_id else None,
                "imported_at": price.created_at.isoformat() if price.created_at else None,
                "uploaded_at": price.import_batch.uploaded_file.uploaded_at.isoformat() if price.import_batch_id and price.import_batch.uploaded_file_id else None,
                "import_batch_name": price.import_batch.uploaded_file.original_file_name if price.import_batch_id and price.import_batch.uploaded_file_id else "",
                "category": str(
                    (batch_metadata or {}).get("category")
                    or import_row_data.get("category")
                    or price.source_type
                    or (
                        price.import_batch.category
                        if price.import_batch_id
                        else None
                    )
                    or ""
                ).strip().upper(),
                "document_type": str(
                    (batch_metadata or {}).get("document_type")
                    or import_row_data.get("source_document_type")
                    or import_row_data.get("document_type")
                    or (batch_metadata or {}).get("category")
                    or import_row_data.get("category")
                    or (
                        price.import_batch.category
                        if price.import_batch_id
                        else None
                    )
                    or "OTHER"
                ).strip().upper(),
                "is_active": price.is_active,
            }
            base_group_key = (
                f"PRODUCT:product:{price.product_id}"
                if price.product_id
                else line_item_group_key({"product_name": description}, "PRODUCT") or f"price-history-{price.id}"
            )

            unit_price_key = str(price.original_unit_price or "").strip()
            currency_key = str(price.currency or "").strip().upper()

            group_key = f"{base_group_key}:price:{unit_price_key}:{currency_key}"

            if group_key in groups:
                continue

            groups[group_key] = {
                "group_key": group_key,
                "base_group_key": group_key,
                "row_type": "PRODUCT",
                "description": description,

                "display_name": (
                    import_row_data.get("display_name")
                    or description
                ),
                "model": import_row_data.get("model"),
                "specification": import_row_data.get(
                    "specification"
                ),
                "remark": import_row_data.get("remark"),
                "note": import_row_data.get("note"),
                "parent_category": import_row_data.get(
                    "parent_category"
                ),

                "supplier": supplier,
                "document_date": document_date,
                "source_file_name": source_file_name,
                "currency": price.currency,
                "record_count": 1,
                "latest_record": record,
                "records": [record],
                "master_product": {
                    "product_id": price.product_id,
                    "item_code": price.product.item_code if price.product_id else None,
                    "part_number": price.product.part_number if price.product_id else None,
                    "brand": price.product.brand if price.product_id else None,
                    "manufacturer": price.product.manufacturer if price.product_id else None,
                    "category": price.product.category.name if price.product_id and price.product.category_id else None,
                    "subcategory": price.product.subcategory.name if price.product_id and price.product.subcategory_id else None,
                } if price.product_id else None,
            }
        # Index group membership once instead of scanning every group per product.
        product_group_keys = {}
        for key in groups:
            parts = key.split(":")
            if len(parts) >= 3 and parts[:2] == ["PRODUCT", "product"]:
                if len(parts) == 3 or parts[3] == "price":
                    product_group_keys.setdefault(":".join(parts[:3]), []).append(key)
        products = Product.objects.select_related("category", "subcategory").filter(is_active=True).only(
            "id", "product_name", "item_code", "part_number", "brand", "manufacturer",
            "category_id", "subcategory_id", "category__id", "category__name",
            "subcategory__id", "subcategory__name",
        )
        if query:
            products = products.filter(
                Q(product_name__icontains=request.query_params.get("q", ""))
                | Q(item_code__icontains=request.query_params.get("q", ""))
                | Q(part_number__icontains=request.query_params.get("q", ""))
                | Q(brand__icontains=request.query_params.get("q", ""))
                | Q(manufacturer__icontains=request.query_params.get("q", ""))
            )
        product_limit = 20000 if query else 1000
        products_started_at = time.perf_counter()
        products = list(products[:product_limit])
        logger.info(
            "line_items_groups_debug phase=products_loaded rows=%s db_load_ms=%.1f",
            len(products),
            (time.perf_counter() - products_started_at) * 1000,
        )
        for product in products:
            product_data = {
                "product_name": product.product_name,
                "item_code": product.item_code,
                "part_number": product.part_number,
                "brand": product.brand,
            }
            group_key = f"PRODUCT:product:{product.id}"

            matching_keys = product_group_keys.get(group_key, [])

            if matching_keys:
                for matched_key in matching_keys:
                    groups[matched_key]["master_product"] = {
                        "product_id": product.id,
                        "item_code": product.item_code,
                        "part_number": product.part_number,
                        "brand": product.brand,
                        "manufacturer": product.manufacturer,
                        "category": product.category.name if product.category_id else None,
                        "subcategory": product.subcategory.name if product.subcategory_id else None,
                    }
                continue
            if not request.query_params.get("include_master"):
                continue
            groups[group_key] = {
                "group_key": group_key,
                "row_type": "PRODUCT",
                "description": product.product_name,
                "item_code": product.item_code,
                "part_number": product.part_number,
                "brand": product.brand,
                "category": product.category.name if product.category_id else None,
                "subcategory": product.subcategory.name if product.subcategory_id else None,
                "record_count": 0,
                "latest_record": None,
                "records": [],
                "master_product": {
                    "product_id": product.id,
                    "item_code": product.item_code,
                    "part_number": product.part_number,
                    "brand": product.brand,
                    "manufacturer": product.manufacturer,
                    "category": product.category.name if product.category_id else None,
                    "subcategory": product.subcategory.name if product.subcategory_id else None,
                },
            }
        benchmarks_started_at = time.perf_counter()
        self._append_market_benchmarks(groups, query)
        logger.info(
            "line_items_groups_debug phase=benchmarks_complete groups=%s elapsed_ms=%.1f",
            len(groups),
            (time.perf_counter() - benchmarks_started_at) * 1000,
        )

        supplier_display_by_key = {}

        for group in groups.values():
            for record in group.get("records") or []:
                supplier_value = str(record.get("supplier") or "").strip()
                supplier_key = normalize_supplier(supplier_value)

                if not supplier_key:
                    continue

                current = supplier_display_by_key.get(supplier_key)

                if (
                    not current
                    or ("." in current and "." not in supplier_value)
                    or ("&" in current and " and " in supplier_value.lower())
                ):
                    supplier_display_by_key[supplier_key] = supplier_value

        for group in groups.values():
            group_supplier_key = normalize_supplier(group.get("supplier") or "")

            if group_supplier_key in supplier_display_by_key:
                group["supplier"] = supplier_display_by_key[group_supplier_key]

            latest_record = group.get("latest_record") or {}
            latest_supplier_key = normalize_supplier(latest_record.get("supplier") or "")

            if latest_supplier_key in supplier_display_by_key:
                latest_record["supplier"] = supplier_display_by_key[latest_supplier_key]

            for record in group.get("records") or []:
                record_supplier_key = normalize_supplier(record.get("supplier") or "")

                if record_supplier_key in supplier_display_by_key:
                    record["supplier"] = supplier_display_by_key[record_supplier_key]

        sorted_groups = sorted(
            groups.values(),
            key=lambda item: (
                item["record_count"] == 0,
                str(item.get("latest_record", {}).get("document_date") if item.get("latest_record") else ""),
                item["description"],
            ),
            reverse=True,
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "line_items_groups_debug phase=complete rows=%s groups=%s total_ms=%.1f",
            len(rows),
            len(sorted_groups),
            elapsed_ms,
        )
        if elapsed_ms >= 1000:
            logger.warning(
                "SLOW_LINE_ITEMS_GROUPS total_ms=%.1f rows=%s groups=%s query=%r",
                elapsed_ms,
                len(rows),
                len(sorted_groups),
                query,
            )
        return ok(sorted_groups)

    def delete(self, request):
        row_id = request.query_params.get("row_id")
        price_history_id = request.query_params.get("price_history_id")

        # ---------------------------------------------------------
        # NORMAL IMPORTED ROW
        # PRODUCT / SERVICE / CHARGE
        # ---------------------------------------------------------
        if row_id:
            row = ImportRow.objects.filter(
                id=row_id,
                is_imported=True,
            ).first()

            if not row:
                return Response(
                    {
                        "success": False,
                        "message": "Import row not found.",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            deleted_prices = PriceHistory.objects.filter(
                import_row_id=row.id,
                is_active=True,
            ).update(
                is_active=False
            )

            row.is_imported = False
            row.save(
                update_fields=[
                    "is_imported",
                    "updated_at",
                ]
            )

            return ok(
                {
                    "row_id": row.id,
                    "deleted_rows": 1,
                    "deleted_price_rows": deleted_prices,
                },
                "Item removed from search.",
            )

        # ---------------------------------------------------------
        # PRICE HISTORY ONLY RECORD
        # Used when record has no ImportRow id
        # ---------------------------------------------------------
        if price_history_id:
            price = PriceHistory.objects.filter(
                id=price_history_id,
                is_active=True,
            ).first()

            if not price:
                return Response(
                    {
                        "success": False,
                        "message": "Price history record not found.",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            product_id = price.product_id

            price.is_active = False
            price.save(
                update_fields=[
                    "is_active",
                    "updated_at",
                ]
            )

            # Hide master product only when it has no active
            # price history and no imported rows remaining.
            if product_id:
                has_active_prices = PriceHistory.objects.filter(
                    product_id=product_id,
                    is_active=True,
                ).exists()

                has_active_rows = ImportRow.objects.filter(
                    product_match__confirmed_product_id=product_id,
                    is_imported=True,
                ).exists()

                if not has_active_prices and not has_active_rows:
                    Product.objects.filter(
                        id=product_id
                    ).update(
                        is_active=False
                    )

            return ok(
                {
                    "price_history_id": price.id,
                    "deleted_price_rows": 1,
                },
                "Price record removed from search.",
            )

        return Response(
            {
                "success": False,
                "message": "row_id or price_history_id is required.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    def _append_market_benchmarks(self, groups, query):
        prices = CommodityPriceHistory.objects.select_related("benchmark").filter(benchmark__is_active=True, benchmark__is_supported=True).order_by("benchmark_id", "-market_date", "-created_at")
        latest_by_benchmark = {}
        counts = {}
        for price in prices:
            counts[price.benchmark_id] = counts.get(price.benchmark_id, 0) + 1
            latest_by_benchmark.setdefault(price.benchmark_id, price)
        allowed_market_symbols = {"HRC=F", "copper", "aluminum"}

        for benchmark in CommodityBenchmark.objects.filter(
            is_active=True,
            is_supported=True,
            provider_symbol__in=allowed_market_symbols,
        ):
            latest = latest_by_benchmark.get(benchmark.id)
            text = normalize_product_text(f"{benchmark.display_name} {benchmark.provider_symbol} market benchmark")
            if query and query not in text:
                continue
            record = None
            if latest:
                is_hrc = benchmark.provider == YAHOO_PROVIDER and benchmark.provider_symbol == HRC_SYMBOL
                metadata = benchmark.metadata or {}
                record = {
                    "row_id": None,
                    "batch_id": None,
                    "commodity_id": benchmark.id,
                    "commodity_price_history_id": latest.id,
                    "description": (
                        "Hot Rolled Coil Steel Market Benchmark"
                        if is_hrc
                        else f"{benchmark.display_name} Market Benchmark"
                    ),
                    "row_type": "MARKET_BENCHMARK",
                    "supplier": (
                        "Yahoo Finance - HRC=F"
                        if is_hrc
                        else f"Metals.Dev - {benchmark.display_name}"
                    ),
                    "quantity": 1,
                    "uom": "KG",
                    "unit_price": str(latest.aed_per_kg),
                    "tax_amount": None,
                    "line_total": str(latest.aed_per_kg),
                    "amount_for_costing": str(latest.aed_per_kg),
                    "currency": "AED",
                    "document_date": latest.market_date.isoformat(),
                    "source_file_name": (
                        "Yahoo Finance HRC=F delayed market feed"
                        if is_hrc
                        else "Metals.Dev market feed"
                    ),
                    "category": "MARKET_PRICE",
                    "document_type": "MARKET_PRICE",
                    "pricing_source_type": "MARKET_BENCHMARK",
                    "source_provider": (
                        "YAHOO_FINANCE"
                        if is_hrc
                        else "METALS_DEV"
                    ),
                    "source_symbol": HRC_SYMBOL if is_hrc else benchmark.provider_symbol,
                    "source": "Yahoo Finance" if is_hrc else "Metals.Dev",
                    "market_rate": str(latest.aed_per_kg),
                    "market_unit": "AED/KG",
                    "market_currency": "AED",
                    "market_date": latest.market_date.isoformat(),
                    "original_price": str(latest.raw_price),
                    "original_currency": latest.raw_currency,
                    "original_unit": latest.raw_unit,
                    "is_delayed": bool((latest.response_metadata or {}).get("is_delayed")) if is_hrc else False,
                    "is_stale": False,
                    "source_market": metadata.get("source_market") if is_hrc else None,
                    "requires_review": True,
                }
            groups[f"market-{benchmark.provider_symbol}"] = {
                "group_key": f"market-{benchmark.provider_symbol}",
                "row_type": "MARKET_BENCHMARK",
                "description": (
                    "Hot Rolled Coil Steel Benchmark"
                    if benchmark.provider == YAHOO_PROVIDER and benchmark.provider_symbol == HRC_SYMBOL
                    else f"{benchmark.display_name} Market Benchmark"
                ),
                "record_count": counts.get(benchmark.id, 0),
                "latest_record": record,
                "records": [record] if record else [],
                "commodity": {
                    "id": benchmark.id,
                    "provider_symbol": benchmark.provider_symbol,
                    "provider": benchmark.provider,
                    "benchmark_name": benchmark.benchmark_name,
                    "steel_supported": benchmark.provider == YAHOO_PROVIDER and benchmark.provider_symbol == HRC_SYMBOL,
                },
            }
class LineItemRecordUpdateView(APIView):
    permission_classes = [AllowAny]

    def patch(self, request, row_id):
        row = get_object_or_404(
            ImportRow.objects.select_related(
                "import_batch",
            ),
            id=row_id,
            is_imported=True,
        )

        allowed_fields = {
            "product_name",
            "item_code",
            "part_number",
            "supplier",
            "quantity",
            "unit_of_measurement",
            "unit_price",
            "line_total",
            "currency",
            "purchase_date",
        }

        patch = {
            key: value
            for key, value in request.data.items()
            if key in allowed_fields
        }

        if not patch:
            return Response(
                {
                    "success": False,
                    "message": "No editable fields supplied.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        row.mapped_data = {
            **(row.mapped_data or {}),
            **patch,
        }

        row.normalized_data = {
            **(row.normalized_data or {}),
            **patch,
        }

        row.save(
            update_fields=[
                "mapped_data",
                "normalized_data",
                "updated_at",
            ]
        )

        price = PriceHistory.objects.filter(
            import_row_id=row.id,
            is_active=True,
        ).first()

        if price:
            if "quantity" in patch:
                price.quantity = _money(
                    patch.get("quantity")
                )

            if "unit_price" in patch:
                new_price = _money(
                    patch.get("unit_price")
                )

                if new_price is not None:
                    price.original_unit_price = new_price
                    price.converted_unit_price = new_price

            if "currency" in patch:
                price.currency = str(
                    patch.get("currency") or ""
                ).strip().upper()

            if "purchase_date" in patch:
                price.purchase_date = (
                    patch.get("purchase_date")
                    or None
                )

            if "unit_of_measurement" in patch:
                price.unit_of_measurement = (
                    patch.get("unit_of_measurement")
                    or ""
                )

            supplier_name = str(
                patch.get("supplier") or ""
            ).strip()

            if supplier_name:
                normalized_supplier = normalize_supplier(
                    supplier_name
                )

                supplier = Supplier.objects.filter(
                    normalized_supplier_name=normalized_supplier,
                    is_active=True,
                ).first()

                if supplier is None:
                    supplier = Supplier.objects.create(
                        supplier_name=supplier_name,
                        normalized_supplier_name=normalized_supplier,
                        is_active=True,
                    )

                price.supplier = supplier

            price.save()

        return ok(
            {
                "row_id": row.id,
            },
            "Product updated successfully.",
        )

class MarketPriceView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        data = []
        for benchmark in CommodityBenchmark.objects.filter(is_active=True).order_by("display_name"):
            latest = CommodityPriceHistory.objects.filter(benchmark=benchmark).order_by("-market_date", "-created_at").first()
            data.append({
                "id": benchmark.id,
                "display_name": benchmark.display_name,
                "provider_symbol": benchmark.provider_symbol,
                "provider": benchmark.provider,
                "is_supported": benchmark.is_supported,
                "latest": {
                    "id": latest.id,
                    "market_date": latest.market_date,
                    "aed_per_kg": latest.aed_per_kg,
                    "aed_per_metric_tonne": latest.aed_per_metric_tonne,
                    "usd_per_metric_tonne": latest.usd_per_metric_tonne,
                } if latest else None,
                "history_count": CommodityPriceHistory.objects.filter(benchmark=benchmark).count(),
            })
        return ok(data)

    def post(self, request):
        return ok(fetch_current_market_prices(force=True), "Market prices fetched.")


class HRCLatestMarketPriceView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        data = YahooHRCMarketPriceService().latest_response()
        if data is None:
            return ok(None, "No HRC market price is available yet.")
        return ok(data)


class HRCHistoryMarketPriceView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        days = request.query_params.get("days") or 30
        return ok(YahooHRCMarketPriceService().history_response(days=days))


def _to_decimal(value, default="0"):
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _first_present(data, *keys):
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def serialize_bid_costing(item):
    lines = []
    for line in item.items.all():
        source_data = line.source_import_row.normalized_data if line.source_import_row_id else {}
        product_id = source_data.get("line_group_key") or (f"market-{line.commodity.provider_symbol}" if line.commodity_id else (f"row-{line.source_import_row_id}" if line.source_import_row_id else f"bid-line-{line.id}"))
        batch = line.source_import_row.import_batch if line.source_import_row_id and line.source_import_row else None
        line_category = (
            source_data.get("category")
            or getattr(batch, "category", None)
            or line.source_type_snapshot
            or ""
        )
        line_document_type = (
            source_data.get("document_type")
            or source_data.get("source_type")
            or getattr(batch, "category", None)
            or line.source_type_snapshot
            or ""
        )

        lines.append({
            "id": line.id,
            "rowId": line.source_import_row_id,
            "productId": product_id,
            "commodityId": line.commodity_id,
            "commodityPriceHistoryId": line.commodity_price_history_id,
            "pricingSourceType": line.pricing_source_type,
            "sourceProvider": line.source_provider,
            "marketRate": float(line.market_rate) if line.market_rate is not None else None,
            "marketUnit": line.market_unit,
            "marketCurrency": line.market_currency,
            "marketDate": line.market_date.isoformat() if line.market_date else None,
            "requiresReview": line.requires_review,
            "product": line.product_name_snapshot,
            "supplier": line.supplier_name_snapshot,
            "source": line_document_type,
            "category": line_category,
            "documentType": line_document_type,
            "sourceFile": line.source_file_name_snapshot,
            "quantity": float(line.quantity),
            "unit": line.unit_of_measurement,
            "unitRate": float(line.unit_rate),
            "currency": line.currency,
            "lineTotal": float(line.line_total),
        })
    currency_set = {line.get("currency") for line in lines if line.get("currency")}
    display_currency = item.currency
    if len(currency_set) > 1:
        display_currency = "MIXED"
    return {
        "id": item.id,
        "bidNo": item.bid_costing_number,
        "title": item.title,
        "project": item.project_name,
        "customer": item.customer_name,
        "location": item.location,
        "remarks": item.remarks,
        "productCount": item.items.count(),
        "totalAmount": float(item.grand_total),
        "currency": display_currency,
        "createdBy": item.created_by.name if item.created_by_id else "System",
        "createdDate": item.created_at.date().isoformat() if item.created_at else "",
        "createdAt": item.created_at.isoformat() if item.created_at else "",
        "updatedAt": item.updated_at.isoformat() if item.updated_at else "",
        "status": item.status,
        "lines": lines,
        "charges": {
            "freight": float(item.freight),
            "other": float(item.other_charges),
            "taxPercentage": float(item.tax_percentage),
            "tax": float(item.tax_amount),
        },
        "materialSubtotal": float(item.material_subtotal),
        "grandTotal": float(item.grand_total),
    }


class BidCostingView(APIView):
    permission_classes = [AllowAny]

    def _currency_for_lines(self, lines, fallback="AED"):
        line_currencies = {str(line.get("currency") or "").upper() for line in lines if line.get("currency")}
        if len(line_currencies) == 1:
            return next(iter(line_currencies))
        if len(line_currencies) > 1:
            return "MIXED"
        return fallback or "AED"

    def _save_items(self, bid, lines):
        BidCostingItem.objects.filter(bid_costing=bid).delete()
        objects = []
        for index, line in enumerate(lines):
            quantity = _to_decimal(line.get("quantity"), "1")
            unit_rate = _to_decimal(line.get("unitRate"))
            line_total = (quantity * unit_rate).quantize(Decimal("0.01"))
            source_row = None
            row_id = line.get("rowId")
            if row_id and str(row_id).isdigit():
                source_row = ImportRow.objects.filter(id=row_id).first()
            row_data = source_row.normalized_data if source_row else {}
            line_category = (
                line.get("category")
                or row_data.get("category")
                or getattr(getattr(source_row, "import_batch", None), "category", None)
                or line.get("documentType")
                or line.get("document_type")
                or line.get("source")
                or ""
            )
            objects.append(BidCostingItem(
                bid_costing=bid,
                product=getattr(source_row, "resolved_product", None),
                source_import_row=source_row,
                supplier=getattr(getattr(source_row, "import_batch", None), "supplier", None),
                product_name_snapshot=line.get("product") or "Unnamed item",
                item_code_snapshot=line.get("itemCode") or "",
                part_number_snapshot=line.get("partNumber") or "",
                supplier_name_snapshot=line.get("supplier") or "",
                source_type_snapshot=line_category,
                source_reference_snapshot=line.get("sourceReference") or "",
                source_file_name_snapshot=line.get("sourceFile") or "",
                quantity=quantity,
                unit_of_measurement=line.get("unit") or "",
                unit_rate=unit_rate,
                currency=line.get("currency") or (bid.currency if bid.currency != "MIXED" else "AED"),
                exchange_rate=Decimal("1.00000000"),
                converted_unit_rate=unit_rate,
                line_total=line_total,
                sort_order=index,
            ))
        BidCostingItem.objects.bulk_create(objects)

    def _apply_totals(self, bid, charges):
        bid.freight = _to_decimal(charges.get("freight"))
        bid.other_charges = _to_decimal(charges.get("other"))
        tax_percentage_value = _first_present(
            charges,
            "taxPercentage",
            "tax_percentage",
            "taxPercent",
            "tax_percent",
            "vatPercentage",
            "vat_percentage",
        )
        tax_amount_value = _first_present(charges, "tax", "taxAmount", "tax_amount", "vat", "vatAmount", "vat_amount")
        bid.tax_percentage = _to_decimal(tax_percentage_value)
        bid.calculate_totals(save=False)
        if not bid.tax_percentage and tax_amount_value not in (None, ""):
            bid.tax_amount = _to_decimal(tax_amount_value)
            bid.grand_total = bid.material_subtotal + bid.freight + bid.other_charges + bid.tax_amount
        BidCosting.objects.filter(id=bid.id).update(
            material_subtotal=bid.material_subtotal,
            freight=bid.freight,
            other_charges=bid.other_charges,
            tax_percentage=bid.tax_percentage,
            tax_amount=bid.tax_amount,
            grand_total=bid.grand_total,
        )

    def get(self, request):
        qs = BidCosting.objects.prefetch_related("items").all()[:100]
        return ok([serialize_bid_costing(item) for item in qs])

    def post(self, request):
        payload = request.data or {}
        lines = payload.get("lines") or []
        charges = payload.get("charges") or {}
        bid = BidCosting.objects.create(
            title=payload.get("title") or "New material bid costing",
            project_name=payload.get("project") or "",
            customer_name=payload.get("customer") or "",
            location=payload.get("location") or "",
            currency=self._currency_for_lines(lines, payload.get("currency") or "AED"),
            remarks=payload.get("remarks") or "",
            freight=_to_decimal(charges.get("freight")),
            other_charges=_to_decimal(charges.get("other")),
            tax_percentage=_to_decimal(_first_present(
                charges,
                "taxPercentage",
                "tax_percentage",
                "taxPercent",
                "tax_percent",
                "vatPercentage",
                "vat_percentage",
            )),
            tax_amount=_to_decimal(_first_present(charges, "tax", "taxAmount", "tax_amount", "vat", "vatAmount", "vat_amount")),
            created_by=None,
        )
        self._save_items(bid, lines)
        self._apply_totals(bid, charges)
        return ok(serialize_bid_costing(BidCosting.objects.prefetch_related("items").get(id=bid.id)), "Bid costing draft saved.", status.HTTP_201_CREATED)

    def put(self, request, bid_id=None):
        bid = get_object_or_404(BidCosting, id=bid_id)
        payload = request.data or {}
        lines = payload.get("lines") or []
        charges = payload.get("charges") or {}
        bid.title = payload.get("title") or bid.title
        bid.project_name = payload.get("project") or ""
        bid.customer_name = payload.get("customer") or ""
        bid.location = payload.get("location") or ""
        bid.remarks = payload.get("remarks") or ""
        bid.currency = self._currency_for_lines(lines, payload.get("currency") or bid.currency)
        bid.save(update_fields=["title", "project_name", "customer_name", "location", "remarks", "currency", "updated_at"])
        self._save_items(bid, lines)
        self._apply_totals(bid, charges)
        return ok(serialize_bid_costing(BidCosting.objects.prefetch_related("items").get(id=bid.id)), "Bid costing draft updated.")

    def delete(self, request, bid_id=None):
        bid = get_object_or_404(BidCosting, id=bid_id)
        bid.delete()
        return ok(message="Bid costing deleted.")

class ProductResolutionView(APIView):
    permission_classes = [AllowAny]
    def post(self, request, batch_id):
        batch = get_object_or_404(ImportBatch, id=batch_id)
        row_ids = request.data.get("row_ids") or []
        action = request.data.get("action")
        rows = ImportRow.objects.filter(import_batch=batch, id__in=row_ids).select_related("product_match")
        if action == "ignore":
            rows.update(validation_status="ignored")
            return ok({"updated_rows": rows.count()}, "Rows ignored.")
        if action == "use_existing":
            product = get_object_or_404(Product, id=request.data.get("product_id"))
            for row in rows:
                row.product_match.confirmed_product = product
                row.product_match.status = ProductMatchStatus.MATCHED
                row.product_match.match_method = "manual"
                row.product_match.save(update_fields=["confirmed_product", "status", "match_method", "updated_at"])
            return ok({"updated_rows": rows.count()}, "Product resolution saved.")
        if action == "create_new":
            for row in rows:
                row.product_match.confirmed_product = None
                row.product_match.status = ProductMatchStatus.NEW_PRODUCT
                row.product_match.match_method = "new_product"
                row.product_match.save(update_fields=["confirmed_product", "status", "match_method", "updated_at"])
            return ok({"updated_rows": rows.count()}, "Rows marked for new product creation.")
        return Response({"success": False, "message": "Invalid product resolution action.", "errors": {"action": ["Use use_existing, create_new or ignore."]}}, status=400)

class PurchaseQuoteDownloadDashboardView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        PurchaseAutoExtractionService.reconcile_completed_zips()
        downloaded_date = request.query_params.get("downloaded_date")
        file_type = str(request.query_params.get("file_type") or "").lower()
        extraction_status = str(request.query_params.get("extraction_status") or "").lower()
        file_search = str(request.query_params.get("search") or "").strip()
        try:
            page_number = max(int(request.query_params.get("page", 1)), 1)
            page_size = min(max(int(request.query_params.get("page_size", 15)), 1), 100)
        except (TypeError, ValueError):
            page_number, page_size = 1, 15

        # Reconcile archive parents on every dashboard read. ZIP/RAR extraction
        # creates jobs for archive members, not for the parent filename, so an
        # older parent can otherwise remain "downloaded" forever even after
        # all child jobs are imported/reviewed.
        archive_parent_ids = list(
            PurchaseQuoteDownload.objects.filter(
                extraction_status__in=["downloaded", "queued"],
                extraction_batch_id__isnull=False,
                file_extension__in=[".zip", ".rar"],
            ).values_list("id", "extraction_batch_id")
        )
        terminal_batches = {
            batch.id: batch.status
            for batch in BulkImportBatch.objects.filter(
                id__in=[batch_id for _, batch_id in archive_parent_ids],
                status__in=["completed", "completed_with_errors", "failed"],
            )
        }
        for parent_id, batch_id in archive_parent_ids:
            batch_status = terminal_batches.get(batch_id)
            if batch_status:
                PurchaseQuoteDownload.objects.filter(id=parent_id).update(
                    extraction_status="failed" if batch_status == "failed" else "extracted",
                    error_message="Archive extraction failed." if batch_status == "failed" else None,
                    extraction_completed_at=timezone.now(),
                    updated_at=timezone.now(),
                )

        queryset = PurchaseQuoteDownload.objects.filter(
            status="downloaded"
        )

        if downloaded_date:
            queryset = queryset.filter(
                downloaded_at__date=downloaded_date
            )
        if file_type == "pdf":
            queryset = queryset.filter(file_extension__iexact=".pdf")
        elif file_type in {"archive", "archives"}:
            queryset = queryset.filter(
                Q(file_extension__iexact=".zip") | Q(file_extension__iexact=".rar")
            )
        if extraction_status in {"downloaded", "queued", "extracted", "failed", "duplicate"}:
            queryset = queryset.filter(extraction_status=extraction_status)
        if file_search:
            queryset = queryset.filter(file_name__icontains=file_search)

        paginator = Paginator(queryset.order_by("-downloaded_at"), page_size)
        page = paginator.get_page(page_number)
        count_queryset = PurchaseQuoteDownload.objects.filter(status="downloaded")
        if downloaded_date:
            count_queryset = count_queryset.filter(downloaded_at__date=downloaded_date)
        if file_search:
            count_queryset = count_queryset.filter(file_name__icontains=file_search)

        counts = count_queryset.aggregate(
            total=Count("pk"),
            pdf=Count("pk", filter=Q(file_extension__iexact=".pdf")),
            archive=Count("pk", filter=Q(file_extension__iexact=".zip") | Q(file_extension__iexact=".rar")),
            **{f"status_{value}": Count("pk", filter=Q(extraction_status=value))
               for value in ("downloaded", "queued", "extracted", "failed", "duplicate")},
        )
        pdf_count = counts["pdf"]
        archive_count = counts["archive"]
        extraction_counts = {
            status: counts[f"status_{status}"]
            for status in ("downloaded", "queued", "extracted", "failed", "duplicate")
        }

        latest_download = queryset.order_by("-downloaded_at").first()

        return ok({
            "downloaded_date": downloaded_date,
            "total_downloaded": counts["total"],
            "pdf_downloaded": pdf_count,
            "latest_downloaded_at": latest_download.downloaded_at if latest_download else None,
            "files": [
                {
                    "id": item.id,
                    "file_name": item.file_name,
                    "file_extension": item.file_extension,
                    "file_size": item.file_size,
                    "quote_date": item.quote_date,
                    "downloaded_at": item.downloaded_at,
                    "status": item.status,
                    "extraction_status": item.extraction_status,
                    "extraction_batch_id": item.extraction_batch_id,
                    "extraction_completed_at": item.extraction_completed_at,
                    "extraction_error": item.error_message,
                    "source_page": item.source_page,
                    "source_row": item.source_row,
                }
                for item in page.object_list
            ],
            "total_pages": paginator.num_pages,
            "page": page.number,
            "page_size": page_size,
            "total_files": paginator.count,
            "archive_downloaded": archive_count,
            "extraction_status_filter": extraction_status or None,
            "extraction_counts": extraction_counts,
        })


class PurchaseQuoteDownloadFileView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, download_id):
        source = get_object_or_404(
            PurchaseQuoteDownload,
            id=download_id,
            status="downloaded",
        )
        path = Path(source.local_path)
        if not path.is_file():
            return Response({"success": False, "message": "Source file is no longer available."}, status=404)

        response = FileResponse(
            path.open("rb"),
            as_attachment=False,
            filename=source.file_name,
            content_type=mimetypes.guess_type(source.file_name)[0] or "application/octet-stream",
        )
        response["Content-Disposition"] = f'inline; filename="{source.file_name.replace(chr(34), "")}"'
        return response


class PurchaseAutoExtractionStartView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        limit = request.data.get("limit")
        all_files = bool(request.data.get("all_files"))
        try:
            result = PurchaseAutoExtractionService().start(
                limit=limit,
                all_files=all_files,
                user=(getattr(request.user, "price_intelligence_profile", None) if request.user.is_authenticated else None),
            )
            return ok(result, "Automatic extraction queued.", status.HTTP_202_ACCEPTED)
        except Exception as exc:
            logger.exception("Automatic Purchase Software extraction failed")
            return fail(ImportServiceError("Automatic extraction failed.", {"detail": [str(exc)]}))
