from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import (
    AppUser,
    BidCosting,
    BulkImportBatch,
    ImportBatch,
    ImportColumnMapping,
    MappingTemplate,
    PriceHistory,
    PriceUser,
    UserRole,
)


def _parse_date(value, fallback):
    try:
        return date.fromisoformat(str(value)) if value else fallback
    except ValueError:
        return fallback


def _user_label(user):
    return user.full_name or user.get_full_name() or user.email or user.username


class UsageAnalyticsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        actor = request.user
        if not actor.is_superuser and actor.role != UserRole.ADMIN:
            return Response({"success": False, "message": "Usage Analytics is available to administrators only."}, status=403)

        today = timezone.localdate()
        start = _parse_date(request.query_params.get("start"), today - timedelta(days=29))
        end = _parse_date(request.query_params.get("end"), today)
        if start > end:
            start, end = end, start
        start_dt = timezone.make_aware(datetime.combine(start, datetime.min.time()))
        end_dt = timezone.make_aware(datetime.combine(end + timedelta(days=1), datetime.min.time()))

        users = list(PriceUser.objects.filter(is_deleted=False).order_by("full_name", "username"))
        app_users = list(AppUser.objects.select_related("auth_user").filter(is_active=True))
        app_by_id = {str(item.id): item for item in app_users}
        user_by_app_id = {str(item.auth_user_id): item for item in app_users if item.auth_user_id}

        sources = [
            ("Import Batches", ImportBatch.objects.filter(uploaded_at__gte=start_dt, uploaded_at__lt=end_dt).select_related("uploaded_by"), "uploaded_at", "uploaded_by"),
            ("Bulk Import Batches", BulkImportBatch.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt).select_related("uploaded_by"), "created_at", "uploaded_by"),
            ("Price Records", PriceHistory.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt).select_related("created_by"), "created_at", "created_by"),
            ("Mapping Templates", MappingTemplate.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt).select_related("created_by"), "created_at", "created_by"),
            ("Column Mappings", ImportColumnMapping.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt).select_related("created_by"), "created_at", "created_by"),
            ("Bid Costings", BidCosting.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt).select_related("created_by"), "created_at", "created_by"),
        ]

        activity_by_user = Counter()
        last_by_user = {}
        module_rows = []
        monthly = defaultdict(lambda: {"activity_count": 0, "created_count": 0, "updated_count": 0, "users": set()})

        for module, queryset, created_field, creator_field in sources:
            rows = list(queryset)
            updated_count = 0
            module_last = None
            module_users = set()
            for row in rows:
                created = getattr(row, created_field, None)
                updated = getattr(row, "updated_at", None)
                if updated and created and updated > created:
                    updated_count += 1
                activity_date = created or updated
                if activity_date and (module_last is None or activity_date > module_last):
                    module_last = activity_date
                creator = getattr(row, creator_field, None)
                if creator:
                    key = str(creator.auth_user_id) if creator.auth_user_id else f"app:{creator.pk}"
                    activity_by_user[key] += 1
                    module_users.add(key)
                    last_by_user[key] = max(last_by_user.get(key, activity_date), activity_date)
                if activity_date:
                    month = activity_date.strftime("%Y-%m")
                    monthly[(month, module)]["activity_count"] += 1
                    monthly[(month, module)]["created_count"] += 1
                    if updated and created and updated > created:
                        monthly[(month, module)]["updated_count"] += 1
                    if creator:
                        monthly[(month, module)]["users"].add(key)
            module_rows.append({
                "module": module,
                "activity_count": len(rows),
                "created_count": len(rows),
                "updated_count": updated_count,
                "last_activity": module_last,
                "unique_active_users": len(module_users),
            })

        selected_role = request.query_params.get("role")
        if selected_role:
            users = [item for item in users if item.role == selected_role]

        active_ids = {key for key, value in activity_by_user.items() if value}
        # Status and last-activity must be based on the user's complete history,
        # not only records inside the currently selected reporting period.
        global_last_by_user = {}
        all_sources = [
            (ImportBatch, "uploaded_at", "uploaded_by"),
            (BulkImportBatch, "created_at", "uploaded_by"),
            (PriceHistory, "created_at", "created_by"),
            (MappingTemplate, "created_at", "created_by"),
            (ImportColumnMapping, "created_at", "created_by"),
            (BidCosting, "created_at", "created_by"),
        ]
        for model, date_field, creator_field in all_sources:
            for row in model.objects.select_related(creator_field).all().iterator():
                activity_date = getattr(row, date_field, None) or getattr(row, "updated_at", None)
                creator = getattr(row, creator_field, None)
                if creator and activity_date:
                    key = str(creator.auth_user_id) if creator.auth_user_id else f"app:{creator.pk}"
                    previous = global_last_by_user.get(key)
                    if previous is None or activity_date > previous:
                        global_last_by_user[key] = activity_date

        # Django already stores the most recent successful login on the user.
        # Include it so a user who only logs in/views the app is not marked inactive.
        for user in users:
            if user.last_login:
                key = str(user.pk)
                previous = global_last_by_user.get(key)
                if previous is None or user.last_login > previous:
                    global_last_by_user[key] = user.last_login

        user_rows = []
        for user in users:
            key = str(user.pk)
            app = user_by_app_id.get(key)
            activity_count = activity_by_user.get(key, 0)
            last = global_last_by_user.get(key)
            if not last:
                status = "Never Active"
            else:
                age = (timezone.now() - last).days
                status = "Active" if age < 30 else "Inactive 30+ Days" if age < 60 else "Inactive 60+ Days" if age < 90 else "Inactive 90+ Days"
            user_rows.append({
                "id": str(user.pk), "name": _user_label(user), "email": user.email,
                "role": user.role, "department": None, "activity_count": activity_count,
                "last_activity": last, "activity_status": status,
            })

        role_summary = []
        for role in UserRole.values:
            members = [item for item in users if item.role == role]
            role_summary.append({
                "role": role,
                "user_count": len(members),
                "active_users": sum(1 for item in members if activity_by_user.get(str(item.pk), 0) > 0),
            })

        months = []
        cursor = date(today.year, today.month, 1)
        for _ in range(12):
            months.append(cursor.strftime("%Y-%m"))
            cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
        trend = [{"month": month, "module": module, "activity_count": value["activity_count"], "created_count": value["created_count"], "updated_count": value["updated_count"], "unique_active_users": len(value["users"])} for (month, module), value in sorted(monthly.items()) if month in months]

        # File extraction trend: one count per uploaded/extracted batch, not per
        # extracted row or price record.
        file_trend = {month: 0 for month in months}
        file_models = ((ImportBatch, "uploaded_at"), (BulkImportBatch, "created_at"))
        trend_start = date(today.year, today.month, 1)
        for _ in range(11):
            trend_start = date(trend_start.year - 1, 12, 1) if trend_start.month == 1 else date(trend_start.year, trend_start.month - 1, 1)
        trend_start_dt = timezone.make_aware(datetime.combine(trend_start, datetime.min.time()))
        for model, date_field in file_models:
            for row in model.objects.filter(**{f"{date_field}__gte": trend_start_dt}).only(date_field).iterator():
                value = getattr(row, date_field, None)
                if value:
                    month = value.strftime("%Y-%m")
                    if month in file_trend:
                        file_trend[month] += 1

        return Response({"success": True, "data": {
            "period": {"start": start, "end": end},
            "user_summary": {"total": len(users), "active": sum(1 for item in users if item.is_active), "inactive": sum(1 for item in users if not item.is_active), "active_in_range": sum(1 for item in user_rows if item["last_activity"] and start_dt <= item["last_activity"] < end_dt), "no_activity_30": sum(1 for item in user_rows if item["activity_status"] == "Inactive 30+ Days"), "no_activity_60": sum(1 for item in user_rows if item["activity_status"] in {"Inactive 60+ Days", "Inactive 90+ Days", "Never Active"}), "no_activity_90": sum(1 for item in user_rows if item["activity_status"] in {"Inactive 90+ Days", "Never Active"})},
            "role_summary": role_summary, "department_summary": None, "modules": module_rows, "users": user_rows,
            "monthly_trend": trend, "monthly_file_extractions": [{"month": month, "count": file_trend[month]} for month in months], "limitations": {"departments": "Not available: no department/team field exists.", "login_activity": "Not available: no login/audit log table exists.", "page_views": "Not tracked in Version 1."},
        }})
