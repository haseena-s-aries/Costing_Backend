from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0019_remove_appuser_is_approved_and_more")]
    operations = [
        migrations.RemoveField(
            model_name="commoditybenchmark",
            name="is_approved",
        ),
        migrations.AddField(
            model_name="appuser",
            name="is_approved",
            field=models.BooleanField(db_index=True, default=True),
        ),
    ]
