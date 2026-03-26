import datetime

from django.db import migrations


def create_followup_types(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")

    FollowUpType.objects.get_or_create(
        name="followup_7day",
        defaults={
            "template_name": "followup_7day",
            "subject": "Fight Health Insurance: Confirm Your Appeal Was Received",
            "text": (
                "7-day check-in: Remind patient to confirm receipt of their "
                "appeal with the insurance company. Suggest faxing or mailing "
                "the appeal if they haven't sent it yet."
            ),
            "duration": datetime.timedelta(days=7),
        },
    )

    FollowUpType.objects.get_or_create(
        name="followup_30day",
        defaults={
            "template_name": "followup_30day",
            "subject": "Fight Health Insurance: Have You Heard Back on Your Appeal?",
            "text": (
                "30-day check-in: Ask if the patient has heard back from "
                "their insurance company. Encourage them to follow up "
                "directly with the insurer if no response."
            ),
            "duration": datetime.timedelta(days=30),
        },
    )

    FollowUpType.objects.get_or_create(
        name="followup_90day",
        defaults={
            "template_name": "followup_90day",
            "subject": "Fight Health Insurance: 90-Day Appeal Check-In",
            "text": (
                "90-day check-in: Final check-in on appeal status. Ask about "
                "the outcome and offer further assistance if needed."
            ),
            "duration": datetime.timedelta(days=90),
        },
    )


def remove_followup_types(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")
    FollowUpType.objects.filter(
        name__in=["followup_7day", "followup_30day", "followup_90day"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0153_followuptype_template_name"),
    ]

    operations = [
        migrations.RunPython(create_followup_types, remove_followup_types),
    ]
