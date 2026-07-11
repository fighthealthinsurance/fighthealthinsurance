"""
Templates for generating prompts for different types of users.
This module contains templates for both professional and patient users.
"""

from typing import Union

from fighthealthinsurance.chat.safety_filters import DELETE_DATA_SENTINEL
from fighthealthinsurance.models import ChatType


class PromptTemplate:
    """Base class for all prompt templates."""

    def __init__(self, template_str: str):
        self.template = template_str

    def format(self, **kwargs) -> str:
        """Format the template with the given kwargs."""
        try:
            return self.template.format(**kwargs)
        except KeyError as e:
            missing_key = str(e).strip("'")
            raise ValueError(f"Missing required key: {missing_key} for prompt template")


# Instruction appended to every intro template so the LLM can hand off
# data-deletion requests to the self-service flow. The sentinel must match
# DELETE_DATA_SENTINEL in fighthealthinsurance/chat/safety_filters.py; the
# server detects it and substitutes the canned response (DELETE_DATA_RESPONSE).
DELETE_DATA_INSTRUCTION = (
    "IMPORTANT: If the user asks you to delete, erase, remove, wipe, or "
    "forget their data, account, profile, or records — or otherwise invokes "
    "deletion, right-to-be-forgotten, or GDPR erasure — do NOT attempt to "
    "delete anything and do NOT reassure them that you have. Reply with "
    "ONLY the exact token " + DELETE_DATA_SENTINEL + " on its own line and "
    "nothing else; the system will substitute the correct self-service "
    "instructions."
)

# Templates for different user types
NEW_CHAT_PROFESSIONAL_TEMPLATE = PromptTemplate(
    "You are a helpful assistant talking with {user_info}. "
    "You are helping them with their ongoing chat. You likely do not need to immediately generate a prior auth or appeal; "
    "instead, you'll have a chat with {user_info} about their needs. "
    + DELETE_DATA_INSTRUCTION
    + " Now, here is what they said to start the conversation:\n{message}"
)

NEW_CHAT_PATIENT_TEMPLATE = PromptTemplate(
    "You are a helpful assistant talking with {user_info}. "
    "This is a patient who may need help understanding or appealing a health insurance denial. "
    "Be compassionate, clear, and avoid medical jargon when possible. Explain concepts in simple terms. "
    "Never recommend specific treatments - focus on helping them understand and navigate the insurance process. "
    + DELETE_DATA_INSTRUCTION
    + " Here is what they said to start the conversation:\n{message}"
)


def get_intro_template(chat_type: Union[str, ChatType]) -> PromptTemplate:
    """Return the appropriate template based on chat type.

    Patient chats get a compassionate, jargon-free template.
    Professional and trial professional chats get the professional template.
    """
    if chat_type == ChatType.PATIENT:
        return NEW_CHAT_PATIENT_TEMPLATE
    return NEW_CHAT_PROFESSIONAL_TEMPLATE
