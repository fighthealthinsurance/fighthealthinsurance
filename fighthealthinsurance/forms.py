from django import forms

from fighthealthinsurance.models import DenialTypes, PlanType

class ShareAppealForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    appeal_text = forms.CharField(required=True)

class DenialForm(forms.Form):
    zip = forms.CharField(required=False)
    pii = forms.BooleanField(required=True)
    privacy = forms.BooleanField(required=True)
    store_raw_email = forms.BooleanField(required=False)
    denial_text = forms.CharField(required=True)
    email = forms.EmailField(required=True)


class DenialRefForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())

class PostInferedForm(forms.Form):
    # Send denial id and e-mail back that way people can't just change the ID
    # and get someone elses denial.
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    denial_type = forms.ModelMultipleChoiceField(queryset=DenialTypes.objects.all())
    denial_type_text = forms.CharField(
        required=False,
        label="Denial Type (text, you can type if the categories don't match the denial type)")
    plan_id = forms.CharField(required=False)
    claim_id = forms.CharField(required=False)
    insurance_company = forms.CharField(required=False)
#    plan_type = forms.ModelMultipleChoiceField(queryset=PlanType.objects.all())
#    plan_type_text = forms.CharField(required=False)
    denial_date = forms.DateField(required=False)
    your_state = forms.CharField(max_length=2, required=False)
    procedure = forms.CharField(
        max_length=200,
        label="What is the procedure/treatment you had denied?")
    diagnosis = forms.CharField(
        max_length=200,
        label="What is the diagnosis (if any) associated with the request." +
        "Does not need to be a diseas it can be any number of personal factors, "+
        "including things like \"high risk homosexual behaviour\" (yeah that's a real one)")


class InsuranceQuestions(forms.Form):
    """Insurance Questions"""

    in_network = forms.BooleanField(
        required=False,
        label="In-network visit")                            
    pre_service =  forms.BooleanField(
        required=False,
        label="Pre-service (claim before doctors visit/service)")
    urgent = forms.BooleanField(
        required=False,
        label="Urgent claim")

    def preface(self):
        return ["""Dear {insurance_company};

My name is $your_name_here and I am writing you regarding claim {claim_id}{denial_date_info}. I believe this claim has been incorrectly processed. I am requestting an internal appeal."""]

    def main(self):
        return []

    def footer(self):
        common = "Additionally, I request all documents involved in this claim, including but not limited to plan documents, qualifications of individuals involved (both in the decision and in creation of policies), any policies policies, procedures, and any related communications. If you are not the plan administrator, forward this request to the plan administrator (and tell me who is the plan administrator so I may follow up with them)."
        if "urgent" in self.cleaned_data and "pre_service" in self.cleaned_data and self.cleaned_data["urgent"] and self.cleaned_data["pre_service"]:
            return [
                common,
                "As an urgent pre-service claim you must respond within the timeline required for my medical situation (up to a maximum of four days). This also serves as notice of concurrent request of external review."]
        elif "pre_service" in self.cleaned_data and self.cleaned_data["pre_service"]:
            return [
                common,
                "As non-urgent pre-service claim I believe you ~30 days to respond."]
        else:
            return [
                common,
                "As a post-service claim I believe you have ~60 days to respond."]
        

class MedicalNeccessaryQuestions(InsuranceQuestions):
    """Questions to ask for medical necessiety."""
    medical_reason = forms.CharField(
        max_length=200,
        label="Why is this medically necessary (if you know)?",
        required=False)

    def main(self):
        return [self.cleaned_data["medical_reason"]]

class ExperimentalQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Insurance companies love to claim anything expensive is experimental." +
        " Some good ways to show something is not experimental: look for " +
        " documents like the \"standards of care\" or any medical journals (including the NIH or pubmed).")


class NotCoveredQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label= "Here the health plan has not said (necessarily) that it is not " +
        "medically necessary, just that they don't want to pay for it." +
        "This one is tricky, but some good avenues to argue for coverage include asking" +
        " for the plan documents and or demanding the policy under which " +
        " it is not covered under."
    )


class OutOfNetworkReimbursement(forms.Form):
    why_need_out_of_network = forms.CharField(
        max_length=300,
        label = "Explain why you need to go out of network. " +
        "Some common reasons: there is no " +
        "in-network provider, the in-network providers don't meet the standards of care " +
        " the in-network providers don't accept new patients or " +
        " the in-network providers don't perform the service needed.")

    def main(self):
        return [
            ("I believe you should cover this out of network service since " +
             self.cleaned_data["why_need_out_of_network"]) ]


class BalanceBillQuestions(forms.Form):
    """Questions to ask for surprise billing."""
    emergency = forms.BooleanField(required=False)
    match_eob = forms.BooleanField(required=False)

    def preface(self):
        return "As you are aware the no-surprises act ..."


class PriorAuthQuestions(InsuranceQuestions):
    emergency = forms.BooleanField(required=False)
    contact_insurance_before = forms.BooleanField(required=False)
    told_prior_auth_not_needed = forms.BooleanField(required=False)
    prior_auth_obtained = forms.BooleanField(required=False)
    prior_auth_id = forms.CharField(max_length=300)

    def main(self):
        r = []
        if "emergency" in self.cleaned_data:
            r += ("This service was an emergency so prior auth could not be " +
                  "obtained.")
        if "told_prior_auth_not_needed" in self.cleaned_data:
            r += ("I was told prior auth would not be needed.")
        if "prior_auth_id" in self.cleaned_data:
            r += ("Prior auth was obtained (id " + self.cleaned_data["prior_auth_id"] + ")")
        return r


class PreventiveCareQuestions(InsuranceQuestions):
    """Questions for preventive care."""
    medical_reason = forms.CharField(
        max_length=300,
        required=False,
        label="Any reasons why you are at an elevated risk requiring this screening.")
    trans_gender = forms.BooleanField(
        required=False,
        label="Are you trans*? Some preventive care is traditionally only covered for certain genders " +
        " and if your trans it's not uncommon for insurance to incorrectly deny necessary coverage.")

    def main(self):
        r = []
        if "trans_gender" in self.cleaned_data:
            r.append(
                "I am trans so it is important that preventive coverage for both genders be " +
                "covered.")
        if self.cleaned_data["medical_reason"]:
            r.append(self.cleaned_data["medical_reason"])
        return r


class ThirdPartyQuestions(forms.Form):
    """Questions to ask for 3rd party insurance questions."""
    is_known_3rd_party = forms.BooleanField(
        required=False,
        label="Was this medical claim the result of an accident that is covered by another insurance (e.g. auto accident where there is known auto insurance or workers comp)"
    )
    alternate_insurance_details = forms.CharField(
        max_length=300)

    def preface(self):
        if "is_known_3rd_party" in self.cleaned_data:
            return "As requested the 3rd party insurance is " + self.cleaned_data["alternate_insurance_details"]
        else:
            return super().preface()


class StepTherapy(MedicalNeccessaryQuestions):
    """Question to ask for step therapy."""
    medically_necessary = forms.CharField(
        required=False,
        label="Why the option from the insurnace company does not work (e.g. " +
        "you've tried the suggested medication, are allergic, not recommended, etc.)")


def magic_combined_form(forms_to_merge):
    if forms_to_merge == []:
        return []
    combined_form = forms.Form()
    for f in forms_to_merge:
        print(dir(f))
        for field_name, field in f.fields.items():
            if field_name not in combined_form.fields:
                combined_form.fields[field_name] = field
            elif field.initial is not None:
                combined_form.fields[field_name].initial += field.initial
    return combined_form
        
