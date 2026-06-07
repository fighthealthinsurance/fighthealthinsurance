from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "fighthealthinsurance",
            "0182_modelhealthalertstate_proposedappeal_created_at_and_more",
        ),
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
