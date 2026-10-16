"""Per-field confidence. The model's own certainty is never used.

A field starts from a base score and is adjusted by independent checks:

  evidence    the quoted evidence must exist in the document text (rapidfuzz
              partial match >= 90) and the value must appear in, or follow
              from, that quote. A quote that cannot be found is the strongest
              sign of a made-up value, so it costs the most.
  agreement   the rule-based extractor found the same value: boost.
              It found a different one: penalty (and a review reason).
  validation  the field took part in a failed business rule: penalty.
  source      the value was read from a scanned page image: small penalty,
              and the quote cannot be verified because there is no text layer.
  ambiguity   a numeric date such as 03/04/26 could be read two ways: penalty.

Null values are scored differently: confidence means "how sure we are the
field is really absent". It is high when nothing in the document looks like the
field and low when the rule extractor found a candidate the LLM missed.

Scores are clamped to [0, 1]. The constants below are the whole method.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rapidfuzz import fuzz

from co_extract.loader import LoadedDocument
from co_extract.normalize import (
    DATE_RE,
    alnum,
    find_amounts,
    match_form,
    normalize_currency,
    parse_date,
)
from co_extract.rules import RuleCandidate, RuleHits, ids_match
from co_extract.schema import SCALAR_FIELDS, ChangeOrder, Field, LineItem, Validation

# --- scoring constants ----------------------------------------------------
BASE_NON_NULL = 0.80
EVIDENCE_VERIFIED = 0.10
EVIDENCE_VERIFIED_UNCHECKED = 0.05  # quote found, but the value (enum, bool, summary) cannot be checked against it
EVIDENCE_MISSING = -0.30  # a value with no quote at all
EVIDENCE_NOT_FOUND = -0.45  # quote does not occur in the document
VALUE_NOT_IN_EVIDENCE = -0.25
RULE_AGREE = 0.08
RULE_DISAGREE = -0.25
VALIDATION_ERROR = -0.30
VALIDATION_WARNING = -0.05
SCANNED_SOURCE = -0.05
AMBIGUOUS_DATE = -0.15

NULL_CHECKED_BY_RULE = 0.85  # null, a rule looked and found nothing
NULL_NOT_CHECKABLE = 0.75  # null, no rule covers this field
NULL_RULE_FOUND_CANDIDATE = 0.25  # null, but a rule found something
NULL_SCANNED_DOC = -0.15  # rules cannot read scanned pages
UNREADABLE_VALUE = 0.20  # the model returned something we could not parse

EVIDENCE_MATCH_SCORE = 90
NAME_MATCH_SCORE = 85

_ID_FIELDS = {"change_order_number", "project_number"}
_DATE_FIELDS = {"date_issued", "date_approved"}
_MONEY_FIELDS = {"subtotal", "tax", "total_amount"}
_NAME_FIELDS = {"project_name", "contractor", "client", "requested_by", "approved_by"}
# Fields the rule extractor can cross-check; a null here is checked, not assumed.
RULE_FIELDS = frozenset(
    {"change_order_number", "project_number", "date_issued", "date_approved", "subtotal", "tax",
     "total_amount", "schedule_impact_days", "currency"}
)  # fmt: skip


class EvidenceStatus(str, Enum):
    verified = "verified"
    missing = "missing"  # no quote given
    not_found = "not_found"  # quote not in the document
    unverifiable = "unverifiable"  # quote is on a scanned page: no text to compare


@dataclass
class ScoringContext:
    doc: LoadedDocument
    rules: RuleHits
    validation: Validation
    ambiguous_fields: set[str] = field(default_factory=set)
    unreadable_fields: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # human-readable disagreements


def _clamp(x: float) -> float:
    return round(min(1.0, max(0.0, x)), 3)


# --- evidence ---------------------------------------------------------------


_NUMBER_TOKEN = re.compile(r"\d(?:[\d,./-]*\d)?")


def _quote_in_text(quote: str, text: str) -> bool:
    """Fuzzy match (>= 90) that also requires every number in the quote to appear
    verbatim. A 90% match would otherwise accept CO-099 for CO-014."""
    needle, haystack = match_form(quote), match_form(text)
    if not needle:
        return False
    if needle in haystack:
        return True
    if not all(token in haystack for token in _NUMBER_TOKEN.findall(needle)):
        return False
    return fuzz.partial_ratio(needle, haystack) >= EVIDENCE_MATCH_SCORE


def check_evidence(evidence: str | None, page: int | None, doc: LoadedDocument) -> tuple[EvidenceStatus, int | None]:
    """Find the quote in the document. Returns (status, page where it was found)."""
    if not evidence or not evidence.strip():
        return EvidenceStatus.missing, page
    claimed = doc.page(page) if page is not None else None
    if claimed is not None and claimed.scanned:
        return EvidenceStatus.unverifiable, page
    if claimed is not None and _quote_in_text(evidence, claimed.text):
        return EvidenceStatus.verified, page
    for other in doc.pages:
        if other is not claimed and not other.scanned and _quote_in_text(evidence, other.text):
            return EvidenceStatus.verified, other.number
    if claimed is None and doc.has_scanned_pages:
        return EvidenceStatus.unverifiable, page
    return EvidenceStatus.not_found, page


def value_supported(name: str, value: Any, evidence: str) -> bool | None:
    """Does the value appear in, or follow from, the quote? None = cannot be checked."""
    if name in _ID_FIELDS:
        return alnum(str(value)) in alnum(evidence)
    if name in _DATE_FIELDS:
        return any(
            parse_date(m.group(0), day_first).value == value
            for m in DATE_RE.finditer(evidence)
            for day_first in (False, True)
        )
    if name in _MONEY_FIELDS:
        found = find_amounts(evidence)
        return value in found or -value in found
    if name == "schedule_impact_days":
        numbers = {int(n) for n in re.findall(r"\d+", evidence)}
        return abs(value) in numbers or (value == 0 and bool(re.search(r"\b(no|none|nil|zero)\b", evidence, re.I)))
    if name == "currency":
        return normalize_currency(evidence) == value or (value == "USD" and "$" in evidence)
    if name in _NAME_FIELDS:
        return fuzz.partial_ratio(match_form(str(value)), match_form(evidence)) >= NAME_MATCH_SCORE
    if name == "references":
        return all(alnum(ref) in alnum(evidence) for ref in value)
    return None  # description, enums, booleans: follows from the quote, not checkable


# --- agreement with the rule extractor ---------------------------------------


def _rule_agrees(name: str, value: Any, candidate: RuleCandidate) -> bool:
    if name in _ID_FIELDS:
        return ids_match(str(value), str(candidate.value))
    return value == candidate.value


def _describe(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value)


# --- scoring ---------------------------------------------------------------


def _penalty_from_validation(name: str, validation: Validation) -> float:
    if any(name in issue.fields for issue in validation.errors):
        return VALIDATION_ERROR
    if any(name in issue.fields for issue in validation.warnings):
        return VALIDATION_WARNING
    return 0.0


def _score_null(name: str, ctx: ScoringContext) -> float:
    if name in ctx.unreadable_fields:
        return UNREADABLE_VALUE
    candidate = ctx.rules.get(name)
    if candidate is not None:
        ctx.notes.append(f"{name}: LLM found nothing but a rule found '{_describe(candidate.value)}'")
        return NULL_RULE_FOUND_CANDIDATE
    score = NULL_CHECKED_BY_RULE if name in RULE_FIELDS else NULL_NOT_CHECKABLE
    return score + (NULL_SCANNED_DOC if ctx.doc.has_scanned_pages else 0.0)


def _score_value(name: str, fld: Field, ctx: ScoringContext) -> float:
    score = BASE_NON_NULL
    status, page = check_evidence(fld.evidence, fld.page, ctx.doc)
    fld.page = page
    if status is EvidenceStatus.verified:
        supported = value_supported(name, fld.value, fld.evidence)
        if supported is None:  # a real quote does not prove a classification or summary is right
            score += EVIDENCE_VERIFIED_UNCHECKED
        else:
            score += EVIDENCE_VERIFIED if supported else VALUE_NOT_IN_EVIDENCE
    elif status is EvidenceStatus.missing:
        score += EVIDENCE_MISSING
    elif status is EvidenceStatus.not_found:
        score += EVIDENCE_NOT_FOUND
    else:
        score += SCANNED_SOURCE
        if value_supported(name, fld.value, fld.evidence or "") is False:
            score += VALUE_NOT_IN_EVIDENCE

    candidate = ctx.rules.get(name)
    if candidate is not None:
        if _rule_agrees(name, fld.value, candidate):
            score += RULE_AGREE
            fld.method = "llm+rule"
        else:
            score += RULE_DISAGREE
            ctx.notes.append(
                f"{name}: LLM '{_describe(fld.value)}' vs rule '{_describe(candidate.value)}'"
            )
    if name in ctx.ambiguous_fields:
        score += AMBIGUOUS_DATE
    return score + _penalty_from_validation(name, ctx.validation)


ROW_WINDOW_CHARS = 60  # how far past a row's quote its other cells can sit


def _amount_in_row(item: LineItem, doc: LoadedDocument) -> bool:
    """The row's amount is in its quote, or in the text right after it. PDF text layers
    put each table cell on its own line, so a model often quotes only the description."""
    if item.amount is None:
        return True
    wanted = {item.amount, -item.amount}
    quote = item.evidence or ""
    if wanted & set(find_amounts(quote)):
        return True
    page = doc.page(item.page) if item.page is not None else None
    if page is None:
        return False
    haystack = match_form(page.text)
    where = fuzz.partial_ratio_alignment(match_form(quote), haystack)
    if where is None:
        return False
    return bool(wanted & set(find_amounts(haystack[where.dest_start : where.dest_end + ROW_WINDOW_CHARS])))


def _score_line_item(item: LineItem, ctx: ScoringContext) -> float:
    score = BASE_NON_NULL
    status, item.page = check_evidence(item.evidence, item.page, ctx.doc)
    if status is EvidenceStatus.verified:
        amount_ok = _amount_in_row(item, ctx.doc)
        score += EVIDENCE_VERIFIED if amount_ok else VALUE_NOT_IN_EVIDENCE
    elif status is EvidenceStatus.missing:
        score += EVIDENCE_MISSING
    elif status is EvidenceStatus.not_found:
        score += EVIDENCE_NOT_FOUND
    else:
        score += SCANNED_SOURCE
    return score + _penalty_from_validation("line_items", ctx.validation)


def score_change_order(co: ChangeOrder, ctx: ScoringContext) -> None:
    """Fill in `confidence` (and `method`, corrected `page`) on every field, in place."""
    for name in SCALAR_FIELDS:
        fld: Field = getattr(co, name)
        raw = _score_null(name, ctx) if fld.value is None else _score_value(name, fld, ctx)
        fld.confidence = _clamp(raw)
    for item in co.line_items:
        item.confidence = _clamp(_score_line_item(item, ctx))
