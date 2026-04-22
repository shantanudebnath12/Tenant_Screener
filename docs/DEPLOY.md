# Deploying Tenant Screener

Target: Supabase (database + auth + storage) + Vercel Hobby (Flask). Total cost: $0.

## Prerequisites

- A Supabase account (supabase.com)
- A Vercel account (vercel.com) with the GitHub repo connected
- A Google Cloud project for OAuth credentials
- Python 3.11+ locally, for running the seed + migration scripts once

## 1. Create the Supabase project

1. supabase.com → **New project**. Region close to your users. Save the
   generated database password — you'll need it.
2. Open **Project Settings → API** and note:
   - `Project URL` (e.g. `https://abcdwxyz.supabase.co`) → `SUPABASE_URL`
   - `anon` public key → `SUPABASE_ANON_KEY`
   - `service_role` secret → `SUPABASE_SERVICE_ROLE_KEY`
3. Open **Project Settings → Database → Connection string → URI** and pick the
   **Transaction pooler** entry (port **6543**). That goes in `DATABASE_URL`,
   with `?prepared_statement_cache_size=0` appended. Example:

   ```
   postgresql+psycopg2://postgres.abcdwxyz:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres?prepared_statement_cache_size=0
   ```

## 2. Configure Supabase Auth

1. **Authentication → Providers → Email**: leave enabled. **Disable**
   self-serve signups under "Confirm email" settings so only admin-created
   users can sign in.
2. **Authentication → Providers → Google**: toggle on. Paste the Client ID +
   Secret from your Google Cloud OAuth consent screen (see step 3). Set
   **Authorized Domains** to `tourbee.ca` so only that domain is accepted.
3. Google Cloud Console → APIs & Services → Credentials → **Create OAuth
   client ID** (Web application).
   - **Authorized redirect URI**: `https://<ref>.supabase.co/auth/v1/callback`
     (NOT your Vercel URL — Supabase receives the OAuth callback, then hands
     off to your site).

## 3. Create the Storage bucket

Supabase Studio → **Storage → New bucket**:
- Name: `tenant-documents`
- Public bucket: **off**

## 4. Run migrations + seed the admin (one-time, local)

From a checkout on your laptop:

```
cp .env.example .env
# Fill in SUPABASE_* and DATABASE_URL

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/migrate.py         # creates tenants + documents tables
python scripts/seed_admin.py      # creates shantanu@tourbee.ca / Rentiv123
```

Then apply RLS policies — paste `scripts/supabase_rls.sql` into the Supabase
SQL Editor and run it.

## 5. Deploy to Vercel

1. Vercel dashboard → **Add New → Project** → import the GitHub repo.
2. Framework preset: **Other**. Build & Output settings: leave defaults
   (the `vercel.json` at the repo root handles the Python runtime).
3. Environment variables — add all of these under Production + Preview:
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL` = `gpt-4o-mini`
   - `SECRET_KEY` = run `python -c "import secrets; print(secrets.token_hex(32))"`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `SUPABASE_STORAGE_BUCKET` = `tenant-documents`
   - `DATABASE_URL` (pooled, port 6543, with `?prepared_statement_cache_size=0`)
4. Click **Deploy**.

Visit the generated `<project>.vercel.app` URL → sign in as
`shantanu@tourbee.ca` / `Rentiv123` → change your password → invite teammates
(they can sign in with their `@tourbee.ca` Google accounts).

## 6. Rolling out updates

1. Bump code on the branch, open a PR, merge to `main`. Vercel auto-deploys.
2. If the change requires a schema migration: **before** merging, run
   `DATABASE_URL=... python scripts/migrate.py` from a local checkout against
   the prod DB. Migrations are backwards-compatible by convention, so the
   in-flight code keeps working.

## Known quirks

- **Cold starts** (~1–3s) after 15 min of idle. First request from a cold
  instance will feel slow; subsequent requests are fast.
- **Function duration** on Vercel Hobby is 60s (Fluid Compute). Very large
  PDFs + slow OpenAI responses can approach this; we cap uploads at 20MB.
- **Vercel's Python runtime is in beta.** If it regresses, the app is a plain
  WSGI Flask app — switching to Render ($7/mo) is a one-hour config change.
