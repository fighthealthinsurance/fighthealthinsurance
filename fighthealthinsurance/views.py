from typing import *

import asyncio
import uszipcode
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from io import BytesIO
import cv2
import numpy as np
import hashlib
from fighthealthinsurance.forms import *
from fighthealthinsurance.models import *
from fighthealthinsurance.process_denial import *
from fighthealthinsurance.utils import *

class IndexView(View):
    def get(self, request):
        return render(
            request,
            'index.html')
    

class AboutView(View):
    def get(self, request):
        return render(request, 'about_us.html')
    

class OtherResourcesView(View):
    def get(self, request):
        return render(request, 'other_resources.html')


class ScanView(View):
    def get(self, request):
        return render(
            request,
            'scrub.html',
            context={
                'ocr_result': '',
                'upload_more': True
            })


class PrivacyPolicyView(View):
    def get(self, request):
        return render(
            request,
            'privacy_policy.html',
            context={
                'title': "Privacy Policy"
            })


class TermsOfServiceView(View):
    def get(self, request):
        return render(
            request,
            'tos.html',
            context={
                'title': "Terms of Service",
            })



class OptOutView(View):
    def get(self, request):
        return render(
            request,
            'opt_out.html',
            context={
                'title': "Opt Out",
            })


class ShareDenialView(View):
    def get(self, request):
        return render(
            request,
            'share_denial.html',
            context={
                'title': 'Share Denial'
            }
        )

    def post(self, request):
        form = ShareDenailForm(request.POST)


class ShareAppealView(View):
    def get(self, request):
        return render(
            request,
            'share_appeal.html',
            context={
                'title': 'Share Appeal'
            }
        )

    def post(self, request):
        form = ShareAppealForm(request.POST)
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            email = form.cleaned_data['email']
            hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()

            # Update the denial
            print(f"di {denial_id} he {hashed_email}")
            denial = Denial.objects.filter(
                denial_id = denial_id,
                # Include the hashed e-mail so folks can't brute force denial_id
                hashed_email = hashed_email).get()
            denial.appeal = form.cleaned_data["appeal"]
            denial.save()


    
class RemoveDataView(View):
    def get(self, request):
        return render(
            request,
            'remove_data.html',
            context={
                'title': "Remove My Data",
            })
    
    def post(self, request):
        email = request.POST.get('email')
        # ... handle further logic here.


class RecommendAppeal(View):
    def post(self, request):
        return render(request, '')


states_with_caps = {
    "AR", "CA", "CT", "DE", "DC", "GA",
    "IL", "IA", "KS", "KY", "ME", "MD",
    "MA", "MI", "MS", "MO", "MT", "NV",
    "NH", "NJ", "NM", "NY", "NC", "MP",
    "OK", "OR", "PA", "RI", "TN", "TX",
    "VT", "VI", "WV"}

class FindNextSteps(View):
    def post(self, request):
        form = PostInferedForm(request.POST)
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            email = form.cleaned_data['email']
            hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()

            # Update the denial
            print(f"di {denial_id} he {hashed_email}")
            denial = Denial.objects.filter(
                denial_id = denial_id,
                # Include the hashed e-mail so folks can't brute force denial_id
                hashed_email = hashed_email).get()

            denial.procedure = form.cleaned_data["procedure"]
            denial.diagnosis = form.cleaned_data["diagnosis"]
            denial.save()


            outside_help_details = ""
            state = form.cleaned_data["your_state"]
            if state in states_with_caps:
                outside_help_details += (
                    "<a href='https://www.cms.gov/CCIIO/Resources/Consumer-Assistance-Grants/" +
                    state +
                    "'>" +
                    f"Your state {state} participates in a" +
                    f"Consumer Assistance Program(CAP), and you may be able to get help " +
                    f"through them.</a>")
            if denial.regulator == Regulator.objects.filter(alt_name="ERISA").get():
                outside_help_details = (
                    "Your plan looks to be an ERISA plan which means your employer <i>may</i>" +
                    " have more input into plan decisions. If your are on good terms with HR " +
                    " it could be worth it to ask them for advice.")
            denial.insurance_company = form.cleaned_data["insurance_company"]
            denial.plan_id = form.cleaned_data["plan_id"]
            denial.claim_id = form.cleaned_data["claim_id"]
            if "denial_type_text" in form.cleaned_data:
                denial.denial_type_text = form.cleaned_data["denial_type_text"]
            denial.denial_type.set(form.cleaned_data["denial_type"])
            denial.state = form.cleaned_data["your_state"]
            denial.save()
            advice = []
            question_forms = []
            for dt in denial.denial_type.all():
                new_form = dt.get_form()
                if new_form is not None:
                    new_form = new_form(
                        initial = {
                            'medical_reason': dt.appeal_text})
                    print(new_form)
                    question_forms.append(new_form)
            print(f"Questions {question_forms}")
            denial_ref_form = DenialRefForm(
                initial = {
                    'denial_id': denial.denial_id,
                    "email": form.cleaned_data['email']
                }
            )
            combined = magic_combined_form(question_forms)
            return render(
                request,
                'outside_help.html',
                context={
                    "outside_help_details": outside_help_details,
                    "combined": combined,
                    "denial_form": denial_ref_form,
                })
        else:
            print(f"Invalid form. {form}")
            # If not valid take the user back.
            return render(
                request,
                'categorize.html',
                context = {
                    'post_infered_form': form,
                    'upload_more': True,
                })

class ChooseAppeal(View):

    def post(self, request):
        form = ChooseAppealForm(request.POST):
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            email = form.cleaned_data['email']
            hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
            appeal_text = form.cleaned_data["appeal_text"]

            # Get the current info
            denial = Denial.objects.filter(
                denial_id = denial_id,
                hashed_email = hashed_email).get()
            denial.appeal_text = appeal_text
            denial.save()
            return render(
                request,
                'appeal.html',
                context={
                    "appeal": appeal,
                    "user_email": email,
                    "denial_id": denial_id,
                })


class GenerateAppeal(View):

    def post(self, request):
        def make_open_prompt(denial_text=None, procedure=None, diagnosis=None):
            start = "Write a health insurance appeal for the following denial:"
            if procedure is not None and diagnosis is not None:
                start = f"Write a health insurance appeal for procedure {procedure} with diagnosis {diagnosis} given the following denial:"
            elif procedure is not None:
                start = f"Write a health insurance appeal for procedure {procedure} given the following denial:"

        def make_open_llama_med_prompt(procedure=None, diagnosis=None):
            if procedure is not None:
                if diagnosis is not None:
                    return f"Why is {procedure} medically necessary for {diagnosis}?"
                else:
                    return f"Why is {procedure} is medically necessary?"
            else:
                return None
            
        def make_biogpt_prompt(procedure=None, diagnosis=None):
            if procedure is not None:
                if diagnosis is not None:
                    return f"{procedure} is medically necessary for {diagnosis} because"
                else:
                    return f"{procedure} is medically necessary because"
            else:
                return None

        form = DenialRefForm(request.POST)
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            email = form.cleaned_data['email']
            hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()

            # Get the current info
            denial = Denial.objects.filter(
                denial_id = denial_id,
                hashed_email = hashed_email).get()

            insurance_company = denial.insurance_company or "insurance company;"
            claim_id = denial.claim_id or "YOURCLAIMIDGOESHERE"
            denial_date_info = ""
            if denial.denial_date is not None:
                denial_date_info = "on or about {denial.denial_date}"

            prefaces = []
            main = []
            footer = []
            # Apply all of our 'expert system'
            # (aka six regexes in a trench coat hiding behind a database).
            for dt in denial.denial_type.all():
                form = dt.get_form()
                if form is not None:
                    parsed = form(request.POST)
                    if parsed.is_valid():
                        print(parsed)
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
                        main += [dt.appeal_text]
            # Use LLMS
            bio_gpt_prompt = make_bio_prompt(
                procedure=denial.procedure,
                diagnosis=denial.diagnosis)
            llama_med_prompt = make_open_llama_med_prompt(
                procedure=denial.procedure,
                diagnosis=denial.diagnosis)
            open_prompt = make_open_prompt(
                denial_text=denial.denial_text,
                procedure=denial.procedure,
                diagnosis=denial.diagnosis)

            medical_reasons = []
            appeals = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                # If we have infered a bio gpt prompt call it
                if bio_gpt_prompt is not None:
                    generated_futures = (
                        "med_reason",
                        executor.submit(RemoteBioGPT.infer, bio_gpt_prompt))
                if llama_med_prompt is not None:
                    generated_futures = (
                        "med_reason",
                        executor.submit(RemoteMed.infer, bio_gpt_prompt))
                if open_prompt is not None:
                    generated_futures = (
                        "full",
                        executor.submit(RemoteOpen, open_prompt))
                for (k, f) in generated_futures:
                    text = concurrent.futures.as_completed(f)
                    appeal_text = ""
                    if k == "full":
                        appeal_text = text
                    else:
                        appeal_text = "\n".join(prefaces + main + [text] + footer)
                    appeals += appeal_text
                    pa = ProposedAppeal(
                        appeal_text=appeal_text,
                        for_denial=denial)
                        

            return render(
                request,
                'appeals.html',
                context={
                    "appeals": appeals,
                    "user_email": email,
                    "denial_id": denial_id,
                })


class OCRView(View):
    def __init__(self):
        from doctr.models import ocr_predictor
        self.model = ocr_predictor(
            det_arch='db_resnet50', reco_arch='crnn_vgg16_bn', pretrained=True)

    def get(self, request):
        return render(
            request,
            'server_side_ocr.html')

    def post(self, request):
        from doctr.io import DocumentFile
        txt = ""
        print(request.FILES)
        files = dict(request.FILES.lists())
        uploader = files['uploader']
        doc_txt = self.ocr_with_tesseract(uploader)
        return render(
            request,
            'scrub.html',
            context={
                'ocr_result': doc_txt,
                'upload_more': False
            })


    def ocr_with_kraken(self, uploader):
        from kraken import binarization
        from kraken.lib import models
        from PIL import Image
        images = list(map(
            lambda x: Image(x.read())), uploader)
        return ""

    def ocr_with_tesseract(self, uploader):
        np_files = list(map(
            lambda x: np.frombuffer(x.read(), dtype=np.uint8),
            uploader))
        imgs = list(map(
            lambda npa: cv2.imdecode(npa, cv2.IMREAD_COLOR),
            np_files))
        result = self.model(imgs)
        print(result)
        words = map(
            lambda words: words['value'],
            flat_map(
                lambda lines: lines['words'],
                flat_map(
                    lambda block: block['lines'],
                    flat_map(
                        lambda page: page['blocks'], result.export()['pages']))))
        doc_txt = " ".join(words)
        return doc_txt


class ProcessView(View):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()
        self.codes_denial_processor = ProcessDenialCodes()
        self.regex_src = DataSource.objects.get(name="regex")
        self.codes_src = DataSource.objects.get(name="codes")
        self.zip_engine = uszipcode.search.SearchEngine()


    def post(self, request):
        form = DenialForm(request.POST)
        if form.is_valid():
            # It's not a password per-se but we want password like hashing.
            # but we don't support changing the values.
            email = form.cleaned_data['email']
            hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
            denial_text = form.cleaned_data['denial_text']
            print(denial_text)
            denial = Denial(
                denial_text = denial_text,
                hashed_email = hashed_email)
            denial.save()
            denial_types = self.regex_denial_processor.get_denialtype(denial_text)
            print(f"mmmk {denial_types}")
            denial_type = []
            for dt in denial_types:
                DenialTypesRelation(
                    denial=denial,
                    denial_type=dt,
                    src=self.regex_src).save()
                denial_type.append(dt)
            denial_types = self.codes_denial_processor.get_denialtype(denial_text)
            print(f"mmmk {denial_types}")
            for dt in denial_types:
                DenialTypesRelation(
                    denial=denial,
                    denial_type=dt,
                    src=self.codes_src).save()
                denial_type.append(dt)
            print(f"denial_type {denial_type}")
            plan_type = self.codes_denial_processor.get_plan_type(denial_text)
            print(f"plan {plan_type}")
            state = None
            zip = form.cleaned_data['zip']
            if  zip is not None and zip != "":
                print(f"zip {zip}")
                state = self.zip_engine.by_zipcode(
                    form.cleaned_data['zip']).state
            procedure = self.regex_denial_processor.get_procedure(denial_text)
            diagnosis = self.regex_denial_processor.get_diagnosis(denial_text)
            form = PostInferedForm(
                initial = {
                    'denial_type': denial_type,
                    'denial_id': denial.denial_id,
                    'email': email,
                    'your_state': state,
                    'procedure': procedure,
                    'diagnosis': diagnosis,
                })
            return render(
                request,
                'categorize.html',
                context = {
                    'post_infered_form': form,
                    'upload_more': True,
                })
        else:
            return render(
                request,
                'scrub.html',
                context={
                    'error': form.errors,
                    '': request.POST.get('denial_text', ''),
                })
