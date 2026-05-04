# Voice Intake MVP Notes

- Transcription is intended to run locally in-browser via Moonshine Web (`window.MoonshineWeb`).
- Raw microphone audio is captured in memory only and never posted to the backend.
- Only user-reviewed, user-submitted transcript text is sent to the existing chat WebSocket path.
- If local STT or microphone APIs are unavailable, the UI offers a text-only fallback.
- Media stream tracks are stopped when recording ends and when the component unmounts.
