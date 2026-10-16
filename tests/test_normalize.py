from datetime import date
from decimal import Decimal

import pytest

from co_extract.normalize import (
    clean_name,
    collapse_whitespace,
    find_amounts,
    infer_day_first,
    normalize_currency,
    parse_date,
    parse_days,
    parse_money,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-02-10", date(2026, 2, 10)),
        ("February 10, 2026", date(2026, 2, 10)),
        ("Feb 10th, 2026", date(2026, 2, 10)),
        ("4 March 2026", date(2026, 3, 4)),
        ("4-Mar-26", date(2026, 3, 4)),
        ("Date Issued: 11 March 2026", date(2026, 3, 11)),
        ("02/17/2026", date(2026, 2, 17)),  # 17 cannot be a month
        ("25/03/26", date(2026, 3, 25)),  # 25 cannot be a month
    ],
)
def test_unambiguous_dates(raw, expected):
    parsed = parse_date(raw)
    assert parsed.value == expected
    assert not parsed.ambiguous


def test_ambiguous_numeric_date_follows_day_first_flag():
    month_first = parse_date("03/04/26", day_first=False)
    day_first = parse_date("03/04/26", day_first=True)
    assert (month_first.value, month_first.ambiguous) == (date(2026, 3, 4), True)
    assert (day_first.value, day_first.ambiguous) == (date(2026, 4, 3), True)


def test_same_day_and_month_is_not_ambiguous():
    assert not parse_date("05/05/2026").ambiguous


@pytest.mark.parametrize("raw", [None, "", "not a date", "31/02/2026", "13/13/2026"])
def test_unparsable_dates_return_none(raw):
    assert parse_date(raw).value is None


def test_infer_day_first_from_other_dates_in_the_document():
    assert infer_day_first("issued 25/03/26, approved 03/04/26") is True
    assert infer_day_first("approved 03/25/26") is False
    assert infer_day_first("approved 03/04/26") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$12,450.00", Decimal("12450.00")),
        ("12450", Decimal("12450.00")),
        ("(4,200.00)", Decimal("-4200.00")),
        ("-$4,200", Decimal("-4200.00")),
        ("$-4,200.50", Decimal("-4200.50")),
        ("CAD 23,772.00", Decimal("23772.00")),
        ("1.234,56", Decimal("1234.56")),  # European format
        ("1,234", Decimal("1234.00")),
        ("US$ 99.5", Decimal("99.50")),
        ("0.005", Decimal("0.01")),  # rounds half up to 2 places
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "n/a", "TBD"])
def test_parse_money_rejects_non_amounts(raw):
    assert parse_money(raw) is None


def test_money_has_two_places():
    assert str(parse_money("5")) == "5.00"


def test_find_amounts_ignores_bare_integers_and_hyphens():
    text = "Qty 640 SF at $3.85 = $2,464.00 - see RFI 87, total ($4,200.00)"
    assert find_amounts(text) == [Decimal("3.85"), Decimal("2464.00"), Decimal("-4200.00")]


@pytest.mark.parametrize(
    "raw, expected",
    [("C$", "CAD"), ("Canadian dollars", "CAD"), ("usd", "USD"), ("$", "USD"), ("EUR", "EUR"), ("pesos", None), (None, None)],
)
def test_normalize_currency(raw, expected):
    assert normalize_currency(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [(5, 5), (-2, -2), ("5 calendar days", 5), ("-3", -3), ("+7 days", 7), ("no change", 0), ("see attached", None), (None, None)],
)
def test_parse_days(raw, expected):
    assert parse_days(raw) == expected


def test_clean_name_and_whitespace():
    assert clean_name("  Tri-County   Drywall Inc. , ") == "Tri-County Drywall Inc."
    assert clean_name("  ") is None
    assert collapse_whitespace("a  \t b c\n\n\n\nd") == "a b c\n\nd"
