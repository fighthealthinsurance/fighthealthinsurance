import re
import urllib
from typing import Dict, Iterable, Optional

from asgiref.sync import sync_to_async
from django import forms

from fighthealthinsurance.form_utils import StyledWidgetsMixin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import Denial

# A mention of WPATH, and the Standards of Care version near one: "Standards
# of Care ..., Version 7" or "SOC-8". Gaps are bounded and the version is
# looked for only near a mention: the open-ended
# WPATH.*?Standards of.*?Care.*?Version.*?(\d+) this replaces backtracked
# polynomially over a long document that mentions WPATH but no version.
_WPATH_MENTION_RE = re.compile(r"WPATH", re.IGNORECASE)
_SOC_VERSION_RE = re.compile(
    r"Standards\s+of\s+Care\b[^.;]{0,160}?\bVersion\s*(\d{1,2})\b"
    r"|\bSOC[\s\-\u2010-\u2015]?v?(\d{1,2})\b",
    re.IGNORECASE,
)
_WPATH_WINDOW_CHARS = 400

# The HRC lookup runs while an appeal is generated; without a timeout a
# slow hrc.org held the appeal for as long as requests cared to wait.
HRC_TIMEOUT_SECONDS = 10


def wpath_version_in(texts: Iterable[str]) -> Optional[str]:
    """The WPATH Standards of Care version plan documents cite.

    Its number when a mention of WPATH has one near it, "" when WPATH is
    mentioned without one, None when no document mentions it. Every
    document is read: the first one uploaded is often the summary of
    benefits, not the policy that cites the standards.
    """
    found: Optional[str] = None
    for text in texts:
        for mention in _WPATH_MENTION_RE.finditer(text):
            window = text[
                max(0, mention.start() - _WPATH_WINDOW_CHARS) : mention.end()
                + _WPATH_WINDOW_CHARS
            ]
            version = _SOC_VERSION_RE.search(window)
            if version:
                return version.group(1) or version.group(2)
            found = ""
    return found


# What a denial type's form asks, now that the questions page also carries
# questions generated for the procedure and diagnosis
# (MLAppealQuestionsHelper) and follow-ups after them:
#
# - facts about the claim and the denial itself (urgent, pre-service, prior
#   auth, other insurance), which the generated questions are told not to
#   ask about;
# - yes/no and pick-one questions whose answer unlocks a specific argument in
#   ``main()`` (screening vs diagnostic, a mid-year formulary change, the
#   final-diagnosis denial the prudent layperson standard forbids).
#
# Free-text questions about the patient's history (age, symptoms, BMI,
# diagnosis, how long in treatment) are left to the generated questions,
# which ask them about this patient's procedure rather than the denial
# type's, so the page no longer asks them twice. Answers stored under the
# removed fields' names still reach the letter through qa_context.


class InsuranceQuestions(StyledWidgetsMixin, forms.Form):
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

    def _append_context(self, response: str, field: str, label: str) -> str:
        """Append field value to context. Booleans get 'label.', others get 'label: val.'."""
        val = self.cleaned_data.get(field)
        if val:
            if response and not response[-1].isspace():
                response += " "
            if isinstance(self.fields.get(field), forms.BooleanField):
                response += f"{label}. "
            else:
                response += f"{label}: {val}. "
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
        # The leading "" is kept: callers dedupe footers by value and the
        # appeal has always started its footer with a blank paragraph.
        if self.cleaned_data.get("urgent") and self.cleaned_data.get("pre_service"):
            return [
                "",
                (
                    "As an urgent pre-service claim you must respond within the "
                    "timeline required for my medical situation (up to a maximum "
                    "of four days). This also serves as notice of concurrent "
                    "request of external review."
                ),
            ]
        if self.cleaned_data.get("pre_service"):
            return [
                "",
                "As this is a non-urgent pre-service claim, I understand you have approximately 30 days to respond.",
            ]
        return [""]


class MedicalNecessaryQuestions(InsuranceQuestions):
    """Questions to ask for medical necessity.

    The patient's history (age, symptoms, what was tried) is left to the
    generated questions, which ask it for this procedure and diagnosis.
    ``medical_reason`` stays: it fills the ``{medical_reason}`` slot of the
    canned appeal templates.
    """

    medical_reason = forms.CharField(
        max_length=200,
        label="Why is this medically necessary (if you know)?",
        required=False,
    )

    def medical_context(self):
        response = super().medical_context()
        response = self._append_context(
            response, "medical_reason", "The medical reason may be"
        )
        return response

    def main(self):
        return [
            "The claim was denied as not medically necessary; however, it is medically necessary for {medical_reason}."
        ]


# Backwards-compat alias: existing DB records may store the old misspelled name
MedicalNeccessaryQuestions = MedicalNecessaryQuestions


class ExperimentalQuestions(MedicalNecessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Why do you believe this is not experimental?",
        help_text=(
            "Insurance companies tend to claim anything expensive is experimental. "
            "Good ways to show something is not experimental: look for documents like the 'standards of care' or medical journals (including the NIH or pubmed)."
        ),
        required=False,
    )


class NotCoveredQuestions(MedicalNecessaryQuestions):
    medical_reason = forms.CharField(
        max_length=200,
        label="Why should this be covered?",
        help_text="The plan may not say it's not medically necessary, just that they don't want to pay. Good arguments: request plan documents, demand the policy under which it is not covered, or explain why it should be covered.",
        required=False,
    )


class NotCoveredByQuestions(NotCoveredQuestions):
    """Alt name to match the db entry (my bad)"""


class UnderpaidOutOfNetworkQuestions(InsuranceQuestions):
    """Questions for the seeded "Underpaid Out of Network" denial type.

    Inherits ``InsuranceQuestions`` for ``preface()``, ``main()`` and
    ``footer()``, which the appeal builder calls unguarded on every form a
    denial type resolves to.
    """

    why_need_out_of_network = forms.CharField(
        max_length=300,
        required=False,
        label="Why did the care have to happen out of network?",
        help_text="E.g., no in-network provider, in-network providers don't meet standards of care, don't accept new patients, or don't perform the needed service.",
    )

    def medical_context(self):
        return self._append_context(
            super().medical_context(),
            "why_need_out_of_network",
            "Reason the care had to be out of network",
        )

    def main(self):
        reason = self.cleaned_data.get("why_need_out_of_network")
        if not reason:
            return []
        if self.prof_pov:
            return [
                f"Out-of-network services were medically necessary in this case because {reason}"
            ]
        return [
            f"I believe you should pay this out of network claim in full since {reason}"
        ]


# This is related to why weren't you able to get a prior auth.
class PriorAuthQuestions(InsuranceQuestions):
    emergency = forms.BooleanField(
        required=False,
        label="This was an emergency, so there was no time to get prior authorization",
    )
    contact_insurance_before = forms.BooleanField(
        required=False, label="I contacted my insurance before the service"
    )
    told_prior_auth_not_needed = forms.BooleanField(
        required=False, label="I was told prior authorization was not needed"
    )
    prior_auth_obtained = forms.BooleanField(
        required=False, label="Prior authorization was obtained"
    )
    prior_auth_id = forms.CharField(
        max_length=300,
        required=False,
        label="Prior authorization number, if you have one",
    )

    def main(self):
        r: list[str] = []
        if self.cleaned_data.get("emergency"):
            r.append(
                "This service was an emergency so prior auth could not be obtained."
            )
        if self.cleaned_data.get("told_prior_auth_not_needed"):
            r.append("It was communicated that prior authorization was not necessary.")
        elif self.cleaned_data.get("contact_insurance_before"):
            r.append("The insurance company was contacted before the service.")
        if self.cleaned_data.get("prior_auth_id"):
            r.append(
                f"Prior auth was obtained (id {self.cleaned_data['prior_auth_id']})"
            )
        elif self.cleaned_data.get("prior_auth_obtained"):
            r.append("Prior authorization was obtained.")
        return r


class GenderAffirmingCareQuestions(InsuranceQuestions):
    """Generic questions for gender affirming care."""

    async def plan_context(self, denial: Denial) -> str:
        """What the appeal should know about the plan and the employer.

        Async, called through common_view_logic.form_plan_context: it reads
        the plan documents and asks hrc.org, and neither may run on the
        event loop of the appeal generator that calls it.
        """
        response = ""
        if denial.state == "CA":
            response += (
                "As covered in https://calmatters.org/health/2024/08"
                "/gender-affirming-care-denials/ CA health plans received the "
                "largest penalty ever for gender-affirming care denials. "
                "The plan must follow https://www.insurance.ca.gov/01-consumers/110-health/"
                "60-resources/upload/CDI-Gender-Nondiscrimination-Regulations.pdf "
                "and insurers may not discriminate against gender affirming "
                "care. If the plan fails to approve this claim the patient "
                "intends to appeal all the way to the relevant regulator."
            )
        else:
            response += (
                "As covered in https://calmatters.org/health/2024/08"
                "/gender-affirming-care-denials/ CA health plans received "
                "the largest penalty ever for gender-affirming care denials "
                "and some states have similar non-discrimination requirements."
            )

        if denial.employer_name is not None:
            # Network only, no ORM: plain asgiref sync_to_async, on its own
            # thread rather than the thread-sensitive one the ORM shares,
            # which up to two slow hrc.org requests would otherwise hold.
            if await sync_to_async(self.employer_hrc_lookup, thread_sensitive=False)(
                denial.employer_name
            ):
                response += (
                    "The employer has stated to the Human Rights Campaign "
                    "(HRC) that it will cover transgender health care. Should "
                    "the plan deny this claim we intend to follow up with "
                    "both HR and the HRC."
                )

        wpath_version = await self.wpath_version(denial)
        if wpath_version is not None:
            response += (
                "As stated in the plan documents the plan must follow the "
                f"WPATH{wpath_version} standards of care."
            )
        return response

    async def wpath_version(self, denial: Denial) -> Optional[str]:
        """The WPATH version the denial's plan documents cite (see
        wpath_version_in), read once per denial for this form.

        Read decrypted through MLPlanDocHelper.load_plan_documents: uploads
        are stored encrypted, and the unencrypted field this used to open
        is empty for every one of them.
        """
        versions: Dict[int, Optional[str]] = self.__dict__.setdefault(
            "_wpath_versions", {}
        )
        if denial.denial_id not in versions:
            documents = await MLPlanDocHelper.load_plan_documents(denial.denial_id)
            versions[denial.denial_id] = wpath_version_in(doc.text for doc in documents)
        return versions[denial.denial_id]

    def employer_hrc_lookup(self, employer):
        # Check and see if the employer is listed in the HRC equality index
        try:
            safe_employer_name = urllib.parse.quote_plus(employer)
            employer_search_string = (
                f"https://www.hrc.org/resources/employers/search?q={safe_employer_name}"
            )
            r = requests.get(employer_search_string, timeout=HRC_TIMEOUT_SECONDS)
            if "No results found for" not in r.text:
                soup = BeautifulSoup(r.text, "html.parser")
                link_re = re.compile("https://www.hrc.org/resources/buyers-guide/.*")
                links = soup.find_all("a", {"href": link_re})
                text = ""
                for bs_link in links:
                    if employer.lower() in bs_link.getText().lower():
                        r = requests.get(bs_link["href"], timeout=HRC_TIMEOUT_SECONDS)
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
    async def plan_context(self, denial: Denial) -> str:
        if await self.wpath_version(denial) == "7":
            return """The plan references version 7 of the WPATH SOC. As covered on P59 of the WPATH 7 SOC the only requirements for breast augmentation is 1. Persistent, well-documented gender dysphoria;
            2. Capacity to make a fully informed decision and to consent for treatment;
            3. Age of majority in a given country (if younger, follow the SOC for children and adolescents);
            4. If significant medical or mental health concerns are present, they must be reasonably well
            controlled."""
        return await super().plan_context(denial)


class PreventiveCareQuestions(InsuranceQuestions):
    """Questions for preventive care.

    Preventive care is not only screening: it also covers immunizations,
    preventive medications (PrEP, statins), contraception, counseling and
    well visits, so nothing here assumes the service is a screening test.
    """

    medical_reason = forms.CharField(
        max_length=300,
        required=False,
        label="Why does the patient need this preventive care (if you know)?",
        help_text="E.g., risk factors such as family history or prior conditions, their age, or their doctor's recommendation.",
    )
    trans_gender = forms.BooleanField(
        required=False,
        label="Is the patient transgender?",
        help_text="Some preventive care is only covered for certain genders. If the patient is transgender, insurance may incorrectly deny necessary coverage. Check this box if it applies.",
    )

    def medical_context(self):
        response = super().medical_context()
        response += (
            "This procedure may be preventive, make sure to include a link to "
            "https://www.healthcare.gov/coverage/preventive-care-benefits/ if "
            "that's the case. Preventive care is not only screening: ACA "
            "preventive coverage also includes immunizations, preventive "
            "medications, contraception and counseling, so do not describe "
            "the service as a screening unless it is one. "
        )
        response = self._append_context(
            response, "trans_gender", "The patient is transgender"
        )
        response = self._append_context(
            response,
            "medical_reason",
            "Why the patient needs this preventive care",
        )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("trans_gender"):
            if self.prof_pov:
                r.append(
                    "The patient is transgender, so it is important that preventive coverage for all relevant genders is provided."
                )
            else:
                r.append(
                    "I am trans so it is important that preventive coverage for both genders be covered."
                )
        if self.cleaned_data.get("medical_reason"):
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

    def preface(self):
        if self.cleaned_data.get("is_known_3rd_party"):
            details = self.cleaned_data.get("alternate_insurance_details", "")
            if self.prof_pov:
                return [
                    f"As requested, I am providing details regarding third-party insurance coverage for this claim: "
                    f"{details}. Please ensure that all relevant coordination of benefits is considered in the review of this claim."
                ]
            return [f"As requested, the third-party insurance is {details}"]
        return super().preface()


class FormularyChangeQuestions(InsuranceQuestions):
    """Questions for formulary changes and continuity of care appeals."""

    currently_taking = forms.BooleanField(
        required=False,
        label="Are you currently taking this medication?",
        help_text="Check if you've been taking this medication and it's being removed from formulary or moved to a higher tier.",
    )
    medication_working = forms.BooleanField(
        required=False,
        label="Is the medication working well for you?",
        help_text="Check if your condition is stable and well-controlled on this medication.",
    )
    tried_alternatives = forms.BooleanField(
        required=False,
        label="Did the alternative medication(s) the insurer suggests not work for you, or cause problems?",
        help_text="Check if you tried the preferred/alternative medications and they didn't work, or caused side effects or other problems you couldn't continue with.",
    )
    mid_year_change = forms.BooleanField(
        required=False,
        label="Is this a mid-year formulary change?",
        help_text="Check if the formulary changed during your plan year (not at annual renewal).",
    )

    def medical_context(self):
        """Return context about continuity of care to inform the LLM."""
        response = super().medical_context()
        response = self._append_context(
            response,
            "currently_taking",
            "The patient is currently taking this medication",
        )
        response = self._append_context(
            response,
            "medication_working",
            "The medication is working well and their condition is stable",
        )
        response = self._append_context(
            response,
            "tried_alternatives",
            "The patient tried the alternative medications and they were not effective or not tolerated",
        )
        response = self._append_context(
            response,
            "mid_year_change",
            "This is a mid-year formulary change, which may trigger additional patient protections",
        )
        return response

    def main(self):
        """Return main appeal text based on answers."""
        r = []

        currently_taking = bool(self.cleaned_data.get("currently_taking"))

        # Only add continuity of care arguments if patient is currently taking the medication
        if currently_taking:
            r.append(
                "I am requesting a medical exception. I believe this is required for continuity of care. "
                "I am currently taking this medication, and switching would disrupt my care."
            )

        if currently_taking and self.cleaned_data.get("medication_working"):
            r.append(
                "My current medication is working effectively. Medical literature shows that "
                "medication switching can lead to adverse outcomes, treatment failures, and "
                "increased healthcare costs."
            )

        if self.cleaned_data.get("tried_alternatives"):
            r.append(
                "I have previously tried the alternative medication(s) you suggest, "
                "which were not effective or not tolerated."
            )

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
        response = self._append_context(
            response,
            "polyp_removal",
            "Polyps were removed during the procedure",
        )
        response = self._append_context(
            response,
            "family_history",
            "Patient has a family history of colorectal cancer",
        )
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
        elif sd == "diagnostic":
            r.append(
                "This was a diagnostic colonoscopy performed to evaluate clinical "
                "findings. Diagnostic colonoscopies are medically necessary "
                "procedures that differ from preventive screenings and are indicated "
                "by symptoms, abnormal test results, or surveillance needs."
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

    tried_lifestyle = forms.BooleanField(
        required=False,
        label="Has the patient tried diet and exercise programs?",
        help_text="Many insurers require documented lifestyle modification attempts before covering anti-obesity medications.",
    )

    def medical_context(self):
        response = super().medical_context()
        response = self._append_context(
            response,
            "tried_lifestyle",
            "Patient has tried lifestyle modifications (diet and exercise)",
        )
        # The coverage criterion the generated questions' BMI and
        # comorbidity answers are measured against.
        response += (
            "FDA-approved anti-obesity medications are indicated for patients "
            "with BMI >= 30 kg/m², or BMI >= 27 kg/m² with at least one "
            "weight-related comorbidity. "
        )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("tried_lifestyle"):
            r.append(
                "The patient has attempted lifestyle modifications including diet and exercise, "
                "which alone have proven insufficient. Clinical guidelines from the Endocrine "
                "Society and the American Association of Clinical Endocrinologists support "
                "pharmacotherapy when lifestyle changes are inadequate."
            )
        return r


class ImagingQuestions(InsuranceQuestions):
    """Questions for imaging denial appeals (MRI, CT scan, etc.)."""

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
        response = self._append_context(
            response,
            "prior_imaging",
            "Less advanced imaging was already performed and was insufficient",
        )
        response = self._append_context(
            response,
            "physician_ordered",
            "This imaging was ordered by the treating physician based on clinical findings",
        )
        return response

    def main(self):
        r = []
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

    functional_impairment = forms.BooleanField(
        required=False,
        label="Does the condition cause significant functional impairment?",
        help_text="E.g., impacts work, school, relationships, or daily activities.",
    )

    def medical_context(self):
        response = super().medical_context()
        response = self._append_context(
            response,
            "functional_impairment",
            "The condition causes significant functional impairment",
        )
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
        if self.cleaned_data.get("functional_impairment"):
            r.append(
                "This condition causes significant functional impairment, "
                "further supporting the medical necessity of continued treatment."
            )
        return r


class EmergencyServicesQuestions(InsuranceQuestions):
    """Questions for emergency room, ambulance, and emergency department denial appeals."""

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
    denied_based_on_final_diagnosis = forms.BooleanField(
        required=False,
        label="Was the claim denied because the final diagnosis turned out not to be an emergency?",
        help_text=(
            "The prudent layperson standard requires coverage based on your symptoms at "
            "the time of the visit, not the final diagnosis."
        ),
    )

    def medical_context(self):
        response = super().medical_context()
        response = self._append_context(
            response,
            "called_911",
            "911 was called / patient transported by ambulance",
        )
        response = self._append_context(
            response,
            "stabilization_needed",
            "Patient required stabilization treatment in the ER",
        )
        response = self._append_context(
            response,
            "denied_based_on_final_diagnosis",
            "The claim was denied based on the final diagnosis rather than the presenting symptoms",
        )
        return response

    def main(self):
        r = []
        r.append(
            "Under the prudent layperson standard (codified in federal law at 42 U.S.C. "
            "§ 1395dd and adopted by most state laws), emergency services must be covered "
            "based on the patient's presenting symptoms, not the final diagnosis. A "
            "reasonable person with the same symptoms would have sought emergency care."
        )
        r.append(
            "As the American College of Emergency Physicians explains, the prudent "
            "layperson standard \"requires that insurance coverage be based on a patient's "
            'symptoms, not their final diagnosis," and patients are not expected to be '
            "healthcare professionals or to diagnose themselves. Because roughly 90% of "
            "urgent and non-urgent symptoms overlap, the appropriateness of an emergency "
            "visit cannot be judged in hindsight from the final diagnosis."
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
        if self.cleaned_data.get("denied_based_on_final_diagnosis"):
            r.append(
                "This claim was denied based on the final diagnosis rather than the "
                "symptoms at the time of presentation, which is precisely what the prudent "
                "layperson standard prohibits."
            )
        r.append(
            "For reference, see the American College of Emergency Physicians: "
            "https://www.emergencyphysicians.org/article/access/prudent-layperson-standard "
            "and https://www.acep.org/administration/reimbursement/reimbursement-faqs/"
            "emtala-and-prudent-layperson-standard-faq."
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
    progress_made = forms.BooleanField(
        required=False,
        label="Has the patient shown progress with this therapy?",
        help_text="Documented progress supports continued medical necessity.",
    )

    def medical_context(self):
        response = super().medical_context()
        tt = self.cleaned_data.get("therapy_type", "")
        if tt and tt != "other":
            response += f"Therapy type: {tt}. "
        response = self._append_context(
            response,
            "progress_made",
            "Patient has demonstrated progress with therapy",
        )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("progress_made"):
            r.append(
                "The patient has demonstrated measurable progress, indicating that "
                "continued therapy is effective and medically necessary. Under the "
                "Jimmo v. Sebelius settlement, coverage cannot be denied solely because "
                "the patient is not improving — maintenance therapy requiring skilled "
                "care must also be covered."
            )
        return r


class GeneticTestingQuestions(InsuranceQuestions):
    """Questions for genetic testing denial appeals."""

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
        response = self._append_context(
            response,
            "treatment_impact",
            "Test results will directly impact the treatment plan",
        )
        response = self._append_context(
            response,
            "guideline_recommended",
            "This test is recommended by clinical guidelines (e.g., NCCN, ACMG)",
        )
        return response

    def main(self):
        r = []
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

    observation_reclassified = forms.BooleanField(
        required=False,
        label="Was the stay reclassified from inpatient to observation?",
        help_text="Hospitals sometimes reclassify stays to observation status, which can affect coverage.",
    )

    def medical_context(self):
        response = super().medical_context()
        response = self._append_context(
            response,
            "observation_reclassified",
            "The stay was reclassified from inpatient to observation status",
        )
        return response

    def main(self):
        r = []
        if self.cleaned_data.get("observation_reclassified"):
            r.append(
                "The stay was reclassified from inpatient to observation status. "
                "CMS guidance (the Two-Midnight Rule, 42 CFR § 412.3) provides that "
                "hospital stays expected to span two or more midnights should generally "
                "be treated as inpatient admissions. Retroactive reclassification to "
                "observation status may be inappropriate."
            )
        return r


class SleepStudyQuestions(InsuranceQuestions):
    """Sleep study denial appeals.

    Asks nothing of its own: the symptoms, screening scores and
    comorbidities it used to ask about are the patient's history, which the
    generated questions ask. It still gives the letter the AASM guidance
    those answers are argued against.
    """

    def medical_context(self):
        response = super().medical_context()
        response += (
            "The American Academy of Sleep Medicine (AASM) clinical guidelines "
            "recommend polysomnography for patients with symptoms suggestive of "
            "sleep-disordered breathing, and untreated sleep apnea worsens "
            "conditions such as hypertension, heart failure and stroke risk. "
        )
        return response


class LabWorkQuestions(InsuranceQuestions):
    """Questions for lab work denial appeals."""

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
        response = self._append_context(
            response,
            "preventive",
            "This lab work is part of preventive/routine screening",
        )
        response = self._append_context(
            response,
            "medication_monitoring",
            "This lab work is needed to safely monitor medication",
        )
        return response

    def main(self):
        r = []
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

    APHASIA_TYPE_LABELS = {
        "brocas": "Broca's (non-fluent/expressive)",
        "wernickes": "Wernicke's (fluent/receptive)",
        "global": "Global",
        "anomic": "Anomic",
        "ppa": "Primary Progressive Aphasia",
    }

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

    def medical_context(self):
        response = super().medical_context()
        at = self.cleaned_data.get("aphasia_type", "")
        if at and at != "other":
            response += f"Aphasia type: {self.APHASIA_TYPE_LABELS.get(at, at)}. "
        cause = self.cleaned_data.get("cause", "")
        if cause and cause != "other":
            response += f"Cause: {cause}. "
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
        r.append(
            "Under the ACA, rehabilitative and habilitative services are essential "
            "health benefits. The Jimmo v. Sebelius settlement clarifies that therapy "
            "coverage cannot be denied solely because the patient has reached a plateau — "
            "skilled maintenance therapy must also be covered when it requires the skills "
            "of a qualified therapist."
        )
        return r


class AssistiveDeviceQuestions(InsuranceQuestions):
    """Questions for assistive device and AAC (Augmentative and Alternative Communication) denial appeals."""

    DEVICE_LABELS = {
        "aac_high_tech": "High-tech AAC device (speech-generating device/tablet)",
        "aac_low_tech": "Low-tech AAC (picture boards/communication books)",
        "mobility": "Mobility device",
        "prosthetic": "Prosthetic device",
        "orthotic": "Orthotic device",
        "hearing": "Hearing aid or cochlear implant",
    }

    device_type = forms.ChoiceField(
        choices=[
            ("", "---"),
            (
                "aac_high_tech",
                "High-tech AAC device (tablet, speech-generating device)",
            ),
            ("aac_low_tech", "Low-tech AAC (picture boards, communication books)"),
            ("mobility", "Mobility device (wheelchair, walker, scooter)"),
            ("prosthetic", "Prosthetic device"),
            ("orthotic", "Orthotic device"),
            ("hearing", "Hearing aid or cochlear implant"),
            ("other", "Other assistive device"),
        ],
        required=False,
        label="Type of assistive device?",
        help_text="The type of device helps determine applicable coverage rules and clinical guidelines.",
    )
    professional_evaluation = forms.BooleanField(
        required=False,
        label="Has a qualified professional evaluated the patient for this device?",
        help_text="E.g., SLP evaluation for AAC, PT/OT evaluation for mobility devices, audiologist for hearing aids.",
    )

    def medical_context(self):
        response = super().medical_context()
        dt = self.cleaned_data.get("device_type", "")
        if dt and dt != "other":
            response += f"Device type: {self.DEVICE_LABELS.get(dt, dt)}. "
        response = self._append_context(
            response,
            "professional_evaluation",
            "Patient has been evaluated by a qualified professional for this device",
        )
        return response

    def main(self):
        r = []
        dt = self.cleaned_data.get("device_type", "")
        is_aac = dt in ("aac_high_tech", "aac_low_tech")
        if is_aac:
            r.append(
                "Augmentative and Alternative Communication (AAC) devices are evidence-based "
                "tools that enable individuals with communication disorders to express needs, "
                "participate in healthcare decisions, and maintain social connections. ASHA "
                "(American Speech-Language-Hearing Association) position statements affirm that "
                "AAC is an appropriate intervention for individuals across the lifespan with "
                "severe communication impairments."
            )
        if self.cleaned_data.get("professional_evaluation"):
            r.append(
                "A qualified professional has evaluated the patient and determined that "
                "this specific device is medically necessary. The evaluation considered "
                "the patient's functional abilities, communication needs, and potential "
                "for benefit from the device."
            )
        if is_aac:
            r.append(
                "Under the ACA, habilitative and rehabilitative services — including "
                "speech-language pathology services and related devices — are essential "
                "health benefits. Medicare covers speech-generating devices as durable "
                "medical equipment (DME) under 42 U.S.C. § 1395x(n). Many state Medicaid "
                "programs and private insurers are required to cover AAC devices when "
                "medically necessary."
            )
        else:
            r.append(
                "Under the ACA, rehabilitative and habilitative services are essential "
                "health benefits. Assistive devices that are medically necessary for the "
                "patient's condition must be covered. Denying coverage for a device that "
                "enables basic functional independence is inconsistent with standard "
                "medical practice and coverage requirements."
            )
        return r
