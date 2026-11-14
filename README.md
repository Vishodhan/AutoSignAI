# AutoSign

## Problem statement

Every new client of a firm has to sign the same client agreement before work on their file can
start. Doing that by hand means preparing the agreement for each client, signing it for the firm,
sending it, chasing the signature, and filing the signed copy. AutoSign automates it: the firm
uploads its agreement once, and for every new client it only enters the client's name and email.
It must work on an agreement it has never seen before.

## Infrastructure

| Part | What it does |
|---|---|
| **Web app** (Python 3.13, FastAPI) | The pages and the REST API. Plain HTML, CSS and JavaScript, no front-end framework. |
| **PDF reading** (PyMuPDF, pyHanko) | Finds every signature place, its date line and its Name line, from the exact positions of the words, lines, boxes and form fields. Reads digital signatures already in the PDF. |
| **AI check** (Google Gemini 3.1 Flash-Lite) | A second opinion on which numbered places are the client's and which are the company's. It never gives positions. |
| **Company signature** (Pillow) | Writes the signer's name in cursive (Great Vibes font, SIL Open Font License) as a transparent PNG. |
| **Signing and email** (DocuSeal REST API) | Puts the company's signature on each client's copy, emails the client, hosts the client's signing page, seals the finished PDF, and emails the signed copy to both sides. |
| **State** (SQLite) | Agreements and client files. |
| **History** (`audit-log.csv`) | One row per event, each row carrying the hash of the row before it, so an edited or deleted row is detected. |
| **Hosting** (Railway) | Builds from `pyproject.toml` and `uv.lock` (no Dockerfile). A volume at `/data` keeps the database, the PDFs and the audit log across deploys. |

```
src/autosign/
  app.py        pages (/, /dashboard, /a/<token>, /verify) and the REST API (/docs)
  workflow.py   who signs where (rules + AI), checking the places, the cursive signature
  docuseal.py   DocuSeal's API: fields on the places, the company pre-signed, emails, status, signed PDF
  pipeline.py   reading the PDF: signature places, their dates and Name lines (PyMuPDF)
  ai.py         Gemini's second opinion on whose each place is
  storage.py    SQLite for agreements and client files; the hash-chained audit log
  policy.py     word lists, roles and limits
  static/       the pages
data/fonts/     the cursive font
tests/          pytest
```

## Workflow

1. **The company enters its details and uploads the agreement** (PDF, once): the company's name,
   who signs for it (job title optional) and the company's email. AutoSign writes the signer's name
   in cursive: that is the company's signature.
2. **AutoSign finds where everyone signs.** Rules find every signature place and decide whose it is
   from the agreement's own party definitions; Gemini checks and numbers the same places. Blocks
   already in the PDF are reused, never duplicated. AutoSign asks the company to confirm the places
   when the rules and Gemini disagree, when two places would overlap, or when the PDF needs a
   person's eye (hidden text, instructions aimed at an AI, JavaScript, special signing rules).
   PDFs that already carry a digital signature are refused.
3. **The company adds a client** (name and email). AutoSign creates a DocuSeal submission with
   numbered fields on the exact places: for each side a signature, a date and a printed name
   ("Company signature 1", "Client date 1", "Client name 1"). Title lines are left blank. The
   company's fields are filled with its cursive signature, the signer's name and today's date; the
   client's name is pre-filled for the client to confirm. AutoSign checks that DocuSeal created
   exactly the fields it sent, and nothing is emailed yet.
4. **The company checks** the copy DocuSeal prepared, then sends it (or cancels it).
5. **DocuSeal emails the client**, who signs on DocuSeal's page. DocuSeal emails the signed copy to
   the client and to the company; AutoSign keeps a copy and records its SHA-256.
6. **The dashboard** (`/dashboard`) shows every client: the document, the email address, when the
   email went out, and whether the client opened or signed it (asked of DocuSeal when a page loads).
   `/verify` tells whether a PDF is exactly a copy signed through AutoSign.

## Configuration

**Keys** go in a `.env` file in the project folder locally (never committed), and in Railway's
Variables tab when deployed. They are never logged or sent to the browser.

| Variable | Needed for |
|---|---|
| `DOCUSEAL_API_KEY` | Sending anything (a DocuSeal Sandbox key while testing) |
| `GEMINI_API_KEY` | The AI check (without it, every agreement asks the company to confirm the places) |
| `AUTOSIGN_HOME` | Where `storage/` lives (Railway: `/data`, the volume) |
| `DOCUSEAL_TEST_EMAIL` | Only for the live DocuSeal test: the address it emails |
| `DOCUSEAL_API_URL`, `SIGNATURE_FONT_FILE` | Optional: another DocuSeal host, another cursive font |

**Run locally**

```
uv sync                 # Python 3.13 and every package, in .venv
uv run autosign         # http://127.0.0.1:8000
```

**Tests**

```
uv run pytest              # no keys needed: the tests make their own PDFs and use a stand-in DocuSeal
uv run pytest -m live -s   # also asks Gemini and the DocuSeal Sandbox (needs their keys)
```

Tests that use the sample PDFs in `data/` skip when those files aren't there (they aren't in the
repository; several are licensed for internal testing only).

**Deploy on Railway**

1. New project → Deploy from GitHub repo → this repository (`railway.json` holds the start command
   and the health check).
2. Add a volume mounted at `/data`.
3. Variables: `AUTOSIGN_HOME=/data`, `DOCUSEAL_API_KEY`, `GEMINI_API_KEY`.
4. Settings → Networking → Generate Domain.

Run a single instance: the SQLite database and the audit log live on the one volume.
