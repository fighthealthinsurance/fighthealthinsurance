from django import forms
from django.test import TestCase
from fighthealthinsurance.form_utils import magic_combined_form, MultipleFileField


class TestFormUtils(TestCase):
    def test_multiple_file_field(self):
        """Test that MultipleFileField is working correctly."""
        field = MultipleFileField()
        self.assertIsNotNone(field)

    def test_magic_combined_form_with_existing_answers(self):
        """Test the magic_combined_form function preserves existing answers."""

        # Create a couple of test forms with some fields
        class TestForm1(forms.Form):
            name = forms.CharField(max_length=100, initial="Default Name")
            age = forms.CharField(initial="", required=False)

        class TestForm2(forms.Form):
            question = forms.CharField(
                max_length=200, initial="What is your favorite color?"
            )
            answer = forms.CharField(max_length=100, initial="", required=False)
            boolean_field = forms.BooleanField(required=False)

        # Create an instance of each form
        form1 = TestForm1()
        form2 = TestForm2()

        # Create a dictionary of existing answers
        existing_answers = {
            "name": "John Doe",  # Should overwrite the initial value
            "age": "38",  # Should set an initial value where there was none
            "boolean_field": "True",  # Should be converted to a boolean True
        }

        # Call the function we're testing
        combined_form = magic_combined_form([form1, form2], existing_answers)

        # Check that the combined form includes all fields
        self.assertIn("name", combined_form.fields)
        self.assertIn("age", combined_form.fields)
        self.assertIn("question", combined_form.fields)
        self.assertIn("answer", combined_form.fields)
        self.assertIn("boolean_field", combined_form.fields)

        # Check that existing answers have been applied correctly
        self.assertEqual(combined_form.fields["name"].initial, "John Doe")
        self.assertEqual(combined_form.fields["age"].initial, "38")
        self.assertEqual(
            combined_form.fields["question"].initial, "What is your favorite color?"
        )
        self.assertEqual(
            combined_form.fields["answer"].initial, ""
        )  # No existing answer
        self.assertTrue(
            combined_form.fields["boolean_field"].initial
        )  # Should be converted to bool

    def test_magic_combined_form_with_generated_questions(self):
        """Test the magic_combined_form function with generated question fields that contain default answers."""

        # Create a form with fields similar to the generated questions
        class AppealQuestionsForm(forms.Form):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # Simulate two questions with default answers
                self.fields["appeal_generated_question_1"] = forms.CharField(
                    label="Question 1?",
                    help_text="Question 1?",
                    required=False,
                    initial="Answer 1",
                )
                self.fields["appeal_generated_question_2"] = forms.CharField(
                    label="Question 2?",
                    help_text="Question 2?",
                    required=False,
                    initial="",  # No default answer
                )

        # Create an instance of the form
        questions_form = AppealQuestionsForm()

        # Create existing answers that should override the initial values
        existing_answers = {
            "appeal_generated_question_1": "New Answer 1",
            "appeal_generated_question_2": "New Answer 2",
        }

        # Call the function we're testing
        combined_form = magic_combined_form([questions_form], existing_answers)

        # Check that the combined form includes the question fields
        self.assertIn("appeal_generated_question_1", combined_form.fields)
        self.assertIn("appeal_generated_question_2", combined_form.fields)

        # Check that existing answers have been applied correctly
        self.assertEqual(
            combined_form.fields["appeal_generated_question_1"].initial, "New Answer 1"
        )
        self.assertEqual(
            combined_form.fields["appeal_generated_question_2"].initial, "New Answer 2"
        )
