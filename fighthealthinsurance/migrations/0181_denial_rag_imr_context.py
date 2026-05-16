from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0180_insurancecompany_pa_requirement_list_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="rag_context",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="imr_context",
            field=models.TextField(blank=True, null=True),
        ),
    ]
