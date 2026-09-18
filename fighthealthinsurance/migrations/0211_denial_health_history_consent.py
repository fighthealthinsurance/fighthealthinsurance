# Somewhere to record the answer to "may the letter use my health history",
# which the health history page now asks. A new column rather than a reading
# of the old one: include_provided_health_history_in_appeal drives whether the
# raw history is attached to the fax, it is False for almost every row because
# that is its default, and a caller can also set it False on purpose through
# the API. Nothing can tell those two Falses apart, so nothing should try.
#
# NULL here means nobody was ever asked. It adds no default to existing rows
# and changes no value, so no case in flight moves.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0210_merge_questions_stamp_and_reserve_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="health_history_consent",
            field=models.BooleanField(default=None, null=True),
        ),
    ]
