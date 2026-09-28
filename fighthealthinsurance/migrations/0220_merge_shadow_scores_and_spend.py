# Joins the chat shadow scores (0216) and the spend counters (0219), which
# were written on separate branches. No operations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0216_chatturn_shadow_scores"),
        ("fighthealthinsurance", "0219_spendcounter"),
    ]

    operations = []
