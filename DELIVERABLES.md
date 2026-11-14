# AutoSign: deliverables

- **Working URL:** https://autosignai-production.up.railway.app (dashboard at `/dashboard`, API docs at `/docs`)
- **Repository:** https://github.com/Vishodhan/AutoSignAI (public)
- Test data only: no real personal information is used anywhere.

## What was built

AutoSign gets a firm's client agreement signed on every new client file. The firm uploads its
agreement once, with its name, the signer's name (job title optional) and its email. AutoSign finds
where the firm signs and where the client signs, even in an agreement it has never seen, and writes
the signer's name in cursive as the firm's signature. For each new client, the firm enters the
client's name and email: DocuSeal puts the firm's signature, name and date on the client's copy, the
firm checks it, and DocuSeal emails the client, who signs in the browser. The firm gets the signed PDF
by email and on its dashboard, with an audit log of every step.

## Try it (about 5 minutes)

1. Open the working URL. Enter a company name **exactly as it is written in the agreement**, a
   signer's name, a company email you can read, and upload the agreement (PDF).
2. On the agreement page, check the places AutoSign found (green: the client, blue: the company).
   If AutoSign asks, confirm or change them.
3. Under **Send it to a client**, enter a client name and an email you can read, and press
   **Prepare the client's copy**. Check the company's signature on the copy, then press
   **Signatures look right: email the client**.
4. Open the email from DocuSeal and sign as the client. The signed copy arrives by email to the
   client and to the company, and `/dashboard` shows it as signed with a link to the signed PDF.

## Features included, and why

| Feature | Why |
|---|---|
| Works on agreements it has never seen | The brief's main test. PyMuPDF rules read the exact positions of words, lines, boxes and form fields, find every signature place, and decide whose it is from the agreement's own party definitions ("Acme Inc. (the "Provider")"). |
| Gemini as a second opinion, never as the placer | In a comparison on scored documents, Gemini alone put places in the wrong row or over printed text (83% accurate, 7 errors per pass); the rules scored 100% after one fix. So the rules decide positions and Gemini checks whose each numbered place is. |
| The firm confirms when AutoSign isn't sure | When the rules and Gemini disagree, when two places would overlap, or when the PDF needs a person's eye (hidden text, instructions aimed at an AI, JavaScript, notary or wet-ink rules), nothing is sent until the firm confirms the places. It can reassign each place or add one by hand. |
| Cursive signature from the signer's name (Pillow) | No image to upload or prepare: the firm's signature is consistent on every copy, sized to fit the signature place exactly, on a transparent background. |
| DocuSeal for signing and email | A legally recognised e-signature flow with its own audit trail and a digitally sealed final PDF, instead of an in-house signing page. AutoSign calls only its REST API. |
| The firm checks each copy before it is sent | The firm sees the exact copy DocuSeal will send, with its signature on it, and can cancel it. AutoSign also checks that DocuSeal created exactly the fields it sent. |
| Signature, date and printed name filled for both sides | The firm's name and date are filled in; the client's name is pre-filled for the client to confirm. Title lines are left blank, as decided. |
| Existing blocks reused, signed places left alone | Fields go on the blocks already printed in the agreement (lines and empty form fields), never duplicated. |
| Dashboard | Every client: document, email address, when the email went out, opened, signed, signed PDF. |
| Hash-chained audit log and `/verify` | Every step is a row carrying the hash of the row before it, so an edited or deleted row is detected; `/verify` tells whether a PDF is exactly a copy signed through AutoSign. |
| Secrets kept out of code | API keys only in `.env` (git-ignored) and Railway's Variables; never logged, never sent to the browser; the DocuSeal key is only sent to DocuSeal's API over HTTPS. |

## Features left out, and why

| Left out | Why |
|---|---|
| Accounts and logins | One firm uses it; agreement pages are private random links. Accounts would come first in a multi-firm product. |
| Word (.docx) uploads | PDF only, to keep the exact page positions the placement depends on; the firm is asked to save as PDF. |
| PDFs already digitally signed | Refused at upload: DocuSeal removes a PDF's own signature fields, so the earlier signature would no longer verify (tested). |
| Scanned PDFs (no text layer) | No OCR: positions from OCR aren't reliable enough to sign on. |
| Webhooks | Statuses are fetched from DocuSeal when a page opens, so no public webhook endpoint or secret is needed. |
| A "sent" email to the firm | DocuSeal can't send arbitrary emails; the firm gets the signed copy when the client signs, and the dashboard shows when each request went out. |
| Title lines, and different names per client block | Not filled by decision. When a client side has several blocks (e.g. LLC members), each is pre-filled with the client's name and the client corrects it on DocuSeal's page. |
| Production DocuSeal plan | The deployed app uses a DocuSeal Sandbox key (free, for testing). Production needs DocuSeal Pro ($20/user/month + $0.20 per signed document). |

## Decisions on unclear points

- The firm signs first, automatically, on each client's copy; the firm's date is the day that copy is
  prepared and sent.
- If the agreement has no block for the firm, only the client signs (the agreement page says so).
- The client signs on DocuSeal's page; nothing is added to DocuSeal's sealed final PDF.
- No two filled places may overlap; if they would, the firm must choose the places before sending.

## How it was tested

- **78 automated tests** (pytest): the rules on generated agreements with known answers and on real
  templates, the whole web flow against a stand-in DocuSeal (prepare, check, send, opened, signed,
  dashboard, audit log, `/verify`), refusals, and malformed AI answers. Live tests run against Gemini
  and the DocuSeal Sandbox.
- **End to end with the real DocuSeal Sandbox:** a Service Contract was prepared, checked, emailed,
  signed by the client from the email, and recorded as signed on the dashboard with the signed PDF.
- **Sample agreements:** the flow was run on the sample agreements (employment, LLC operating,
  service, mutual NDAs, independent-contractor, lease, government forms). Where AutoSign was sure or
  the firm confirmed, the firm's signature, date and name landed on the right lines; agreements with
  no signature block, or a block it couldn't tie to the client, stopped for the firm to choose the
  places; digitally signed PDFs were refused.

## Tools and AI assistants used

- **Python 3.13**, **uv**, **FastAPI** and **Uvicorn**, **PyMuPDF**, **pyHanko**, **Pillow**, **httpx**, **SQLite**, **pytest**
- **Google Gemini 3.1 Flash-Lite** (`google-genai`): the second opinion on whose each place is
- **DocuSeal REST API** (Sandbox): signing, emails, the client's signing page; DocuSeal's Agent Skills
  documentation as the API reference while building
- **Railway**: hosting, with a volume for the database, PDFs and audit log
- **Claude Code** (Anthropic): the AI coding assistant used for design, code, tests and reviews
