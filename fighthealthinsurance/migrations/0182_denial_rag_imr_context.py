from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0181_alter_clinicaltrialquerydata_query"),
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
