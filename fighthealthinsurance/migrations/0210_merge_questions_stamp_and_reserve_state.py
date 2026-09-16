# Two branches each added a 0209: the question-set stamp
# (0209_denial_generated_questions_for) and the reserve state stamp
# (0209_proposedappeal_built_for_state). Neither touches the other's table,
# so this merge node only joins the graph.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0209_denial_generated_questions_for"),
        ("fighthealthinsurance", "0209_proposedappeal_built_for_state"),
    ]

    operations = []
