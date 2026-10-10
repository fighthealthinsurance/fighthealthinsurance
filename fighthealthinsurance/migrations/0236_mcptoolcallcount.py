from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0235_letterpromptmode_sectioned_and_thirds"),
    ]

    operations = [
        migrations.CreateModel(
            name="McpToolCallCount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("hour", models.DateTimeField()),
                ("tool", models.CharField(max_length=64)),
                ("outcome", models.CharField(max_length=16)),
                ("count", models.PositiveIntegerField(default=0)),
                ("last_call_at", models.DateTimeField()),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("hour", "tool", "outcome"),
                        name="mcp_tool_call_count_hour_tool_outcome",
                    )
                ],
            },
        ),
    ]
