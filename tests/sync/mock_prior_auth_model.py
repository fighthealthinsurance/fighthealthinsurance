"""Mock models for prior auth testing."""

from typing import Tuple, Optional, Dict, Any, List


class MockPriorAuthModel:
    """A mock model that returns predefined responses for prior authorization."""

    async def generate_prior_auth_proposals(
        self,
        diagnosis: str,
        treatment: str,
        answers: Dict[str, str],
        insurance_company: Optional[str] = None,
    ) -> List[str]:
        """
        Generate mock proposals for a prior authorization request.

        Args:
            diagnosis: The patient's diagnosis
            treatment: The proposed treatment
            answers: Answers to the questions about the case
            insurance_company: Optional name of the insurance company

        Returns:
            A list of string proposals
        """
        # Return standard proposals regardless of the input
        proposals = [
            f"This is a standard mock prior authorization proposal for {diagnosis} treatment with {treatment}.",
            f"This is an alternative prior authorization proposal for {diagnosis}.",
        ]

        return proposals

    async def generate_prior_auth_questions(
        self, diagnosis: str, treatment: str, insurance_company: Optional[str] = None
    ) -> List[List[str]]:
        """
        Generate mock questions for a prior authorization.

        Args:
            diagnosis: The patient's diagnosis
            treatment: The proposed treatment
            insurance_company: Optional name of the insurance company

        Returns:
            A list of questions in the format [[question1, hint1], [question2, hint2], ...]
        """
        # Return standard questions regardless of the input
        questions = [
            [
                "How long has the patient had this condition?",
                "Please provide duration in months/years",
            ],
            [
                "Has the patient tried conservative treatments?",
                "List any previous treatments",
            ],
            ["What is the severity of the condition?", "Mild, moderate, or severe"],
        ]

        return questions
