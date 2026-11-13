"""
Test setup: a throwaway data folder for the app, a test company signature, and the shared fixtures.

pytest loads this file before any test, so the app's storage (AUTOSIGN_HOME) and the company's
signature image (COMPANY_SIGNATURE_FILE) point at a temporary folder before any autosign module is
imported. Your real storage/ and data/signature.png are never touched by the tests.

Only the tests marked `live` may call Gemini (uv run pytest -m live -s). Every other test runs as if
no GEMINI_API_KEY were set, or with the stand-in AI from the `fake_ai` fixture.
"""

import os
import tempfile
from pathlib import Path

import pytest

TEST_HOME = Path(tempfile.mkdtemp(prefix="autosign-test-"))
os.environ["AUTOSIGN_HOME"] = str(TEST_HOME)
os.environ["COMPANY_SIGNATURE_FILE"] = str(TEST_HOME / "data" / "signature.png")

from autosign import ai, app, storage               # noqa: E402  (must come after AUTOSIGN_HOME is set)
from helpers import agreeing_ai, make_test_signature    # noqa: E402

make_test_signature(storage.COMPANY_SIGNATURE)


@pytest.fixture(scope="session")
def client():
    """Requests as the pages make them."""
    from fastapi.testclient import TestClient
    with TestClient(app.app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def no_real_ai(request, monkeypatch):
    """Outside the live tests, the app behaves as if the AI isn't set up."""
    if "live" not in request.keywords:
        monkeypatch.setattr(ai, "available", lambda: False)


@pytest.fixture
def fake_ai(monkeypatch):
    """Replace the AI with a stand-in that agrees with the rules (where the real model isn't the point)."""
    monkeypatch.setattr(ai, "ask_ai", agreeing_ai)
