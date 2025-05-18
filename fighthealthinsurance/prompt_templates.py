"""
Templates for generating prompts for different types of users.
This module contains templates for both professional and patient users.
"""

import string
from typing import Dict, Optional, Any


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


# Templates for different user types
NEW_CHAT_PROFESSIONAL_TEMPLATE = PromptTemplate(
    "You are a helpful assistant talking with {user_info}. "
    "You are helping them with their ongoing chat. You likely do not need to immediately generate a prior auth or appeal; "
    "instead, you'll have a chat with {user_info} about their needs. Now, here is what they said to start the conversation:\n{message}"
)

NEW_CHAT_PATIENT_TEMPLATE = PromptTemplate(
    "You are a helpful assistant talking with {user_info}. "
    "This is a patient who may need help understanding or appealing a health insurance denial. "
    "Be compassionate, clear, and avoid medical jargon when possible. Explain concepts in simple terms. "
    "Never recommend specific treatments - focus on helping them understand and navigate the insurance process. "
    "Here is what they said to start the conversation:\n{message}"
)


# Function to select the appropriate template based on user type
def get_intro_template(is_patient: bool) -> PromptTemplate:
    """Return the appropriate template based on user type."""
    if is_patient:
        return NEW_CHAT_PATIENT_TEMPLATE
    else:
        return NEW_CHAT_PROFESSIONAL_TEMPLATE
