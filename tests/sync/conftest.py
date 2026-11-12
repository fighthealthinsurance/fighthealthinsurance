"""Sync tests never reach a live Temporal, and read scans with OCR only when asked.

The save paths these tests drive (update_denial, record_intent, deliver)
would connect to whatever TEMPORAL_* the invoking environment names, and tox
passes the environment through. Pinned off here for every sync test; the
temporal tox env is where that integration is exercised.

Reading a shared plan document runs OCR on any page with little or no text,
and a real tesseract run costs a second or two a page. Most sync tests read
short made-up PDFs and are not about OCR, so OCR reads nothing for them; a
test about OCR opts out with @pytest.mark.real_ocr.
"""

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _no_live_temporal():
    with override_settings(
        TEMPORAL_ENABLED=False,
        TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
        TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
        TEMPORAL_CHAT_POLICY_ENABLED=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _ocr_reads_nothing(request, monkeypatch):
    if request.node.get_closest_marker("real_ocr"):
        return

    async def nothing_read(content, pages, *args, **kwargs):
        return {"texts": [[number, ""] for number in pages], "stopped_at": None}

    monkeypatch.setattr("fighthealthinsurance.pdf_text.aocr_pdf_pages", nothing_read)
