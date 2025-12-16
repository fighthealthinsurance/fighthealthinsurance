"""Tests for chat safety features including crisis detection and false promise filtering."""

import pytest
from django.test import TestCase

from fighthealthinsurance.chat_interface import (
    _detect_crisis_keywords,
    _detect_false_promises,
    CRISIS_KEYWORDS,
    CRISIS_RESOURCES,
    FALSE_PROMISE_PATTERNS,
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
                _detect_crisis_keywords(message),
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
                _detect_crisis_keywords(message),
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
                _detect_crisis_keywords(message),
                f"Failed to detect crisis in: {message}",
            )

    def test_case_insensitive_detection(self):
        """Test that detection is case-insensitive."""
        self.assertTrue(_detect_crisis_keywords("I WANT TO KILL MYSELF"))
        self.assertTrue(_detect_crisis_keywords("i want to die"))
        self.assertTrue(_detect_crisis_keywords("I Want To End My Life"))

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
                _detect_crisis_keywords(message),
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
                _detect_crisis_keywords(message),
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
                _detect_crisis_keywords(message),
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
                _detect_false_promises(response),
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
                _detect_false_promises(response),
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
                _detect_false_promises(response),
                f"Incorrectly flagged appropriate response: {response}",
            )

    def test_handles_none_input(self):
        """Test that None input returns False."""
        self.assertFalse(_detect_false_promises(None))

    def test_handles_empty_string(self):
        """Test that empty string returns False."""
        self.assertFalse(_detect_false_promises(""))

    def test_case_insensitive_detection(self):
        """Test that detection is case-insensitive."""
        self.assertTrue(_detect_false_promises("I GUARANTEE YOUR APPROVAL"))
        self.assertTrue(_detect_false_promises("This Will Definitely Get Approved"))


class TestCrisisKeywordList(TestCase):
    """Tests for the crisis keywords list completeness."""

    def test_keywords_are_lowercase(self):
        """All keywords should be lowercase for consistent matching."""
        for keyword in CRISIS_KEYWORDS:
            self.assertEqual(
                keyword,
                keyword.lower(),
                f"Keyword '{keyword}' should be lowercase",
            )

    def test_keywords_are_unique(self):
        """All keywords should be unique."""
        self.assertEqual(
            len(CRISIS_KEYWORDS),
            len(set(CRISIS_KEYWORDS)),
            "Crisis keywords list contains duplicates",
        )

    def test_keywords_are_specific_first_person(self):
        """
        Keywords should be specific first-person expressions to avoid
        false positives on clinical/insurance terms.
        """
        # All keywords should contain "i" (first person) or be very specific phrases
        for keyword in CRISIS_KEYWORDS:
            has_first_person = "i " in keyword or "i'" in keyword or keyword.startswith("i ")
            is_specific_phrase = len(keyword.split()) >= 3  # Multi-word phrases are more specific
            self.assertTrue(
                has_first_person or is_specific_phrase,
                f"Keyword '{keyword}' may be too general - consider making it more specific",
            )


class TestFalsePromisePatternList(TestCase):
    """Tests for the false promise patterns list."""

    def test_patterns_compile(self):
        """All patterns should be valid regex."""
        import re

        for pattern in FALSE_PROMISE_PATTERNS:
            try:
                re.compile(pattern)
            except re.error as e:
                self.fail(f"Pattern '{pattern}' failed to compile: {e}")

    def test_patterns_are_unique(self):
        """All patterns should be unique."""
        self.assertEqual(
            len(FALSE_PROMISE_PATTERNS),
            len(set(FALSE_PROMISE_PATTERNS)),
            "False promise patterns list contains duplicates",
        )
