import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Provider",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("full_name", models.CharField(max_length=120)),
                ("city", models.CharField(max_length=80)),
                ("district", models.CharField(max_length=80)),
                ("phone", models.CharField(max_length=20)),
                ("rating", models.DecimalField(decimal_places=1, default=5.0, max_digits=2)),
                ("is_available", models.BooleanField(default=True)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-is_available", "-rating", "full_name"],
            },
        ),
        migrations.CreateModel(
            name="ServiceType",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80, unique=True)),
                ("slug", models.SlugField(unique=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="ServiceRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("customer_name", models.CharField(max_length=120)),
                ("customer_phone", models.CharField(max_length=20)),
                ("city", models.CharField(max_length=80)),
                ("district", models.CharField(max_length=80)),
                ("details", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[("new", "Yeni"), ("matched", "Eslestirildi"), ("completed", "Tamamlandi")],
                        default="new",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "matched_provider",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="service_requests",
                        to="Myapp.provider",
                    ),
                ),
                (
                    "service_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="requests",
                        to="Myapp.servicetype",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="provider",
            name="service_type",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="providers", to="Myapp.servicetype"),
        ),
    ]
