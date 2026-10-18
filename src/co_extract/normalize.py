"""Turn raw strings from the document into typed values.

Dates become `date`, money becomes 2-place `Decimal`, day counts become `int`.
Anything that cannot be parsed returns None rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable

from co_extract.schema import ChangeOrder, Field, LineItem, LLMExtraction, LLMField

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip

_MONTH = r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")"

_ISO = r"(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})"
_NUMERIC = r"(?P<na>\d{1,2})[/.\-](?P<nb>\d{1,2})[/.\-](?P<ny>\d{4}|\d{2})"
_DAY_MONTH = rf"(?P<dd>\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+(?P<dm>{_MONTH})\.?[,\s\-]+(?P<dy>\d{{4}}|\d{{2}})"
_MONTH_DAY = rf"(?P<mm>{_MONTH})\.?\s+(?P<md>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<my>\d{{4}})"

DATE_RE = re.compile(
    rf"\b(?:{_ISO}|{_NUMERIC}|{_DAY_MONTH}|{_MONTH_DAY})\b", re.IGNORECASE
)

_AMOUNT_RE = re.compile(
    r"(?P<neg_paren>\()?\s*(?P<sign>-)?(?:US\$|CA\$|C\$|\$|USD|CAD)?(?P<sign2>-)?\s*"
    r"(?P<num>\d(?:[\d,.]*\d)?)\s*(?P<close>\))?\s*(?P<trail>CR\b)?",
    re.IGNORECASE,
)


def collapse_whitespace(text: str) -> str:
    """Normalise horizontal whitespace and blank lines; keep line breaks."""
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def match_form(text: str) -> str:
    """Lowercase, single-spaced text used for evidence matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def alnum(text: str) -> str:
    """Lowercase letters and digits only, for ID comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def clean_name(raw: str | None) -> str | None:
    """Trim whitespace and stray punctuation around a name or ID."""
    if raw is None:
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip(" \t\n,;:|")
    return cleaned or None


# ---- dates ---------------------------------------------------------------


@dataclass(frozen=True)
class ParsedDate:
    value: date | None
    ambiguous: bool = False


def _full_year(raw: str) -> int:
    year = int(raw)
    if len(raw) == 4:
        return year
    return 2000 + year if year <= 69 else 1900 + year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _date_from_match(m: re.Match[str], day_first: bool) -> ParsedDate:
    g = m.groupdict()
    if g["iy"]:
        return ParsedDate(_safe_date(int(g["iy"]), int(g["im"]), int(g["id"])))
    if g["na"]:
        a, b, year = int(g["na"]), int(g["nb"]), _full_year(g["ny"])
        if a > 12:
            return ParsedDate(_safe_date(year, b, a))
        if b > 12:
            return ParsedDate(_safe_date(year, a, b))
        day, month = (a, b) if day_first else (b, a)
        return ParsedDate(_safe_date(year, month, day), ambiguous=a != b)
    if g["dd"]:
        return ParsedDate(
            _safe_date(_full_year(g["dy"]), MONTHS[g["dm"].lower()], int(g["dd"]))
        )
    return ParsedDate(_safe_date(int(g["my"]), MONTHS[g["mm"].lower()], int(g["md"])))


def parse_date(raw: str | None, day_first: bool = False) -> ParsedDate:
    """Parse the first date found in `raw`. Numeric dates like 03/04/26 are
    ambiguous when both parts are <= 12; `day_first` decides how to read them."""
    if not raw:
        return ParsedDate(None)
    m = DATE_RE.search(raw)
    return _date_from_match(m, day_first) if m else ParsedDate(None)


def find_dates(text: str, day_first: bool = False) -> list[ParsedDate]:
    """Every parsable date in `text`, in order."""
    return [_date_from_match(m, day_first) for m in DATE_RE.finditer(text)]


def infer_day_first(text: str) -> bool | None:
    """Read the document's own numeric dates for the day/month order.

    A date such as 25/03/26 can only be day-first; 03/25/26 only month-first.
    Returns None when nothing in the text settles it."""
    for m in DATE_RE.finditer(text):
        if m.group("na"):
            a, b = int(m.group("na")), int(m.group("nb"))
            if a > 12 >= b:
                return True
            if b > 12 >= a:
                return False
    return None


# ---- money ---------------------------------------------------------------

_CURRENCY_ALIASES = {
    "CAD": "CAD", "C$": "CAD", "CA$": "CAD", "CDN": "CAD", "CANADIAN": "CAD",
    "USD": "USD", "US$": "USD", "$": "USD", "US": "USD",
    "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP",
}  # fmt: skip


def normalize_currency(raw: str | None) -> str | None:
    """Map 'C$', 'Canadian dollars', 'usd' ... to an ISO-style code."""
    if not raw:
        return None
    text = raw.strip().upper()
    if text in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[text]
    for token in re.findall(r"[A-Z]{2,8}|[A-Z]{1,2}\$|\$|€|£", text):
        if token in _CURRENCY_ALIASES:
            return _CURRENCY_ALIASES[token]
    return None


def _decimal_from_digits(token: str) -> Decimal | None:
    """Handle 1,234.56 and 1.234,56 style numbers."""
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        head, _, tail = token.rpartition(",")
        token = head.replace(",", "") + "." + tail if len(tail) <= 2 else token.replace(",", "")
    elif token.count(".") > 1:
        token = token.replace(".", "")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _money_from_match(m: re.Match[str]) -> Decimal | None:
    number = _decimal_from_digits(m.group("num"))
    if number is None:
        return None
    negative = bool(
        (m.group("neg_paren") and m.group("close"))
        or m.group("sign")
        or m.group("sign2")
        or m.group("trail")
    )
    if negative:
        number = -number
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_money(raw: str | None) -> Decimal | None:
    """Parse '$12,450.00', '(4,200.00)', '-$4,200', 'CAD 1.234,50' to Decimal."""
    if not raw:
        return None
    m = _AMOUNT_RE.search(raw.strip())
    return _money_from_match(m) if m else None


def find_amounts(text: str) -> list[Decimal]:
    """Every money-looking number in `text` (digits with decimals or commas)."""
    amounts = []
    for m in _AMOUNT_RE.finditer(text):
        token = m.group("num")
        if "." not in token and "," not in token and not re.search(r"[$]", m.group(0)):
            continue  # bare integers are usually quantities, not money
        value = _money_from_match(m)
        if value is not None:
            amounts.append(value)
    return amounts


def parse_quantity(raw: str | None) -> Decimal | None:
    """Parse a quantity such as '12', '1,200' or '8.5'."""
    if not raw:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return _decimal_from_digits(m.group()) if m else None


# ---- day counts ----------------------------------------------------------


def parse_days(raw: int | str | None) -> int | None:
    """'5 calendar days' -> 5, '-3' -> -3, 'no change' -> 0."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    m = re.search(r"[+-]?\s*\d+", raw)
    if m:
        return int(m.group().replace(" ", ""))
    if re.search(r"\b(no|none|zero|nil)\b", raw, re.IGNORECASE):
        return 0
    return None


# ---- LLM output -> typed ChangeOrder ---------------------------------------


@dataclass
class Normalized:
    change_order: ChangeOrder
    ambiguous: set[str]  # date fields whose day/month order was a guess
    unreadable: dict[str, str]  # field -> the text the model returned but we could not parse


def _clean_references(raw: list[str] | None) -> list[str] | None:
    cleaned = list(dict.fromkeys(r for r in (clean_name(x) for x in raw or []) if r))
    return cleaned or None


def to_change_order(extraction: LLMExtraction, day_first: bool = False) -> Normalized:
    """Normalise every field: ISO dates, 2-place Decimals, ints, trimmed names."""
    ambiguous: set[str] = set()
    unreadable: dict[str, str] = {}

    def convert(name: str, llm: LLMField[Any], parse: Callable[[Any], Any]) -> Field[Any]:
        present = llm.value not in (None, [], "")
        value = parse(llm.value) if present else None
        if present and value is None:
            unreadable[name] = str(llm.value)
        return Field(value=value, evidence=llm.evidence, page=llm.page)

    def date_of(name: str, llm: LLMField[str]) -> Field[date]:
        parsed_ambiguous = False

        def parse(raw: str) -> date | None:
            nonlocal parsed_ambiguous
            parsed = parse_date(raw, day_first)
            parsed_ambiguous = parsed.ambiguous
            return parsed.value

        fld = convert(name, llm, parse)
        if parsed_ambiguous:
            ambiguous.add(name)
        return fld

    def passthrough(value: Any) -> Any:
        return value

    items = [
        LineItem(
            description=clean_name(i.description),
            quantity=parse_quantity(i.quantity),
            unit=clean_name(i.unit),
            unit_price=parse_money(i.unit_price),
            amount=parse_money(i.amount),
            evidence=i.evidence,
            page=i.page,
        )
        for i in extraction.line_items
    ]
    e = extraction
    co = ChangeOrder(
        change_order_number=convert("change_order_number", e.change_order_number, clean_name),
        project_name=convert("project_name", e.project_name, clean_name),
        project_number=convert("project_number", e.project_number, clean_name),
        contractor=convert("contractor", e.contractor, clean_name),
        client=convert("client", e.client, clean_name),
        date_issued=date_of("date_issued", e.date_issued),
        date_approved=date_of("date_approved", e.date_approved),
        description=convert("description", e.description, clean_name),
        reason_category=convert("reason_category", e.reason_category, passthrough),
        line_items=items,
        subtotal=convert("subtotal", e.subtotal, parse_money),
        tax=convert("tax", e.tax, parse_money),
        total_amount=convert("total_amount", e.total_amount, parse_money),
        currency=convert("currency", e.currency, normalize_currency),
        schedule_impact_days=convert("schedule_impact_days", e.schedule_impact_days, parse_days),
        status=convert("status", e.status, passthrough),
        requested_by=convert("requested_by", e.requested_by, clean_name),
        approved_by=convert("approved_by", e.approved_by, clean_name),
        signed=convert("signed", e.signed, passthrough),
        references=convert("references", e.references, _clean_references),
    )
    return Normalized(co, ambiguous, unreadable)
