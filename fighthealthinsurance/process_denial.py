import itertools
import concurrent
import csv
import os
import re
from abc import ABC, abstractmethod
from functools import cache, lru_cache
from typing import Tuple, List, Optional
import time

import icd10
import requests
from fighthealthinsurance.models import (
    AppealTemplates,
    DenialTypes,
    Diagnosis,
    PlanType,
    Procedures,
    Regulator,
)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)


# Process all of our "expert system" rules.


class DenialBase(ABC):
    @abstractmethod
    def get_denialtype(self, text):
        pass

    @abstractmethod
    def get_regulator(self, text):
        pass

    @abstractmethod
    def get_plan_type(self, text):
        pass


class ProcessDenialCodes(DenialBase):
    """Process the denial type based on the procedure codes."""

    def __init__(self):
        self.preventive_denial = DenialTypes.objects.filter(
            name="Preventive Care"
        ).get()
        # These will match many things which are not ICD10 codes or CPT codes but
        # then the lookup will hopefully fail.
        self.icd10_re = re.compile(
            "[\\(\\s:\\.\,]+([A-TV-Z][0-9][0-9AB]\\.?[0-9A-TV-Z]{0,4})[\\s:\\.\\)\,]",
            re.M | re.UNICODE,
        )
        self.cpt_code_re = re.compile(
            "[\\(\\s:\,]+(\\d{4,4}[A-Z0-9])[\\s:\\.\\)\,]", re.M | re.UNICODE
        )
        self.preventive_regex = re.compile(
            "(exposure to human immunodeficiency virus|preventive|high risk homosexual)",
            re.M | re.UNICODE | re.IGNORECASE,
        )
        try:
            with open("./data/preventitivecodes.csv") as f:
                rows = csv.reader(f)
                self.preventive_codes = {k: v for k, v in rows}
        except Exception:
            self.preventive_codes = {}
        try:
            with open("./data/preventive_diagnosis.csv") as f:
                rows = csv.reader(f)
                self.preventive_diagnosis = {k: v for k, v in rows}
        except:
            self.preventive_diagnosis = {}

    def get_denialtype(self, text):
        """Get the denial type. For now short circuit logic."""
        icd_codes = self.icd10_re.finditer(text)
        for i in icd_codes:
            diag = i.group(1)
            tag = icd10.find(diag)
            if tag is not None:
                print(tag)
                print(tag.block_description)
                if re.search("preventive", tag.block_description, re.IGNORECASE):
                    return [self.preventive_denial]
                if diag in self.preventive_diagnosis:
                    return [self.preventive_denial]
        cpt_codes = self.cpt_code_re.finditer(text)
        for i in cpt_codes:
            code = i.group(1)
            print(code)
            if code in self.preventive_codes:
                return [self.preventive_denial]
        return []

    def get_regulator(self, text):
        return []

    def get_plan_type(self, text):
        return []


class RemoteModel(object):
    """
    For models which produce a "full" appeal tag them with "full"
    For medical reason (e.g. ones where we hybrid with our "expert system"
    ) tag them with "med_reason".
    """

    def infer(self, prompt, t):
        pass


class PalmAPI(RemoteModel):
    def bad_result(self, result) -> bool:
        return result is not None

    @cache
    def infer(self, prompt: str, t: str) -> List[Tuple[str, str]]:
        result = self._infer(prompt)
        if self.bad_result(result):
            result = self._infer(prompt)
        if result is not None:
            return [(t, result)]
        else:
            return []

    def get_procedure_and_diagnosis(self, prompt):
        return (None, None)

    def _infer(self, prompt) -> Optional[str]:
        API_KEY = os.getenv("PALM_KEY")
        if API_KEY is None:
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={API_KEY}"
        print(f"Looking up model {self} w/prompt {prompt}")
        try:
            import requests

            s = requests.Session()
            result = s.post(url, json={"contents": [{"parts": [{"text": prompt}]}]})
            json_result = result.json()
            candidates = json_result["candidates"]
            return candidates[0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Error exception: {e} from {self} w/PaLM")
            return None


class RemoteOpenLike(RemoteModel):
    def __init__(self, api_base, token, model, system_message, procedure_message=None):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_message = system_message
        self.procedure_message = procedure_message
        self.max_len = 4096 * 2
        self.procedure_response_regex = re.compile(
            r"\s*procedure\s*:?\s*", re.IGNORECASE
        )
        self.diagnosis_response_regex = re.compile(
            r"\s*diagnosis\s*:?\s*", re.IGNORECASE
        )

    def bad_result(self, result: Optional[str]) -> bool:
        bad = "Therefore, the Health Plans denial should be overturned."
        if result is None:
            return True
        if bad in result:
            return True
        if len(result.strip(" ")) < 5:
            return True
        return False

    @cache
    def infer(self, prompt: str, t: str) -> List[Tuple[str, str]]:
        result = self._infer(self.system_message, prompt)
        if self.bad_result(result):
            result = self._infer(self.system_message, prompt)
        if self.bad_result(result):
            result = None
        if result is not None:
            return [(t, result)]
        else:
            return []

    def _clean_procedure_response(self, response):
        return self.procedure_response_regex.sub("", response)

    def _clean_diagnosis_response(self, response):
        if "Not available in" in response or "Not provided" in response:
            return None
        return self.diagnosis_response_regex.sub("", response)

    @cache
    def get_procedure_and_diagnosis(
        self, prompt: str
    ) -> tuple[Optional[str], Optional[str]]:
        if self.procedure_message is None:
            print(f"No procedure message for {self.model} skipping")
            return (None, None)
        model_response = self._infer(self.procedure_message, prompt)
        if model_response is None or "Diagnosis" not in model_response:
            print("Retrying query.")
            model_response = self._infer(self.procedure_message, prompt)
        if model_response is not None:
            responses: list[str] = model_response.split("\n")
            if len(responses) == 2:
                r = (
                    self._clean_procedure_response(responses[0]),
                    self._clean_diagnosis_response(responses[1]),
                )
                return r
            elif len(responses) == 1:
                r = (self._clean_procedure_response(responses[0]), None)
                return r
            elif len(responses) > 2:
                procedure = None
                diagnosis = None
                for ra in responses:
                    if "Diagnosis" in ra:
                        diagnosis = self._clean_diagnosis_response(ra)
                    if "Procedure" in ra:
                        procedure = self._clean_procedure_response(ra)
                return (procedure, diagnosis)
            else:
                print(
                    f"Non-understood response {model_response} for procedure/diagnsosis."
                )
        else:
            print(f"No model response for {self.model}")
        return (None, None)

    def _infer(self, system_prompt, prompt) -> Optional[str]:
        print(f"Looking up model {self.model} using {self.api_base}")
        if self.token is None:
            print(f"Error no Token provided for {self.model}.")
        if prompt is None:
            print(f"Error: must supply a prompt.")
            return None
        url = f"{self.api_base}/chat/completions"
        try:
            import requests

            s = requests.Session()
            # Combine the message, Mistral's VLLM container does not like the system role anymore?
            # despite it still being fine-tuned with the system role.
            combined_content = (
                f"<<SYS>>{system_prompt}<</SYS>>{prompt[0 : self.max_len]}"
            )
            result = s.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": combined_content,
                        },
                    ],
                    "temperature": 0.7,
                },
            )
            print(f"Got {result} on {self.api_base} {self}")
            json_result = result.json()
        except Exception as e:
            print(f"Error {e} calling {self.api_base}")
            return None
        try:
            r = json_result["choices"][0]["message"]["content"]
            print(f"Got {r} from {self.model} w/ {self.api_base} {self}")
            return r
        except Exception as e:
            print(
                f"Error {e} processing {json_result} from {self.api_base} w/ url {url} --  {self}"
            )
            return None


class RemoteFullOpenLike(RemoteOpenLike):
    def __init__(self, api_base, token, model):
        system_message = """You possess extensive medical expertise and enjoy crafting appeals for health insurance denials as a personal interest. As a patient, not a doctor, you advocate for yourself. Your writing style is direct, akin to patio11 or a bureaucrat, and maintains a professional tone without expressing frustration towards insurance companies. You may consider emphasizing the unique and potentially essential nature of the medical intervention, using "YourNameMagic" as your name, "SCSID" for the subscriber ID, and "GPID" as the group ID. Make sure to write in the form of a letter. You can be verbose. Start your response with Dear [Insurance Company];"""
        procedure_message = """You have an in-depth understanding of insurance and have gained extensive experience working in a medical office. Your expertise lies in deciphering health insurance denial letters to identify the requested procedure and, if available, the associated diagnosis. Please provide a concise response with the procedure on one line and the diagnosis on the next line."""
        return super().__init__(
            api_base, token, model, system_message, procedure_message
        )

    def model_type(self) -> str:
        return "full"


class RemoteHealthInsurance(RemoteFullOpenLike):
    def __init__(self):
        self.port = os.getenv("HEALTH_BACKEND_PORT", "80")
        self.host = os.getenv("HEALTH_BACKEND_HOST")
        self.url = None
        if self.port is not None and self.host is not None:
            self.url = f"http://{self.host}:{self.port}/v1"
        else:
            print(f"Error setting up remote health {self.host}:{self.port}")
        self.model = os.getenv(
            "HEALTH_BACKEND_MODEL", "TotallyLegitCo/fighthealthinsurance_model_v0.3"
        )
        super().__init__(self.url, token="", model=self.model)


class RemoteTogetherAI(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.together.xyz"
        token = os.getenv("TOGETHER_KEY")
        model = "mistralai/Mixtral-8x7B-Instruct-v0.1"
        super().__init__(api_base, token, model)


class RemotePerplexityInstruct(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        super().__init__(api_base, token, model)


class RemotePerplexityMedReason(RemoteOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        medical_reason_message = "You are a doctor answering a friends question as to why a procedure is medically necessary."
        super().__init__(api_base, token, model, medical_reason_message)


class RemoteOpen(RemoteFullOpenLike):
    """Use RemoteOpen for denial magic calls a service"""

    def __init__(self):
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        model = "HuggingFaceH4/zephyr-7b-beta"
        super().__init__(api_base, token, model)


class RemoteOpenInst(RemoteFullOpenLike):
    """Use RemoteOpen for denial magic calls a service"""

    def __init__(self):
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        model = "mistralai/Mistral-7B-Instruct-v0.1"
        super().__init__(api_base, token, model)


class ProcessDenialRegex(DenialBase):
    """Process the denial type based on the regexes stored in the database."""

    def __init__(self):
        self.planTypes = PlanType.objects.all()
        self.regulators = Regulator.objects.all()
        self.denialTypes = DenialTypes.objects.all()
        self.diagnosis = Diagnosis.objects.all()
        self.procedures = Procedures.objects.all()
        self.templates = AppealTemplates.objects.all()

    def get_procedure(self, text):
        print(f"Getting procedure types for {text}")
        procedure = None
        for d in self.procedures:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("procedure")[0]
        return None

    def get_diagnosis(self, text):
        print(f"Getting procedure types for {text}")
        procedure = None
        for d in self.diagnosis:
            print(f"Exploring {d} w/ {d.regex}")
            if d.regex.pattern != "":
                s = d.regex.search(text)
                if s is not None:
                    print("positive regex match")
                    return s.groups("diagnosis")[0]
        return None

    def get_procedure_and_diagnosis(self, text):
        print("Getting procedure and diagnosis")
        return (self.get_procedure(text), self.get_diagnosis(text))

    def get_denialtype(self, text):
        print(f"Getting denial types for {text}")
        denials = []
        for d in self.denialTypes:
            print(f"Exploring {d} w/ {d.regex} & {d.negative_regex}")
            if d.regex.pattern != "" and d.regex.search(text) is not None:
                print("positive regex match")
                if (
                    d.negative_regex.pattern == ""
                    or d.negative_regex.search(text) is None
                ):
                    print("no negative regex match!")
                    denials.append(d)
        print(f"Collected: {denials}")
        return denials

    def get_regulator(self, text):
        regulators = []
        for r in self.regulators:
            if (
                r.regex.search(text) is not None
                and r.negative_regex.search(text) is None
            ):
                regulators.append(r)
        return regulators

    def get_plan_type(self, text):
        plans = []
        for p in self.planTypes:
            if p.regex.pattern != "" and p.regex.search(text) is not None:
                print(f"positive regex match for plan {p}")
                if p.negative_regex != "" or p.negative_regex.search(text) is None:
                    plans.append(p)
            else:
                print(f"no match {p}")
        return plans

    def get_appeal_templates(self, text):
        templates = []
        for t in self.templates:
            if t.regex.pattern != "" and t.regex.search(text) is not None:
                templates.append(t)
                print("yay match")
            else:
                print(f"no match on {t.regex.pattern}")
        return templates


class AppealTemplateGenerator(object):
    def __init__(self, prefaces, main, footer):
        self.prefaces = prefaces
        self.main = main
        self.footer = footer
        self.combined = str("\n".join(prefaces + main + footer))
        print(f"Template {self.combined} on {self} ")

    def generate_static(self):
        print(f"Generating static on {self} sending back {self.combined}")
        if "{medical_reason}" not in self.combined and self.combined != "":
            return self.combined
        else:
            return None

    def generate(self, medical_reason):
        result = self.combined.replace("{medical_reason}", medical_reason)
        if result != "":
            return result
        else:
            return None


class AppealGenerator(object):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()
        self.perplexity = RemotePerplexityInstruct()
        self.perplexity_med = RemotePerplexityMedReason()
        self.anyscale = RemoteOpen()
        self.anyscale2 = RemoteOpenInst()
        self.remotehealth = RemoteHealthInsurance()
        self.together = RemoteTogetherAI()
        self.palm = PalmAPI()

    def get_procedure_and_diagnosis(self, denial_text=None):
        prompt = self.make_open_procedure_prompt(denial_text)
        models_to_try = [
            self.regex_denial_processor,
#            self.perplexity,
#            self.together,
#            self.anyscale,
            self.remotehealth,
#            self.palm,
        ]
        procedure = None
        diagnosis = None
        for model in models_to_try:
            print(f"Exploring model {model}")
            procedure_diagnosis = model.get_procedure_and_diagnosis(denial_text)
            if procedure_diagnosis is not None:
                if len(procedure_diagnosis) > 1:
                    procedure = procedure or procedure_diagnosis[0]
                    diagnosis = diagnosis or procedure_diagnosis[1]
                else:
                    print(
                        f"Unexpected procedure diagnosis len on {procedure_diagnosis}"
                    )
                if procedure is not None and diagnosis is not None:
                    print(f"Return with procedure {procedure} and {diagnosis}")
                    return (procedure, diagnosis)
        print(f"Fell through :/ could not fully populate.")
        return (procedure, diagnosis)

    def make_open_procedure_prompt(self, denial_text=None):
        if denial_text is not None:
            return f"What was the procedure/treatment and what is the diagnosis from the following denial (remember to provide two strings seperated by MAGIC as your response): {denial_text}"
        else:
            return None

    def make_open_prompt(self, denial_text=None, procedure=None, diagnosis=None) -> str:
        if denial_text is None:
            return None
        start = "Write a health insurance appeal for the following denial:"
        if (
            procedure is not None
            and procedure != ""
            and diagnosis is not None
            and diagnosis != ""
        ):
            start = f"Write a health insurance appeal for procedure {procedure} with diagnosis {diagnosis} given the following denial:"
        elif procedure is not None and procedure != "":
            start = f"Write a health insurance appeal for procedure {procedure} given the following denial:"
        return f"{start}\n{denial_text}"

    def make_open_med_prompt(
        self, procedure=None, diagnosis=None
    ) -> Optional[str]:
        if procedure is not None:
            if diagnosis is not None:
                return f"Why is {procedure} medically necessary for {diagnosis}?"
            else:
                return f"Why is {procedure} is medically necessary?"
        else:
            return None

    def make_appeals(self, denial, template_generator):
        open_prompt = self.make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )
        open_med_reason_prompt = self.make_open_med_prompt(
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        medical_reasons = []
        # TODO: use the streaming and cancellable APIs (maybe some fancy JS on the client side?)
        generated_futures = []

        # For any model that we have a prompt for try to call it
        def get_model_result(model: RemoteModel, prompt: str, t: str) -> List[Tuple[str, Optional[str]]]:
            print(f"Looking up on {model}")
            if prompt is None:
                print(f"No prompt for {model} skipping")
                return []
            results = model.infer(prompt, t)
            print(f"Infered {results} for {model}-{t} using {prompt}")
            print("Yay!")
            return results

        calls = [
            [self.remotehealth, open_prompt, "full"],
#            [self.together, open_prompt],
#            [self.palm, open_prompt],
        ]

        backup_calls = [
#            [self.perplexity, open_prompt],
#            [self.anyscale, open_prompt],
#            [self.anyscale2, open_prompt],
#            [self.runpod, open_prompt],
        ]
        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = template_generator.generate_static()
        appeals = []
        if static_appeal is None:
            calls.extend(
                [
                    [self.remotehealth, open_med_reason_prompt, "med_reason"],
                ]
            )
        else:
            # Otherwise just put in as is.
            appeals.append(static_appeal)

        # Executor map wants a list for each parameter.

        def make_calls_async(calls):
            print(f"Calling models: {calls}")
            generated_futures = map(
                lambda x: executor.submit(get_model_result, *x), calls
            )

            def generated_to_appeals_text(k_text):
                model_results = k_text.result()
                appeals_text = []
                for k, text in model_results:
                    if text is None:
                        pass
                    appeal_text = ""
                    # It's either full or a reason to plug into a template
                    if k == "full":
                        appeals_text += text
                    else:
                        appeal_text += template_generator.generate(text)
                return appeals_text

            generated_text = map(
                generated_to_appeals_text,
                concurrent.futures.as_completed(generated_futures),
            )
            return generated_text

        generated_text = make_calls_async(calls)
        appeals = itertools.chain(appeals, generated_text)
        # Check and make sure we have a result
        try:
            appeals = itertools.chain([appeals.__next__()], appeals)
        except StopIteration:
            appeals = make_calls_async(backup_calls)
        print(f"Sending back {appeals}")
        return itertools.chain.from_iterable(appeals)
