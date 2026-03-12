from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0149_deletetoken_rename_email_to_hashed_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="pubmedarticlesummarized",
            name="authors",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pubmedarticlesummarized",
            name="journal",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pubmedarticlesummarized",
            name="year",
            field=models.TextField(blank=True, null=True),
        ),
    ]
