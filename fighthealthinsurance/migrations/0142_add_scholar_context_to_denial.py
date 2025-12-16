# Generated migration for scholar_context field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fighthealthinsurance', '0141_add_google_scholar_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='denial',
            name='scholar_context',
            field=models.TextField(blank=True, null=True),
        ),
    ]
