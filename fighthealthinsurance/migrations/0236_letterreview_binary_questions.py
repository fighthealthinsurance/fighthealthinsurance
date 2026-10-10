from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0235_letterpromptmode_sectioned_and_thirds"),
    ]

    operations = [
        migrations.AddField(
            model_name="letterreviewpacket",
            name="form",
            field=models.CharField(
                choices=[
                    ("verdict", "One verdict per letter"),
                    ("binary", "Yes or no questions per letter"),
                ],
                default="verdict",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="letterreviewlabel",
            name="verdict",
            field=models.CharField(
                blank=True,
                choices=[
                    ("fabricates", "Fabricates"),
                    ("flag", "Flag"),
                    ("clean", "Clean"),
                ],
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="letterreviewlabel",
            name="invents_or_contradicts",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="letterreviewlabel",
            name="unsupported_history",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="letterreviewlabel",
            name="argues_against_reason",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="letterreviewlabel",
            name="specific_medical_necessity",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="letterreviewlabel",
            name="ready_to_send",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
