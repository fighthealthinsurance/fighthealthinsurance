from django.db import migrations, models


class Migration(migrations.Migration):
    # Also joins the two 0227 leaves (#1148 and #1149 landed side by side).
    dependencies = [
        ("fighthealthinsurance", "0227_consentrecord"),
        ("fighthealthinsurance", "0227_denial_channel_and_spend_reservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="assistanthandoff",
            name="bound",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
