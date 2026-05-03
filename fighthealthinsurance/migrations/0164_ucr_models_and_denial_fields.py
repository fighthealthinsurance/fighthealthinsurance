# Hand-written migration for UCR feature (see UCR-OON-Reimbursement-Plan.md §4.3).
# Format matches Django 5.2.9 auto-generated output.

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Now

import fighthealthinsurance.encrypted_amount_field


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0163_imrdecision_pg_trgm_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="UCRGeographicArea",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("zip3", "ZIP3"),
                            ("msa", "MSA"),
                            ("state", "State"),
                            ("national", "National"),
                            ("medicare_locality", "Medicare Locality"),
                        ],
                        max_length=24,
                    ),
                ),
                ("code", models.CharField(db_index=True, max_length=32)),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=200),
                ),
            ],
            options={
                "unique_together": {("kind", "code")},
            },
        ),
        migrations.CreateModel(
            name="UCRRate",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("procedure_code", models.CharField(db_index=True, max_length=10)),
                ("modifier", models.CharField(blank=True, default="", max_length=4)),
                ("percentile", models.PositiveSmallIntegerField()),
                ("amount_cents", models.PositiveIntegerField()),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("medicare_pfs", "CMS Medicare PFS"),
                            ("fair_health", "FAIR Health"),
                            ("fhi_aggregate", "FHI Aggregate"),
                        ],
                        max_length=32,
                    ),
                ),
                ("effective_date", models.DateField()),
                ("expires_date", models.DateField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "geographic_area",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rates",
                        to="fighthealthinsurance.ucrgeographicarea",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=[
                            "procedure_code",
                            "geographic_area",
                            "effective_date",
                            "expires_date",
                        ],
                        name="ucr_rate_lookup_idx",
                    ),
                    models.Index(
                        fields=["source", "effective_date"],
                        name="ucr_rate_source_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(percentile__gte=1)
                        & models.Q(percentile__lte=100),
                        name="ucr_rate_percentile_range",
                    ),
                ],
                "unique_together": {
                    (
                        "procedure_code",
                        "modifier",
                        "geographic_area",
                        "percentile",
                        "source",
                        "effective_date",
                    )
                },
            },
        ),
        migrations.CreateModel(
            name="UCRLookup",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("procedure_code", models.CharField(max_length=10)),
                ("modifier", models.CharField(blank=True, default="", max_length=4)),
                ("service_zip", models.CharField(blank=True, default="", max_length=5)),
                ("rates_snapshot", models.JSONField()),
                (
                    "billed_amount_cents",
                    fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                        blank=True, default="", max_length=512
                    ),
                ),
                (
                    "allowed_amount_cents",
                    fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                        blank=True, default="", max_length=512
                    ),
                ),
                (
                    "paid_amount_cents",
                    fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                        blank=True, default="", max_length=512
                    ),
                ),
                ("created", models.DateTimeField(db_default=Now())),
                (
                    "denial",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ucr_lookups",
                        to="fighthealthinsurance.denial",
                    ),
                ),
                (
                    "matched_area",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="fighthealthinsurance.ucrgeographicarea",
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
                "indexes": [
                    models.Index(
                        fields=["denial", "created"],
                        name="ucr_lookup_denial_idx",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="denial",
            name="service_zip",
            field=models.CharField(blank=True, default="", max_length=5),
        ),
        migrations.AddField(
            model_name="denial",
            name="procedure_code",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=10
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="procedure_modifier",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
        migrations.AddField(
            model_name="denial",
            name="billed_amount_cents",
            field=fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                blank=True, default="", max_length=512
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="allowed_amount_cents",
            field=fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                blank=True, default="", max_length=512
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="paid_amount_cents",
            field=fighthealthinsurance.encrypted_amount_field.EncryptedAmountField(
                blank=True, default="", max_length=512
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="ucr_refreshed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="latest_ucr_lookup",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="fighthealthinsurance.ucrlookup",
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="ucr_context",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
