# Joins 0260_public_plan_documents and 0262_plan_sponsor, which landed
# from two pull requests at once, each on 0259. Changes nothing in the
# database.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0260_public_plan_documents"),
        ("fighthealthinsurance", "0262_plan_sponsor"),
    ]

    operations = []
