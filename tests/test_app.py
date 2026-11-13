"""
The web app end to end: upload the agreement, AutoSign signs for the company, a client signs
through their link, and the company gets the signed PDF and the audit log.
"""

from pathlib import Path

import pymupdf

from autosign import ai, storage, workflow
from helpers import (FIRM, agreeing_ai, confirm_places, make_test_signature, multi_block_doc, new_client_file, paragraph,
                     places_body, sign_as_client, signature_data_url, signature_rows, side_by_side, upload_agreement)


def client_only_doc(folder):
    """An engagement letter with a block for the client only: the firm doesn't sign it."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "CLIENT ENGAGEMENT AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, 'This Agreement is made between Acme Analytics Inc. (the "Firm") and the client named '
                         'below (the "Client"). The Firm will start work once the Client signs.')
    signature_rows(page, 72, 400, "CLIENT", labels=("Client name:", "Signature:", "Date:"))
    path = Path(folder) / "client_only.pdf"
    doc.save(path)
    return str(path)


def signed_pdf(client, agreement):
    """The first client file's signed copy, as the company downloads it."""
    view = client.get(f"/api/agreements/{agreement['token']}").json()
    response = client.get(view["files"][0]["signed_pdf_url"])
    assert response.status_code == 200, response.text
    return pymupdf.open(stream=response.content, filetype="pdf")


def images_on(page):
    return len(page.get_images())


# =============================================================================
# The whole flow
# =============================================================================

def test_company_signs_first_then_the_client_signs(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    assert agreement["company"]["signed_at"], agreement["company"]
    assert agreement["company"]["signer"] == "Dana Reyes"

    file = new_client_file(client, agreement)
    assert file["status"] == "sent"
    token = file["sign_url"].rsplit("/", 1)[1]

    signing = client.get(f"/api/sign/{token}").json()
    assert signing["status"] == "viewed"
    assert signing["company_signed_at"]          # the client sees that the company has already signed
    assert signing["sign_pages"] == [2]

    result = sign_as_client(client, file)
    assert result["ok"]

    doc = signed_pdf(client, agreement)
    assert doc.page_count == 3                   # the agreement's 2 pages and the certificate
    assert images_on(doc[1]) == 2                # the company's signature and the client's
    certificate = doc[2].get_text()
    assert "Signing certificate" in certificate
    assert "Signed for Acme Analytics Inc. by" in certificate
    assert "Jordan Lee" in certificate

    view = client.get(f"/api/agreements/{agreement['token']}").json()
    row = view["files"][0]
    assert row["status"] == "signed"
    assert row["created_at"] and row["viewed_at"] and row["signed_at"]
    assert row["signer_name"] == "Jordan Lee"


def test_every_block_is_signed_in_a_two_part_agreement(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, multi_block_doc(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    assert len(agreement["company"]["spots"]) == 2
    sign_as_client(client, new_client_file(client, agreement))

    doc = signed_pdf(client, agreement)
    assert images_on(doc[1]) == 2
    assert images_on(doc[2]) == 2


def test_each_client_gets_their_own_copy(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    first = new_client_file(client, agreement, name="Foo Holdings LLC", reference="A-1")
    second = new_client_file(client, agreement, name="Bar Partners LP", reference="A-2")
    assert first["sign_url"] != second["sign_url"]

    sign_as_client(client, first, name="Jordan Lee")
    view = client.get(f"/api/agreements/{agreement['token']}").json()
    status_by_client = {row["client_name"]: row["status"] for row in view["files"]}
    assert status_by_client == {"Foo Holdings LLC": "signed", "Bar Partners LP": "sent"}


# =============================================================================
# When AutoSign isn't sure, and changing the places
# =============================================================================

def test_without_the_ai_the_company_confirms_before_any_link(client, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["status"] == "needs_check"
    assert any("AI check didn't run" in reason for reason in agreement["reasons"])
    assert agreement["company"]["signed_at"] is None

    response = client.post(f"/api/agreements/{agreement['token']}/files", json={"client_name": "Foo Holdings LLC"})
    assert response.status_code == 409

    confirmed = confirm_places(client, agreement, places_body(agreement))
    assert confirmed["status"] == "ready"
    assert confirmed["company"]["signed_at"]
    sign_as_client(client, new_client_file(client, confirmed))


def test_when_the_ai_disagrees_the_company_confirms(client, monkeypatch, tmp_path):
    def disagreeing_ai(pdf_path, analysis, firm_name):
        opinion = agreeing_ai(pdf_path, analysis, firm_name)
        client_picks = opinion["answer"]["picks"]
        company_picks = opinion["answer"]["company_picks"]
        opinion["answer"]["picks"] = company_picks          # the AI swaps the two sides
        opinion["answer"]["company_picks"] = client_picks
        return opinion

    monkeypatch.setattr(ai, "ask_ai", disagreeing_ai)
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["status"] == "needs_check"
    assert any("The rules and the AI picked different places" in reason for reason in agreement["reasons"])


def test_the_company_can_choose_not_to_sign(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["company"]["signed_at"]

    confirmed = confirm_places(client, agreement, places_body(agreement, company_spots=[], company_manual=[]))
    assert confirmed["company"]["signed_at"] is None
    assert confirmed["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS

    sign_as_client(client, new_client_file(client, confirmed))
    doc = signed_pdf(client, confirmed)
    assert images_on(doc[1]) == 1                # only the client's signature
    assert "only the client signs" in doc[-1].get_text()


def test_the_company_can_add_its_place_by_hand(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, client_only_doc(tmp_path))
    assert agreement["company"]["spots"] == []
    assert agreement["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS

    by_hand = [{"page": 1, "x": 420, "y": 600, "date": None}]
    confirmed = confirm_places(client, agreement, places_body(agreement, company_manual=by_hand))
    assert confirmed["company"]["signed_at"], confirmed["company"]
    assert confirmed["company"]["manual"][0]["page"] == 1
    assert "Your company signs on page 1 (placed by hand)" in confirmed["summary"]

    sign_as_client(client, new_client_file(client, confirmed))
    doc = signed_pdf(client, confirmed)
    assert images_on(doc[0]) == 2                # the company's signature, placed by hand, and the client's


def test_a_place_is_signed_by_one_side_only(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    client_spot = agreement["client"]["spots"][0]
    body = places_body(agreement, company_spots=[client_spot])
    response = confirm_places(client, agreement, body, expect=422)
    assert "chosen twice" in response["detail"]

    no_client = places_body(agreement, client_spots=[])
    response = confirm_places(client, agreement, no_client, expect=422)
    assert "where the client signs" in response["detail"]


def test_no_block_for_the_company_means_only_the_client_signs(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, client_only_doc(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    assert agreement["company"]["spots"] == []
    assert agreement["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS

    sign_as_client(client, new_client_file(client, agreement))
    doc = signed_pdf(client, agreement)
    assert images_on(doc[0]) == 1
    assert "only the client signs" in doc[-1].get_text()


def test_without_a_company_signature_image_only_the_client_signs(client, fake_ai, tmp_path):
    image = storage.COMPANY_SIGNATURE
    moved = image.with_name("moved-away.png")
    image.rename(moved)
    try:
        agreement = upload_agreement(client, side_by_side(tmp_path))
    finally:
        moved.rename(image)
    assert agreement["company"]["signed_at"] is None
    assert "isn't set up on this server" in agreement["company"]["note"]

    sign_as_client(client, new_client_file(client, agreement))
    doc = signed_pdf(client, agreement)
    assert images_on(doc[1]) == 1


# =============================================================================
# What is refused
# =============================================================================

def test_word_files_and_other_non_pdfs_are_refused(client):
    files = [("file", ("agreement.docx", b"PK\x03\x04 not a pdf", "application/octet-stream"))]
    response = client.post("/api/agreements", files=files, data={"firm_name": FIRM})
    assert response.status_code == 422
    assert "save the agreement as a PDF" in response.json()["detail"]


def test_an_agreement_over_the_page_limit_is_refused(client, tmp_path):
    doc = pymupdf.open()
    for _ in range(101):
        doc.new_page()
    path = tmp_path / "long.pdf"
    doc.save(path)
    response = upload_agreement(client, path, expect=422)
    assert "the limit is 100" in response["detail"]


def test_a_link_is_never_signed_twice(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    file = new_client_file(client, agreement)
    sign_as_client(client, file)
    sign_as_client(client, file, expect=409)


def test_signing_needs_a_name_a_signature_and_consent(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    token = new_client_file(client, agreement)["sign_url"].rsplit("/", 1)[1]
    good = {"signer_name": "Jordan Lee", "signature": signature_data_url(), "consent": True}

    missing_name = dict(good, signer_name=" ")
    no_consent = dict(good, consent=False)
    no_signature = dict(good, signature="not an image")
    for body in (missing_name, no_consent, no_signature):
        assert client.post(f"/api/sign/{token}", json=body).status_code == 422


def test_requests_from_other_websites_are_refused(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    response = client.post(f"/api/agreements/{agreement['token']}/files", json={"client_name": "Foo Holdings LLC"},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_unknown_links_are_a_clear_404(client):
    assert client.get("/api/agreements/not-a-real-token").status_code == 404
    assert client.get("/api/sign/not-a-real-token").status_code == 404


# =============================================================================
# The audit log and checking a copy
# =============================================================================

def test_the_audit_log_records_every_step_and_is_hash_chained(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    sign_as_client(client, new_client_file(client, agreement))

    csv_text = client.get(f"/api/agreements/{agreement['token']}/audit.csv").text
    events = [line.split(",")[2] for line in csv_text.strip().splitlines()[1:]]
    assert events == ["agreement_uploaded", "places_confirmed", "company_signed", "signing_link_created",
                      "link_opened", "signed"]
    assert storage.verify_audit()["ok"]


def test_an_edited_audit_log_is_detected(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    new_client_file(client, agreement)
    original = storage.AUDIT_FILE.read_bytes()
    try:
        storage.AUDIT_FILE.write_bytes(original.replace(b"Foo Holdings LLC", b"Someone Else", 1))
        assert not storage.verify_audit()["ok"]
    finally:
        storage.AUDIT_FILE.write_bytes(original)
    assert storage.verify_audit()["ok"]


def test_names_that_look_like_formulas_stay_text_in_the_audit_log(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    new_client_file(client, agreement, name="=HYPERLINK(\"http://x\")", reference="")
    csv_text = client.get(f"/api/agreements/{agreement['token']}/audit.csv").text
    assert "'=HYPERLINK" in csv_text


def test_verify_recognises_a_signed_copy_and_nothing_else(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    sign_as_client(client, new_client_file(client, agreement))
    view = client.get(f"/api/agreements/{agreement['token']}").json()
    signed_bytes = client.get(view["files"][0]["signed_pdf_url"]).content

    match = client.post("/api/verify", files=[("file", ("copy.pdf", signed_bytes, "application/pdf"))]).json()
    assert match["match"] and match["client_name"] == "Foo Holdings LLC"

    changed = signed_bytes + b"\n% one more byte"
    other = client.post("/api/verify", files=[("file", ("copy.pdf", changed, "application/pdf"))]).json()
    assert not other["match"]


# =============================================================================
# Pages and health
# =============================================================================

def test_every_page_is_served(client):
    for path in ("/", "/a/any-token", "/s/any-token", "/verify"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]


def test_page_pictures_are_served(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    response = client.get(f"/api/agreements/{agreement['token']}/pages/2.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    token = new_client_file(client, agreement)["sign_url"].rsplit("/", 1)[1]
    response = client.get(f"/api/sign/{token}/pages/2.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert client.get(f"/api/sign/{token}/pages/9.png").status_code == 404


def test_health_reports_the_ai_the_company_signature_and_the_log(client):
    health = client.get("/api/health").json()
    assert health["ok"]
    assert health["company_signature"] is True
    assert "configured" in health["ai"]
    assert health["audit_log"]["ok"]


def test_the_test_signature_is_never_your_real_one():
    assert str(storage.COMPANY_SIGNATURE).startswith(str(storage.HOME))
    assert "autosign-test-" in str(storage.HOME)
    make_test_signature(storage.COMPANY_SIGNATURE)          # rewriting it is harmless: it is the tests' own copy
