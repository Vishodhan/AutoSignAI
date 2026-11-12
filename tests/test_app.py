"""
The web app: uploads, review actions, the ledger, security and the background worker.

The AI is replaced by a stand-in that agrees with the rules (see the fake_ai fixture).
"""

import io
import os
import time
from pathlib import Path

from autosign import ai, app, pipeline, storage
from helpers import DATA, TEST_SIGNATURE, client_signed, filled_service_contract, multi_block_doc, set_auto_sign, side_by_side, upload, wait_for

def test_api_auto_sign_end_to_end(client, fake_ai, tmp_path):
    set_auto_sign(client, True)
    contract = filled_service_contract(tmp_path, tag="api1")
    job = wait_for(client, upload(client, contract)["job_id"])
    assert job["status"] == "signed", job["reasons"]
    assert job["decision"] == "auto"
    assert job["date_filled"].endswith("in field Date_2")
    pdf = client.get(f"/api/jobs/{job['job_id']}/download")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # The signed copy is also saved in output/ under the original name.
    assert job["output_path"] == str(Path("output") / "service_filledapi1-signed.pdf")
    assert (storage.HOME / job["output_path"]).read_bytes() == pdf.content
    preview = client.get(f"/api/jobs/{job['job_id']}/pages/4/preview?view=signed")
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    # Uploading the same file again, or the signed copy, never signs twice.
    again = upload(client, contract)
    assert again["status"] == "duplicate"
    signed_copy = tmp_path / "signed-copy.pdf"
    signed_copy.write_bytes(pdf.content)
    assert upload(client, signed_copy)["status"] == "duplicate"
    set_auto_sign(client, False)


def test_api_signs_every_block_in_one_go(client, fake_ai, tmp_path):
    set_auto_sign(client, True)
    try:
        job = wait_for(client, upload(client, multi_block_doc(tmp_path, tag="api-multi"))["job_id"])
    finally:
        set_auto_sign(client, False)
    assert job["status"] == "signed", job["reasons"]
    assert job["signed_where"].startswith("p2") and "; p3" in job["signed_where"]
    assert job["date_filled"].count("Nov") + job["date_filled"].count(" at p") >= 2
    assert job["rules_pick"] == job["ai_pick"] and job["agreed"] == "yes"


def test_api_shadow_mode_then_approve(client, fake_ai, tmp_path):
    set_auto_sign(client, False)
    job = wait_for(client, upload(client, side_by_side(tmp_path, "shadow.pdf", tag="shadow"))["job_id"])
    assert job["status"] == "review" and "Shadow mode" in job["reasons"][0]
    detail = client.get(f"/api/jobs/{job['job_id']}").json()
    assert detail["record"]["verdict"]["passed"]
    preview = client.get(f"/api/jobs/{job['job_id']}/pages/2/preview")
    assert preview.status_code == 200
    approved = client.post(f"/api/jobs/{job['job_id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "signed" and approved.json()["decision"] == "approved"
    assert client.post(f"/api/jobs/{job['job_id']}/approve").status_code == 409   # can't sign twice


def test_api_choose_another_spot_and_place_by_hand(client, fake_ai, tmp_path):
    job = wait_for(client, upload(client, str(DATA / "Mutual-NDA-Template-QuickSigner.pdf"))["job_id"])
    assert job["status"] == "review"
    record = client.get(f"/api/jobs/{job['job_id']}").json()["record"]
    party_b = next(s for s in record["analysis"]["spots"] if s["header"] == "PARTY B")
    response = client.post(f"/api/jobs/{job['job_id']}/adjust", json={"spot": party_b["id"], "date": party_b["date_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["signed_where"].startswith("p5")

    manual = wait_for(client, upload(client, side_by_side(tmp_path, "manual.pdf", tag="manual"))["job_id"])
    response = client.post(f"/api/jobs/{manual['job_id']}/adjust",
                           json={"page": 2, "x": 450, "y": 175, "date_x": 372, "date_y": 274})
    assert response.status_code == 200, response.text
    assert "placed by hand" in response.json()["signed_where"]


def test_api_reject_and_rerun(client, fake_ai, tmp_path):
    job = wait_for(client, upload(client, side_by_side(tmp_path, "reject.pdf", tag="reject"))["job_id"])
    rerun = client.post(f"/api/jobs/{job['job_id']}/rerun")
    assert rerun.status_code == 200
    job = wait_for(client, job["job_id"])
    assert job["status"] == "review"
    rejected = client.post(f"/api/jobs/{job['job_id']}/reject", json={"reason": "Wrong client"})
    assert rejected.json()["status"] == "rejected" and rejected.json()["reasons"] == ["Wrong client"]
    assert client.post(f"/api/jobs/{job['job_id']}/rerun").status_code == 409   # a person's rejection stands


def test_api_lock_level_2_can_not_be_approved(client, fake_ai, tmp_path):
    job = wait_for(client, upload(client, client_signed(tmp_path, 2))["job_id"])
    assert job["status"] == "review"
    response = client.post(f"/api/jobs/{job['job_id']}/approve")
    assert response.status_code == 422 and "lock level 2" in response.text


def test_api_lock_level_2_over_the_limit_can_not_be_placed_by_hand(client, fake_ai, tmp_path):
    client.put("/api/settings", json={"max_pages": 1})
    try:
        job = wait_for(client, upload(client, client_signed(tmp_path, 2))["job_id"])
    finally:
        client.put("/api/settings", json={"max_pages": 300})
    assert job["status"] == "review" and "Over the limit" in job["reasons"][0]
    response = client.post(f"/api/jobs/{job['job_id']}/adjust", json={"page": 2, "x": 450, "y": 175})
    assert response.status_code == 422 and "lock level 2" in response.text
    assert not (storage.SIGNED / f"{job['job_id']}.pdf").exists()


def test_api_refuses_requests_from_other_websites(client):
    """Another website open in your browser can't approve, reject or replace your signature."""
    job_url = "/api/jobs/2026-01-01-0001/approve"
    assert client.post(job_url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(job_url, headers={"X-AutoSign": "0"}).status_code == 403          # no Origin, no header
    assert client.get("/api/health", headers={"Host": "evil.example:8000"}).status_code == 403   # DNS rebinding
    assert client.post(job_url, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 404   # our own page: allowed


def test_api_the_same_document_is_never_signed_twice(client, fake_ai, monkeypatch, tmp_path):
    """Job A fails, the same file comes in again as job B and is signed; re-running A must not sign it again."""
    real_analyze, calls = pipeline.analyze, {"n": 0}

    def crash_once(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real_analyze(*args)

    monkeypatch.setattr(pipeline, "analyze", crash_once)
    pdf = side_by_side(tmp_path, "twice.pdf", tag="twice")
    first = wait_for(client, upload(client, pdf)["job_id"])
    assert first["status"] == "failed"
    set_auto_sign(client, True)
    try:
        second = wait_for(client, upload(client, pdf)["job_id"])
        assert second["status"] == "signed", second["reasons"]
        assert client.post(f"/api/jobs/{first['job_id']}/rerun").status_code == 200
        first = wait_for(client, first["job_id"])
    finally:
        set_auto_sign(client, False)
    assert first["status"] == "duplicate" and second["job_id"] in first["reasons"][0]


def test_api_a_copy_the_ai_does_not_confirm_is_never_kept(client, fake_ai, monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "ai_check_signed", lambda *args: {"ok": False, "detail": "simulated mismatch"})
    set_auto_sign(client, True)
    try:
        job = wait_for(client, upload(client, side_by_side(tmp_path, "unconfirmed.pdf", tag="unconfirmed"))["job_id"])
    finally:
        set_auto_sign(client, False)
    assert job["status"] == "review" and "didn't confirm" in job["reasons"][0]
    assert not list(storage.SIGNED.glob(f"{job['job_id']}*"))                      # no signed, pending or work file


def test_api_worker_keeps_going_when_the_daily_export_fails(client, fake_ai, monkeypatch, tmp_path):
    import sqlite3

    def broken_export():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "daily_export", broken_export)
    for n in (1, 2):
        job = wait_for(client, upload(client, side_by_side(tmp_path, f"export{n}.pdf", tag=f"export{n}"))["job_id"])
        assert job["status"] == "review"
    assert client.get("/api/health").json()["worker_running"]


def test_api_failed_job_can_be_rejected(client, fake_ai, monkeypatch, tmp_path):
    def crash(*args):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(pipeline, "analyze", crash)
    job = wait_for(client, upload(client, side_by_side(tmp_path, "fails.pdf", tag="fails"))["job_id"])
    assert job["status"] == "failed"
    rejected = client.post(f"/api/jobs/{job['job_id']}/reject", json={"reason": "Broken file"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


def test_api_preview_of_an_unreadable_file_is_a_clear_404(client):
    response = client.post("/api/documents", files=[("files", ("broken.pdf", b"%PDF-1.7 garbage", "application/pdf"))])
    job = wait_for(client, response.json()["jobs"][0]["job_id"])
    assert job["status"] == "rejected"
    preview = client.get(f"/api/jobs/{job['job_id']}/pages/1/preview")
    assert preview.status_code == 404 and "can't be shown" in preview.text


def test_output_copies_never_overwrite_each_other(tmp_path):
    storage.SIGNED.mkdir(parents=True, exist_ok=True)
    (storage.SIGNED / "T-1.pdf").write_bytes(b"%PDF-1 first")
    (storage.SIGNED / "T-2.pdf").write_bytes(b"%PDF-1 second")
    first, second = app.deliver("T-1", "Same Name.pdf"), app.deliver("T-2", "Same Name.pdf")
    assert first != second
    assert (storage.HOME / first).read_bytes() == b"%PDF-1 first" and (storage.HOME / second).read_bytes() == b"%PDF-1 second"


def test_api_excel_keeps_formula_like_names_as_text(client, fake_ai, tmp_path):
    from openpyxl import load_workbook
    wait_for(client, upload(client, side_by_side(tmp_path, "plain-name.pdf", tag="formula"), name="=1+2.pdf")["job_id"])
    workbook = load_workbook(io.BytesIO(client.get("/api/ledger/export.xlsx").content))
    cells = [row[1] for row in workbook["Ledger"].iter_rows(min_row=2) if row[1].value == "=1+2.pdf"]
    assert cells and all(c.data_type == "s" for c in cells)


def test_api_inbox_folder(client, fake_ai, tmp_path):
    source = side_by_side(tmp_path, "inbox_doc.pdf", tag="inbox")
    target = storage.INBOX / "inbox_doc.pdf"
    target.write_bytes(Path(source).read_bytes())
    os.utime(target, (time.time() - 10, time.time() - 10))     # as if it was copied in a while ago
    response = client.post("/api/inbox/process").json()
    assert [j["status"] for j in response["jobs"]] == ["queued"]
    assert not target.exists() and (storage.PROCESSED / "inbox_doc.pdf").exists()
    assert wait_for(client, response["jobs"][0]["job_id"])["status"] == "review"


def test_api_refuses_files_that_are_not_pdfs(client):
    response = client.post("/api/documents", files=[("files", ("notes.pdf", b"hello", "application/pdf"))])
    assert response.json()["jobs"][0]["status"] == "refused"


def test_api_settings_are_validated(client):
    assert client.put("/api/settings", json={"date_format": "no date here"}).status_code == 422
    assert client.put("/api/settings", json={"auto_sign_types": ["lease", "poem"]}).status_code == 422
    assert client.put("/api/settings", json={"company_name": "  "}).status_code == 422
    good = client.put("/api/settings", json={"date_format": "%d/%m/%Y"})
    assert good.status_code == 200 and good.json()["date_example"].count("/") == 2
    client.put("/api/settings", json={"date_format": "%b %d, %Y"})


def test_api_signature_upload(client):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (300, 100), "white")
    ImageDraw.Draw(image).line([(10, 80), (150, 20), (290, 70)], fill="black", width=5)
    raw = io.BytesIO()
    image.save(raw, "PNG")
    original = TEST_SIGNATURE.read_bytes()
    before = client.get("/api/settings").json()["signature"]["sha256"]
    response = client.post("/api/settings/signature", files={"file": ("sig.png", raw.getvalue(), "image/png")})
    assert response.status_code == 200 and response.json()["signature"]["sha256"] != before
    backups = list(storage.SIGNATURE_BACKUPS.glob("*-signature.png"))
    assert backups and backups[-1].read_bytes() == original           # the old image was kept
    bad = client.post("/api/settings/signature", files={"file": ("sig.png", b"not an image", "image/png")})
    assert bad.status_code == 422
    TEST_SIGNATURE.write_bytes(original)                               # put the test signature back


def test_api_no_signature_image_means_no_signing(client, fake_ai, tmp_path):
    """Without a signature image nothing is signed: there is no stand-in signature."""
    hidden = TEST_SIGNATURE.with_name("moved-away.png")
    TEST_SIGNATURE.rename(hidden)
    try:
        assert client.get("/api/health").json()["signature"]["found"] is False
        job = wait_for(client, upload(client, side_by_side(tmp_path, "nosig.pdf", tag="nosig"))["job_id"])
        assert job["status"] == "review" and any("No signature image" in r for r in job["reasons"])
        response = client.post(f"/api/jobs/{job['job_id']}/approve")
        assert response.status_code == 422 and "No signature image" in response.text
    finally:
        hidden.rename(TEST_SIGNATURE)


def test_api_ledger_excel_and_audit_log(client):
    from openpyxl import load_workbook
    export = client.get("/api/ledger/export.xlsx")
    assert export.status_code == 200
    workbook = load_workbook(io.BytesIO(export.content))
    assert workbook.sheetnames == ["Ledger", "Auto-signed"]
    assert workbook["Ledger"]["A1"].value == "Job ID" and workbook["Ledger"].max_row > 1
    assert client.get("/api/audit/verify").json()["ok"]
    # Quietly editing a past audit line breaks the chain, and the check finds it.
    storage.db_execute("UPDATE audit SET detail = '{\"reason\": \"edited\"}' WHERE seq = 2")
    result = client.get("/api/audit/verify").json()
    assert not result["ok"] and result["broken_at"] == 2


def test_api_summary_and_health(client):
    summary = client.get("/api/summary").json()
    assert summary["all_time"]["total"] > 0 and summary["recent"]
    health = client.get("/api/health").json()
    assert health["app"] == "ok" and health["worker_running"]
    assert client.get("/").status_code == 200
