import re
import urllib

from django import forms

import requests
from bs4 import BeautifulSoup
from loguru import logger

from fighthealthinsurance.models import Denial, PlanDocuments
from fighthealthinsurance.utils import extract_file_text


class InsuranceQuestions(forms.Form):
    """Insurance Questions"""

    in_network = forms.BooleanField(required=False, label="In-network visit")
    pre_service = forms.BooleanField(
        required=False, label="Pre-service (claim before doctors visit/service)"
    )
    urgent = forms.BooleanField(required=False, label="Urgent claim")

    def __init__(self, *args, prof_pov: bool = False, **kwargs):
        self.prof_pov = prof_pov
        logger.debug(f"InsuranceQuestions initialized with prof_pov={prof_pov}")
        super().__init__(*args, **kwargs)
        if self.prof_pov and "in_network" in self.fields:
            # Remove in_network field for professional view since it is asked in an earlier form
            self.fields.pop("in_network")

    def medical_context(self):
        response = ""
        if "urgent" in self.cleaned_data and self.cleaned_data["urgent"]:
            response += "This is an urgent claim."
        if "pre_service" in self.cleaned_data and self.cleaned_data["pre_service"]:
            response += "This is a pre-service claim."
        if (
            not self.prof_pov
            and "in_network" in self.cleaned_data
            and self.cleaned_data["in_network"]
        ):
            response += "This is an in-network claim."
        return response

    def preface(self):
        if self.prof_pov:
            return [
                """Dear {insurance_company},\n\nAs a medical professional, I have reviewed the details of claim {claim_id}{denial_date_info} and, in my professional opinion, I believe it has been incorrectly processed. I am requesting an internal appeal on behalf of my patient."""
            ]
        return [
            """Dear {insurance_company};\n\nMy name is $your_name_here and I am writing you regarding claim {claim_id}{denial_date_info}. I believe this claim has been incorrectly processed. I am requesting an internal appeal."""
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
            if self.prof_pov:
                return [
                    common,
                    (
                        "As an urgent pre-service claim you must respond within the "
                        "timeline required for my medical situation (up to a maximum "
                        "of four days). This also serves as notice of concurrent "
                        "request of external review."
                    ),
                ]
            return [
                common,
                (
                    "As an urgent pre-service claim you must respond within the "
                    "timeline required for my medical situation (up to a maximum "
                    "of four days). This also serves as notice of concurrent "
                    "request of external review."
                ),
            ]
        elif "pre_service" in self.cleaned_data and self.cleaned_data["pre_service"]:
            return [
                common,
                "As this is a non-urgent pre-service claim, I understand you have approximately 30 days to respond.",
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
    age = forms.CharField(
        required=False, label="Age of the person with the denied claim?"
    )

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
            "The claim was denied as not medically necessary; however, it is medically necessary for {medical_reason}."
        ]


class ExperimentalQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Why do you believe this is not experimental?",
        help_text=(
            "Insurance companies tend to claim anything expensive is experimental. "
            "Good ways to show something is not experimental: look for documents like the 'standards of care' or medical journals (including the NIH or pubmed)."
        ),
        required=False,
    )


class NotCoveredQuestions(MedicalNeccessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Why should this be covered?",
        help_text="The plan may not say it's not medically necessary, just that they don't want to pay. Good arguments: request plan documents, demand the policy under which it is not covered, or explain why it should be covered.",
        required=False,
    )


class NotCoveredByQuestions(NotCoveredQuestions):
    """Alt name to match the db entry (my bad)"""


class OutOfNetworkReimbursement(forms.Form):
    why_need_out_of_network = forms.CharField(
        max_length=300,
        label="Why do you need to go out of network?",
        help_text="E.g., no in-network provider, in-network providers don't meet standards of care, don't accept new patients, or don't perform the needed service.",
    )

    def __init__(self, *args, prof_pov: bool = False, **kwargs):
        self.prof_pov = prof_pov
        super().__init__(*args, **kwargs)

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
        if self.prof_pov:
            return [
                "Based on my professional assessment, out-of-network services are medically necessary in this case because "
                + self.cleaned_data["why_need_out_of_network"]
            ]
        return [
            "I believe you should cover this out of network service since "
            + self.cleaned_data["why_need_out_of_network"]
        ]


class BalanceBillQuestions(forms.Form):
    """Questions to ask for surprise billing."""

    emergency = forms.BooleanField(required=False)
    match_eob = forms.BooleanField(required=False)

    def __init__(self, *args, prof_pov: bool = False, **kwargs):
        self.prof_pov = prof_pov
        super().__init__(*args, **kwargs)

    def preface(self):
        if "emergency" in self.cleaned_data:
            if self.prof_pov:
                return [
                    "The No Surprises Act prohibits balance billing and similar practices in the majority of emergency cases (see https://www.cms.gov/newsroom/fact-sheets/no-surprises-understand-your-rights-against-surprise-medical-bills). Please ensure full compliance with these federal requirements in the processing of claim {claim_id}{denial_date_info}."
                ]
            return [
                "As you are aware the no-surprises act prohibits balance billing and similar practices in the majority of emergency cases (see https://www.cms.gov/newsroom/fact-sheets/no-surprises-understand-your-rights-against-surprise-medical-bills)"
            ]
        else:
            return [""]


# This is related to why weren't you able to get a prior auth.
class PriorAuthQuestions(InsuranceQuestions):
    emergency = forms.BooleanField(required=False)
    contact_insurance_before = forms.BooleanField(required=False)
    told_prior_auth_not_needed = forms.BooleanField(required=False)
    prior_auth_obtained = forms.BooleanField(required=False)
    prior_auth_id = forms.CharField(max_length=300, required=False)

    def main(self):
        r: list[str] = []
        if "emergency" in self.cleaned_data:
            r.append(
                "This service was an emergency so prior auth could not be "
                + "obtained."
            )
        if "told_prior_auth_not_needed" in self.cleaned_data:
            r.append("It was communicated that prior authorization was not necessary.")
        if "prior_auth_id" in self.cleaned_data:
            r.append(
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
            response += (
                "As covered in https://calmatters.org/health/2024/08"
                "/gender-affirming-care-denials/ CA health plans received the "
                "largest penality ever for gender-affirming care denials. "
                "The plan must follow https://www.insurance.ca.gov/01-consumers/110-health/"
                "60-resources/upload/CDI-Gender-Nondiscrimination-Regulations.pdf "
                "and insurers may not discriminate against gender affirming "
                "care. If the plan fails to approve this claim the patient "
                "intents to appeal all the way to the relevant regulator."
            )
        else:
            response += (
                "As covered in https://calmatters.org/health/2024/08"
                "/gender-affirming-care-denials/ CA health plans received "
                "the largest penality ever for gender-affirming care denials "
                "and some states have similar non-discriminiation requirements."
            )

        if denial.employer_name is not None:
            if self.employer_hrc_lookup(denial.employer_name):
                response += (
                    "The employer has stated to the human rights collation "
                    "(HRC) that it will cover transgender health care. Should "
                    "the plan deny this claim we intend to follow up with "
                    "both HR and the HRC."
                )

        wpath_version = self.wpath_version(denial)
        if wpath_version is not None:
            response += (
                "As stated in the plan documents the plan must follow the "
                f"WPATH{wpath_version} standards of care."
            )
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
            contents = extract_file_text(path)
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
            logger.debug(f"Error {e} getting employer HRC score")
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
        label="Reason for elevated risk requiring this screening.",
        help_text="Briefly describe any factors that may increase the need for this preventive screening (e.g., family history, prior conditions, or other risk factors).",
    )
    trans_gender = forms.BooleanField(
        required=False,
        label="Is the patient transgender?",
        help_text="Some preventive care is only covered for certain genders. If the patient is transgender, insurance may incorrectly deny necessary coverage. Check this box if it applies.",
    )

    def __init__(self, *args, prof_pov: bool = False, **kwargs):
        self.prof_pov = prof_pov
        super().__init__(*args, **kwargs)

    def medical_context(self):
        response = (
            "This procedure may be preventive, make sure to include a link to "
            "https://www.healthcare.gov/coverage/preventive-care-benefits/ if "
            "that's the case."
        )
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
            if self.prof_pov:
                r.append(
                    "The patient is transgender, so it is important that preventive coverage for all relevant genders is provided."
                )
            else:
                r.append(
                    "I am trans so it is important that preventive coverage for both genders be covered."
                )
        if self.cleaned_data["medical_reason"]:
            r.append(self.cleaned_data["medical_reason"])
        return r


class ThirdPartyQuestions(InsuranceQuestions):
    """Questions to ask for 3rd party insurance questions."""

    alternate_insurance_details = forms.CharField(
        max_length=300,
        required=False,
        label="Any details regarding secondary or other insurance if available?",
    )
    is_known_3rd_party = forms.BooleanField(
        required=False,
        label="Was this claim due to an accident covered by other insurance?",
        help_text="E.g., auto accident with known auto insurance, or workers comp. Check if another insurer should be responsible.",
    )

    def __init__(self, *args, prof_pov=False, **kwargs):
        self.prof_pov = prof_pov
        super().__init__(*args, **kwargs)

    def preface(self):
        if "is_known_3rd_party" in self.cleaned_data:
            if self.prof_pov:
                return [
                    "As requested, I am providing details regarding third-party insurance coverage for this claim: "
                    + self.cleaned_data["alternate_insurance_details"]
                    + ". Please ensure that all relevant coordination of benefits is considered in the review of this claim."
                ]
            return [
                "As requested, the third-party insurance is "
                + self.cleaned_data["alternate_insurance_details"]
            ]
        return super().preface()


class StepTherapy(MedicalNeccessaryQuestions):
    """Question to ask for step therapy."""

    medically_necessary = forms.CharField(
        required=False,
        label="Why doesn't the insurance supported care option work?",
        help_text="E.g., you've tried the suggested medication, are allergic, it is not recommended, or it was ineffective. Briefly explain why the insurer's alternative is not appropriate in your case.",
    )


class FormularyChangeQuestions(InsuranceQuestions):
    """Questions for formulary changes and continuity of care appeals."""

    currently_taking = forms.BooleanField(
        required=False,
        label="Are you currently taking this medication?",
        help_text="Check if you've been taking this medication and it's being removed from formulary or moved to a higher tier.",
    )
    how_long_taking = forms.CharField(
        max_length=100,
        required=False,
        label="How long have you been taking this medication?",
        help_text="E.g., '6 months', '2 years', 'since 2021'. This helps establish continuity of care.",
    )
    medication_working = forms.BooleanField(
        required=False,
        label="Is the medication working well for you?",
        help_text="Check if your condition is stable and well-controlled on this medication.",
    )
    tried_alternatives = forms.BooleanField(
        required=False,
        label="Have you tried the alternative medication(s) the insurer suggests?",
        help_text="Check if you've previously tried and failed on the preferred/alternative medications.",
    )
    alternative_problems = forms.CharField(
        max_length=300,
        required=False,
        label="What problems did you have with alternatives (if any)?",
        help_text="E.g., 'Side effects were intolerable', 'Did not control my symptoms', 'I'm allergic to the alternative'.",
    )
    mid_year_change = forms.BooleanField(
        required=False,
        label="Is this a mid-year formulary change?",
        help_text="Check if the formulary changed during your plan year (not at annual renewal).",
    )

    def medical_context(self):
        """Return context about continuity of care to inform the LLM."""
        response = ""
        if self.cleaned_data.get("currently_taking"):
            response += "The patient is currently taking this medication. "
            if self.cleaned_data.get("how_long_taking"):
                response += f"They have been taking it for {self.cleaned_data['how_long_taking']}. "
        if self.cleaned_data.get("medication_working"):
            response += "The medication is working well and their condition is stable. "
        if self.cleaned_data.get("tried_alternatives"):
            response += "The patient has tried alternative medications. "
            if self.cleaned_data.get("alternative_problems"):
                response += f"Problems with alternatives: {self.cleaned_data['alternative_problems']}. "
        if self.cleaned_data.get("mid_year_change"):
            response += "This is a mid-year formulary change, which may trigger additional patient protections. "
        return response

    def main(self):
        """Return main appeal text based on answers."""
        r = []

        currently_taking = bool(self.cleaned_data.get("currently_taking"))

        # Only add continuity of care arguments if patient is currently taking the medication
        if currently_taking:
            r.append(
                "I am requesting a medical exception. I believe this is required for continuity of care. "
            )
        if currently_taking and self.cleaned_data.get("how_long_taking"):
            r.append(
                f"I have been taking this medication for {self.cleaned_data['how_long_taking']} "
                "and my condition is well-controlled. Switching medications would disrupt my care."
            )

        if currently_taking and self.cleaned_data.get("medication_working"):
            r.append(
                "My current medication is working effectively. Medical literature shows that "
                "medication switching can lead to adverse outcomes, treatment failures, and "
                "increased healthcare costs."
            )

        if self.cleaned_data.get("tried_alternatives"):
            base = "I have previously tried the alternative medication(s) you suggest"
            if self.cleaned_data.get("alternative_problems"):
                r.append(
                    f"{base}, but experienced problems: {self.cleaned_data['alternative_problems']}."
                )
            else:
                r.append(f"{base}, which did not work for me.")

        if self.cleaned_data.get("mid_year_change"):
            r.append(
                "This formulary change occurred during my plan year. Most often "
                "health plans must provide a reasonable transition process when drugs are removed "
                "from formulary. Many states also have non-medical switching laws that prohibit "
                "forcing patients to switch stable medications mid-year."
            )

        return r


class ColonoscopyQuestions(InsuranceQuestions):
    """Questions for colonoscopy denial appeals."""

    screening_or_diagnostic = forms.ChoiceField(
        choices=[
            ("", "---"),
            ("screening", "Screening (preventive, no symptoms)"),
            ("diagnostic", "Diagnostic (symptoms, follow-up, or surveillance)"),
        ],
        required=False,
        label="Was this a screening or diagnostic colonoscopy?",
        help_text="Screening colonoscopies are preventive and often must be covered at no cost under the ACA. Diagnostic colonoscopies are performed due to symptoms or prior findings.",
    )
    age = forms.CharField(
        max_length=10,
        required=False,
        label="Patient's age?",
        help_text="USPSTF recommends routine screening starting at age 45.",
    )
    polyp_removal = forms.BooleanField(
        required=False,
        label="Were polyps removed during the procedure?",
        help_text="Under the ACA and recent guidance, polyp removal during a screening colonoscopy should still be covered as preventive.",
    )
    family_history = forms.BooleanField(
        required=False,
        label="Does the patient have a family history of colorectal cancer?",
        help_text="Family history may warrant earlier or more frequent screening.",
    )

    def medical_context(self):
        response = super().medical_context()
        sd = self.cleaned_data.get("screening_or_diagnostic", "")
        if sd == "screening":
            response += "This was a screening (preventive) colonoscopy. "
        elif sd == "diagnostic":
            response += "This was a diagnostic colonoscopy. "
        if self.cleaned_data.get("age"):
            response += f"Patient age: {self.cleaned_data['age']}. "
        if self.cleaned_data.get("polyp_removal"):
            response += "Polyps were removed during the procedure. "
        if self.cleaned_data.get("family_history"):
            response += "Patient has a family history of colorectal cancer. "
        return response

    def main(self):
        r = []
        sd = self.cleaned_data.get("screening_or_diagnostic", "")
        if sd == "screening":
            r.append(
                "This colonoscopy was a preventive screening. Under the ACA, "
                "screening colonoscopies recommended by the USPSTF (grade A or B) "
                "must be covered without cost-sharing. The USPSTF recommends colorectal "
                "cancer screening for adults aged 45 to 75."
            )
        if self.cleaned_data.get("polyp_removal"):
            r.append(
                "Polyps were removed during this procedure. Under the Consolidated "
                "Appropriations Act of 2023 and updated ACA guidance, polyp removal "
                "during a screening colonoscopy does not change the preventive "
                "classification of the procedure. The colonoscopy must still be "
                "covered as preventive with no cost-sharing."
            )
        if self.cleaned_data.get("family_history"):
            r.append(
                "The patient has a family history of colorectal cancer, which "
                "increases their risk and supports the medical necessity of this "
                "procedure per ACS and NCCN guidelines."
            )
        return r


class WeightLossMedicationQuestions(InsuranceQuestions):
    """Questions for weight loss / anti-obesity medication denials (Zepbound, Ozempic, Mounjaro, Wegovy)."""

    bmi = forms.CharField(
        max_length=10,
        required=False,
        label="Patient's BMI (if known)?",
        help_text="FDA-approved anti-obesity medications typically require BMI >= 30, or >= 27 with a weight-related comorbidity.",
    )
    comorbidities = forms.CharField(
        max_length=300,
        required=False,
        label="Weight-related comorbidities (if any)?",
        help_text="E.g., type 2 diabetes, hypertension, sleep apnea, cardiovascular disease, PCOS. These support medical necessity.",
    )
    tried_lifestyle = forms.BooleanField(
        required=False,
        label="Has the patient tried diet and exercise programs?",
        help_text="Many insurers require documented lifestyle modification attempts before covering anti-obesity medications.",
    )
    tried_other_meds = forms.CharField(
        max_length=300,
        required=False,
        label="Other weight loss treatments tried (if any)?",
        help_text="E.g., other medications, structured weight loss programs, bariatric surgery consultation.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("bmi"):
            response += f"Patient BMI: {self.cleaned_data['bmi']}. "
        if self.cleaned_data.get("comorbidities"):
            response += (
                f"Weight-related comorbidities: {self.cleaned_data['comorbidities']}. "
            )
        if self.cleaned_data.get("tried_lifestyle"):
            response += (
                "Patient has tried lifestyle modifications (diet and exercise). "
            )
        if self.cleaned_data.get("tried_other_meds"):
            response += (
                f"Other treatments tried: {self.cleaned_data['tried_other_meds']}. "
            )
        return response

    def main(self):
        r = []
        bmi = self.cleaned_data.get("bmi")
        comorbidities = self.cleaned_data.get("comorbidities")
        if bmi:
            r.append(
                f"The patient's BMI is {bmi}. FDA-approved anti-obesity medications "
                "are indicated for patients with BMI >= 30 kg/m², or BMI >= 27 kg/m² "
                "with at least one weight-related comorbidity."
            )
        if comorbidities:
            r.append(
                f"The patient has the following weight-related comorbidities: {comorbidities}. "
                "These conditions are directly worsened by obesity and support the medical "
                "necessity of pharmacological treatment."
            )
        if self.cleaned_data.get("tried_lifestyle"):
            r.append(
                "The patient has attempted lifestyle modifications including diet and exercise, "
                "which alone have proven insufficient. Clinical guidelines from the Endocrine "
                "Society and the American Association of Clinical Endocrinologists support "
                "pharmacotherapy when lifestyle changes are inadequate."
            )
        if self.cleaned_data.get("tried_other_meds"):
            r.append(
                f"The patient has also tried: {self.cleaned_data['tried_other_meds']}. "
                "The requested medication represents an appropriate next step in treatment."
            )
        return r


class ImagingQuestions(InsuranceQuestions):
    """Questions for imaging denial appeals (MRI, CT scan, etc.)."""

    symptoms = forms.CharField(
        max_length=300,
        required=False,
        label="What symptoms prompted this imaging?",
        help_text="Describe the symptoms that led the physician to order this imaging study.",
    )
    prior_imaging = forms.BooleanField(
        required=False,
        label="Was less advanced imaging already performed (e.g., X-ray before MRI)?",
        help_text="Some insurers require step-through imaging. If an X-ray or ultrasound was done first, note this.",
    )
    physician_ordered = forms.BooleanField(
        required=False,
        label="Was this imaging ordered by the treating physician?",
        help_text="Physician-ordered imaging based on clinical findings supports medical necessity.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("symptoms"):
            response += f"Symptoms prompting imaging: {self.cleaned_data['symptoms']}. "
        if self.cleaned_data.get("prior_imaging"):
            response += (
                "Less advanced imaging was already performed and was insufficient. "
            )
        if self.cleaned_data.get("physician_ordered"):
            response += "This imaging was ordered by the treating physician based on clinical findings. "
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("symptoms"):
            r.append(
                f"This imaging was ordered to evaluate: {self.cleaned_data['symptoms']}. "
                "The treating physician determined that this imaging modality was necessary "
                "for accurate diagnosis and treatment planning."
            )
        if self.cleaned_data.get("prior_imaging"):
            r.append(
                "Less advanced imaging was already performed and did not provide "
                "sufficient diagnostic information, warranting this more detailed study."
            )
        if self.cleaned_data.get("physician_ordered"):
            r.append(
                "This imaging study was ordered by the treating physician based on "
                "clinical examination findings. The American College of Radiology "
                "Appropriateness Criteria support imaging when clinical evaluation "
                "indicates the need for further diagnostic workup."
            )
        return r


class MentalHealthQuestions(InsuranceQuestions):
    """Questions for psychotherapy and mental health denial appeals."""

    diagnosis = forms.CharField(
        max_length=200,
        required=False,
        label="Mental health diagnosis (if known)?",
        help_text="E.g., major depressive disorder, generalized anxiety disorder, PTSD, bipolar disorder.",
    )
    treatment_duration = forms.CharField(
        max_length=100,
        required=False,
        label="How long has the patient been in treatment?",
        help_text="Continuity of mental health treatment is important for effective care.",
    )
    functional_impairment = forms.BooleanField(
        required=False,
        label="Does the condition cause significant functional impairment?",
        help_text="E.g., impacts work, school, relationships, or daily activities.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("diagnosis"):
            response += f"Mental health diagnosis: {self.cleaned_data['diagnosis']}. "
        if self.cleaned_data.get("treatment_duration"):
            response += (
                f"Treatment duration: {self.cleaned_data['treatment_duration']}. "
            )
        if self.cleaned_data.get("functional_impairment"):
            response += "The condition causes significant functional impairment. "
        response += (
            "The Mental Health Parity and Addiction Equity Act (MHPAEA) requires that "
            "mental health benefits be provided on parity with medical/surgical benefits. "
        )
        return response

    def main(self):
        r = []
        r.append(
            "Under the Mental Health Parity and Addiction Equity Act (MHPAEA), health "
            "plans must ensure that treatment limitations for mental health and substance "
            "use disorder benefits are no more restrictive than those applied to "
            "medical/surgical benefits."
        )
        if self.cleaned_data.get("diagnosis"):
            r.append(
                f"The patient has been diagnosed with {self.cleaned_data['diagnosis']}, "
                "which is a recognized condition requiring ongoing treatment."
            )
        if self.cleaned_data.get("functional_impairment"):
            r.append(
                "This condition causes significant functional impairment, "
                "further supporting the medical necessity of continued treatment."
            )
        if self.cleaned_data.get("treatment_duration"):
            r.append(
                f"The patient has been in treatment for {self.cleaned_data['treatment_duration']}. "
                "Disrupting established mental health treatment can lead to regression "
                "and worsened outcomes."
            )
        return r


class EmergencyServicesQuestions(InsuranceQuestions):
    """Questions for emergency room, ambulance, and emergency department denial appeals."""

    symptoms_at_time = forms.CharField(
        max_length=300,
        required=False,
        label="What symptoms or situation prompted the emergency visit?",
        help_text="Describe what a reasonable person would have experienced that made emergency care seem necessary.",
    )
    called_911 = forms.BooleanField(
        required=False,
        label="Was 911 called or was the patient transported by ambulance?",
        help_text="Ambulance transport further supports the emergency nature of the situation.",
    )
    stabilization_needed = forms.BooleanField(
        required=False,
        label="Did the patient require stabilization treatment in the ER?",
        help_text="Treatment received in the ER supports that the visit was medically necessary.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("symptoms_at_time"):
            response += f"Emergency symptoms: {self.cleaned_data['symptoms_at_time']}. "
        if self.cleaned_data.get("called_911"):
            response += "911 was called / patient transported by ambulance. "
        if self.cleaned_data.get("stabilization_needed"):
            response += "Patient required stabilization treatment in the ER. "
        return response

    def main(self):
        r = []
        r.append(
            "Under the prudent layperson standard (codified in federal law at 42 U.S.C. "
            "§ 1395dd and adopted by most state laws), emergency services must be covered "
            "based on the patient's presenting symptoms, not the final diagnosis. A "
            "reasonable person with the same symptoms would have sought emergency care."
        )
        if self.cleaned_data.get("symptoms_at_time"):
            r.append(
                f"The patient presented with: {self.cleaned_data['symptoms_at_time']}. "
                "These symptoms would cause a reasonable person to believe that immediate "
                "medical attention was necessary."
            )
        if self.cleaned_data.get("called_911"):
            r.append(
                "The severity of the situation warranted calling 911 and/or ambulance "
                "transport, which further demonstrates the emergency nature of the visit."
            )
        if self.cleaned_data.get("stabilization_needed"):
            r.append(
                "The patient required stabilization treatment in the emergency department. "
                "Under EMTALA, hospitals must provide stabilizing treatment for emergency "
                "conditions regardless of insurance status."
            )
        return r


class TherapyRehabQuestions(InsuranceQuestions):
    """Questions for physical therapy, speech therapy, and therapeutic services denial appeals."""

    therapy_type = forms.ChoiceField(
        choices=[
            ("", "---"),
            ("physical", "Physical therapy"),
            ("speech", "Speech therapy"),
            ("occupational", "Occupational therapy"),
            ("other", "Other therapeutic service"),
        ],
        required=False,
        label="Type of therapy?",
    )
    functional_goals = forms.CharField(
        max_length=300,
        required=False,
        label="What functional goals is this therapy addressing?",
        help_text="E.g., regain ability to walk, improve speech after stroke, restore hand function.",
    )
    progress_made = forms.BooleanField(
        required=False,
        label="Has the patient shown progress with this therapy?",
        help_text="Documented progress supports continued medical necessity.",
    )
    daily_impact = forms.CharField(
        max_length=300,
        required=False,
        label="How does the condition impact daily activities?",
        help_text="Describe specific limitations in work, self-care, mobility, or communication.",
    )

    def medical_context(self):
        response = super().medical_context()
        tt = self.cleaned_data.get("therapy_type", "")
        if tt and tt != "other":
            response += f"Therapy type: {tt}. "
        if self.cleaned_data.get("functional_goals"):
            response += f"Functional goals: {self.cleaned_data['functional_goals']}. "
        if self.cleaned_data.get("progress_made"):
            response += "Patient has demonstrated progress with therapy. "
        if self.cleaned_data.get("daily_impact"):
            response += (
                f"Impact on daily activities: {self.cleaned_data['daily_impact']}. "
            )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("functional_goals"):
            r.append(
                f"This therapy is necessary to achieve the following functional goals: "
                f"{self.cleaned_data['functional_goals']}. The treating therapist has "
                "determined that skilled intervention is required and the patient cannot "
                "achieve these goals independently."
            )
        if self.cleaned_data.get("progress_made"):
            r.append(
                "The patient has demonstrated measurable progress, indicating that "
                "continued therapy is effective and medically necessary. Under the "
                "Jimmo v. Sebelius settlement, coverage cannot be denied solely because "
                "the patient is not improving — maintenance therapy requiring skilled "
                "care must also be covered."
            )
        if self.cleaned_data.get("daily_impact"):
            r.append(
                f"Without this therapy, the patient's daily functioning is significantly "
                f"impaired: {self.cleaned_data['daily_impact']}."
            )
        return r


class GeneticTestingQuestions(InsuranceQuestions):
    """Questions for genetic testing denial appeals."""

    clinical_indication = forms.CharField(
        max_length=300,
        required=False,
        label="Why was genetic testing ordered?",
        help_text="E.g., family history of hereditary cancer, suspected genetic disorder, treatment selection for cancer.",
    )
    treatment_impact = forms.BooleanField(
        required=False,
        label="Will test results change the treatment plan?",
        help_text="Genetic tests that inform treatment decisions (e.g., targeted therapy selection) have strong clinical utility.",
    )
    guideline_recommended = forms.BooleanField(
        required=False,
        label="Is this test recommended by clinical guidelines (e.g., NCCN, ACMG)?",
        help_text="Tests recommended by national guidelines have strong evidence for coverage.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("clinical_indication"):
            response += f"Clinical indication for testing: {self.cleaned_data['clinical_indication']}. "
        if self.cleaned_data.get("treatment_impact"):
            response += "Test results will directly impact the treatment plan. "
        if self.cleaned_data.get("guideline_recommended"):
            response += (
                "This test is recommended by clinical guidelines (e.g., NCCN, ACMG). "
            )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("clinical_indication"):
            r.append(
                f"This genetic test was ordered because: {self.cleaned_data['clinical_indication']}. "
                "The clinical utility of this test is well-established."
            )
        if self.cleaned_data.get("treatment_impact"):
            r.append(
                "The results of this test will directly inform treatment decisions. "
                "Genetic testing that guides clinical management has demonstrated "
                "clinical utility and cost-effectiveness by enabling targeted therapies "
                "and avoiding ineffective treatments."
            )
        if self.cleaned_data.get("guideline_recommended"):
            r.append(
                "This genetic test is recommended by established clinical guidelines "
                "(such as NCCN or ACMG). Under 45 CFR § 156.122, health plans "
                "providing essential health benefits must cover medically necessary "
                "diagnostic services."
            )
        return r


class InpatientHospitalQuestions(InsuranceQuestions):
    """Questions for inpatient care and hospital stay denial appeals."""

    admission_reason = forms.CharField(
        max_length=300,
        required=False,
        label="Why was inpatient admission necessary?",
        help_text="Describe why the patient's condition required hospital-level care rather than outpatient treatment.",
    )
    observation_reclassified = forms.BooleanField(
        required=False,
        label="Was the stay reclassified from inpatient to observation?",
        help_text="Hospitals sometimes reclassify stays to observation status, which can affect coverage.",
    )
    length_of_stay = forms.CharField(
        max_length=50,
        required=False,
        label="Length of hospital stay?",
        help_text="E.g., '3 days', '1 week'. Helps assess whether the stay was appropriate.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("admission_reason"):
            response += f"Reason for inpatient admission: {self.cleaned_data['admission_reason']}. "
        if self.cleaned_data.get("observation_reclassified"):
            response += (
                "The stay was reclassified from inpatient to observation status. "
            )
        if self.cleaned_data.get("length_of_stay"):
            response += f"Length of stay: {self.cleaned_data['length_of_stay']}. "
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("admission_reason"):
            r.append(
                f"Inpatient admission was medically necessary because: "
                f"{self.cleaned_data['admission_reason']}. The patient's condition "
                "required hospital-level monitoring, treatment intensity, or services "
                "that could not be safely provided in an outpatient setting."
            )
        if self.cleaned_data.get("observation_reclassified"):
            r.append(
                "The stay was reclassified from inpatient to observation status. "
                "CMS guidance (the Two-Midnight Rule, 42 CFR § 412.3) provides that "
                "hospital stays expected to span two or more midnights should generally "
                "be treated as inpatient admissions. Retroactive reclassification to "
                "observation status may be inappropriate."
            )
        if self.cleaned_data.get("length_of_stay"):
            r.append(
                f"The hospital stay lasted {self.cleaned_data['length_of_stay']}, "
                "which was clinically appropriate given the patient's condition and "
                "treatment requirements."
            )
        return r


class SleepStudyQuestions(InsuranceQuestions):
    """Questions for sleep study denial appeals."""

    symptoms = forms.CharField(
        max_length=300,
        required=False,
        label="What symptoms prompted the sleep study?",
        help_text="E.g., excessive daytime sleepiness, loud snoring, witnessed apneas, morning headaches, fatigue.",
    )
    screening_score = forms.CharField(
        max_length=100,
        required=False,
        label="Sleep screening questionnaire score (if available)?",
        help_text="E.g., Epworth Sleepiness Scale score, STOP-BANG score. These support clinical indication.",
    )
    comorbidities = forms.CharField(
        max_length=300,
        required=False,
        label="Relevant comorbidities?",
        help_text="E.g., hypertension, obesity, heart failure, stroke history. Untreated sleep apnea worsens these conditions.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("symptoms"):
            response += f"Sleep-related symptoms: {self.cleaned_data['symptoms']}. "
        if self.cleaned_data.get("screening_score"):
            response += f"Screening score: {self.cleaned_data['screening_score']}. "
        if self.cleaned_data.get("comorbidities"):
            response += (
                f"Relevant comorbidities: {self.cleaned_data['comorbidities']}. "
            )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("symptoms"):
            r.append(
                f"The patient presents with: {self.cleaned_data['symptoms']}. "
                "The American Academy of Sleep Medicine (AASM) clinical guidelines "
                "recommend polysomnography for patients with symptoms suggestive of "
                "sleep-disordered breathing."
            )
        if self.cleaned_data.get("screening_score"):
            r.append(
                f"The patient's screening score ({self.cleaned_data['screening_score']}) "
                "indicates a high probability of sleep-disordered breathing, supporting "
                "the need for diagnostic polysomnography."
            )
        if self.cleaned_data.get("comorbidities"):
            r.append(
                f"The patient has comorbidities ({self.cleaned_data['comorbidities']}) "
                "that are known to be worsened by untreated sleep apnea. Diagnosis and "
                "treatment of sleep disorders can improve outcomes for these conditions "
                "and reduce overall healthcare costs."
            )
        return r


class LabWorkQuestions(InsuranceQuestions):
    """Questions for lab work denial appeals."""

    clinical_reason = forms.CharField(
        max_length=300,
        required=False,
        label="Why was this lab work ordered?",
        help_text="E.g., monitoring a chronic condition, diagnostic workup for symptoms, medication monitoring.",
    )
    preventive = forms.BooleanField(
        required=False,
        label="Is this lab work part of preventive/routine screening?",
        help_text="Many routine screening labs (e.g., cholesterol, diabetes screening) are required to be covered as preventive under the ACA.",
    )
    medication_monitoring = forms.BooleanField(
        required=False,
        label="Is this lab work needed to monitor medication?",
        help_text="Lab work required to safely monitor medications (e.g., liver function for statins, kidney function for metformin) is medically necessary.",
    )

    def medical_context(self):
        response = super().medical_context()
        if self.cleaned_data.get("clinical_reason"):
            response += f"Reason for lab work: {self.cleaned_data['clinical_reason']}. "
        if self.cleaned_data.get("preventive"):
            response += "This lab work is part of preventive/routine screening. "
        if self.cleaned_data.get("medication_monitoring"):
            response += "This lab work is needed to safely monitor medication. "
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("clinical_reason"):
            r.append(
                f"This laboratory testing was ordered because: {self.cleaned_data['clinical_reason']}. "
                "The treating physician determined this testing was necessary for "
                "appropriate clinical management."
            )
        if self.cleaned_data.get("preventive"):
            r.append(
                "This laboratory testing is part of preventive/routine screening. "
                "Under the ACA, preventive services recommended by the USPSTF with "
                "an A or B rating must be covered without cost-sharing. This includes "
                "screening tests for conditions such as diabetes, cholesterol, and STIs."
            )
        if self.cleaned_data.get("medication_monitoring"):
            r.append(
                "This laboratory testing is required to safely monitor the patient's "
                "medication. Failure to perform necessary monitoring labs could result "
                "in serious adverse effects. Standard of care requires ongoing laboratory "
                "monitoring for many medications."
            )
        return r


class AphasiaTreatmentQuestions(InsuranceQuestions):
    """Questions for aphasia treatment denial appeals."""

    aphasia_type = forms.ChoiceField(
        choices=[
            ("", "---"),
            ("brocas", "Broca's aphasia (non-fluent/expressive)"),
            ("wernickes", "Wernicke's aphasia (fluent/receptive)"),
            ("global", "Global aphasia"),
            ("anomic", "Anomic aphasia"),
            ("ppa", "Primary Progressive Aphasia"),
            ("other", "Other / not sure"),
        ],
        required=False,
        label="Type of aphasia (if known)?",
        help_text="The type of aphasia helps determine which treatments are most appropriate.",
    )
    cause = forms.ChoiceField(
        choices=[
            ("", "---"),
            ("stroke", "Stroke"),
            ("tbi", "Traumatic brain injury"),
            ("tumor", "Brain tumor"),
            ("neurodegenerative", "Neurodegenerative disease"),
            ("other", "Other"),
        ],
        required=False,
        label="What caused the aphasia?",
        help_text="The underlying cause helps establish medical necessity and expected recovery trajectory.",
    )
    treatment_type = forms.CharField(
        max_length=300,
        required=False,
        label="What type(s) of treatment are being requested?",
        help_text="E.g., Speech-Language Therapy (SLT), Constraint-Induced Language Therapy (CILT), "
        "Melodic Intonation Therapy (MIT), Semantic Feature Analysis (SFA), group therapy.",
    )
    onset_date = forms.CharField(
        max_length=50,
        required=False,
        label="When did the aphasia begin (approximate)?",
        help_text="E.g., '3 months ago', 'January 2025'. Both early and chronic aphasia benefit from treatment.",
    )
    functional_impact = forms.CharField(
        max_length=300,
        required=False,
        label="How does aphasia impact the patient's daily communication?",
        help_text="E.g., cannot express basic needs, difficulty with phone calls, unable to work, social isolation.",
    )

    def medical_context(self):
        response = super().medical_context()
        at = self.cleaned_data.get("aphasia_type", "")
        if at and at != "other":
            type_labels = {
                "brocas": "Broca's (non-fluent/expressive)",
                "wernickes": "Wernicke's (fluent/receptive)",
                "global": "Global",
                "anomic": "Anomic",
                "ppa": "Primary Progressive Aphasia",
            }
            response += f"Aphasia type: {type_labels.get(at, at)}. "
        cause = self.cleaned_data.get("cause", "")
        if cause and cause != "other":
            response += f"Cause: {cause}. "
        if self.cleaned_data.get("treatment_type"):
            response += f"Requested treatment: {self.cleaned_data['treatment_type']}. "
        if self.cleaned_data.get("onset_date"):
            response += f"Onset: {self.cleaned_data['onset_date']}. "
        if self.cleaned_data.get("functional_impact"):
            response += f"Functional impact: {self.cleaned_data['functional_impact']}. "
        response += (
            "Research from the NIH and ASHA demonstrates that intensive speech-language "
            "therapy for aphasia produces significant improvements in language function. "
            "Neuroplasticity research supports ongoing treatment even in chronic aphasia. "
        )
        return response

    def main(self):
        r = []
        r.append(
            "Aphasia is an acquired language disorder that significantly impairs "
            "communication. Research published by the National Institutes of Health "
            "and endorsed by the American Speech-Language-Hearing Association (ASHA) "
            "demonstrates that speech-language therapy produces meaningful improvements "
            "in language outcomes for people with aphasia. The evidence base supports "
            "both intensive and ongoing treatment."
        )
        cause = self.cleaned_data.get("cause", "")
        if cause == "stroke":
            r.append(
                "This aphasia resulted from a stroke. The American Heart Association / "
                "American Stroke Association guidelines recommend speech-language therapy "
                "as a standard component of post-stroke rehabilitation. Rehabilitation "
                "services following stroke are an essential health benefit under the ACA."
            )
        elif cause == "tbi":
            r.append(
                "This aphasia resulted from a traumatic brain injury. Clinical guidelines "
                "support speech-language therapy as part of comprehensive TBI rehabilitation."
            )
        if self.cleaned_data.get("treatment_type"):
            treatment = self.cleaned_data["treatment_type"]
            r.append(
                f"The requested treatment ({treatment}) is an evidence-based approach "
                "for aphasia rehabilitation. Treatments such as Constraint-Induced "
                "Language Therapy (CILT), Melodic Intonation Therapy (MIT), and "
                "Semantic Feature Analysis (SFA) have peer-reviewed evidence supporting "
                "their effectiveness in improving language function."
            )
        if self.cleaned_data.get("onset_date"):
            r.append(
                f"The aphasia began approximately {self.cleaned_data['onset_date']}. "
                "Research on neuroplasticity demonstrates that the brain can continue "
                "to reorganize language networks well beyond the acute recovery period. "
                "Studies published in journals such as Stroke and Brain show that patients "
                "with chronic aphasia continue to benefit from speech-language therapy."
            )
        if self.cleaned_data.get("functional_impact"):
            r.append(
                f"The aphasia significantly impacts daily communication: "
                f"{self.cleaned_data['functional_impact']}. Without treatment, "
                "these communication barriers can lead to social isolation, depression, "
                "reduced quality of life, and inability to manage one's own healthcare — "
                "all of which increase overall healthcare costs."
            )
        r.append(
            "Under the ACA, rehabilitative and habilitative services are essential "
            "health benefits. The Jimmo v. Sebelius settlement clarifies that therapy "
            "coverage cannot be denied solely because the patient has reached a plateau — "
            "skilled maintenance therapy must also be covered when it requires the skills "
            "of a qualified therapist."
        )
        return r
