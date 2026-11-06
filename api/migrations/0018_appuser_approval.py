from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0017_purchasequotedownload_extraction_completed_at")]
    operations = [
        migrations.AddField(
            model_name="appuser",
            name="is_approved",
            field=models.BooleanField(db_index=True, default=True),
        ),
    ]
