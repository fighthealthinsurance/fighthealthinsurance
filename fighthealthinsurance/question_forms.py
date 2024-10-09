import os

from django import forms

from bs4 import BeautifulSoup

import pymupdf
import re
from fighthealthinsurance.models import Denial, PlanDocuments
from fighthealthinsurance.form_utils import *
import urllib
import requests


class InsuranceQuestions(forms.Form):
    """Insurance Questions"""

    in_network = forms.BooleanField(required=False, label="In-network visit")
    pre_service = forms.BooleanField(
        required=False, label="Pre-service (claim before doctors visit/service)"
    )
    urgent = forms.BooleanField(required=False, label="Urgent claim")

    def medical_context(self):
        response = ""
        if "urgent" in self.cleaned_data and self.cleaned_data["urgent"]:
            response += "This is an urgent claim."
        if "pre_service" in self.cleaned_data and self.cleaned_data["pre_service"]:
            response += "This is a pre-service claim."
        if "in_network" in self.cleaned_data and self.cleaned_data["in_network"]:
            response += "This is an in-network claim."
        return response

    def preface(self):
        return [
            """Dear {insurance_company};

My name is $your_name_here and I am writing you regarding claim {claim_id}{denial_date_info}. I believe this claim has been incorrectly processed. I am requesting an internal appeal."""
        ]

    def main(self):
        return []

    def footer(self):
        common = ""
        if (
            "urgent" in self.cleaned_data
            and "pre_service" in self.cleaned_data
            and self.cleaned_data["urgent"]
            and self.cleaned_data["pre_service"]
        ):
            return [
                common,
                "As an urgent pre-service claim you must respond within the timeline required for my medical situation (up to a maximum of four days). This also serves as notice of concurrent request of external review.",
            ]
        elif "pre_service" in self.cleaned_data and self.cleaned_data["pre_service"]:
            return [
                common,
                "As non-urgent pre-service claim I believe you ~30 days to respond.",
            ]
        else:
            return [
                common,
                # "As a post-service claim I believe you have ~60 days to respond.",
            ]


class MedicalNeccessaryQuestions(InsuranceQuestions):
    """Questions to ask for medical necessiety."""

    medical_reason = forms.CharField(
        max_length=200,
        label="Why is this medically necessary (if you know)?",
        required=False,
    )
    age = forms.CharField(required=False, label="What is your age?")

    def medical_context(self):
        response = ""
        r = None
        a = None
        if "medical_reason" in self.cleaned_data:
            r = self.cleaned_data["medical_reason"]
            if r is not None and r != "":
                response += f"The medical reason may be {r}."
        if "age" in self.cleaned_data:
            a = self.cleaned_data["age"]
            if a is not None and a != "":
                response += f"The patient age is {a}."
        return response

    def generate_reason(self):
        """Return the reason OR the special tag {medical_reason} where we will ask the LLM why it might be medically necessary."""
        if self.cleaned_data["medical_reason"] == "":
            return "{medical_reason}"
        else:
            return [self.cleaned_data["medical_reason"]]

    def main(self):
        return [
            "I understand that my claim was denied as not medically necessary, however I believe it is medically necessary for {medical_reason}."
        ]


class ExperimentalQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Insurance companies love to claim anything expensive is experimental."
        + " Some good ways to show something is not experimental: look for "
        + ' documents like the "standards of care" or any medical journals (including the NIH or pubmed).',
    )


class NotCoveredQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Here the health plan has not said (necessarily) that it is not "
        + "medically necessary, just that they don't want to pay for it."
        + "This one is tricky, but some good avenues to argue for coverage include asking"
        + " for the plan documents and or demanding the policy under which "
        + " it is not covered under.",
    )


class NotCoveredByQuestions(NotCoveredQuestions):
    """Alt name to match the db entry (my bad)"""


class OutOfNetworkReimbursement(forms.Form):
    why_need_out_of_network = forms.CharField(
        max_length=300,
        label="Explain why you need to go out of network. "
        + "Some common reasons: there is no "
        + "in-network provider, the in-network providers don't meet the standards of care "
        + " the in-network providers don't accept new patients or "
        + " the in-network providers don't perform the service needed.",
    )

    def medical_context(self):
        r = self.cleaned_data["why_need_out_of_network"]
        if r is not None and r != "":
            return (
                "One reason why this out of network claim should be accepted could be "
                + r
            )
        else:
            return ""

    def main(self):
        return [
            (
                "I believe you should cover this out of network service since "
                + self.cleaned_data["why_need_out_of_network"]
            )
        ]


class BalanceBillQuestions(forms.Form):
    """Questions to ask for surprise billing."""

    emergency = forms.BooleanField(required=False)
    match_eob = forms.BooleanField(required=False)

    def preface(self):
        if "emergency" in self.cleaned_data:
            return "As you are aware the no-surprises act prohibits balance billing and similar practices in the majority of emergency cases (see https://www.cms.gov/newsroom/fact-sheets/no-surprises-understand-your-rights-against-surprise-medical-bills)"
        else:
            return ""


class PriorAuthQuestions(InsuranceQuestions):
    emergency = forms.BooleanField(required=False)
    contact_insurance_before = forms.BooleanField(required=False)
    told_prior_auth_not_needed = forms.BooleanField(required=False)
    prior_auth_obtained = forms.BooleanField(required=False)
    prior_auth_id = forms.CharField(max_length=300, required=False)

    def main(self):
        r = []
        if "emergency" in self.cleaned_data:
            r += (
                "This service was an emergency so prior auth could not be "
                + "obtained."
            )
        if "told_prior_auth_not_needed" in self.cleaned_data:
            r += "I was told prior auth would not be needed."
        if "prior_auth_id" in self.cleaned_data:
            r += (
                "Prior auth was obtained (id "
                + self.cleaned_data["prior_auth_id"]
                + ")"
            )
        return r


class GenderAffirmingCareQuestions(InsuranceQuestions):
    """Generic questions for gender affirming care."""

    def plan_context(self, denial: Denial):
        response = ""
        if denial.state == "CA":
            response += "As covered in https://calmatters.org/health/2024/08/gender-affirming-care-denials/ CA health plans received the largest penality ever for gender-affirming care denials. The plan must follow https://www.insurance.ca.gov/01-consumers/110-health/60-resources/upload/CDI-Gender-Nondiscrimination-Regulations.pdf and insurers may not discriminate against gender affirming care. If the plan fails to approve this claim the patient intents to appeal all the way to the relevant regulator."
        else:
            response += "As covered in https://calmatters.org/health/2024/08/gender-affirming-care-denials/ CA health plans received the largest penality ever for gender-affirming care denials and some states have similar non-discriminiation requirements."

        if denial.employer_name is not None:
            if self.employer_hrc_lookup(denial.employer_name):
                response += "The employer has stated to the human rights collation (HRC) that it will cover transgender health care. Should the plan deny this claim we intend to follow up with both HR and the HRC."

        wpath_version = self.wpath_version(denial)
        if wpath_version is not None:
            response += f"As stated in the plan documents the plan must follow the WPATH{wpath_version} standards of care."
        return response

    def wpath_version(self, denial):
        return self._wpath_version(
            map(
                lambda doc: doc.plan_document.path,
                PlanDocuments.objects.filter(denial=denial),
            )
        )

    def _wpath_version(self, plan_paths):
        # Do we have plan documents and do they reference WPATH?
        wpath_version = None
        for path in plan_paths:
            contents = None
            if ".pdf" in path:
                try:
                    doc = pymupdf.open(path)
                    contents = ""
                    for page in doc:
                        contents += page.get_text()
                except:
                    print(f"Error reading {path}")
            if contents is None:
                with open(path, "r") as file:
                    contents = file.read()
            soc_version_re = re.compile(
                "WPATH.*?Standards of.*?Care.*?Version.*?(\\d+).*",
                re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
            m = re.search(soc_version_re, contents)
            if m is not None:
                wpath_version = m.group(1)
                # Exit as soon as we find any WPATH SOC version
                return wpath_version
            if "WPATH" in contents:
                if wpath_version is None:
                    # We don't know the version but it is refed
                    wpath_version = ""

            return wpath_version

    def employer_hrc_lookup(self, employer):
        # Check and see if the employer is listed in the HRC equality index
        try:
            safe_employer_name = urllib.parse.quote_plus(employer)
            employer_search_string = (
                f"https://www.hrc.org/resources/employers/search?q={safe_employer_name}"
            )
            r = requests.get(employer_search_string)
            if "No results found for" not in r.text:
                soup = BeautifulSoup(r.text, "html.parser")
                link_re = re.compile("https://www.hrc.org/resources/buyers-guide/.*")
                links = soup.find_all("a", {"href": link_re})
                text = ""
                for bs_link in links:
                    if employer.lower() in bs_link.getText().lower():
                        r = requests.get(bs_link["href"])
                        text = r.text
                        break
                # Very hacky check to see if the employer should cover by HRC
                if "Equality 100 Award" in text:
                    return True
                elif "45/50" in text or "50/50" in text:
                    return True
        except Exception as e:
            print(f"Error {e} getting employer HRC score")
            return False
        return False


class GenderAffirmingCareBreastAugmentationQuestions(GenderAffirmingCareQuestions):

    def plan_context(self, denial: Denial):
        if self.wpath_version(denial) == "7":
            return """The plan references version 7 of the WPATH SOC. As covered on P59 of the WPATH 7 SOC the only requirements for breast augmentation is 1. Persistent, well-documented gender dysphoria;
2. Capacity to make a fully informed decision and to consent for treatment;
3. Age of majority in a given country (if younger, follow the SOC for children and adolescents);
4. If significant medical or mental health concerns are present, they must be reasonably well
controlled."""


class PreventiveCareQuestions(InsuranceQuestions):
    """Questions for preventive care."""

    medical_reason = forms.CharField(
        max_length=300,
        required=False,
        label="Any reasons why you are at an elevated risk requiring this screening.",
    )
    trans_gender = forms.BooleanField(
        required=False,
        label="Are you trans*? Some preventive care is traditionally only covered for certain genders "
        + " and if your trans it's not uncommon for insurance to incorrectly deny necessary coverage.",
    )

    def medical_context(self):
        response = "This procedure may be preventive, make sure to include a link to https://www.healthcare.gov/coverage/preventive-care-benefits/ if that's the case."
        if "trans_gender" in self.cleaned_data and self.cleaned_data["trans_gender"]:
            response += "The patient is transgender."
        if (
            "medical_reason" in self.cleaned_data
            and self.cleaned_data["medical_reason"]
        ):
            response += (
                "The patient may be at increased risk due to "
                + self.cleaned_data["medical_reason"]
            )
        return response

    def main(self):
        r = []
        if "trans_gender" in self.cleaned_data and self.cleaned_data["trans_gender"]:
            r.append(
                "I am trans so it is important that preventive coverage for both genders be "
                + "covered."
            )
        if self.cleaned_data["medical_reason"]:
            r.append(self.cleaned_data["medical_reason"])
        return r


class ThirdPartyQuestions(forms.Form):
    """Questions to ask for 3rd party insurance questions."""

    is_known_3rd_party = forms.BooleanField(
        required=False,
        label="Was this medical claim the result of an accident that is covered by another insurance (e.g. auto accident where there is known auto insurance or workers comp)",
    )
    alternate_insurance_details = forms.CharField(max_length=300)

    def preface(self):
        if "is_known_3rd_party" in self.cleaned_data:
            return (
                "As requested the 3rd party insurance is "
                + self.cleaned_data["alternate_insurance_details"]
            )
        else:
            return super().preface()


class StepTherapy(MedicalNeccessaryQuestions):
    """Question to ask for step therapy."""

    medically_necessary = forms.CharField(
        required=False,
        label="Why the option from the insurnace company does not work (e.g. "
        + "you've tried the suggested medication, are allergic, not recommended, etc.)",
    )
