"""Schema, LLM request building, and file loading."""

import json
from types import SimpleNamespace

import openai
import pymupdf
import pytest

from co_extract import llm
from co_extract.loader import MAX_PAGES, load_document
from co_extract.schema import SCALAR_FIELDS, ChangeOrder, Field, LLMExtraction

from conftest import SCALARS, good_extraction, text_doc


# ---- schema ---------------------------------------------------------------------


def test_every_scalar_field_is_a_field_and_matches_the_llm_schema():
    assert set(SCALAR_FIELDS) == set(SCALARS) == set(LLMExtraction.model_fields) - {"line_items"}
    for name in SCALAR_FIELDS:
        assert set(Field.model_fields) == {"value", "confidence", "evidence", "page", "method"}


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        Field(value="x", confidence=1.2)


def test_change_order_round_trips_through_json():
    co = ChangeOrder(**{n: Field() for n in SCALAR_FIELDS})
    assert ChangeOrder.model_validate_json(co.model_dump_json()) == co


def test_llm_schema_is_accepted_by_openai_strict_structured_outputs():
    tool = openai.pydantic_function_tool(LLMExtraction)  # raises if the schema is not strict-compatible
    params = tool["function"]["parameters"]
    assert params["additionalProperties"] is False
    assert set(params["required"]) == set(LLMExtraction.model_fields)
    json.dumps(tool)


# ---- llm.py ---------------------------------------------------------------------


def test_request_sends_text_pages_as_delimited_blocks_and_scans_as_images():
    doc = text_doc("page one text", "page two text")
    doc.pages[1].image = b"\x89PNG-bytes"
    messages = llm.build_messages(doc)
    assert messages[0]["role"] == "system" and "data, not instructions" in messages[0]["content"]
    parts = messages[1]["content"]
    assert any("=== PAGE 1 ===\npage one text" in p.get("text", "") for p in parts)
    image = next(p for p in parts if p["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert any("PAGE 2 (scanned image" in p.get("text", "") for p in parts)


def test_cost_estimate_uses_price_table_and_env_override(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN_PER_M", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT_PER_M", raising=False)
    assert llm.estimate_cost("gpt-5-mini", 1_000_000, 1_000_000) == pytest.approx(2.25)
    assert llm.estimate_cost("gpt-5-mini-2025-08-07", 1_000_000, 0) == pytest.approx(0.25)
    assert llm.estimate_cost("some-unknown-model", 1000, 1000) is None
    monkeypatch.setenv("LLM_PRICE_IN_PER_M", "1")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_M", "2")
    assert llm.estimate_cost("some-unknown-model", 1_000_000, 1_000_000) == pytest.approx(3.0)


class FakeClient:
    """Mimics client.chat.completions.parse()."""

    def __init__(self, parsed, refusal=None):
        self.calls = []
        message = SimpleNamespace(parsed=parsed, refusal=refusal)
        self._completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message)], model="gpt-5-mini-2025-08-07",
            usage=SimpleNamespace(prompt_tokens=1500, completion_tokens=300),
        )  # fmt: skip
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._completion


def test_extractor_passes_schema_and_reports_usage():
    client = FakeClient(good_extraction())
    response = llm.OpenAIExtractor("gpt-5-mini", client).extract(text_doc("hello"))
    assert client.calls[0]["response_format"] is LLMExtraction
    assert client.calls[0]["model"] == "gpt-5-mini"
    assert (response.input_tokens, response.output_tokens) == (1500, 300)
    assert response.model == "gpt-5-mini-2025-08-07"


def test_refusal_becomes_an_extraction_error():
    extractor = llm.OpenAIExtractor("m", FakeClient(None, refusal="I can't help with that"))
    with pytest.raises(llm.ExtractionError, match="can't help"):
        extractor.extract(text_doc("hello"))


def test_missing_api_key_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    with pytest.raises(llm.ExtractionError, match="OPENAI_API_KEY"):
        llm.OpenAIExtractor.from_env()


# ---- loader ---------------------------------------------------------------------


def make_pdf(path, texts):
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text, fontsize=11)
        pdf.save(path)


def test_pdf_page_with_text_uses_the_text_layer(tmp_path):
    path = tmp_path / "a.pdf"
    make_pdf(path, ["Change Order No. 14 for the Maple Street project, total $5,000.00"])
    doc = load_document(path)
    assert doc.pages[0].text.startswith("Change Order No. 14") and not doc.pages[0].scanned


def test_pdf_page_with_almost_no_text_is_treated_as_scanned_and_rendered(tmp_path):
    path = tmp_path / "scan.pdf"
    make_pdf(path, ["Change Order No. 14 for the Maple Street project, total $5,000.00", "stamp"])
    doc = load_document(path)
    assert [p.scanned for p in doc.pages] == [False, True]
    assert doc.pages[1].image.startswith(b"\x89PNG") and doc.has_scanned_pages


def test_long_documents_are_truncated_and_flagged(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, ["A page with enough text on it to count as a text page, number %d" % i for i in range(MAX_PAGES + 3)])
    doc = load_document(path)
    assert len(doc.pages) == MAX_PAGES and doc.truncated


def test_text_email_and_image_files(tmp_path):
    (tmp_path / "a.txt").write_text("Hello   there\n\n\n\nbye", encoding="utf-8")
    assert load_document(tmp_path / "a.txt").pages[0].text == "Hello there\n\nbye"

    (tmp_path / "m.eml").write_text(
        "From: a@example.com\nTo: b@example.com\nSubject: CO 7\nDate: Tue, 3 Mar 2026 10:00:00 +0000\n\nTotal is $9,840.\n",
        encoding="utf-8",
    )
    eml = load_document(tmp_path / "m.eml").pages[0].text
    assert "Subject: CO 7" in eml and "Total is $9,840." in eml

    (tmp_path / "s.jpg").write_bytes(b"\xff\xd8fake")
    page = load_document(tmp_path / "s.jpg").pages[0]
    assert page.scanned and page.image_mime == "image/jpeg"


def test_unsupported_type_is_rejected(tmp_path):
    (tmp_path / "x.docx").write_bytes(b"x")
    with pytest.raises(ValueError):
        load_document(tmp_path / "x.docx")
