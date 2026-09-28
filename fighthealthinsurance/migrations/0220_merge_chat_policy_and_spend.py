# Joins the chat routing policy (0215) and the provider spend counters
# (0219), which were written on separate branches. No operations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0215_chatroutingpolicy"),
        ("fighthealthinsurance", "0219_spendcounter"),
    ]

    operations = []
