"""Extract document dates and verify the tenant's name appears in the document."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from dateutil import parser as dateparser


# Allow document dates from up to 5 years in the past, 30 days in the future
# (to tolerate minor clock skew or post-dated statements).
MIN_PLAUSIBLE_DATE = date.today() - timedelta(days=365 * 5)
MAX_PLAUSIBLE_DATE = date.today() + timedelta(days=30)


# Regex patterns for common date formats. dateutil handles the actual parsing
# — the regex just narrows down candidate substrings.
DATE_PATTERNS = [
    # 12/31/2024, 1-3-25
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b"),
    # 2024-12-31
    re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2})\b"),
    # December 31, 2024 / Dec 31 2024
    re.compile(
        r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{2,4})\b",
        re.IGNORECASE,
    ),
    # 31 December 2024
    re.compile(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{2,4})\b",
        re.IGNORECASE,
    ),
]


FUZZY_MATCH = 0.85
PARTIAL_MATCH = 0.70


@dataclass
class ParseResult:
    document_date: date | None
    parse_status: str  # ok | failed | unsupported
    parse_note: str
    name_match_status: str  # match | fuzzy | partial | no_match | unknown
    name_match_score: float | None
    matched_name: str | None


def parse_document(file_path: str | Path, mime_type: str | None, tenant_name: str) -> ParseResult:
    """Extract a document date and run a name match against the tenant's name.

    Only PDFs are parsed. Anything else returns ``unsupported`` and ``unknown`` —
    the user is then prompted in the UI to fill the date and confirm the name.
    """
    path = Path(file_path)
    is_pdf = (mime_type == "application/pdf") or path.suffix.lower() == ".pdf"

    if not is_pdf:
        return ParseResult(
            document_date=None,
            parse_status="unsupported",
            parse_note="Automatic parsing only supports PDFs. Set the date and confirm the name manually.",
            name_match_status="unknown",
            name_match_score=None,
            matched_name=None,
        )

    text = _extract_pdf_text(path)
    if text is None:
        return ParseResult(
            document_date=None,
            parse_status="failed",
            parse_note="Could not read the PDF (it may be image-only or corrupted).",
            name_match_status="unknown",
            name_match_score=None,
            matched_name=None,
        )

    document_date = _find_most_recent_date(text)
    if document_date is None:
        date_status = "failed"
        date_note = "No recognizable date found in the document."
    else:
        date_status = "ok"
        date_note = f"Detected document date: {document_date.isoformat()}"

    name_status, name_score, matched = _match_name(text, tenant_name)

    return ParseResult(
        document_date=document_date,
        parse_status=date_status,
        parse_note=date_note,
        name_match_status=name_status,
        name_match_score=name_score,
        matched_name=matched,
    )


def _extract_pdf_text(path: Path) -> str | None:
    try:
        import pdfplumber
    except ImportError:
        return None

    try:
        chunks: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text:
                    chunks.append(page_text)
        return "\n".join(chunks) if chunks else ""
    except Exception:
        return None


def _candidate_date_strings(text: str) -> Iterable[str]:
    seen: set[str] = set()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1)
            if value not in seen:
                seen.add(value)
                yield value


def _find_most_recent_date(text: str) -> date | None:
    plausible: list[date] = []
    for candidate in _candidate_date_strings(text):
        try:
            parsed = dateparser.parse(candidate, dayfirst=False, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            continue
        if not parsed:
            continue
        d = parsed.date() if isinstance(parsed, datetime) else parsed
        if MIN_PLAUSIBLE_DATE <= d <= MAX_PLAUSIBLE_DATE:
            plausible.append(d)
    return max(plausible) if plausible else None


# --- Name matching --------------------------------------------------------


_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def _normalize(value: str) -> str:
    return " ".join(value.lower().translate(_PUNCT_TABLE).split())


def _name_variants(full_name: str) -> list[str]:
    norm = _normalize(full_name)
    if not norm:
        return []
    tokens = norm.split()
    variants = {norm}
    if len(tokens) >= 2:
        variants.add(f"{tokens[0]} {tokens[-1]}")
        variants.add(f"{tokens[-1]} {tokens[0]}")
    return [v for v in variants if v]


def _match_name(text: str, tenant_name: str) -> tuple[str, float | None, str | None]:
    if not tenant_name.strip():
        return "unknown", None, None

    normalized_text = _normalize(text)
    if not normalized_text:
        return "no_match", 0.0, None

    text_tokens = normalized_text.split()
    best_status = "no_match"
    best_score = 0.0
    best_snippet: str | None = None

    for variant in _name_variants(tenant_name):
        if variant in normalized_text:
            return "match", 1.0, variant

        v_tokens = variant.split()
        window_size = len(v_tokens)
        if window_size == 0 or window_size > len(text_tokens):
            continue

        for i in range(len(text_tokens) - window_size + 1):
            window = " ".join(text_tokens[i : i + window_size])
            score = SequenceMatcher(None, variant, window).ratio()
            if score > best_score:
                best_score = score
                best_snippet = window
                if score >= FUZZY_MATCH:
                    best_status = "fuzzy"
                elif score >= PARTIAL_MATCH:
                    best_status = "partial"
                else:
                    best_status = "no_match"

    return best_status, round(best_score, 3), best_snippet
