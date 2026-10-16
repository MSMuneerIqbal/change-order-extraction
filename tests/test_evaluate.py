import json
from decimal import Decimal
from pathlib import Path

import pytest

from co_extract.evaluate import (
    KIND,
    label_change_order,
    load_cases,
    run_eval,
    run_rules_baseline,
    score_line_items,
    values_match,
)
from co_extract.schema import SCALAR_FIELDS
from co_extract.validate import validate

from conftest import FakeExtractor, extraction

ROOT = Path(__file__).resolve().parent.parent


def test_every_field_has_a_comparison_rule():
    assert set(KIND) == set(SCALAR_FIELDS)


@pytest.mark.parametrize(
    "name, predicted, expected, ok",
    [
        ("change_order_number", "co-014", "CO-014", True),
        ("change_order_number", "CO-014", "CO-041", False),
        ("total_amount", Decimal("100.00"), "100.01", True),
        ("total_amount", Decimal("100.00"), "100.02", False),
        ("total_amount", None, "100.00", False),
        ("total_amount", None, None, True),
        ("date_issued", "2026-03-04", "2026-03-04", True),
        ("date_issued", "2026-04-03", "2026-03-04", False),
        ("contractor", "Tri-County Drywall Inc", "Tri-County Drywall Inc.", True),
        ("contractor", "Kestrel Electric", "Tri-County Drywall Inc.", False),
        ("description", "Add partition at corridor 2B per RFI-087", "Add a partition at corridor 2B per RFI-087.", True),
        ("status", None, "unknown", True),
        ("status", "approved", "pending", False),
        ("signed", None, False, True),
        ("references", ["RFI-087", "CO-017"], ["co017", "rfi087"], True),
        ("references", [], None, True),
        ("references", ["RFI-087"], ["RFI-088"], False),
        ("schedule_impact_days", 0, 0, True),
        ("schedule_impact_days", None, 0, False),
    ],
)
def test_values_match(name, predicted, expected, ok):
    assert values_match(name, predicted, expected) is ok


def test_line_item_matching_is_one_to_one():
    expected = [{"description": "Foreman labour", "amount": "1000.00"}, {"description": "Foreman labour", "amount": "1000.00"}]
    predicted = [{"description": "foreman labour", "amount": "1000.00"}]
    assert score_line_items(predicted, expected) == (1, 1, 2)
    assert score_line_items(predicted + predicted + predicted, expected) == (2, 3, 2)
    assert score_line_items([{"description": "Foreman labour", "amount": "999.00"}], expected) == (0, 1, 2)


def test_every_label_is_a_valid_change_order_and_flaws_are_the_intended_ones():
    cases = load_cases(ROOT / "data" / "samples", ROOT / "data" / "labels")
    assert len(cases) == 12
    flawed = {c.path.name for c in cases if validate(label_change_order(c.label)).errors}
    assert flawed == {"05_missing_number_and_date.pdf", "06_total_math_error.pdf"}


def test_eval_writes_reports_and_never_invents_numbers(tmp_path):
    samples, labels = tmp_path / "samples", tmp_path / "labels"
    samples.mkdir()
    labels.mkdir()
    source = ROOT / "data"
    (samples / "03_email.txt").write_text((source / "samples" / "03_email.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (labels / "03_email.json").write_text((source / "labels" / "03_email.json").read_text(encoding="utf-8"), encoding="utf-8")
    cases = load_cases(samples, labels)

    report = run_eval(cases, FakeExtractor(extraction()), tmp_path / "eval", threshold=0.7)  # a model that finds nothing
    assert report["documents"] == 1
    assert report["per_field"]["change_order_number"]["correct"] == 0
    assert report["per_field"]["project_number"]["correct"] == 1  # both null
    assert report["line_items"]["recall"] == 0.0
    assert report["review"]["flagged"] == 1
    assert sum(b["fields"] for b in report["calibration"]) == len(SCALAR_FIELDS)
    assert json.loads((tmp_path / "eval" / "results.json").read_text(encoding="utf-8")) == report
    assert "Per-field accuracy" in (tmp_path / "eval" / "results.md").read_text(encoding="utf-8")
    assert (tmp_path / "eval" / "outputs" / "03_email.json").exists()


def test_rules_baseline_runs_on_the_real_samples(tmp_path):
    cases = load_cases(ROOT / "data" / "samples", ROOT / "data" / "labels")
    report = run_rules_baseline(cases, tmp_path)
    assert report["total_amount"]["found"] > 0
    assert (tmp_path / "rules_baseline.md").exists()
