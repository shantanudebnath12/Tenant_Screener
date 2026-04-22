-- RLS policies for the tenant-screening app.
--
-- Model: all Tourbee users see the same data. RLS here is defense-in-depth
-- against a leaked anon key. Flask uses the service role key, which bypasses
-- these policies — it is the primary gatekeeper via @login_required.
--
-- Run this once against your Supabase project (SQL Editor), after the initial
-- migration has created the tables.

ALTER TABLE public.tenants   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenants_authed   ON public.tenants;
DROP POLICY IF EXISTS documents_authed ON public.documents;

CREATE POLICY tenants_authed   ON public.tenants
  FOR ALL TO authenticated
  USING (true) WITH CHECK (true);

CREATE POLICY documents_authed ON public.documents
  FOR ALL TO authenticated
  USING (true) WITH CHECK (true);

-- Storage bucket policy. Create the private bucket `tenant-documents` first
-- (Storage → New bucket → toggle Public off), then run this.
DROP POLICY IF EXISTS tenant_docs_authed ON storage.objects;

CREATE POLICY tenant_docs_authed ON storage.objects
  FOR ALL TO authenticated
  USING (bucket_id = 'tenant-documents')
  WITH CHECK (bucket_id = 'tenant-documents');
