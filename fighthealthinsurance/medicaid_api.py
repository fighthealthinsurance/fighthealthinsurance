import pandas as pd
from typing import Optional


def get_medicaid_info(state: Optional[str], **kwargs) -> str:
    # Simulate fetching Medicaid info based on the state and other parameters
    if state:
        # Here you would typically query a database or an API to get the Medicaid info
        return f"Medicaid info for {state} with parameters: {kwargs}"
    else:
       return "No state selected, general medicaid info is []" # Fetch your general medicaid info google doc and put it here