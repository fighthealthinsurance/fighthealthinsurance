import pandas as pd
from typing import Optional


def get_medicaid_info(state: Optional[str], **kwargs) -> str:
    # Simulate fetching Medicaid info based on the state and other parameters
    if state:
        # Here you would typically query a database or an API to get the Medicaid info
        return f"Medicaid info for {state} with parameters: {kwargs}"
    else:
       return "No state selected, general medicaid info is []" # Fetch your general medicaid info google doc and put it here


def is_eligible(**kwargs) -> Tuple[bool, List[str]]:
    """
    Simulate an eligibility check for Medicaid based on the provided parameters.
    Returns a tuple of (eligibility, missing_info).
    If we return false and missing_info is not empty it means the user is not eligible
    with our current information, and we need to ask them for more information.
    If we return false and missing_info is empty it means we can't find a way for the user
    to be eligible for medicaid, and we should route them to a professional for further assistance.
    If we return true, it means we think the user is eligible for medicaid and we'll route them to
    signup.
    :param kwargs: Parameters for eligibility check
    """
    # Simulate eligibility check based on the provided parameters
    # This is a placeholder implementation
    eligibility = False  # Assume eligible for demonstration purposes
    missing_info = ["Work hours", "Income level"]  # Example of missing info

    return eligibility, missing_info