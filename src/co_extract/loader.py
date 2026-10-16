"""Load a PDF, image, .txt or .eml into per-page text (and page images when needed)."""

from __future__ import annotations

import email
import mimetypes
from dataclasses import dataclass, field
from email import policy
from pathlib import Path

import pymupdf

from co_extract.normalize import collapse_whitespace

MIN_TEXT_CHARS = 50  # fewer characters than this on a page = treat it as scanned
MAX_PAGES = 25  # pages beyond this are dropped and the result is flagged
RENDER_DPI = 150
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
TEXT_SUFFIXES = {".txt", ".eml"}
SUPPORTED_SUFFIXES = {".pdf"} | IMAGE_SUFFIXES | TEXT_SUFFIXES


@dataclass
class Page:
    number: int  # 1-based
    text: str
    image: bytes | None = None  # set when the page has to be read visually
    image_mime: str = "image/png"

    @property
    def scanned(self) -> bool:
        return self.image is not None


@dataclass
class LoadedDocument:
    source_file: str
    pages: list[Page] = field(default_factory=list)
    truncated: bool = False

    @property
    def has_scanned_pages(self) -> bool:
        return any(p.scanned for p in self.pages)

    def page(self, number: int) -> Page | None:
        return next((p for p in self.pages if p.number == number), None)


def _load_pdf(path: Path) -> LoadedDocument:
    doc = LoadedDocument(source_file=path.name)
    with pymupdf.open(path) as pdf:
        doc.truncated = pdf.page_count > MAX_PAGES
        for index in range(min(pdf.page_count, MAX_PAGES)):
            pdf_page = pdf[index]
            text = collapse_whitespace(pdf_page.get_text("text"))
            image = None
            if len(text) < MIN_TEXT_CHARS:
                zoom = RENDER_DPI / 72
                image = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
            doc.pages.append(Page(number=index + 1, text=text, image=image))
    return doc


def _load_image(path: Path) -> LoadedDocument:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    page = Page(number=1, text="", image=path.read_bytes(), image_mime=mime)
    return LoadedDocument(source_file=path.name, pages=[page])


def _load_text(path: Path) -> LoadedDocument:
    if path.suffix.lower() == ".eml":
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = message.get_body(preferencelist=("plain", "html"))
        headers = "\n".join(
            f"{name}: {message[name]}" for name in ("From", "To", "Date", "Subject") if message[name]
        )
        raw = f"{headers}\n\n{body.get_content() if body else ''}"
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    return LoadedDocument(source_file=path.name, pages=[Page(number=1, text=collapse_whitespace(raw))])


def load_document(path: Path) -> LoadedDocument:
    """Load one file. Raises ValueError for unsupported types."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix in IMAGE_SUFFIXES:
        return _load_image(path)
    if suffix in TEXT_SUFFIXES:
        return _load_text(path)
    raise ValueError(f"Unsupported file type: {path.name}")
