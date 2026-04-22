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
| `app.py` | Flask app factory + all routes + small template filters. Loads `.env` on startup. |
| `models.py` | `Tenant` and `Document` SQLAlchemy models; `DOCUMENT_TYPES` enum. `Document` now stores extracted values from the LLM. |
| `document_extractor.py` | **Primary extractor.** Renders PDF pages to images via PyMuPDF, sends to OpenAI (`gpt-4o-mini`) with a strict JSON-schema structured output. Extracts doc type, date, name, issuer, and financial figures (income, balance, credit score, ID expiry). Falls back to `document_parser` on failure. |
| `document_parser.py` | **Deterministic fallback.** PDF text extraction + regex date parsing + `SequenceMatcher` name match. Used when `OPENAI_API_KEY` is missing or the API call fails. |
| `eligibility.py` | Rules engine. **All thresholds are constants at the top of the file** |
| `templates/base.html` | Layout, flash messages, top nav |
| `templates/index.html` | Dashboard: applicants table with verdict pill |
| `templates/tenant_form.html` | New / edit applicant form (create form has doc upload slots; edit form has all numeric fields) |
| `templates/tenant_detail.html` | Info, documents table (with extracted-values drop-down), verdict panel, upload form |
| `static/style.css` | Single CSS file. Verdict, confidence, and name-match pills are color-coded |
| `.env` | `OPENAI_API_KEY` + `OPENAI_MODEL`. Gitignored. Copy from `.env.example`. |
| `instance/tenants.db` | SQLite DB (gitignored, created on first run) |
| `uploads/<tenant_id>/<uuid>_<name>` | Uploaded files (gitignored) |

## Key design decisions

- **SQLite + local files** so the app is one `pip install` + `flask run`. Swapping to Postgres later is just changing `SQLALCHEMY_DATABASE_URI`.
- **Files stored as `<tenant_id>/<uuid>_<secure_filename>`** — uuid prevents overwrites; `secure_filename` + `send_from_directory` block path traversal.
- **Parsing only handles PDFs.** Images / DOCX get `parse_status='unsupported'` and the user fills the date + confirms the name manually. Adding OCR (pytesseract) is the obvious next step but not in scope.
- **Name match is a graded signal**, not a boolean: `match | fuzzy | partial | no_match | unknown`. `unknown` (parser couldn't run) → `warn`, not `fail`, with a manual-confirm checkbox.
- **No auth.** Single-user / local-trust app. Adding Flask-Login is a future step if needed.
- **No tests yet.** Smoke-tested by hand and via inline scripts at build time. Adding pytest is a clear next step.

## How extraction works

There are two layers. `document_extractor.extract_document(path, mime, tenant_name)` is the entry point called by every upload/reparse route.

### Primary: LLM extractor (`document_extractor.py`)

1. Render up to `MAX_PAGES` (default 3) of the PDF to PNG via PyMuPDF (`fitz`) at 150 DPI. No OCR, no poppler — PyMuPDF handles both text and scanned PDFs by rasterizing them.
2. Base64-encode the images and send to OpenAI chat completions with `response_format={"type": "json_schema", "json_schema": EXTRACTION_SCHEMA, "strict": true}`. Schema is in `EXTRACTION_SCHEMA` at the top of the file.
3. The model returns exactly the schema fields: `document_type`, `document_date` (ISO), `date_label`, `name_on_document`, `issuer`, per-doc-type figures (`gross_monthly_income`, `closing_balance`, `credit_score`, `id_expiration_date`, …), `confidence` (high/medium/low), `notes`.
4. **Name match is not delegated to the LLM.** We take the LLM's `name_on_document` and run `SequenceMatcher` against the tenant's full name ourselves — deterministic, inspectable, not subject to "is this the same person?" hallucination.

### Fallback: deterministic parser (`document_parser.py`)

Used automatically when `OPENAI_API_KEY` is missing or the API call fails. Same contract. PDF text via `pdfplumber`, date regex + `dateutil`, name match via `SequenceMatcher` sliding window. The note on the document row tells you which path ran.

### Auto-population of tenant fields

After every upload/reparse, `app._refresh_tenant_from_documents(tenant)` reads the latest document of each relevant type and pulls values into the Tenant record:
- Pay stub `gross_monthly_income` → `tenant.monthly_income`
- Bank statement `closing_balance` → `tenant.bank_balance`
- Credit report `credit_score` → `tenant.credit_score`

Last-upload-wins per doc type. The user can still override any value on the edit form.

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

- Pytest suite, especially around `document_extractor`, `document_parser`, and `eligibility`
- Cost / latency tracking per extraction call
- Retry logic for transient OpenAI errors (currently fails straight to fallback)
- Surface `document_type_predicted` mismatches more prominently in the UI (already shows a ⚠ line)
- Authentication / multi-user
- Email-the-applicant flow
- Bulk export (PDF / CSV) of an applicant's screening report
