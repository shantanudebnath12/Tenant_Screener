# Agent Context

Handoff notes for future Claude sessions / other agents working on this repo.
Read this **before** the README — it explains the *why* and where the moving
parts live.

## What this is

A Flask web app for screening **employed** tenant applicants. Users add an
applicant, upload supporting documents (pay stubs, bank statements, credit
reports, ID, offer letters), and the app produces an Approved / Conditional /
Denied verdict with itemized reasons.

Two checks are first-class signals (not just nice-to-haves) and motivated the
whole design:

1. **Document date parsing** — flag stale documents (e.g. pay stubs > 60 days)
2. **Name verification** — confirm the tenant's name appears in each document
   (fraud signal: applicants submitting someone else's pay stubs)

## Stack

Python 3.10+ · Flask · Flask-SQLAlchemy (SQLite) · pdfplumber · python-dateutil ·
Jinja templates · single hand-written CSS file. **No build step, no JS framework.**

## File map

| File | Purpose |
|---|---|
| `app.py` | Flask app factory + all routes + small template filters |
| `models.py` | `Tenant` and `Document` SQLAlchemy models; `DOCUMENT_TYPES` enum |
| `document_parser.py` | PDF text extraction, date regex + `dateutil` parsing, name matching via `difflib.SequenceMatcher` |
| `eligibility.py` | Rules engine. **All thresholds are constants at the top of the file** |
| `templates/base.html` | Layout, flash messages, top nav |
| `templates/index.html` | Dashboard: applicants table with verdict pill |
| `templates/tenant_form.html` | New / edit applicant form |
| `templates/tenant_detail.html` | Info, documents table, verdict panel, upload form |
| `static/style.css` | Single CSS file, ~200 lines. Verdict pills are color-coded |
| `instance/tenants.db` | SQLite DB (gitignored, created on first run) |
| `uploads/<tenant_id>/<uuid>_<name>` | Uploaded files (gitignored) |

## Key design decisions

- **SQLite + local files** so the app is one `pip install` + `flask run`. Swapping to Postgres later is just changing `SQLALCHEMY_DATABASE_URI`.
- **Files stored as `<tenant_id>/<uuid>_<secure_filename>`** — uuid prevents overwrites; `secure_filename` + `send_from_directory` block path traversal.
- **Parsing only handles PDFs.** Images / DOCX get `parse_status='unsupported'` and the user fills the date + confirms the name manually. Adding OCR (pytesseract) is the obvious next step but not in scope.
- **Name match is a graded signal**, not a boolean: `match | fuzzy | partial | no_match | unknown`. `unknown` (parser couldn't run) → `warn`, not `fail`, with a manual-confirm checkbox.
- **No auth.** Single-user / local-trust app. Adding Flask-Login is a future step if needed.
- **No tests yet.** Smoke-tested by hand and via inline scripts at build time. Adding pytest is a clear next step.

## How parsing works (`document_parser.py`)

`parse_document(file_path, mime_type, tenant_name) -> ParseResult`:

1. If not a PDF → return `unsupported` / `unknown`. Done.
2. Extract text with `pdfplumber` (text-layer only — no OCR).
3. **Date:** run a set of regexes for common formats (US slash, ISO, "Month DD, YYYY", "DD Month YYYY"), parse each candidate with `dateutil`, filter to a plausible window (5 years past → 30 days future), return the **most recent**.
4. **Name:** normalize both the tenant name and the extracted text (lowercase, strip punctuation, collapse whitespace). Try variants: full name, `first last`, `last first`. Exact substring → `match`. Otherwise sliding-window `SequenceMatcher` ratio: ≥0.85 → `fuzzy`, 0.70–0.85 → `partial`, else `no_match`.

## How eligibility works (`eligibility.py`)

`evaluate(tenant) -> Verdict` runs each rule, collects `Reason(level, message)` items, then:

- Any `fail` → **Denied**
- Any `warn`, no fails → **Conditional**
- All `pass` → **Approved**

Rules: income/rent ratio (≥3×), credit score (≥650), bank reserve (≥2× rent), all required docs present (pay stub / bank statement / credit report / ID), per-doc freshness (60/90 days for pay stub & bank statement; 90/120 for credit report), and **per-doc name match** for required docs.

To tune thresholds, edit the constants at the top of `eligibility.py` — that's the only place they live.

## Routes

```
GET   /                                       Dashboard
GET   /tenants/new        POST same           Create
GET   /tenants/<id>                           Detail (info, docs, verdict)
GET   /tenants/<id>/edit  POST same           Edit
POST  /tenants/<id>/delete                    Delete (cascades docs + files)
POST  /tenants/<id>/documents                 Upload doc
POST  /documents/<id>/date                    Manual date override
POST  /documents/<id>/confirm-name            Manual name confirmation
POST  /documents/<id>/reparse                 Re-run parser
POST  /documents/<id>/delete                  Delete doc
GET   /documents/<id>/download                Serve file
```

## Running / verifying

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run
# → http://127.0.0.1:5000/
```

Smoke test path: create a tenant (`income=6000, rent=2000, credit=720, balance=5000`),
upload a recent PDF pay stub, confirm date + name match populate, then upload a
PDF with a different person's name → verdict flips to Denied with the
name-mismatch reason.

## Git / branch

Working branch: `claude/tenant-screening-app-TtNI1`. Initial commit: `c94b715`.
Push went through after the user granted the Claude GitHub App write access on
`shantanudebnath12/Tenant_Screener`.

## Likely next steps (not yet done)

- OCR for image-only PDFs and image uploads (pytesseract)
- Pytest suite, especially around `document_parser` and `eligibility`
- Authentication / multi-user
- Email-the-applicant flow
- Bulk export (PDF / CSV) of an applicant's screening report
