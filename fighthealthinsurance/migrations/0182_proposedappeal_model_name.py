from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0181_alter_clinicaltrialquerydata_query"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedappeal",
            name="model_name",
            field=models.CharField(
                blank=True, db_index=True, max_length=200, null=True
            ),
        ),
        migrations.AddField(
            model_name="proposedappeal",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True, null=True),
        ),
    ]
