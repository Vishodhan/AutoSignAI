"""
Test setup: a throwaway data folder for the app, a test signature, and the shared fixtures.

pytest loads this file before any test, so the app's data folder (AUTOSIGN_HOME) points at a
temporary folder before any autosign module is imported. Your real storage/, inbox/, output/,
settings.json and data/signature.png are never touched by the tests.
"""

import os
import tempfile
from pathlib import Path

import pytest

TEST_HOME = Path(tempfile.mkdtemp(prefix="autosign-test-"))
os.environ["AUTOSIGN_HOME"] = str(TEST_HOME)

from autosign import ai, app, storage                 # noqa: E402  (must come after AUTOSIGN_HOME is set)
from helpers import agreeing_ai, make_test_signature  # noqa: E402

# The app reads the signature from settings["signature_file"] (data/signature.png) under AUTOSIGN_HOME.
make_test_signature(storage.HOME / storage.DEFAULT_SETTINGS["signature_file"])


@pytest.fixture(scope="session")
def client():
    """Requests as the GUI makes them: to 127.0.0.1:8000, with the X-AutoSign header."""
    from fastapi.testclient import TestClient
    with TestClient(app.app, base_url="http://127.0.0.1:8000", headers={"X-AutoSign": "1"}) as c:
        yield c


@pytest.fixture
def fake_ai(monkeypatch):
    """Replace the AI with a stand-in that agrees with the rules (where the real model isn't the point)."""
    monkeypatch.setattr(ai, "ask_ai", lambda pdf, analysis, settings: agreeing_ai(analysis, settings))
    monkeypatch.setattr(ai, "ai_check_signed", lambda *args: {"ok": True, "detail": "stand-in check"})
