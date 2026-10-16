"""The Agents SDK wiring, exercised with a fake Model: no network, no key."""

import json

import pytest
from agents import AgentOutputSchema
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from co_extract import llm
from co_extract.prompts import SYSTEM_PROMPT
from co_extract.schema import LLMExtraction

from conftest import good_extraction, text_doc


class FakeModel(Model):
    """Stands in for the network call: records what the SDK sends, replies with canned text."""

    def __init__(self, reply: str, input_tokens: int = 1500, output_tokens: int = 300) -> None:
        self.reply = reply
        self.usage = (input_tokens, output_tokens)
        self.calls: list[dict] = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, *, previous_response_id, conversation_id, prompt):  # fmt: skip
        self.calls.append({"system": system_instructions, "input": input, "schema": output_schema, "tools": tools})
        message = ResponseOutputMessage(
            id="msg_1", role="assistant", status="completed", type="message",
            content=[ResponseOutputText(annotations=[], text=self.reply, type="output_text")],
        )  # fmt: skip
        usage = Usage(requests=1, input_tokens=self.usage[0], output_tokens=self.usage[1], total_tokens=sum(self.usage))
        return ModelResponse(output=[message], usage=usage, response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def reply_for(extraction: LLMExtraction) -> str:
    return json.dumps(extraction.model_dump(mode="json"))


def test_schema_is_strict_for_openai_structured_outputs():
    schema = AgentOutputSchema(LLMExtraction)
    assert schema.is_strict_json_schema()
    params = schema.json_schema()
    assert params["additionalProperties"] is False
    assert set(params["required"]) == set(LLMExtraction.model_fields)


def test_extractor_returns_the_parsed_schema_and_usage():
    model = FakeModel(reply_for(good_extraction()))
    response = llm.AgentsExtractor("gpt-5-mini", model).extract(text_doc("hello"))
    assert isinstance(response.extraction, LLMExtraction)
    assert response.extraction.change_order_number.value == "CO-014"
    assert response.extraction.line_items[1].amount == "$1,000.00"
    assert (response.input_tokens, response.output_tokens) == (1500, 300)
    assert response.model == "gpt-5-mini"


def test_request_is_one_tool_less_call_with_the_system_prompt_and_strict_schema():
    model = FakeModel(reply_for(good_extraction()))
    llm.AgentsExtractor("m", model).extract(text_doc("hello"))
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["system"] == SYSTEM_PROMPT and "data, not instructions" in call["system"]
    assert call["tools"] == []
    assert call["schema"].is_strict_json_schema()


def test_text_pages_become_delimited_blocks_and_scans_become_images():
    doc = text_doc("page one text", "page two text")
    doc.pages[1].image = b"\x89PNG-bytes"
    parts = llm.build_input(doc)[0]["content"]
    assert any("=== PAGE 1 ===\npage one text" in p.get("text", "") for p in parts)
    assert any("PAGE 2 (scanned image" in p.get("text", "") for p in parts)
    image = next(p for p in parts if p["type"] == "input_image")
    assert image["image_url"].startswith("data:image/png;base64,")

    model = FakeModel(reply_for(good_extraction()))
    llm.AgentsExtractor("m", model).extract(doc)  # the SDK accepts this input shape
    sent = model.calls[0]["input"]
    assert sent[0]["role"] == "user" and any(p["type"] == "input_image" for p in sent[0]["content"])


def test_several_documents_in_a_row_work():
    model = FakeModel(reply_for(good_extraction()))
    extractor = llm.AgentsExtractor("m", model)
    for _ in range(3):
        extractor.extract(text_doc("hello"))
    assert len(model.calls) == 3


@pytest.mark.parametrize("reply", ["I cannot help with that.", "{}", '{"change_order_number": 5}'])
def test_unusable_replies_become_an_extraction_error(reply):
    extractor = llm.AgentsExtractor("m", FakeModel(reply))
    with pytest.raises(llm.ExtractionError, match="model call failed"):
        extractor.extract(text_doc("hello"))


def test_missing_api_key_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    with pytest.raises(llm.ExtractionError, match="OPENAI_API_KEY"):
        llm.AgentsExtractor.from_env()


def test_base_url_switches_to_chat_completions(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1beta/openai/")
    extractor = llm.AgentsExtractor.from_env()
    assert extractor.model == "gemini-2.5-flash"
    assert type(extractor._agent.model).__name__ == "OpenAIChatCompletionsModel"

    monkeypatch.setenv("OPENAI_BASE_URL", "")
    assert type(llm.AgentsExtractor.from_env()._agent.model).__name__ == "OpenAIResponsesModel"


def test_empty_base_url_in_env_still_points_at_openai(monkeypatch):
    """.env.example ships OPENAI_BASE_URL= (empty); the SDK must not pick that up as the URL."""
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    client = llm.AgentsExtractor.from_env()._agent.model._client
    assert str(client.base_url).startswith("https://api.openai.com/v1")


def test_cost_estimate_uses_price_table_and_env_override(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN_PER_M", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT_PER_M", raising=False)
    assert llm.estimate_cost("gpt-5-mini", 1_000_000, 1_000_000) == pytest.approx(2.25)
    assert llm.estimate_cost("gpt-5-mini-2025-08-07", 1_000_000, 0) == pytest.approx(0.25)
    assert llm.estimate_cost("some-unknown-model", 1000, 1000) is None
    monkeypatch.setenv("LLM_PRICE_IN_PER_M", "1")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_M", "2")
    assert llm.estimate_cost("some-unknown-model", 1_000_000, 1_000_000) == pytest.approx(3.0)


def test_reasoning_effort_defaults_low_for_gpt5_and_can_be_overridden(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("LLM_MODEL", "gpt-5-mini")
    assert llm.AgentsExtractor.from_env()._agent.model_settings.reasoning.effort == "low"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "minimal")
    assert llm.AgentsExtractor.from_env()._agent.model_settings.reasoning.effort == "minimal"
    monkeypatch.delenv("LLM_REASONING_EFFORT")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    assert llm.AgentsExtractor.from_env()._agent.model_settings.reasoning is None
