"""
The web app: intake (uploads and the inbox folder), the job queue and its worker, and the REST API
with the GUI page.

One worker handles one document at a time, so only one AI model is ever loaded on the GPU.

Start it with `uv run autosign`. GUI: http://127.0.0.1:8000, API docs: http://127.0.0.1:8000/docs
"""

import getpass
import json
import os
import queue
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from autosign import ai, pipeline, policy, storage
from autosign.storage import DB_LOCK, DEFAULT_SETTINGS, HOME, INBOX, INDEX_HTML, ORIGINALS, OUTPUT, PROCESSED, REPORTS, SETTINGS_FILE, SIGNATURE_BACKUPS, SIGNED, audit, db_execute, db_query, export_excel, get_job, init_db, load_settings, now_text, public_job, save_settings, signature_info, signature_path, update_job, validate_settings, verify_audit

# =============================================================================
# Intake, the job queue, and processing
# =============================================================================

FINAL_STATUSES = ("signed", "rejected", "failed", "duplicate")

SIGN_LOCK = threading.Lock()     # one signing at a time
JOBS = queue.Queue()             # job IDs waiting for the worker
WORKER = {"thread": None}
WORKER_LOCK = threading.Lock()


def deliver(job_id, filename):
    """Copy a signed PDF to output/ under its original name, e.g. output/Contract-signed.pdf.

    An existing file is never overwritten: the copy gets the job ID in its name instead.
    """
    stem = Path(filename).stem
    names = [f"{stem}-signed.pdf", f"{stem}-signed-{job_id}.pdf"] + [f"{stem}-signed-{job_id}-{n}.pdf" for n in range(2, 50)]
    try:
        data = (SIGNED / f"{job_id}.pdf").read_bytes()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name in names:
            target = OUTPUT / name
            try:
                with open(target, "xb") as f:          # "x": fails if the file already exists
                    f.write(data)
            except FileExistsError:
                continue
            return str(target.relative_to(HOME)) if target.is_relative_to(HOME) else str(target)
    except OSError as error:
        return f"not copied to the output folder ({error}); download it from History"
    return "not copied to the output folder (too many files with that name); download it from History"


def create_job(filename, data, source, uploader):
    """Store a received PDF and queue it, or record it as a duplicate of a document seen before."""
    fingerprint = pipeline.bytes_fingerprint(data)
    with DB_LOCK:
        seq = db_query("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM jobs")[0]["n"]
        job_id = f"{datetime.now():%Y-%m-%d}-{seq:04d}"
        earlier = db_query("SELECT job_id, status, fingerprint FROM jobs WHERE (fingerprint = ? OR output_fingerprint = ?) "
                           "AND status NOT IN ('failed', 'duplicate') ORDER BY seq LIMIT 1", (fingerprint, fingerprint))
        if earlier:
            status = "duplicate"
            what = "the same file as" if earlier[0]["fingerprint"] == fingerprint else "the signed copy from"
            reasons = [f"This is {what} job {earlier[0]['job_id']} ({earlier[0]['status']}). Documents are never signed twice."]
        else:
            status, reasons = "queued", []
            (ORIGINALS / f"{job_id}.pdf").write_bytes(data)
        db_execute("INSERT INTO jobs (seq, job_id, filename, source, uploader, fingerprint, received_at, status, reasons) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (seq, job_id, filename, source, uploader, fingerprint, now_text(), status, json.dumps(reasons)))
        audit(job_id, "received", {"filename": filename, "source": source, "uploader": uploader,
                                   "sha256": fingerprint, "status": status})
    if status == "queued":
        enqueue(job_id)
    return {"job_id": job_id, "filename": filename, "status": status, "reasons": reasons}


def is_pdf(data):
    return b"%PDF-" in data[:1024]


def process_inbox():
    """Queue every PDF in the inbox folder, then move it to inbox/processed."""
    results = []
    for path in sorted(INBOX.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        if time.time() - path.stat().st_mtime < 2:
            results.append({"filename": path.name, "status": "skipped", "reasons": ["The file is still being copied. Try again in a moment."]})
            continue
        try:
            data = path.read_bytes()
        except PermissionError:
            results.append({"filename": path.name, "status": "skipped", "reasons": ["Another program has the file open. Close it and try again."]})
            continue
        if not is_pdf(data):
            results.append({"filename": path.name, "status": "refused", "reasons": ["Not a PDF file"]})
            continue
        result = create_job(path.name, data, "inbox", getpass.getuser())
        target = PROCESSED / path.name
        if target.exists():
            target = PROCESSED / f"{path.stem}-{result['job_id']}{path.suffix}"
        try:
            shutil.move(str(path), str(target))
        except OSError as error:
            result["reasons"] = result["reasons"] + [f"Queued, but the file couldn't be moved out of the inbox ({error})"]
        results.append(result)
    return results


def enqueue(job_id):
    JOBS.put(job_id)
    start_worker()


def start_worker():
    """Start the single background worker (once). Jobs left queued by a crash are picked up again."""
    with WORKER_LOCK:
        if WORKER["thread"] is not None and WORKER["thread"].is_alive():
            return
        for row in db_query("SELECT job_id FROM jobs WHERE status IN ('queued', 'processing') ORDER BY seq"):
            JOBS.put(row["job_id"])
        WORKER["thread"] = threading.Thread(target=worker_loop, daemon=True, name="autosign-worker")
        WORKER["thread"].start()


def worker_loop():
    """Process queued jobs forever. Nothing that goes wrong with one job (or the ledger) stops the worker."""
    while True:
        try:
            job_id = JOBS.get(timeout=60)
        except queue.Empty:
            job_id = None
        if job_id:
            try:
                process_job(job_id)
            except Exception as error:          # one broken file never stops the batch
                try:
                    fail_job(job_id, error)
                except Exception:
                    pass                        # the ledger is unavailable: the job stays queued for the next start
        try:
            storage.daily_export()
        except Exception:
            pass                                # the export is retried on the next loop


def fail_job(job_id, error):
    message = f"{type(error).__name__}: {error}"
    update_job(job_id, status="failed", error=message, finished_at=now_text(),
               reasons=["Processing failed. Re-run the job, or reject it."])
    audit(job_id, "failed", {"error": message})


def party_summary(analysis):
    parties = analysis.get("parties") or {}
    items = []
    if parties.get("our_name_found"):
        items.append(f"{parties.get('our_name_text')} ({parties.get('our_role') or 'role unknown'})")
    for name, role in zip(parties.get("other_names", []), parties.get("named_roles", [])):
        items.append(f"{name} ({role})")
    if not items and parties.get("roles"):
        items.append("Roles: " + ", ".join(parties["roles"]))
    return "; ".join(items)


def signed_elsewhere(job_id):
    """Another job that already signed this same document (same fingerprint), if any."""
    job = get_job(job_id)
    if job is None:
        return None
    rows = db_query("SELECT job_id FROM jobs WHERE status = 'signed' AND job_id != ? AND "
                    "(fingerprint = ? OR output_fingerprint = ?) ORDER BY seq LIMIT 1",
                    (job_id, job["fingerprint"], job["fingerprint"]))
    return rows[0]["job_id"] if rows else None


def process_job(job_id):
    job = get_job(job_id)
    if job is None or job["status"] not in ("queued", "processing"):
        return                                   # already handled (a job can be queued twice)
    started = time.time()
    other = signed_elsewhere(job_id)
    if other:                                    # e.g. a failed job re-run after the same file was signed again
        reasons = [f"This document was already signed in job {other}. Documents are never signed twice."]
        update_job(job_id, status="duplicate", reasons=reasons, finished_at=now_text())
        audit(job_id, "duplicate", {"signed_in": other})
        return
    update_job(job_id, status="processing", error=None)
    settings = load_settings()
    sig = signature_info(settings)
    original = str(ORIGINALS / f"{job_id}.pdf")

    # 1. Parse and 2. Detect (rules)
    analysis = pipeline.analyze(original, settings, sig["aspect"])
    record = {"analysis": analysis, "ai": None, "verdict": None, "signing": None,
              "settings_used": {k: settings[k] for k in ("company_name", "company_aliases", "usual_roles", "signer_name",
                                                         "model", "auto_sign", "auto_sign_types", "require_company_name",
                                                         "max_blocks")}}
    fields = {"pages": analysis["page_count"], "lock_level": analysis["lock_level"] or ("none" if analysis["signatures"] == [] else ""),
              "doc_type": analysis["doc_type"], "parties": party_summary(analysis),
              "our_role": (analysis.get("parties") or {}).get("our_role") or ""}
    if analysis["verdict"] in ("rejected", "review"):
        status = analysis["verdict"]
        finish_job(job_id, status, analysis["reasons"], record, started, **fields)
        audit(job_id, "rejected" if status == "rejected" else "to_review", {"reasons": analysis["reasons"]})
        return

    # 2. Detect (AI) and 3. Verify
    if analysis["spots"]:
        opinion = ai.ask_ai(original, analysis, settings)
    else:
        opinion = {"ok": False, "skipped": True, "error": "No signature spot to choose from"}
    verdict = pipeline.run_checks(analysis, opinion, settings)
    record["ai"], record["verdict"] = opinion, verdict
    rules_set, ai_set = sorted(verdict["rules_pick"]), sorted(verdict["ai_pick"])
    fields.update(rules_pick=", ".join(map(str, rules_set)), ai_pick=", ".join(map(str, ai_set)),
                  agreed="yes" if rules_set and rules_set == ai_set else "no",
                  model=settings["model"], model_digest=opinion.get("digest"), prompt_version=policy.PROMPT_VERSION)

    # 4. Sign every block that is yours, only when every check passed and auto-sign is on
    if verdict["passed"] and settings["auto_sign"] and sig["found"]:
        with SIGN_LOCK:                          # one signing at a time, here and in the review screen
            result = sign_now(job_id, analysis, {"spots": verdict["proposal"]}, settings, sig, ai_check=True)
            record["signing"] = signing_record(result)
            if result["ok"]:
                delivered = deliver(job_id, job["filename"])
                finish_job(job_id, "signed", [], record, started, decision="auto",
                           **signed_fields(result, sig, delivered), **fields)
                audit(job_id, "auto_signed", audit_detail(result, sig, delivered))
            else:
                finish_job(job_id, "review", result["reasons"], record, started, **fields)
                audit(job_id, "to_review", {"reasons": result["reasons"]})
        return
    reasons = verdict["reasons"] + analysis["reasons"]
    if verdict["passed"]:
        reasons = ["Shadow mode: every check passed, but auto-sign is off. Approve to sign."]
    if not sig["found"]:
        reasons.append(sig["error"])
    finish_job(job_id, "review", reasons, record, started, **fields)
    audit(job_id, "to_review", {"reasons": reasons})


def finish_job(job_id, status, reasons, record, started, **fields):
    update_job(job_id, status=status, reasons=reasons, record=record, finished_at=now_text(),
               seconds=round(time.time() - started, 1), **fields)


def sign_now(job_id, analysis, choice, settings, sig, ai_check):
    """Stamp and date a copy at every chosen spot (or at points a reviewer clicked), then check it.

    `choice` is {"spots": [{"spot": id, "date": id or None}, ...]} or
    {"manual": [{"page", "x", "y", "date_x", "date_y"}, ...]}.
    Returns {"ok": ..., "reasons": [...], ...}; a failed check deletes the copy.
    """
    original = str(ORIGINALS / f"{job_id}.pdf")
    out = SIGNED / f"{job_id}.pdf"
    pending = SIGNED / f"{job_id}.pending.pdf"   # gets the final name only after every check passed
    date_text = datetime.now().strftime(settings["date_format"])
    if not sig["found"]:
        return {"ok": False, "reasons": [sig["error"]]}
    other = signed_elsewhere(job_id)
    if other:
        return {"ok": False, "reasons": [f"This document was already signed in job {other}. Documents are never signed twice."]}
    # Read the client's signatures from the file itself, not from the stored analysis.
    state = pipeline.signature_state(original)
    if state["blocked"]:
        return {"ok": False, "reasons": [state["blocked"] + ". It needs a certificate signature, or a copy without the lock."]}
    mode = state["mode"]
    try:
        if "manual" in choice:
            placements = []
            for m in choice["manual"]:
                has_date = m.get("date_x") is not None and m.get("date_y") is not None
                date_point = (float(m["date_x"]), float(m["date_y"])) if has_date else None
                placements.append(pipeline.manual_placement(original, int(m["page"]), float(m["x"]), float(m["y"]),
                                                          date_point, sig["aspect"]))
            if not placements:
                raise ValueError("No placement was given")
        else:
            placements = pipeline.placements_for_spots(analysis, choice["spots"], original, sig["aspect"], date_text)
        flatten = settings["flatten_after_signing"] and mode == "content"
        result = pipeline.sign_pdf(original, str(pending), placements, sig["png"], date_text, mode, flatten,
                                 settings["signer_name"] or settings["company_name"])
    except (ValueError, TypeError, KeyError) as error:
        return {"ok": False, "reasons": [str(error)]}
    if not result["ok"]:
        failed = [f"{c['title']} ({c['detail']})" for c in result["checks"] if not c["passed"]]
        return {"ok": False, "reasons": ["The signed copy failed its checks and was discarded: " + "; ".join(failed)],
                "checks": result["checks"], "placements": placements}
    ai_results = []
    if ai_check:
        our_ids = [p["spot_id"] for p in placements]
        for placement in placements:
            ai_result = ai.ai_check_signed(str(pending), analysis, placement, settings, our_ids)
            ai_result["spot_id"] = placement["spot_id"]
            ai_results.append(ai_result)
            if not ai_result["ok"]:
                pending.unlink(missing_ok=True)
                return {"ok": False, "checks": result["checks"], "placements": placements, "ai_check": ai_results,
                        "reasons": [f"The AI's look at the signed page didn't confirm spot {placement['spot_id']}, so the "
                                    "copy was discarded: " + (ai_result.get("detail") or ai_result.get("error", ""))]}
    os.replace(pending, out)
    return {"ok": True, "reasons": [], "checks": result["checks"], "placements": placements, "ai_check": ai_results,
            "date_text": date_text, "output": str(out), "output_fingerprint": pipeline.file_fingerprint(out),
            "flattened": result["flattened"]}


def signing_record(result):
    return {k: result.get(k) for k in ("ok", "reasons", "checks", "placements", "ai_check", "date_text", "flattened")}


def describe_placements(placements, date_text):
    """'p4, x 241-385 pt, y 651-683 pt; p6, ...' and the matching date descriptions, for the ledger."""
    where, dates = [], []
    for placement in placements:
        r = placement["signature"]
        text = f"p{placement['page']}, x {r[0]:.0f}-{r[2]:.0f} pt, y {r[1]:.0f}-{r[3]:.0f} pt"
        where.append(text + (" (placed by hand)" if placement.get("manual") else ""))
        date = placement.get("date")
        if date and date["kind"] == "field":
            dates.append(f"{date_text} in field {date.get('field_name')}")
        elif date:
            dates.append(f"{date_text} at p{placement['page']}, x {date['point'][0]:.0f}, y {date['point'][1]:.0f} pt")
    return "; ".join(where), "; ".join(dates)


def signed_fields(result, sig, delivered):
    where, date_filled = describe_placements(result["placements"], result["date_text"])
    return {"signed_where": where, "date_filled": date_filled, "output_path": delivered,
            "output_fingerprint": result["output_fingerprint"], "signature_sha256": sig["sha256"]}


def audit_detail(result, sig, delivered):
    where, date_filled = describe_placements(result["placements"], result["date_text"])
    return {"where": where, "date": date_filled, "output_sha256": result["output_fingerprint"],
            "signature_file": sig["path"], "signature_sha256": sig["sha256"], "saved_to": delivered}


# =============================================================================
# The REST API and the GUI page
# =============================================================================

def review_job(job_id, allowed=("review",)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} doesn't exist")
    if job["status"] not in allowed:
        raise HTTPException(409, f"Job {job_id} is {job['status']}, not waiting for review")
    return job


def sign_from_review(job_id, choice, decision):
    with SIGN_LOCK:
        job = review_job(job_id)
        record = json.loads(job["record"]) if job["record"] else {}
        analysis = record.get("analysis")
        if not analysis or analysis.get("verdict") == "rejected":
            raise HTTPException(409, "This job can't be signed")
        settings = load_settings()
        sig = signature_info(settings)
        result = sign_now(job_id, analysis, choice, settings, sig, ai_check=False)
        if not result["ok"]:
            raise HTTPException(422, " ".join(result["reasons"]))
        delivered = deliver(job_id, job["filename"])
        record["signing"] = signing_record(result)
        record["review"] = {"decision": decision, "choice": choice, "reviewer": getpass.getuser(), "at": now_text()}
        update_job(job_id, status="signed", reasons=[], record=record, finished_at=now_text(), decision=decision,
                   reviewer=getpass.getuser(), **signed_fields(result, sig, delivered))
        detail = audit_detail(result, sig, delivered)
        detail["reviewer"] = getpass.getuser()
        audit(job_id, decision, detail)
    return public_job(get_job(job_id))


def startup():
    for folder in (ORIGINALS, SIGNED, REPORTS, PROCESSED, OUTPUT):
        folder.mkdir(parents=True, exist_ok=True)
    for leftover in list(SIGNED.glob("*.pending.pdf")) + list(SIGNED.glob("*.work.pdf")):
        leftover.unlink(missing_ok=True)       # half-finished copies from a crash are never kept
    init_db()
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)
    start_worker()


@asynccontextmanager
async def lifespan(app):
    startup()
    yield


app = FastAPI(title="AutoSignPDF", version="1.0",
              description="Finds your company's signature block in a contract, checks it twice (rules and a local AI), "
                          "and stamps your signature and the date.", lifespan=lifespan)


# The app holds signing power, so it answers only this computer's own pages and programs.
ALLOWED_HOSTS = set(os.environ.get("AUTOSIGN_ALLOWED_HOSTS", "127.0.0.1:8000,localhost:8000").split(","))


@app.middleware("http")
async def only_this_computer(request, call_next):
    """Refuse requests that don't come from the AutoSignPDF page or a program on this computer.

    Host: another website can't reach the app under a different name (DNS rebinding).
    Changes (POST, PUT): browsers always send an Origin, so other websites' requests are refused;
    programs without an Origin must send the header X-AutoSign: 1, which websites can't add.
    """
    host = request.headers.get("host", "")
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": f"Requests for '{host}' are refused. Open the app at http://127.0.0.1:8000"},
                            status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is not None and origin.removeprefix("http://") not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Changes are only accepted from the AutoSignPDF page itself"}, status_code=403)
        if origin is None and request.headers.get("x-autosign") != "1":
            return JSONResponse({"detail": "Programs must send the header X-AutoSign: 1 with POST and PUT requests"},
                                status_code=403)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def gui():
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.post("/api/documents", summary="Upload one or more PDFs; returns job IDs")
def upload_documents(files: list[UploadFile] = File(...), source: str = Form("api")):
    results = []
    for f in files:
        name = Path(f.filename or "document.pdf").name
        data = f.file.read()
        if not is_pdf(data):
            results.append({"filename": name, "status": "refused", "reasons": ["Not a PDF file"]})
            continue
        results.append(create_job(name, data, source if source in ("gui", "api") else "api", getpass.getuser()))
    return {"jobs": results}


@app.post("/api/inbox/process", summary="Queue every PDF in the inbox folder")
def inbox_process():
    return {"inbox": str(INBOX), "jobs": process_inbox()}


@app.get("/api/jobs", summary="List jobs, newest first, by status or search text")
def list_jobs(status: str = "", q: str = "", limit: int = 200):
    sql, params = "SELECT * FROM jobs WHERE 1 = 1", []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (job_id LIKE ? OR filename LIKE ? OR parties LIKE ? OR doc_type LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(max(1, min(limit, 2000)))
    return {"jobs": [public_job(r) for r in db_query(sql, params)]}


@app.get("/api/jobs/{job_id}", summary="Job detail: parties, spots, rule and AI evidence, check results")
def job_detail(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} doesn't exist")
    return public_job(job, with_record=True)


@app.get("/api/jobs/{job_id}/pages/{page}/preview", summary="PNG of a page with the spots and proposed stamp drawn in")
def page_preview(job_id: str, page: int, view: str = "proposal"):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} doesn't exist")
    record = json.loads(job["record"]) if job["record"] else {}
    try:
        if view == "signed":
            path = SIGNED / f"{job_id}.pdf"
            if job["status"] != "signed" or not path.exists():
                raise HTTPException(404, "This job has no signed copy")
            png = pipeline.render_page(str(path), page)
        else:
            path = ORIGINALS / f"{job_id}.pdf"
            if not path.exists():
                raise HTTPException(404, "The original file isn't stored (duplicates are not kept)")
            analysis = record.get("analysis") if view == "proposal" else None
            png = pipeline.render_page(str(path), page, analysis, proposal_placements(record) if analysis else None)
    except HTTPException:
        raise
    except Exception as error:                 # e.g. a rejected file that isn't a readable PDF
        raise HTTPException(404, f"This page can't be shown: {error}")
    return Response(png, media_type="image/png")


def proposal_placements(record):
    """The proposed stamp and date rectangles for every proposed spot, for drawing on the preview."""
    analysis = record.get("analysis") or {}
    placements = []
    for choice in as_proposal_list((record.get("verdict") or {}).get("proposal")):
        spot = next((s for s in analysis.get("spots", []) if s["id"] == choice["spot"]), None)
        if not spot or not spot["placement"].get("fits"):
            continue
        date = next((d for d in analysis.get("dates", []) if d["id"] == choice.get("date")), None)
        placements.append({"page": spot["page"], "signature": spot["placement"]["rect"],
                           "date": date["placement"] if date and date["placement"].get("fits") else None})
    return placements


@app.post("/api/jobs/{job_id}/approve", summary="Sign every proposed spot (review queue only)")
def approve(job_id: str):
    job = review_job(job_id)
    record = json.loads(job["record"]) if job["record"] else {}
    proposal = as_proposal_list((record.get("verdict") or {}).get("proposal"))
    if not proposal:
        raise HTTPException(409, "There is no proposal for this job. Choose spots or place the signature by hand.")
    return sign_from_review(job_id, {"spots": proposal}, "approved")


def as_proposal_list(proposal):
    """Proposals are lists of {spot, date}; records from before multi-block signing held a single one."""
    if isinstance(proposal, dict):
        return [proposal]
    return proposal or []


def number_or_none(value, name):
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be a spot number")


@app.post("/api/jobs/{job_id}/adjust", summary="Sign at the spots you choose, or at points you clicked")
def adjust(job_id: str, body: dict = Body(..., examples=[
        {"spots": [{"spot": 2, "date": 4}, {"spot": 7, "date": None}]},
        {"placements": [{"page": 4, "x": 300, "y": 665, "date_x": 430, "date_y": 675}]},
        {"spot": 2, "date": 4}, {"page": 4, "x": 300, "y": 665}])):
    """Body forms: {"spots": [{spot, date}, ...]}, {"placements": [{page, x, y, date_x, date_y}, ...]},
    or the single forms {spot, date} and {page, x, y, date_x, date_y}."""
    items = body.get("spots")
    if items is None and body.get("spot") is not None:
        items = [{"spot": body["spot"], "date": body.get("date")}]
    manual = body.get("placements")
    if manual is None and body.get("page") is not None:
        manual = [{k: body.get(k) for k in ("page", "x", "y", "date_x", "date_y")}]
    if items:
        choice = {"spots": [{"spot": number_or_none(i.get("spot"), "spot"), "date": number_or_none(i.get("date"), "date")}
                            for i in items]}
        if any(c["spot"] is None for c in choice["spots"]):
            raise HTTPException(422, "Every entry needs a spot number")
    elif manual:
        for m in manual:
            if m.get("page") is None or m.get("x") is None or m.get("y") is None:
                raise HTTPException(422, "Every placement needs page, x and y")
        choice = {"manual": manual}
    else:
        raise HTTPException(422, "Send {spots: [{spot, date}]} or {placements: [{page, x, y, date_x, date_y}]}")
    return sign_from_review(job_id, choice, "adjusted")


@app.post("/api/jobs/{job_id}/reject", summary="Reject, with a reason")
def reject(job_id: str, body: dict = Body(default={})):
    reason = str(body.get("reason") or "").strip() or "Rejected by the reviewer"
    with SIGN_LOCK:
        review_job(job_id, allowed=("review", "failed"))
        update_job(job_id, status="rejected", reasons=[reason], decision="rejected", reviewer=getpass.getuser(),
                   finished_at=now_text())
        audit(job_id, "rejected", {"reason": reason, "reviewer": getpass.getuser()})
    return public_job(get_job(job_id))


@app.post("/api/jobs/{job_id}/rerun", summary="Process a job again with the current settings")
def rerun(job_id: str):
    with SIGN_LOCK:
        job = get_job(job_id)
        if job is None:
            raise HTTPException(404, f"Job {job_id} doesn't exist")
        if job["decision"] == "rejected":
            raise HTTPException(409, "A reviewer rejected this job, so it can't be re-run. Upload it again if it changed.")
        if job["status"] not in ("review", "failed", "rejected"):
            raise HTTPException(409, f"Job {job_id} is {job['status']} and can't be re-run")
        update_job(job_id, status="queued", reasons=[], error=None)
        audit(job_id, "rerun", {"previous_status": job["status"]})
    enqueue(job_id)
    return public_job(get_job(job_id))


@app.get("/api/jobs/{job_id}/download", summary="Download the signed PDF")
def download(job_id: str):
    job = get_job(job_id)
    path = SIGNED / f"{job_id}.pdf"
    if job is None or job["status"] != "signed" or not path.exists():
        raise HTTPException(404, "This job has no signed copy")
    return FileResponse(path, media_type="application/pdf", filename=f"{Path(job['filename']).stem}-signed.pdf")


@app.get("/api/jobs/{job_id}/original", summary="Download the PDF as it was received")
def original(job_id: str):
    job = get_job(job_id)
    path = ORIGINALS / f"{job_id}.pdf"
    if job is None or not path.exists():
        raise HTTPException(404, "The original file isn't stored (duplicates are not kept)")
    return FileResponse(path, media_type="application/pdf", filename=job["filename"])


@app.get("/api/ledger/export.xlsx", summary="Download the ledger as an Excel workbook")
def ledger_export():
    path = export_excel(REPORTS / f"ledger-{datetime.now():%Y-%m-%d-%H%M%S}.xlsx")
    audit("-", "export", {"file": path.name})
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/audit/verify", summary="Check that nobody edited or deleted audit log entries")
def audit_verify():
    return verify_audit()


@app.get("/api/settings", summary="Read the settings")
def get_settings():
    settings = load_settings()
    sig = signature_info(settings)
    return {"settings": settings, "doc_types": policy.DOC_TYPES,
            "signature": {k: (round(v, 3) if k == "aspect" else v) for k, v in sig.items() if k != "png"},
            "date_example": datetime.now().strftime(settings["date_format"]),
            "inbox": str(INBOX), "output": str(OUTPUT)}


@app.put("/api/settings", summary="Change company, signer, auto-sign switch, model and limits")
def put_settings(body: dict = Body(...)):
    try:
        settings = validate_settings(body)
    except ValueError as error:
        raise HTTPException(422, str(error))
    before = load_settings()
    save_settings(settings)
    changed = {k: settings[k] for k in settings if settings[k] != before.get(k)}
    audit("-", "settings_changed", changed)
    return get_settings()


@app.get("/api/settings/signature", summary="The signature image as it will be stamped (background removed, cropped)")
def get_signature():
    sig = signature_info(load_settings())
    if not sig["found"]:
        raise HTTPException(404, sig["error"])
    return Response(sig["png"], media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/settings/signature", summary="Replace the signature image (PNG or JPG); the old file is backed up")
def put_signature(file: UploadFile = File(...)):
    data = file.file.read()
    try:
        png = pipeline.prepare_signature(data)
    except Exception as error:
        raise HTTPException(422, f"That image can't be used: {error}")
    path = signature_path(load_settings())
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        SIGNATURE_BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = SIGNATURE_BACKUPS / f"{datetime.now():%Y%m%d-%H%M%S}-{path.name}"
        shutil.copy2(path, backup)
    path.write_bytes(png)
    audit("-", "signature_changed", {"sha256": pipeline.bytes_fingerprint(png), "uploaded": file.filename,
                                     "saved_to": str(path), "previous_kept_as": str(backup) if backup else None})
    return get_settings()


@app.get("/api/health", summary="Ollama reachable, model installed and loaded, GPU memory")
def health():
    settings = load_settings()
    sig = signature_info(settings)
    return {"app": "ok", "worker_running": bool(WORKER["thread"] and WORKER["thread"].is_alive()),
            "queued": JOBS.qsize(), "auto_sign": settings["auto_sign"], "ollama": ai.ollama_status(settings),
            "signature": {"found": sig["found"], "path": sig["path"], "error": sig["error"]},
            "output": str(OUTPUT)}


@app.get("/api/summary", summary="Counts for the dashboard")
def summary():
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db_query("SELECT status, decision, received_at FROM jobs")
    def count(items, **want):
        return sum(1 for r in items if all(r.get(k) == v for k, v in want.items()))
    todays = [r for r in rows if (r["received_at"] or "").startswith(today)]
    return {
        "today": {"auto_signed": count(todays, status="signed", decision="auto"),
                  "approved": count(todays, status="signed") - count(todays, status="signed", decision="auto"),
                  "review": count(todays, status="review"), "rejected": count(todays, status="rejected"),
                  "failed": count(todays, status="failed"), "duplicate": count(todays, status="duplicate"),
                  "queued": count(todays, status="queued") + count(todays, status="processing")},
        "all_time": {"total": len(rows), "signed": count(rows, status="signed"), "review": count(rows, status="review")},
        "recent": [public_job(r) for r in db_query("SELECT * FROM jobs ORDER BY seq DESC LIMIT 8")],
    }


# =============================================================================
# Starting the app
# =============================================================================

def main():
    print("AutoSignPDF is running. Open http://127.0.0.1:8000 (API docs at /docs). Press Ctrl+C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
