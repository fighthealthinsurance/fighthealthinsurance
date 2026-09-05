"""Add the per-denial generation lease table (see generation_lease.py).

Empty on creation and touched only by generation entry points, so this is
dark-safe: while the appeal journey is off no journey ever holds a lease,
and the interactive path's steal is one cheap UPDATE with no behavior change.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0201_proposedappeal_text_fingerprint"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppealGenerationLease",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("holder", models.CharField(blank=True, default="", max_length=128)),
                ("expires_at", models.DateTimeField()),
                ("deadline", models.DateTimeField()),
                ("epoch", models.PositiveIntegerField(default=0)),
                (
                    "for_denial",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="generation_lease",
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
        ),
    ]
