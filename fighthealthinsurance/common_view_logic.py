import json

from typing import Any, Tuple

from django.forms import Form
from django.core.validators import validate_email
from django.http import StreamingHttpResponse
from string import Template

from fighthealthinsurance.forms import *
from fighthealthinsurance.models import *
from fighthealthinsurance.generate_appeal import *
from dataclasses import dataclass

import uszipcode

appealGenerator = AppealGenerator()


class RemoveDataHelper:
    @classmethod
    def remove_data_for_email(cls, email: str):
        hashed_email = Denial.get_hashed_email(email)
        denials = Denial.objects.filter(hashed_email=hashed_email).delete()
        FollowUpSched.objects.filter(email=email).delete()


states_with_caps = {
    "AR",
    "CA",
    "CT",
    "DE",
    "DC",
    "GA",
    "IL",
    "IA",
    "KS",
    "KY",
    "ME",
    "MD",
    "MA",
    "MI",
    "MS",
    "MO",
    "MT",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "MP",
    "OK",
    "OR",
    "PA",
    "RI",
    "TN",
    "TX",
    "VT",
    "VI",
    "WV",
}


@dataclass
class NextStepInfo:
    outside_help_details: list[Tuple[str, str]]
    combined_form: Form
    semi_sekret: str

    def convert_to_serializable(self):
        print(
            f"Preparing to convert {self.outside_help_details} {self.combined_form} and {self.semi_sekret}"
        )
        return NextStepInfoSerializable(
            self.outside_help_details,
            map(lambda xy: self._field_to_dict(*xy), self.combined_form.fields.items()),
            self.semi_sekret,
        )

    def _field_to_dict(self, field_name, field):
        label = field.label
        visible = not field.hidden_widget
        required = field.required
        help_text = field.help_text
        initial = field.initial
        field_type = field.__class__.__name__
        r = {
            "name": field_name,
            "field_type": field_type,
            "label": label,
            "visible": visible,
            "required": required,
            "help_text": help_text,
            "initial": initial,
            "type": field_type,
        }
        if hasattr(field, "choices"):
            r["choices"] = field.choices
        return r


@dataclass
class NextStepInfoSerializable:
    outside_help_details: list[str]
    combined_form: list[Any]
    semi_sekret: str


class FindNextStepsHelper:
    @classmethod
    def find_next_steps(
        cls,
        denial_id: str,
        email: str,
        procedure: str,
        diagnosis: str,
        insurance_company,
        plan_id,
        claim_id,
        denial_type,
        your_state,
        denial_date,
        semi_sekret,
        captcha=None,
        denial_type_text=None,
        plan_source=None,
    ) -> NextStepInfo:
        hashed_email = Denial.get_hashed_email(email)
        # Update the denial
        denial = Denial.objects.filter(
            denial_id=denial_id,
            # Include the hashed e-mail so folks can't brute force denial_id
            hashed_email=hashed_email,
            semi_sekret=semi_sekret,
        ).get()
        denial.denial_date = denial_date

        denial.procedure = procedure
        denial.diagnosis = diagnosis
        if plan_source is not None:
            denial.plan_source.set(plan_source)
        denial.save()

        outside_help_details = []
        state = your_state

        if state in states_with_caps:
            outside_help_details.append(
                (
                    (
                        "<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/"
                        + state
                        + "'>"
                        + f"Your state {state} participates in a "
                        + f"Consumer Assistance Program(CAP), and you may be able to get help "
                        + f"through them.</a>"
                    ),
                    "Visit <a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/'>CMS for more info</a>",
                )
            )
        if denial.regulator == Regulator.objects.filter(alt_name="ERISA").get():
            outside_help_details.append(
                (
                    (
                        "Your plan looks to be an ERISA plan which means your employer <i>may</i>"
                        + " have more input into plan decisions. If your are on good terms with HR "
                        + " it could be worth it to ask them for advice."
                    ),
                    "Talk to your employer's HR if you are on good terms with them.",
                )
            )
        denial.insurance_company = insurance_company
        denial.plan_id = plan_id
        denial.claim_id = claim_id
        if denial_type_text is not None:
            denial.denial_type_text = denial_type_text
        denial.denial_type.set(denial_type)
        denial.state = your_state
        denial.save()
        question_forms = []
        for dt in denial.denial_type.all():
            new_form = dt.get_form()
            if new_form is not None:
                new_form = new_form(initial={"medical_reason": dt.appeal_text})
                question_forms.append(new_form)
        combined_form = magic_combined_form(question_forms)
        return NextStepInfo(outside_help_details, combined_form, semi_sekret)


@dataclass
class DenialResponseInfo:
    selected_denial_type: list[DenialTypes]
    all_denial_types: list[DenialTypes]
    denial_id: str
    your_state: str
    procedure: str
    diagnosis: str
    semi_sekret: str


class DenialCreatorHelper:
    regex_denial_processor = ProcessDenialRegex()
    zip_engine = uszipcode.search.SearchEngine()
    # Lazy load to avoid bootstrap problem w/new project
    _codes_denial_processor = None
    _regex_src = None
    _codes_src = None
    _all_denial_types = None

    def codes_denial_processor(cls):
        if cls._codes_denial_processor is None:
            cls._codes_denial_processor = ProcessDenialCodes()
        return cls._codes_denial_processor

    def regex_src(cls):
        if cls._regex_src is None:
            cls._regex_src = DataSource.objects.get(name="regex")
        return cls._regex_src

    def codes_src(cls):
        if cls._codes_src is None:
            cls._codes_src = DataSource.objects.get(name="codes")
        return cls._codes_src

    def all_denial_types(cls):
        if cls._all_denial_types is None:
            cls._all_denial_types = DenialTypes.objects.all()
        return cls._all_denial_types

    @classmethod
    def create_denial(
        cls,
        email,
        denial_text,
        zip,
        pii=False,
        tos=False,
        privacy=False,
        use_external_models=False,
        store_raw_email=False,
    ):
        hashed_email = Denial.get_hashed_email(email)
        # If they ask us to store their raw e-mail we do
        possible_email = None
        validate_email(email)
        if store_raw_email:
            possible_email = email

        denial = Denial.objects.create(
            denial_text=denial_text,
            hashed_email=hashed_email,
            use_external=use_external_models,
            raw_email=possible_email,
        )

        # Try and guess at the denial types
        denial_types = cls.regex_denial_processor().get_denialtype(denial_text)
        denial_type = []
        for dt in denial_types:
            DenialTypesRelation(denial=denial, denial_type=dt, src=cls.regex_src()).save()
            denial_type.append(dt)

        # Guess at the plan type
        plan_type = cls.codes_denial_processor().get_plan_type(denial_text)
        # Infer the state
        your_state = None
        if zip is not None and zip != "":
            try:
                your_state = cls.zip_engine.by_zipcode(zip).state
            except:
                # Default to no state
                your_state = None
        (procedure, diagnosis) = appealGenerator.get_procedure_and_diagnosis(
            denial_text
        )
        return DenialResponseInfo(
            denial_type,
            cls.all_denial_types(),
            denial.denial_id,
            your_state,
            procedure,
            diagnosis,
            denial.semi_sekret,
        )


class AppealsBackendHelper:
    regex_denial_processor = ProcessDenialRegex()

    @classmethod
    def generate_appeals(cls, parameters):
        denial_id = parameters["denial_id"]
        hashed_email = Denial.get_hashed_email(parameters["email"])

        # Get the current info
        denial = Denial.objects.filter(
            denial_id=denial_id, hashed_email=hashed_email
        ).get()

        non_ai_appeals = list(
            map(
                lambda t: t.appeal_text,
                cls.regex_denial_processor.get_appeal_templates(
                    denial.denial_text, denial.diagnosis
                ),
            )
        )

        insurance_company = denial.insurance_company or "insurance company;"
        claim_id = denial.claim_id or "YOURCLAIMIDGOESHERE"
        prefaces = []
        main = []
        footer = []
        medical_reasons = []
        medical_context = ""
        # Extract any medical context AND
        # Apply all of our 'expert system'
        # (aka six regexes in a trench coat hiding behind a database).
        for dt in denial.denial_type.all():
            form = dt.get_form()
            if form is not None:
                parsed = form(parameters)
                if parsed.is_valid():
                    # Check and see if the form has a context method
                    op = getattr(parsed, "medical_context", None)
                    if op is not None and callable(op):
                        try:
                            medical_context += parsed.medical_context()
                        except Exception as e:
                            print(
                                f"Error {e} processing form {form} for medical context"
                            )
                    if (
                        "medical_reason" in parsed.cleaned_data
                        and parsed.cleaned_data["medical_reason"] != ""
                    ):
                        medical_reasons.append(parsed.cleaned_data["medical_reason"])
                        print(f"Med reason {medical_reasons}")
                    # Questionable dynamic template
                    new_prefaces = parsed.preface()
                    for p in new_prefaces:
                        if p not in prefaces:
                            prefaces.append(p)
                    new_main = parsed.main()
                    for m in new_main:
                        if m not in main:
                            main.append(m)
                    new_footer = parsed.footer()
                    for f in new_footer:
                        if f not in footer:
                            footer.append(f)
                else:
                    if dt.appeal_text is not None:
                        main.append(dt.appeal_text)

        # Add the context to the denial
        denial.qa_context = medical_context
        denial.save()
        appeals = itertools.chain(
            non_ai_appeals,
            appealGenerator.make_appeals(
                denial,
                AppealTemplateGenerator(prefaces, main, footer),
                medical_context=medical_context,
                medical_reasons=medical_reasons,
            ),
        )

        def save_appeal(appeal_text):
            # Save all of the proposed appeals, so we can use RL later.
            t = time.time()
            print(f"{t}: Saving {appeal_text}")
            pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial)
            pa.save()
            return appeal_text

        def sub_in_appeals(appeal: str) -> str:
            print(f"Processing {appeal}")
            s = Template(appeal)
            ret = s.safe_substitute(
                {
                    "insurance_company": denial.insurance_company
                    or "{insurance_company}",
                    "[Insurance Company Name]": denial.insurance_company
                    or "{insurance_company}",
                    "[Insert Date]": denial.date or "{date}",
                    "[Reference Number from Denial Letter]": denial.claim_id
                    or "{claim_id}",
                    "[Claim ID]": denial.claim_id or "{claim_id}",
                    "{claim_id}": denial.claim_id or "{claim_id}",
                    "[Diagnosis]": denial.diagnosis or "{diagnosis}",
                    "[Procedure]": denial.procedure or "{procedure}",
                    "{diagnosis}": denial.diagnosis or "{diagnosis}",
                    "{procedure}": denial.procedure or "{procedure}",
                }
            )
            return ret

        filtered_appeals = filter(lambda x: x != None, appeals)
        saved_appeals = map(save_appeal, filtered_appeals)
        subbed_appeals = map(sub_in_appeals, saved_appeals)
        subbed_appeals_json = map(lambda e: json.dumps(e) + "\n", subbed_appeals)
        return StreamingHttpResponse(
            subbed_appeals_json, content_type="application/json"
        )
