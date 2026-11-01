from django.db import migrations, models

# The denial types the review page shows up front (see
# forms.CoreFirstCheckboxList); kept in step with fixtures/initial.yaml.
CORE_DENIAL_TYPES = (
    "Medically Necessary",
    "Prior Authorization Requred",
    "Experimental Medical Treatment",
    "Not Covered By Plan",
    "In-Network Treated as Out of Network",
    "Denied Out-Of-Network Provider",
    "Insufficient Medical Information",
    "STEP Therapy -- have to try cheaper options first",
    "Other",
    "Not On Formulary",
)


def mark_core(apps, schema_editor):
    DenialTypes = apps.get_model("fighthealthinsurance", "DenialTypes")
    DenialTypes.objects.filter(name__in=CORE_DENIAL_TYPES).update(core=True)


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0248_choosertask_complexity"),
    ]

    operations = [
        migrations.AddField(
            model_name="denialtypes",
            name="core",
            field=models.BooleanField(
                default=False,
                help_text="Always show this type on the review page's list.",
            ),
        ),
        migrations.RunPython(mark_core, migrations.RunPython.noop),
    ]
