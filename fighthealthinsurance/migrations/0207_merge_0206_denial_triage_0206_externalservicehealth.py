# Two branches each added migration 0206 off 0205 and merged the same night:
# the denial triage columns (0206_denial_triage) and the external service
# health table (0206_externalservicehealth). They touch different tables, so
# this merge node only joins the graph; it has no operations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0206_denial_triage"),
        ("fighthealthinsurance", "0206_externalservicehealth"),
    ]

    operations = []
