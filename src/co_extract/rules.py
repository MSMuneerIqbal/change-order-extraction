"""Rule-based extractor (no LLM). A cross-check on the LLM, never the main source.

It only looks at text layers, so it sees nothing on scanned pages. It covers the
fields where a regex is reliable: IDs, labelled dates, labelled money, day counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from co_extract.loader import LoadedDocument
from co_extract.normalize import (
    DATE_RE,
    alnum,
    find_amounts,
    infer_day_first,
    parse_date,
)

EVIDENCE_MAX_CHARS = 200


@dataclass(frozen=True)
class RuleCandidate:
    value: str | int | Decimal | date
    evidence: str
    page: int
    ambiguous: bool = False


RuleHits = dict[str, RuleCandidate]

_CO_TOKEN = re.compile(r"\b(?P<id>(?:CO|COR|PCO|CCO|CCD)[-\s#]?\d[\w\-./]*)", re.IGNORECASE)
_CO_LABEL = re.compile(
    r"change\s+order(?:\s+request)?\s*(?:no\.?|number|num\.?|#)\s*[:.]?\s*(?P<id>(?=[\w\-./]*\d)[\w\-./]+)",
    re.IGNORECASE,
)
_PROJECT_NO = re.compile(
    r"\b(?:project|job)\s*(?:no\.?|number|num\.?|#)\s*[:.]?\s*(?P<id>(?=[\w\-./]*\d)[\w\-./]+)",
    re.IGNORECASE,
)

_ISSUED_LABELS = (
    r"date\s+issued|issue\s+date|issued(?:\s+on)?|date\s+submitted|submitted(?:\s+on)?|"
    r"date\s+of\s+issue|co\s+date|date"
)
_APPROVED_LABELS = (
    r"date\s+approved|approval\s+date|approved(?:\s+on)?|date\s+of\s+approval|"
    r"date\s+accepted|accepted(?:\s+on)?"
)

_SIGNATURE_LINE = re.compile(r"approv|accept|signed|signature", re.IGNORECASE)

_MONEY_LABELS: dict[str, tuple[str, ...]] = {
    "total_amount": (
        r"total\s+(?:this\s+)?change\s+order(?:\s+amount)?",
        r"change\s+order\s+(?:total|amount)",
        r"net\s+change(?:\s+to\s+contract)?",
        r"grand\s+total",
        r"total\s+(?:amount|price|cost|due)",
        r"total",
    ),
    "subtotal": (r"sub-?\s?total",),
    "tax": (r"sales\s+tax", r"gst", r"hst", r"pst", r"vat", r"tax"),
}
_NOT_THE_CHANGE_TOTAL = re.compile(
    r"revised|new\s+contract|original|previous|prior|adjusted|contract\s+sum|balance", re.IGNORECASE
)

_DAYS = re.compile(r"(?P<n>\d+)\s*(?:calendar|working|business|work)?\s*days?", re.IGNORECASE)
_SCHEDULE_WORDS = re.compile(r"schedule|extension|extend|delay|contract\s+time|calendar\s+days", re.IGNORECASE)
_NOT_SCHEDULE = re.compile(r"payment|net\s*\d+|invoice|within\s+\d+|retainage|lien|notice", re.IGNORECASE)
_REDUCTION_WORDS = re.compile(
    r"\b(?:reduc\w*|decreas\w*|deduct\w*|credit\w*|earlier|saved?|less)\b", re.IGNORECASE
)
_NO_IMPACT = re.compile(
    r"no\s+(?:change|adjustment|impact|extension|additional\s+time|time)\b|"
    r"(?:schedule|time)[^\n]*\b(?:none|n/a|unchanged|no change)\b",
    re.IGNORECASE,
)

_CURRENCY_MARKS = (
    ("CAD", re.compile(r"\bCAD\b|\bC\$|\bCA\$|\bCanadian\b", re.IGNORECASE)),
    ("USD", re.compile(r"\bUSD\b|\bUS\$|\bU\.S\.", re.IGNORECASE)),
)

_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)|disregard\s+(?:the\s+)?(?:above|previous|prior)|"
    r"system\s+prompt|note\s+to\s+(?:the\s+)?(?:ai|assistant|llm|model)|"
    r"(?:^|\n)\s*(?:system|assistant)\s*:|as\s+an\s+ai\b|ai\s+(?:note|instruction)s?\s*:",
    re.IGNORECASE,
)


Line = tuple[int, str]  # (page number, text)


def _lines(doc: LoadedDocument) -> list[Line]:
    """Non-empty lines of every text page. PDF text layers often put a label and
    its value on separate lines, so callers may look one line ahead."""
    return [
        (page.number, line.strip())
        for page in doc.pages
        for line in page.text.splitlines()
        if line.strip()
    ]


def _next_line(lines: list[Line], index: int, page_no: int) -> str:
    """The line after `index` on the same page, or ''."""
    if index + 1 < len(lines) and lines[index + 1][0] == page_no:
        return lines[index + 1][1]
    return ""


def _evidence(line: str) -> str:
    return line[:EVIDENCE_MAX_CHARS]


def _first_co_number(doc: LoadedDocument) -> RuleCandidate | None:
    for page_no, line in _lines(doc):
        m = _CO_LABEL.search(line) or _CO_TOKEN.search(line)
        if m:
            return RuleCandidate(m.group("id").strip(" .,;:"), _evidence(line), page_no)
    return None


def _first_project_number(doc: LoadedDocument) -> RuleCandidate | None:
    for page_no, line in _lines(doc):
        m = _PROJECT_NO.search(line)
        if m:
            return RuleCandidate(m.group("id").strip(" .,;:"), _evidence(line), page_no)
    return None


def _labelled_date(
    doc: LoadedDocument, labels: str, day_first: bool, skip: re.Pattern[str] | None = None
) -> RuleCandidate | None:
    inline = re.compile(rf"\b(?:{labels})\s*[:\-]?\s*(?P<date>{DATE_RE.pattern})", re.IGNORECASE)
    label_only = re.compile(rf"\b(?:{labels})\s*[:\-]?\s*$", re.IGNORECASE)
    lines = _lines(doc)
    for index, (page_no, line) in enumerate(lines):
        if skip and skip.search(line):
            continue
        raw, evidence = None, line
        if m := inline.search(line):
            raw = m.group("date")
        elif label_only.search(line):  # value is on the next line (form cell)
            following = _next_line(lines, index, page_no)
            if found := DATE_RE.match(following):
                raw, evidence = found.group(0), f"{line} {following}"
        parsed = parse_date(raw, day_first)
        if parsed.value:
            return RuleCandidate(parsed.value, _evidence(evidence), page_no, parsed.ambiguous)
    return None


def _labelled_amount(doc: LoadedDocument, field_name: str) -> RuleCandidate | None:
    """Last line (preferred: the final page's total) with a money label and an amount."""
    best: RuleCandidate | None = None
    for label in _MONEY_LABELS[field_name]:
        pattern = re.compile(rf"\b(?:{label})\b(?P<rest>.*)$", re.IGNORECASE)
        lines = _lines(doc)
        for index, (page_no, line) in enumerate(lines):
            if field_name == "total_amount" and (
                _NOT_THE_CHANGE_TOTAL.search(line) or re.search(r"sub-?\s?total", line, re.IGNORECASE)
            ):
                continue
            m = pattern.search(line)
            if not m:
                continue
            amounts = find_amounts(re.sub(r"\d+(?:\.\d+)?\s*%", "", m.group("rest")))
            evidence = line
            if not amounts:  # value is on the next line (table cell)
                following = _next_line(lines, index, page_no)
                amounts = find_amounts(following) if len(following) < 30 else []
                evidence = f"{line} {following}"
            if amounts:
                best = RuleCandidate(amounts[-1], _evidence(evidence), page_no)
        if best:
            return best
    return None


def _schedule_days(doc: LoadedDocument) -> RuleCandidate | None:
    for page_no, line in _lines(doc):
        if _NOT_SCHEDULE.search(line):
            continue
        if _NO_IMPACT.search(line) and re.search(r"time|schedule|days", line, re.IGNORECASE):
            return RuleCandidate(0, _evidence(line), page_no)
        m = _DAYS.search(line)
        if m and _SCHEDULE_WORDS.search(line):
            days = int(m.group("n"))
            return RuleCandidate(-days if _REDUCTION_WORDS.search(line) else days, _evidence(line), page_no)
    return None


def _currency(doc: LoadedDocument) -> RuleCandidate | None:
    for code, pattern in _CURRENCY_MARKS:
        for page_no, line in _lines(doc):
            if pattern.search(line):
                return RuleCandidate(code, _evidence(line), page_no)
    return None


def extract_with_rules(doc: LoadedDocument) -> RuleHits:
    """Run every rule over the document's text layers."""
    full_text = "\n".join(p.text for p in doc.pages)
    day_first = infer_day_first(full_text) or False
    hits: dict[str, RuleCandidate | None] = {
        "change_order_number": _first_co_number(doc),
        "project_number": _first_project_number(doc),
        "date_issued": _labelled_date(doc, _ISSUED_LABELS, day_first, _SIGNATURE_LINE),
        "date_approved": _labelled_date(doc, _APPROVED_LABELS, day_first),
        "subtotal": _labelled_amount(doc, "subtotal"),
        "tax": _labelled_amount(doc, "tax"),
        "total_amount": _labelled_amount(doc, "total_amount"),
        "schedule_impact_days": _schedule_days(doc),
        "currency": _currency(doc),
    }
    return {name: hit for name, hit in hits.items() if hit is not None}


def ids_match(a: str, b: str) -> bool:
    """Compare IDs ignoring punctuation and a leading letter prefix (CO-014 == 014)."""
    strip = lambda s: re.sub(r"^[a-z]+", "", alnum(s))  # noqa: E731
    return alnum(a) == alnum(b) or (bool(strip(a)) and strip(a) == strip(b))


_OTHER_CO_CONTEXT = re.compile(r"previous|prior|supersede|revises|replaces|reference|\bref\b", re.IGNORECASE)


def find_change_order_numbers(doc: LoadedDocument) -> list[str]:
    """Distinct numbers printed as 'Change Order No. X' headings, skipping lines that only
    refer to another change order. More than one means several change orders in one file."""
    numbers: list[str] = []
    for _, line in _lines(doc):
        m = _CO_LABEL.search(line)
        if m and not _OTHER_CO_CONTEXT.search(line):
            number = m.group("id").strip(" .,;:")
            if not any(ids_match(number, seen) for seen in numbers):
                numbers.append(number)
    return numbers


def find_injection_phrases(doc: LoadedDocument) -> list[str]:
    """Lines that look like instructions aimed at an AI. Documents are data only."""
    return [_evidence(line) for _, line in _lines(doc) if _INJECTION.search(line)]
