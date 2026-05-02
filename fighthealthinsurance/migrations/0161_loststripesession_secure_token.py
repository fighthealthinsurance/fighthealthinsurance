from django.db import migrations, models

from fighthealthinsurance.utils import sekret_gen


def populate_secure_tokens(apps, schema_editor):
    """Backfill a unique secure_token for any pre-existing rows."""
    LostStripeSession = apps.get_model("fighthealthinsurance", "LostStripeSession")
    for session in LostStripeSession.objects.filter(secure_token=""):
        session.secure_token = sekret_gen()
        session.save(update_fields=["secure_token"])


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0160_cmscoveragecache"),
    ]

    operations = [
        migrations.AddField(
            model_name="loststripesession",
            name="secure_token",
            field=models.CharField(db_index=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(populate_secure_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="loststripesession",
            name="secure_token",
            field=models.CharField(
                default=sekret_gen, max_length=64, unique=True, db_index=True
            ),
        ),
    ]
