# Change-order extraction: approach, results, failure modes

## 1. Approach

Change orders arrive as forms, letters, emails and scans, so a fixed template does not work. I make one LLM call per
document, through the OpenAI Agents SDK (one agent, no tools) with Structured Outputs. The model copies values from the
document, returns null when something is missing, and gives a verbatim quote and page number for every value.

I do not trust its output on its own. Three independent checks sit around it:

- **Evidence check.** The quote must exist in the document text (rapidfuzz partial match of 90 or more, plus an exact
  match on every number in the quote, because 90% would accept CO-099 for CO-014). A quote that cannot be found is the
  clearest sign of a made-up value, so it carries the biggest penalty.
- **Rule cross-check.** A regex extractor (no LLM) finds the CO number, project number, labelled dates, subtotal, tax,
  total, schedule days and currency. Agreement boosts a field; disagreement sends the document to review. It is a
  second opinion, never the main extractor.
- **Business rules.** Line items must add up to the subtotal, subtotal plus tax must equal the total, quantity times
  price must equal the amount, approval must not precede issue. Failures lower the fields involved and flag the document.

Scanned pages (under 50 characters of text) go to the vision model as images. There is no OCR, so a quote on a scan
cannot be checked.

## 2. Schema decisions

- Every field is `{value, confidence, evidence, page, method}`. The confidence is the pipeline's, never the model's.
- The model returns dates and money as printed; my code normalises them (ISO dates, 2-place `Decimal`, integers), so
  day-first versus month-first lives in testable code. Money is a string in the JSON, so no float rounding.
- Null means "not in the document" and has its own confidence: how sure I am the field is really absent.
- `currency` is only filled when the document names one. A bare `$` stays null with a warning, not a guess.

## 3. How confidence is calculated

Each non-null field starts at 0.80. Then: +0.10 if the quote is found and supports the value (+0.05 for enums,
booleans and summaries, which a quote cannot verify); -0.45 if the quote is not in the document; -0.25 if it is but
the value does not come from it; -0.30 if there is no quote; +0.08 if the rules agree, -0.25 if they disagree; -0.30
for a failed business rule; -0.05 for a scan; -0.15 for an ambiguous numeric date. Scores are clamped to 0-1 (constants
in `confidence.py`). `needs_review` is set by a required field below 0.7, or any error, disagreement, ambiguity or
suspected injected instruction.

**Calibration (final run):** 0.9-1.0: 99.0% correct (103 of 104 fields); 0.7-0.9: 97.4% (114 of 117); 0.5-0.7: 100%
(7 of 7). That does not show good calibration: only 4 of 228 fields were wrong, so the buckets cannot be told apart.
All 4 scored 0.75 to 0.9. They are judgement calls, and a real quote proves the quote exists, not that the model
classified it as I would.

## 4. Results

`gpt-5-mini`, reasoning effort low, 12 synthetic documents, 228 fields (`uv run co-extract eval`, `eval/results.md`).

| | First run | Final run |
|---|---|---|
| Field accuracy | 90.4% | **98.2%** (224 of 228) |
| Line items, precision / recall | 95.6% / 97.7% | **100% / 100%** (44 of 44) |
| Documents flagged `needs_review` | 6 of 12 | 4 of 12 |

The 4 wrong fields: `reason_category` on two documents (a subjective pick), and `requested_by` and `signed` on the
email (the model took the sender as the requester and an email sign-off as a signature).

**Not a clean test.** After the first run I changed labels, prompt and scoring using the same 12 documents, so the
final number is a development-set figure; `eval/first_run_results.md` is closer to untuned. About half of the jump
(9 of 18 fields) is currency: only `$` is printed in 11 documents, I had labelled them USD, and the model returned
null on 9 and USD on 2. I decided a bare `$` should be null and changed labels and prompt, which is a convention
change, not a better model. The rest came from prompt rules and two email labels the document did not support.
Three runs of the same prompt gave 97.4%, 97.8% and 98.2%, differing by one or two subjective fields, with identical
line items and review decisions. Read that range; the final run matches the committed code and is also the highest.
A separate script recounted the results with the same totals, and all 147 evidence quotes on text pages are exact
substrings of their documents.

**Review.** Samples 05 (missing CO number) and 06 (the document's own arithmetic is wrong) were flagged, as were 08
(ambiguous date) and 10 (injected instruction) where the model happened to be right; those flags warn about risk.
For critical fields plus inconsistent documents, review caught 2 of 2, but only 2 of the 5 documents with any wrong
field, because the misses are judgement fields it cannot see. In the first run it caught a real model error: a
subtotal row listed as a fourth line item.

**Rule extractor alone** (`eval --rules-only`): right every time it finds a value, but it finds only 55% to 86% of
the labelled values on its fields, and cannot read names, line items, status or signatures.

**Unseen documents.** Five new documents I wrote after tuning (a new layout, two change orders in one file, a
non-change-order page, `.eml` and `.jpg`) are too few to prove anything, but exposed three gaps, now fixed with tests:
a table with a missing amount skipped the subtotal check; two change orders in one file were flagged only by accident
(it now says so and lists the numbers); and "two extra working days" scored low because the quote has no digit. The
non-change-order page came back empty and flagged, with nothing invented.

## 5. Failure modes

"Seen" means observed in a live run on this small set, not a guarantee.

| Failure mode | What happens |
|---|---|
| Bad scans, handwriting | Sent as images; quotes unverifiable (-0.05). Seen: two synthetic noisy scans read correctly. Real handwriting untested; no OCR fallback. |
| Multi-page tables | The line-sum check catches a dropped or extra row. Seen: a 14-row, two-page table was complete. |
| Several COs in one file | Only the first is extracted; several "Change Order No." headings are flagged with the numbers. Other numbering is not detected; nothing splits them. |
| Crossed-out values | The prompt says use the new amount. Seen: sample 09 (image) correct. A strike over a text layer is invisible. |
| Ambiguous dates (03/04/26) | Order inferred from other dates, else month-first; confidence drops and the document is flagged. Seen: sample 08. |
| Math errors in the document | Reported as errors; values stay as printed. Seen: sample 06. |
| Missing fields | Null with a "really absent" confidence; missing required fields force review. Seen: sample 05. |
| Invented values | The evidence check. It did not fire live (nothing was fabricated); unit tests cover it. |
| Rows with no amount | Checked against the subtotal using the amounts present; a table with no prices is a scope list. |
| Currency | A bare `$` gives null plus a warning; `CAD` is read correctly (sample 12). |
| Judgement fields | `reason_category`, client or requester in an email. Confidence does not catch them. |
| Very long documents | Cut at 25 pages and flagged. No chunking. |
| Hidden instructions | The prompt says text is data, pages are delimited, and a regex flags "ignore all previous instructions" and sends the document to review. Evidence checks cannot help (an injected quote is real). Seen: sample 10 ignored and flagged. |

Problems I hit: PyMuPDF puts each table cell on its own line, so my first rules missed every total; the rules took a
signature-block date as the issue date; a 90% fuzzy match accepted a changed digit; and an empty `OPENAI_BASE_URL=` in
`.env.example` made every call fail until I always passed an explicit URL.

## 6. What I would do next

- A review screen showing the page next to each flagged field, using the stored evidence and page.
- Learn from corrections: reviewer fixes as few-shot examples; penalty weights fitted on real data.
- Treat judgement fields differently (lower base confidence, or review by rule).
- A real held-out evaluation set of far more than 12 customer documents.
- Per-customer templates, an OCR fallback to verify scan quotes, and splitting files with several change orders.
- Cost and speed: send only the pages that matter, cache by document hash, escalate only flagged documents.

## 7. Cost and speed per document

Final run: 23.4 s and about $0.0046 per document (roughly 2,600 input and 2,000 output tokens); earlier runs took 16
to 17 s and $0.0044 to $0.0045, so latency varies. The cost is an estimate from token counts and a small price table in
`llm.py`, not a bill. Reasoning effort `low` matters: the default took about 45 s per document.
