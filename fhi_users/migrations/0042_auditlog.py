# Generated manually for simplified audit logging

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fhi_users", "0041_professionaluser_credentials"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("timestamp", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("event_type", models.CharField(db_index=True, max_length=50)),
                ("description", models.TextField(blank=True, default="")),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("username", models.CharField(blank=True, default="", max_length=255)),
                ("is_professional", models.BooleanField(default=False)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True, default="")),
                ("path", models.CharField(blank=True, default="", max_length=2048)),
                ("method", models.CharField(blank=True, default="", max_length=10)),
                ("status_code", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("response_time_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("extra_data", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "ordering": ["-timestamp"],
            },
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["user", "timestamp"], name="fhi_users_a_user_id_abc123_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["event_type", "timestamp"], name="fhi_users_a_event_t_def456_idx"),
        ),
    ]
