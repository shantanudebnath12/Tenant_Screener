"""Lazy, module-level Supabase clients.

Two clients exist:

* ``service_client()`` — initialized with the service role key. Bypasses RLS.
  Used for all server-side reads, writes, signed-URL generation, and admin
  user management.
* ``anon_client()`` — initialized with the anon key. Used to verify a user's
  access token via ``supabase.auth.get_user(token)``; the SDK attaches the
  token to the request itself.

Both clients are cached per process so cold-start overhead is paid once.
"""

from __future__ import annotations

import os
from functools import lru_cache

from supabase import Client, create_client


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


@lru_cache(maxsize=1)
def service_client() -> Client:
    return create_client(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))


@lru_cache(maxsize=1)
def anon_client() -> Client:
    return create_client(_env("SUPABASE_URL"), _env("SUPABASE_ANON_KEY"))


def storage_bucket() -> str:
    return os.environ.get("SUPABASE_STORAGE_BUCKET", "tenant-documents")
