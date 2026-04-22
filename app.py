"""Tenant Screener — Flask application.

All state lives in Supabase: Postgres for rows, Storage for uploaded documents,
Auth for users. Flask is the gatekeeper (every route behind ``@login_required``)
and runs the OpenAI extraction + eligibility logic.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

load_dotenv()

import eligibility
from auth import auth_bp, login_required
from document_extractor import extract_document
from models import DOCUMENT_TYPES, Document, Tenant, db
from supabase_client import service_client, storage_bucket

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
SIGNED_URL_TTL_SECONDS = 300  # 5 minutes


def create_app(database_uri: str | None = None) -> Flask:
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        database_uri
        or os.environ.get("DATABASE_URL")
        or _local_sqlite_fallback()
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # PgBouncer transaction-mode doesn't support prepared statements.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith(("postgresql", "postgres")):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {"prepare_threshold": None},
            "pool_pre_ping": True,
        }
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    db.init_app(app)

    app.register_blueprint(auth_bp)
    _register_routes(app)
    _register_template_helpers(app)
    return app


def _local_sqlite_fallback() -> str:
    base_dir = Path(__file__).parent.resolve()
    instance_dir = base_dir / "instance"
    instance_dir.mkdir(exist_ok=True)
    return f"sqlite:///{instance_dir / 'tenants.db'}"


# --- Form helpers ---------------------------------------------------------


def _parse_date_field(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _tenant_from_form(form, tenant: Tenant | None = None) -> Tenant:
    t = tenant or Tenant()
    t.full_name = (form.get("full_name") or "").strip()
    t.email = (form.get("email") or "").strip()
    t.phone = (form.get("phone") or "").strip()
    t.employer = (form.get("employer") or "").strip()
    t.job_title = (form.get("job_title") or "").strip()
    t.monthly_income = _parse_float(form.get("monthly_income"))
    t.bank_balance = _parse_float(form.get("bank_balance"))
    t.credit_score = _parse_int(form.get("credit_score"))
    t.target_rent = _parse_float(form.get("target_rent"))
    t.move_in_date = _parse_date_field(form.get("move_in_date"))
    t.notes = (form.get("notes") or "").strip()
    return t


def _validate_tenant(t: Tenant) -> list[str]:
    errors: list[str] = []
    if not t.full_name:
        errors.append("Full name is required.")
    if not t.email:
        errors.append("Email is required.")
    if t.target_rent <= 0:
        errors.append("Rent must be greater than 0.")
    if t.monthly_income < 0:
        errors.append("Monthly income cannot be negative.")
    return errors


# --- Storage helpers ------------------------------------------------------


def _storage_path(tenant_id: uuid.UUID, filename: str) -> str:
    safe = secure_filename(filename) or "upload.bin"
    return f"{tenant_id}/{uuid.uuid4().hex}_{safe}"


def _create_signed_upload_url(path: str) -> dict:
    """Return {token, signed_url, path} via Supabase Storage's signed upload."""
    bucket = service_client().storage.from_(storage_bucket())
    resp = bucket.create_signed_upload_url(path)
    # Shape: {'signed_url': ..., 'token': ..., 'path': ...}
    return resp


def _create_signed_download_url(path: str) -> str | None:
    try:
        bucket = service_client().storage.from_(storage_bucket())
        resp = bucket.create_signed_url(path, SIGNED_URL_TTL_SECONDS)
        return resp.get("signedURL") or resp.get("signed_url")
    except Exception:
        return None


def _download_storage_to_tmp(path: str) -> Path:
    bucket = service_client().storage.from_(storage_bucket())
    data = bucket.download(path)
    suffix = Path(path).suffix or ".bin"
    tmp = NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return Path(tmp.name)


def _delete_storage_object(path: str) -> None:
    try:
        service_client().storage.from_(storage_bucket()).remove([path])
    except Exception:
        pass


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# --- Extraction wiring ----------------------------------------------------


def _process_uploaded_object(
    tenant: Tenant, storage_path: str, doc_type: str, original_filename: str, mime_type: str | None
) -> Document:
    """Fetch an already-uploaded object from Storage, run extraction, build a
    Document row ready to be added to the session."""
    local = _download_storage_to_tmp(storage_path)
    try:
        size = local.stat().st_size
        result = extract_document(local, mime_type, tenant.full_name)
    finally:
        try:
            local.unlink()
        except OSError:
            pass
    return Document(
        tenant_id=tenant.id,
        doc_type=doc_type,
        original_filename=original_filename,
        storage_path=storage_path,
        mime_type=mime_type,
        size_bytes=size,
        parsed_document_date=result.document_date,
        parse_status=result.parse_status,
        parse_note=result.parse_note,
        name_match_status=result.name_match_status,
        name_match_score=result.name_match_score,
        matched_name=result.matched_name,
        document_type_predicted=result.document_type_predicted,
        issuer=result.issuer,
        extraction_confidence=result.extraction_confidence,
        extracted_values_json=result.extracted_values_json,
    )


def _refresh_tenant_from_documents(tenant: Tenant) -> None:
    def latest_of(doc_type: str):
        docs = [d for d in tenant.documents if d.doc_type == doc_type]
        return max(docs, key=lambda d: d.uploaded_at) if docs else None

    pay_stub = latest_of("pay_stub")
    if pay_stub:
        value = pay_stub.extracted_values.get("gross_monthly_income")
        if isinstance(value, (int, float)) and value > 0:
            tenant.monthly_income = float(value)

    bank = latest_of("bank_statement")
    if bank:
        value = bank.extracted_values.get("closing_balance")
        if isinstance(value, (int, float)):
            tenant.bank_balance = float(value)

    credit = latest_of("credit_report")
    if credit:
        value = credit.extracted_values.get("credit_score")
        if isinstance(value, int) and 300 <= value <= 900:
            tenant.credit_score = value


# --- Routes ---------------------------------------------------------------


def _register_routes(app: Flask) -> None:

    @app.get("/")
    @login_required
    def index():
        tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
        rows = [(t, eligibility.evaluate(t)) for t in tenants]
        return render_template("index.html", rows=rows)

    @app.route("/tenants/new", methods=["GET", "POST"])
    @login_required
    def new_tenant():
        if request.method == "POST":
            tenant = _tenant_from_form(request.form)
            # The new-applicant page uploads files to a pending UUID before the
            # tenant exists. Adopt that same UUID so storage paths line up.
            hint = request.form.get("tenant_id_hint", "").strip()
            if hint:
                try:
                    tenant.id = uuid.UUID(hint)
                except ValueError:
                    pass
            tenant.created_by_user_id = uuid.UUID(g.user.id)
            tenant.created_by_email = g.user.email
            errors = _validate_tenant(tenant)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "tenant_form.html", tenant=tenant, mode="new", doc_types=DOCUMENT_TYPES
                )
            db.session.add(tenant)
            db.session.flush()  # assign the UUID before we attach documents

            # The form posts a JSON array of {storage_path, doc_type, original_filename, mime_type}
            # for files the browser already uploaded directly to Storage.
            uploads_blob = request.form.get("uploads_json", "[]")
            import json as _json

            try:
                uploads = _json.loads(uploads_blob)
            except (TypeError, ValueError):
                uploads = []

            doc_count = 0
            for item in uploads:
                try:
                    doc = _process_uploaded_object(
                        tenant,
                        storage_path=item["storage_path"],
                        doc_type=item.get("doc_type", "other"),
                        original_filename=item.get("original_filename", "upload.bin"),
                        mime_type=item.get("mime_type"),
                    )
                except Exception:
                    continue
                db.session.add(doc)
                doc_count += 1

            if doc_count:
                db.session.flush()
                _refresh_tenant_from_documents(tenant)

            db.session.commit()

            msg = "Applicant created."
            if doc_count:
                msg += f" {doc_count} document{'s' if doc_count != 1 else ''} uploaded."
            flash(msg, "success")
            return redirect(url_for("tenant_detail", tenant_id=tenant.id))
        return render_template(
            "tenant_form.html", tenant=Tenant(), mode="new", doc_types=DOCUMENT_TYPES
        )

    @app.route("/tenants/<uuid:tenant_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_tenant(tenant_id: uuid.UUID):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        if request.method == "POST":
            _tenant_from_form(request.form, tenant)
            errors = _validate_tenant(tenant)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("tenant_form.html", tenant=tenant, mode="edit")
            db.session.commit()
            flash("Applicant updated.", "success")
            return redirect(url_for("tenant_detail", tenant_id=tenant.id))
        return render_template("tenant_form.html", tenant=tenant, mode="edit")

    @app.get("/tenants/<uuid:tenant_id>")
    @login_required
    def tenant_detail(tenant_id: uuid.UUID):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        verdict = eligibility.evaluate(tenant)
        download_urls = {
            str(doc.id): _create_signed_download_url(doc.storage_path) for doc in tenant.documents
        }
        return render_template(
            "tenant_detail.html",
            tenant=tenant,
            verdict=verdict,
            doc_types=DOCUMENT_TYPES,
            download_urls=download_urls,
        )

    @app.post("/tenants/<uuid:tenant_id>/delete")
    @login_required
    def delete_tenant(tenant_id: uuid.UUID):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        for doc in list(tenant.documents):
            _delete_storage_object(doc.storage_path)
        db.session.delete(tenant)
        db.session.commit()
        flash("Applicant deleted.", "success")
        return redirect(url_for("index"))

    @app.post("/tenants/<uuid:tenant_id>/documents/upload-url")
    @login_required
    def document_upload_url(tenant_id: uuid.UUID) -> Response:
        """Ask Flask for a signed URL the browser can PUT a file to directly.

        The tenant doesn't have to exist yet — on the new-applicant page the
        browser generates a UUID, uploads against it, then submits the form
        which creates the tenant with that same UUID. The register_document
        step enforces tenant existence.
        """
        payload = request.get_json(silent=True) or {}
        filename = (payload.get("filename") or "").strip()
        if not filename or not _allowed(filename):
            return jsonify({"error": "Unsupported file type."}), 400
        path = _storage_path(tenant_id, filename)
        info = _create_signed_upload_url(path)
        return jsonify(
            {
                "storage_path": path,
                "signed_url": info.get("signed_url"),
                "token": info.get("token"),
                "bucket": storage_bucket(),
            }
        )

    @app.post("/tenants/<uuid:tenant_id>/documents")
    @login_required
    def register_document(tenant_id: uuid.UUID) -> Response:
        """Called after the browser has uploaded the bytes to Storage."""
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        payload = request.get_json(silent=True) or {}
        storage_path = payload.get("storage_path")
        doc_type = payload.get("doc_type", "other")
        original_filename = payload.get("original_filename", "upload.bin")
        mime_type = payload.get("mime_type")
        if not storage_path:
            return jsonify({"error": "storage_path required"}), 400
        doc = _process_uploaded_object(
            tenant,
            storage_path=storage_path,
            doc_type=doc_type,
            original_filename=original_filename,
            mime_type=mime_type,
        )
        db.session.add(doc)
        db.session.flush()
        _refresh_tenant_from_documents(tenant)
        db.session.commit()
        return jsonify({"ok": True, "document_id": str(doc.id)})

    @app.post("/documents/<uuid:doc_id>/date")
    @login_required
    def update_document_date(doc_id: uuid.UUID):
        doc = db.session.get(Document, doc_id) or abort(404)
        doc.manual_document_date = _parse_date_field(request.form.get("manual_document_date"))
        db.session.commit()
        flash("Document date updated.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<uuid:doc_id>/confirm-name")
    @login_required
    def confirm_document_name(doc_id: uuid.UUID):
        doc = db.session.get(Document, doc_id) or abort(404)
        doc.name_manually_confirmed = bool(request.form.get("confirmed"))
        db.session.commit()
        flash("Name confirmation updated.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<uuid:doc_id>/reparse")
    @login_required
    def reparse_document(doc_id: uuid.UUID):
        doc = db.session.get(Document, doc_id) or abort(404)
        try:
            local = _download_storage_to_tmp(doc.storage_path)
        except Exception as exc:
            flash(f"Could not fetch document from storage: {exc}", "error")
            return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))
        try:
            result = extract_document(local, doc.mime_type, doc.tenant.full_name)
        finally:
            try:
                local.unlink()
            except OSError:
                pass
        doc.parsed_document_date = result.document_date
        doc.parse_status = result.parse_status
        doc.parse_note = result.parse_note
        doc.name_match_status = result.name_match_status
        doc.name_match_score = result.name_match_score
        doc.matched_name = result.matched_name
        doc.document_type_predicted = result.document_type_predicted
        doc.issuer = result.issuer
        doc.extraction_confidence = result.extraction_confidence
        doc.extracted_values_json = result.extracted_values_json
        _refresh_tenant_from_documents(doc.tenant)
        db.session.commit()
        flash("Document re-parsed.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<uuid:doc_id>/delete")
    @login_required
    def delete_document(doc_id: uuid.UUID):
        doc = db.session.get(Document, doc_id) or abort(404)
        tenant_id = doc.tenant_id
        _delete_storage_object(doc.storage_path)
        db.session.delete(doc)
        db.session.commit()
        flash("Document deleted.", "success")
        return redirect(url_for("tenant_detail", tenant_id=tenant_id))

    @app.get("/documents/<uuid:doc_id>/download")
    @login_required
    def download_document(doc_id: uuid.UUID):
        doc = db.session.get(Document, doc_id) or abort(404)
        url = _create_signed_download_url(doc.storage_path)
        if not url:
            abort(404)
        return redirect(url)


def _register_template_helpers(app: Flask) -> None:
    @app.template_filter("money")
    def money(value):
        try:
            return f"${float(value):,.0f}"
        except (TypeError, ValueError):
            return "$0"

    @app.template_filter("date_or_dash")
    def date_or_dash(value):
        if not value:
            return "—"
        return value.strftime("%b %d, %Y")

    @app.context_processor
    def inject_globals():
        return {
            "today": date.today(),
            "current_user": getattr(g, "user", None),
            "supabase_url": os.environ.get("SUPABASE_URL", ""),
            "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
            "storage_bucket_name": storage_bucket(),
        }


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
