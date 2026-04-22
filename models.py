from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


DOCUMENT_TYPES = [
    ("pay_stub", "Pay Stub"),
    ("bank_statement", "Bank Statement"),
    ("credit_report", "Credit Report"),
    ("id", "Government ID"),
    ("offer_letter", "Employment Offer Letter"),
    ("other", "Other"),
]

DOCUMENT_TYPE_LABELS = dict(DOCUMENT_TYPES)


def _uuid_column(*args, **kwargs):
    # sa.Uuid is Postgres-native UUID + SQLite CHAR(32) fallback — returns
    # uuid.UUID both ways, so the Python code is dialect-agnostic.
    return db.Column(sa.Uuid(as_uuid=True), *args, **kwargs)


class Tenant(db.Model):
    __tablename__ = "tenants"

    id = _uuid_column(primary_key=True, default=uuid.uuid4)
    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50))
    employer = db.Column(db.String(200))
    job_title = db.Column(db.String(200))
    monthly_income = db.Column(db.Float, nullable=False, default=0.0)
    bank_balance = db.Column(db.Float, default=0.0)
    credit_score = db.Column(db.Integer)
    target_rent = db.Column(db.Float, nullable=False, default=0.0)
    move_in_date = db.Column(db.Date)
    notes = db.Column(db.Text)
    # Audit only. References Supabase's auth.users.id. Never used in WHERE
    # clauses — all authenticated Tourbee users share the same dataset.
    created_by_user_id = _uuid_column(nullable=True)
    created_by_email = db.Column(db.String(320))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = db.relationship(
        "Document",
        backref="tenant",
        cascade="all, delete-orphan",
        order_by="Document.uploaded_at.desc()",
    )


class Document(db.Model):
    __tablename__ = "documents"

    id = _uuid_column(primary_key=True, default=uuid.uuid4)
    tenant_id = _uuid_column(db.ForeignKey("tenants.id"), nullable=False)
    doc_type = db.Column(db.String(50), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    # Path in the Supabase Storage bucket: "<tenant_id>/<uuid>_<safe_name>".
    storage_path = db.Column(db.String(700), nullable=False)
    mime_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Date extracted from the document contents.
    parsed_document_date = db.Column(db.Date)
    manual_document_date = db.Column(db.Date)
    parse_status = db.Column(db.String(50), default="pending")
    parse_note = db.Column(db.String(500))

    name_match_status = db.Column(db.String(20), default="unknown")
    name_match_score = db.Column(db.Float)
    matched_name = db.Column(db.String(300))
    name_manually_confirmed = db.Column(db.Boolean, default=False)

    document_type_predicted = db.Column(db.String(50))
    issuer = db.Column(db.String(200))
    extraction_confidence = db.Column(db.String(20))
    extracted_values_json = db.Column(db.Text)

    @property
    def effective_date(self):
        return self.manual_document_date or self.parsed_document_date

    @property
    def type_label(self):
        return DOCUMENT_TYPE_LABELS.get(self.doc_type, self.doc_type)

    @property
    def extracted_values(self):
        if not self.extracted_values_json:
            return {}
        import json as _json

        try:
            return _json.loads(self.extracted_values_json)
        except (TypeError, ValueError):
            return {}
