# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fighthealthinsurance', '0135_add_plan_documents_summary'),
    ]

    operations = [
        migrations.AddField(
            model_name='denial',
            name='microsite_slug',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
