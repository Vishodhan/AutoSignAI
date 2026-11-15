"""
The web app end to end: the company enters its details and uploads the agreement, AutoSign finds the
places and writes the company's signature in cursive, DocuSeal (a stand-in here) signs each client's
copy for the company, the company checks it, DocuSeal emails the client, the client signs, and the
company gets the signed PDF, the dashboard and the audit log.
"""

import base64
import io
import os
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from autosign import ai, app, docuseal, pipeline, storage, workflow
from autosign.policy import MAX_PAGES
from helpers import (FIRM, TEST_SIGNATURE, agreeing_ai, area_rect, client_signed, confirm_places,
                     date_field_on_line_doc, multi_block_doc, new_client_file, paragraph, places_body, send_file,
                     short_date_line_doc, side_by_side, signature_rows, upload_agreement)


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


def latest_body(fake):
    """What AutoSign sent DocuSeal for the last client file."""
    return fake.submissions[max(fake.submissions)]["body"]


def fields_of(body):
    fields = {}
    for field in body["documents"][0]["fields"]:
        fields[field["name"]] = field
    return fields


def submitters_of(body):
    submitters = {}
    for submitter in body["submitters"]:
        submitters[submitter["role"]] = submitter
    return submitters


def images_on(page):
    return len(page.get_images())


def stored(agreement):
    """The agreement as AutoSign keeps it (with the exact positions of the places)."""
    return storage.agreement_by_token(agreement["token"])


def close(first, second):
    for a, b in zip(first, second):
        if abs(a - b) > 0.2:
            return False
    return True


@pytest.fixture
def check_now(monkeypatch):
    """Every page load asks DocuSeal about the client files (no 15-second wait between two checks)."""
    monkeypatch.setattr(app, "REFRESH_EVERY_SECONDS", 0)


# =============================================================================
# The whole flow
# =============================================================================

def test_the_company_signature_is_the_signer_s_name_in_cursive(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path), signer="Dana Reyes", title="Director",
                                 email="contracts@acme.test")
    assert agreement["signer_name"] == "Dana Reyes"
    assert agreement["signer_title"] == "Director"
    assert agreement["company_email"] == "contracts@acme.test"

    png = client.get(agreement["signature_url"]).content
    image = Image.open(io.BytesIO(png))
    assert image.mode == "RGBA"
    assert image.size == (448, 128)                            # 112 x 32 pt, the largest place, at 4 px per pt
    assert image.getpixel((0, 0))[3] == 0                      # no background
    assert image.getchannel("A").getbbox() is not None         # but ink

    preview = client.get("/api/signature-preview.png", params={"name": "Dana Reyes"})
    assert preview.status_code == 200
    assert preview.content == png                              # the home page shows exactly what is used


def test_the_whole_flow_through_docuseal(client, fake_ai, fake_docuseal, check_now, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    signature = client.get(agreement["signature_url"]).content

    file = new_client_file(client, agreement, name="Acme Inc", email="client@acme.test")
    assert file["status"] == "preview"
    assert file["company_date"] == workflow.date_today(0)
    assert [page["number"] for page in file["preview_pages"]] == [2]

    body = latest_body(fake_docuseal)
    fields = fields_of(body)
    assert list(fields) == ["Company signature 1", "Company date 1", "Company name 1",
                            "Client signature 1", "Client date 1", "Client name 1"]       # no title fields
    assert fields["Client date 1"]["type"] == "date"
    submitters = submitters_of(body)
    company = submitters["Company"]
    assert company["completed"] is True and company["send_email"] is False
    assert base64.b64decode(company["values"]["Company signature 1"]) == signature
    assert company["values"]["Company date 1"] == workflow.date_today(0)
    assert company["values"]["Company name 1"] == "Dana Reyes"
    assert submitters["Client"]["values"] == {"Client name 1": "Acme Inc"}      # the client can correct it
    assert submitters["Client"]["email"] == "client@acme.test"
    assert submitters["Client"]["send_email"] is False and body["send_email"] is False
    assert body["bcc_completed"] == "contracts@acme.test"      # the company gets the signed copy
    assert fake_docuseal.emails == []                           # nothing is emailed before the company checks

    # The fields sit exactly on the places AutoSign found.
    kept = stored(agreement)
    pdf = pymupdf.open(storage.agreement_pdf(kept))
    page = pdf[1]
    assert close(area_rect(page, fields["Client signature 1"]["areas"][0]), kept["placements"][0]["signature"])
    company_place = kept["company"]["placements"][0]["signature"]
    assert close(area_rect(page, fields["Company signature 1"]["areas"][0]), company_place)
    # The date gets its whole line (DocuSeal wraps text that doesn't fit), and the name its Name line.
    assert close(area_rect(page, fields["Company date 1"]["areas"][0]), [370, 263, 540, 277])
    assert close(area_rect(page, fields["Company name 1"]["areas"][0]), [370, 207, 540, 221])
    assert close(area_rect(page, fields["Client name 1"]["areas"][0]), [112, 207, 282, 221])

    # The company checks DocuSeal's copy: its signature is on page 2, the client's place is empty.
    preview = pymupdf.open(stream=client.get(file["preview_pdf_url"]).content, filetype="pdf")
    assert images_on(preview[1]) == 1
    picture = client.get(file["preview_pages"][0]["url"])
    assert picture.headers["content-type"] == "image/png"

    sent = send_file(client, file)
    assert sent["status"] == "sent"
    assert sent["email_sent_at"] == "2026-11-14 15:00:00 UTC"
    assert fake_docuseal.emails == ["client@acme.test"]
    send_file(client, file, expect=409)                         # never emailed twice

    submission_id = max(fake_docuseal.submissions)
    fake_docuseal.client_opens(submission_id)
    row = client.get(f"/api/agreements/{agreement['token']}").json()["files"][0]
    assert row["status"] == "opened" and row["viewed_at"] == "2026-11-14 15:05:00 UTC"

    fake_docuseal.client_signs(submission_id, TEST_SIGNATURE.read_bytes())
    row = client.get(f"/api/agreements/{agreement['token']}").json()["files"][0]
    assert row["status"] == "signed"
    assert row["signed_at"] == "2026-11-14 15:10:00 UTC"
    assert row["signer_name"] == "Acme Inc"
    signed = pymupdf.open(stream=client.get(row["signed_pdf_url"]).content, filetype="pdf")
    assert images_on(signed[1]) == 2                            # the company's signature and the client's

    dashboard = client.get("/api/dashboard").json()
    sent_row = dashboard["files"][0]
    assert sent_row["document"] == "side_by_side.pdf"
    assert sent_row["client_email"] == "client@acme.test"
    assert sent_row["email_sent_at"] == "2026-11-14 15:00:00 UTC"
    assert dashboard["agreements"][0]["emails_sent"] == 1 and dashboard["agreements"][0]["signed"] == 1


def test_the_company_can_cancel_a_copy_it_did_not_like(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    file = new_client_file(client, agreement)
    cancelled = client.post(file["cancel_url"], json={}).json()
    assert cancelled["status"] == "cancelled"
    assert fake_docuseal.submissions[max(fake_docuseal.submissions)]["archived"]
    send_file(client, file, expect=409)
    assert fake_docuseal.emails == []


def test_a_field_docuseal_added_on_its_own_stops_everything(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    fake_docuseal.extra_field = "Print Name"
    response = new_client_file(client, agreement, expect=502)
    assert "don't match" in response["detail"] and "Print Name" in response["detail"]
    assert fake_docuseal.submissions[max(fake_docuseal.submissions)]["archived"]
    assert client.get(f"/api/agreements/{agreement['token']}").json()["files"] == []


def test_without_docuseal_nothing_can_be_sent(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["docuseal_ready"] is False
    response = new_client_file(client, agreement, expect=503)
    assert "DOCUSEAL_API_KEY" in response["detail"]


# =============================================================================
# When AutoSign isn't sure, and changing the places
# =============================================================================

def test_without_the_ai_the_company_confirms_before_any_client(client, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    assert agreement["status"] == "needs_check"
    assert any("AI check didn't run" in reason for reason in agreement["reasons"])
    new_client_file(client, agreement, expect=409)

    confirmed = confirm_places(client, agreement, places_body(agreement))
    assert confirmed["status"] == "ready"
    new_client_file(client, confirmed)


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


def test_the_company_can_choose_not_to_sign(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    confirmed = confirm_places(client, agreement, places_body(agreement, company_spots=[], company_manual=[]))
    assert confirmed["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS

    new_client_file(client, confirmed)
    body = latest_body(fake_docuseal)
    assert list(submitters_of(body)) == ["Client"]
    assert list(fields_of(body)) == ["Client signature 1", "Client date 1", "Client name 1"]


def test_the_company_can_add_its_place_by_hand(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, client_only_doc(tmp_path))
    assert agreement["company"]["spots"] == []
    assert agreement["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS

    by_hand = [{"page": 1, "x": 380, "y": 600, "date": [380, 650]}]
    confirmed = confirm_places(client, agreement, places_body(agreement, company_manual=by_hand))
    assert confirmed["company"]["manual"][0]["page"] == 1
    assert "Your company signs on page 1 (placed by hand)" in confirmed["summary"]

    new_client_file(client, confirmed)
    fields = fields_of(latest_body(fake_docuseal))
    page = pymupdf.open(storage.agreement_pdf(stored(agreement)))[0]
    signature = area_rect(page, fields["Company signature 1"]["areas"][0])
    date = area_rect(page, fields["Company date 1"]["areas"][0])
    # Each box starts exactly where the company clicked: its top-left corner.
    assert abs(signature.x0 - 380) < 0.5 and abs(signature.y0 - 600) < 0.5
    assert abs(date.x0 - 380) < 0.5 and abs(date.y0 - 650) < 0.5


def test_a_place_added_by_hand_near_the_edge_stays_on_the_page(tmp_path):
    pdf_path = client_only_doc(tmp_path)
    page_width = pymupdf.open(pdf_path)[0].rect.width
    placement = pipeline.manual_placement(pdf_path, 1, page_width - 10, 1, None, workflow.SIGNATURE_ASPECT)
    x0, y0, x1, y1 = placement["signature"]
    assert x1 <= page_width - 2 + 0.1
    assert y0 >= 2


def test_every_block_is_signed_in_a_two_part_agreement(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, multi_block_doc(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    new_client_file(client, agreement)
    names = list(fields_of(latest_body(fake_docuseal)))
    for name in ("Company signature 1", "Company signature 2", "Client signature 1", "Client signature 2"):
        assert name in names, names


def test_the_client_s_date_goes_where_the_date_form_field_is(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, date_field_on_line_doc(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    new_client_file(client, agreement)
    date = fields_of(latest_body(fake_docuseal))["Client date 1"]
    page = pymupdf.open(storage.agreement_pdf(stored(agreement)))[0]
    widget = next(widget for widget in page.widgets() if widget.field_name == "ClientDate")
    assert close(area_rect(page, date["areas"][0]), widget.rect)


def test_a_date_that_does_not_fit_is_left_off_and_noted(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, short_date_line_doc(tmp_path, 46))     # any date is wider, even at 9 pt
    assert agreement["status"] == "ready", agreement["reasons"]
    notes = [note for note in agreement["notes"] if "signed without a date" in note]
    assert len(notes) == 1, agreement["notes"]
    new_client_file(client, agreement)
    assert "Client date 1" not in fields_of(latest_body(fake_docuseal))


def test_a_place_is_signed_by_one_side_only(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    client_spot = agreement["client"]["spots"][0]
    body = places_body(agreement, company_spots=[client_spot])
    response = confirm_places(client, agreement, body, expect=422)
    assert "chosen twice" in response["detail"]

    no_client = places_body(agreement, client_spots=[])
    response = confirm_places(client, agreement, no_client, expect=422)
    assert "where the client signs" in response["detail"]


def test_no_block_for_the_company_means_only_the_client_signs(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, client_only_doc(tmp_path))
    assert agreement["status"] == "ready", agreement["reasons"]
    assert agreement["company"]["note"] == workflow.ONLY_THE_CLIENT_SIGNS
    new_client_file(client, agreement)
    assert list(submitters_of(latest_body(fake_docuseal))) == ["Client"]


# =============================================================================
# DocuSeal's areas and requests
# =============================================================================

def test_docuseal_areas_are_fractions_of_the_page_as_it_is_shown():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    area = docuseal.area_on(page, [61.2, 79.2, 183.6, 158.4])
    assert area == {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1, "page": 1}

    page.set_rotation(90)
    rotated = docuseal.area_on(page, [61.2, 79.2, 183.6, 158.4])
    shown = pymupdf.Rect(61.2, 79.2, 183.6, 158.4) * page.rotation_matrix
    assert abs(rotated["x"] - shown.x0 / 792) < 1e-4 and abs(rotated["w"] - shown.width / 792) < 1e-4
    assert rotated["x"] + rotated["w"] <= 1 and rotated["y"] + rotated["h"] <= 1


def test_a_request_that_could_send_twice_is_never_retried():
    assert docuseal.may_retry("POST", status_code=429)          # DocuSeal did nothing: safe
    assert docuseal.may_retry("GET", status_code=503)
    assert not docuseal.may_retry("POST", status_code=503)      # it may have created the submission
    assert not docuseal.may_retry("PUT", status_code=502)       # it may have sent the email


# =============================================================================
# What is refused
# =============================================================================

def test_word_files_and_other_non_pdfs_are_refused(client):
    files = [("file", ("agreement.docx", b"PK\x03\x04 not a pdf", "application/octet-stream"))]
    form = {"firm_name": FIRM, "company_signer": "Dana Reyes", "signer_title": "Director",
            "company_email": "contracts@acme.test"}
    response = client.post("/api/agreements", files=files, data=form)
    assert response.status_code == 422
    assert "save the agreement as a PDF" in response.json()["detail"]


def test_an_agreement_over_the_page_limit_is_refused(client, tmp_path):
    doc = pymupdf.open()
    for _ in range(MAX_PAGES + 1):
        doc.new_page()
    path = tmp_path / "long.pdf"
    doc.save(path)
    response = upload_agreement(client, path, expect=422)
    assert f"the limit is {MAX_PAGES}" in response["detail"]


def test_an_agreement_someone_already_signed_digitally_is_refused(client, fake_ai, tmp_path):
    response = upload_agreement(client, client_signed(tmp_path, "approval"), expect=422)
    assert "already carries a digital signature" in response["detail"]


def test_the_signer_s_name_and_email_are_required_but_not_the_title(client, fake_ai, tmp_path):
    response = upload_agreement(client, side_by_side(tmp_path), email="not an email", expect=422)
    assert "email" in response["detail"]
    response = upload_agreement(client, side_by_side(tmp_path), signer=" ", expect=422)
    assert "person who signs" in response["detail"]
    agreement = upload_agreement(client, side_by_side(tmp_path), title="")
    assert agreement["signer_title"] == ""


def test_the_client_s_email_is_required(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    response = new_client_file(client, agreement, email="jordan at foo", expect=422)
    assert "client's email" in response["detail"]
    assert fake_docuseal.submissions == {}


def test_requests_from_other_websites_are_refused(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    body = {"client_name": "Foo Holdings LLC", "client_email": "jordan@foo.test"}
    response = client.post(f"/api/agreements/{agreement['token']}/files", json=body,
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_unknown_links_are_a_clear_404(client):
    assert client.get("/api/agreements/not-a-real-token").status_code == 404
    assert client.get("/api/files/not-a-real-token/preview.pdf").status_code == 404
    assert client.post("/api/files/not-a-real-token/send", json={}).status_code == 404


# =============================================================================
# The audit log and checking a copy
# =============================================================================

def signed_file(client, fake, agreement):
    """One client file all the way through: prepared, sent, opened and signed. Returns its row."""
    file = new_client_file(client, agreement)
    send_file(client, file)
    submission_id = max(fake.submissions)
    fake.client_opens(submission_id)
    client.get(f"/api/agreements/{agreement['token']}")
    fake.client_signs(submission_id, TEST_SIGNATURE.read_bytes())
    return client.get(f"/api/agreements/{agreement['token']}").json()["files"][0]


def test_the_audit_log_records_every_step_and_is_hash_chained(client, fake_ai, fake_docuseal, check_now, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    signed_file(client, fake_docuseal, agreement)

    csv_text = client.get(f"/api/agreements/{agreement['token']}/audit.csv").text
    events = [line.split(",")[2] for line in csv_text.strip().splitlines()[1:]]
    assert events == ["agreement_uploaded", "signature_made", "places_confirmed", "prepared_in_docuseal",
                      "email_sent", "link_opened", "signed"]
    assert storage.verify_audit()["ok"]


def test_an_edited_audit_log_is_detected(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    new_client_file(client, agreement)
    original = storage.AUDIT_FILE.read_bytes()
    try:
        storage.AUDIT_FILE.write_bytes(original.replace(b"Foo Holdings LLC", b"Someone Else", 1))
        assert not storage.verify_audit()["ok"]
    finally:
        storage.AUDIT_FILE.write_bytes(original)
    assert storage.verify_audit()["ok"]


def test_names_that_look_like_formulas_stay_text_in_the_audit_log(client, fake_ai, fake_docuseal, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    new_client_file(client, agreement, name="=HYPERLINK(\"http://x\")", reference="")
    csv_text = client.get(f"/api/agreements/{agreement['token']}/audit.csv").text
    assert "'=HYPERLINK" in csv_text


def test_verify_recognises_a_signed_copy_and_nothing_else(client, fake_ai, fake_docuseal, check_now, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    row = signed_file(client, fake_docuseal, agreement)
    signed_bytes = client.get(row["signed_pdf_url"]).content

    match = client.post("/api/verify", files=[("file", ("copy.pdf", signed_bytes, "application/pdf"))]).json()
    assert match["match"] and match["client_name"] == "Foo Holdings LLC"

    changed = signed_bytes + b"\n% one more byte"
    other = client.post("/api/verify", files=[("file", ("copy.pdf", changed, "application/pdf"))]).json()
    assert not other["match"]


# =============================================================================
# Pages and health
# =============================================================================

def test_every_page_is_served(client):
    for path in ("/", "/dashboard", "/a/any-token", "/verify"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]


def test_page_pictures_are_served(client, fake_ai, tmp_path):
    agreement = upload_agreement(client, side_by_side(tmp_path))
    response = client.get(f"/api/agreements/{agreement['token']}/pages/2.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert client.get(f"/api/agreements/{agreement['token']}/pages/9.png").status_code == 404


def test_health_reports_the_ai_docuseal_the_font_and_the_log(client):
    health = client.get("/api/health").json()
    assert health["ok"]
    assert "configured" in health["ai"] and "configured" in health["docuseal"]
    assert health["signature_font"] is True
    assert health["audit_log"]["ok"]


# =============================================================================
# Live test with the DocuSeal Sandbox (uv run pytest -m live -s -k docuseal; needs DOCUSEAL_API_KEY in .env)
# =============================================================================



def lines_of_text(page):
    """Each line of text on a page, with its box."""
    found = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            found.append((text, line["bbox"]))
    return found


@pytest.mark.live
def test_live_docuseal_prepares_a_copy_and_emails_it(tmp_path):
    """Creates one Sandbox submission with test data and checks DocuSeal's copy: the company's signature,
    its date on one line, its name on the Name line. Sends ONE signing request only when DOCUSEAL_TEST_EMAIL
    (an address you own, set in .env so it is never committed) is set."""
    if not docuseal.available():
        pytest.skip("DOCUSEAL_API_KEY isn't set")
    test_email = os.environ.get("DOCUSEAL_TEST_EMAIL")
    pdf = side_by_side(tmp_path)
    found = workflow.detect(pdf, FIRM)
    fields = docuseal.fields_for(pdf, found["analysis"], found["client_placements"], found["company"]["placements"])
    signature = workflow.typed_signature_png("Dana Reyes", storage.SIGNATURE_FONT)
    date_text = workflow.date_today()
    values = docuseal.company_values(fields, signature, date_text, "Dana Reyes")
    company = {"name": "Dana Reyes, Director, Acme Analytics Inc.", "values": values}
    client_details = {"name": "Live Test Client", "email": test_email or "", "external_id": "autosign-live-test",
                      "values": docuseal.client_values(fields, "Live Test Client")}
    submission = docuseal.create_submission("AutoSign live test (test data)", Path(pdf).read_bytes(), fields,
                                            company, client_details, "")
    partial = pymupdf.open(stream=docuseal.partly_signed_pdf(submission["id"]), filetype="pdf")
    assert images_on(partial[1]) == 1                           # the company's signature, and only that
    written = lines_of_text(partial[1])
    print()
    for text, box in written:
        if date_text[:3] in text or "2026" in text or "Dana" in text:
            print(f"   {text!r} at {[round(value) for value in box]}")
    assert any(text == date_text for text, _ in written)        # the date on one line
    assert any(text == "Dana Reyes" for text, _ in written)     # the company's name on its Name line

    if not test_email:
        return
    sent_at = docuseal.send(submission["client_submitter_id"])
    state = docuseal.submission_state(submission["id"])
    print(f"submission {submission['id']}: {len(fields)} fields; sent_at {sent_at or state['sent_at']}")
    assert sent_at or state["sent_at"]
