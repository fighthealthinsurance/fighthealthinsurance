# Joins the two migrations numbered 0260, which landed from two pull
# requests at once. Changes nothing in the database.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0260_policydocument_potential_spam"),
        ("fighthealthinsurance", "0260_uploadcount"),
    ]

    operations = []
