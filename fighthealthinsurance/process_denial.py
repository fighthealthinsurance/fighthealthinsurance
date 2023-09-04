import os
import requests
import csv
import icd10
import re
from abc import ABC, abstractmethod
from fighthealthinsurance.models import (
    DenialTypes,
    PlanType,
    Regulator,
    Diagnosis,
    Procedures,
)
from typing import Optional
from memoize import memoize

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


class RemoteModel:
    """
    For models which produce a "full" appeal tag them with "full"
    For medical reason (e.g. ones where we hybrid with our "expert system"
    ) tag them with "med_reason".
    """

    @classmethod
    def infer(cls, prompt) -> Optional[str]:
        pass

    @classmethod
    def model_type(cls) -> str:
        pass


class RemoteBioGPT:
    """Use BioGPT for denial magic calls a service"""

    @classmethod
    def infer(cls, prompt) -> Optional[str]:
        try:
            return requests.post(
                "http://model-backend-svc/biogpt/infer", json={"prompt": prompt}
            ).content
        except:
            return None

    @classmethod
    def model_type(cls) -> str:
        return "med_reason"


class RemoteMed:
    """Use RemoteMed for denial magic calls a service"""

    @classmethod
    def infer(cls, prompt) -> Optional[str]:
        try:
            return requests.post(
                "http://model-backend-svc/openllamamed/infer", json={"prompt": prompt}
            ).content
        except:
            return None

    @classmethod
    def model_type(cls) -> str:
        return "med_reason"


class RemoteOpen:
    """Use RemoteOpen for denial magic calls a service"""

    @classmethod
    def infer(cls, prompt) -> Optional[str]:
        api_base = os.getenv("OPENAI_API_BASE")
        token = os.getenv("OPENAI_API_KEY")
        if api_base is None:
            print("Error no API base provided for anyscale")
        if token is None:
            print("Error no Token provided for anyscale.")
        if prompt is None:
            print("Error: must supply a prompt.")
            return None
        url = f"{api_base}/chat/completions"
        try:
            import requests

            s = requests.Session()
            json_result = s.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "meta-llama/Llama-2-70b-chat-hf",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                },
            ).json()
        except Exception as e:
            print(f"Error {e} calling anyscale")
            return None
        try:
            return json_result["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"Error {e} processing {json_result} from anyscale.")

    @classmethod
    def model_type(cls) -> str:
        return "full"


class ProcessDenialRegex(DenialBase):
    """Process the denial type based on the regexes stored in the database."""

    def __init__(self):
        self.planTypes = PlanType.objects.all()
        self.regulators = Regulator.objects.all()
        self.denialTypes = DenialTypes.objects.all()
        self.diagnosis = Diagnosis.objects.all()
        self.procedures = Procedures.objects.all()

    def get_procedure(self, text):
        print(f"Getting procedure types for {text}")
        procedure = None
        for d in self.procedures:
            print(f"Exlporing {d} w/ {d.regex}")
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
            print(f"Exlporing {d} w/ {d.regex}")
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
            print(f"Exlporing {d} w/ {d.regex} & {d.negative_regex}")
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
                regulators.push(r)
        return regulators

    def get_plan_type(self, text):
        plans = []
        for p in self.planTypes:
            if p.regex.pattern != "" and p.regex.search(text) is not None:
                print(f"positive regex match for plan {p}")
                if p.negative_regex != "" or p.negative_regex.search(text) is None:
                    plans.push(p)
            else:
                print(f"no match {p}")
        return plans
