"""
Where the app keeps things: its folders, its settings (settings.json), the ledger (one SQLite
row per document), the hash-chained audit log, and the Excel export.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from autosign import pipeline, policy

# =============================================================================
# Folders and settings
# =============================================================================

# Where the code is (src/autosign) and where the app keeps its data: the project folder, two levels up.
# Tests point AUTOSIGN_HOME at a temporary folder instead.
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]
HOME = Path(os.environ.get("AUTOSIGN_HOME", PROJECT_DIR))
STORAGE = HOME / "storage"
ORIGINALS = STORAGE / "originals"        # every received PDF, named by job ID (never modified)
SIGNED = STORAGE / "signed"              # signed copies, named by job ID (used for downloads and previews)
REPORTS = STORAGE / "reports"            # Excel exports
SIGNATURE_BACKUPS = STORAGE / "signature-backups"   # earlier signature images, kept when you upload a new one
DB_FILE = STORAGE / "ledger.db"
INBOX = HOME / "inbox"                   # drop PDFs here, then click "Process inbox"
PROCESSED = INBOX / "processed"          # inbox files move here once they are queued
OUTPUT = HOME / "output"                 # every signed PDF is also saved here under its original name
SETTINGS_FILE = HOME / "settings.json"
INDEX_HTML = PACKAGE_DIR / "static" / "index.html"
DEFAULT_ASPECT = 3.5                     # signature width / height, used for placement only when no image exists

DEFAULT_SETTINGS = {
    "company_name": "Acme Analytics Inc.",       # an example: change it to your company in Settings
    "company_aliases": ["Acme Analytics"],
    "usual_roles": ["Service Provider", "Provider", "Consultant", "Receiving Party", "Disclosing Party"],
    "signer_name": "",                           # the person whose signature this is (optional)
    "signature_file": "data/signature.png",      # your signature image, read fresh on every signing
    "date_format": "%b %d, %Y",
    "auto_sign": False,                  # off = shadow mode: everything waits for a person
    "auto_sign_types": ["nda", "mutual_nda", "service_agreement", "msa", "sow"],
    "require_company_name": False,       # on = auto-sign only when your company's name is in the document
    "max_blocks": 5,                     # more of your blocks than this in one document goes to review
    "model": "qwen3.5-4b-32k:latest",            # qwen3.5:4b with a 32k context (see README)
    "ollama_host": "http://127.0.0.1:11434",
    "ai_timeout": 180,
    "num_ctx": 32768,                            # context size sent with every AI request
    "max_pages": 300,
    "max_mb": 100,
    "flatten_after_signing": False,
}


def load_settings():
    """The defaults, overridden by settings.json. Keys the app no longer uses are dropped."""
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    return settings


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def as_list(value):
    """Accept a list or a comma-separated string; return a clean list of strings."""
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def as_int(value, name, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def validate_settings(new):
    """Check settings sent by the GUI or API. Raises ValueError with a message a person can act on."""
    s = load_settings()
    s.update({k: v for k, v in new.items() if k in DEFAULT_SETTINGS})
    for key in ("company_name", "model", "ollama_host", "date_format", "signature_file"):
        s[key] = str(s[key]).strip()
        if not s[key]:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} can't be empty")
    s["signer_name"] = str(s["signer_name"] or "").strip()
    if Path(s["signature_file"]).suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("The signature file must be a .png or .jpg image, for example data/signature.png")
    for key in ("company_aliases", "usual_roles", "auto_sign_types"):
        s[key] = as_list(s[key])
    unknown = [t for t in s["auto_sign_types"] if t not in policy.DOC_TYPES]
    if unknown:
        raise ValueError(f"Unknown document type(s): {', '.join(unknown)}. Use: {', '.join(policy.DOC_TYPES)}")
    for key in ("auto_sign", "flatten_after_signing", "require_company_name"):
        s[key] = s[key] in (True, "true", "True", 1, "1", "on")
    s["max_blocks"] = as_int(s["max_blocks"], "Most blocks per document", 1, 20)
    s["ai_timeout"] = as_int(s["ai_timeout"], "AI timeout", 10, 1200)
    s["num_ctx"] = as_int(s["num_ctx"], "Context size", 2048, 131072)
    s["max_pages"] = as_int(s["max_pages"], "Max pages", 1, 5000)
    s["max_mb"] = as_int(s["max_mb"], "Max MB", 1, 2000)
    try:
        sample = datetime(2026, 11, 9).strftime(s["date_format"])
    except ValueError:
        raise ValueError("The date format isn't valid. Example: %b %d, %Y gives Nov 09, 2026")
    if not re.search(r"\d", sample):
        raise ValueError("The date format must produce a date, for example %b %d, %Y or %m/%d/%Y")
    return s


def signature_path(settings):
    path = Path(settings["signature_file"])
    return path if path.is_absolute() else HOME / path


def signature_info(settings):
    """Your signature image, read fresh from its file and cleaned in memory (the file isn't changed).

    Without a usable image nothing can be signed: there is no stand-in signature on purpose.
    The fingerprint of the file is logged with every signing, so a swapped image shows up.
    """
    path, shown = signature_path(settings), settings["signature_file"]
    if not path.exists():
        return {"found": False, "path": shown, "aspect": DEFAULT_ASPECT,
                "error": f"No signature image at {shown}. Put your signature there, or upload one in Settings."}
    raw = path.read_bytes()
    try:
        png = pipeline.prepare_signature(raw)
    except Exception as error:
        return {"found": False, "path": shown, "aspect": DEFAULT_ASPECT,
                "error": f"The signature image at {shown} can't be used: {error}"}
    aspect, width, height = pipeline.image_aspect(png)
    return {"found": True, "path": shown, "png": png, "aspect": aspect, "width": width, "height": height,
            "sha256": pipeline.bytes_fingerprint(raw), "error": ""}


# =============================================================================
# The ledger (SQLite), the audit log and the Excel export
# =============================================================================

DB_LOCK = threading.RLock()      # one writer at a time for job IDs and the audit chain


JOB_COLUMNS = ["job_id", "filename", "source", "uploader", "fingerprint", "received_at", "finished_at",
               "status", "reasons", "pages", "lock_level", "doc_type", "parties", "our_role", "signed_where",
               "date_filled", "rules_pick", "ai_pick", "agreed", "model", "model_digest", "prompt_version",
               "reviewer", "decision", "output_path", "output_fingerprint", "signature_sha256", "seconds",
               "error", "record"]


def connect():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def db_query(sql, params=()):
    con = connect()
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_execute(sql, params=()):
    con = connect()
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def init_db():
    con = connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY, "
                    + ", ".join(f"{c} TEXT" if c != "job_id" else "job_id TEXT UNIQUE" for c in JOB_COLUMNS) + ")")
        con.execute("CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, at TEXT, job_id TEXT, event TEXT, "
                    "detail TEXT, prev_hash TEXT, hash TEXT)")
        con.commit()
    finally:
        con.close()


def get_job(job_id):
    rows = db_query("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    return rows[0] if rows else None


def update_job(job_id, **fields):
    for key in fields:
        if key not in JOB_COLUMNS:
            raise KeyError(f"Unknown ledger column: {key}")
    values = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in fields.values()]
    with DB_LOCK:
        db_execute("UPDATE jobs SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE job_id = ?", (*values, job_id))


def now_text():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def audit(job_id, event, detail):
    """Append one line to the audit log. Each line carries the hash of the line before it."""
    with DB_LOCK:
        last = db_query("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")
        prev = last[0]["hash"] if last else "0" * 64
        at = now_text()
        detail_json = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(f"{prev}|{at}|{job_id}|{event}|{detail_json}".encode("utf-8")).hexdigest()
        db_execute("INSERT INTO audit (at, job_id, event, detail, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
                   (at, job_id, event, detail_json, prev, digest))


def verify_audit():
    """Recompute the hash chain. An edited or deleted line breaks it."""
    prev, count = "0" * 64, 0
    for row in db_query("SELECT * FROM audit ORDER BY seq"):
        expected = hashlib.sha256(f"{prev}|{row['at']}|{row['job_id']}|{row['event']}|{row['detail']}".encode("utf-8")).hexdigest()
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"ok": False, "entries": count, "broken_at": row["seq"],
                    "detail": f"Entry {row['seq']} ({row['event']} for {row['job_id']}) was changed or an entry before it was removed"}
        prev, count = row["hash"], count + 1
    return {"ok": True, "entries": count, "detail": f"All {count} entries are intact"}


def public_job(row, with_record=False):
    """A ledger row as the API returns it: JSON columns decoded, the big record only on request."""
    job = {k: v for k, v in row.items() if k != "record"}
    job["reasons"] = json.loads(job["reasons"]) if job.get("reasons") else []
    for key in ("pages", "seconds"):
        if job.get(key) not in (None, ""):
            job[key] = float(job[key]) if key == "seconds" else int(job[key])
    if with_record:
        job["record"] = json.loads(row["record"]) if row.get("record") else {}
    return job


def daily_export():
    """Once a day, save the ledger as an Excel workbook in storage/reports."""
    path = REPORTS / f"ledger-{datetime.now():%Y-%m-%d}.xlsx"
    if path.exists() or not db_query("SELECT 1 FROM jobs LIMIT 1"):
        return
    try:
        export_excel(path)
    except OSError:
        pass          # the file may be open in Excel; the next loop tries again


EXPORT_COLUMNS = [
    ("job_id", "Job ID"), ("filename", "File"), ("source", "Source"), ("uploader", "Uploaded by"),
    ("received_at", "Received"), ("finished_at", "Finished"), ("status", "Status"), ("decision", "Decision"),
    ("reasons", "Reasons"), ("pages", "Pages"), ("lock_level", "Lock level"), ("doc_type", "Document type"),
    ("parties", "Parties"), ("our_role", "Your role"), ("signed_where", "Where it signed"),
    ("date_filled", "Date filled"), ("rules_pick", "Rules' pick"), ("ai_pick", "AI's pick"), ("agreed", "Agreed"),
    ("model", "Model"), ("model_digest", "Model digest"), ("prompt_version", "Prompt version"),
    ("reviewer", "Reviewer"), ("fingerprint", "Original SHA-256"), ("output_fingerprint", "Signed SHA-256"),
    ("output_path", "Signed file"), ("signature_sha256", "Signature image SHA-256"), ("seconds", "Seconds"),
    ("error", "Error"),
]


def excel_value(key, value):
    if value is None:
        return ""
    if key == "reasons":
        value = "; ".join(json.loads(value)) if value else ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))


def add_sheet(workbook, title, rows, first=False):
    sheet = workbook.active if first else workbook.create_sheet()
    sheet.title = title
    sheet.append([label for _, label in EXPORT_COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([excel_value(key, row.get(key)) for key, _ in EXPORT_COLUMNS])
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"        # a file named '=1+2.pdf' stays text, never a formula
    sheet.freeze_panes = "B2"
    for index, (key, label) in enumerate(EXPORT_COLUMNS, start=1):
        width = max([len(label)] + [len(excel_value(key, r.get(key))) for r in rows[:200]])
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(60, width + 2)


def export_excel(path):
    rows = db_query("SELECT * FROM jobs ORDER BY seq")
    workbook = Workbook()
    add_sheet(workbook, "Ledger", rows, first=True)
    add_sheet(workbook, "Auto-signed", [r for r in rows if r.get("decision") == "auto"])
    temp = Path(path).with_suffix(".tmp.xlsx")
    workbook.save(temp)
    os.replace(temp, path)
    return path
