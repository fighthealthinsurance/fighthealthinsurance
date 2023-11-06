import csv
import os
import re
from abc import ABC, abstractmethod
from functools import cache
from typing import Optional

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


class RemoteOpenLike(RemoteModel):
    def __init__(self, api_base, token, model, system_message):
        self.api_base = api_base
        self.token = token
        self.model = model
        self.system_message = system_message
    
    @cache
    def infer(self, prompt) -> Optional[str]:
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
            json_result = s.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": self.system_message,
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                },
            ).json()
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
        system_message = "You have a deep medical knowledge write appeals for health insurance denials. You are a patient, not a doctor. You are writing on behalf of yourself. You write directly, in the style of patio11 or a bureaucrat but never get mad at the insurance companies. Feel free to speculate why it might be megically necessary. Use YourNameMagic in place of your name, SCSID for the subscriber id, and GPID as the group id."
        return super(self).__init__(api_base, token, model, system_message)


    def model_type(self) -> str:
        return "full"


class RemoteMedicalNecessaryOpenLike(RemoteOpenLike):
    def __init__(self, api_base, token, model):
        system_message = "You have a deep medical knowledge write appeals for health insurance denials. You have a health insurance denial, what is the treatment and why is it medically necessary?"
        return super(self).__init__(api_base, token, model, system_message)


class RemotePerplexityInstruct(RemoteFullOpenLike):
    """Use RemotePerplexity for denial magic calls a service"""

    def __init__(self):
        api_base = "https://api.perplexity.ai"
        token = os.getenv("PERPLEXITY_API")
        model = "mistral-7b-instruct"
        return super(self).__init__(api_base, token, model)


class RemoteOpen(RemoteFullOpenLike):
    """Use RemoteOpen for denial magic calls a service"""

    def __init__(self):
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        model = "meta-llama/Llama-2-70b-chat-hf"
        return super(self).__init__(api_base, token, model)


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
        self.combined = "\n".join(prefaces + main + footer)

    def generate_static(self):
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
        self.biogpt = 

    def make_open_prompt(self, denial_text=None, procedure=None, diagnosis=None) -> str:
        start = "Write a health insurance appeal for the following denial:"
        if (procedure is not None
            and procedure != ""
            and diagnosis is not None
            and diagnosis != ""):
            start = f"Write a health insurance appeal for procedure {procedure} with diagnosis {diagnosis} given the following denial:"
        elif procedure is not None and procedure != "":
            start = f"Write a health insurance appeal for procedure {procedure} given the following denial:"
        return f"{start}\n{denial_text}"

    def make_open_llama_med_prompt(self, procedure=None, diagnosis=None) -> Optional[str]:
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


    def make_appeals(self, denial_text, insurnace_company, claim_id, denial_date, t):
        denial_date_info = ""
        if denial_date is not None:
            denial_date_info = "on or about {denial_date}"
        # Use LLMS
        bio_gpt_prompt = make_biogpt_prompt(
            procedure=denial.procedure, diagnosis=denial.diagnosis
        )
        llama_med_prompt = make_open_llama_med_prompt(
            procedure=denial.procedure, diagnosis=denial.diagnosis
        )
        open_prompt = make_open_prompt(
            denial_text=denial.denial_text,
            procedure=denial.procedure,
            diagnosis=denial.diagnosis,
        )

        medical_reasons = []
        # The "vanilla" expert system only appeal:
        raw_appeal = "\n".join(prefaces + main + footer)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            generated_futures = []

            # For any model that we have a prompt for try to call it
            def get_model_result(
                    model: RemoteModel, prompt: str
            ) -> (str, Optional[str]):
                print(f"Looking up on {model}")
                if prompt is None:
                    print(f"No prompt for {model} skipping")
                    return (model.model_type(), None)
                infered = model.infer(prompt)
                t = model.model_type()
                print(f"Infered {infered} for {model} using {prompt}")
                return (t, infered)

            calls = [[RemoteOpen(), open_prompt], [RemotePerplexity(), open_prompt]]
            # If we need to know the medical reason ask our friendly LLMs
            if "{medical_reason}" in raw_appeal:
                calls.extend(
                    [
                        [RemoteBioGPT(), bio_gpt_prompt],
                        [RemoteMed(), llama_med_prompt],
                    ]
                )
            else:
                # Otherwise just put in as is.
                if raw_appeal != "":
                    appeals.append(raw_appeal)

            # Executor map wants a list for each parameter.
            model_calls = list(zip(*calls))

            # We don't get back futures using executor.map but they are still called in parallel.
            generated: List[(str, Optional[str])] = list(
                executor.map(get_model_result, *model_calls)
            )

            # Get the futures as the become available.
            for k_text in generated:
                k, text = k_text
                if text is None:
                    continue
                appeal_text = ""
                if k == "full":
                    appeal_text = text
                else:
                    appeal_text = t.generate(text)
                if appeal_text is not None:
                    appeals.append(appeal_text)
            appeal_text = appeals.generate_static()
            if appeal_text is not None:
                appeals.append(appeal_text)
            return appeals
