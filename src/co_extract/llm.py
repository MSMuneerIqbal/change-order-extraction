"""LLM extraction through the OpenAI Agents SDK, with Structured Outputs.

One agent, no tools, one model call per document. The agent's `output_type` is the
`LLMExtraction` schema, so the SDK sends it as a strict JSON schema and validates the
reply. Model, key and base URL come from the environment / .env. Setting
OPENAI_BASE_URL (for example Gemini's OpenAI-compatible endpoint) switches to the
Chat Completions API, because such endpoints do not offer the Responses API.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Protocol

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel, RunConfig, Runner
from agents.exceptions import AgentsException
from agents.models.interface import Model
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError
from openai.types.shared import Reasoning
from pydantic import ValidationError

from co_extract.loader import LoadedDocument
from co_extract.prompts import SYSTEM_PROMPT, USER_INTRO, image_page_label, text_page_block
from co_extract.schema import LLMExtraction

DEFAULT_MODEL = "gpt-5-mini"
OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_REASONING_EFFORT = "low"  # extraction is reading, not puzzle solving; "medium" was ~10x slower
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


def build_input(doc: LoadedDocument) -> list[dict[str, Any]]:
    """One user message: text pages as delimited blocks, scanned pages as images."""
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": USER_INTRO}]
    for page in doc.pages:
        if page.scanned:
            encoded = base64.b64encode(page.image or b"").decode("ascii")
            parts.append({"type": "input_text", "text": image_page_label(page.number)})
            parts.append(
                {"type": "input_image", "image_url": f"data:{page.image_mime};base64,{encoded}", "detail": "high"}
            )
        else:
            parts.append({"type": "input_text", "text": text_page_block(page.number, page.text)})
    return [{"role": "user", "content": parts}]


class AgentsExtractor:
    """Runs a single tool-less Agent whose output type is the extraction schema."""

    def __init__(self, model_name: str, model: Model, reasoning_effort: str | None = None) -> None:
        self.model = model_name
        settings = ModelSettings(reasoning=Reasoning(effort=reasoning_effort)) if reasoning_effort else ModelSettings()
        self._agent = Agent(
            name="change-order-extractor",
            instructions=SYSTEM_PROMPT,
            model=model,
            model_settings=settings,
            output_type=LLMExtraction,
        )

    @classmethod
    def from_env(cls) -> AgentsExtractor:
        """Read LLM_MODEL, OPENAI_API_KEY and OPENAI_BASE_URL (from .env if present)."""
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ExtractionError("OPENAI_API_KEY is not set. Copy .env.example to .env and add your key.")
        custom_url = os.getenv("OPENAI_BASE_URL") or None
        name = os.getenv("LLM_MODEL") or DEFAULT_MODEL
        # Always pass a URL: with None the SDK re-reads OPENAI_BASE_URL, and .env.example leaves it empty.
        client = AsyncOpenAI(api_key=api_key, base_url=custom_url or OPENAI_URL, timeout=REQUEST_TIMEOUT_S)
        model: Model = OpenAIChatCompletionsModel(name, client) if custom_url else OpenAIResponsesModel(name, client)
        # Reasoning models (gpt-5 family) spend most of their time and cost thinking; keep it low.
        # LLM_REASONING_EFFORT overrides; other models get no reasoning setting unless it is set.
        default_effort = DEFAULT_REASONING_EFFORT if name.startswith("gpt-5") and not custom_url else None
        return cls(name, model, os.getenv("LLM_REASONING_EFFORT") or default_effort)

    def extract(self, doc: LoadedDocument) -> LLMResponse:
        try:
            result = Runner.run_sync(
                self._agent,
                build_input(doc),
                max_turns=1,  # no tools: the first reply is the answer
                run_config=RunConfig(tracing_disabled=True),  # customer documents are not sent to trace storage
            )
            extraction = result.final_output_as(LLMExtraction, raise_if_incorrect_type=True)
        except (AgentsException, OpenAIError, ValidationError, TypeError) as exc:
            raise ExtractionError(f"{doc.source_file}: model call failed: {exc}") from exc
        usage = result.context_wrapper.usage
        return LLMResponse(extraction, self.model, usage.input_tokens, usage.output_tokens)
