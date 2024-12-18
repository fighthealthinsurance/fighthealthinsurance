from typing import Awaitable, Tuple, Optional
from abc import ABC, abstractmethod


class DenialBase(ABC):
    @abstractmethod
    async def get_denialtype(self, denial_text, procedure, diagnosis):
        pass

    @abstractmethod
    async def get_regulator(self, text) -> Optional[str]:
        pass

    @abstractmethod
    async def get_plan_type(self, text) -> Optional[str]:
        pass

    @abstractmethod
    async def get_procedure_and_diagnosis(
        self, denial_text
    ) -> Tuple[Optional[str], Optional[str]]:
        pass
