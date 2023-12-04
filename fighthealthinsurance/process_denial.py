import itertools
import concurrent
import csv
import os
import re
from abc import ABC, abstractmethod
from functools import cache, lru_cache
from typing import Optional
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

    def infer(self, prompt):
        pass

    def model_type(self):
        pass


class RemoteBioGPT(RemoteModel):
    """Use BioGPT for denial magic calls a service"""

    @cache
    def infer(self, prompt) -> Optional[str]:
        try:
            return requests.post(
                "http://model-backend-svc/biogpt/infer", json={"prompt": prompt}
            ).text
        except:
            return None

    def model_type(self) -> str:
        return "med_reason"


class RemoteMed(RemoteModel):
    """Use RemoteMed for denial magic calls a service"""

    @cache
    def infer(self, prompt) -> Optional[str]:
        try:
            return requests.post(
                "http://model-backend-svc/openllamamed/infer", json={"prompt": prompt}
            ).text
        except:
            return None

    def model_type(self) -> str:
        return "med_reason"


class RemoteRunPod(RemoteModel):
    def __init__(self):
        self.model_name = os.getenv("RUNPOD_ENDPOINT", "")
        self.token = os.getenv("RUNPOD_API_KEY", "")
        # We use our own caching mechanism here so we don't cache None
        self.internal_cache = {}

    def infer(self, prompt) -> Optional[str]:
        if prompt in self.internal_cache and self.internal_cache[prompt] is not None:
            print(f"Using cached value for f{prompt} to avoid runpod.")
            return self.internal_cache[prompt]
        print(f"Looking up runpod {self.model_name}")
        if self.token is None:
            print("Error no Token provided for runpod.")

        if prompt is None:
            print("Error: must supply a prompt.")
            return None
        url = f"https://api.runpod.ai/v2/{self.model_name}/runsync"
        try:
            import requests

            s = requests.Session()
            json_result = s.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                json={"input": {"prompt": prompt}},
            ).json()
            print(f"jr {json_result} on runpod.")
        except Exception as e:
            print(f"Error {e} calling runpod")
            return None
        print(f"Initial result {json_result}")
        # When the backedn takes more than 60 seconds
        while "status" in json_result and json_result["status"] == "IN_QUEUE":
            job_id = json_result["id"]
            url = f"https://api.runpod.ai/v2/{self.model_name}/status/{job_id}"
            s = requests.Session()
            json_result = s.post(
                url, headers={"Authorization": f"Bearer {self.token}"}
            ).json()
            print(f"jr {json_result} on runpod.")
            time.sleep(4)
        try:
            r = json_result["output"]["result"]
            self.internal_cache[prompt] = r
            return r
        except Exception as e:
            print(f"Error {e} processing {json_result} from runpod.")
            return None

    def model_type(self) -> str:
        return "full"


class RemoteOpenLike(RemoteModel):
    def __init__(self, api_base, token, model, system_message, procedure_message=None):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_message = system_message
        self.procedure_message = procedure_message
        self.max_len = 4096 * 2
        self.procedure_response_regex = re.compile(r"\s*procedure\s*:?\s*", re.IGNORECASE)
        self.diagnosis_response_regex = re.compile(r"\s*diagnosis\s*:?\s*", re.IGNORECASE)

    def infer(self, prompt: str) -> Optional[str]:
        return self._infer(self.system_message, prompt)

    def _clean_procedure_response(self, response):
        return self.procedure_response_regex.sub("", response)

    def _clean_procedure_response(self, response):
        return self.diagnosis_response_regex.sub("", response)

    def get_procedure_and_diagnosis(
        self, prompt: str
    ) -> (Optional[str], Optional[str]):
        if self.procedure_message is None:
            print(f"No procedure message for {self.model} skipping")
            return (None, None)
        model_response = self._infer(self.procedure_message, prompt)
        if model_response is not None:
            responses = model_response.split("\n")
            if len(responses) == 2:
                r = (
                    self._clean_procedure_response(responses[0]),
                    self._clean_diagnosis_response(responses[1]))
                return r
            elif len(responses) == 1:
                r = (self._clean_procedure_response(responses[0]), None)
                return r
            else:
                print(f"Non-understood response {model_response} for procedure/diagnsosis.")
        else:
            print(f"No model response for {self.model}")
        return (None, None)

    @cache
    def _infer(self, system_prompt, prompt) -> Optional[str]:
        print(f"Looking up model {self.model} using {self.api_base}")
        if self.token is None:
            print("Error no Token provided for perplexity.")
        if prompt is None:
            print("Error: must supply a prompt.")
            return None
        url = f"{self.api_base}/chat/completions"
        try:
            import requests

            s = requests.Session()
            result = s.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {"role": "user", "content": prompt[0:self.max_len]},
                    ],
                    "temperature": 0.7,
                },
            )
            print(f"Got {result} on {self.api_base}")
            json_result = result.json()
        except Exception as e:
            print(f"Error {e} calling {self.api_base}")
            return None
        try:
            r = json_result["choices"][0]["message"]["content"]
            return r
        except Exception as e:
            print(f"Error {e} processing {json_result} from {self.api_base}.")
            return None


class RemoteFullOpenLike(RemoteOpenLike):
    def __init__(self, api_base, token, model):
        system_message = "You have a deep medical knowledge and write appeals for health insurance denials for fun. You are a patient, not a doctor. You are writing on behalf of yourself. You write directly, in the style of patio11 or a bureaucrat but never get mad at the insurance companies. Feel free to speculate why it might be megically necessary. Use YourNameMagic in place of your name, SCSID for the subscriber id, and GPID as the group id."
        procedure_message = "You have a deep insurance knowledge, have worked in a doctors office for years, and are an expert at reading health insurance denial letters. Your job is to figure out what procedure is being requested and for what diagnosis (if available). Answer consicely with the procedure on one line and diagnosis on the next line."
        return super().__init__(
            api_base, token, model, system_message, procedure_message
        )

    def model_type(self) -> str:
        return "full"


class RemoteHealthInsurance(RemoteFullOpenLike):
    def __init__(self):
        self.port = os.getenv("HEALTH_BACKEND_PORT")
        self.host = os.getenv("HEALTH_BACKEND_HOST")
        self.url = None
        if self.port is not None and self.host is not None:
            self.url = f"http://{self.host}:{self.port}"
        self.model = "/fighthealthinsurance_model_v0.2"
        super().__init__(self.url, token="", model=self.model)


class RemotePerplexityInstruct(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        super().__init__(api_base, token, model)


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
        if "{medical_reason}" not in self.combined:
            return self.combined
        else:
            return None

    def generate(self, medical_reason):
        return self.combined.replace("{medical_reason}", medical_reason)


class AppealGenerator(object):
    def __init__(self):
        self.regex_denial_processor = ProcessDenialRegex()
        self.perplexity = RemotePerplexityInstruct()
        self.anyscale = RemoteOpen()
        self.anyscale2 = RemoteOpenInst()
        self.biogpt = RemoteBioGPT()
        self.runpod = RemoteRunPod()
        self.health = RemoteHealthInsurance()

    def get_procedure_and_diagnosis(self, denial_text=None):
        prompt = self.make_open_procedure_prompt(denial_text)
        models_to_try = [
            self.regex_denial_processor,
            self.perplexity,
            self.anyscale,
            self.health,
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
                    print(f"Unexpected procedure diagnosis len on {procedure_diagnosis}")
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

    def make_open_llama_med_prompt(
        self, procedure=None, diagnosis=None
    ) -> Optional[str]:
        if procedure is not None:
            if diagnosis is not None:
                return f"Why is {procedure} medically necessary for {diagnosis}?"
            else:
                return f"Why is {procedure} is medically necessary?"
        else:
            return None

    def make_biogpt_prompt(self, procedure=None, diagnosis=None) -> Optional[str]:
        if procedure is not None:
            if diagnosis is not None:
                return f"{procedure} is medically necessary for {diagnosis} because"
            else:
                return f"{procedure} is medically necessary because"
        else:
            return None

    def make_appeals(self, denial, t):
        # Use LLMS
        bio_gpt_prompt = self.make_biogpt_prompt(
            procedure=denial.procedure, diagnosis=denial.diagnosis
        )
        llama_med_prompt = self.make_open_llama_med_prompt(
            procedure=denial.procedure, diagnosis=denial.diagnosis
        )
        open_prompt = self.make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        medical_reasons = []
        # TODO: use the streaming and cancellable APIs (maybe some fancy JS on the client side?)
        generated_futures = []

        # For any model that we have a prompt for try to call it
        def get_model_result(model: RemoteModel, prompt: str) -> (str, Optional[str]):
            print(f"Looking up on {model}")
            if prompt is None:
                print(f"No prompt for {model} skipping")
                return (model.model_type(), None)
            infered = model.infer(prompt)
            t = model.model_type()
            print(f"Infered {infered} for {model} using {prompt}")
            return (t, infered)

        calls = [
            [self.remotehealth, open_prompt],
            [self.perplexity, open_prompt],
            [self.anyscale, open_prompt],
            [self.anyscale2, open_prompt],
            [self.runpod, open_prompt],
        ]
        # If we need to know the medical reason ask our friendly LLMs
        static_appeal = t.generate_static()
        appeals = []
        if static_appeal is None:
            calls.extend(
                [
                    [self.biogpt, bio_gpt_prompt],
                ]
            )
        else:
            # Otherwise just put in as is.
            appeals.append(static_appeal)

        # Executor map wants a list for each parameter.

        print(f"Calling models: {calls}")
        generated_futures = map(lambda x: executor.submit(get_model_result, *x), calls)

        def generated_to_appeals_text(k_text):
            k, text = k_text.result()
            if text is None:
                return text
            appeal_text = ""
            # It's either full or a reason to plug into a template
            if k == "full":
                appeal_text = text
            else:
                appeal_text = t.generate(text)
            return appeal_text

        generated_text = map(
            generated_to_appeals_text,
            concurrent.futures.as_completed(generated_futures),
        )

        appeals = itertools.chain(appeals, generated_text)
        print(f"Sending back {appeals}")
        return appeals
