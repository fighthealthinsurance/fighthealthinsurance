from django.db import migrations


class Migration(migrations.Migration):
    """#1148 and #1149 each added a 0227 on 0226; this joins the two leaves."""

    dependencies = [
        ("fighthealthinsurance", "0227_consentrecord"),
        ("fighthealthinsurance", "0227_denial_channel_and_spend_reservation"),
    ]

    operations = []
