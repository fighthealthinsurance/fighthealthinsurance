# Joins the two 0236 migrations, which merged side by side. A merge rather
# than a renumber, so it is safe wherever either one has already run.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0236_letterreview_binary_questions"),
        ("fighthealthinsurance", "0236_mcptoolcallcount"),
    ]

    operations = []
