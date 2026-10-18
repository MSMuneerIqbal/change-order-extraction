"""Business-rule validation. Errors lower confidence and force review; warnings
are informational and only lightly lower confidence."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from co_extract.schema import ChangeOrder, Issue, Status, Validation

TOLERANCE = Decimal("0.01")
MAX_PAST_DAYS = 365 * 10
MAX_FUTURE_DAYS = 365 * 2


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


def _check_required(co: ChangeOrder, errors: list[Issue]) -> None:
    for name in ("change_order_number", "description"):
        if getattr(co, name).value is None:
            errors.append(Issue(code="missing_required", message=f"{name} is missing", fields=[name]))
    if co.total_amount.value is None and not co.line_items:
        errors.append(
            Issue(
                code="missing_required",
                message="neither total_amount nor line_items found",
                fields=["total_amount", "line_items"],
            )
        )


def _check_amounts(co: ChangeOrder, errors: list[Issue]) -> None:
    subtotal, tax, total = co.subtotal.value, co.tax.value, co.total_amount.value
    amounts = [item.amount for item in co.line_items if item.amount is not None]
    missing = len(co.line_items) - len(amounts)
    # A scope list with no prices at all is fine; rows with a price and rows without is not.
    if amounts:
        line_sum = sum(amounts, Decimal("0"))
        if subtotal is not None:
            target, target_field = subtotal, "subtotal"
        elif total is not None:
            target, target_field = total - (tax or Decimal("0")), "total_amount"
        else:
            target, target_field = None, ""
        if target is not None and not _close(line_sum, target):
            errors.append(
                Issue(
                    code="line_sum_mismatch",
                    message=f"line items add up to {line_sum}, document says {target}"
                    + (f" ({missing} line item(s) have no amount)" if missing else ""),
                    fields=["line_items", target_field],
                )
            )
    if subtotal is not None and total is not None:
        expected = subtotal + (tax or Decimal("0"))
        if not _close(expected, total):
            errors.append(
                Issue(
                    code="total_mismatch",
                    message=f"subtotal + tax = {expected}, document total is {total}",
                    fields=["subtotal", "tax", "total_amount"],
                )
            )
    for item in co.line_items:
        if None not in (item.quantity, item.unit_price, item.amount):
            product = item.quantity * item.unit_price
            if not _close(product, item.amount):
                errors.append(
                    Issue(
                        code="line_math_mismatch",
                        message=f"'{item.description}': {item.quantity} x {item.unit_price} = {product}, not {item.amount}",
                        fields=["line_items"],
                    )
                )


def _check_dates(co: ChangeOrder, today: date, errors: list[Issue]) -> None:
    issued, approved = co.date_issued.value, co.date_approved.value
    if issued and approved and approved < issued:
        errors.append(
            Issue(
                code="date_order",
                message=f"approved ({approved}) is before issued ({issued})",
                fields=["date_issued", "date_approved"],
            )
        )
    earliest, latest = today - timedelta(MAX_PAST_DAYS), today + timedelta(MAX_FUTURE_DAYS)
    for name in ("date_issued", "date_approved"):
        value = getattr(co, name).value
        if value and not earliest <= value <= latest:
            errors.append(Issue(code="date_out_of_range", message=f"{name} {value} is implausible", fields=[name]))


def _check_warnings(co: ChangeOrder, warnings: list[Issue]) -> None:
    if co.status.value == Status.approved:
        if co.approved_by.value is None or co.date_approved.value is None:
            warnings.append(
                Issue(
                    code="approved_without_approver",
                    message="status is approved but approved_by or date_approved is missing",
                    fields=["status", "approved_by", "date_approved"],
                )
            )
        if co.signed.value is False:
            warnings.append(
                Issue(code="approved_unsigned", message="status is approved but the signature block is empty",
                      fields=["status", "signed"])
            )
    if co.total_amount.value is not None and co.total_amount.value < 0:
        warnings.append(
            Issue(code="negative_total", message="negative total (credit change order)", fields=["total_amount"])
        )
    has_money = co.total_amount.value is not None or any(i.amount is not None for i in co.line_items)
    if has_money and co.currency.value is None:
        warnings.append(Issue(code="missing_currency", message="amounts found but no currency", fields=["currency"]))


def validate(co: ChangeOrder, today: date | None = None) -> Validation:
    """Apply the business rules. `today` is injectable for deterministic tests."""
    result = Validation()
    _check_required(co, result.errors)
    _check_amounts(co, result.errors)
    _check_dates(co, today or date.today(), result.errors)
    _check_warnings(co, result.warnings)
    return result
