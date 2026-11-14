"""
DocuSeal: puts the company's signature on each client's copy, and collects the client's signature.

For each client file, AutoSign sends DocuSeal the agreement with one field on every place it found:

    create_submission()   the company's places are pre-filled with its cursive signature and the date,
                          and marked signed; the client's places are left empty. No email goes out yet.
    partly_signed_pdf()   the agreement as DocuSeal prepared it, for the company to check
    send()                DocuSeal emails the client the signing request
    submission_state()    whether the client opened, signed or declined
    signed_pdf()          the finished PDF, once the client has signed
    cancel()              archive a submission the company decided not to send

DocuSeal hosts the client's signing page, sends the emails (the request to the client, then the
signed copy to the client and to the company), and seals the finished PDF with its own digital
signature, so nothing is ever added to that PDF afterwards.

The API key is read from DOCUSEAL_API_KEY (the .env file locally). It is only sent to DocuSeal's API,
in the X-Auth-Token header over HTTPS with the certificate checked. It is never logged, never shown,
and never sent to the browser. Document links DocuSeal returns are downloaded without it.
"""

import base64
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import pymupdf

from autosign.policy import DATE_FONT_SIZES, DOCUSEAL_DATE_FORMAT

API_URL = os.environ.get("DOCUSEAL_API_URL") or "https://api.docuseal.com"
TIMEOUT_SECONDS = 60
RETRY_WAITS = [2, 5]            # seconds to wait before each retry
BUSY_STATUSES = {500, 502, 503, 504}
COMPANY_ROLE = "Company"
CLIENT_ROLE = "Client"


class DocuSealError(Exception):
    """DocuSeal refused the request or couldn't be reached. The message never contains the key."""


def api_key():
    return (os.environ.get("DOCUSEAL_API_KEY") or "").strip()


def available():
    return bool(api_key())


def status():
    return {"provider": "DocuSeal", "configured": available()}


# =============================================================================
# Talking to the API
# =============================================================================

def error_message(response):
    """What DocuSeal said was wrong, shortened."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])[:300]
    return str(body)[:200]


def may_retry(method, status_code=None, error=None):
    """Whether a failed request can safely be sent again.

    'Too many requests' (429) is always safe to retry: DocuSeal did nothing. Anything else is only
    retried for reads, or when the connection never opened, so a submission is never created twice
    and an email is never sent twice.
    """
    if error is not None:
        if method == "GET":
            return True
        return isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
    if status_code == 429:
        return True
    return method == "GET" and status_code in BUSY_STATUSES


def call(method, path, body=None):
    """One request to DocuSeal's API. Returns DocuSeal's JSON answer; raises DocuSealError."""
    if not available():
        raise DocuSealError("DocuSeal isn't set up on this server: add DOCUSEAL_API_KEY to the .env file")
    headers = {"X-Auth-Token": api_key(), "Accept": "application/json"}
    url = API_URL.rstrip("/") + path
    attempt = 0
    while True:
        try:
            response = httpx.request(method, url, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            if attempt < len(RETRY_WAITS) and may_retry(method, error=error):
                time.sleep(RETRY_WAITS[attempt])
                attempt += 1
                continue
            raise DocuSealError(f"DocuSeal couldn't be reached ({type(error).__name__})")
        if response.status_code >= 400:
            if attempt < len(RETRY_WAITS) and may_retry(method, status_code=response.status_code):
                time.sleep(RETRY_WAITS[attempt])
                attempt += 1
                continue
            raise DocuSealError(f"DocuSeal refused the request (HTTP {response.status_code}: "
                                f"{error_message(response)})")
        return response.json()


def download(url):
    """A PDF from a link DocuSeal returned. The API key is never sent with it."""
    if urlsplit(url).scheme != "https":
        raise DocuSealError("DocuSeal returned a document link that isn't HTTPS")
    try:
        response = httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as error:
        raise DocuSealError(f"The document couldn't be downloaded from DocuSeal ({type(error).__name__})")
    if response.status_code >= 400 or not response.content.startswith(b"%PDF"):
        raise DocuSealError(f"DocuSeal's document link didn't return a PDF (HTTP {response.status_code})")
    return response.content


def utc_text(value):
    """DocuSeal's '2026-11-14T07:29:17.101Z' as AutoSign writes times: '2026-11-14 07:29:17 UTC'."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# =============================================================================
# The fields: one per signature and per date, on the places AutoSign found
# =============================================================================

def field_name(role, kind, number):
    """'Company signature 1', 'Client date 2': the places are numbered for each side."""
    return f"{role} {kind} {number}"


def area_on(page, rect):
    """A rectangle in PDF points, as the pipeline measures it (on the unrotated page), as a DocuSeal
    area: fractions of the page as it is shown, page numbers counted from 1."""
    shown = pymupdf.Rect(rect) * page.rotation_matrix
    shown.normalize()
    width = page.rect.width
    height = page.rect.height
    return {"x": round(shown.x0 / width, 5), "y": round(shown.y0 / height, 5),
            "w": round(shown.width / width, 5), "h": round(shown.height / height, 5), "page": page.number + 1}


def find_by_id(items, item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    return None


def date_rect(analysis, placement):
    """Where a place's date goes, or None when it has no date (or the date didn't fit there).

    The whole date line (or date field, or the space after the 'Date' label) is given to DocuSeal, not
    just the width of the date text: DocuSeal pads its text boxes and wraps text that doesn't fit.
    """
    date = placement.get("date")
    if not date or not date.get("rect"):
        return None
    found = find_by_id(analysis["dates"], placement.get("date_id"))
    if found is None:
        return date["rect"]             # a place added by hand: its date box is already wide
    return found["area"]


def name_rect(analysis, placement):
    """Where the signer's printed name goes in the place's block ('Name: ____'), or None."""
    spot = find_by_id(analysis["spots"], placement.get("spot_id"))
    if spot is None:
        return None
    found = find_by_id(analysis.get("names") or [], spot.get("name_id"))
    if found is None:
        return None
    return found["area"]


def text_field(role, kind, number, page, rect, preferences):
    return {"name": field_name(role, kind, number), "type": "text", "role": role, "required": True,
            "areas": [area_on(page, rect)], "preferences": preferences}


def side_fields(doc, analysis, role, placements, date_type):
    """A side's fields, for each of its places: the signature, the date, and the signer's printed name."""
    fields = []
    for number, placement in enumerate(placements, start=1):
        page = doc[placement["page"] - 1]
        fields.append({"name": field_name(role, "signature", number), "type": "signature", "role": role,
                       "required": True, "areas": [area_on(page, placement["signature"])]})
        rect = date_rect(analysis, placement)
        if rect is not None:
            preferences = {"font_size": placement["date"].get("size") or DATE_FONT_SIZES[0]}
            date = text_field(role, "date", number, page, rect, preferences)
            if date_type == "date":
                date["type"] = "date"
                preferences["format"] = DOCUSEAL_DATE_FORMAT
            fields.append(date)
        rect = name_rect(analysis, placement)
        if rect is not None:
            fields.append(text_field(role, "name", number, page, rect, {"font_size": DATE_FONT_SIZES[0]}))
    return fields


def fields_for(pdf_path, analysis, client_placements, company_placements):
    """Every field DocuSeal is asked to create: a signature, a date and a printed name for each place
    (when its block has a date and a name line; title lines are never filled). The company's date and
    name are text AutoSign fills in; the client fills in theirs on DocuSeal's page."""
    with pymupdf.open(pdf_path) as doc:
        company = side_fields(doc, analysis, COMPANY_ROLE, company_placements, "text")
        client = side_fields(doc, analysis, CLIENT_ROLE, client_placements, "date")
    return company + client


def field_kind(field):
    """'signature', 'date' or 'name', from a field's name ('Company date 1')."""
    return field["name"].split(" ")[1]


def company_values(fields, signature_png, date_text, signer_name):
    """What DocuSeal writes at the company's places: its cursive signature, today's date, the signer's name."""
    encoded = base64.b64encode(signature_png).decode("ascii")
    values = {}
    for field in fields:
        if field["role"] != COMPANY_ROLE:
            continue
        kind = field_kind(field)
        if kind == "signature":
            values[field["name"]] = encoded
        elif kind == "date":
            values[field["name"]] = date_text
        elif kind == "name":
            values[field["name"]] = signer_name
    return values


def client_values(fields, client_name):
    """What the client's form starts with: their name on the name lines (the client can correct it)."""
    values = {}
    for field in fields:
        if field["role"] == CLIENT_ROLE and field_kind(field) == "name":
            values[field["name"]] = client_name
    return values


def check_fields(sent, created):
    """DocuSeal must have created exactly the fields AutoSign sent: no extra block, and none missing."""
    sent_names = sorted(str(field["name"]) for field in sent)
    created_names = sorted(str(field.get("name")) for field in created)
    if sent_names == created_names:
        return
    extra = sorted(set(created_names) - set(sent_names))
    missing = sorted(set(sent_names) - set(created_names))
    raise DocuSealError(f"DocuSeal's fields don't match the places AutoSign sent (extra: {', '.join(extra) or 'none'}; "
                        f"missing: {', '.join(missing) or 'none'}). Nothing was sent.")


def submitter_for(answer, role):
    for submitter in answer.get("submitters") or []:
        if submitter.get("role") == role:
            return submitter
    raise DocuSealError(f"DocuSeal's answer has no {role.lower()}")


# =============================================================================
# Submissions
# =============================================================================

def create_submission(document_name, pdf_bytes, fields, company, client, company_email):
    """One client file in DocuSeal: the company already signed (when it signs at all), the client to sign.

    company is {"name", "values"}, or None when only the client signs. client is {"name", "email",
    "external_id", "values"}. Nothing is emailed yet: send() does that once the company has checked the
    copy. The company gets the signed copy by email when the client signs (bcc_completed), and replies
    to DocuSeal's emails go to the company. Returns {"id", "client_submitter_id"}.
    """
    submitters = []
    if company is not None:
        submitters.append({"role": COMPANY_ROLE, "name": company["name"], "completed": True, "send_email": False,
                           "values": company["values"]})
    client_submitter = {"role": CLIENT_ROLE, "name": client["name"], "email": client["email"],
                        "external_id": client["external_id"], "send_email": False}
    if client.get("values"):
        client_submitter["values"] = client["values"]
    submitters.append(client_submitter)
    document = {"name": document_name, "file": base64.b64encode(pdf_bytes).decode("ascii"), "fields": fields}
    body = {"name": document_name, "send_email": False, "order": "preserved", "documents": [document],
            "submitters": submitters}
    if company_email:
        body["bcc_completed"] = company_email
        body["reply_to"] = company_email
    created = call("POST", "/submissions/pdf", body)
    try:
        check_fields(fields, created.get("fields") or [])
        client_submitter = submitter_for(created, CLIENT_ROLE)
    except DocuSealError:
        cancel_quietly(created["id"])
        raise
    return {"id": created["id"], "client_submitter_id": client_submitter["id"]}


def documents(submission_id):
    """The submission's documents: partly filled while it is pending, the signed ones once it is completed."""
    answer = call("GET", f"/submissions/{submission_id}/documents")
    found = answer.get("documents") or []
    if not found:
        raise DocuSealError("DocuSeal returned no document for this submission")
    return found


def partly_signed_pdf(submission_id):
    return download(documents(submission_id)[0]["url"])


def signed_pdf(submission_id):
    return download(documents(submission_id)[0]["url"])


def send(client_submitter_id):
    """DocuSeal emails the client the signing request. Returns when DocuSeal says it sent it (UTC text),
    or None when DocuSeal doesn't say."""
    answer = call("PUT", f"/submitters/{client_submitter_id}", {"send_email": True})
    if answer.get("sent_at"):
        return utc_text(answer["sent_at"])
    submitter = call("GET", f"/submitters/{client_submitter_id}")      # the update's answer may not carry it yet
    return utc_text(submitter.get("sent_at"))


def submission_state(submission_id):
    """Where the client is: when the request was sent, opened, signed or declined (UTC text or None)."""
    answer = call("GET", f"/submissions/{submission_id}")
    client = submitter_for(answer, CLIENT_ROLE)
    completed = answer.get("status") == "completed" or client.get("status") == "completed"
    return {"completed": completed, "sent_at": utc_text(client.get("sent_at")),
            "opened_at": utc_text(client.get("opened_at")), "completed_at": utc_text(client.get("completed_at")),
            "declined_at": utc_text(client.get("declined_at")), "signer_name": client.get("name") or ""}


def cancel(submission_id):
    """Archive a submission: its signing link stops working."""
    call("DELETE", f"/submissions/{submission_id}")


def cancel_quietly(submission_id):
    """Archive a submission AutoSign just created and won't use; a failure here changes nothing for the company."""
    try:
        cancel(submission_id)
    except DocuSealError:
        pass
