from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0153_policydocument_policydocumentanalysis"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydocument",
            name="plan_category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("employer_erisa", "Employer Plan (ERISA)"),
                    (
                        "employer_non_erisa",
                        "Employer Plan (Non-ERISA, e.g. government/church)",
                    ),
                    (
                        "aca_marketplace",
                        "ACA Marketplace (Healthcare.gov / State Exchange)",
                    ),
                    ("medicare_traditional", "Medicare (Traditional/Original)"),
                    ("medicare_advantage", "Medicare Advantage (Part C)"),
                    ("medicaid_chip", "Medicaid / CHIP"),
                    ("tricare", "TRICARE (Military)"),
                    ("va", "VA Health Care"),
                    ("individual_off_exchange", "Individual Plan (Off-Exchange)"),
                    ("short_term", "Short-Term Health Plan"),
                    ("unknown", "I'm Not Sure"),
                ],
                default="unknown",
                max_length=50,
            ),
        ),
    ]
