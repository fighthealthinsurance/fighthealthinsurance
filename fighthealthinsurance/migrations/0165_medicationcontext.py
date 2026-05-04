# Add MedicationContext: curated drug-class knowledge that the appeal
# generator injects into the LLM prompt when a denial mentions a drug
# in the class.

import regex_field.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0164_ucr_models_and_denial_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="MedicationContext",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "drug_class",
                    models.CharField(
                        help_text=(
                            "Therapeutic class (e.g. 'GLP-1 receptor agonist', "
                            "'anti-CGRP monoclonal antibody', 'TNF inhibitor')."
                        ),
                        max_length=200,
                    ),
                ),
                (
                    "brand_names",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Newline-separated brand names in this class (display only)."
                        ),
                    ),
                ),
                (
                    "generic_names",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Newline-separated generic / INN names in this class (display only)."
                        ),
                    ),
                ),
                (
                    "regex",
                    regex_field.fields.RegexField(
                        help_text=(
                            "Pattern that matches any brand or generic in this class "
                            "against denial text or stated diagnosis."
                        ),
                        max_length=800,
                    ),
                ),
                (
                    "fda_indications",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Brief summary of FDA-approved indications. Used to "
                            "rebut 'experimental/investigational' denials."
                        ),
                    ),
                ),
                (
                    "common_denial_reasons",
                    models.TextField(
                        blank=True,
                        help_text="Common reasons insurers cite when denying this class.",
                    ),
                ),
                (
                    "appeal_context",
                    models.TextField(
                        help_text=(
                            "Curated argumentation injected into the appeal generation "
                            "prompt. Should include guideline citations, step-therapy "
                            "counter-arguments, and any class-specific clinical context."
                        )
                    ),
                ),
                (
                    "pubmed_ids",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Newline-separated PubMed IDs of foundational evidence for "
                            "this class. Auto-fetched into the citation pipeline."
                        ),
                    ),
                ),
                (
                    "active",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "Disable to temporarily exclude this context from prompts."
                        ),
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Medication Contexts",
                "ordering": ["drug_class"],
            },
        ),
    ]
