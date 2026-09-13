"""Backfill ``your_state`` from ``state`` for denials corrected before this change.

Until now the review POST wrote the state the person typed to ``state`` only,
while the intake path wrote the zip code's guess to ``your_state`` -- and
``your_state`` is the column the appeal generator (generate_appeal.py:2358) and
the UCR geographic lookup (ucr_helper.py:349) read. So every denial where
someone already corrected their state is sitting in the old shape: ``state``
holds the correction, ``your_state`` holds the guess.

Shipping the write-both-columns fix without this leaves exactly the people the
bug already hit still getting an appeal addressed to the wrong state's
regulator, and the new confirmed-state check in
``DenialCreatorHelper.create_or_update_denial`` would make it permanent: it
sees a value in ``state``, stops inferring from the zip, and the stale guess in
``your_state`` never changes again. A read-side fallback cannot close this:
``your_state`` on those rows is not empty, it is wrong, so nothing distinguishes
it from a value worth keeping. The value has to move.

Only rows that disagree are touched. A row with no confirmed state keeps its
zip inference, which is still the best value available for it, and a row whose
two columns already agree is left alone.

The reverse is a no-op on purpose: the overwritten guess was derived from the
zip and is not worth restoring, and re-deriving it would need the zip database
as it was.
"""

from django.db import migrations
from django.db.models import F, Q


def copy_confirmed_state_to_your_state(apps, schema_editor):
    Denial = apps.get_model("fighthealthinsurance", "Denial")
    # ``~Q(your_state=F("state"))`` covers a NULL ``your_state`` as well as a
    # differing one: Django renders the negation as
    # ``NOT (your_state = state AND your_state IS NOT NULL AND state IS NOT
    # NULL)``, which is true for a row with no guess at all. The rows with a
    # guess that already matches are the ones left out. (The migration test's
    # no-guess row is what holds this.)
    (
        Denial.objects.filter(state__isnull=False)
        .exclude(state="")
        .filter(~Q(your_state=F("state")))
        .update(your_state=F("state"))
    )


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0208_dataremovaltotals"),
    ]

    operations = [
        migrations.RunPython(
            copy_confirmed_state_to_your_state, migrations.RunPython.noop
        ),
    ]
