from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("Myapp", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="provider",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
    ]
