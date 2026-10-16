"""Evaluation: run the pipeline on the samples and compare with data/labels.

Metrics: per-field accuracy, line-item precision/recall, review behaviour,
confidence calibration, latency and cost. Everything reported is computed from
the run; nothing is hard-coded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from co_extract.confidence import RULE_FIELDS
from co_extract.llm import Extractor
from co_extract.loader import SUPPORTED_SUFFIXES, load_document
from co_extract.normalize import alnum, match_form
from co_extract.pipeline import process_loaded
from co_extract.rules import extract_with_rules
from co_extract.schema import SCALAR_FIELDS, ChangeOrder, ExtractionResult
from co_extract.validate import validate

MONEY_TOLERANCE = Decimal("0.01")
FUZZY_MIN = 85
ITEM_DESC_MIN = 60
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0001))
# Fields where a wrong value costs real money or compliance. A wrong reason_category or
# currency is a smaller problem, so review is also scored on this narrower set.
CRITICAL_FIELDS = frozenset(
    {"change_order_number", "subtotal", "tax", "total_amount", "date_issued", "date_approved",
     "status", "schedule_impact_days"}
)  # fmt: skip

KIND = {
    "change_order_number": "id", "project_number": "id",
    "date_issued": "date", "date_approved": "date",
    "subtotal": "money", "tax": "money", "total_amount": "money",
    "status": "exact", "reason_category": "exact", "currency": "exact",
    "schedule_impact_days": "exact", "signed": "bool", "references": "refs",
    "project_name": "name", "contractor": "name", "client": "name",
    "requested_by": "name", "approved_by": "name", "description": "text",
}  # fmt: skip


@dataclass
class Case:
    path: Path
    label: dict[str, Any]


def load_cases(samples_dir: Path, labels_dir: Path) -> list[Case]:
    """Pair every sample file with its label file (same stem)."""
    cases = []
    for path in sorted(samples_dir.iterdir()):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        label_path = labels_dir / f"{path.stem}.json"
        if not label_path.exists():
            raise FileNotFoundError(f"No label for {path.name}: expected {label_path}")
        cases.append(Case(path, json.loads(label_path.read_text(encoding="utf-8"))))
    return cases


# ---- comparison --------------------------------------------------------------


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def values_match(name: str, predicted: Any, expected: Any) -> bool:
    """Compare one field using the rule for its kind (exact / money / date / fuzzy)."""
    kind = KIND[name]
    predicted, expected = _plain(predicted), _plain(expected)
    if kind == "bool":
        return bool(predicted) == bool(expected)
    if name == "status":
        predicted, expected = predicted or "unknown", expected or "unknown"
    if kind == "refs":
        return {alnum(r) for r in predicted or []} == {alnum(r) for r in expected or []}
    if predicted is None or expected is None:
        return predicted is None and expected is None
    if kind == "money":
        return abs(Decimal(str(predicted)) - Decimal(str(expected))) <= MONEY_TOLERANCE
    if kind == "date":
        as_text = lambda v: v.isoformat() if isinstance(v, date) else str(v)  # noqa: E731
        return as_text(predicted) == as_text(expected)
    if kind == "id":
        return str(predicted).strip().casefold() == str(expected).strip().casefold()
    if kind in ("name", "text"):
        scorer = fuzz.token_set_ratio if kind == "text" else fuzz.ratio
        return scorer(match_form(str(predicted)), match_form(str(expected))) >= FUZZY_MIN
    return predicted == expected


def score_line_items(predicted: list[dict[str, Any]], expected: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Greedy one-to-one match on amount and description. Returns (matched, n_pred, n_label)."""
    remaining = list(expected)
    matched = 0
    for p in predicted:
        for e in remaining:
            same_amount = (
                p["amount"] is None and e["amount"] is None
            ) or (
                p["amount"] is not None
                and e["amount"] is not None
                and abs(Decimal(str(p["amount"])) - Decimal(str(e["amount"]))) <= MONEY_TOLERANCE
            )
            similar = fuzz.token_set_ratio(match_form(p["description"] or ""), match_form(e["description"] or ""))
            if same_amount and similar >= ITEM_DESC_MIN:
                remaining.remove(e)
                matched += 1
                break
    return matched, len(predicted), len(expected)


def label_change_order(label: dict[str, Any]) -> ChangeOrder:
    """Wrap a label in the output schema so the validator can run on it."""
    data: dict[str, Any] = {name: {"value": label[name]} for name in SCALAR_FIELDS}
    data["line_items"] = label["line_items"]
    return ChangeOrder.model_validate(data)


@dataclass
class DocScore:
    file: str
    correct: dict[str, bool]
    confidence: dict[str, float]
    items: tuple[int, int, int]
    needs_review: bool
    should_review: bool
    should_review_critical: bool
    wrong_fields: list[str]
    latency_ms: int
    cost_usd: float | None
    input_tokens: int
    output_tokens: int


def score_document(result: ExtractionResult, label: dict[str, Any]) -> DocScore:
    co = result.change_order
    correct = {n: values_match(n, getattr(co, n).value, label[n]) for n in SCALAR_FIELDS}
    confidence = {n: getattr(co, n).confidence for n in SCALAR_FIELDS}
    predicted_items = [i.model_dump(mode="json") for i in co.line_items]
    items = score_line_items(predicted_items, label["line_items"])
    wrong = [n for n, ok in correct.items() if not ok]
    items_wrong = items[0] < max(items[1], items[2])
    extraction_wrong = bool(wrong) or items_wrong
    critical_wrong = items_wrong or any(n in CRITICAL_FIELDS for n in wrong)
    # A document that is itself inconsistent (missing number, bad arithmetic) should be reviewed too.
    document_flawed = bool(validate(label_change_order(label)).errors)
    return DocScore(
        file=result.source_file,
        correct=correct,
        confidence=confidence,
        items=items,
        needs_review=result.needs_review,
        should_review=extraction_wrong or document_flawed,
        should_review_critical=critical_wrong or document_flawed,
        wrong_fields=wrong,
        latency_ms=result.processing.latency_ms,
        cost_usd=result.processing.cost_usd_estimate,
        input_tokens=result.processing.input_tokens,
        output_tokens=result.processing.output_tokens,
    )


# ---- aggregation -------------------------------------------------------------


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def build_report(scores: list[DocScore], model: str) -> dict[str, Any]:
    n_docs = len(scores)
    per_field = {}
    for name in SCALAR_FIELDS:
        hits = sum(s.correct[name] for s in scores)
        per_field[name] = {"correct": hits, "total": n_docs, "accuracy": _ratio(hits, n_docs)}
    all_hits = sum(v["correct"] for v in per_field.values())

    matched = sum(s.items[0] for s in scores)
    n_pred = sum(s.items[1] for s in scores)
    n_label = sum(s.items[2] for s in scores)

    calibration = []
    for low, high in BUCKETS:
        points = [s.correct[n] for s in scores for n in SCALAR_FIELDS if low <= s.confidence[n] < high]
        calibration.append(
            {"bucket": f"{low:.1f}-{min(high, 1.0):.1f}", "fields": len(points), "correct": sum(points),
             "accuracy": _ratio(sum(points), len(points))}  # fmt: skip
        )

    flagged = [s for s in scores if s.needs_review]
    should = [s for s in scores if s.should_review]
    caught = [s for s in should if s.needs_review]
    should_critical = [s for s in scores if s.should_review_critical]
    caught_critical = [s for s in should_critical if s.needs_review]
    costs = [s.cost_usd for s in scores if s.cost_usd is not None]
    return {
        "model": model,
        "documents": n_docs,
        "field_accuracy_overall": _ratio(all_hits, n_docs * len(SCALAR_FIELDS)),
        "per_field": per_field,
        "line_items": {
            "precision": _ratio(matched, n_pred), "recall": _ratio(matched, n_label),
            "matched": matched, "predicted": n_pred, "expected": n_label,
        },  # fmt: skip
        "review": {
            "needs_review_rate": _ratio(len(flagged), n_docs),
            "flagged": len(flagged),
            "documents_that_should_be_reviewed": len(should),
            "caught": len(caught),
            "catch_rate": _ratio(len(caught), len(should)),
            "flagged_but_clean": [s.file for s in flagged if not s.should_review],
            "missed": [s.file for s in should if not s.needs_review],
            "critical_should_review": len(should_critical),
            "critical_caught": len(caught_critical),
            "critical_catch_rate": _ratio(len(caught_critical), len(should_critical)),
            "critical_missed": [s.file for s in should_critical if not s.needs_review],
        },
        "calibration": calibration,
        "avg_latency_ms": round(sum(s.latency_ms for s in scores) / n_docs) if n_docs else None,
        "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "total_input_tokens": sum(s.input_tokens for s in scores),
        "total_output_tokens": sum(s.output_tokens for s in scores),
        "per_document": [
            {"file": s.file, "needs_review": s.needs_review, "should_review": s.should_review,
             "wrong_fields": s.wrong_fields, "latency_ms": s.latency_ms, "cost_usd": s.cost_usd}
            for s in scores
        ],  # fmt: skip
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evaluation results", "",
        f"Model: `{report['model']}`  |  Documents: {report['documents']}  |  "
        f"Overall field accuracy: **{_pct(report['field_accuracy_overall'])}**", "",
        "## Per-field accuracy", "", "| Field | Correct | Accuracy |", "|---|---|---|",
    ]  # fmt: skip
    lines += [f"| {n} | {v['correct']}/{v['total']} | {_pct(v['accuracy'])} |" for n, v in report["per_field"].items()]
    li = report["line_items"]
    lines += [
        "", "## Line items", "",
        f"Precision {_pct(li['precision'])} ({li['matched']}/{li['predicted']}), "
        f"recall {_pct(li['recall'])} ({li['matched']}/{li['expected']}).", "",
    ]  # fmt: skip
    rv = report["review"]
    lines += [
        "## Review", "",
        f"- Flagged for review: {rv['flagged']}/{report['documents']} ({_pct(rv['needs_review_rate'])})",
        f"- Documents that should be reviewed (wrong extraction, or the document itself is inconsistent): "
        f"{rv['documents_that_should_be_reviewed']}",
        f"- Caught by review: {rv['caught']} ({_pct(rv['catch_rate'])})",
        f"- Counting only critical fields (CO number, amounts, dates, status, schedule days, line items) "
        f"or a flawed document: {rv['critical_should_review']} should be reviewed, "
        f"{rv['critical_caught']} caught ({_pct(rv['critical_catch_rate'])}); "
        f"missed: {', '.join(rv['critical_missed']) or 'none'}",
        f"- Flagged but extraction was correct and document consistent: {', '.join(rv['flagged_but_clean']) or 'none'}",
        f"- Missed (wrong but not flagged): {', '.join(rv['missed']) or 'none'}", "",
        "## Calibration (field accuracy by confidence bucket)", "",
        "| Confidence | Fields | Correct | Accuracy |", "|---|---|---|---|",
    ]  # fmt: skip
    lines += [f"| {b['bucket']} | {b['fields']} | {b['correct']} | {_pct(b['accuracy'])} |" for b in report["calibration"]]
    cost = report["avg_cost_usd"]
    lines += [
        "", "## Cost and speed", "",
        f"- Average latency per document: {report['avg_latency_ms']} ms",
        f"- Average estimated cost per document: {'unknown (no price for this model)' if cost is None else f'${cost:.5f}'}",
        f"- Tokens: {report['total_input_tokens']} in, {report['total_output_tokens']} out", "",
        "## Per document", "", "| File | Flagged | Should flag | Wrong fields |", "|---|---|---|---|",
    ]  # fmt: skip
    lines += [
        f"| {d['file']} | {d['needs_review']} | {d['should_review']} | {', '.join(d['wrong_fields']) or '-'} |"
        for d in report["per_document"]
    ]
    return "\n".join(lines) + "\n"


# ---- entry points ------------------------------------------------------------


def run_eval(
    cases: list[Case], extractor: Extractor, out_dir: Path, threshold: float
) -> dict[str, Any]:
    """Run the full pipeline on every case and write eval/results.{md,json} + per-document outputs."""
    outputs = out_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    scores = []
    for case in cases:
        result = process_loaded(load_document(case.path), extractor, threshold)
        (outputs / f"{case.path.stem}.json").write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        scores.append(score_document(result, case.label))
    report = build_report(scores, extractor.model)
    (out_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "results.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def run_rules_baseline(cases: list[Case], out_dir: Path) -> dict[str, Any]:
    """Score the rule extractor alone (no LLM, no key needed): how much it finds and how often it is right."""
    stats = {name: {"found": 0, "correct": 0, "labelled": 0} for name in sorted(RULE_FIELDS)}
    for case in cases:
        hits = extract_with_rules(load_document(case.path))
        for name, s in stats.items():
            s["labelled"] += case.label[name] is not None
            if name in hits:
                s["found"] += 1
                s["correct"] += values_match(name, hits[name].value, case.label[name])
    report = {
        name: {**s, "precision": _ratio(s["correct"], s["found"]), "recall": _ratio(s["correct"], s["labelled"])}
        for name, s in stats.items()
    }
    lines = [
        "# Rule-extractor baseline (no LLM)", "",
        "| Field | Found | Correct | Labelled | Precision | Recall |", "|---|---|---|---|---|---|",
    ]  # fmt: skip
    lines += [
        f"| {n} | {v['found']} | {v['correct']} | {v['labelled']} | {_pct(v['precision'])} | {_pct(v['recall'])} |"
        for n, v in report.items()
    ]
    (out_dir / "rules_baseline.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "rules_baseline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
