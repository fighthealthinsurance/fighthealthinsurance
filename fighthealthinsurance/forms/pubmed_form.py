from django import forms


class PubMedPreloadForm(forms.Form):
    """Form for pre-loading PubMed searches for medications and conditions."""

    medications = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Enter medications, one per line or separated by commas",
            }
        ),
        help_text="Enter medications, one per line or separated by commas",
        label="Medications",
        required=False,
    )

    conditions = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Enter conditions/diagnoses, one per line or separated by commas",
            }
        ),
        help_text="Enter conditions or diagnoses, one per line or separated by commas",
        label="Conditions/Diagnoses",
        required=False,
    )

    def clean_medications(self):
        """
        Clean the medications field by splitting by newlines or commas and stripping whitespace.
        """
        medications = self.cleaned_data.get("medications", "")
        if not medications:
            return []

        # Split by either newlines or commas
        if "\n" in medications:
            meds_list = [med.strip() for med in medications.split("\n") if med.strip()]
        else:
            meds_list = [med.strip() for med in medications.split(",") if med.strip()]

        return meds_list

    def clean_conditions(self):
        """
        Clean the conditions field by splitting by newlines or commas and stripping whitespace.
        """
        conditions = self.cleaned_data.get("conditions", "")
        if not conditions:
            return []

        # Split by either newlines or commas
        if "\n" in conditions:
            cond_list = [
                cond.strip() for cond in conditions.split("\n") if cond.strip()
            ]
        else:
            cond_list = [cond.strip() for cond in conditions.split(",") if cond.strip()]

        return cond_list
