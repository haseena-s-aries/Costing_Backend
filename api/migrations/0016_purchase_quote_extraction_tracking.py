from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0015_extractionprofile")]

    operations = [
        migrations.AddField(
            model_name="purchasequotedownload",
            name="file_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="purchasequotedownload",
            name="extraction_batch_id",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="purchasequotedownload",
            name="extraction_status",
            field=models.CharField(db_index=True, default="downloaded", max_length=30),
        ),
    ]
