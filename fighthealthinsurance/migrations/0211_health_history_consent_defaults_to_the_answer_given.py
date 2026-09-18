# The health history page never asked whether the history could go in the
# letter, and the drafting code never read the column, so it used the history
# either way. Now that the page asks and the drafting code honours the answer,
# a row still carrying the old False default would silently lose a history its
# owner typed on purpose. Those rows are given the answer their case was
# actually run under. Rows with no history are left alone: there is nothing to
# decide about, and the new column default covers them from here.

from django.db import migrations, models


def keep_using_a_history_somebody_already_typed(apps, schema_editor):
    Denial = apps.get_model("fighthealthinsurance", "Denial")
    Denial.objects.exclude(health_history__isnull=True).exclude(
        health_history=""
    ).update(include_provided_health_history_in_appeal=True)


def noop(apps, schema_editor):
    """Reversing leaves the answers alone; the column default goes back."""


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0210_merge_questions_stamp_and_reserve_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="denial",
            name="include_provided_health_history_in_appeal",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(keep_using_a_history_somebody_already_typed, noop),
    ]
