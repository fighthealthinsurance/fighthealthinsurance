import datetime
import json
from dataclasses import dataclass
from string import Template
from typing import Any, Optional, Tuple, Iterable

from django.core.files import File
from django.core.validators import validate_email
from django.forms import Form
from django.http import StreamingHttpResponse
from django.template.loader import render_to_string

import uszipcode
from fighthealthinsurance.core_forms import *
from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.fax_utils import flexible_fax_magic
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.models import *
from fighthealthinsurance.question_forms import *
from fighthealthinsurance.utils import pubmed_fetcher

appealGenerator = AppealGenerator()


class RemoveDataHelper:
    @classmethod
    def remove_data_for_email(cls, email: str):
        hashed_email = Denial.get_hashed_email(email)
        Denial.objects.filter(hashed_email=hashed_email).delete()
        FollowUpSched.objects.filter(email=email).delete()
        FollowUp.objects.filter(hashed_email=hashed_email).delete()
        FaxesToSend.objects.filter(hashed_email=hashed_email).delete()


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
        return NextStepInfoSerializable(
            outside_help_details=self.outside_help_details,
            combined_form=list(
                map(
                    lambda xy: self._field_to_dict(*xy),
                    self.combined_form.fields.items(),
                )
            ),
            semi_sekret=self.semi_sekret,
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
class FaxHelperResults:
    uuid: str
    hashed_email: str


class SendFaxHelper:
    @classmethod
    def stage_appeal_fax(
        cls,
        denial_id: str,
        fax_phone: str,
        email: str,
        semi_sekret: str,
        completed_appeal_text: str,
        include_provided_health_history: bool,
        name: str,
        insurance_company: str,
        pubmed_articles_to_include: str = "",
    ):
        hashed_email = Denial.get_hashed_email(email)
        # Get the current info
        denial = Denial.objects.filter(
            denial_id=denial_id, hashed_email=hashed_email
        ).get()
        denial.insurance_company = insurance_company
        files_for_fax: list[str] = []
        # Cover page
        cover_context = {
            "receiver_name": denial.insurance_company or "",
            "receiver_fax_number": fax_phone,
            "company_name": "Fight Health Insurance -- A service of Totally Legit Co.",
            "company_fax_number": "415-840-7591",
            "fax_sent_datetime": str(datetime.datetime.now()),
        }
        html_content = render_to_string(
            "faxes/cover.html",
            context=cover_context,
        )
        with tempfile.NamedTemporaryFile(
            suffix=".html", prefix="info_cover", mode="w+t", delete=False
        ) as f:
            f.write(html_content)
            files_for_fax.append(f.name)
        # Actual appeal
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=False
        ) as f:
            f.write(completed_appeal_text)
            f.flush()
            files_for_fax.append(f.name)
        # Health history (if enabled)
        if (
            include_provided_health_history
            and denial.health_history is not None
            and len(denial.health_history) > 2
        ):
            with tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="healthhist", mode="w+t", delete=False
            ) as f:
                f.write("Health History:\n")
                f.write(denial.health_history)
                files_for_fax.append(f.name)
                f.flush()

        pubmed_ids_parsed = pubmed_articles_to_include.split(",")
        pubmed_docs: list[PubMedArticleSummarized] = []
        # Try and include the pubmed ids that we have but also fetch if not present
        for pmid in pubmed_ids_parsed:
            if pmid is None or pmid == "":
                continue
            try:
                pubmed_docs.append(PubMedArticleSummarized.objects.get(pmid == pmid))
            except:
                try:
                    fetched = pubmed_fetcher.article_by_pmid(pmid)
                    article = PubMedArticleSummarized.objects.create(
                        pmid=pmid,
                        doi=fetched.doi,
                        title=fetched.title,
                        abstract=fetched.abstract,
                        text=fetched.content.text,
                    )
                    pubmed_docs.append(article)
                except:
                    print(f"Skipping {pmid}")

        for pubmed_doc in pubmed_docs:
            with tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="pubmeddoc", mode="w+t", delete=False
            ) as f:
                if pubmed_doc.title is not None:
                    f.write(pubmed_doc.title + "\n")
                if pubmed_doc.abstract is not None:
                    f.write("Abstract:\n")
                    f.write(pubmed_doc.abstract)
                if pubmed_doc.text is not None:
                    f.write("Text:\n")
                    f.write(pubmed_doc.text)
                files_for_fax.append(f.name)
                f.flush()

        doc_path = flexible_fax_magic.assemble_single_output(
            input_paths=files_for_fax, extra="", user_header=str(uuid.uuid4())
        )
        doc_fname = os.path.basename(doc_path)
        doc = open(doc_path, "rb")
        pmids = ""
        try:
            pmids = (
                PubMedQueryData.objects.filter(denial_id=denial_id).get().articles or ""
            )
        except:
            pass
        fts = FaxesToSend.objects.create(
            hashed_email=hashed_email,
            paid=False,
            pmids=pmids,
            appeal_text=completed_appeal_text,
            health_history=denial.health_history,
            email=email,
            denial_id=denial,
            name=name,
            # This should work but idk why it does not
            combined_document=File(file=doc, name=doc_fname),
            destination=fax_phone,
        )
        return FaxHelperResults(uuid=fts.uuid, hashed_email=hashed_email)

    @classmethod
    def remote_send_fax(cls, hashed_email, uuid):
        """Send a fax using ray non-blocking"""
        # Mark fax as to be sent just in case ray doesn't follow through
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.should_send = True
        f.save()
        future = fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)


class ChooseAppealHelper:
    @classmethod
    def choose_appeal(
        cls, denial_id: str, appeal_text: str, email: str, semi_sekret: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        hashed_email = Denial.get_hashed_email(email)
        # Get the current info
        denial = Denial.objects.filter(
            denial_id=denial_id, hashed_email=hashed_email, semi_sekret=semi_sekret
        ).get()
        denial.appeal_text = appeal_text
        denial.save()
        pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial, chosen=True)
        pa.save()
        articles = None
        try:
            pmqd = PubMedQueryData.objects.filter(denial_id=denial_id).get()
            if pmqd.articles is not None:
                articles = ",".join(pmqd.articles.split(",")[0:2])
        except:
            pass
        return (denial.appeal_fax_number, denial.insurance_company, articles)


@dataclass
class NextStepInfoSerializable:
    outside_help_details: list[Tuple[str, str]]
    combined_form: list[Any]
    semi_sekret: str


class FollowUpHelper:
    @classmethod
    def fetch_denial(
        cls, uuid: str, follow_up_semi_sekret: str, hashed_email: str, **kwargs
    ):
        denial = Denial.objects.filter(
            uuid=uuid, follow_up_semi_sekret=follow_up_semi_sekret
        ).get()
        if denial is None:
            raise Exception(
                f"Failed to find denial for {uuid} & {follow_up_semi_sekret}"
            )
        return denial

    @classmethod
    def store_follow_up_result(
        cls,
        uuid: str,
        follow_up_semi_sekret: str,
        hashed_email: str,
        user_comments: str,
        appeal_result: str,
        follow_up_again: bool,
        medicare_someone_to_help: bool = False,
        email: Optional[str] = None,
        quote: Optional[str] = None,
        name_for_quote: Optional[str] = None,
        use_quote: bool = False,
        followup_documents=[],
    ):
        denial = cls.fetch_denial(
            uuid=uuid,
            follow_up_semi_sekret=follow_up_semi_sekret,
            hashed_email=hashed_email,
        )
        # Store the follow up response returns nothing but may raise
        denial_id = denial.denial_id
        follow_up = FollowUp.objects.create(
            hashed_email=hashed_email,
            denial_id=denial,
            more_follow_up_requested=follow_up_again,
            follow_up_medicare_someone_to_help=medicare_someone_to_help,
            use_quote=use_quote,
            email=email,
            name_for_quote=name_for_quote,
            quote=quote,
        )
        # If they asked for additional follow up add a new schedule
        if follow_up_again:
            FollowUpSched.objects.create(
                email=denial.raw_email,
                denial_id=denial,
                follow_up_date=denial.date + datetime.timedelta(days=15),
            )
        for document in followup_documents:
            fd = FollowUpDocuments.objects.create(
                follow_up_document=document, denial=denial, follow_up_id=follow_up
            )
            fd.save()
        denial.appeal_result = appeal_result
        denial.save()


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
        employer_name=None,
        appeal_fax_number=None,
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
        # Only set employer name if it's not too long
        if employer_name is not None and len(employer_name) < 200:
            denial.employer_name = employer_name
        if (
            appeal_fax_number is not None
            and len(appeal_fax_number) > 5
            and len(appeal_fax_number) < 30
        ):
            denial.appeal_fax_number = appeal_fax_number

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
    denial_id: int
    your_state: Optional[str]
    procedure: Optional[str]
    diagnosis: Optional[str]
    employer_name: Optional[str]
    semi_sekret: str
    appeal_fax_number: Optional[str]


class DenialCreatorHelper:
    regex_denial_processor = ProcessDenialRegex()
    zip_engine = uszipcode.search.SearchEngine()
    # Lazy load to avoid bootstrap problem w/new project
    _codes_denial_processor = None
    _regex_src = None
    _codes_src = None
    _all_denial_types = None

    @classmethod
    def codes_denial_processor(cls):
        if cls._codes_denial_processor is None:
            cls._codes_denial_processor = ProcessDenialCodes()
        return cls._codes_denial_processor

    @classmethod
    def regex_src(cls):
        if cls._regex_src is None:
            cls._regex_src = DataSource.objects.get(name="regex")
        return cls._regex_src

    @classmethod
    def codes_src(cls):
        if cls._codes_src is None:
            cls._codes_src = DataSource.objects.get(name="codes")
        return cls._codes_src

    @classmethod
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
        health_history=None,
        pii=False,
        tos=False,
        privacy=False,
        use_external_models=False,
        store_raw_email=False,
        plan_documents=None,
    ):
        hashed_email = Denial.get_hashed_email(email)
        # If they ask us to store their raw e-mail we do
        possible_email = None
        validate_email(email)
        if store_raw_email:
            possible_email = email

        appeal_fax_number = None
        try:
            appeal_fax_number = appealGenerator.get_fax_number(
                denial_text=denial_text, use_external=use_external_models
            )
            if appeal_fax_number not in denial_text:
                appeal_fax_number = None
        except:
            pass

        denial = Denial.objects.create(
            denial_text=denial_text,
            hashed_email=hashed_email,
            use_external=use_external_models,
            raw_email=possible_email,
            health_history=health_history,
            appeal_fax_number=appeal_fax_number,
        )
        if possible_email is not None:
            FollowUpSched.objects.create(
                email=possible_email,
                follow_up_date=denial.date + datetime.timedelta(days=15),
                denial_id=denial,
            )

        for plan_document in plan_documents:
            pd = PlanDocuments.objects.create(
                plan_document=plan_document, denial=denial
            )
            pd.save()

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
            denial_text=denial_text, use_external=denial.use_external
        )
        r = re.compile(r"Group Name:\s*(.*?)(,|)\s*(INC|CO|LTD)")
        g = r.search(denial_text)
        employer_name = None
        if g is not None:
            employer_name = g.group(1)
            denial.employer_name = employer_name
            denial.save()

        # Try and guess at the denial types
        denial_types = cls.regex_denial_processor.get_denialtype(
            denial_text=denial_text, procedure=procedure, diagnosis=diagnosis
        )
        denial_type = []
        for dt in denial_types:
            DenialTypesRelation(
                denial=denial, denial_type=dt, src=cls.regex_src()
            ).save()
            denial_type.append(dt)
        return DenialResponseInfo(
            selected_denial_type=denial_type,
            all_denial_types=cls.all_denial_types(),
            denial_id=denial.denial_id,
            your_state=your_state,
            procedure=procedure,
            diagnosis=diagnosis,
            employer_name=employer_name,
            semi_sekret=denial.semi_sekret,
            appeal_fax_number=appeal_fax_number,
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

        non_ai_appeals: List[str] = list(
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
        medical_reasons = set()
        medical_context = set()
        plan_context = set()
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
                            mc = parsed.medical_context()
                            if mc is not None:
                                medical_context.add(mc)
                        except Exception as e:
                            print(
                                f"Error {e} processing form {form} for medical context"
                            )
                    # Check for plan context
                    op = getattr(parsed, "plan_context", None)
                    if op is not None and callable(op):
                        try:
                            pc = parsed.plan_context(denial)
                            if pc is not None:
                                plan_context.add(pc)
                        except Exception as e:
                            print(f"Error {e} processing form {form} for plan context")
                    # See if we have a provided medical reason
                    if (
                        "medical_reason" in parsed.cleaned_data
                        and parsed.cleaned_data["medical_reason"] != ""
                    ):
                        medical_reasons.add(parsed.cleaned_data["medical_reason"])
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
        if medical_context is not None:
            denial.qa_context = " ".join(medical_context)
        if plan_context is not None:
            denial.plan_context = " ".join(set(plan_context))
        denial.save()
        appeals: Iterable[str] = appealGenerator.make_appeals(
            denial,
            AppealTemplateGenerator(prefaces, main, footer),
            medical_reasons=medical_reasons,
            non_ai_appeals=non_ai_appeals,
        )

        def save_appeal(appeal_text):
            # Save all of the proposed appeals, so we can use RL later.
            t = time.time()
            print(f"{t}: Saving {appeal_text}")
            pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial)
            pa.save()
            return appeal_text

        def sub_in_appeals(appeal: str) -> str:
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
