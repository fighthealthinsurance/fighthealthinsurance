from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0148_deletetoken"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="deletetoken",
            name="email",
        ),
        migrations.AddField(
            model_name="deletetoken",
            name="hashed_email",
            field=models.CharField(default="", max_length=300, unique=True),
            preserve_default=False,
        ),
    ]
