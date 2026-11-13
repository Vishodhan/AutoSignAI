# AutoSign

Every new client has to sign the same agreement before work on their file can start. AutoSign
automates that:

1. **Upload the agreement once** (PDF). AutoSign finds where the client signs and where your
   company signs, even in an agreement it has never seen.
2. **Check the places** when AutoSign isn't sure. It says why, and you choose who signs at each
   place (or add a place by hand). Nothing is sent until you confirm.
3. **AutoSign signs for your company** once, on the copy every client will sign.
4. **Open a client file** (one case for one client) and copy its private signing link to the client.
5. **The client signs in the browser**: types, draws or uploads a signature and ticks a consent box.
6. **You get the signed PDF** (both signatures plus a signing certificate page) and an audit log,
   on a dashboard that shows every client file: link created, opened, signed, and by whom.

There are no accounts and no emails. Each agreement page and each signing link is a long random
secret, and you send the signing link yourself. Use test data only.

## Run it locally

```
uv sync                 # Python 3.13 and every package, in .venv
uv run autosign         # http://127.0.0.1:8000 (API documentation at /docs)
```

Run it from the project folder: AutoSign reads `.env`, `data/` and `storage/` there.

- **Your company's signature** is `data/signature.png` (PNG or JPEG; a white background is
  removed automatically). `COMPANY_SIGNATURE_FILE` points at another image. Without one, AutoSign
  says so and only the client signs.
- **The AI check (optional):** put `GEMINI_API_KEY=...` in a `.env` file in this folder. Without a
  key everything still works, but every agreement asks you to confirm the places.

## Tests

```
uv run pytest              # no AI key needed: the tests make their own PDFs
uv run pytest -m live -s   # also asks Gemini (needs GEMINI_API_KEY)
```

Tests that use the sample PDFs in `data/` skip when those files aren't there (they aren't in the
repository; several are licensed for internal testing only).

## Deploy on Railway

1. New project → **Deploy from GitHub repo** → this repository. Railway builds it from
   `pyproject.toml` and `uv.lock` (no Dockerfile); `railway.json` holds the start command and the
   health check. The company's signature image comes with the code (`data/signature.png`).
2. Add a **volume** mounted at `/data` (it keeps the database, the PDFs and the audit log across deploys).
3. **Variables**: `AUTOSIGN_HOME=/data`, and `GEMINI_API_KEY` for the AI check.
4. **Settings → Networking → Generate Domain** for the public `*.up.railway.app` address.

## How it works

**Finding the places.** PyMuPDF reads the exact positions of the words, lines, boxes and form
fields. Rules find every signature spot and date spot, and decide whose each one is from the
agreement's own party definitions (for example *Acme Inc. (the "Provider") and the client (the
"Client")*). Google Gemini gets the same numbered spots, as text and as pictures, and says which
are the client's and which are the company's. Gemini never gives coordinates: it only chooses
among the numbered spots, so a stamp's position always comes from the PDF itself.

AutoSign is **sure** only when the rules and Gemini agree, every stamp fits without covering text,
and nothing in the PDF needs a person's eye (hidden text, instructions aimed at an AI, JavaScript,
special signing rules such as wet ink or a notary). Otherwise the agreement page lists the reasons,
and the company chooses who signs where before any link can be created. PDFs whose digital
certification forbids adding a signature are refused.

**Signing.** Each signature is cleaned (background removed, cropped, padded to the stamp's shape)
and stamped at its place with the date. After every stamp, the signed copy is checked against the
one before it: the signature and the date are where they should be, and nothing else on any page
changed. A copy that fails a check is never kept. PDFs that already carry digital signatures are
only changed append-only, so those signatures stay valid.

**The record.** The signed PDF ends with a signing certificate: who signed for each side, when,
from which IP address and browser, the consent text, and the SHA-256 of the agreement before
signing. Every step is a row in `storage/audit-log.csv`, and each row carries the hash of the row
before it, so an edited or deleted row is detected. `/verify` tells whether a PDF is exactly a
copy AutoSign signed.

## Layout

```
src/autosign/
  app.py        the web app: pages and REST API (/docs)
  workflow.py   who signs where (rules + AI), signing for the company and the client, the certificate page
  pipeline.py   reading the PDF, finding signature spots, stamping and checking (PyMuPDF)
  ai.py         Gemini's second opinion on whose each spot is
  storage.py    SQLite for agreements and client files; the hash-chained audit log (CSV)
  policy.py     word lists, roles and limits
  static/       the pages (plain HTML, CSS and JavaScript)
tests/          pytest
```

## Tools

Python 3.13, FastAPI, PyMuPDF, pyHanko, Pillow, Google Gemini (`gemini-3.1-flash-lite`), uv,
Railway. Built with Claude Code as the AI coding assistant.
