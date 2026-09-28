# Joins the chat shadow scores (0220_merge_shadow_scores_and_spend) with the
# routing policy now on main (0220_merge_chat_policy_and_spend). No
# operations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0220_merge_chat_policy_and_spend"),
        ("fighthealthinsurance", "0220_merge_shadow_scores_and_spend"),
    ]

    operations = []
