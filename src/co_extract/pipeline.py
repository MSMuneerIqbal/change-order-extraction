"""Glue: load -> LLM -> rules -> normalise -> validate -> score -> result."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from co_extract.confidence import ScoringContext, score_change_order
from co_extract.llm import Extractor, estimate_cost
from co_extract.loader import LoadedDocument, load_document
from co_extract.normalize import infer_day_first, to_change_order
from co_extract.rules import extract_with_rules, find_change_order_numbers, find_injection_phrases
from co_extract.schema import REQUIRED_FIELDS, ChangeOrder, ExtractionResult, Issue, Processing, Validation
from co_extract.validate import validate

DEFAULT_REVIEW_THRESHOLD = 0.7
# Warnings that are serious enough to send the document to a person.
REVIEW_WARNING_CODES = frozenset(
    {"ambiguous_date", "possible_prompt_injection", "truncated_document", "multiple_change_orders"}
)


def _extra_warnings(doc: LoadedDocument, ambiguous: set[str], unreadable: dict[str, str]) -> list[Issue]:
    warnings = [
        Issue(code="ambiguous_date", message=f"{name} is a numeric date that could be day-first or month-first", fields=[name])
        for name in sorted(ambiguous)
    ]
    warnings += [
        Issue(code="unreadable_value", message=f"{name}: the model returned '{raw}', which is not a usable value", fields=[name])
        for name, raw in sorted(unreadable.items())
    ]
    if doc.truncated:
        warnings.append(Issue(code="truncated_document", message="document is longer than the page limit; later pages were not read"))
    numbers = find_change_order_numbers(doc)
    if len(numbers) > 1:
        warnings.append(
            Issue(
                code="multiple_change_orders",
                message=f"file contains more than one change order ({', '.join(numbers)}); only the first is extracted",
            )
        )
    for line in find_injection_phrases(doc):
        warnings.append(Issue(code="possible_prompt_injection", message=f"text that looks like an instruction to an AI: '{line}'"))
    return warnings


def _required_confidence(co: ChangeOrder, name: str) -> float:
    """Confidence of a required field. A missing total is covered by line items."""
    fld = getattr(co, name)
    if name == "total_amount" and fld.value is None and co.line_items:
        return min(item.confidence for item in co.line_items)
    return fld.confidence


def decide_review(
    co: ChangeOrder, validation: Validation, notes: list[str], unreadable: dict[str, str], threshold: float
) -> tuple[bool, list[str]]:
    """Return (needs_review, reasons)."""
    reasons = [f"validation error: {issue.message}" for issue in validation.errors]
    for name in REQUIRED_FIELDS:
        confidence = _required_confidence(co, name)
        if confidence < threshold:
            reasons.append(f"required field {name} has low confidence ({confidence:.2f} < {threshold})")
    reasons += [f"{name}: the model returned '{raw}', which is not a usable value" for name, raw in sorted(unreadable.items())]
    reasons += notes
    reasons += [w.message for w in validation.warnings if w.code in REVIEW_WARNING_CODES]
    return bool(reasons), reasons


def process_loaded(
    doc: LoadedDocument,
    extractor: Extractor,
    threshold: float = DEFAULT_REVIEW_THRESHOLD,
    today: date | None = None,
) -> ExtractionResult:
    """Run the full pipeline on an already-loaded document."""
    started = time.perf_counter()
    response = extractor.extract(doc)
    rules = extract_with_rules(doc)
    day_first = infer_day_first("\n".join(p.text for p in doc.pages)) or False
    normalized = to_change_order(response.extraction, day_first)
    co = normalized.change_order

    validation = validate(co, today)
    validation.warnings += _extra_warnings(doc, normalized.ambiguous, normalized.unreadable)
    ctx = ScoringContext(doc, rules, validation, normalized.ambiguous, set(normalized.unreadable))
    score_change_order(co, ctx)
    needs_review, reasons = decide_review(co, validation, ctx.notes, normalized.unreadable, threshold)

    return ExtractionResult(
        source_file=doc.source_file,
        pages=len(doc.pages),
        change_order=co,
        validation=validation,
        needs_review=needs_review,
        review_reasons=reasons,
        processing=Processing(
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            cost_usd_estimate=estimate_cost(response.model, response.input_tokens, response.output_tokens),
        ),
    )


def process_document(
    path: Path,
    extractor: Extractor,
    threshold: float = DEFAULT_REVIEW_THRESHOLD,
    today: date | None = None,
) -> ExtractionResult:
    """Load one file and run the pipeline on it."""
    return process_loaded(load_document(path), extractor, threshold, today)
