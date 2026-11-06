from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0026_priceuser_is_rejected")]
    operations = [migrations.AddField(
        model_name="priceuser", name="is_deleted",
        field=models.BooleanField(default=False),
    )]
