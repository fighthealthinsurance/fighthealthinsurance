import asyncstdlib as a
from asgiref.sync import sync_to_async, async_to_sync
import asyncio
import datetime
import json
from dataclasses import dataclass
from string import Template
from typing import AsyncIterator, Awaitable, Any, Optional, Tuple, Iterable
from loguru import logger
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
import ray

from django.core.files import File
from django.core.validators import validate_email
from django.forms import Form
from django.template.loader import render_to_string
from django.db.models import QuerySet
from django.db import connections


import uszipcode
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.fax_utils import flexible_fax_magic
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.models import *
from fighthealthinsurance.forms import questions as question_forms
from fighthealthinsurance.utils import interleave_iterator_for_keep_alive
from fhi_users.models import ProfessionalUser, UserDomain
from .pubmed_tools import PubMedTools
from .utils import check_call, send_fallback_email

appealGenerator = AppealGenerator()


class RemoveDataHelper:
    @classmethod
    def remove_data_for_email(cls, email: str):
        hashed_email: str = Denial.get_hashed_email(email)
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

    def _field_to_dict(self, field_name: str, field: Any) -> dict[str, Any]:
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


class AppealAssemblyHelper:
    async def _convert_input(self, input_path: str) -> Optional[str]:
        if input_path.endswith(".pdf"):
            return input_path
        else:
            await asyncio.sleep(0)
            base_convert_command = [
                "pandoc",
                "--wrap=auto",
                input_path,
                f"-o{input_path}.pdf",
            ]
            try:
                await check_call(base_convert_command)
                return f"{input_path}.pdf"
            # pandoc failures are often character encoding issues
            except Exception as e:
                # try to convert if we've got txt input
                new_input_path = input_path
                if input_path.endswith(".txt"):
                    try:
                        command = [
                            "iconv",
                            "-c",
                            "-t utf8",
                            f"-o{input_path}.u8.txt",
                            input_path,
                        ]
                        await check_call(command)
                        new_input_path = f"{input_path}.u8.txt"
                    except:
                        pass
                # Try a different engine
                for engine in ["lualatex", "xelatex"]:
                    convert_command = base_convert_command
                    convert_command.extend([f"--pdf-engine={engine}"])
                    try:
                        await check_call(base_convert_command)
                        return f"{input_path}.pdf"
                    except:
                        pass
                return None

    async def assemble_single_output(
        self, user_header: str, extra: str, input_paths: list[str], target: str
    ) -> str:
        """Assembles all the inputs into one output. Will need to be chunked."""
        merger = PdfMerger()
        converted_paths = await asyncio.gather(
            *(self._convert_input(path) for path in input_paths)
        )

        for pdf_path in filter(None, converted_paths):
            merger.append(pdf_path)

        merger.write(target)
        merger.close()
        return target

    def create_or_update_appeal(
        self,
        fax_phone: str,
        completed_appeal_text: str,
        company_name: str,
        email: str,
        include_provided_health_history: bool,
        name: str,
        insurance_company: Optional[str] = None,
        denial: Optional[Denial] = None,
        denial_id: Optional[str] = None,
        semi_sekret: Optional[str] = None,
        appeal: Optional[Appeal] = None,
        creating_professional: Optional[ProfessionalUser] = None,
        primary_professional: Optional[ProfessionalUser] = None,
        patient_user: Optional[PatientUser] = None,
        domain: Optional[UserDomain] = None,
        patient_address: Optional[str] = None,
        patient_fax: Optional[str] = None,
        cover_template_path: str = "faxes/cover.html",
        cover_template_string: Optional[str] = None,
        company_phone_number: str = "202-938-3266",
        company_fax_number: str = "415-840-7591",
        pubmed_ids_parsed: Optional[List[str]] = None,
    ) -> Appeal:
        if denial is None:
            if denial_id is not None:
                denial = (
                    Denial.objects.filter(denial_id=denial_id)
                    .filter(
                        hashed_email=Denial.get_hashed_email(email),
                        semi_sekret=semi_sekret,
                    )
                    .get()
                )
        if denial is None:
            raise Exception("No denial ID or denial provided.")
        # Build our cover page
        professional_name: Optional[str] = None
        if primary_professional:
            professional_name = f"{primary_professional.user.first_name} {primary_professional.user.last_name}"
        # Get the reply fax number
        professional_fax_number: Optional[str] = None
        if (
            primary_professional
            and primary_professional.fax_number is not None
            and len(primary_professional.fax_number) > 5
        ):
            professional_fax_number = primary_professional.fax_number
        elif domain and domain.office_fax:
            professional_fax_number = domain.office_fax
        hashed_email = Denial.get_hashed_email(email)
        # Get the current info
        if insurance_company:
            denial.insurance_company = insurance_company
        else:
            insurance_company = denial.insurance_company
        claim_id = denial.claim_id
        health_history: Optional[str] = None
        if include_provided_health_history:
            health_history = denial.health_history
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="alltogether", mode="w+b", delete=False
        ) as t:
            self._assemble_appeal_pdf(
                insurance_company=insurance_company,
                patient_name=name,
                claim_id=claim_id,
                fax_phone=fax_phone,
                completed_appeal_text=completed_appeal_text,
                health_history=health_history,
                pubmed_ids_parsed=pubmed_ids_parsed,
                company_name=company_name,
                cover_template_path=cover_template_path,
                cover_template_string=cover_template_string,
                company_phone_number=company_phone_number,
                company_fax_number=company_fax_number,
                professional_fax_number=professional_fax_number,
                professional_name=professional_name,
                target=t.name,
            )
            t.flush()
            t.seek(0)
            doc_fname = os.path.basename(t.name)
            if appeal is None:
                appeal = Appeal.objects.create(
                    for_denial=denial,
                    appeal_text=completed_appeal_text,
                    hashed_email=hashed_email,
                    document_enc=File(t, name=doc_fname),
                    primary_professional=primary_professional,
                    creating_professional=creating_professional,
                    patient_user=patient_user,
                )
            else:
                appeal.update(
                    for_denial=denial,
                    appeal_text=completed_appeal_text,
                    hashed_email=hashed_email,
                    document_enc=File(t, name=doc_fname),
                    primary_professional=primary_professional,
                    creating_professional=creating_professional,
                    patient_user=patient_user,
                )
            appeal.save()
            return appeal

    # TODO: Asyncify
    def _assemble_appeal_pdf(
        self,
        insurance_company: Optional[str],
        fax_phone: str,
        completed_appeal_text: str,
        company_name: str,
        patient_name: str,
        claim_id: Optional[str],
        health_history: Optional[str] = None,
        patient_address: Optional[str] = None,
        patient_fax: Optional[str] = None,
        cover_template_path: str = "faxes/cover.html",
        cover_template_string: Optional[str] = None,
        company_phone_number: str = "202-938-3266",
        company_fax_number: str = "415-840-7591",
        professional_fax_number: Optional[str] = None,
        professional_name: Optional[str] = None,
        pubmed_ids_parsed: Optional[List[str]] = None,
        target: str = "",
    ):
        if len(target) < 2:
            return
        # Build our cover page
        cover_context = {
            "receiver_name": insurance_company or "",
            "receiver_fax_number": fax_phone,
            "company_name": company_name,
            "company_fax_number": company_fax_number,
            "company_phone_number": company_phone_number,
            "fax_sent_datetime": str(datetime.datetime.now()),
            "provider_fax_number": professional_fax_number,
            "provider_name": professional_name,
            "professional_fax_number": professional_fax_number,
            "patient_name": patient_name,
            "claim_id": claim_id,
        }
        cover_content: str = ""
        # Render the cover content
        if cover_template_string and len(cover_template_string) > 0:
            cover_content = Template(cover_template_string).substitute(cover_context)
            print(
                f"Rendering cover letter from string {cover_template_string} and got {cover_content}"
            )
        else:
            cover_content = render_to_string(
                cover_template_path,
                context=cover_context,
            )
            print(
                f"Rendering cover letter from path {cover_template_path} and got {cover_content}"
            )
        files_for_fax: list[str] = []
        cover_letter_file = tempfile.NamedTemporaryFile(
            suffix=".html", prefix="info_cover", mode="w+t", delete=True
        )
        cover_letter_file.write(cover_content)
        cover_letter_file.flush()
        files_for_fax.append(cover_letter_file.name)

        # Appeal text
        appeal_text_file = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        )
        appeal_text_file.write(completed_appeal_text)
        appeal_text_file.flush()
        files_for_fax.append(appeal_text_file.name)

        # Health history
        # Make the file scope up here so it lasts until after we've got the single output
        health_history_file = None
        if health_history and len(health_history) > 2:
            health_history_file = tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="healthhist", mode="w+t", delete=True
            )
            health_history_file.write("Health History:\n")
            health_history_file.write(health_history)
            files_for_fax.append(health_history_file.name)
            health_history_file.flush()

        # PubMed articles
        if pubmed_ids_parsed is not None and len(pubmed_ids_parsed) > 0:
            pmt = PubMedTools()
            pubmed_docs: list[PubMedArticleSummarized] = pmt.get_articles(
                pubmed_ids_parsed
            )
            pubmed_docs_paths = [
                x for x in map(pmt.article_as_pdf, pubmed_docs) if x is not None
            ]
            files_for_fax.extend(pubmed_docs_paths)
        # TODO: Add more generic DOI handler.

        # Combine and return path
        target = async_to_sync(self.assemble_single_output)(
            input_paths=files_for_fax,
            extra="",
            user_header=str(uuid.uuid4()),
            target=target,
        )
        return target


@dataclass
class FaxHelperResults:
    uuid: str
    hashed_email: str


class SendFaxHelper:
    appeal_assembly_helper = AppealAssemblyHelper()

    @classmethod
    def stage_appeal_as_fax(
        cls,
        appeal: Appeal,
        email: str,
        professional: bool = False,
    ):
        denial = appeal.for_denial
        if denial is None:
            raise Exception("No denial")
        appeal_fax_number = denial.appeal_fax_number
        hashed_email = Denial.get_hashed_email(email)
        appeal_text = appeal.appeal_text
        if not appeal_text:
            raise Exception("No appeal text")
        fts = FaxesToSend.objects.create(
            paid=True,
            pmids=appeal.pubmed_ids_json,
            hashed_email=hashed_email,
            appeal_text=appeal_text,
            email=email,
            denial_id=denial,
            # This should work but idk why it does not
            combined_document_enc=appeal.document_enc,
            destination=appeal_fax_number,
            professional=professional,
        )
        fax_actor_ref.get.do_send_fax.remote(fts.hashed_email, fts.uuid)
        return FaxHelperResults(uuid=fts.uuid, hashed_email=hashed_email)

    @classmethod
    def blocking_dosend_target(cls, email) -> int:
        faxes = FaxesToSend.objects.filter(email=email, sent=False)
        c = 0
        for f in faxes:
            future = fax_actor_ref.get.do_send_fax.remote(f.hashed_email, f.uuid)
            ray.get(future)
            c = c + 1
        return c

    @classmethod
    def blocking_dosend_all(cls, count) -> int:
        faxes = FaxesToSend.objects.filter(sent=False)[0:count]
        c = 0
        for fax in faxes:
            future = fax_actor_ref.get.do_send_fax.remote(fax.hashed_email, fax.uuid)
            ray.get(future)
            c = c + 1
        return c

    @classmethod
    def resend(cls, fax_phone, uuid, hashed_email) -> bool:
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.destination = fax_phone
        f.save()
        future = fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True

    @classmethod
    def remote_send_fax(cls, hashed_email, uuid) -> bool:
        """Send a fax using ray non-blocking"""
        # Mark fax as to be sent just in case ray doesn't follow through
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.should_send = True
        f.paid = True
        f.save()
        future = fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True


class ChooseAppealHelper:
    @classmethod
    def choose_appeal(
        cls, denial_id: str, appeal_text: str, email: str, semi_sekret: str
    ) -> Tuple[
        Optional[str], Optional[str], Optional[QuerySet[PubMedArticleSummarized]]
    ]:
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
            pmqd = PubMedQueryData.objects.filter(denial_id=denial_id)[0]
            if pmqd.articles is not None:
                article_ids = json.loads(pmqd.articles)
                articles = PubMedArticleSummarized.objects.filter(
                    pmid__in=article_ids
                ).distinct()
        except Exception as e:
            logger.debug(f"Error loading pubmed data {e}")
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
                follow_up_document_enc=document, denial=denial, follow_up_id=follow_up
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
        denial_date: Optional[datetime.date] = None,
        semi_sekret: str = "",
        your_state: Optional[str] = None,
        captcha=None,
        denial_type_text: Optional[str] = None,
        plan_source=None,
        employer_name: Optional[str] = None,
        appeal_fax_number: Optional[str] = None,
    ) -> NextStepInfo:
        hashed_email = Denial.get_hashed_email(email)
        # Update the denial
        denial = Denial.objects.filter(
            denial_id=denial_id,
            # Include the hashed e-mail so folks can't brute force denial_id
            hashed_email=hashed_email,
            semi_sekret=semi_sekret,
        ).get()
        if denial_date:
            denial.denial_date = denial_date

        if procedure is not None and len(procedure) < 200:
            denial.procedure = procedure
        if diagnosis is not None and len(diagnosis) < 200:
            denial.diagnosis = diagnosis
        if plan_source is not None:
            denial.plan_source.set(plan_source)
        denial.save()
        # Only set employer name if it's not too long
        if employer_name is not None and len(employer_name) < 300:
            denial.employer_name = employer_name
        else:
            employer_name = None
        if (
            appeal_fax_number is not None
            and len(appeal_fax_number) > 5
            and len(appeal_fax_number) < 30
        ):
            denial.appeal_fax_number = appeal_fax_number

        outside_help_details = []
        state = your_state or denial.your_state

        if state in states_with_caps:
            outside_help_details.append(
                (
                    (
                        "<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/"
                        + state
                        + "'>"
                        + f"Your state {state} participates in a "
                        + f"Consumer Assistance Program (CAP), and you may be able to get help "
                        + f"through them.</a>"
                    ),
                    "Visit CMS.gov for more info<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/'> here</a>",
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
        if denial_type:
            denial.denial_type.set(denial_type)
        if your_state:
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
    uuid: str
    your_state: Optional[str]
    procedure: Optional[str]
    diagnosis: Optional[str]
    employer_name: Optional[str]
    semi_sekret: str
    appeal_fax_number: Optional[str]


class PatientNotificationHelper:
    @classmethod
    def send_signup_invitation(
        cls, email: str, professional_name: Optional[str], practice_number: str
    ):
        subject = "Welcome to Fight Paperwork"
        if professional_name:
            subject += " from {professional_name}"
        return send_fallback_email(
            subject=subject,
            template_name="welcome_patient",
            context={"practice_number": practice_number},
            to_email=email,
        )

    @classmethod
    def notify_of_draft_appeal(
        cls, email: str, professional_name: Optional[str], practice_number: str
    ):
        subject = "Draft Appeal on Fight Paperwork"
        if professional_name:
            subject += " from {professional_name}"
        return send_fallback_email(
            subject=subject,
            template_name="draft_appeal",
            context={"practice_number": practice_number},
            to_email=email,
        )


class ProfessionalNotificationHelper:
    @classmethod
    def send_signup_invitation(
        cls, email: str, professional_name: str, practice_number: str
    ):
        return send_fallback_email(
            subject="You are invited to join your coworker on Fight Paperwork",
            template_name="welcome_professional",
            context={
                "professional_name": professional_name,
                "practice_number": practice_number,
            },
            to_email=email,
        )


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
    async def regex_src(cls):
        if cls._regex_src is None:
            cls._regex_src = await DataSource.objects.aget(name="regex")
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
    def create_or_update_denial(
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
        denial: Optional[Denial] = None,
        creating_professional: Optional[ProfessionalUser] = None,
        primary_professional: Optional[ProfessionalUser] = None,
        patient_user: Optional[PatientUser] = None,
    ):
        hashed_email = Denial.get_hashed_email(email)
        # If they ask us to store their raw e-mail we do
        possible_email = None
        validate_email(email)
        if store_raw_email:
            possible_email = email

        # If we don't have a denial we're making a new one
        if denial is None:
            try:
                denial = Denial.objects.create(
                    denial_text=denial_text,
                    hashed_email=hashed_email,
                    use_external=use_external_models,
                    raw_email=possible_email,
                    health_history=health_history,
                    creating_professional=creating_professional,
                    primary_professional=primary_professional,
                    patient_user=patient_user,
                )
            except Exception as e:
                # This is a temporary hack to drop non-ASCII characters
                denial_text = (
                    denial_text.encode("ascii", errors="ignore")
                    .decode(errors="ignore")
                    .replace("\x00", "")
                )
                denial = Denial.objects.create(
                    denial_text=denial_text,
                    hashed_email=hashed_email,
                    use_external=use_external_models,
                    raw_email=possible_email,
                    health_history=health_history,
                )
        else:
            denial.update(
                denial_text=denial_text,
                hashed_email=hashed_email,
                use_external=use_external_models,
                raw_email=possible_email,
                health_history=health_history,
                creating_professional=creating_professional,
                primary_professional=primary_professional,
                patient_user=patient_user,
            )
            denial.save()

        if possible_email is not None:
            FollowUpSched.objects.create(
                email=possible_email,
                follow_up_date=denial.date + datetime.timedelta(days=15),
                denial_id=denial,
            )
        your_state = None
        if zip is not None and zip != "":
            try:
                your_state = cls.zip_engine.by_zipcode(zip).state
                denial.your_state = your_state
            except:
                # Default to no state
                your_state = None
        # Optionally:
        # Fire off some async requests to the model to extract info.
        # denial_id = denial.denial_id
        # executor.submit(cls.start_background, denial_id)
        # For now we fire this off "later" on a dedicated page with javascript magic.
        r = re.compile(r"Group Name:\s*(.*?)(,|)\s*(INC|CO|LTD|LLC)\s+", re.IGNORECASE)
        g = r.search(denial_text)
        # TODO: Update based on plan document upload if present.
        employer_name = None
        if g is not None:
            employer_name = g.group(1)
            if len(employer_name) < 300:
                denial.employer_name = employer_name
                denial.save()

        denial_id = denial.denial_id
        semi_sekret = denial.semi_sekret
        return cls._update_denial(
            denial=denial, health_history=health_history, plan_documents=plan_documents
        )

    @classmethod
    def start_background(cls, denial_id):
        async_to_sync(cls._start_background(denial_id))

    @classmethod
    async def extract_entity(cls, denial_id: int) -> AsyncIterator[str]:
        # Fax extraction is fire and forget and can run in parallel to the other tass
        asyncio.create_task(cls.extract_set_fax_number(denial_id))
        asyncs: list[Awaitable[Any]] = [
            # Denial type depends on denial and diagnosis
            cls.extract_set_denial_and_diagnosis(denial_id),
            cls.extract_set_denialtype(denial_id),
            asyncio.sleep(0, result=""),
        ]

        async def waitAndReturnNewline(a: Awaitable) -> str:
            try:
                await a
            except:
                logger.opt(exception=True).warning("Failed to process {a}")
            return "\n"

        # I don't live this but in SQLLite we end up with locking issues
        # TODO: Fix this.
        formatted: AsyncIterator[str] = a.map(waitAndReturnNewline, asyncs)
        # StreamignHttpResponse needs a synchronous iterator otherwise it blocks.
        interleaved: AsyncIterator[str] = interleave_iterator_for_keep_alive(formatted)
        return interleaved

    @classmethod
    async def _start_background(cls, denial_id):
        """Run"""
        asyncio.ensure_future(cls.extract_set_fax_number(denial_id))
        await cls.extract_set_denialtype(denial_id)
        await cls.extract_set_denial_and_diagnosis(denial_id)

    @classmethod
    async def extract_set_denialtype(cls, denial_id):
        logger.debug(f"Extracting and setting denial types....")
        # Try and guess at the denial types
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        denial_types = await cls.regex_denial_processor.get_denialtype(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        logger.debug(f"Ok lets rock with {denial_types}")
        for dt in denial_types:
            try:
                await DenialTypesRelation.objects.acreate(
                    denial=denial, denial_type=dt, src=await cls.regex_src()
                )
            except:
                logger.opt(exception=True).debug(f"Failed setting denial type")
        logger.debug(f"Done setting denial types")

    @classmethod
    async def extract_set_fax_number(cls, denial_id):
        # Try and extract the appeal fax number
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        appeal_fax_number = None
        try:
            appeal_fax_number = await appealGenerator.get_fax_number(
                denial_text=denial.denial_text, use_external=denial.use_external_models
            )
        except:
            pass
        # Slight gaurd against halucinations
        if appeal_fax_number is not None:
            # TODO: More flexible regex matching
            if (
                appeal_fax_number not in denial.denial_text
                and "Fax" not in denial.denial_text
            ) or len(appeal_fax_number) > 30:
                appeal_fax_number = None
        if appeal_fax_number is not None:
            denial = await Denial.objects.filter(denial_id=denial_id).aget()
            denial.fax_number = appeal_fax_number
            await denial.asave()

    @classmethod
    async def extract_set_denial_and_diagnosis(cls, denial_id: int):
        denial = await Denial.objects.filter(denial_id=denial_id).aget()
        try:
            (procedure, diagnosis) = await appealGenerator.get_procedure_and_diagnosis(
                denial_text=denial.denial_text, use_external=denial.use_external
            )
            if procedure is not None and len(procedure) < 200:
                denial.procedure = procedure
            if diagnosis is not None and len(diagnosis) < 200:
                denial.diagnosis = diagnosis
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to extract procedure and diagnosis for denial {denial_id}"
            )
        finally:
            denial.extract_procedure_diagnosis_finished = True
            await denial.asave()

    @classmethod
    def update_denial(
        cls,
        email,
        denial_id,
        semi_sekret,
        health_history=None,
        plan_documents=None,
    ):
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.filter(
            hashed_email=hashed_email, denial_id=denial_id, semi_sekret=semi_sekret
        ).get()
        return cls._update_denial(
            denial, health_history=health_history, plan_documents=plan_documents
        )

    @classmethod
    def _update_denial(cls, denial, health_history=None, plan_documents=None):
        if plan_documents is not None:
            for plan_document in plan_documents:
                pd = PlanDocuments.objects.create(
                    plan_document_enc=plan_document, denial=denial
                )
                pd.save()

        if health_history is not None:
            denial.health_history = health_history
            denial.save()
        # Return the current the state
        return DenialResponseInfo(
            selected_denial_type=denial.denial_type.all(),
            all_denial_types=cls.all_denial_types(),
            uuid=denial.uuid,
            denial_id=denial.denial_id,
            your_state=denial.your_state,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
            employer_name=denial.employer_name,
            semi_sekret=denial.semi_sekret,
            appeal_fax_number=denial.appeal_fax_number,
        )


class AppealsBackendHelper:
    regex_denial_processor = ProcessDenialRegex()

    @classmethod
    async def generate_appeals(cls, parameters):
        denial_id = parameters["denial_id"]
        email = parameters["email"]
        semi_sekret = parameters["semi_sekret"]
        hashed_email = Denial.get_hashed_email(email)

        if denial_id is None:
            raise Exception("Missing denial id")
        if semi_sekret is None:
            raise Exception("Missing sekret")

        # Get the current info
        await asyncio.sleep(0)
        denial = await Denial.objects.filter(
            denial_id=denial_id, semi_sekret=semi_sekret, hashed_email=hashed_email
        ).aget()

        non_ai_appeals: List[str] = list(
            map(
                lambda t: t.appeal_text,
                await cls.regex_denial_processor.get_appeal_templates(
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
        async for dt in denial.denial_type.all():
            form = await sync_to_async(dt.get_form)()
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
                            logger.debug(
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
                            logger.debug(
                                f"Error {e} processing form {form} for plan context"
                            )
                    # See if we have a provided medical reason
                    if (
                        "medical_reason" in parsed.cleaned_data
                        and parsed.cleaned_data["medical_reason"] != ""
                    ):
                        medical_reasons.add(parsed.cleaned_data["medical_reason"])
                        logger.debug(f"Med reason {medical_reasons}")
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
        await denial.asave()
        appeals: Iterable[str] = await sync_to_async(appealGenerator.make_appeals)(
            denial,
            AppealTemplateGenerator(prefaces, main, footer),
            medical_reasons=medical_reasons,
            non_ai_appeals=non_ai_appeals,
        )

        async def save_appeal(appeal_text: str) -> str:
            # Save all of the proposed appeals, so we can use RL later.
            t = time.time()
            logger.debug(f"{t}: Saving {appeal_text}")
            await asyncio.sleep(0)
            # YOLO on saving appeals, sqllite gets sad.
            try:
                pa = ProposedAppeal(appeal_text=appeal_text, for_denial=denial)
                await pa.asave()
            except Exception as e:
                logger.opt(exception=True).warning(
                    "Failed to save proposed appeal: {e}"
                )
                pass
            return appeal_text

        async def sub_in_appeals(appeal: str) -> str:
            await asyncio.sleep(0)
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

        async def format_response(response: str) -> str:
            return json.dumps(response) + "\n"

        filtered_appeals: Iterator[str] = filter(lambda x: x != None, appeals)

        # We convert to async here.
        saved_appeals: AsyncIterator[str] = a.map(save_appeal, filtered_appeals)
        subbed_appeals: AsyncIterator[str] = a.map(sub_in_appeals, saved_appeals)
        subbed_appeals_json: AsyncIterator[str] = a.map(format_response, subbed_appeals)
        # StreamignHttpResponse needs a synchronous iterator otherwise it blocks.
        interleaved: AsyncIterator[str] = interleave_iterator_for_keep_alive(
            subbed_appeals_json
        )
        return interleaved
