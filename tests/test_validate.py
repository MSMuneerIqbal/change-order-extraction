from datetime import date
from decimal import Decimal

from co_extract.schema import ChangeOrder, Field, LineItem, Status
from co_extract.validate import validate

TODAY = date(2026, 6, 1)
NAMES = [
    "change_order_number", "project_name", "project_number", "contractor", "client", "date_issued",
    "date_approved", "description", "reason_category", "subtotal", "tax", "total_amount", "currency",
    "schedule_impact_days", "status", "requested_by", "approved_by", "signed", "references",
]  # fmt: skip


def make(items: list[LineItem] | None = None, **values) -> ChangeOrder:
    base = {"change_order_number": "CO-1", "description": "Add outlets", "total_amount": Decimal("100.00"),
            "currency": "USD"}  # fmt: skip
    base.update(values)
    fields = {n: Field(value=base.get(n)) for n in NAMES}
    return ChangeOrder(line_items=items or [], **fields)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def test_valid_change_order_has_no_errors_or_warnings():
    result = validate(make(), TODAY)
    assert result.errors == [] and result.warnings == []


def test_required_fields():
    result = validate(make(change_order_number=None, description=None, total_amount=None), TODAY)
    assert [i.fields for i in result.errors if i.code == "missing_required"] == [
        ["change_order_number"], ["description"], ["total_amount", "line_items"],
    ]  # fmt: skip


def test_line_items_can_stand_in_for_the_total():
    items = [LineItem(description="x", amount=Decimal("50.00"))]
    assert validate(make(items, total_amount=None), TODAY).errors == []


def test_line_items_must_add_up_to_subtotal():
    items = [LineItem(amount=Decimal("40.00")), LineItem(amount=Decimal("59.00"))]
    result = validate(make(items, subtotal=Decimal("100.00")), TODAY)
    assert "line_sum_mismatch" in codes(result.errors)
    assert validate(make(items, subtotal=Decimal("99.01"), total_amount=Decimal("99.01")), TODAY).errors == []  # within 0.01


def test_line_items_compared_to_total_minus_tax_when_no_subtotal():
    items = [LineItem(amount=Decimal("90.00"))]
    assert validate(make(items, tax=Decimal("10.00")), TODAY).errors == []
    assert "line_sum_mismatch" in codes(validate(make(items, tax=Decimal("5.00")), TODAY).errors)


def test_subtotal_plus_tax_must_equal_total():
    result = validate(make(subtotal=Decimal("90.00"), tax=Decimal("9.00")), TODAY)
    assert "total_mismatch" in codes(result.errors)
    assert validate(make(subtotal=Decimal("90.00"), tax=Decimal("10.00")), TODAY).errors == []
    assert validate(make(subtotal=Decimal("100.00")), TODAY).errors == []  # missing tax = 0


def test_quantity_times_price_must_equal_amount():
    bad = LineItem(description="wire", quantity=Decimal("10"), unit_price=Decimal("5.00"), amount=Decimal("55.00"))
    assert "line_math_mismatch" in codes(validate(make([bad], total_amount=Decimal("55.00")), TODAY).errors)
    ok = LineItem(quantity=Decimal("10"), unit_price=Decimal("5.00"), amount=Decimal("50.01"))
    assert validate(make([ok], total_amount=Decimal("50.01")), TODAY).errors == []


def test_approved_before_issued_is_an_error():
    result = validate(make(date_issued=date(2026, 3, 10), date_approved=date(2026, 3, 4)), TODAY)
    assert "date_order" in codes(result.errors)
    assert validate(make(date_issued=date(2026, 3, 4), date_approved=date(2026, 3, 4)), TODAY).errors == []


def test_implausible_dates_are_errors():
    assert "date_out_of_range" in codes(validate(make(date_issued=date(1999, 1, 1)), TODAY).errors)
    assert "date_out_of_range" in codes(validate(make(date_approved=date(2031, 1, 1)), TODAY).errors)
    assert validate(make(date_issued=date(2026, 5, 30)), TODAY).errors == []


def test_approved_without_approver_or_date_warns():
    result = validate(make(status=Status.approved), TODAY)
    assert "approved_without_approver" in codes(result.warnings)
    complete = make(status=Status.approved, approved_by="A. Lee", date_approved=date(2026, 5, 1), signed=True)
    assert validate(complete, TODAY).warnings == []


def test_negative_total_is_allowed_but_warns():
    result = validate(make(total_amount=Decimal("-4200.00")), TODAY)
    assert result.errors == []
    assert "negative_total" in codes(result.warnings)


def test_missing_currency_warns_when_amounts_exist():
    assert "missing_currency" in codes(validate(make(currency=None), TODAY).warnings)
