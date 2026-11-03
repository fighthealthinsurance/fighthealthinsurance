from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0257_evaluatorloginthrottle"),
    ]

    operations = [
        migrations.AddField(
            model_name="chooservote",
            name="served_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="active_ms",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="expanded_candidate_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="review_signals",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="network_class",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="asn_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="country",
            field=models.CharField(blank=True, default="", max_length=2),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="user_agent",
            field=models.CharField(blank=True, default="", max_length=400),
        ),
    ]
