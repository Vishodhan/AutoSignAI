"""
Where AutoSign keeps things.

    storage/autosign.db      SQLite: the agreements and the client files (their state, not their history)
    storage/audit-log.csv    the audit log: one row per event, hash-chained, append-only
    storage/agreements/      every uploaded agreement (<id>.pdf), and the company's cursive
                             signature made from the signer's name (<id>.signature.png)
    storage/previews/        each client file's copy as DocuSeal prepared it, with the company's
                             signature on it, for the company to check before it is sent
    storage/signed/          each client file's signed copy, downloaded from DocuSeal
    data/fonts/              the cursive font of the company's signature, kept with the code

AutoSign runs from the project folder (locally the repository, on Railway /app), and finds .env,
data/ and storage/ there. AUTOSIGN_HOME moves storage/ elsewhere (on Railway: the volume, /data);
SIGNATURE_FONT_FILE points at another cursive font.

An agreement row holds, as JSON: the analysis (pipeline.analyze), the AI's opinion, the client's
places ('proposal') and their positions ('placements'), and 'company': the company's places and
their positions. A client file row holds the DocuSeal submission it was sent as.
"""

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("AUTOSIGN_HOME") or ".").resolve()
STORAGE = HOME / "storage"
AGREEMENTS = STORAGE / "agreements"
PREVIEWS = STORAGE / "previews"
SIGNED = STORAGE / "signed"
DB_FILE = STORAGE / "autosign.db"
AUDIT_FILE = STORAGE / "audit-log.csv"
STATIC = Path(__file__).resolve().parent / "static"
SIGNATURE_FONT = Path(os.environ.get("SIGNATURE_FONT_FILE") or "data/fonts/GreatVibes-Regular.ttf").resolve()

LOCK = threading.RLock()        # one writer at a time for the audit chain and the database


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def new_token():
    """A link secret: 144 random bits, so links can't be guessed."""
    return secrets.token_urlsafe(18)


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


# =============================================================================
# The database: agreements and client files
# =============================================================================

AGREEMENT_COLUMNS = ["token", "filename", "firm_name", "signer_name", "signer_title", "company_email", "created_at",
                     "sha256", "analysis", "ai", "proposal", "placements", "reasons", "summary", "status", "company"]
# A client file's status: preview (prepared in DocuSeal, waiting for the company's check), sent,
# opened, signed, declined, or cancelled (the company didn't send it).
FILE_COLUMNS = ["agreement_id", "manage_token", "client_name", "client_email", "reference", "status", "created_at",
                "submission_id", "client_submitter_id", "company_date", "email_sent_at", "viewed_at", "signed_at",
                "signer_name", "signed_sha256", "checked_at"]
JSON_COLUMNS = {"analysis", "ai", "proposal", "placements", "reasons", "company"}
UNIQUE_COLUMNS = {"token", "manage_token"}
FILE_LOOKUPS = {"id", "manage_token", "signed_sha256"}


def connect():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def decode(row):
    """A database row as a dict, with its JSON columns read back."""
    row = dict(row)
    for key in JSON_COLUMNS & row.keys():
        if row[key]:
            row[key] = json.loads(row[key])
        else:
            row[key] = None
    return row


def encode(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def query(sql, params=()):
    con = connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [decode(row) for row in rows]


def first(sql, params):
    rows = query(sql, params)
    if rows:
        return rows[0]
    return None


def execute(sql, params=()):
    con = connect()
    try:
        cursor = con.execute(sql, params)
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def column_definition(column):
    if column in UNIQUE_COLUMNS:
        return f"{column} TEXT UNIQUE"
    return f"{column} TEXT"


def create_table(con, table, columns):
    """Create the table, or add the columns a database from an older version doesn't have yet."""
    definitions = ", ".join(column_definition(column) for column in columns)
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, {definitions})")
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for column in columns:
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def init():
    """Create the folders and tables."""
    for folder in (AGREEMENTS, PREVIEWS, SIGNED):
        folder.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        create_table(con, "agreements", AGREEMENT_COLUMNS)
        create_table(con, "files", FILE_COLUMNS)
        con.commit()
    finally:
        con.close()


def check_columns(table, columns, fields):
    unknown = sorted(set(fields) - set(columns))
    if unknown:
        raise KeyError(f"Unknown {table} column(s): {', '.join(unknown)}")


def insert(table, columns, fields):
    check_columns(table, columns, fields)
    names = list(fields)
    values = [encode(fields[name]) for name in names]
    marks = ", ".join("?" for _ in names)
    with LOCK:
        return execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})", values)


def update(table, columns, row_id, fields):
    check_columns(table, columns, fields)
    names = list(fields)
    values = [encode(fields[name]) for name in names]
    assignments = ", ".join(f"{name} = ?" for name in names)
    with LOCK:
        execute(f"UPDATE {table} SET {assignments} WHERE id = ?", values + [row_id])


def create_agreement(**fields):
    fields = dict(fields, token=new_token(), created_at=now_text())
    return insert("agreements", AGREEMENT_COLUMNS, fields)


def update_agreement(agreement_id, **fields):
    update("agreements", AGREEMENT_COLUMNS, agreement_id, fields)


def agreement_by_token(token):
    return first("SELECT * FROM agreements WHERE token = ?", (token,))


def agreement_by_id(agreement_id):
    return first("SELECT * FROM agreements WHERE id = ?", (agreement_id,))


def all_agreements():
    return query("SELECT * FROM agreements ORDER BY id DESC")


def agreement_pdf(agreement):
    """The agreement exactly as the company uploaded it. This is what DocuSeal gets for each client."""
    return AGREEMENTS / f"{agreement['id']}.pdf"


def signature_png(agreement):
    """The company's signature for this agreement: the signer's name in cursive."""
    return AGREEMENTS / f"{agreement['id']}.signature.png"


def create_file(**fields):
    fields = dict(fields, created_at=now_text())
    return insert("files", FILE_COLUMNS, fields)


def update_file(file_id, **fields):
    update("files", FILE_COLUMNS, file_id, fields)


def file_by(column, value):
    if column not in FILE_LOOKUPS:
        raise KeyError(column)
    return first(f"SELECT * FROM files WHERE {column} = ?", (value,))


def files_for(agreement_id):
    return query("SELECT * FROM files WHERE agreement_id = ? ORDER BY id DESC", (agreement_id,))


def all_files():
    return query("SELECT * FROM files ORDER BY id DESC")


def preview_pdf(file):
    """The copy DocuSeal prepared for this client file, with the company's signature on it."""
    return PREVIEWS / f"{file['id']}.pdf"


def signed_pdf(file):
    return SIGNED / f"{file['id']}.pdf"


# =============================================================================
# The audit log: a CSV file, one row per event, each row carrying the hash of the row before it
# =============================================================================

AUDIT_COLUMNS = ["seq", "time_utc", "event", "agreement_id", "file_id", "document", "client", "reference",
                 "actor", "ip", "detail", "sha256", "prev_hash", "hash"]
GENESIS = "0" * 64
_last = {"seq": None, "hash": None}         # the last row written, kept so each append doesn't re-read the log


def safe_cell(value):
    """Text as stored in the log. A cell starting with = + - @ gets a quote, so Excel never runs it as a formula."""
    if value is None:
        value = ""
    text = " ".join(str(value).split())
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def row_hash(prev, row):
    parts = [prev]
    for column in AUDIT_COLUMNS:
        if column not in ("prev_hash", "hash"):
            parts.append(row[column])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def read_audit():
    if not AUDIT_FILE.exists():
        return []
    with AUDIT_FILE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_last_row():
    rows = read_audit()
    if rows:
        _last["seq"] = int(rows[-1]["seq"])
        _last["hash"] = rows[-1]["hash"]
    else:
        _last["seq"] = 0
        _last["hash"] = GENESIS


def audit(event, agreement=None, file=None, actor="", ip="", detail="", sha256=""):
    """Append one event to the audit log."""
    values = {"event": event, "actor": actor, "ip": ip, "detail": detail, "sha256": sha256,
              "agreement_id": "", "document": "", "file_id": "", "client": "", "reference": ""}
    if agreement:
        values["agreement_id"] = agreement["id"]
        values["document"] = agreement["filename"]
    if file:
        values["file_id"] = file["id"]
        values["client"] = file["client_name"]
        values["reference"] = file["reference"]
    with LOCK:
        if _last["seq"] is None:
            load_last_row()
        values["seq"] = _last["seq"] + 1
        values["time_utc"] = now_text()
        row = {}
        for column in AUDIT_COLUMNS[:-2]:
            row[column] = safe_cell(values[column])
        row["prev_hash"] = _last["hash"]
        row["hash"] = row_hash(row["prev_hash"], row)
        new_log = not AUDIT_FILE.exists()
        with AUDIT_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
            if new_log:
                writer.writeheader()
            writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())
        _last["seq"] = int(row["seq"])
        _last["hash"] = row["hash"]
        return row


def verify_audit():
    """Recompute the hash chain. An edited, inserted or deleted row breaks it."""
    prev = GENESIS
    count = 0
    for row in read_audit():
        if row["prev_hash"] != prev or row["hash"] != row_hash(prev, row):
            return {"ok": False, "entries": count, "broken_at": row["seq"],
                    "detail": f"Row {row['seq']} ({row['event']}) was changed, or a row before it was removed"}
        prev = row["hash"]
        count += 1
    return {"ok": True, "entries": count, "detail": f"All {count} rows are intact"}


def audit_rows(agreement_id):
    """The audit log rows of one agreement and its client files."""
    rows = []
    for row in read_audit():
        if row["agreement_id"] == str(agreement_id):
            rows.append(row)
    return rows


def audit_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=AUDIT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()
