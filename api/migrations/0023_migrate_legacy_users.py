from django.db import migrations


def copy_legacy_users(apps, schema_editor):
    """Copy the old auth.User/AppUser data into the new Django user model."""
    PriceUser = apps.get_model("api", "PriceUser")
    LegacyProfile = apps.get_model("api", "AppUser")

    profiles = list(LegacyProfile.objects.all())
    by_email = {str(p.email or "").strip().lower(): p for p in profiles if p.email}
    by_employee = {str(p.employee_code or "").strip().lower(): p for p in profiles if p.employee_code}

    # auth.User is swapped after AUTH_USER_MODEL is enabled, so its historical
    # manager cannot be used here. Read the already-existing legacy table
    # directly and write into the new PriceUser table.
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT username, password, last_login, is_superuser, first_name, "
            "last_name, email, is_staff, is_active, date_joined FROM auth_user"
        )
        legacy_users = cursor.fetchall()

    for row in legacy_users:
        (
            username, password, last_login, is_superuser, first_name,
            last_name, email_value, is_staff, is_active, date_joined,
        ) = row
        email = (email_value or "").strip().lower()
        profile = by_email.get(email)
        employee_code = profile.employee_code if profile else None
        # The migration can be retried after a partial SQL Server transaction,
        # and an admin may already have been created manually. Do not insert a
        # second row when either unique identity is already present.
        if (
            PriceUser.objects.filter(username=username).exists()
            or (employee_code and PriceUser.objects.filter(employee_code=employee_code).exists())
            or (email and PriceUser.objects.filter(email__iexact=email).exists())
        ):
            continue
        PriceUser.objects.create(
            username=username,
            password=password,
            last_login=last_login,
            is_superuser=is_superuser,
            first_name=first_name,
            last_name=last_name,
            email=email_value,
            is_staff=is_staff,
            is_active=is_active if profile is None else profile.is_active,
            date_joined=date_joined,
            full_name=(profile.name if profile else f"{first_name} {last_name}").strip() or username,
            employee_code=employee_code,
            requested_role=(profile.role if profile else "admin" if is_superuser else "user"),
            role=(profile.role if profile else "admin" if is_superuser else "user"),
            is_approved=(profile.is_approved if profile else is_superuser),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0022_alter_priceuser_groups_and_more"),
    ]

    operations = [migrations.RunPython(copy_legacy_users, migrations.RunPython.noop)]
