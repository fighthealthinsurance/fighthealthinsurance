"""Drop the (misleading) ``max_length=300`` on ``ClinicalTrialQueryData.query``.

``find_trials_for_denial`` builds the cache key by concatenating
``Denial.procedure`` and ``Denial.diagnosis`` (each up to 300 chars on
``Denial``), so the combined query can legitimately exceed 300 characters.
PostgreSQL doesn't enforce ``max_length`` on ``TextField`` at the database
level, but the cap still trips form/admin validators. Matches the existing
fix on ``NICEQueryData.query``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0180_insurancecompany_pa_requirement_list_url"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clinicaltrialquerydata",
            name="query",
            field=models.TextField(),
        ),
    ]
