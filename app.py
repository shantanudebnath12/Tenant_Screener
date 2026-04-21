"""Tenant Screener — Flask application."""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

import eligibility
from document_parser import parse_document
from models import DOCUMENT_TYPES, Document, Tenant, db


ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def create_app(database_uri: str | None = None, upload_dir: str | None = None) -> Flask:
    app = Flask(__name__)
    base_dir = Path(__file__).parent.resolve()
    instance_dir = base_dir / "instance"
    instance_dir.mkdir(exist_ok=True)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri or f"sqlite:///{instance_dir / 'tenants.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    upload_path = Path(upload_dir) if upload_dir else base_dir / "uploads"
    upload_path.mkdir(exist_ok=True)
    app.config["UPLOAD_DIR"] = str(upload_path)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    _register_routes(app)
    _register_template_helpers(app)
    return app


# --- Helpers --------------------------------------------------------------


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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


def _delete_file_safely(stored_filename: str, upload_dir: str) -> None:
    full = Path(upload_dir) / stored_filename
    try:
        if full.exists():
            full.unlink()
    except OSError:
        pass


def _save_and_parse_upload(tenant: Tenant, file, doc_type: str, upload_dir: str) -> Document:
    original = secure_filename(file.filename)
    stored = f"{tenant.id}/{uuid.uuid4().hex}_{original}"
    full_path = Path(upload_dir) / stored
    full_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(full_path)
    size = full_path.stat().st_size
    result = parse_document(full_path, file.mimetype, tenant.full_name)
    return Document(
        tenant_id=tenant.id,
        doc_type=doc_type,
        original_filename=original,
        stored_filename=stored,
        mime_type=file.mimetype,
        size_bytes=size,
        parsed_document_date=result.document_date,
        parse_status=result.parse_status,
        parse_note=result.parse_note,
        name_match_status=result.name_match_status,
        name_match_score=result.name_match_score,
        matched_name=result.matched_name,
    )


# --- Routes ---------------------------------------------------------------


def _register_routes(app: Flask) -> None:

    @app.get("/")
    def index():
        tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
        rows = [(t, eligibility.evaluate(t)) for t in tenants]
        return render_template("index.html", rows=rows)

    @app.route("/tenants/new", methods=["GET", "POST"])
    def new_tenant():
        if request.method == "POST":
            tenant = _tenant_from_form(request.form)
            errors = _validate_tenant(tenant)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "tenant_form.html", tenant=tenant, mode="new", doc_types=DOCUMENT_TYPES
                )
            db.session.add(tenant)
            db.session.commit()

            doc_count = 0
            skipped: list[str] = []
            for value, label in DOCUMENT_TYPES:
                file = request.files.get(f"file_{value}")
                if not file or not file.filename:
                    continue
                if not _allowed(file.filename):
                    skipped.append(label)
                    continue
                doc = _save_and_parse_upload(tenant, file, value, app.config["UPLOAD_DIR"])
                db.session.add(doc)
                doc_count += 1
            if doc_count:
                db.session.commit()

            msg = "Applicant created."
            if doc_count:
                msg += f" {doc_count} document{'s' if doc_count != 1 else ''} uploaded."
            flash(msg, "success")
            if skipped:
                flash(f"Skipped unsupported file type for: {', '.join(skipped)}.", "error")
            return redirect(url_for("tenant_detail", tenant_id=tenant.id))
        return render_template(
            "tenant_form.html", tenant=Tenant(), mode="new", doc_types=DOCUMENT_TYPES
        )

    @app.route("/tenants/<int:tenant_id>/edit", methods=["GET", "POST"])
    def edit_tenant(tenant_id: int):
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

    @app.get("/tenants/<int:tenant_id>")
    def tenant_detail(tenant_id: int):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        verdict = eligibility.evaluate(tenant)
        return render_template(
            "tenant_detail.html",
            tenant=tenant,
            verdict=verdict,
            doc_types=DOCUMENT_TYPES,
        )

    @app.post("/tenants/<int:tenant_id>/delete")
    def delete_tenant(tenant_id: int):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        for doc in list(tenant.documents):
            _delete_file_safely(doc.stored_filename, app.config["UPLOAD_DIR"])
        db.session.delete(tenant)
        db.session.commit()
        flash("Applicant deleted.", "success")
        return redirect(url_for("index"))

    @app.post("/tenants/<int:tenant_id>/documents")
    def upload_document(tenant_id: int):
        tenant = db.session.get(Tenant, tenant_id) or abort(404)
        file = request.files.get("file")
        doc_type = request.form.get("doc_type", "other")

        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(url_for("tenant_detail", tenant_id=tenant_id))
        if not _allowed(file.filename):
            flash("Unsupported file type.", "error")
            return redirect(url_for("tenant_detail", tenant_id=tenant_id))

        doc = _save_and_parse_upload(tenant, file, doc_type, app.config["UPLOAD_DIR"])
        db.session.add(doc)
        db.session.commit()
        flash("Document uploaded.", "success")
        return redirect(url_for("tenant_detail", tenant_id=tenant.id))

    @app.post("/documents/<int:doc_id>/date")
    def update_document_date(doc_id: int):
        doc = db.session.get(Document, doc_id) or abort(404)
        doc.manual_document_date = _parse_date_field(request.form.get("manual_document_date"))
        db.session.commit()
        flash("Document date updated.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<int:doc_id>/confirm-name")
    def confirm_document_name(doc_id: int):
        doc = db.session.get(Document, doc_id) or abort(404)
        doc.name_manually_confirmed = bool(request.form.get("confirmed"))
        db.session.commit()
        flash("Name confirmation updated.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<int:doc_id>/reparse")
    def reparse_document(doc_id: int):
        doc = db.session.get(Document, doc_id) or abort(404)
        full_path = Path(app.config["UPLOAD_DIR"]) / doc.stored_filename
        if not full_path.exists():
            flash("File missing on disk; cannot re-parse.", "error")
            return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))
        result = parse_document(full_path, doc.mime_type, doc.tenant.full_name)
        doc.parsed_document_date = result.document_date
        doc.parse_status = result.parse_status
        doc.parse_note = result.parse_note
        doc.name_match_status = result.name_match_status
        doc.name_match_score = result.name_match_score
        doc.matched_name = result.matched_name
        db.session.commit()
        flash("Document re-parsed.", "success")
        return redirect(url_for("tenant_detail", tenant_id=doc.tenant_id))

    @app.post("/documents/<int:doc_id>/delete")
    def delete_document(doc_id: int):
        doc = db.session.get(Document, doc_id) or abort(404)
        tenant_id = doc.tenant_id
        _delete_file_safely(doc.stored_filename, app.config["UPLOAD_DIR"])
        db.session.delete(doc)
        db.session.commit()
        flash("Document deleted.", "success")
        return redirect(url_for("tenant_detail", tenant_id=tenant_id))

    @app.get("/documents/<int:doc_id>/download")
    def download_document(doc_id: int):
        doc = db.session.get(Document, doc_id) or abort(404)
        # send_from_directory protects against path traversal.
        directory, filename = os.path.split(doc.stored_filename)
        return send_from_directory(
            os.path.join(app.config["UPLOAD_DIR"], directory),
            filename,
            as_attachment=True,
            download_name=doc.original_filename,
        )


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
    def inject_today():
        return {"today": date.today()}


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
