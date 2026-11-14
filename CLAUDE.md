# AutoSign (AutoSignPDF)

Take-home assessment for Astra AI. A company enters its details (company name, signer's name and job title,
company email) and uploads its client agreement once; AutoSign finds the places and writes the signer's name in
cursive as the company's signature. For every new client the company enters the client's name and email;
DocuSeal signs the client's copy for the company, the company checks it, DocuSeal emails the client, the client
signs on DocuSeal's page, and the company gets the signed PDF, a dashboard and an audit log. It must work on an
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
  app.py        FastAPI: pages (/, /dashboard, /a/<token>, /verify) and the REST API (/api/...);
                also reads the local .env file (Gemini and DocuSeal keys) at start-up, never logging values
  workflow.py   detect() who signs where (rules + AI), check_places() after the company edits them,
                typed_signature_png(): the signer's name in cursive (Pillow, data/fonts/GreatVibes-Regular.ttf)
  docuseal.py   DocuSeal REST API: fields on the places, the company pre-signed, send, status, signed PDF
  pipeline.py   PDF parsing, signature-spot detection with each block's date and name lines, and the exact
                rectangles of the places (PyMuPDF); "ours" = the client here. DocuSeal does the signing
  ai.py         Google Gemini second opinion on which spots are the client's and the company's (optional)
  storage.py    SQLite for state (agreements, client files), audit-log.csv (hash-chained) for history;
                <id>.pdf the upload, <id>.signature.png the cursive signature, previews/ and signed/ per client file
  policy.py     word lists, roles and limits
  static/       the HTML pages, CSS and JavaScript (no frameworks)
tests/          pytest; tests make their own PDFs, use a stand-in DocuSeal, and skip sample files that aren't present
```

## Commands

```
uv run autosign                  # run locally at http://127.0.0.1:8000
uv run pytest                    # tests (no keys needed; DocuSeal is a stand-in)
uv run pytest -m live -s         # live Gemini and DocuSeal Sandbox tests (keys in .env; the DocuSeal one
                                 # emails DOCUSEAL_TEST_EMAIL once)
```

## Decisions so far

- **PDF only.** Word files (.docx, .doc) are refused with a message to save them as PDF first. No Word-to-PDF
  conversion, no LibreOffice, no Dockerfile: Railway builds the Python app directly.
- No logins and no private dashboard link: one company uses AutoSign, on a site link shared only with it.
  Agreement pages are still private random links (`/a/<token>`); `/dashboard` lists everything.
- **DocuSeal (REST API only; MCP and Agent Skills are coding references) does the signing and the emails.**
  For each client file AutoSign creates a submission from the PDF with one numbered field per place
  ("Company signature 1", "Client date 1"); the company's fields are pre-filled with its cursive signature and
  the date (the day the client's copy is prepared and sent) and marked completed; nothing is emailed until the
  company has checked DocuSeal's partly signed copy. Then DocuSeal emails the client (who signs on DocuSeal's
  page); `bcc_completed` sends the signed copy to the company's email. Nothing is added to DocuSeal's finished
  PDF (DocuSeal seals it). AutoSign checks DocuSeal created exactly the fields it sent; any difference stops it.
- The company's signature is the signer's name in cursive (name only), a transparent PNG shaped like the
  largest place (448 x 128 px for 112 x 32 pt), made with Pillow from `data/fonts/GreatVibes-Regular.ttf` (OFL).
  The signer's name is required; the job title is optional (kept in DocuSeal's record and on the dashboard,
  never written on the PDF).
- Only the signature, the date and the Name line are filled, for each side. The company's Name line gets the
  signer's name; the client's is pre-filled with the client's name and the client can correct it. Title lines
  are never filled. Each date gets its whole date line as its DocuSeal box (DocuSeal pads text boxes and wraps
  text that doesn't fit, so a box as narrow as the date wrapped it onto two lines).
- The rules (PyMuPDF) find each block; Gemini checks and numbers them; DocuSeal fields go on those exact
  rectangles. Existing blocks are reused (printed lines, empty form fields), never duplicated; already signed
  places are left alone. PDFs that already carry a digital signature are refused at upload (DocuSeal removes
  a PDF's own signature fields, so that signature would no longer verify).
- No two filled places may overlap (two signatures, or two places sharing one date or name line):
  `workflow.overlap_problems` makes the company confirm the places at upload, refuses such places when the
  company confirms them, and the app refuses to prepare a client's copy while any remain.
- If the agreement has no block for the company, the agreement page says so and only the client signs.
- `README.md` covers the problem statement, infrastructure, workflow and configuration only; the assessment's
  deliverables (what was built, features in and out, tools) are in `DELIVERABLES.md`.
- The app runs from the project folder (locally the repo, on Railway /app) and finds `.env`, `data/` and
  `storage/` there; `AUTOSIGN_HOME` moves `storage/` (Railway: `/data`), `SIGNATURE_FONT_FILE` the font.
- When rules and AI disagree (or the AI isn't set up), the company must confirm the signature places before
  anything is sent. On the agreement page the company chooses who signs at each place (the client, the
  company, or nobody) and can add places by hand for either side.
- PDFs whose digital certification forbids a stamp (lock level 1 or 2), or whose signature fields can't be
  read, are refused at upload.
- Client file statuses (preview, sent, opened, signed, declined, cancelled) come from DocuSeal, checked when a
  page loads (each file at most every 15 s, at most 10 per load, to respect the Sandbox's limits). No webhooks.
- The DocuSeal Sandbox key lives only in `.env` for now; it is not added to Railway until the user says so,
  so the deployed site can't send yet. Production DocuSeal (Pro, paid) is the user's decision.

- Sample PDFs in `data/` are not committed (several are licensed for internal testing only).
- The Gemini and DocuSeal keys live only in `.env` locally (git-ignored) and, later, in Railway's Variables tab.
  Never print, log or commit them. Test email addresses also stay in `.env` (DOCUSEAL_TEST_EMAIL), not in code.

## On hold (don't change until the user says so)

- `static/common.js`: a shared signature pad. (Clients sign on DocuSeal's page now; the old signing page was removed.)
