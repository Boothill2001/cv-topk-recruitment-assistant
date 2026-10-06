# CV TopK Recruitment Assistant

Private source repository for the recruitment pilot and its separate English portfolio.
Start reading **[docs/START-HERE.md](docs/START-HERE.md)** for the current architecture, teaching instructions, and debugging map.

| Folder | Purpose |
|---|---|
| recruitment-pilot | Real Vietnamese React/TypeScript + FastAPI + PostgreSQL application |
| portfolio-en | Separate English UI showcase with fictional data and simulated scoring |
| docs | Architecture handoff and code-learning roadmap |

Customer data, credentials, uploads, database, backups, screenshots and videos are intentionally excluded. A clone contains source code, not the existing customer database. Do not treat portfolio sample scores as AI results.

## Local setup

Python 3.11, Node.js/npm and PostgreSQL 17 are needed for the real pilot. Launch `recruitment-pilot/start.bat` on Windows: it prepares dependencies/frontend and a local PostgreSQL cluster using the provided setup script. Default HTTP port: **9652**; private PostgreSQL port: **55432**. Alternatively configure `DATABASE_URL` for a separate database before starting. The scripts are Windows-oriented and expect PostgreSQL binaries to be installed.

Configure provider key files/OAuth only on the backend through the application settings. Never commit populated key files. The example `recruitment-pilot/api-keys.example.txt` has blank values. Exa uses `EXA_API_KEY` via the environment or the ignored `runtime/secrets/exa.env` file.

The cloned pilot starts without customer CVs, JD history or tokens. Supply your own authorized test data. Drive mapping currently contains customer-specific IDs in `backend/integrations.py`; review and configure these before enabling sync on another installation.

English showcase: `portfolio-en/start.bat`, port **8654**. This UI does not call the real recruitment backend or paid AI services.

## Verification

```powershell
cd recruitment-pilot
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run build
```

Tests use isolated fixtures/mocked providers unless a specifically named live diagnostic is deliberately run. Do not run `backend/diagnose_*` or `backend/validate_structured.py` blindly: they can use configured real data/services. Check their code first.

This is a single-customer localhost pilot. Authentication, tenant isolation, cloud deployment and independently benchmarked ranking quality are not provided by making this repository private.
