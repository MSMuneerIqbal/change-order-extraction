"""Pydantic models: what the LLM returns, and what the pipeline outputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel
from pydantic import Field as PydField

T = TypeVar("T")

Method = Literal["llm", "rule", "llm+rule"]


class ReasonCategory(str, Enum):
    design_change = "design_change"
    owner_request = "owner_request"
    unforeseen_condition = "unforeseen_condition"
    code_requirement = "code_requirement"
    error_omission = "error_omission"
    other = "other"


class Status(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    unknown = "unknown"


# --------------------------------------------------------------------------
# What the LLM returns (Structured Outputs). No confidence here: the model's
# own number is not trusted. Dates and money stay as raw strings, exactly as
# printed, so the normaliser (not the model) decides what they mean.
# Every field is required (strict mode); "missing" is expressed as null.
# --------------------------------------------------------------------------


class LLMField(BaseModel, Generic[T]):
    value: T | None
    evidence: str | None
    page: int | None


class LLMLineItem(BaseModel):
    description: str | None
    quantity: str | None
    unit: str | None
    unit_price: str | None
    amount: str | None
    evidence: str | None
    page: int | None


class LLMExtraction(BaseModel):
    change_order_number: LLMField[str]
    project_name: LLMField[str]
    project_number: LLMField[str]
    contractor: LLMField[str]
    client: LLMField[str]
    date_issued: LLMField[str]
    date_approved: LLMField[str]
    description: LLMField[str]
    reason_category: LLMField[ReasonCategory]
    line_items: list[LLMLineItem]
    subtotal: LLMField[str]
    tax: LLMField[str]
    total_amount: LLMField[str]
    currency: LLMField[str]
    schedule_impact_days: LLMField[int]
    status: LLMField[Status]
    requested_by: LLMField[str]
    approved_by: LLMField[str]
    signed: LLMField[bool]
    references: LLMField[list[str]]


# --------------------------------------------------------------------------
# Pipeline output
# --------------------------------------------------------------------------


class Field(BaseModel, Generic[T]):
    """One extracted value with its confidence and provenance."""

    value: T | None = None
    confidence: float = PydField(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None
    page: int | None = None
    method: Method = "llm"


class LineItem(BaseModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    confidence: float = PydField(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None
    page: int | None = None


class ChangeOrder(BaseModel):
    change_order_number: Field[str]
    project_name: Field[str]
    project_number: Field[str]
    contractor: Field[str]
    client: Field[str]
    date_issued: Field[date]
    date_approved: Field[date]
    description: Field[str]
    reason_category: Field[ReasonCategory]
    line_items: list[LineItem] = PydField(default_factory=list)
    subtotal: Field[Decimal]
    tax: Field[Decimal]
    total_amount: Field[Decimal]
    currency: Field[str]
    schedule_impact_days: Field[int]
    status: Field[Status]
    requested_by: Field[str]
    approved_by: Field[str]
    signed: Field[bool]
    references: Field[list[str]]


class Issue(BaseModel):
    """A validation problem. `fields` names the fields to penalise."""

    code: str
    message: str
    fields: list[str] = PydField(default_factory=list)


class Validation(BaseModel):
    errors: list[Issue] = PydField(default_factory=list)
    warnings: list[Issue] = PydField(default_factory=list)


class Processing(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd_estimate: float | None = None


class ExtractionResult(BaseModel):
    source_file: str
    pages: int
    change_order: ChangeOrder
    validation: Validation
    needs_review: bool
    review_reasons: list[str]
    processing: Processing


# Fields that are scalar `Field[...]` members of ChangeOrder, in schema order.
SCALAR_FIELDS: tuple[str, ...] = tuple(
    name
    for name, info in ChangeOrder.model_fields.items()
    if name != "line_items"
)

REQUIRED_FIELDS: tuple[str, ...] = ("change_order_number", "description", "total_amount")
