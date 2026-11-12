"""
AutoSignPDF: finds your company's signature blocks in a contract PDF and stamps your signature and the date.

    pipeline.py   the four steps: Parse -> Detect -> Verify -> Sign (plus page previews and the
                  signature image)
    ai.py         the local model: its second opinion on the spots, and its look at the signed page
    policy.py     every word list, distance and limit the rules use (tune the rules here)
    storage.py    folders, settings, the SQLite ledger, the audit log and the Excel export
    app.py        intake, the job queue and worker, the REST API and the GUI page; `main()` starts it

Start it with `uv run autosign`.
"""

__version__ = "1.0"
