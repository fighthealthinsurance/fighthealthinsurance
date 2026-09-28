# Joins the reply check's migrations (0221_chatturn_jev_tiers) with main's
# (0221_merge_policy_run_id_and_spend, 0221_merge_shadow_scores_and_policy).
# No operations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0221_chatturn_jev_tiers"),
        ("fighthealthinsurance", "0221_merge_policy_run_id_and_spend"),
        ("fighthealthinsurance", "0221_merge_shadow_scores_and_policy"),
    ]

    operations = []
