from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0025_validate_user_foreign_keys")]
    operations = [migrations.AddField(
        model_name="priceuser", name="is_rejected",
        field=models.BooleanField(default=False),
    )]
