# The Temporal workflow run that wrote each chat routing policy row, unique
# when set, so a run writes at most one row however often its activity is
# retried. Rows from the compute_chat_policy command leave it empty (NULL).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0215_chatroutingpolicy"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatroutingpolicy",
            name="run_id",
            field=models.CharField(
                blank=True,
                default=None,
                help_text=(
                    "The Temporal workflow run that wrote this row; each run "
                    "writes at most one. Empty for rows from the "
                    "compute_chat_policy command."
                ),
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="chatroutingpolicy",
            constraint=models.UniqueConstraint(
                condition=models.Q(("run_id__isnull", False)),
                fields=("run_id",),
                name="uniq_chatroutingpolicy_run_id",
            ),
        ),
    ]
