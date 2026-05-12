"""Tests for chat safety features including crisis detection and false promise filtering."""

from django.test import TestCase

from fighthealthinsurance.chat.safety_filters import (
    detect_crisis_keywords,
    detect_delete_data_request,
    detect_false_promises,
    llm_requested_delete_handoff,
    CRISIS_RESOURCES,
    DELETE_DATA_RESPONSE,
    DELETE_DATA_SENTINEL,
)

# Import internal variables for testing
from fighthealthinsurance.chat.safety_filters import (
    _CRISIS_PHRASES,
    _DELETE_DATA_PHRASES,
    _FALSE_PROMISE_PATTERNS,
)


class TestCrisisDetection(TestCase):
    """Tests for crisis/self-harm keyword detection."""

    def test_detects_active_suicide_expressions(self):
        """Test that active first-person suicide expressions are detected."""
        suicide_messages = [
            "I want to kill myself",
            "I'm going to kill myself",
            "I want to end my life",
            "I want to die",
            "I don't want to live",
            "I'd rather be dead",
            "I'm better off dead",
            "I have no reason to live",
        ]
        for message in suicide_messages:
            self.assertTrue(
                detect_crisis_keywords(message),
                f"Failed to detect crisis in: {message}",
            )

    def test_detects_self_harm_expressions(self):
        """Test that self-harm expressions are detected."""
        self_harm_messages = [
            "I want to hurt myself",
            "I'm going to hurt myself",
            "I've been cutting myself",
            "I'm cutting myself",
        ]
        for message in self_harm_messages:
            self.assertTrue(
                detect_crisis_keywords(message),
                f"Failed to detect crisis in: {message}",
            )

    def test_detects_planning_language(self):
        """Test that planning/intent language is detected."""
        planning_messages = [
            "I'm thinking about ending it",
            "I'm planning to end it all",
        ]
        for message in planning_messages:
            self.assertTrue(
                detect_crisis_keywords(message),
                f"Failed to detect crisis in: {message}",
            )

    def test_case_insensitive_detection(self):
        """Test that detection is case-insensitive."""
        self.assertTrue(detect_crisis_keywords("I WANT TO KILL MYSELF"))
        self.assertTrue(detect_crisis_keywords("i want to die"))
        self.assertTrue(detect_crisis_keywords("I Want To End My Life"))

    def test_does_not_flag_mental_health_denial_appeals(self):
        """
        CRITICAL TEST: Ensure we don't block legitimate mental health insurance appeals.

        People seeking help with denied mental health coverage should NOT trigger
        crisis detection - they're here to fight their insurance, not in crisis.
        """
        mental_health_denial_messages = [
            # Suicide/suicidal ideation treatment denials
            "My insurance denied coverage for suicidal ideation treatment",
            "They won't cover my suicide prevention therapy",
            "The claim for my suicidal thoughts treatment was rejected",
            "I need help appealing a denial for suicide risk assessment",
            "My psychiatrist recommended treatment for suicidal ideation but insurance denied it",
            # Self-harm treatment denials
            "Insurance denied my self-harm therapy coverage",
            "They rejected the claim for my self-injury treatment program",
            "I need to appeal a denial for cutting behavior therapy",
            # Depression/mental health denials
            "My depression treatment was denied",
            "Insurance won't cover my mental health hospitalization",
            "They denied coverage for my psychiatric evaluation",
            "I need help with a denial for therapy sessions",
            # General mental health context
            "The patient has a history of suicidal ideation",
            "Treatment for self-harm behaviors was medically necessary",
            "My doctor says I need intensive outpatient for suicide risk",
        ]
        for message in mental_health_denial_messages:
            self.assertFalse(
                detect_crisis_keywords(message),
                f"Incorrectly flagged mental health denial appeal: {message}",
            )

    def test_does_not_flag_normal_health_messages(self):
        """Test that normal health insurance questions are not flagged."""
        normal_messages = [
            "My insurance denied my MRI claim",
            "How do I appeal a prior authorization denial?",
            "I need help with my Medicaid application",
            "The insurance company won't cover my medication",
            "Can you help me write an appeal letter?",
            "I'm frustrated with my insurance company",
            "This denial is killing my budget",  # "killing" used figuratively
            "My claim was dead on arrival",  # "dead" used figuratively
            "I want to fight this denial",
            "I'm going to appeal this decision",
        ]
        for message in normal_messages:
            self.assertFalse(
                detect_crisis_keywords(message),
                f"Incorrectly flagged normal message: {message}",
            )

    def test_does_not_flag_frustrated_but_not_crisis(self):
        """Test that frustration expressions are not flagged as crisis."""
        frustrated_messages = [
            "I give up on this insurance company",
            "There's no point in even trying with these people",
            "I can't go on fighting this alone",
            "This is hopeless",
            "I feel like giving up on this appeal",
        ]
        for message in frustrated_messages:
            self.assertFalse(
                detect_crisis_keywords(message),
                f"Incorrectly flagged frustrated message: {message}",
            )

    def test_crisis_resources_contains_hotlines(self):
        """Test that crisis resources include essential hotlines."""
        self.assertIn("988", CRISIS_RESOURCES)
        self.assertIn("741741", CRISIS_RESOURCES)
        self.assertIn("pflag.org", CRISIS_RESOURCES)
        self.assertIn("Trans Lifeline", CRISIS_RESOURCES)


class TestFalsePromiseDetection(TestCase):
    """Tests for false promise detection in AI responses."""

    def test_detects_guarantee_promises(self):
        """Test that guarantee language is detected."""
        false_promise_responses = [
            "I guarantee your appeal will be approved",
            "This will definitely get approved",
            "You're certain to win this appeal",
            "I promise you will be approved",
            "This guarantee approval will work",
            "You have a 100% chance of success",
        ]
        for response in false_promise_responses:
            self.assertTrue(
                detect_false_promises(response),
                f"Failed to detect false promise in: {response}",
            )

    def test_detects_overconfident_language(self):
        """Test that overconfident success language is detected."""
        overconfident_responses = [
            "This will certainly get you approved",
            "You will definitely win your appeal",
            "This approach always works for appeals",
            "Appeals like this always succeed",
        ]
        for response in overconfident_responses:
            self.assertTrue(
                detect_false_promises(response),
                f"Failed to detect false promise in: {response}",
            )

    def test_allows_appropriate_language(self):
        """Test that appropriate hedged language is not flagged."""
        appropriate_responses = [
            "This may improve your chances of approval",
            "Many appeals like this are successful",
            "Studies suggest most appeals are eventually approved",
            "This approach has been effective for other patients",
            "While I can't guarantee the outcome, this is a strong case",
            "The evidence supports your appeal",
            "This is a compelling argument for medical necessity",
            "I'll help you draft a persuasive appeal",
        ]
        for response in appropriate_responses:
            self.assertFalse(
                detect_false_promises(response),
                f"Incorrectly flagged appropriate response: {response}",
            )

    def test_handles_none_input(self):
        """Test that None input returns False."""
        self.assertFalse(detect_false_promises(None))

    def test_handles_empty_string(self):
        """Test that empty string returns False."""
        self.assertFalse(detect_false_promises(""))

    def test_case_insensitive_detection(self):
        """Test that detection is case-insensitive."""
        self.assertTrue(detect_false_promises("I GUARANTEE YOUR APPROVAL"))
        self.assertTrue(detect_false_promises("This Will Definitely Get Approved"))


class TestCrisisPhraseList(TestCase):
    """Tests for the crisis phrases list completeness."""

    def test_phrases_are_unique(self):
        """All phrases should be unique."""
        self.assertEqual(
            len(_CRISIS_PHRASES),
            len(set(_CRISIS_PHRASES)),
            "Crisis phrases list contains duplicates",
        )


class TestFalsePromisePatternList(TestCase):
    """Tests for the false promise patterns list."""

    def test_patterns_compile(self):
        """All patterns should be valid regex."""
        import re

        for pattern in _FALSE_PROMISE_PATTERNS:
            try:
                re.compile(pattern)
            except re.error as e:
                self.fail(f"Pattern '{pattern}' failed to compile: {e}")

    def test_patterns_are_unique(self):
        """All patterns should be unique."""
        self.assertEqual(
            len(_FALSE_PROMISE_PATTERNS),
            len(set(_FALSE_PROMISE_PATTERNS)),
            "False promise patterns list contains duplicates",
        )


class TestDeleteDataDetection(TestCase):
    """Tests for detection of user requests to delete their data."""

    def test_detects_explicit_delete_data_requests(self):
        """Direct deletion phrasings should be detected."""
        delete_messages = [
            "Please delete my data",
            "I want you to delete my account",
            "Remove my information from your system",
            "Erase my profile",
            "Wipe my records",
            "Please close my account",
            "I'd like to cancel my account",
            "Can you deactivate my account?",
            "Delete everything about me",
            "Please forget me",
            "I want you to forget my data",
            "I am submitting a GDPR deletion request",
            "I want to invoke my right to be forgotten",
            "I want to opt out of having my data stored",
        ]
        for message in delete_messages:
            self.assertTrue(
                detect_delete_data_request(message),
                f"Failed to detect delete-data request in: {message}",
            )

    def test_case_insensitive_detection(self):
        """Detection should be case-insensitive."""
        self.assertTrue(detect_delete_data_request("DELETE MY ACCOUNT"))
        self.assertTrue(detect_delete_data_request("Erase My Data"))
        self.assertTrue(detect_delete_data_request("close my account"))

    def test_does_not_flag_unrelated_delete_phrases(self):
        """Deleting parts of a document or unrelated content should not trigger."""
        unrelated_messages = [
            "Please delete this paragraph from my appeal",
            "Remove the diagnosis from the record I uploaded",
            "Can you delete the wrong date in my submission?",
            "I'd like to close my claim with the insurer",
            "Please cancel my appointment with the doctor",
            "Forget that I mentioned the deductible earlier",
            "Erase the second sentence of the letter",
            "Wipe the formatting from the pasted text",
        ]
        for message in unrelated_messages:
            self.assertFalse(
                detect_delete_data_request(message),
                f"Incorrectly flagged unrelated message: {message}",
            )

    def test_known_overmatches_are_documented(self):
        """Document known false positives so reviewers see them in failing diffs.

        "Delete my profile picture" matches `delete my profile` and triggers
        the deletion handoff. This is intentional — directing such users at the
        deletion flow is a reasonable response to a non-existent feature
        request — but if we ever narrow the pattern, this test should fail and
        force a deliberate decision.
        """
        self.assertTrue(detect_delete_data_request("Please delete my profile picture"))

    def test_does_not_flag_normal_health_messages(self):
        """Normal insurance-appeal questions should never trigger."""
        normal_messages = [
            "My insurance denied my MRI claim",
            "Can you help me write an appeal letter?",
            "How do I appeal a prior authorization denial?",
            "What information do you need from me?",
        ]
        for message in normal_messages:
            self.assertFalse(
                detect_delete_data_request(message),
                f"Incorrectly flagged normal message: {message}",
            )

    def test_handles_empty_or_none(self):
        """Empty/None input should return False without error."""
        self.assertFalse(detect_delete_data_request(""))
        self.assertFalse(detect_delete_data_request(None))  # type: ignore[arg-type]

    def test_canned_response_links_to_self_service_page(self):
        """Canned response must point users at /remove_data."""
        self.assertIn("/remove_data", DELETE_DATA_RESPONSE)


class TestLLMDeleteHandoff(TestCase):
    """Tests for detecting the LLM-emitted delete-data sentinel."""

    def test_detects_sentinel_alone(self):
        self.assertTrue(llm_requested_delete_handoff(DELETE_DATA_SENTINEL))

    def test_detects_sentinel_case_insensitive(self):
        self.assertTrue(llm_requested_delete_handoff("[[delete_data_request]]"))
        self.assertTrue(llm_requested_delete_handoff("[[Delete_Data_Request]]"))

    def test_detects_sentinel_embedded_in_text(self):
        """Even if the model adds stray whitespace or text, the sentinel triggers."""
        self.assertTrue(llm_requested_delete_handoff(f"\n{DELETE_DATA_SENTINEL}\n"))
        self.assertTrue(llm_requested_delete_handoff(f"Sure. {DELETE_DATA_SENTINEL}"))

    def test_does_not_trigger_on_plain_text(self):
        plain_responses = [
            "Sure, I can help you with that appeal.",
            "Please share more details about the denial.",
            "Here is a draft of your appeal letter.",
            "delete data request",  # bare phrase without brackets
        ]
        for response in plain_responses:
            self.assertFalse(
                llm_requested_delete_handoff(response),
                f"Incorrectly triggered on: {response}",
            )

    def test_handles_empty_or_none(self):
        self.assertFalse(llm_requested_delete_handoff(""))
        self.assertFalse(llm_requested_delete_handoff(None))  # type: ignore[arg-type]


class TestDeleteDataPhraseList(TestCase):
    """Sanity checks on the delete-data phrase list."""

    def test_patterns_compile(self):
        """All phrases should be valid regex fragments."""
        import re

        for phrase in _DELETE_DATA_PHRASES:
            try:
                re.compile(phrase)
            except re.error as e:
                self.fail(f"Phrase '{phrase}' failed to compile: {e}")

    def test_phrases_are_unique(self):
        self.assertEqual(
            len(_DELETE_DATA_PHRASES),
            len(set(_DELETE_DATA_PHRASES)),
            "Delete-data phrases list contains duplicates",
        )


class TestPromptTemplateSentinelCoupling(TestCase):
    """Guard against drift between the LLM-facing instruction and the sentinel."""

    def test_intro_templates_reference_the_sentinel(self):
        """If the sentinel changes, both intro templates must update with it."""
        from fighthealthinsurance.prompt_templates import (
            DELETE_DATA_INSTRUCTION,
            NEW_CHAT_PATIENT_TEMPLATE,
            NEW_CHAT_PROFESSIONAL_TEMPLATE,
        )

        self.assertIn(DELETE_DATA_SENTINEL, DELETE_DATA_INSTRUCTION)
        self.assertIn(DELETE_DATA_SENTINEL, NEW_CHAT_PATIENT_TEMPLATE.template)
        self.assertIn(DELETE_DATA_SENTINEL, NEW_CHAT_PROFESSIONAL_TEMPLATE.template)

    def test_sentinel_in_template_is_detected(self):
        """The exact sentinel embedded in the template must round-trip through the detector."""
        from fighthealthinsurance.prompt_templates import DELETE_DATA_INSTRUCTION

        self.assertTrue(llm_requested_delete_handoff(DELETE_DATA_INSTRUCTION))
