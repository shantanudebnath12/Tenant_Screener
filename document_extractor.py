"""LLM-based document extractor.

Sends rendered PDF pages to OpenAI's vision model and asks for a structured
extraction (document type, date, name on document, issuer, key financial
figures). Used for format-agnostic reading of bank statements, pay stubs,
credit reports, IDs, and offer letters — avoiding per-format regex.

Falls back to the deterministic parser (``document_parser.parse_document``)
when the API key is missing or the call fails.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import string
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Maximum pages to send to the model per document. Financial statements put
# the summary on page 1; additional pages are mostly transaction rows that
# don't help date / name / balance extraction and just add token cost.
MAX_PAGES = 3
IMAGE_DPI = 150

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

FUZZY_MATCH = 0.85
PARTIAL_MATCH = 0.70

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


# --- Extraction schema ----------------------------------------------------

# Strict JSON schema passed to OpenAI structured outputs. The model is
# required to emit exactly these fields — no extras, no missing keys.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "name": "document_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_type",
            "document_date",
            "date_label",
            "name_on_document",
            "issuer",
            "gross_monthly_income",
            "net_pay_this_period",
            "pay_period",
            "closing_balance",
            "average_balance",
            "credit_score",
            "id_expiration_date",
            "confidence",
            "notes",
        ],
        "properties": {
            "document_type": {
                "type": "string",
                "enum": [
                    "pay_stub",
                    "bank_statement",
                    "credit_report",
                    "id",
                    "offer_letter",
                    "other",
                    "unknown",
                ],
                "description": "What kind of document this is.",
            },
            "document_date": {
                "type": ["string", "null"],
                "description": (
                    "ISO date (YYYY-MM-DD). Priority by doc type: "
                    "pay stub — Pay Date / Check Date > Issue Date > Pay Period "
                    "End (never Pay Period Begin); "
                    "bank statement — Statement Date / Closing Date / Closing "
                    "Balance on / Period End (never a transaction date); "
                    "credit report — Date Issued / Report Date / Date Pulled; "
                    "ID — Issue Date (NOT expiration); "
                    "offer letter — letter date, fall back to Start Date. "
                    "Null if not found."
                ),
            },
            "date_label": {
                "type": ["string", "null"],
                "description": (
                    "The label or phrase next to the date you chose (e.g. "
                    "'Closing Balance on', 'Pay date', 'Date issued')."
                ),
            },
            "name_on_document": {
                "type": ["string", "null"],
                "description": "Primary person named on the document. Null if not found.",
            },
            "issuer": {
                "type": ["string", "null"],
                "description": "Bank / employer / credit bureau that issued the document.",
            },
            "gross_monthly_income": {
                "type": ["number", "null"],
                "description": (
                    "For pay stubs only: estimated gross MONTHLY income. If "
                    "the stub shows bi-weekly or weekly, convert to monthly "
                    "(bi-weekly × 2.1667, weekly × 4.333). Null otherwise."
                ),
            },
            "net_pay_this_period": {
                "type": ["number", "null"],
                "description": "Pay stubs only: net pay for this period, unconverted.",
            },
            "pay_period": {
                "type": ["string", "null"],
                "enum": ["weekly", "bi-weekly", "semi-monthly", "monthly", None],
                "description": "Pay stubs only: frequency of pay.",
            },
            "closing_balance": {
                "type": ["number", "null"],
                "description": "Bank statement only: closing / ending balance.",
            },
            "average_balance": {
                "type": ["number", "null"],
                "description": "Bank statement only: average daily balance if shown.",
            },
            "credit_score": {
                "type": ["integer", "null"],
                "description": "Credit report only: primary credit score (300-900 range).",
            },
            "id_expiration_date": {
                "type": ["string", "null"],
                "description": "ID only: expiration date in ISO format.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "Your confidence in this extraction. 'low' when the image "
                    "is hard to read, dates are ambiguous, or key fields are "
                    "missing."
                ),
            },
            "notes": {
                "type": "string",
                "description": "One short sentence about the extraction. Empty string if nothing notable.",
            },
        },
    },
}


# --- Result object --------------------------------------------------------


@dataclass
class ExtractionResult:
    # Core fields used by the eligibility engine and UI.
    document_date: date | None
    parse_status: str  # ok | failed | unsupported
    parse_note: str
    name_match_status: str  # match | fuzzy | partial | no_match | unknown
    name_match_score: float | None
    matched_name: str | None

    # LLM-enriched fields.
    document_type_predicted: str | None = None
    issuer: str | None = None
    extraction_confidence: str | None = None  # high | medium | low
    extracted_values_json: str | None = None  # JSON blob of the full LLM response

    @property
    def extracted_values(self) -> dict[str, Any]:
        if not self.extracted_values_json:
            return {}
        try:
            return json.loads(self.extracted_values_json)
        except (TypeError, ValueError):
            return {}


# --- Public API -----------------------------------------------------------


def extract_document(
    file_path: str | Path, mime_type: str | None, tenant_name: str
) -> ExtractionResult:
    """Extract structured data from a document via OpenAI, with deterministic fallback."""
    path = Path(file_path)

    if not _is_supported(path, mime_type):
        return _unsupported_result()

    if not os.environ.get("OPENAI_API_KEY"):
        log.info("OPENAI_API_KEY not set — falling back to deterministic parser.")
        return _fallback(path, mime_type, tenant_name, reason="No API key configured.")

    try:
        images = _render_document_to_images(path, mime_type, max_pages=MAX_PAGES)
    except Exception as exc:
        log.exception("Failed to render document: %s", exc)
        return _fallback(path, mime_type, tenant_name, reason=f"Could not render document: {exc}")

    if not images:
        return _fallback(path, mime_type, tenant_name, reason="No renderable content.")

    try:
        raw = _call_openai(images, tenant_name)
    except Exception as exc:
        log.exception("OpenAI call failed: %s", exc)
        return _fallback(path, mime_type, tenant_name, reason=f"LLM extraction failed: {exc}")

    return _result_from_llm(raw, tenant_name)


# --- Internals ------------------------------------------------------------


def _is_supported(path: Path, mime_type: str | None) -> bool:
    ext = path.suffix.lower()
    if ext == ".pdf" or mime_type == "application/pdf":
        return True
    if ext in IMAGE_EXTENSIONS or (mime_type and mime_type.startswith("image/")):
        return True
    return False


def _is_image(path: Path, mime_type: str | None) -> bool:
    ext = path.suffix.lower()
    return ext in IMAGE_EXTENSIONS or (mime_type or "").startswith("image/")


def _unsupported_result() -> ExtractionResult:
    return ExtractionResult(
        document_date=None,
        parse_status="unsupported",
        parse_note="Only PDFs are auto-extracted. Set the date and confirm the name manually.",
        name_match_status="unknown",
        name_match_score=None,
        matched_name=None,
    )


def _fallback(path: Path, mime_type: str | None, tenant_name: str, reason: str) -> ExtractionResult:
    # Images can't be parsed deterministically (no OCR) — mark unsupported.
    if _is_image(path, mime_type):
        return ExtractionResult(
            document_date=None,
            parse_status="unsupported",
            parse_note=f"{reason} Image files can only be auto-read with the LLM extractor.",
            name_match_status="unknown",
            name_match_score=None,
            matched_name=None,
        )

    # PDF: use the deterministic regex parser; attach the fallback reason.
    from document_parser import parse_document

    base = parse_document(path, mime_type, tenant_name)
    note = f"{reason} Using deterministic parser. {base.parse_note}".strip()
    return ExtractionResult(
        document_date=base.document_date,
        parse_status=base.parse_status,
        parse_note=note,
        name_match_status=base.name_match_status,
        name_match_score=base.name_match_score,
        matched_name=base.matched_name,
        document_type_predicted=None,
        issuer=None,
        extraction_confidence=None,
        extracted_values_json=None,
    )


def _render_document_to_images(
    path: Path, mime_type: str | None, max_pages: int
) -> list[tuple[bytes, str]]:
    """Return a list of (bytes, mime_type) tuples ready for the vision API.

    PDFs are rasterized one page per tuple; image files are passed through.
    """
    if _is_image(path, mime_type):
        ext = path.suffix.lower().lstrip(".")
        resolved = mime_type or {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/png")
        return [(path.read_bytes(), resolved)]

    import fitz  # PyMuPDF

    images: list[tuple[bytes, str]] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            zoom = IMAGE_DPI / 72  # 72 is the PDF default DPI
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            images.append((pix.tobytes("png"), "image/png"))
    return images


def _call_openai(images: list[tuple[bytes, str]], tenant_name: str) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    today = date.today().isoformat()

    system_prompt = (
        "You extract structured data from tenant-screening documents (pay stubs, "
        "bank statements, credit reports, government IDs, offer letters) for a "
        "rental-eligibility app. Accuracy matters — this affects a housing decision.\n"
        "\n"
        f"Today's date is {today}. Use ISO-8601 (YYYY-MM-DD) for every date.\n"
        "\n"
        "=== Which date goes in `document_date` ===\n"
        "Documents have several dates. Pick the ONE that best represents when the "
        "document reflects the applicant's state TODAY. Rules by document type, in "
        "priority order:\n"
        "\n"
        "• PAY STUB: 'Pay Date' or 'Check Date' (preferred) > 'Issue Date' > "
        "'Pay Period End'. NEVER use 'Pay Period Begin' / 'Period Start'.\n"
        "• BANK STATEMENT: 'Statement Date' / 'Closing Date' / 'Closing Balance on' / "
        "'Statement Period End'. NEVER use a transaction date from the activity rows.\n"
        "• CREDIT REPORT: 'Date Issued' / 'Report Date' / 'Date Pulled' / "
        "'As of'. Don't use account-open dates.\n"
        "• GOVERNMENT ID: 'Issue Date' (NOT expiration). Expiration goes in "
        "`id_expiration_date`.\n"
        "• OFFER LETTER: the letter's date, or 'Start Date' if no letter date shown.\n"
        "\n"
        "Also record the human-readable label you saw next to that date in "
        "`date_label` (e.g. 'Pay Date', 'Closing Balance on').\n"
        "\n"
        "=== Amounts ===\n"
        "• `gross_monthly_income` (pay stubs only): always normalise to monthly. "
        "Weekly × 4.333, bi-weekly × 2.1667, semi-monthly × 2, monthly × 1. Use "
        "GROSS pay (before deductions), not net.\n"
        "• `net_pay_this_period` (pay stubs): net pay for the CURRENT period, "
        "unconverted.\n"
        "• `closing_balance` (bank statements): closing / ending balance. Include "
        "cents.\n"
        "• `credit_score` (credit reports): the headline score (FICO / Equifax / "
        "TransUnion). Integer, 300–900 range.\n"
        "\n"
        "=== Name ===\n"
        "`name_on_document` = primary account holder / employee / licensee, exactly "
        "as printed. Don't normalise case.\n"
        "\n"
        "=== Confidence ===\n"
        "• 'high' — image is clear, required fields unambiguous.\n"
        "• 'medium' — image OK but some fields ambiguous, or multiple candidates.\n"
        "• 'low' — image hard to read, the date you picked is older than ~180 "
        "days (unusual for screening), or the document_type doesn't match what "
        "you expected. Explain briefly in `notes`.\n"
        "\n"
        "If a field isn't clearly present, return null. Do not guess."
    )

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Applicant name (for context only; don't validate here): {tenant_name}\n"
                f"Today: {today}\n\n"
                "Extract the fields defined by the schema, following the date-priority "
                "rules exactly. If you see both 'Pay Period End' and 'Pay Date' on a "
                "pay stub, you MUST use 'Pay Date'."
            ),
        }
    ]
    for img_bytes, img_mime in images:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{img_mime};base64,{b64}"},
            }
        )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_schema", "json_schema": EXTRACTION_SCHEMA},
        temperature=0,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned empty content.")
    return json.loads(content)


def _result_from_llm(raw: dict[str, Any], tenant_name: str) -> ExtractionResult:
    # Parse the document date.
    doc_date: date | None = None
    raw_date = raw.get("document_date")
    if raw_date:
        try:
            doc_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            doc_date = None

    if doc_date is None:
        parse_status = "failed"
        parse_note = "LLM did not return a date."
    else:
        label = raw.get("date_label") or ""
        parse_note = f"Extracted date: {doc_date.isoformat()}"
        if label:
            parse_note += f" (labelled '{label}')"
        parse_status = "ok"

    confidence = raw.get("confidence")
    notes = raw.get("notes") or ""
    if notes:
        parse_note = f"{parse_note} · {notes}"
    if confidence:
        parse_note = f"{parse_note} · confidence: {confidence}"

    # Name match via deterministic string comparison — keep the LLM out of
    # the "is this the same person" decision.
    name_on_doc = raw.get("name_on_document") or ""
    status, score, snippet = _match_name(name_on_doc, tenant_name)

    return ExtractionResult(
        document_date=doc_date,
        parse_status=parse_status,
        parse_note=parse_note,
        name_match_status=status,
        name_match_score=score,
        matched_name=snippet or (name_on_doc or None),
        document_type_predicted=raw.get("document_type"),
        issuer=raw.get("issuer"),
        extraction_confidence=confidence,
        extracted_values_json=json.dumps(raw),
    )


# --- Name matching (deterministic) ----------------------------------------


def _normalize(value: str) -> str:
    return " ".join(value.lower().translate(_PUNCT_TABLE).split())


def _match_name(name_on_doc: str, tenant_name: str) -> tuple[str, float | None, str | None]:
    if not tenant_name.strip():
        return "unknown", None, None
    if not name_on_doc.strip():
        return "no_match", 0.0, None

    norm_doc = _normalize(name_on_doc)
    norm_tenant = _normalize(tenant_name)
    if not norm_doc or not norm_tenant:
        return "no_match", 0.0, None

    if norm_tenant == norm_doc or norm_tenant in norm_doc or norm_doc in norm_tenant:
        return "match", 1.0, name_on_doc

    # Tokens-based variants (handle "First Last" vs "Last, First" etc.)
    t_tokens = norm_tenant.split()
    variants = {norm_tenant}
    if len(t_tokens) >= 2:
        variants.add(f"{t_tokens[0]} {t_tokens[-1]}")
        variants.add(f"{t_tokens[-1]} {t_tokens[0]}")

    best_score = 0.0
    for v in variants:
        score = SequenceMatcher(None, v, norm_doc).ratio()
        if score > best_score:
            best_score = score

    if best_score >= FUZZY_MATCH:
        return "fuzzy", round(best_score, 3), name_on_doc
    if best_score >= PARTIAL_MATCH:
        return "partial", round(best_score, 3), name_on_doc
    return "no_match", round(best_score, 3), name_on_doc
