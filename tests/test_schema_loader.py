"""Schema and file loading."""

import pymupdf
import pytest

from co_extract.loader import MAX_PAGES, load_document
from co_extract.schema import SCALAR_FIELDS, ChangeOrder, Field, LLMExtraction

from conftest import SCALARS


# ---- schema ---------------------------------------------------------------------


def test_every_scalar_field_is_a_field_and_matches_the_llm_schema():
    assert set(SCALAR_FIELDS) == set(SCALARS) == set(LLMExtraction.model_fields) - {"line_items"}
    assert set(Field.model_fields) == {"value", "confidence", "evidence", "page", "method"}
    assert all(issubclass(ChangeOrder.model_fields[name].annotation, Field) for name in SCALAR_FIELDS)


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        Field(value="x", confidence=1.2)


def test_change_order_round_trips_through_json():
    co = ChangeOrder(**{n: Field() for n in SCALAR_FIELDS})
    assert ChangeOrder.model_validate_json(co.model_dump_json()) == co


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
