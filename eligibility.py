"""Tenant eligibility engine.

Each rule produces a :class:`Reason` at level ``pass``, ``warn``, or ``fail``.
The overall verdict is ``Approved`` when all reasons pass, ``Conditional`` when
there is at least one warn (and no fails), and ``Denied`` if any rule fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable


# --- Tunable thresholds ---------------------------------------------------

INCOME_TO_RENT_RATIO = 3.0
RESERVE_RATIO_PASS = 2.0
RESERVE_RATIO_FAIL = 1.0

CREDIT_PASS = 650
CREDIT_FAIL = 600

REQUIRED_DOC_TYPES = ["pay_stub", "bank_statement", "credit_report", "id"]
DOC_TYPE_LABELS = {
    "pay_stub": "Pay stub",
    "bank_statement": "Bank statement",
    "credit_report": "Credit report",
    "id": "Government ID",
}

# Days-old thresholds per document type. Anything older than the warn threshold
# but within fail is a warning; older than fail is a failure.
DOC_FRESHNESS = {
    "pay_stub": (60, 90),
    "bank_statement": (60, 90),
    "credit_report": (90, 120),
    "id": (None, None),  # only expiration matters; not enforced here
}


# --- Data structures ------------------------------------------------------


@dataclass
class Reason:
    level: str  # pass | warn | fail
    message: str


@dataclass
class Verdict:
    label: str  # Approved | Conditional | Denied
    reasons: list[Reason] = field(default_factory=list)

    @property
    def fails(self) -> list[Reason]:
        return [r for r in self.reasons if r.level == "fail"]

    @property
    def warns(self) -> list[Reason]:
        return [r for r in self.reasons if r.level == "warn"]

    @property
    def passes(self) -> list[Reason]:
        return [r for r in self.reasons if r.level == "pass"]


# --- Engine ---------------------------------------------------------------


def evaluate(tenant) -> Verdict:
    reasons: list[Reason] = []

    reasons.append(_check_income(tenant))
    reasons.append(_check_credit(tenant))
    reasons.append(_check_reserve(tenant))
    reasons.extend(_check_required_docs(tenant))
    reasons.extend(_check_document_freshness(tenant))
    reasons.extend(_check_name_matches(tenant))

    if any(r.level == "fail" for r in reasons):
        label = "Denied"
    elif any(r.level == "warn" for r in reasons):
        label = "Conditional"
    else:
        label = "Approved"

    return Verdict(label=label, reasons=reasons)


# --- Individual rules -----------------------------------------------------


def _check_income(tenant) -> Reason:
    rent = float(tenant.target_rent or 0)
    income = float(tenant.monthly_income or 0)
    if rent <= 0:
        return Reason("warn", "Rent is not set — cannot evaluate income coverage.")
    ratio = income / rent
    if ratio >= INCOME_TO_RENT_RATIO:
        return Reason("pass", f"Income is {ratio:.1f}× rent (target ≥ {INCOME_TO_RENT_RATIO:.0f}×).")
    return Reason(
        "fail",
        f"Income is only {ratio:.1f}× rent — below the {INCOME_TO_RENT_RATIO:.0f}× threshold.",
    )


def _check_credit(tenant) -> Reason:
    score = tenant.credit_score
    if score is None:
        return Reason("warn", "Credit score not provided.")
    if score >= CREDIT_PASS:
        return Reason("pass", f"Credit score {score} meets the {CREDIT_PASS} threshold.")
    if score >= CREDIT_FAIL:
        return Reason(
            "warn",
            f"Credit score {score} is between {CREDIT_FAIL}–{CREDIT_PASS - 1} — borderline.",
        )
    return Reason("fail", f"Credit score {score} is below the {CREDIT_FAIL} minimum.")


def _check_reserve(tenant) -> Reason:
    rent = float(tenant.target_rent or 0)
    balance = float(tenant.bank_balance or 0)
    if rent <= 0:
        return Reason("warn", "Rent is not set — cannot evaluate cash reserves.")
    months = balance / rent
    if months >= RESERVE_RATIO_PASS:
        return Reason("pass", f"Bank reserve covers {months:.1f} months of rent.")
    if months >= RESERVE_RATIO_FAIL:
        return Reason(
            "warn",
            f"Bank reserve covers only {months:.1f} months — below the {RESERVE_RATIO_PASS:.0f}-month target.",
        )
    return Reason(
        "fail",
        f"Bank reserve covers only {months:.1f} months — under the {RESERVE_RATIO_FAIL:.0f}-month minimum.",
    )


def _check_required_docs(tenant) -> Iterable[Reason]:
    present_types = {d.doc_type for d in tenant.documents}
    out: list[Reason] = []
    for doc_type in REQUIRED_DOC_TYPES:
        label = DOC_TYPE_LABELS[doc_type]
        if doc_type in present_types:
            out.append(Reason("pass", f"{label} uploaded."))
        else:
            out.append(Reason("fail", f"{label} is missing."))
    return out


def _check_document_freshness(tenant) -> Iterable[Reason]:
    today = date.today()
    out: list[Reason] = []
    for doc_type, (warn_days, fail_days) in DOC_FRESHNESS.items():
        if warn_days is None:
            continue
        label = DOC_TYPE_LABELS.get(doc_type, doc_type)
        docs = [d for d in tenant.documents if d.doc_type == doc_type]
        if not docs:
            # Missing-doc reason is already produced by _check_required_docs.
            continue
        dated = [d for d in docs if d.effective_date]
        if not dated:
            out.append(
                Reason(
                    "warn",
                    f"{label}: no document date set — confirm the document is recent.",
                )
            )
            continue
        latest = max(d.effective_date for d in dated)
        age = (today - latest).days
        if age <= warn_days:
            out.append(Reason("pass", f"{label} is {age} days old (within {warn_days})."))
        elif age <= fail_days:
            out.append(
                Reason("warn", f"{label} is {age} days old — older than {warn_days} days.")
            )
        else:
            out.append(
                Reason("fail", f"{label} is {age} days old — older than {fail_days} days.")
            )
    return out


def _check_name_matches(tenant) -> Iterable[Reason]:
    out: list[Reason] = []
    for doc_type in REQUIRED_DOC_TYPES:
        docs = [d for d in tenant.documents if d.doc_type == doc_type]
        if not docs:
            continue
        label = DOC_TYPE_LABELS[doc_type]
        # Use the most recent doc of this type for name verification.
        doc = max(docs, key=lambda d: d.uploaded_at)
        status = doc.name_match_status or "unknown"
        if doc.name_manually_confirmed:
            out.append(Reason("pass", f"{label}: name confirmed manually."))
            continue
        if status in ("match", "fuzzy"):
            score_text = f" (score {doc.name_match_score:.2f})" if doc.name_match_score else ""
            out.append(Reason("pass", f"{label}: tenant name found in document{score_text}."))
        elif status == "partial":
            out.append(
                Reason(
                    "warn",
                    f"{label}: tenant name only partially matches the document — please review.",
                )
            )
        elif status == "no_match":
            out.append(
                Reason(
                    "fail",
                    f"{label}: tenant name does not appear in the document.",
                )
            )
        else:  # unknown
            out.append(
                Reason(
                    "warn",
                    f"{label}: name match could not be verified automatically — confirm manually.",
                )
            )
    return out
