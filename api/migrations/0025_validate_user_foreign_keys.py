from django.db import migrations


def ensure_user_constraints(apps, schema_editor):
    quote = schema_editor.quote_name
    user = apps.get_model("api", "PriceUser")
    for app_label, model_name, field_name in (
        ("admin", "LogEntry", "user"),
        ("authtoken", "Token", "user"),
        ("api", "AppUser", "auth_user"),
    ):
        model = apps.get_model(app_label, model_name)
        table = model._meta.db_table
        field = model._meta.get_field(field_name)
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(cursor, table)
        if any(info.get("foreign_key") == (user._meta.db_table, user._meta.pk.column)
               and info["columns"] == [field.column] for info in constraints.values()):
            continue
        name = f"{table}_{field.column}_priceuser_fk"
        schema_editor.execute(
            f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(name)} "
            f"FOREIGN KEY ({quote(field.column)}) REFERENCES "
            f"{quote(user._meta.db_table)} ({quote(user._meta.pk.column)})"
        )


class Migration(migrations.Migration):
    dependencies = [("api", "0024_retarget_user_foreign_keys")]
    operations = [migrations.RunPython(ensure_user_constraints)]
