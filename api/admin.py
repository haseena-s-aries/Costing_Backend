from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import AppUser, PriceUser

@admin.register(AppUser)
class AppUserAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "employee_code", "role", "is_approved", "is_active")
    list_filter = ("role", "is_approved", "is_active")
    search_fields = ("name", "email", "employee_code")
    # These profiles remain for historical purchase/bid ownership only.
    # Active login accounts are managed through PriceUser/UserAdmin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(PriceUser)
class PriceUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Priceora", {"fields": ("full_name", "employee_code", "requested_role", "role", "is_approved")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Priceora", {"fields": ("email", "full_name", "employee_code", "requested_role", "role", "is_approved")}),
    )
    list_display = ("username", "email", "employee_code", "role", "is_approved", "is_staff", "is_active")
