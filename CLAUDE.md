# AutoSign (AutoSignPDF)

Take-home assessment for Astra AI. A company uploads its client agreement once; AutoSign signs it for
the company once; for every new client file the company copies a signing link; the client signs in the
browser; the company gets the signed PDF (both signatures) and an audit log. It must work on an
agreement it has never seen (the tester uses the "MIND client agreement").

Deadline: **Sunday 15 November 2026, about 14:16 PST** (22:16 UTC). No GitHub commits after it.
Repository: https://github.com/Vishodhan/AutoSignAI. Hosting: Railway (Hobby plan, volume mounted at `/data`).

## Rules for working on this project

1. **Never delete a file or resource without asking first and waiting for the user's answer.**
2. **Never make a financial decision without the user's answer** (Railway plan, paid services, API billing, deployments that cost money).
3. **Write high-quality, readable Python**, the way a human software engineer would: clear names, short functions,
   one step per line. Avoid one-liners, dense comprehensions, chained conditional expressions and other compressed syntax.
4. **When anything is unclear, ask the user right away.** Don't assume.
5. Push to GitHub only when the user says so.
6. AVoid creating more than 10 Python files. The whole workflow should be simple and easy to understand for the developer and the user.

## Layout

```
src/autosign/
  app.py        FastAPI: pages (/, /a/<token>, /s/<token>, /verify) and the REST API (/api/...);
                also reads the local .env file (the Gemini key) at start-up, never logging values
  workflow.py   detect() who signs where (rules + AI), check_places() after the company edits them,
                presign() for the company, sign() for the client, add the certificate page
  pipeline.py   PDF parsing, signature-spot detection, stamping and checks (PyMuPDF); "ours" = the client here
  ai.py         Google Gemini second opinion on which spots are the client's and the company's (optional)
  storage.py    SQLite for state (agreements, files), audit-log.csv (hash-chained) for history;
                <id>.pdf is the upload, <id>.presigned.pdf the copy the company signed (what clients sign)
  policy.py     word lists, roles and limits
  static/       the HTML pages, CSS and JavaScript (no frameworks)
tests/          pytest; tests make their own PDFs and skip the sample files that aren't present
```

## Commands

```
uv run autosign                  # run locally at http://127.0.0.1:8000
uv run pytest                    # tests (no AI key needed)
uv run pytest -m live -s         # live Gemini tests (needs GEMINI_API_KEY)
```

## Decisions so far

- **PDF only.** Word files (.docx, .doc) are refused with a message to save them as PDF first. No Word-to-PDF
  conversion, no LibreOffice, no Dockerfile: Railway builds the Python app directly.
- No logins: access is by private random links. No emails: the company copies the signing link.
- **The company signs first, automatically**, then each client signs that pre-signed copy. The company's
  date is the day AutoSign adds the company's signature. The signed PDF has both signatures and a certificate page.
- If the agreement has no block for the company (or no signature image is set up), the agreement page says
  so and only the client signs.
- The company's signature image is `data/signature.png`, committed with the code (PNG files may be committed).
  The app runs from the project folder (locally the repo, on Railway /app) and finds `.env`, `data/` and
  `storage/` there; `AUTOSIGN_HOME` moves `storage/` (Railway: `/data`), `COMPANY_SIGNATURE_FILE` the image.
  Uploading the image through the app is left for later.
- The client can type, draw or upload their signature on the signing page.
- When rules and AI disagree (or the AI isn't set up), the company must confirm the signature places before
  links can be created. On the agreement page the company chooses who signs at each place (the client, the
  company, or nobody) and can add places by hand for either side; AutoSign then signs for the company again.
- PDFs whose digital certification forbids a stamp (lock level 1 or 2), or whose signature fields can't be
  read, are refused at upload. Already-signed PDFs are stamped append-only so their signatures stay valid.
- One page per agreement for the company (`/a/<token>`): status, the client files dashboard (client name, link
  created, opened, signed, by whom, signed PDF, copy link), the audit CSV, and the signature places. No separate file page.

- Sample PDFs in `data/` are not committed (several are licensed for internal testing only).
- The Gemini key lives only in `.env` locally (git-ignored) and in Railway's Variables tab. Never print, log or commit it.

## On hold (don't change until the user says so)

- `static/common.js`: a shared signature pad. The signing page has its own inline pad for now.
