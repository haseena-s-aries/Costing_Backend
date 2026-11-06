from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0016_purchase_quote_extraction_tracking")]

    operations = [
        migrations.AddField(
            model_name="purchasequotedownload",
            name="extraction_completed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
