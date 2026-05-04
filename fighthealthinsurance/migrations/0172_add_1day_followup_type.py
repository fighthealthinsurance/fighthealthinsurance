import datetime

from django.db import migrations


def create_1day_followup_type(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")

    FollowUpType.objects.get_or_create(
        name="followup_1day",
        defaults={
            "template_name": "followup_1day",
            "subject": "Fight Health Insurance: Quick Check-In",
            "text": (
                "1-day check-in: quick status check after appeal generation "
                "to ask whether the draft was usable and gather feedback."
            ),
            "duration": datetime.timedelta(days=1),
        },
    )


def remove_1day_followup_type(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")
    FollowUpType.objects.filter(name="followup_1day").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0171_alter_denial_follow_up_semi_sekret_and_more"),
    ]

    operations = [
        migrations.RunPython(create_1day_followup_type, remove_1day_followup_type),
    ]
