"""
The web app: the pages and the REST API.

There are no accounts: AutoSign is used by one company, on a link shared only with it.
    /             enter your company's details and upload the agreement
    /dashboard    every agreement, and every client it was emailed to
    /a/<token>    one agreement: where everyone signs, and its client files
    /verify       check whether a PDF is a copy signed through AutoSign

The flow:
    1. The company enters its name, the signer's name and job title, and its email, and uploads its
       agreement (PDF). AutoSign writes the signer's name in cursive: the company's signature.
    2. AutoSign finds the client's and the company's places (rules, checked by Gemini). When it
       isn't sure, the company checks the places on the agreement page and confirms them.
    3. For each client, the company enters the client's name and email. AutoSign sends DocuSeal the
       agreement with a field on every place: the company's are signed with its cursive signature and
       today's date, the client's are left empty. The company checks the copy DocuSeal prepared.
    4. Once the company confirms, DocuSeal emails the client, who signs on DocuSeal's page. DocuSeal
       emails the signed copy to the client and to the company; AutoSign keeps a copy, and the
       dashboard shows when each email went out and where each client is.

Run it locally with `uv run autosign` (http://127.0.0.1:8000). API documentation: /docs.
"""

import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def load_dotenv(path=Path(".env")):
    """Read the .env file of the project folder (the Gemini and DocuSeal keys) into the environment.

    On Railway the variables are set in the service's Variables tab and there is no .env file, so
    this does nothing there. Values already in the environment always win. Nothing read here is
    ever printed or logged.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


load_dotenv()           # before the modules below read their settings from the environment

import uvicorn                                                              # noqa: E402
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile   # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response         # noqa: E402
from fastapi.staticfiles import StaticFiles                                # noqa: E402

from autosign import ai, docuseal, storage, workflow                       # noqa: E402
from autosign.policy import MAX_MB, MAX_PAGES                              # noqa: E402

HEAVY = threading.BoundedSemaphore(2)      # reading documents and preparing them in DocuSeal: two at a time
SENDING = threading.Lock()                 # one send or cancel at a time, so a client is never emailed twice
REFRESHING = threading.Lock()              # one status check with DocuSeal at a time
REFRESH_EVERY_SECONDS = 15                 # a client file is checked with DocuSeal at most this often...
REFRESH_AT_MOST = 10                       # ...and at most this many per page load (the Sandbox's limits)
WAITING_FOR_THE_CLIENT = ("sent", "opened")
EMAIL_PATTERN = re.compile(r"^[^@\s<>\"',;:]+@[^@\s<>\"',;:]+\.[A-Za-z]{2,}$")


@asynccontextmanager
async def lifespan(app):
    storage.init()
    yield


app = FastAPI(title="AutoSign", version="2.0.0", lifespan=lifespan,
              description="Upload your client agreement once. For each client, AutoSign signs it for your company "
                          "through DocuSeal; you check the copy, DocuSeal emails the client, the client signs, "
                          "and you get the signed PDF and an audit log.")
app.mount("/static", StaticFiles(directory=storage.STATIC), name="static")


@app.middleware("http")
async def guard(request, call_next):
    """Refuse changes sent from other websites, and keep link secrets out of Referer headers and search engines."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Requests from other websites are refused"}, status_code=403)
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# =============================================================================
# Pages
# =============================================================================

def page(name):
    return FileResponse(storage.STATIC / name, media_type="text/html")


@app.get("/", include_in_schema=False)
def home_page():
    return page("home.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return page("dashboard.html")


@app.get("/a/{token}", include_in_schema=False)
def agreement_page(token: str):
    return page("agreement.html")


@app.get("/verify", include_in_schema=False)
def verify_page():
    return page("verify.html")


# =============================================================================
# Helpers
# =============================================================================

def client_ip(request):
    if request.client:
        return request.client.host
    return ""


def clean_text(value, limit):
    """One line of text from a form: spaces collapsed, cut to `limit` characters."""
    return " ".join(str(value or "").split())[:limit]


def clean_email(value):
    """An email address from a form, or None when it doesn't look like one."""
    email = clean_text(value, 200)
    if EMAIL_PATTERN.match(email):
        return email
    return None


def find_agreement(token):
    agreement = storage.agreement_by_token(token)
    if agreement is None:
        raise HTTPException(404, "This agreement link doesn't exist. Check that you copied the whole link.")
    return agreement


def find_file(token):
    file = storage.file_by("manage_token", token)
    if file is None:
        raise HTTPException(404, "This client file doesn't exist. Check that you copied the whole link.")
    return file


def png_response(png):
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


def pdf_response(path, filename, download):
    disposition = "inline"
    if download:
        disposition = "attachment"
    return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type=disposition)


def safe_filename(text):
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", text).strip()[:80]
    return cleaned or "agreement"


def file_pdf_name(agreement, file, words):
    stem = safe_filename(Path(agreement["filename"]).stem)
    client = safe_filename(file["client_name"])
    return f"{stem} - {words} {client}.pdf"


def describe_pages(placements):
    pages = []
    for placement in placements:
        pages.append(f"page {placement['page']}")
    return "; ".join(pages)


def read_upload(upload):
    data = upload.file.read(MAX_MB * 1024 * 1024 + 1)
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"The file is bigger than {MAX_MB} MB")
    if not data:
        raise HTTPException(422, "The file is empty")
    return data


def read_page_count(path):
    try:
        pages = workflow.page_count(path)
    except Exception:
        raise HTTPException(422, "The file can't be opened as a PDF")
    if pages > MAX_PAGES:
        raise HTTPException(422, f"The agreement has {pages} pages; the limit is {MAX_PAGES}")
    return pages


def company_placements(agreement):
    company = agreement["company"] or {}
    return company.get("placements") or []


def signing_pages(agreement):
    """The pages with a place to sign, for either side."""
    pages = set()
    for placement in (agreement["placements"] or []) + company_placements(agreement):
        pages.add(placement["page"])
    return sorted(pages)


def signer_line(agreement):
    """'Dana Reyes, Director, Acme Analytics Inc.': who signs for the company, as DocuSeal records it."""
    parts = [agreement["signer_name"], agreement["signer_title"], agreement["firm_name"]]
    return ", ".join(part for part in parts if part)


# =============================================================================
# What the pages are sent
# =============================================================================

def file_summary(file, agreement):
    """One client file, as the agreement page and the dashboard show it."""
    token = file["manage_token"]
    summary = {
        "document": agreement["filename"],
        "agreement_url": f"/a/{agreement['token']}",
        "client_name": file["client_name"],
        "client_email": file["client_email"],
        "reference": file["reference"],
        "status": file["status"],
        "created_at": file["created_at"],
        "company_date": file["company_date"],
        "email_sent_at": file["email_sent_at"],
        "viewed_at": file["viewed_at"],
        "signed_at": file["signed_at"],
        "signer_name": file["signer_name"],
        "preview_pdf_url": None,
        "preview_pages": [],
        "signed_pdf_url": None,
        "send_url": f"/api/files/{token}/send",
        "cancel_url": f"/api/files/{token}/cancel",
    }
    if file["status"] == "preview":
        summary["preview_pdf_url"] = f"/api/files/{token}/preview.pdf"
        pages = []
        for number in signing_pages(agreement):
            pages.append({"number": number, "url": f"/api/files/{token}/preview/{number}.png"})
        summary["preview_pages"] = pages
    if file["status"] == "signed":
        summary["signed_pdf_url"] = f"/api/files/{token}/signed.pdf"
    return summary


def spot_summary(spot):
    return {"id": spot["id"], "page": spot["page"], "label": spot["label"], "header": spot.get("header"),
            "fits": spot["placement"]["fits"]}


def places_summary(places):
    """A side's places as the agreement page edits them: the spot ids, and the places added by hand."""
    return {"spots": workflow.chosen_spot_ids(places), "manual": places.get("manual") or []}


def agreement_view(agreement):
    analysis = agreement["analysis"]
    company = agreement["company"] or {}
    spots = []
    for spot in analysis["spots"]:
        if spot["kind"] == "signature":
            spots.append(spot_summary(spot))
    files = []
    for file in storage.files_for(agreement["id"]):
        files.append(file_summary(file, agreement))
    company_view = places_summary(company)
    company_view["note"] = company.get("note")
    notes = analysis["notes"] + workflow.date_notes(agreement["placements"] or [], company_placements(agreement))
    return {
        "token": agreement["token"],
        "filename": agreement["filename"],
        "firm_name": agreement["firm_name"],
        "signer_name": agreement["signer_name"],
        "signer_title": agreement["signer_title"],
        "company_email": agreement["company_email"],
        "signature_url": f"/api/agreements/{agreement['token']}/signature.png",
        "created_at": agreement["created_at"],
        "page_count": analysis["page_count"],
        "pages": analysis["pages"],
        "status": agreement["status"],
        "summary": agreement["summary"],
        "reasons": agreement["reasons"] or [],
        "notes": notes,
        "spots": spots,
        "client": places_summary(agreement["proposal"] or {}),
        "company": company_view,
        "files": files,
        "docuseal_ready": docuseal.available(),
    }


def agreement_row(agreement, files):
    """One agreement, as the dashboard lists it."""
    sent = 0
    signed = 0
    for file in files:
        if file["email_sent_at"]:
            sent += 1
        if file["status"] == "signed":
            signed += 1
    return {"filename": agreement["filename"], "firm_name": agreement["firm_name"],
            "signer_name": agreement["signer_name"], "signer_title": agreement["signer_title"],
            "company_email": agreement["company_email"], "created_at": agreement["created_at"],
            "status": agreement["status"], "url": f"/a/{agreement['token']}", "emails_sent": sent, "signed": signed}


# =============================================================================
# Where each client is: asked of DocuSeal when a page loads
# =============================================================================

def seconds_since(text):
    """Seconds since a time AutoSign wrote ('2026-11-14 07:29:17 UTC'); a large number when there is none."""
    if not text:
        return 10 ** 9
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return 10 ** 9
    return (datetime.now(timezone.utc) - moment).total_seconds()


def record_signed(file, agreement, state, updates):
    """The client signed: keep DocuSeal's signed PDF and its fingerprint."""
    pdf = docuseal.signed_pdf(file["submission_id"])
    storage.signed_pdf(file).write_bytes(pdf)
    sha = storage.fingerprint(pdf)
    signed_at = state["completed_at"] or storage.now_text()
    signer = state["signer_name"] or file["client_name"]
    updates.update(status="signed", signed_at=signed_at, signer_name=signer, signed_sha256=sha)
    detail = (f"signed on DocuSeal at {signed_at}; DocuSeal emails the signed copy to {file['client_email']} "
              f"and to {agreement['company_email']}; AutoSign keeps a copy")
    storage.audit("signed", agreement=agreement, file=file, actor=signer, detail=detail, sha256=sha)


def refresh_file(file):
    """Ask DocuSeal where one client file is, and record what changed."""
    state = docuseal.submission_state(file["submission_id"])
    agreement = storage.agreement_by_id(file["agreement_id"])
    updates = {"checked_at": storage.now_text()}
    if state["sent_at"] and not file["email_sent_at"]:
        updates["email_sent_at"] = state["sent_at"]
    if state["opened_at"] and not file["viewed_at"]:
        updates["viewed_at"] = state["opened_at"]
        updates["status"] = "opened"
        storage.audit("link_opened", agreement=agreement, file=file, actor="the client",
                      detail=f"DocuSeal reports the client opened the signing page at {state['opened_at']}")
    if state["declined_at"]:
        updates["status"] = "declined"
        storage.audit("declined", agreement=agreement, file=file, actor="the client",
                      detail=f"DocuSeal reports the client declined to sign at {state['declined_at']}")
    elif state["completed"]:
        record_signed(file, agreement, state, updates)
    storage.update_file(file["id"], **updates)


def refresh_statuses(files):
    """Ask DocuSeal about the client files still waiting for the client: each one at most every
    REFRESH_EVERY_SECONDS, and at most REFRESH_AT_MOST per call. A failure keeps the last known status."""
    if not docuseal.available():
        return
    if not REFRESHING.acquire(blocking=False):
        return
    try:
        checked = 0
        for file in files:
            if checked >= REFRESH_AT_MOST:
                break
            if file["status"] not in WAITING_FOR_THE_CLIENT or not file["submission_id"]:
                continue
            if seconds_since(file["checked_at"]) < REFRESH_EVERY_SECONDS:
                continue
            checked += 1
            try:
                refresh_file(file)
            except docuseal.DocuSealError:
                storage.update_file(file["id"], checked_at=storage.now_text())
    finally:
        REFRESHING.release()


# =============================================================================
# Agreements (the company's side)
# =============================================================================

def company_details(firm_name, company_signer, signer_title, company_email):
    """The company's details from the upload form, checked. Raises HTTPException(422) with what to fix."""
    firm_name = clean_text(firm_name, 120)
    if len(firm_name) < 2:
        raise HTTPException(422, "Enter your company's name, as it appears in the agreement")
    signer = clean_text(company_signer, 100)
    if len(signer) < 2:
        raise HTTPException(422, "Enter the full name of the person who signs for your company")
    title = clean_text(signer_title, 80)          # optional: kept in DocuSeal's record, never written on the PDF
    email = clean_email(company_email)
    if email is None:
        raise HTTPException(422, "Enter your company's email address (DocuSeal sends the signed copies there)")
    return firm_name, signer, title, email


@app.post("/api/agreements", summary="Your company's details and its agreement (PDF): find the places, make the signature")
def upload_agreement(request: Request, file: UploadFile = File(...), firm_name: str = Form(...),
                     company_signer: str = Form(...), company_email: str = Form(...), signer_title: str = Form("")):
    firm_name, signer, title, email = company_details(firm_name, company_signer, signer_title, company_email)
    filename = Path(file.filename or "agreement.pdf").name[:150]
    pdf = read_upload(file)
    if b"%PDF-" not in pdf[:1024]:
        raise HTTPException(422, "Upload the agreement as a PDF file. Word files aren't accepted: "
                                 "save the agreement as a PDF first.")
    try:
        signature = workflow.typed_signature_png(signer, storage.SIGNATURE_FONT)
    except ValueError as error:
        raise HTTPException(500, f"Your company's signature can't be made: {error}")

    with HEAVY:
        work = storage.AGREEMENTS / f"upload-{storage.new_token()}.pdf"
        work.write_bytes(pdf)
        try:
            pages = read_page_count(work)
            found = workflow.detect(str(work), firm_name)
        except ValueError as error:
            work.unlink(missing_ok=True)
            raise HTTPException(422, f"This agreement can't be used: {error}")
        except Exception:
            work.unlink(missing_ok=True)        # the half-uploaded copy; the company's own file is untouched
            raise

    sure = not found["reasons"]
    status = "needs_check"
    if sure:
        status = "ready"
    company = dict(found["company"], signer=signer)
    agreement_id = storage.create_agreement(
        filename=filename, firm_name=firm_name, signer_name=signer, signer_title=title, company_email=email,
        sha256=storage.fingerprint(pdf), analysis=found["analysis"], ai=found["ai"], proposal=found["client"],
        placements=found["client_placements"], reasons=found["reasons"], summary=found["summary"], status=status,
        company=company)
    agreement = storage.agreement_by_id(agreement_id)
    os.replace(work, storage.agreement_pdf(agreement))
    storage.signature_png(agreement).write_bytes(signature)

    ip = client_ip(request)
    storage.audit("agreement_uploaded", agreement=agreement, actor=signer_line(agreement), ip=ip,
                  detail=f"{pages} page(s); company email {email}", sha256=agreement["sha256"])
    storage.audit("signature_made", agreement=agreement, actor="AutoSign", ip=ip,
                  detail=f"the name '{signer}' written in cursive, as {firm_name}'s signature",
                  sha256=storage.fingerprint(signature))
    if sure:
        storage.audit("places_confirmed", agreement=agreement, actor="AutoSign (rules and AI agree)",
                      detail=found["summary"])
    return {"token": agreement["token"], "url": f"/a/{agreement['token']}", "ready": sure}


@app.get("/api/signature-preview.png", summary="A name written in cursive, as your company's signature would look")
def signature_preview(name: str = ""):
    name = clean_text(name, 100)
    if len(name) < 2:
        raise HTTPException(422, "Enter the signer's full name")
    try:
        return png_response(workflow.typed_signature_png(name, storage.SIGNATURE_FONT))
    except ValueError as error:
        raise HTTPException(500, f"The signature can't be made: {error}")


@app.get("/api/agreements/{token}", summary="The agreement, where everyone signs, and its client files")
def get_agreement(token: str):
    agreement = find_agreement(token)
    refresh_statuses(storage.files_for(agreement["id"]))
    return agreement_view(agreement)


@app.get("/api/agreements/{token}/pages/{number}.png", summary="A page with the signature places drawn in")
def agreement_page_image(token: str, number: int):
    agreement = find_agreement(token)
    try:
        png = workflow.render_for_company(storage.agreement_pdf(agreement), number, agreement["analysis"],
                                          agreement["proposal"] or {}, agreement["placements"] or [],
                                          agreement["company"] or {})
    except ValueError as error:
        raise HTTPException(404, str(error))
    return png_response(png)


@app.get("/api/agreements/{token}/signature.png", summary="Your company's signature: the signer's name in cursive")
def agreement_signature(token: str):
    agreement = find_agreement(token)
    path = storage.signature_png(agreement)
    if not path.exists():
        raise HTTPException(404, "This agreement has no cursive signature")
    return png_response(path.read_bytes())


@app.put("/api/agreements/{token}/places", summary="Confirm (or change) where the client and your company sign")
def confirm_places(request: Request, token: str, body: dict = Body(...)):
    """The body: {"client": places, "company": places}, where places are
    {"choices": [{"spot": id}], "manual": [{"page", "x", "y", "date": [x, y] or null}]}."""
    agreement = find_agreement(token)
    pdf = str(storage.agreement_pdf(agreement))
    analysis = agreement["analysis"]
    try:
        client, client_placements, company_places, placements = workflow.check_places(analysis, body, pdf)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, f"These places can't be used: {error}")

    company = dict(agreement["company"] or {}, choices=company_places["choices"], manual=company_places["manual"],
                   placements=placements, note=None)
    if not workflow.has_places(company):
        company["note"] = workflow.ONLY_THE_CLIENT_SIGNS
    summary = workflow.summarize(analysis, client, company)
    storage.update_agreement(agreement["id"], proposal=client, placements=client_placements, company=company,
                             summary=summary, status="ready")
    storage.audit("places_confirmed", agreement=agreement, actor="the company", ip=client_ip(request), detail=summary)
    return agreement_view(storage.agreement_by_id(agreement["id"]))


@app.get("/api/agreements/{token}/audit.csv", summary="The audit log rows for this agreement and its files")
def agreement_audit(token: str):
    agreement = find_agreement(token)
    csv_text = storage.audit_csv(storage.audit_rows(agreement["id"]))
    name = safe_filename(Path(agreement["filename"]).stem)
    return Response(csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name} - audit log.csv"'})


# =============================================================================
# Client files: prepared in DocuSeal, checked by the company, then emailed to the client
# =============================================================================

def company_signature(agreement):
    """The company's cursive signature for this agreement (made now for an agreement uploaded before signatures were)."""
    path = storage.signature_png(agreement)
    if not path.exists():
        name = agreement["signer_name"] or (agreement["company"] or {}).get("signer") or agreement["firm_name"]
        path.write_bytes(workflow.typed_signature_png(name, storage.SIGNATURE_FONT))
    return path.read_bytes()


def prepare_in_docuseal(agreement, client_name, client_email, date_text, external_id):
    """A DocuSeal submission for one client: the company's places signed, the client's empty, nothing emailed.
    Returns the submission's ids and the copy DocuSeal prepared. Raises docuseal.DocuSealError."""
    pdf_path = storage.agreement_pdf(agreement)
    placements = company_placements(agreement)
    fields = docuseal.fields_for(str(pdf_path), agreement["analysis"], agreement["placements"] or [], placements)
    company = None
    if placements:
        signer = agreement["signer_name"] or (agreement["company"] or {}).get("signer") or agreement["firm_name"]
        values = docuseal.company_values(fields, company_signature(agreement), date_text, signer)
        company = {"name": signer_line(agreement), "values": values}
    client = {"name": client_name, "email": client_email, "external_id": external_id,
              "values": docuseal.client_values(fields, client_name)}
    submission = docuseal.create_submission(agreement["filename"], pdf_path.read_bytes(), fields, company, client,
                                            agreement["company_email"])
    try:
        pdf = docuseal.partly_signed_pdf(submission["id"])
    except docuseal.DocuSealError:
        docuseal.cancel_quietly(submission["id"])
        raise
    return {"id": submission["id"], "client_submitter_id": submission["client_submitter_id"], "pdf": pdf,
            "fields": len(fields)}


@app.post("/api/agreements/{token}/files",
          summary="Prepare a client's copy in DocuSeal: your company's signature on it, nothing emailed yet")
def create_file(request: Request, token: str, body: dict = Body(...)):
    agreement = find_agreement(token)
    if agreement["status"] != "ready":
        raise HTTPException(409, "Confirm where the client signs before sending the agreement to a client")
    problems = workflow.overlap_problems(agreement["analysis"], agreement["placements"] or [],
                                         company_placements(agreement))
    if problems:
        raise HTTPException(409, f"Two places would overlap ({problems[0]}). Check the places below first.")
    client_name = clean_text(body.get("client_name"), 120)
    if len(client_name) < 2:
        raise HTTPException(422, "Enter the client's name")
    client_email = clean_email(body.get("client_email"))
    if client_email is None:
        raise HTTPException(422, "Enter the client's email address")
    reference = clean_text(body.get("reference"), 80)
    if not docuseal.available():
        raise HTTPException(503, "DocuSeal isn't set up on this server: add DOCUSEAL_API_KEY to the .env file")

    date_text = workflow.date_today(body.get("tz_offset_minutes", 0))
    manage_token = storage.new_token()
    with HEAVY:
        try:
            prepared = prepare_in_docuseal(agreement, client_name, client_email, date_text, manage_token)
        except docuseal.DocuSealError as error:
            raise HTTPException(502, str(error))
    file_id = storage.create_file(agreement_id=agreement["id"], manage_token=manage_token, client_name=client_name,
                                  client_email=client_email, reference=reference, status="preview",
                                  submission_id=str(prepared["id"]),
                                  client_submitter_id=str(prepared["client_submitter_id"]), company_date=date_text)
    file = storage.file_by("id", file_id)
    storage.preview_pdf(file).write_bytes(prepared["pdf"])

    signed_for_company = "only the client signs"
    if company_placements(agreement):
        signed_for_company = (f"signed for {agreement['firm_name']} by {agreement['signer_name']} on "
                              f"{describe_pages(company_placements(agreement))}; date written: {date_text}")
    detail = (f"DocuSeal submission {prepared['id']} for {client_email}: {signed_for_company}; "
              f"{prepared['fields']} field(s), exactly as AutoSign sent them; nothing emailed yet")
    storage.audit("prepared_in_docuseal", agreement=agreement, file=file, actor=agreement["firm_name"],
                  ip=client_ip(request), detail=detail, sha256=storage.fingerprint(prepared["pdf"]))
    return file_summary(file, agreement)


@app.get("/api/files/{token}/preview.pdf", summary="The copy DocuSeal prepared, with your company's signature")
def file_preview_pdf(token: str, download: int = 0):
    file = find_file(token)
    path = storage.preview_pdf(file)
    if not path.exists():
        raise HTTPException(404, "There is no prepared copy for this client file")
    agreement = storage.agreement_by_id(file["agreement_id"])
    return pdf_response(path, file_pdf_name(agreement, file, "prepared for"), download)


@app.get("/api/files/{token}/preview/{number}.png", summary="A page of the copy DocuSeal prepared")
def file_preview_page(token: str, number: int):
    file = find_file(token)
    path = storage.preview_pdf(file)
    if not path.exists():
        raise HTTPException(404, "There is no prepared copy for this client file")
    try:
        png = workflow.render_plain(path, number)
    except ValueError as error:
        raise HTTPException(404, str(error))
    return png_response(png)


@app.post("/api/files/{token}/send", summary="Your company's signature looks right: DocuSeal emails the client")
def send_file(request: Request, token: str):
    with SENDING:
        file = find_file(token)
        if file["status"] != "preview":
            raise HTTPException(409, "This client file was already sent, or it was cancelled")
        agreement = storage.agreement_by_id(file["agreement_id"])
        try:
            sent_at = docuseal.send(file["client_submitter_id"])
        except docuseal.DocuSealError as error:
            raise HTTPException(502, str(error))
        email_sent_at = sent_at or storage.now_text()
        storage.update_file(file["id"], status="sent", email_sent_at=email_sent_at)
        detail = (f"your company checked its signature; DocuSeal emailed the signing request to {file['client_email']} "
                  f"at {email_sent_at}")
        storage.audit("email_sent", agreement=agreement, file=file, actor=agreement["firm_name"],
                      ip=client_ip(request), detail=detail)
    return file_summary(storage.file_by("id", file["id"]), agreement)


@app.post("/api/files/{token}/cancel", summary="Don't send this copy: DocuSeal archives it")
def cancel_file(request: Request, token: str):
    with SENDING:
        file = find_file(token)
        if file["status"] != "preview":
            raise HTTPException(409, "Only a copy that hasn't been sent can be cancelled")
        agreement = storage.agreement_by_id(file["agreement_id"])
        try:
            docuseal.cancel(file["submission_id"])
        except docuseal.DocuSealError as error:
            raise HTTPException(502, str(error))
        storage.update_file(file["id"], status="cancelled")
        storage.audit("cancelled", agreement=agreement, file=file, actor=agreement["firm_name"],
                      ip=client_ip(request), detail=f"not sent; DocuSeal submission {file['submission_id']} archived")
    return file_summary(storage.file_by("id", file["id"]), agreement)


@app.get("/api/files/{token}/signed.pdf", summary="A client file's signed PDF (?download=1 to save)")
def file_signed_pdf(token: str, download: int = 0):
    file = find_file(token)
    if file["status"] != "signed":
        raise HTTPException(409, "This agreement hasn't been signed yet")
    agreement = storage.agreement_by_id(file["agreement_id"])
    return pdf_response(storage.signed_pdf(file), file_pdf_name(agreement, file, "signed by"), download)


@app.get("/api/dashboard", summary="Every agreement, and every client file: when its email went out, and to whom")
def dashboard():
    refresh_statuses(storage.all_files())
    agreements = storage.all_agreements()
    by_id = {agreement["id"]: agreement for agreement in agreements}
    files_by_agreement = {}
    files = []
    for file in storage.all_files():
        agreement = by_id.get(int(file["agreement_id"]))       # the files table keeps it as text
        if agreement is None:
            continue
        files_by_agreement.setdefault(agreement["id"], []).append(file)
        files.append(file_summary(file, agreement))
    rows = []
    for agreement in agreements:
        rows.append(agreement_row(agreement, files_by_agreement.get(agreement["id"], [])))
    return {"agreements": rows, "files": files, "docuseal_ready": docuseal.available()}


# =============================================================================
# Checking a copy, and health
# =============================================================================

def signed_in_log(sha):
    for row in storage.read_audit():
        if row["event"] == "signed" and row["sha256"] == sha:
            return True
    return False


@app.post("/api/verify", summary="Is this PDF a copy signed through AutoSign? Compares its SHA-256 with the audit log")
def verify(file: UploadFile = File(...)):
    sha = storage.fingerprint(read_upload(file))
    log_intact = storage.verify_audit()["ok"]
    signed = storage.file_by("signed_sha256", sha)
    if signed is None or not signed_in_log(sha):
        return {"match": False, "sha256": sha, "log_intact": log_intact}

    agreement = storage.agreement_by_id(signed["agreement_id"])
    return {
        "match": True,
        "sha256": sha,
        "document": agreement["filename"],
        "firm_name": agreement["firm_name"],
        "client_name": signed["client_name"],
        "reference": signed["reference"],
        "signer_name": signed["signer_name"],
        "signed_at": signed["signed_at"],
        "log_intact": log_intact,
    }


@app.get("/api/health", summary="Is the app up; are the AI, DocuSeal and the signature font set up; is the audit log intact")
def health():
    return {
        "ok": True,
        "ai": ai.status(),
        "docuseal": docuseal.status(),
        "signature_font": storage.SIGNATURE_FONT.exists(),
        "audit_log": storage.verify_audit(),
    }


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"AutoSign is running. Open http://{host}:{port} (API docs at /docs). Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*", log_level="warning")


if __name__ == "__main__":
    main()
