"""initial schema — tenants + documents

Revision ID: 20260422_01
Revises:
Create Date: 2026-04-22

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260422_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=50)),
        sa.Column("employer", sa.String(length=200)),
        sa.Column("job_title", sa.String(length=200)),
        sa.Column("monthly_income", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bank_balance", sa.Float(), server_default="0"),
        sa.Column("credit_score", sa.Integer()),
        sa.Column("target_rent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("move_in_date", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True)),
        sa.Column("created_by_email", sa.String(length=320)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.String(length=50), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("storage_path", sa.String(length=700), nullable=False),
        sa.Column("mime_type", sa.String(length=100)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("parsed_document_date", sa.Date()),
        sa.Column("manual_document_date", sa.Date()),
        sa.Column("parse_status", sa.String(length=50), server_default="pending"),
        sa.Column("parse_note", sa.String(length=500)),
        sa.Column("name_match_status", sa.String(length=20), server_default="unknown"),
        sa.Column("name_match_score", sa.Float()),
        sa.Column("matched_name", sa.String(length=300)),
        sa.Column("name_manually_confirmed", sa.Boolean(), server_default=sa.false()),
        sa.Column("document_type_predicted", sa.String(length=50)),
        sa.Column("issuer", sa.String(length=200)),
        sa.Column("extraction_confidence", sa.String(length=20)),
        sa.Column("extracted_values_json", sa.Text()),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("tenants")
