"""Create the initial admin user in Supabase Auth.

Run this once, locally, after your Supabase project is up. Requires
SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment.

    python scripts/seed_admin.py

The user is flagged with user_metadata.must_change_password=True so their
first sign-in forces a password rotation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from supabase_client import service_client

ADMIN_EMAIL = "shantanu@tourbee.ca"
ADMIN_PASSWORD = "Rentiv123"


def main() -> None:
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

    client = service_client()
    try:
        resp = client.auth.admin.create_user(
            {
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
                "email_confirm": True,
                "user_metadata": {"must_change_password": True},
            }
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "already been registered" in msg or "already exists" in msg:
            print(f"{ADMIN_EMAIL} already exists — no changes made.")
            return
        raise
    user = getattr(resp, "user", resp)
    print(f"Created admin: {ADMIN_EMAIL} (id={getattr(user, 'id', '?')})")
    print("First sign-in will force a password change.")


if __name__ == "__main__":
    main()
