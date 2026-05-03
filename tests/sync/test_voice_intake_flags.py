from django.test import TestCase, override_settings


class TestVoiceIntakeFlags(TestCase):
    def _set_consent(self):
        session = self.client.session
        session["consent_completed"] = True
        session.save()

    @override_settings(ENABLE_VOICE_INTAKE=True, ENABLE_LOCAL_STT=True)
    def test_chat_template_shows_voice_flags_enabled(self):
        self._set_consent()
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('data-enable-voice-intake="true"', html)
        self.assertIn('data-enable-local-stt="true"', html)

    @override_settings(ENABLE_VOICE_INTAKE=False, ENABLE_LOCAL_STT=False)
    def test_chat_template_shows_voice_flags_disabled(self):
        self._set_consent()
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('data-enable-voice-intake="false"', html)
        self.assertIn('data-enable-local-stt="false"', html)
