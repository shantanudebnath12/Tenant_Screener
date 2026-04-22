from datetime import datetime

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


class Tenant(db.Model):
    __tablename__ = "tenants"

    id = db.Column(db.Integer, primary_key=True)
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

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False)
    doc_type = db.Column(db.String(50), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    stored_filename = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Date extracted from the document contents (most recent date found).
    parsed_document_date = db.Column(db.Date)
    # User-provided override if parsing is wrong or missing.
    manual_document_date = db.Column(db.Date)
    parse_status = db.Column(db.String(50), default="pending")  # pending|ok|failed|unsupported
    parse_note = db.Column(db.String(500))

    # Name verification — does the tenant's name appear in the document?
    name_match_status = db.Column(db.String(20), default="unknown")  # match|fuzzy|partial|no_match|unknown
    name_match_score = db.Column(db.Float)  # 0.0 – 1.0
    matched_name = db.Column(db.String(300))  # snippet that matched, for display
    name_manually_confirmed = db.Column(db.Boolean, default=False)

    # LLM extraction enrichment (null if the extractor didn't run).
    document_type_predicted = db.Column(db.String(50))  # what the LLM thinks the doc is
    issuer = db.Column(db.String(200))  # bank / employer / credit bureau
    extraction_confidence = db.Column(db.String(20))  # high | medium | low
    extracted_values_json = db.Column(db.Text)  # raw JSON blob from the extractor

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
