from django.db import migrations

# The check-in subjects in plain sentence case, keeping the brand prefix
# people search for. The 7- and 30-day ones no longer assume an appeal was
# sent: they go to people who took a letter into their AI chat, or never
# got one, as well. A row is changed only while it still holds the subject
# 0155/0176 seeded, so a subject staff edited in the admin stays.
SUBJECTS = {
    "followup_1day": (
        "Fight Health Insurance: Quick Check-In",
        "Fight Health Insurance: A quick check-in",
    ),
    "followup_7day": (
        "Fight Health Insurance: Confirm Your Appeal Was Received",
        "Fight Health Insurance: Checking in after a week",
    ),
    "followup_30day": (
        "Fight Health Insurance: Have You Heard Back on Your Appeal?",
        "Fight Health Insurance: Checking in after a month",
    ),
    "followup_90day": (
        "Fight Health Insurance: 90-Day Appeal Check-In",
        "Fight Health Insurance: One last check-in",
    ),
}


def plain_subjects(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")
    for name, (seeded, plain) in SUBJECTS.items():
        FollowUpType.objects.filter(name=name, subject=seeded).update(subject=plain)


def seeded_subjects(apps, schema_editor):
    FollowUpType = apps.get_model("fighthealthinsurance", "FollowUpType")
    for name, (seeded, plain) in SUBJECTS.items():
        FollowUpType.objects.filter(name=name, subject=plain).update(subject=seeded)


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0231_intakejourneyevent_outcome_not_built"),
    ]

    operations = [
        migrations.RunPython(plain_subjects, seeded_subjects),
    ]
