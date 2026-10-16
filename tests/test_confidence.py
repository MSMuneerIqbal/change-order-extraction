from datetime import date
from decimal import Decimal

import pytest

from co_extract import confidence as c
from co_extract.loader import LoadedDocument, Page
from co_extract.rules import RuleCandidate
from co_extract.schema import ChangeOrder, Field, Issue, LineItem, Validation

from conftest import SCALARS, text_doc

DOC = text_doc("Change Order No.: CO-014\nDate Issued: February 10, 2026\nTotal $1,602.10\nApproved by: Robert Okafor")


# ---- evidence matching ----------------------------------------------------------------


def test_exact_quote_is_verified():
    status, page = c.check_evidence("Change Order No.: CO-014", 1, DOC)
    assert (status, page) == (c.EvidenceStatus.verified, 1)


def test_quote_matches_despite_case_whitespace_and_small_typos():
    assert c.check_evidence("change order  no.:\nCO-014", 1, DOC)[0] is c.EvidenceStatus.verified
    assert c.check_evidence("Approved by: Robert Okafar", 1, DOC)[0] is c.EvidenceStatus.verified  # 1 letter off


def test_invented_quote_is_not_found():
    status, _ = c.check_evidence("Approved by: Maria Santos, Owner", 1, DOC)
    assert status is c.EvidenceStatus.not_found


def test_missing_quote():
    assert c.check_evidence(None, 1, DOC)[0] is c.EvidenceStatus.missing
    assert c.check_evidence("   ", 1, DOC)[0] is c.EvidenceStatus.missing


def test_quote_found_on_a_different_page_corrects_the_page_number():
    doc = text_doc("first page text", "Total change order amount: $9,840.00")
    assert c.check_evidence("Total change order amount: $9,840.00", 1, doc) == (c.EvidenceStatus.verified, 2)


def test_quote_on_a_scanned_page_cannot_be_verified():
    doc = LoadedDocument("scan.pdf", [Page(number=1, text="", image=b"png")])
    assert c.check_evidence("Total $5,000.00", 1, doc)[0] is c.EvidenceStatus.unverifiable


@pytest.mark.parametrize(
    "name, value, evidence, expected",
    [
        ("change_order_number", "CO-014", "Change Order No.: CO-014", True),
        ("change_order_number", "CO-041", "Change Order No.: CO-014", False),
        ("date_issued", date(2026, 3, 4), "Date: 03/04/26", True),
        ("date_issued", date(2026, 4, 3), "Date: 03/04/26", True),  # either reading follows from the quote
        ("date_issued", date(2026, 3, 5), "Date: 03/04/26", False),
        ("total_amount", Decimal("1602.10"), "TOTAL $1,602.10", True),
        ("total_amount", Decimal("-4200.00"), "Total deduction $4,200.00", True),  # sign may be implied
        ("total_amount", Decimal("1602.11"), "TOTAL $1,602.10", False),
        ("schedule_impact_days", 3, "+3 calendar days", True),
        ("schedule_impact_days", 0, "No change to contract time", True),
        ("schedule_impact_days", 9, "+3 calendar days", False),
        ("currency", "CAD", "All amounts in Canadian dollars", True),
        ("currency", "USD", "TOTAL $1,602.10", True),
        ("currency", "CAD", "TOTAL $1,602.10", False),
        ("approved_by", "Robert Okafor", "Approved by: Robert Okafor, PM", True),
        ("approved_by", "Maria Santos", "Approved by: Robert Okafor", False),
        ("references", ["RFI-087", "SC-2025-044"], "Ref: RFI-087; Subcontract SC-2025-044", True),
        ("references", ["RFI-099"], "Ref: RFI-087", False),
        ("status", "approved", "STATUS: APPROVED", None),  # enums cannot be checked against text
    ],
)
def test_value_supported(name, value, evidence, expected):
    assert c.value_supported(name, value, evidence) is expected


# ---- scoring -----------------------------------------------------------------------


def make_co(**fields: Field) -> ChangeOrder:
    data = {n: fields.get(n, Field()) for n in SCALARS}
    return ChangeOrder(**data)


def ctx(doc=DOC, rules=None, validation=None, **kw) -> c.ScoringContext:
    return c.ScoringContext(doc, rules or {}, validation or Validation(), **kw)


def score(name: str, fld: Field, **kw) -> float:
    co = make_co(**{name: fld})
    c.score_change_order(co, ctx(**kw))
    return getattr(co, name).confidence


def co_number(evidence="Change Order No.: CO-014", value="CO-014") -> Field:
    return Field(value=value, evidence=evidence, page=1)


def test_verified_value_scores_base_plus_evidence_bonus():
    assert score("change_order_number", co_number()) == pytest.approx(c.BASE_NON_NULL + c.EVIDENCE_VERIFIED)


def test_made_up_evidence_gets_a_big_penalty():
    fld = co_number(evidence="Change Order No.: CO-099", value="CO-099")
    assert score("change_order_number", fld) == pytest.approx(c.BASE_NON_NULL + c.EVIDENCE_NOT_FOUND)


def test_value_not_in_its_own_quote_is_penalised():
    fld = co_number(value="CO-777")  # quote is real, value does not come from it
    assert score("change_order_number", fld) == pytest.approx(c.BASE_NON_NULL + c.VALUE_NOT_IN_EVIDENCE)


def test_value_without_any_evidence_is_penalised():
    fld = Field(value="CO-014")
    assert score("change_order_number", fld) == pytest.approx(c.BASE_NON_NULL + c.EVIDENCE_MISSING)


def test_agreeing_rule_boosts_and_marks_method():
    co = make_co(change_order_number=co_number())
    c.score_change_order(co, ctx(rules={"change_order_number": RuleCandidate("CO-014", "x", 1)}))
    assert co.change_order_number.confidence == pytest.approx(c.BASE_NON_NULL + c.EVIDENCE_VERIFIED + c.RULE_AGREE)
    assert co.change_order_number.method == "llm+rule"


def test_disagreeing_rule_penalises_and_leaves_a_note():
    context = ctx(rules={"change_order_number": RuleCandidate("CO-041", "x", 1)})
    co = make_co(change_order_number=co_number())
    c.score_change_order(co, context)
    assert co.change_order_number.confidence == pytest.approx(c.BASE_NON_NULL + c.EVIDENCE_VERIFIED + c.RULE_DISAGREE)
    assert co.change_order_number.method == "llm"
    assert "CO-041" in context.notes[0]


def test_failed_validation_rule_penalises_the_fields_involved():
    validation = Validation(errors=[Issue(code="x", message="m", fields=["change_order_number"])])
    assert score("change_order_number", co_number(), validation=validation) == pytest.approx(
        c.BASE_NON_NULL + c.EVIDENCE_VERIFIED + c.VALIDATION_ERROR
    )
    warning = Validation(warnings=[Issue(code="x", message="m", fields=["change_order_number"])])
    assert score("change_order_number", co_number(), validation=warning) == pytest.approx(
        c.BASE_NON_NULL + c.EVIDENCE_VERIFIED + c.VALIDATION_WARNING
    )


def test_scanned_source_gets_a_small_penalty():
    doc = LoadedDocument("scan.pdf", [Page(number=1, text="", image=b"png")])
    assert score("change_order_number", co_number(), doc=doc) == pytest.approx(c.BASE_NON_NULL + c.SCANNED_SOURCE)


def test_ambiguous_date_is_penalised():
    fld = Field(value=date(2026, 2, 10), evidence="Date Issued: February 10, 2026", page=1)
    plain = score("date_issued", fld)
    assert score("date_issued", fld, ambiguous_fields={"date_issued"}) == pytest.approx(plain + c.AMBIGUOUS_DATE)


def test_null_is_confident_when_nothing_looks_like_the_field():
    assert score("change_order_number", Field()) == pytest.approx(c.NULL_CHECKED_BY_RULE)
    assert score("project_name", Field()) == pytest.approx(c.NULL_NOT_CHECKABLE)


def test_null_is_unsure_when_a_rule_found_a_candidate():
    context = ctx(rules={"change_order_number": RuleCandidate("CO-014", "x", 1)})
    co = make_co()
    c.score_change_order(co, context)
    assert co.change_order_number.confidence == pytest.approx(c.NULL_RULE_FOUND_CANDIDATE)
    assert "CO-014" in context.notes[0]


def test_null_on_a_scanned_document_is_less_sure_because_rules_cannot_read_it():
    doc = LoadedDocument("scan.pdf", [Page(number=1, text="", image=b"png")])
    assert score("change_order_number", Field(), doc=doc) == pytest.approx(c.NULL_CHECKED_BY_RULE + c.NULL_SCANNED_DOC)


def test_unreadable_value_scores_low():
    assert score("date_issued", Field(), unreadable_fields={"date_issued"}) == pytest.approx(c.UNREADABLE_VALUE)


def test_scores_are_clamped_to_unit_interval():
    fld = Field(value="CO-014", evidence="Change Order No.: CO-014", page=1)
    best = score("change_order_number", fld, rules={"change_order_number": RuleCandidate("CO-014", "x", 1)})
    assert 0.0 <= best <= 1.0
    worst_validation = Validation(errors=[Issue(code="x", message="m", fields=["change_order_number"])])
    worst = score("change_order_number", Field(value="CO-099", evidence="made up", page=1),
                  validation=worst_validation, rules={"change_order_number": RuleCandidate("CO-014", "x", 1)})  # fmt: skip
    assert worst == 0.0


def test_line_item_scoring():
    good = LineItem(description="Foreman labour", amount=Decimal("1000.00"), evidence="Foreman labour 16 HR $62.50 $1,000.00", page=1)
    fake = LineItem(description="Gold plating", amount=Decimal("9.00"), evidence="Gold plating $9.00", page=1)
    co = ChangeOrder(**{n: Field() for n in SCALARS}, line_items=[good, fake])
    doc = text_doc("Foreman labour\n16\nHR\n$62.50\n$1,000.00")
    c.score_change_order(co, ctx(doc=doc))
    assert good.confidence > 0.7 > fake.confidence
