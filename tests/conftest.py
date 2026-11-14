"""
Test setup: a throwaway data folder for the app, a test signature image, and the shared fixtures.

pytest loads this file before any test, so the app's storage (AUTOSIGN_HOME) points at a temporary
folder before any autosign module is imported. Your real storage/ is never touched by the tests.

Only the tests marked `live` may call Gemini or DocuSeal (uv run pytest -m live -s). Every other test
runs as if neither key were set, or with the stand-ins of the `fake_ai` and `fake_docuseal` fixtures;
a test that would reach the real DocuSeal fails instead.
"""

import os
import tempfile
from pathlib import Path

import pytest

TEST_HOME = Path(tempfile.mkdtemp(prefix="autosign-test-"))
os.environ["AUTOSIGN_HOME"] = str(TEST_HOME)

from autosign import ai, app, docuseal              # noqa: E402  (must come after AUTOSIGN_HOME is set)
from helpers import TEST_SIGNATURE, FakeDocuSeal, agreeing_ai, make_test_signature    # noqa: E402

make_test_signature(TEST_SIGNATURE)


def no_network(*args, **kwargs):
    raise AssertionError("A test tried to reach the real DocuSeal")


@pytest.fixture(scope="session")
def client():
    """Requests as the pages make them."""
    from fastapi.testclient import TestClient
    with TestClient(app.app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def no_real_services(request, monkeypatch):
    """Outside the live tests, the app behaves as if neither the AI nor DocuSeal is set up."""
    if "live" in request.keywords:
        return
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(docuseal, "available", lambda: False)
    monkeypatch.setattr(docuseal, "call", no_network)
    monkeypatch.setattr(docuseal, "download", no_network)


@pytest.fixture
def fake_ai(monkeypatch):
    """Replace the AI with a stand-in that agrees with the rules (where the real model isn't the point)."""
    monkeypatch.setattr(ai, "ask_ai", agreeing_ai)


@pytest.fixture
def fake_docuseal(monkeypatch):
    """Replace DocuSeal's API with an in-memory stand-in. Returns it, so a test can play the client."""
    fake = FakeDocuSeal()
    monkeypatch.setattr(docuseal, "available", lambda: True)
    monkeypatch.setattr(docuseal, "call", fake.call)
    monkeypatch.setattr(docuseal, "download", fake.download)
    return fake
