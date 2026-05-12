from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0177_alter_ongoingchat_denied_item_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="denial",
            name="use_external",
            field=models.BooleanField(default=True),
        ),
    ]
