# Tenant Screener

A small Flask app for screening prospective **employed** tenants. Add an
applicant, upload their supporting documents (pay stubs, bank statements,
credit reports, ID, offer letter), and get an automated eligibility verdict.

The app does two things that matter:

1. **Parses each document's date** so stale documents (e.g. a pay stub older
   than 60 days) can be flagged.
2. **Verifies the applicant's name appears in each document** to catch
   mismatched or fraudulent uploads.

## Stack

- Python 3.10+
- Flask + Flask-SQLAlchemy (SQLite)
- pdfplumber for PDF text extraction
- python-dateutil for date parsing
- Plain Jinja templates + a single CSS file

## Run it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run
```

Then open <http://127.0.0.1:5000/>.

The SQLite DB lives in `instance/tenants.db` and uploads in `uploads/` —
both are gitignored.

## Eligibility rules (tunable in `eligibility.py`)

| Rule | Pass | Warn | Fail |
|---|---|---|---|
| Income to rent | ≥ 3× | — | < 3× |
| Credit score | ≥ 650 | 600–649 | < 600 |
| Bank reserve | ≥ 2× rent | 1–2× rent | < 1× rent |
| Required docs | pay stub, bank statement, credit report, ID present | — | any missing |
| Pay stub age | ≤ 60 days | 61–90 days | > 90 days |
| Bank statement age | ≤ 60 days | 61–90 days | > 90 days |
| Credit report age | ≤ 90 days | 91–120 days | > 120 days |
| Name on document | match / fuzzy match (≥ 0.85) | partial (0.70–0.85) or unknown | no match |

Verdict: **Approved** (all pass) · **Conditional** (≥ 1 warn, no fails) · **Denied** (any fail).

## Document parsing

Only PDFs are auto-parsed today. For other files (images, DOCX), the upload
still works — the document is stored, but you'll need to set the date and
confirm the name match manually on the document row.

If parsing gets the wrong date, edit the date inline on the detail page or hit
**Re-parse**.
