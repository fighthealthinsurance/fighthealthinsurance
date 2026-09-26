# Which drafts were on screen when a pick was made. The appeals page folds
# every draft past its visible limit behind a "Show more" button, so the
# drafts generated for a denial are not the drafts the user saw, and the
# model-usage dashboard's "presented" denominator counted the folded ones as
# candidates that lost. The browser now reports the visible ids with the pick
# and they are kept on the chosen row.
#
# NULL means nobody reported: picks from before this column, and the flows
# that cannot say (share-appeal, the professional API). Nothing about
# existing rows changes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0211_denial_health_history_consent"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedappeal",
            name="presented_ids",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
