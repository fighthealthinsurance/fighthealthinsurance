# Joins the policy schedule's run id with the routing-policy and budget
# branches of the graph. No schema change.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0218_chatroutingpolicy_run_id"),
        ("fighthealthinsurance", "0220_merge_chat_policy_and_spend"),
    ]

    operations = []
