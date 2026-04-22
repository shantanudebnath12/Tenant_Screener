"""Supabase-backed authentication for Flask.

Session handling is deliberately thin:

* The Supabase JS SDK in the browser logs the user in and sets a cookie
  named ``sb-<project-ref>-auth-token`` with the JSON session blob (access
  token, refresh token, user info). The SDK also refreshes tokens on its own.
* On every Flask request, ``current_user()`` extracts the access token from
  that cookie and hands it to ``supabase.auth.get_user(token)``. That call
  does full JWT verification (signature, issuer, expiry). No PyJWT here.
* ``@login_required`` is a route decorator: redirects to ``/login`` on
  failure, forces a password change on first login, and stashes the user
  on ``flask.g`` so views can read ``g.user``.
"""

from __future__ import annotations

import json
import re
from functools import wraps
from typing import Any

from flask import Blueprint, Response, g, jsonify, redirect, render_template, request, url_for

from supabase_client import anon_client, service_client

auth_bp = Blueprint("auth", __name__)


_COOKIE_PATTERN = re.compile(r"^sb-[a-z0-9]+-auth-token(?:\.\d+)?$")


def _project_ref_from_url() -> str | None:
    """Extract the project ref (the 'abcd' in abcd.supabase.co) from SUPABASE_URL."""
    import os

    url = os.environ.get("SUPABASE_URL", "")
    match = re.match(r"https?://([a-z0-9]+)\.supabase\.co", url)
    return match.group(1) if match else None


def _assemble_cookie_value() -> str | None:
    """Supabase JS may split the session across chunked cookies (`.0`, `.1`, …).
    This reassembles them in order and returns the raw value, or None if absent.
    """
    prefix_candidates = []
    ref = _project_ref_from_url()
    if ref:
        prefix_candidates.append(f"sb-{ref}-auth-token")

    # Fall back: scan all cookies for anything matching the Supabase pattern.
    for name in request.cookies:
        if name in prefix_candidates:
            continue
        if _COOKIE_PATTERN.match(name):
            base = name.split(".")[0]
            if base not in prefix_candidates:
                prefix_candidates.append(base)

    for base in prefix_candidates:
        if base in request.cookies:
            return request.cookies[base]
        # Chunked form.
        chunks = []
        i = 0
        while True:
            key = f"{base}.{i}"
            if key not in request.cookies:
                break
            chunks.append(request.cookies[key])
            i += 1
        if chunks:
            return "".join(chunks)
    return None


def _extract_access_token() -> str | None:
    raw = _assemble_cookie_value()
    if not raw:
        return None
    # Newer Supabase JS stores the cookie as a base64-prefixed JSON. Older
    # stores plain JSON. Try both.
    candidate = raw
    if candidate.startswith("base64-"):
        import base64

        try:
            candidate = base64.b64decode(candidate[len("base64-") :]).decode("utf-8")
        except Exception:
            return None
    try:
        blob = json.loads(candidate)
    except (TypeError, ValueError):
        # Sometimes the SDK URL-encodes the value.
        try:
            from urllib.parse import unquote

            blob = json.loads(unquote(candidate))
        except Exception:
            return None
    if isinstance(blob, dict):
        return blob.get("access_token")
    return None


class CurrentUser:
    __slots__ = ("id", "email", "user_metadata")

    def __init__(self, id: str, email: str, user_metadata: dict[str, Any]):
        self.id = id
        self.email = email
        self.user_metadata = user_metadata or {}

    @property
    def must_change_password(self) -> bool:
        return bool(self.user_metadata.get("must_change_password"))


def current_user() -> CurrentUser | None:
    token = _extract_access_token()
    if not token:
        return None
    try:
        resp = anon_client().auth.get_user(token)
    except Exception:
        return None
    user = getattr(resp, "user", None)
    if not user:
        return None
    return CurrentUser(
        id=str(user.id),
        email=user.email or "",
        user_metadata=dict(user.user_metadata or {}),
    )


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("auth.login", next=request.path))
        g.user = user
        if user.must_change_password and request.endpoint not in {
            "auth.change_password",
            "auth.change_password_ack",
            "auth.logout",
            "static",
        }:
            return redirect(url_for("auth.change_password"))
        return view(*args, **kwargs)

    return wrapper


@auth_bp.get("/login")
def login():
    if current_user():
        return redirect(url_for("index"))
    return render_template("login.html", next_path=request.args.get("next", ""))


@auth_bp.get("/logout")
def logout():
    # The Supabase JS SDK clears its own cookie; this page triggers it.
    return render_template("logout.html")


@auth_bp.get("/account/password")
@login_required
def change_password():
    return render_template("change_password.html", forced=g.user.must_change_password)


@auth_bp.post("/account/password/ack")
@login_required
def change_password_ack() -> Response:
    """Called after the browser successfully ran supabase.auth.updateUser({password}).

    Clears ``must_change_password`` on the user's metadata via the admin API.
    """
    try:
        service_client().auth.admin.update_user_by_id(
            g.user.id, {"user_metadata": {"must_change_password": False}}
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})
