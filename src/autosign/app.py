"""
The web app: the pages and the REST API.

There are no accounts. Each thing gets its own private link, a long random secret:
    /a/<token>   the company's agreement page: where everyone signs, the client files and their status
    /s/<token>   one client's signing page
Links are never listed anywhere, so only people who were given one can open it.

The flow:
    1. The company uploads its agreement (PDF). AutoSign finds the client's and the company's places.
    2. When AutoSign isn't sure, the company checks the places on the agreement page and confirms.
    3. AutoSign signs for the company once (data/signature.png), on the copy every client will sign.
    4. For each client file the company copies a signing link; the client signs in the browser.

Run it locally with `uv run autosign` (http://127.0.0.1:8000). API documentation: /docs.
"""

import base64
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit


def load_dotenv(path=Path(".env")):
    """Read the .env file of the project folder (the Gemini key) into the environment, for running locally.

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

from autosign import ai, storage, workflow                                 # noqa: E402
from autosign.policy import MAX_MB, MAX_PAGES, MAX_SIGNATURE_BYTES         # noqa: E402

HEAVY = threading.BoundedSemaphore(2)      # reading and signing documents: two at a time
SIGNING = threading.Lock()                 # one signing at a time, so a link can never be signed twice
SIGNATURE_MISSING = "Your company's signature image isn't set up on this server, so only the client will sign."


@asynccontextmanager
async def lifespan(app):
    storage.init()
    yield


app = FastAPI(title="AutoSign", version="1.0.0", lifespan=lifespan,
              description="Upload your client agreement once. AutoSign signs it for your company, then you send "
                          "each new client file a signing link. The client signs in the browser; you get the "
                          "signed PDF and an audit log.")
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


@app.get("/a/{token}", include_in_schema=False)
def agreement_page(token: str):
    return page("agreement.html")


@app.get("/s/{token}", include_in_schema=False)
def sign_page(token: str):
    return page("sign.html")


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


def browser(request):
    return request.headers.get("user-agent", "")[:300]


def base_url(request):
    return os.environ.get("PUBLIC_URL") or str(request.base_url).rstrip("/")


def clean_text(value, limit):
    """One line of text from a form: spaces collapsed, cut to `limit` characters."""
    return " ".join(str(value or "").split())[:limit]


def find_agreement(token):
    agreement = storage.agreement_by_token(token)
    if agreement is None:
        raise HTTPException(404, "This agreement link doesn't exist. Check that you copied the whole link.")
    return agreement


def find_file(column, token):
    file = storage.file_by(column, token)
    if file is None:
        raise HTTPException(404, "This link doesn't exist. Check that you copied the whole link.")
    return file


def png_response(png):
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


def safe_filename(text):
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", text).strip()[:80]
    return cleaned or "agreement"


def signed_name(agreement, file):
    stem = safe_filename(Path(agreement["filename"]).stem)
    client = safe_filename(file["client_name"])
    return f"{stem} - signed by {client}.pdf"


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


# =============================================================================
# What the pages are sent
# =============================================================================

def file_summary(file):
    """One client file, as the company's dashboard shows it."""
    summary = {
        "client_name": file["client_name"],
        "reference": file["reference"],
        "status": file["status"],
        "created_at": file["created_at"],
        "viewed_at": file["viewed_at"],
        "signed_at": file["signed_at"],
        "signer_name": file["signer_name"],
        "sign_url": f"/s/{file['sign_token']}",
        "signed_pdf_url": None,
    }
    if file["status"] == "signed":
        summary["signed_pdf_url"] = f"/api/files/{file['manage_token']}/signed.pdf"
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
        files.append(file_summary(file))
    company_view = places_summary(company)
    company_view.update(signer=company.get("signer"), signed_at=company.get("signed_at"),
                        date=company.get("date"), note=company.get("note"))
    return {
        "token": agreement["token"],
        "filename": agreement["filename"],
        "firm_name": agreement["firm_name"],
        "created_at": agreement["created_at"],
        "page_count": analysis["page_count"],
        "pages": analysis["pages"],
        "status": agreement["status"],
        "summary": agreement["summary"],
        "reasons": agreement["reasons"] or [],
        "notes": analysis["notes"],
        "spots": spots,
        "client": places_summary(agreement["proposal"] or {}),
        "company": company_view,
        "files": files,
    }


# =============================================================================
# Signing for the company
# =============================================================================

def sign_for_company(agreement, tz_offset_minutes, ip):
    """Put the company's signature on the copy every client signs.

    With no place for the company, no signature image, or a place the stamp doesn't fit, the
    agreement page says why and the client is the only one who signs.
    """
    company = dict(agreement["company"])
    company["signed_at"] = None
    company["note"] = None
    signer = company.get("signer") or agreement["firm_name"]
    date_text = workflow.date_today(tz_offset_minutes)
    presigned = storage.presigned_pdf(agreement)

    if not workflow.has_places(company):
        company["note"] = workflow.ONLY_THE_CLIENT_SIGNS
    elif not storage.COMPANY_SIGNATURE.exists():
        company["note"] = SIGNATURE_MISSING
    else:
        try:
            company["placements"] = workflow.presign(storage.agreement_pdf(agreement), agreement["analysis"], company,
                                                     agreement["placements"], storage.COMPANY_SIGNATURE.read_bytes(),
                                                     signer, date_text, presigned)
            company["signed_at"] = storage.now_text()
            company["date"] = date_text
            company["sha256"] = storage.fingerprint(presigned.read_bytes())
        except workflow.SigningError as error:
            company["note"] = f"{error}. Only the client will sign."
    storage.update_agreement(agreement["id"], company=company)

    if company["signed_at"]:
        pages = describe_pages(company["placements"])
        detail = f"signed for {agreement['firm_name']} on {pages}; date written: {date_text}"
        storage.audit("company_signed", agreement=agreement, actor=signer, ip=ip, detail=detail,
                      sha256=company["sha256"])


# =============================================================================
# Agreements (the company's side)
# =============================================================================

@app.post("/api/agreements", summary="Upload the client agreement (PDF only), find the places, sign for the company")
def upload_agreement(request: Request, file: UploadFile = File(...), firm_name: str = Form(...),
                     company_signer: str = Form(""), tz_offset_minutes: int = Form(0)):
    firm_name = clean_text(firm_name, 120)
    if len(firm_name) < 2:
        raise HTTPException(422, "Enter your company's name, as it appears in the agreement")
    filename = Path(file.filename or "agreement.pdf").name[:150]
    pdf = read_upload(file)
    if b"%PDF-" not in pdf[:1024]:
        raise HTTPException(422, "Upload the agreement as a PDF file. Word files aren't accepted: "
                                 "save the agreement as a PDF first.")

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
    company = dict(found["company"], signer=clean_text(company_signer, 100))
    agreement_id = storage.create_agreement(
        filename=filename, firm_name=firm_name, sha256=storage.fingerprint(pdf), analysis=found["analysis"],
        ai=found["ai"], proposal=found["client"], placements=found["client_placements"], reasons=found["reasons"],
        summary=found["summary"], status=status, company=company)
    agreement = storage.agreement_by_id(agreement_id)
    os.replace(work, storage.agreement_pdf(agreement))

    ip = client_ip(request)
    storage.audit("agreement_uploaded", agreement=agreement, actor=firm_name, ip=ip,
                  detail=f"{pages} page(s)", sha256=agreement["sha256"])
    if sure:
        storage.audit("places_confirmed", agreement=agreement, actor="AutoSign (rules and AI agree)",
                      detail=found["summary"])
        with SIGNING, HEAVY:
            sign_for_company(agreement, tz_offset_minutes, ip)
    return {"token": agreement["token"], "url": f"/a/{agreement['token']}", "ready": sure}


@app.get("/api/agreements/{token}", summary="The agreement, where everyone signs, and its client files")
def get_agreement(token: str):
    return agreement_view(find_agreement(token))


@app.get("/api/agreements/{token}/pages/{number}.png", summary="A page with the signature places drawn in")
def agreement_page_image(token: str, number: int):
    agreement = find_agreement(token)
    try:
        png = workflow.render_for_company(storage.pdf_for_client(agreement), number, agreement["analysis"],
                                          agreement["proposal"] or {}, agreement["placements"] or [],
                                          agreement["company"] or {})
    except ValueError as error:
        raise HTTPException(404, str(error))
    return png_response(png)


@app.put("/api/agreements/{token}/places", summary="Confirm (or change) where the client and your company sign")
def confirm_places(request: Request, token: str, body: dict = Body(...)):
    """The body: {"client": places, "company": places, "tz_offset_minutes": n}, where places are
    {"choices": [{"spot": id}], "manual": [{"page", "x", "y", "date": [x, y] or null}]}."""
    agreement = find_agreement(token)
    pdf = str(storage.agreement_pdf(agreement))
    analysis = agreement["analysis"]
    try:
        client, client_placements, company_places, company_placements = workflow.check_places(analysis, body, pdf)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, f"These places can't be used: {error}")

    company = dict(agreement["company"] or {}, choices=company_places["choices"], manual=company_places["manual"],
                   placements=company_placements)
    summary = workflow.summarize(analysis, client, company)
    ip = client_ip(request)
    # Under the signing lock, so no client signs while the places and the pre-signed copy change.
    with SIGNING, HEAVY:
        storage.update_agreement(agreement["id"], proposal=client, placements=client_placements, company=company,
                                 summary=summary, status="ready")
        storage.audit("places_confirmed", agreement=agreement, actor="the company", ip=ip, detail=summary)
        sign_for_company(storage.agreement_by_id(agreement["id"]), body.get("tz_offset_minutes", 0), ip)
    return agreement_view(storage.agreement_by_id(agreement["id"]))


@app.post("/api/agreements/{token}/files", summary="Open a client file and create its signing link")
def create_file(request: Request, token: str, body: dict = Body(...)):
    agreement = find_agreement(token)
    if agreement["status"] != "ready":
        raise HTTPException(409, "Confirm where the client signs before creating signing links")
    client_name = clean_text(body.get("client_name"), 120)
    reference = clean_text(body.get("reference"), 80)
    if len(client_name) < 2:
        raise HTTPException(422, "Enter the client's name")
    file = storage.file_by("id", storage.create_file(agreement["id"], client_name, reference))
    storage.audit("signing_link_created", agreement=agreement, file=file, actor=agreement["firm_name"],
                  ip=client_ip(request))
    return file_summary(file)


@app.get("/api/agreements/{token}/audit.csv", summary="The audit log rows for this agreement and its files")
def agreement_audit(token: str):
    agreement = find_agreement(token)
    csv_text = storage.audit_csv(storage.audit_rows(agreement["id"]))
    name = safe_filename(Path(agreement["filename"]).stem)
    return Response(csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name} - audit log.csv"'})


@app.get("/api/files/{token}/signed.pdf", summary="A client file's signed PDF, for the company (?download=1 to save)")
def file_signed_pdf(token: str, download: int = 0):
    return signed_response(find_file("manage_token", token), download)


def signed_response(file, download):
    if file["status"] != "signed":
        raise HTTPException(409, "This agreement hasn't been signed yet")
    agreement = storage.agreement_by_id(file["agreement_id"])
    disposition = "inline"
    if download:
        disposition = "attachment"
    return FileResponse(storage.signed_pdf(file), media_type="application/pdf", filename=signed_name(agreement, file),
                        content_disposition_type=disposition)


# =============================================================================
# Signing (the client's side)
# =============================================================================

@app.get("/api/sign/{token}", summary="What the client is asked to sign")
def get_signing(request: Request, token: str):
    file = find_file("sign_token", token)
    agreement = storage.agreement_by_id(file["agreement_id"])
    if file["status"] == "sent":
        storage.update_file(file["id"], status="viewed", viewed_at=storage.now_text())
        storage.audit("link_opened", agreement=agreement, file=file, actor="the client", ip=client_ip(request),
                      detail=browser(request)[:200])
        file = storage.file_by("id", file["id"])

    placements = agreement["placements"] or []
    sign_pages = sorted({placement["page"] for placement in placements})
    company = agreement["company"] or {}
    signed_page_count = None
    if file["status"] == "signed":
        signed_page_count = workflow.page_count(storage.signed_pdf(file))     # the certificate is one more page
    return {
        "firm_name": agreement["firm_name"],
        "document": agreement["filename"],
        "client_name": file["client_name"],
        "reference": file["reference"],
        "status": file["status"],
        "signed_at": file["signed_at"],
        "signer_name": file["signer_name"],
        "page_count": agreement["analysis"]["page_count"],
        "signed_page_count": signed_page_count,
        "sign_pages": sign_pages,
        "company_signed_at": company.get("signed_at"),
        "company_signer": company.get("signer") or agreement["firm_name"],
        "consent_text": workflow.CONSENT_TEXT,
        "ready": agreement["status"] == "ready" and bool(placements),
        "signed_url": f"/api/sign/{token}/signed.pdf",
    }


@app.get("/api/sign/{token}/pages/{number}.png", summary="A page to read, with the places to sign, or once signed")
def signing_page_image(token: str, number: int):
    file = find_file("sign_token", token)
    agreement = storage.agreement_by_id(file["agreement_id"])
    try:
        if file["status"] == "signed":
            png = workflow.render_plain(storage.signed_pdf(file), number)
        else:
            png = workflow.render_for_signer(storage.pdf_for_client(agreement), number, agreement["placements"] or [])
    except ValueError as error:
        raise HTTPException(404, str(error))
    return png_response(png)


@app.post("/api/sign/{token}", summary="Sign: the client's name, signature image and consent")
def sign(request: Request, token: str, body: dict = Body(...)):
    signer_name = clean_text(body.get("signer_name"), 100)
    if len(signer_name) < 2:
        raise HTTPException(422, "Type your full name")
    if body.get("consent") is not True:
        raise HTTPException(422, "Tick the box to agree to sign electronically")
    image = decode_signature(body.get("signature"))

    with SIGNING, HEAVY:
        file = find_file("sign_token", token)
        agreement = storage.agreement_by_id(file["agreement_id"])
        if file["status"] == "signed":
            raise HTTPException(409, "This agreement is already signed")
        if agreement["status"] != "ready" or not agreement["placements"]:
            raise HTTPException(409, "This agreement isn't ready to sign yet. Ask the company that sent it.")

        analysis = agreement["analysis"]
        date_text = workflow.date_today(body.get("tz_offset_minutes", 0))
        out = storage.signed_pdf(file)
        source = storage.pdf_for_client(agreement)
        try:
            placements, png = workflow.sign(str(source), analysis, agreement["proposal"], image, signer_name,
                                            date_text, str(out))
        except workflow.SigningError as error:
            raise HTTPException(422, str(error))

        signed_at = storage.now_text()
        ip = client_ip(request)
        rows = certificate_rows(request, agreement, file, signer_name, signed_at, date_text, placements)
        with_certificate = workflow.add_certificate(out, rows, [("Client's signature", png)], analysis["mode"])
        signed_sha = storage.fingerprint(out.read_bytes())
        certificate = "no"
        if with_certificate:
            certificate = "yes"
        storage.update_file(file["id"], status="signed", signed_at=signed_at, signer_name=signer_name, signer_ip=ip,
                            signer_agent=browser(request), signed_sha256=signed_sha, certificate=certificate,
                            placements=placements)

        detail = (f"signed on {describe_pages(placements)}; date written: {date_text}; consent ticked; "
                  f"agreement SHA-256 {agreement['sha256']}")
        storage.audit("signed", agreement=agreement, file=file, actor=signer_name, ip=ip, sha256=signed_sha,
                      detail=detail)
    return {"ok": True, "signed_url": f"/api/sign/{token}/signed.pdf", "signed_sha256": signed_sha}


def company_rows(agreement):
    """The certificate's lines about the company's signature."""
    company = agreement["company"] or {}
    if not company.get("signed_at"):
        return [("Company signature", "Not part of this agreement: only the client signs", False)]
    signer = company.get("signer") or agreement["firm_name"]
    return [
        (f"Signed for {agreement['firm_name']} by", signer, False),
        ("Company signed at", company["signed_at"], False),
        ("Date written by the company", company.get("date") or "", False),
    ]


def certificate_rows(request, agreement, file, signer_name, signed_at, date_text, placements):
    """The lines of the signing certificate: (label, value, monospace)."""
    client_file = file["client_name"]
    if file["reference"]:
        client_file += f" (ref. {file['reference']})"
    rows = [
        ("Agreement", agreement["filename"], False),
        ("Sent by", agreement["firm_name"], False),
        ("Client file", client_file, False),
    ]
    rows += company_rows(agreement)
    rows += [
        ("Client signed by", f"{signer_name} (name typed by the signer)", False),
        ("Client signed at", signed_at, False),
        ("Date written by the client", date_text, False),
        ("Client signed on", describe_pages(placements), False),
        ("Client's IP address", client_ip(request) or "unknown", False),
        ("Client's browser", browser(request) or "unknown", False),
        ("Signing link created", file["created_at"], False),
        ("Link first opened", file["viewed_at"] or signed_at, False),
        ("Consent", workflow.CONSENT_TEXT + " (ticked by the signer)", False),
        ("Agreement SHA-256 as uploaded", agreement["sha256"], True),
    ]
    company = agreement["company"] or {}
    if company.get("signed_at"):
        rows.append(("SHA-256 after the company signed", company["sha256"], True))
    rows.append(("Check a copy", f"The SHA-256 of this complete signed PDF is kept in the audit log. Upload a copy "
                                 f"at {base_url(request)}/verify to check it is this exact document.", False))
    return rows


def decode_signature(value):
    """The client's signature as the browser sends it: a PNG as a data: URL (typed, drawn or uploaded)."""
    if not isinstance(value, str) or not value.startswith("data:image/png;base64,"):
        raise HTTPException(422, "Add your signature: type it, draw it or upload an image")
    try:
        data = base64.b64decode(value.split(",", 1)[1], validate=True)
    except Exception:
        raise HTTPException(422, "The signature couldn't be read. Add it again.")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise HTTPException(413, "The signature image is too big")
    return data


@app.get("/api/sign/{token}/signed.pdf", summary="The signed PDF for the client (inline, or ?download=1)")
def signing_signed_pdf(token: str, download: int = 0):
    return signed_response(find_file("sign_token", token), download)


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


@app.get("/api/health", summary="Is the app up; are the AI and the company signature set up; is the audit log intact")
def health():
    return {
        "ok": True,
        "ai": ai.status(),
        "company_signature": storage.COMPANY_SIGNATURE.exists(),
        "audit_log": storage.verify_audit(),
    }


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"AutoSign is running. Open http://{host}:{port} (API docs at /docs). Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*", log_level="warning")


if __name__ == "__main__":
    main()
