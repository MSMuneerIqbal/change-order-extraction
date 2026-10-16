# Change-order extraction

Reads messy construction change orders (PDF, scan, image, email text) and returns validated JSON.
Every field carries a confidence score, a verbatim evidence quote and a page number.
Documents that look wrong are flagged `needs_review` with plain-English reasons.

Take-home project for an AI Engineer role at Sledge. Approach, failure modes and results are in [WRITEUP.md](WRITEUP.md).

## Pipeline

```
 PDF / png / jpg / .txt / .eml
            │
   1 load   │  text layer per page; a page with < 50 chars is "scanned" -> page image
   2 clean  │  normalise whitespace, keep page numbers
            ▼
   3 LLM (OpenAI Agents SDK, one call per document, Structured Outputs) ┐
            │    value + verbatim evidence + page per field    │   4 rule extractor
            ▼                                                  │   (regex, no LLM)
   5 normalise  ISO dates · Decimal money · int days · trimmed names
            ▼                                                  │
   6 validate   required fields, sums, dates, status           │
            ▼                                                  │
   7 confidence  base score + evidence check + agreement  ◄───┘
            ▼
   8 JSON per document  {change_order, validation, needs_review, review_reasons, processing}
```

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # installs everything, including test and sample-generation tools
cp .env.example .env         # then put your key in .env
```

`.env` (never committed):

| Variable | Meaning |
|---|---|
| `OPENAI_API_KEY` | API key |
| `LLM_MODEL` | model name, default `gpt-5-mini` (needs Structured Outputs + image input) |
| `OPENAI_BASE_URL` | empty for OpenAI (Responses API); set it to use another OpenAI-compatible endpoint such as Gemini's (Chat Completions API) |
| `REVIEW_THRESHOLD` | required-field confidence below this sets `needs_review` (default 0.7) |

No `uv`? `pip install -r requirements.txt` and run `python -m co_extract.cli ...` with `PYTHONPATH=src`.

## Run

```bash
uv run pytest                                      # 172 tests, no API key needed
uv run python scripts/make_samples.py              # regenerate data/samples + data/labels (deterministic)
uv run co-extract extract data/samples --out out/  # a file or a folder
uv run co-extract eval                             # runs every sample, writes eval/results.md and .json
uv run co-extract eval --rules-only                # rule extractor alone, no key needed
```

## Output

One JSON file per document. This is real output for `data/samples/01_clean_form.pdf`, abridged
(full files in [examples/](examples/)):

```jsonc
{
  "source_file": "01_clean_form.pdf",
  "pages": 1,
  "change_order": {
    "change_order_number": {"value": "CO-014", "confidence": 0.98, "evidence": "Change Order No.: CO-014", "page": 1, "method": "llm+rule"},
    "total_amount":        {"value": "5689.62", "confidence": 0.98, "evidence": "TOTAL CHANGE ORDER AMOUNT
$5,689.62", "page": 1, "method": "llm+rule"},
    "currency":            {"value": null, "confidence": 0.85, "evidence": null, "page": null, "method": "llm"},
    "line_items": [
      {"description": "5/8\" Type X gypsum board, both sides of new partition", "quantity": "640", "unit": "SF",
       "unit_price": "3.85", "amount": "2464.00", "confidence": 0.9, "page": 1}   // evidence omitted here
    ],
    "...": "other fields omitted"
  },
  "validation": {"errors": [], "warnings": [{"code": "missing_currency", "message": "amounts found but no currency", "fields": ["currency"]}]},
  "needs_review": false,
  "review_reasons": [],
  "processing": {"model": "gpt-5-mini", "input_tokens": 2286, "output_tokens": 1710, "latency_ms": 20097, "cost_usd_estimate": 0.003992}
}
```

Money is a string with 2 decimal places (exact, no float rounding), dates are ISO 8601, days are integers
(positive = delay, negative = time saved). `currency` is null here because the document only prints `$`, which
does not say which dollar; that raises a warning instead of a guess.
[examples/06_total_math_error.json](examples/06_total_math_error.json) shows a flagged document, and
[examples/09_crossed_out_amount.json](examples/09_crossed_out_amount.json) a scanned image with a crossed-out amount.

## Results

Live run with `gpt-5-mini` on the 12 synthetic documents (`eval/results.md`, details and caveats in [WRITEUP.md](WRITEUP.md)):

| | |
|---|---|
| Field accuracy | 97.4% (222 of 228); 90.4% in the first run, before I fixed labels and the prompt |
| Line items, precision / recall | 100% / 100% (44 of 44) |
| Flagged `needs_review` | 4 of 12 (the missing number, the arithmetic error, the ambiguous date, the injected instruction) |
| Latency / cost per document | about 17 s / $0.0045 (estimate) |

I tuned the prompt and labels on these same 12 documents, so treat the accuracy as a development-set number, not
a held-out estimate. The 6 remaining wrong fields are judgement calls (`reason_category`, and who the client and
requester are in the email), and the confidence score does not catch them.

## How confidence works

The model's own certainty is never used. Each field starts at 0.80 and is adjusted by independent checks
(all constants are at the top of [src/co_extract/confidence.py](src/co_extract/confidence.py)):

| Check | Effect |
|---|---|
| Evidence quote found in the document (rapidfuzz partial match >= 90, and every number in the quote appears exactly) and the value appears in the quote | +0.10 (+0.05 for enums, booleans and summaries, which the quote cannot verify) |
| Quote not found anywhere in the document | -0.45 |
| Quote found but the value does not come from it | -0.25 |
| No quote for a non-null value | -0.30 |
| Rule extractor found the same value / a different value | +0.08 / -0.25 |
| Field is part of a failed business rule (error / warning) | -0.30 / -0.05 |
| Value read from a scanned page (the quote cannot be checked without a text layer) | -0.05 |
| Numeric date that could be day-first or month-first | -0.15 |

A null value gets "how sure are we it is really absent": 0.85 when a rule looked and found nothing,
0.75 when no rule covers the field, 0.25 when a rule found a candidate the model missed, and 15 points less on scanned documents.

`needs_review` is set when a validation error exists, a required field (`change_order_number`, `description`,
`total_amount`) scores below the threshold, the model and the rules disagree, a numeric date is ambiguous,
the text looks like instructions aimed at an AI, or the document was longer than the page limit.

## Known limits

- The eval set is 12 synthetic documents, and I tuned on them. It shows behaviour on the failure modes I chose; it says little about real-world accuracy.
- Confidence does not catch judgement errors (`reason_category`, who the client is in an email): a real quote does not prove a classification is right.
- With only 6 wrong fields in the eval, the calibration table cannot show whether confidence is well calibrated.
- A bare `$` gives a null currency and a warning, so most documents here carry that warning.
- One change order per file. If a file holds several, the prompt asks for the first and ignores the rest; nothing detects the others.
- Crossed-out values are only visible on image pages. A strike-through drawn over a text layer does not show up in the extracted text.
- Scanned pages have no text layer, so their evidence quotes cannot be verified, and the rule extractor sees nothing there.
- Numeric dates such as `03/04/26` default to month-first unless another date in the document settles it; they are flagged either way.
- Documents longer than 25 pages are cut off and flagged. There is no chunking.
- The rule extractor only knows English labels and North American formats.
- Cost estimates use a small built-in price table (override with `LLM_PRICE_IN_PER_M` / `LLM_PRICE_OUT_PER_M`); check your provider's current prices.
- The model call goes through the OpenAI Agents SDK (a single agent with no tools; tracing is switched off so documents are not sent to trace storage). Unit tests use a fake model; the live path was run for the results above.
- The Gemini OpenAI-compatible endpoint is supported through `OPENAI_BASE_URL` (Chat Completions API) but I have not tested it.

## Layout

```
src/co_extract/   loader · llm · prompts · rules · schema · normalize · validate · confidence · pipeline · evaluate · cli
scripts/          make_samples.py (synthetic documents + labels)
data/samples/     12 documents      data/labels/   ground-truth JSON
examples/         real output files  eval/          results.md / results.json
tests/            mocked-LLM tests
```

MIT licensed. All sample companies, people and projects are invented.
