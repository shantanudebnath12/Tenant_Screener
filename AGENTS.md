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

## Stack (Phase 2)

- **Backend**: Flask (Python 3.11+), SQLAlchemy 2, Alembic
- **Database**: **Supabase Postgres** via PgBouncer pooler (port 6543)
- **Storage**: **Supabase Storage**, private bucket `tenant-documents`
- **Auth**: **Supabase Auth** (email/password + Google OAuth; restricted to `@tourbee.ca`)
- **Extraction**: OpenAI vision (`gpt-4o-mini`) + PyMuPDF for rasterisation
- **Hosting**: **Vercel Hobby** (Fluid Compute, 60s function duration)
- **Frontend**: Jinja templates + hand-written CSS + a few lines of vanilla JS (Supabase JS SDK from CDN for login + direct-to-Storage uploads)

## Access model

All authenticated Tourbee users see and edit every applicant. There is **no
per-user ownership logic**. `tenants.created_by_user_id` and
`tenants.created_by_email` are stored for audit display only — never used in
`WHERE` clauses.

Domain restriction (`@tourbee.ca`) is enforced at the Supabase Google provider.
Email/password signups are disabled at the provider level; only users created
via the admin API (via `scripts/seed_admin.py` or the Supabase dashboard) can
sign in with a password.

## File map

| File | Purpose |
|---|---|
| `app.py` | Flask app factory, all routes, template filters. Every `/tenants/*` and `/documents/*` route is behind `@login_required`. |
| `auth.py` | Supabase JWT verification (via `supabase.auth.get_user(token)`), `@login_required`, `/login`, `/logout`, `/account/password`. |
| `supabase_client.py` | Lazy cached Supabase clients (service-role for server work, anon for token verification). |
| `models.py` | `Tenant` and `Document` SQLAlchemy models, UUID primary keys. |
| `document_extractor.py` | OpenAI vision extractor (unchanged from Phase 1). |
| `document_parser.py` | Deterministic fallback parser (unchanged). |
| `eligibility.py` | Rules engine (unchanged). |
| `templates/base.html` | Layout, auth nav, loads Supabase JS SDK globally. |
| `templates/login.html` | Password + Google button, driven entirely by Supabase JS SDK. |
| `templates/change_password.html` | Forced on first login via `must_change_password` metadata flag. |
| `templates/logout.html` | Triggers `supabase.auth.signOut()` then redirects. |
| `templates/tenant_form.html` | New-applicant form uploads docs direct-to-Storage before POSTing. |
| `templates/tenant_detail.html` | Upload form posts direct-to-Storage, then notifies Flask. |
| `static/style.css` | Single CSS file. |
| `alembic.ini`, `migrations/` | Schema migrations — `python scripts/migrate.py` runs `upgrade head`. |
| `api/index.py`, `vercel.json` | Vercel Python runtime entry point. |
| `scripts/seed_admin.py` | Creates `shantanu@tourbee.ca` with forced password change. |
| `scripts/migrate.py` | Wraps `alembic upgrade head`. |
| `scripts/supabase_rls.sql` | "Authenticated can do anything" RLS policies (defense in depth). |
| `docs/DEPLOY.md` | Step-by-step deployment walkthrough. |

## Session handling (the bit worth understanding)

- The browser loads Supabase JS SDK from CDN in `base.html`.
- `/login` uses it directly: `sb.auth.signInWithPassword(...)` or
  `sb.auth.signInWithOAuth({provider: 'google'})`.
- The SDK owns the session: stores it in localStorage + writes an
  `sb-<ref>-auth-token` cookie automatically.
- On every Flask request, `auth.current_user()` parses that cookie and hands
  the access token to `supabase.auth.get_user(token)`. That one call does full
  JWT verification (signature, expiry, issuer). **No PyJWT, no custom cookie
  endpoint.**
- Token refresh is handled by the SDK, in the browser.

## File upload flow (direct-to-Storage)

Vercel Hobby caps request bodies at 4.5MB, so files bypass Flask:

1. Browser → Flask: `POST /tenants/<id>/documents/upload-url { filename }`.
2. Flask uses the service role key to mint a **signed upload URL** (5-min
   expiry) and returns `{ signed_url, token, storage_path, bucket }`.
3. Browser uploads the file bytes using `sb.storage.from(bucket)
   .uploadToSignedUrl(path, token, file)`.
4. Browser → Flask: `POST /tenants/<id>/documents` with the `storage_path`.
5. Flask downloads from Storage into `/tmp`, runs `extract_document(...)`,
   inserts the `Document` row, recomputes the verdict.

Download flow: Flask mints short-lived signed URLs during
`tenant_detail` template rendering; links point to those.

On new-applicant, the browser generates a UUID up front, uses it as both the
storage-path prefix and the eventual `Tenant.id` (server adopts it via the
`tenant_id_hint` form field).

## Routes

```
GET   /login                                Supabase-SDK-driven login page
GET   /logout                               Signs out + redirects
GET/POST /account/password                  Forced on first login
POST  /account/password/ack                 Clears must_change_password

GET   /                                     Dashboard (login required)
GET/POST /tenants/new                       Create applicant
GET   /tenants/<uuid>                       Detail + verdict
GET/POST /tenants/<uuid>/edit               Edit
POST  /tenants/<uuid>/delete                Delete

POST  /tenants/<uuid>/documents/upload-url  → signed URL for direct upload
POST  /tenants/<uuid>/documents             Register an uploaded object; runs extraction

POST  /documents/<uuid>/date                Manual date override
POST  /documents/<uuid>/confirm-name        Manual name confirmation
POST  /documents/<uuid>/reparse             Re-run extractor
POST  /documents/<uuid>/delete              Delete doc + Storage object
GET   /documents/<uuid>/download            302 to a signed download URL
```

## Running locally

See `docs/DEPLOY.md` for the full setup. Short version:

```bash
cp .env.example .env              # fill SUPABASE_* and DATABASE_URL
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/migrate.py         # create tables in Supabase
python scripts/seed_admin.py      # one-time
flask --app app run
```

## Known risks / trade-offs

- **Vercel Python runtime is in beta**; if it regresses, fallback plan is
  Render ($7/mo) — the Flask code is a plain WSGI app, migration is ~1hr.
- **Cold starts** 1–3s after 15min idle.
- **PgBouncer transaction mode** forbids server-side prepared statements; we
  append `?prepared_statement_cache_size=0` in the `DATABASE_URL` and set
  `prepare_threshold=None` in SQLAlchemy engine options.

## Git / branch

Working branch: `claude/tenant-screening-app-TtNI1`.

## Likely next steps (not yet done)

- Pytest suite around the Supabase Storage flow and auth middleware
- Background processing for large PDFs (so the upload response returns fast)
- Per-user activity log (we already stamp `created_by_email`)
- Bulk export (PDF / CSV) of an applicant's screening report
