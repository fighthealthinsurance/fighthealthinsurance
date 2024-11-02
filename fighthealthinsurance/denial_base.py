from abc import ABC, abstractmethod


class DenialBase(ABC):
    @abstractmethod
    def get_denialtype(self, denial_text, procedure, diagnosis):
        pass

    @abstractmethod
    def get_regulator(self, text):
        pass

    @abstractmethod
    def get_plan_type(self, text):
        pass

    @abstractmethod
    def get_procedure_and_diagnosis(self, denial_text):
        pass
