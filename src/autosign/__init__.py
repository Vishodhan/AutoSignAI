"""
AutoSign: a company uploads its client agreement once, AutoSign signs it for the company, and each
new client signs it in the browser through a private link.

    app.py        the web app: the pages and the REST API; `main()` starts it (uv run autosign)
    workflow.py   who signs where (rules + AI), signing for the company and for the client, the certificate
    pipeline.py   reading the PDF, finding the signature spots, stamping a copy and checking it
    ai.py         Gemini's second opinion on whose each spot is
    storage.py    the SQLite database, the stored PDFs and the hash-chained audit log
    policy.py     every word list, distance and limit the rules use
"""
