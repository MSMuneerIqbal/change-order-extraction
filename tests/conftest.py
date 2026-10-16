"""Shared helpers: build LLM responses and documents without any API."""

from __future__ import annotations

from typing import Any

from co_extract.llm import LLMResponse
from co_extract.loader import LoadedDocument, Page
from co_extract.schema import LLMExtraction, LLMField, LLMLineItem

SCALARS = [
    "change_order_number", "project_name", "project_number", "contractor", "client", "date_issued",
    "date_approved", "description", "reason_category", "subtotal", "tax", "total_amount", "currency",
    "schedule_impact_days", "status", "requested_by", "approved_by", "signed", "references",
]  # fmt: skip


def f(value: Any = None, evidence: str | None = None, page: int | None = 1) -> LLMField[Any]:
    """An LLM field; evidence/page are dropped when the value is null."""
    if value is None:
        return LLMField.model_construct(value=None, evidence=None, page=None)
    return LLMField.model_construct(value=value, evidence=evidence, page=page)


def extraction(items: list[LLMLineItem] | None = None, **fields: LLMField[Any]) -> LLMExtraction:
    """A full LLMExtraction where every field not given is null."""
    data = {name: fields.get(name, f()) for name in SCALARS}
    return LLMExtraction.model_construct(line_items=items or [], **data)


def item(description: str, qty: str, price: str, amount: str, evidence: str | None = None) -> LLMLineItem:
    return LLMLineItem(
        description=description, quantity=qty, unit="EA", unit_price=price, amount=amount,
        evidence=evidence or f"{description} {amount}", page=1,
    )  # fmt: skip


def text_doc(*page_texts: str, name: str = "doc.txt") -> LoadedDocument:
    pages = [Page(number=i, text=t) for i, t in enumerate(page_texts, start=1)]
    return LoadedDocument(source_file=name, pages=pages)


class FakeExtractor:
    """Stands in for the OpenAI client: returns a canned extraction."""

    model = "gpt-5-mini"

    def __init__(self, result: LLMExtraction, input_tokens: int = 1200, output_tokens: int = 400) -> None:
        self._result = result
        self._usage = (input_tokens, output_tokens)

    def extract(self, doc: LoadedDocument) -> LLMResponse:
        return LLMResponse(self._result, self.model, *self._usage)


GOOD_DOC = """\
CHANGE ORDER
Change Order No.: CO-014
Date Issued: February 10, 2026
Date Approved: February 17, 2026
Project: Maple Street Medical Office Build-out
Description of change
Add a 1-hour fire-rated partition at corridor 2B per RFI-087.
Fire caulking at head of wall
1
LS
$480.00
$480.00
Foreman labour
16
HR
$62.50
$1,000.00
Subtotal
$1,480.00
Sales tax (8.25%)
$122.10
TOTAL CHANGE ORDER AMOUNT
$1,602.10
Contract time adjustment: +3 calendar days
Approved by: Robert Okafor
"""


def good_extraction() -> LLMExtraction:
    """What a correct model would return for GOOD_DOC."""
    return extraction(
        items=[
            item("Fire caulking at head of wall", "1", "$480.00", "$480.00", "Fire caulking at head of wall"),
            item("Foreman labour", "16", "$62.50", "$1,000.00", "Foreman labour"),
        ],
        change_order_number=f("CO-014", "Change Order No.: CO-014"),
        date_issued=f("February 10, 2026", "Date Issued: February 10, 2026"),
        date_approved=f("February 17, 2026", "Date Approved: February 17, 2026"),
        project_name=f("Maple Street Medical Office Build-out", "Project: Maple Street Medical Office Build-out"),
        description=f("Add a 1-hour fire-rated partition at corridor 2B per RFI-087.",
                      "Add a 1-hour fire-rated partition at corridor 2B per RFI-087."),
        subtotal=f("$1,480.00", "Subtotal $1,480.00"),
        tax=f("$122.10", "Sales tax (8.25%) $122.10"),
        total_amount=f("$1,602.10", "TOTAL CHANGE ORDER AMOUNT $1,602.10"),
        currency=f("USD", "$1,602.10"),
        schedule_impact_days=f(3, "Contract time adjustment: +3 calendar days"),
        approved_by=f("Robert Okafor", "Approved by: Robert Okafor"),
    )  # fmt: skip
