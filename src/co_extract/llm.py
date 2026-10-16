"""LLM extraction through the OpenAI SDK with Structured Outputs.

Works with any OpenAI-compatible endpoint (set OPENAI_BASE_URL), for example
Gemini's. Model, key and base URL come from the environment / .env.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from co_extract.loader import LoadedDocument
from co_extract.prompts import SYSTEM_PROMPT, USER_INTRO, image_page_label, text_page_block
from co_extract.schema import LLMExtraction

DEFAULT_MODEL = "gpt-5-mini"
REQUEST_TIMEOUT_S = 120

# USD per million tokens (input, output). Estimates: check your provider's price list.
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gemini-2.5-flash": (0.30, 2.50),
}


class ExtractionError(RuntimeError):
    """The model call failed or returned something unusable."""


@dataclass
class LLMResponse:
    extraction: LLMExtraction
    model: str
    input_tokens: int
    output_tokens: int


class Extractor(Protocol):
    """Anything that turns a loaded document into an LLMResponse."""

    model: str

    def extract(self, doc: LoadedDocument) -> LLMResponse: ...


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD estimate, or None when the model's price is unknown."""
    price_in, price_out = os.getenv("LLM_PRICE_IN_PER_M"), os.getenv("LLM_PRICE_OUT_PER_M")
    if price_in and price_out:
        rates = (float(price_in), float(price_out))
    else:
        match = max((k for k in PRICES_PER_M if model.startswith(k)), key=len, default=None)
        if match is None:
            return None
        rates = PRICES_PER_M[match]
    return round((input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000, 6)


def build_messages(doc: LoadedDocument) -> list[dict[str, Any]]:
    """System prompt plus one user message: text pages as blocks, scans as images."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": USER_INTRO}]
    for page in doc.pages:
        if page.scanned:
            encoded = base64.b64encode(page.image or b"").decode("ascii")
            parts.append({"type": "text", "text": image_page_label(page.number)})
            parts.append({"type": "image_url", "image_url": {"url": f"data:{page.image_mime};base64,{encoded}"}})
        else:
            parts.append({"type": "text", "text": text_page_block(page.number, page.text)})
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": parts}]


class OpenAIExtractor:
    """Calls chat.completions.parse() with the LLMExtraction schema."""

    def __init__(self, model: str, client: OpenAI) -> None:
        self.model = model
        self._client = client

    @classmethod
    def from_env(cls) -> OpenAIExtractor:
        """Read LLM_MODEL, OPENAI_API_KEY and OPENAI_BASE_URL (from .env if present)."""
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ExtractionError("OPENAI_API_KEY is not set. Copy .env.example to .env and add your key.")
        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None, timeout=REQUEST_TIMEOUT_S)
        return cls(os.getenv("LLM_MODEL") or DEFAULT_MODEL, client)

    def extract(self, doc: LoadedDocument) -> LLMResponse:
        try:
            completion = self._client.chat.completions.parse(
                model=self.model,
                messages=build_messages(doc),
                response_format=LLMExtraction,
            )
        except (OpenAIError, ValidationError) as exc:
            raise ExtractionError(f"{doc.source_file}: model call failed: {exc}") from exc
        message = completion.choices[0].message
        if message.parsed is None:
            raise ExtractionError(f"{doc.source_file}: model returned no structured output ({message.refusal})")
        usage = completion.usage
        return LLMResponse(
            extraction=message.parsed,
            model=completion.model or self.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )
