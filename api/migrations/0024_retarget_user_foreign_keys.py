"""Repair physical foreign keys left behind by the AUTH_USER_MODEL switch.

The migration state already points to PriceUser, so an ordinary AlterField
would not detect the old database relationship. Keep legacy auth tables intact.
"""
import copy

from django.db import migrations


def retarget_users(apps, schema_editor):
    connection = schema_editor.connection
    alias = connection.alias
    quote = schema_editor.quote_name
    old_user = apps.get_model("auth", "User")
    new_user = apps.get_model("api", "PriceUser")
    targets = [("admin", "LogEntry", "user"),
               ("authtoken", "Token", "user"),
               ("api", "AppUser", "auth_user")]
    pending = []
    with connection.cursor() as cursor:
        for app_label, model_name, field_name in targets:
            model = apps.get_model(app_label, model_name)
            table = model._meta.db_table
            field = model._meta.get_field(field_name)
            constraints = connection.introspection.get_constraints(cursor, table)
            legacy_fks = [name for name, info in constraints.items()
                          if info.get("foreign_key") == (old_user._meta.db_table, "id")
                          and info["columns"] == [field.column]]
            if legacy_fks:
                pending.append((model, field, legacy_fks))
        if not pending:
            return
        cursor.execute("SELECT id, username FROM " + quote(old_user._meta.db_table))
        legacy_names = dict(cursor.fetchall())

    current_ids = dict(new_user.objects.using(alias).values_list("username", "id"))
    mapping = {old_id: current_ids[name] for old_id, name in legacy_names.items()
               if name in current_ids}
    # Validate every reference before dropping any constraint. Never guess an
    # identity from matching numeric IDs or from a shared email address.
    for model, field, _ in pending:
        used = set(model.objects.using(alias).exclude(**{field.attname: None})
                   .values_list(field.attname, flat=True))
        missing = used - mapping.keys()
        if missing:
            raise RuntimeError(
                f"Cannot migrate {model._meta.db_table}: legacy user IDs "
                f"{sorted(missing)} have no matching PriceUser username. "
                "Restore/migrate those accounts before retrying."
            )

    for model, field, constraint_names in pending:
        table = quote(model._meta.db_table)
        column = quote(field.column)
        for name in constraint_names:
            schema_editor.execute(f"ALTER TABLE {table} DROP CONSTRAINT {quote(name)}")
        # One CASE update avoids collisions when old/new ID ranges overlap.
        if mapping:
            cases = " ".join("WHEN %s THEN %s" for _ in mapping)
            params = [value for pair in mapping.items() for value in pair]
            schema_editor.execute(
                f"UPDATE {table} SET {column} = CASE {column} {cases} ELSE {column} END",
                params,
            )
        old_field = copy.copy(field)
        old_field.remote_field = copy.copy(field.remote_field)
        old_field.remote_field.model = old_user
        # Invalidate cached target-field resolution on the copied field.
        for key in ("related_fields", "reverse_related_fields", "local_related_fields",
                    "foreign_related_fields", "path_infos", "reverse_path_infos"):
            old_field.__dict__.pop(key, None)
        # Backend schema editor also handles int -> bigint and dependent indexes.
        schema_editor.alter_field(model, old_field, field)


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0023_migrate_legacy_users"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("authtoken", "0004_alter_tokenproxy_options"),
    ]
    operations = [migrations.RunPython(retarget_users)]
