from typing import List, Tuple, Optional
from loguru import logger
import asyncio
import time
from fighthealthinsurance.models import Denial
from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.utils import best_within_timelimit


class MLAppealQuestionsHelper:
    @staticmethod
    async def generate_questions_for_denial(
        denial: Denial, speculative: bool
    ) -> List[Tuple[str, str]]:
        """
        Generate appeal questions for a given denial.

        Args:
            denial: The denial object for which to generate questions.
            speculative: Whether this is a speculative generation (candidate) or final.

        Returns:
            A list of (question, answer) tuples.
        """
        questions: List[Tuple[str, str]] = []
        # Check if candidate questions exist and the diagnosis/procedure has not changed
        if (
            denial.candidate_procedure == denial.procedure
            and denial.candidate_diagnosis == denial.diagnosis
            and denial.candidate_generated_questions
            and len(denial.candidate_generated_questions) > 0
        ):
            logger.debug(f"Using candidate questions for denial {denial.denial_id}")
            questions = denial.candidate_generated_questions
        # Generate questions using the AppealGenerator
        else:
            try:
                appeal_generator = AppealGenerator()

                raw_questions = await asyncio.wait_for(
                    appeal_generator.get_appeal_questions(
                        denial_text=denial.denial_text,
                        procedure=denial.procedure,
                        diagnosis=denial.diagnosis,
                        patient_context=denial.health_history,
                        plan_context=denial.plan_context,
                        timeout=30,
                    ),
                    timeout=45,
                )  # Add a safety timeout

                # If we got results, process them
                if raw_questions:
                    # Ensure questions are in the form of tuples
                    questions = [
                        (q[0], q[1]) if isinstance(q, (list, tuple)) else (str(q), "")
                        for q in raw_questions
                    ]

                    # Filter out any lines containing "Note:" as they are typically context lines
                    questions = [
                        (q, a)
                        for q, a in questions
                        if "Note:" not in q and "Note:" not in a
                    ]

                    # If the last line contains a note, remove it
                    if questions and len(questions) > 0:
                        last_q, last_a = questions[-1]
                        if "note" in last_q.lower() or "note" in last_a.lower():
                            questions.pop()
                else:
                    logger.warning(
                        f"No questions generated for denial {denial.denial_id}"
                    )

            except asyncio.TimeoutError:
                logger.warning(
                    f"Timeout while generating questions for denial {denial.denial_id}"
                )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to generate questions for denial {denial.denial_id}: {e}"
                )
        # Update the denial with the result
        if questions and len(questions) > 0:
            logger.debug(
                f"Generated {len(questions)} questions for denial {denial.denial_id}"
            )
            qs = Denial.objects.filter(denial_id=denial.denial_id)
            if speculative:
                await qs.aupdate(candidate_generated_questions=questions)
            else:
                await qs.aupdate(generated_questions=questions)
            return questions
        else:
            return []
