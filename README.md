# AutoSignPDF

Finds your company's signature block(s) in a contract PDF, has Python rules and a local AI model
pick them independently, runs an 8-point check, and stamps your signature image and the date on a
copy. Anything that isn't certain waits in a review queue in the browser. Everything runs on this
computer; nothing is sent anywhere.

The design is in `docs/AutoSignPDF_System_Design.html`.

## Set up (once)

1. Install [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com).
2. In this folder: `uv sync` (creates `.venv` with Python 3.13 and every package).
3. `ollama pull qwen3.5:4b`, then make the 32k-context variant the app uses: save a file named
   `Modelfile` with the two lines `FROM qwen3.5:4b` and `PARAMETER num_ctx 32768`, and run
   `ollama create qwen3.5-4b-32k -f Modelfile` (it reuses the same weights, so no extra space).
   Set `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`, then restart Ollama: they keep
   the 32k context within a 6 GB GPU. The model and context size can be changed in Settings.
4. Put your signature image at `data/signature.png` (PNG or JPG; a white background is removed
   automatically). Without it, nothing is signed.

## Run

```
uv run autosign
```

(`uv run python -m autosign.app` does the same.)

Open http://127.0.0.1:8000. API documentation: http://127.0.0.1:8000/docs.

- **Upload** PDFs in the GUI, or put them in `inbox/` and click **Process inbox** on the Dashboard.
- Documents that pass all 8 checks are signed automatically **only when Auto-sign is on** in
  Settings. It starts off (shadow mode): every document waits in **Review**, where you approve,
  pick other spots, place the signature by hand, or reject.
- Signed PDFs go to `output/<name>-signed.pdf` (and stay downloadable from **History**). The
  ledger (`storage/ledger.db`) records every document; **Export to Excel** saves it as a workbook.

Set your company name, its other names, the roles you usually have and the date format in
**Settings**. "Require your company name" makes auto-sign stricter: only documents that contain
your name are signed without a person.

Programs calling the API must send the header `X-AutoSign: 1` with POST and PUT requests. The app
answers only on 127.0.0.1, so other websites in your browser can't use it.

## Test

```
uv run pytest              # fast tests: rules, signing, client-signed PDFs, the API (no AI needed)
uv run pytest -m live -s   # live tests with the real models in Ollama (prints time per answer)
```

The tests build their own PDFs (filled contracts, side-by-side and multi-page blocks, traps,
client-signed copies at each lock level) and also run on the five samples in `data/`.

## Project layout

```
AutoSignPDF/
├── pyproject.toml            dependencies, the `autosign` command, test settings
├── src/autosign/             the code (an installable package)
│   ├── pipeline.py           the four steps, one section each: Parse -> Detect -> Verify -> Sign
│   │                         (plus page previews and cleaning the signature image)
│   ├── ai.py                 the local model: its second opinion, and its look at the signed page
│   ├── policy.py             every word list, distance and limit the rules use (tune the rules here)
│   ├── storage.py            folders, settings, the SQLite ledger, the audit log, Excel export
│   ├── app.py                intake (upload, inbox), the job queue, the REST API; main() starts it
│   ├── __init__.py           marks the folder as a package; lists the modules
│   └── static/index.html     the GUI (HTML, CSS and JavaScript, no libraries)
├── tests/
│   ├── conftest.py           test setup: a throwaway data folder and shared fixtures
│   ├── helpers.py            builds the test PDFs (each with a known right answer); shared helpers
│   ├── test_pipeline.py      the four steps on your samples and generated documents; live AI tests
│   └── test_app.py           the web app: uploads, review actions, security, the worker
├── data/                     your sample contracts and signature.png
├── docs/                     the design plan
├── settings.json             your settings (written by the Settings screen)
└── inbox/, output/, storage/ incoming PDFs, signed PDFs, the app's records (created on first run)
```

## Not handled in this version

- Scanned PDFs (no text layer) are rejected.
- Pages that are rotated go to review; placing by hand doesn't work on them.
- If the client certified the PDF with lock level 1 it is rejected; lock level 2 waits for review
  (both would need a certificate signature, which this version doesn't make).
