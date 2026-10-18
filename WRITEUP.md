# Change-order extraction: approach, results, failure modes

## 1. Approach

Change orders arrive as forms, letters, emails and scans, so a fixed template does not work. I use one LLM call
per document, through the OpenAI Agents SDK (one agent, no tools) with Structured Outputs, to read the layout and
fill a strict schema. The model has to copy values from the document, return null when something is missing, and
give a short verbatim quote and page number for every value it returns.

I do not trust the model's output on its own. Three independent checks sit around it:

- **Evidence check.** The quote must exist in the document text (rapidfuzz partial match of 90 or more, plus an exact
  match on every number in the quote, because a 90% match would accept CO-099 for CO-014). A quote that cannot be found
  is the clearest sign of a made-up value, so it carries the biggest penalty.
- **Rule cross-check.** A small regex extractor (no LLM) independently finds the CO number, project number, labelled
  dates, subtotal, tax, total, schedule days and currency. If it agrees with the model the field gets a boost; if it
  disagrees, or finds something the model said was absent, the document goes to review. It is a second opinion, never
  the main extractor.
- **Business rules.** Line items must add up to the subtotal, subtotal plus tax must equal the total, quantity times
  price must equal the amount, approval must not come before issue, and so on. Failures lower the confidence of the
  fields involved and flag the document.

Scanned pages (under 50 characters of text) are rendered to an image and sent to the vision model. There is no OCR
dependency, which keeps setup simple but means the quote on a scanned page cannot be checked.

## 2. Schema decisions

- Every field is `{value, confidence, evidence, page, method}`. A reviewer can see where each value came from, and
  the confidence is the pipeline's, not the model's.
- The model returns dates and money as raw strings, exactly as printed. My code normalises them (ISO dates, 2-place
  `Decimal`, integers). This keeps the evidence check honest and puts decisions like day-first versus month-first in
  code I can test, not in the prompt. Money is serialised as a string (`"5689.62"`) so no float rounding touches it.
- Null means "not in the document", and carries its own confidence: how sure I am the field is really absent.
- `currency` is only filled when the document names one. A bare `$` does not say USD or CAD, so it stays null and
  raises a warning rather than a silent guess.
- Line items carry their own evidence and confidence, so one bad row does not lower the score of the whole table.
- `needs_review` comes with plain-English reasons, so a person knows what to look at first.

## 3. How confidence is calculated

Each non-null field starts at 0.80, then: +0.10 if the quote is found and supports the value (+0.05 when the value is
an enum, boolean or summary that cannot be checked against the quote); -0.45 if the quote is not in the document;
-0.25 if the quote is real but the value does not come from it; -0.30 if there is no quote; +0.08 if the rule extractor
agrees, -0.25 if it disagrees; -0.30 for a failed business rule on that field; -0.05 for a value read from a scan;
-0.15 for an ambiguous numeric date. Scores are clamped to 0-1. The constants sit at the top of `confidence.py`.
`needs_review` is set when a required field falls below 0.7 or when any error, disagreement, ambiguity or
suspected injected instruction is present.

**Calibration result (final run):** 0.9-1.0: 99.0% correct (103 of 104 fields); 0.7-0.9: 97.4% (114 of 117); 0.5-0.7:
100% (7 of 7); below 0.5: no fields. This is not evidence that the scores are well calibrated. Only 4 of 228 fields
were wrong, so the buckets cannot be told apart, and all 4 wrong fields scored 0.75 to 0.9. They are judgement calls
(see section 4), and the score does not catch those: a real quote proves the quote exists, not that the model
classified it the way I would. What the 0.5-0.7 bucket does show is the design working: those are correctly
extracted values in sample 06 whose own document does not add up, so the pipeline lowered them on purpose to send
the document to review.

## 4. Results

`gpt-5-mini` (reasoning effort low), 12 synthetic documents, 228 fields. Everything below is from `uv run co-extract eval`
(`eval/results.md`).

| | Run 1 | Run 2 | Final run |
|---|---|---|---|
| Field accuracy | 90.4% | 97.4% (222 of 228) | **98.2%** (224 of 228) |
| Line items, precision / recall | 95.6% / 97.7% | 100% / 100% | **100% / 100%** (44 of 44) |
| Documents flagged `needs_review` | 6 of 12 | 4 of 12 | 4 of 12 |
| Average latency per document | 18.3 s | 16.7 s | 23.4 s |
| Average estimated cost per document | $0.00457 | $0.00445 | $0.00462 |

Final-run fields below 100%: `reason_category` 10 of 12 (a subjective pick; the model and I disagreed on two), and
`requested_by` and `signed` 11 of 12 each, both from the email (sample 03): the model named the sender as the
requester and read the email sign-off as a signature. The other 16 fields were 12 of 12.

**Repeatability.** The same prompt was run three times on the same documents: 97.4%, 97.8% and 98.2%. The differences
came from one or two subjective fields flipping (`reason_category`, and the email's `client`). Line items and every
review decision were identical across the runs. So a difference of a field or two between runs is noise. I report the
final run because it is the one that matches the committed code (between run 2 and the final run I changed validation,
scoring and review messages after the unseen-document check below, not the prompt); it also happens to be the highest
of the three, so read the range, 97.4% to 98.2%, not the top number. A separate script, which does not use my
evaluator, recounted the run 2 and final results with the same totals, and all 147 evidence quotes on text pages are
exact substrings of their documents.

**How run 2 differs from run 1, and why the numbers are not a clean test.** After run 1 I corrected the labels and
changed the prompt using those same 12 documents, so run 2 is a development-set number, not a held-out estimate. Run 1 is the
closer thing to an untuned result, and is kept in `eval/first_run_results.md`. The changes:

- *Labels.* In 11 documents only `$` is printed, and I had labelled the currency USD. The model returned null on 9 and
  USD on 2, which was inconsistent. I decided a bare `$` should give null, set those labels to null and told the model
  so. That fix is a convention change, not a model improvement, and it explains most of the jump (currency went from
  3 to 12 of 12). I also fixed two labels in sample 03 that the document did not support (a client named only in an
  email domain, and a description I had paraphrased).
- *Prompt.* Identifiers only (no "Change Order No." or "Subcontract"), project names without the job number, no
  subtotal row as a line item, quote joined across lines for table cells, and `reason_category` is a classification.
- *Scoring.* Half the evidence bonus for values the quote cannot verify, after `reason_category` was wrong at 0.9.
- *Cost.* Reasoning effort `low`, after the default took 45 s per document.

**Review behaviour.** Both flagged-but-correct documents were flagged on purpose: 08 (an ambiguous `03/04/26` date) and
10 (an injected instruction). The model extracted both correctly and ignored the injected text, but review is a warning
about risk, not a verdict on one answer. Treating every flag as a false alarm if the answer was right would be the
wrong lesson. Review missed 3 documents with a wrong field (01, 03 and 09), all judgement fields (`reason_category`,
the email), so it caught 2 of the 5 documents that should be reviewed for any wrong field. For critical fields (CO
number, amounts, dates, status, schedule days, line items) plus documents that are themselves inconsistent, it caught 2
of 2 (samples 05 and 06); the model made no critical error in the final run. In run 1, the one real model error on a
critical field, a subtotal row counted as a fourth line item in sample 12, was caught by the line-sum check ("line
items add up to 49240.00, document says 24620.00").

**Unseen documents.** After the tuning I tried five documents I wrote that were not in the 12 and not used for tuning:
a new layout (PCO number, `17.03.2026` dates, C$ amounts, GST), a file with two change orders, a safety-meeting
document that is not a change order, and the email and the crossed-out scan as `.eml` and `.jpg`. Five documents prove
little, but they found three real gaps, now fixed with tests:

- *Line rows without an amount.* In the new layout two table rows came back with a unit price and no amount (the
  column positions are lost in the text layer). My line-sum check skipped any table with a missing amount, so nothing
  was flagged. It now checks the amounts that are present, so the document is flagged. On the re-run the model
  happened to fill in all three amounts, so I verified this fix on the earlier output and with a unit test, not on a
  live failure.
- *Two change orders in one file.* The model extracted the first, and the document was flagged, but by accident: the
  rule extractor took the last page's total and disagreed. It now says "file contains more than one change order
  (CO-5, CO-6); only the first is extracted".
- *Numbers in words.* "We would need two extra working days" scored 0.55 for a correct 2 because the quote has no
  digit. It now scores 0.9.

The non-change-order document came back empty and flagged, with no invented values. The `.eml` and `.jpg` inputs worked.

**Rule extractor on its own** (`uv run co-extract eval --rules-only`, no model): where it finds a value it is
right every time, but it misses 15% to 46% of the labelled values (scans, emails in sentences, unlabelled dates) and cannot
read names, descriptions, line items, status or signatures at all.

| Field | Found | Correct | Labelled | Precision | Recall |
|---|---|---|---|---|---|
| change_order_number | 9 | 9 | 11 | 100% | 81.8% |
| date_issued | 6 | 6 | 11 | 100% | 54.5% |
| date_approved | 6 | 6 | 7 | 100% | 85.7% |
| project_number | 6 | 6 | 11 | 100% | 54.5% |
| subtotal | 5 | 5 | 6 | 100% | 83.3% |
| tax | 3 | 3 | 4 | 100% | 75.0% |
| total_amount | 9 | 9 | 12 | 100% | 75.0% |
| schedule_impact_days | 7 | 7 | 10 | 100% | 70.0% |
| currency | 1 | 1 | 1 | 100% | 100% |

## 5. Failure modes

Rows marked "seen" were observed in a live run on this set; with one model and 12 synthetic documents that is an
observation, not a guarantee.

| Failure mode | What the pipeline does |
|---|---|
| **Bad scans, handwriting** | Sent to the vision model as an image. The quote cannot be verified, so those values lose 0.05 and null values are less certain. Seen: the rotated, noisy scan (04) and the photo-style PNG (09) were read correctly, but these are synthetic scans. Real handwriting is untested and there is no OCR fallback. |
| **Multi-page tables** | The prompt asks for rows across all pages; the loader keeps page numbers; the line-sum check catches a dropped or extra row. Seen: the 14-row, two-page table (02) came back complete. |
| **More than one CO in a file** | The prompt takes the first and ignores the rest. Several "Change Order No." headings are detected and the document is flagged with the numbers listed. Seen on an unseen two-change-order file. A file that numbers its change orders differently is not detected, and nothing splits them. |
| **Crossed-out values** | The prompt says to use the new amount; the model can see a strike only on an image page. Seen: sample 09 (struck `$18,500.00` rewritten as `$16,900.00`) was correct, line item and total. A strike over a text layer is invisible in extracted text. |
| **Ambiguous dates (03/04/26)** | The model copies the date as printed. Code infers the order from other dates in the document (25/03/26 can only be day-first), otherwise assumes month-first, lowers confidence and flags the document. Seen: sample 08 flagged. |
| **Table rows with no amount** | Checked against the subtotal using the amounts that are present, so a missing amount shows up as a mismatch. A table with no prices at all is treated as a scope list. Seen on an unseen layout (verified on the earlier output and by test). |
| **Math errors in the document** | Reported as validation errors with both numbers. Extracted values stay as printed. Seen: sample 06 flagged, total confidence 0.68. |
| **Missing fields** | Null with a high "really absent" confidence when nothing looks like the field; low when a rule found a candidate. Missing required fields are errors and force review. Seen: sample 05 flagged for the missing CO number. |
| **The LLM inventing values** | The evidence check. It did not fire on this set because the model did not fabricate anything here. It is covered by unit tests with a made-up quote, not by a live failure. |
| **Model reads the table wrong** | Seen in run 1: the model listed the "Subtotal" row as a line item. The line-sum check caught it, and a prompt rule fixed it. |
| **Currency** | A bare `$` gives null and a `missing_currency` warning, not a guess. This warns on most documents here. A document that prints `CAD` or "Canadian dollars" is read correctly (sample 12). |
| **Judgement fields** | Seen: `reason_category`, and in the email which person is the client or requester and whether a sign-off is a signature. Confidence does not catch these (all wrong at 0.75 to 0.9). They need a human or a stricter definition. |
| **Very long documents** | Cut off at 25 pages and flagged for review. No chunking yet. |
| **Instructions hidden in a document** | The system prompt says document text is data, pages are wrapped in delimiters, and a regex flags lines like "ignore all previous instructions" and sends the document to review. The evidence check does not help here, since an injected "status: approved" is a real quote. Seen: sample 10 had one; the model ignored it and the document was flagged. |

Problems I hit while building it: PyMuPDF puts every table cell on its own line, so my first rules missed all the totals
until they looked one line ahead; the rules took the signature-block date as the issue date; a 90% fuzzy match accepted
a changed digit; and an empty `OPENAI_BASE_URL=` in `.env.example` made every call fail until I always passed an explicit URL.

## 6. What I would do next

- A review screen that shows the page next to each flagged field, using the evidence and page I already store.
- Learn from corrections: store reviewer fixes, use them as few-shot examples, and fit the penalty weights on real data.
- Treat judgement fields differently: lower base confidence, or send them for review by rule.
- A real evaluation set: far more than 12 documents, from real customers, held out from prompt tuning.
- Per-customer templates and vocabularies, so a known contractor's layout is cheaper to read and its CO numbering is checked.
- An OCR fallback to verify quotes on scanned pages and run the rule extractor there.
- Split a file with several change orders into separate extractions (detection and flagging exist, splitting does not).
- Cost and speed: send only the pages that matter, cache by document hash, use the cheapest model for clean text
  pages and escalate only flagged documents.

## 7. Cost and speed per document

Final run: 23.4 s and about $0.0046 per document on average (roughly 2,600 input and 2,000 output tokens). Total for all
12 documents: 30,900 input and 23,834 output tokens. The earlier runs were 16 to 17 s and $0.0044 to $0.0045, so expect
latency to vary with API and machine load. Cost is an estimate from the provider's token counts and a small price table
in `llm.py` (override with `LLM_PRICE_*`), not a bill. Latency is the model call plus post-processing; loading and page
rendering are not included. Most output tokens are the quote and field JSON, and `gpt-5-mini` at default reasoning
effort took about 45 s per document, so the `low` setting matters. Scanned pages add image tokens. The first command
that calls the model also imports the OpenAI SDK, which can take a while on a cold machine; `--help` and `--rules-only`
do not.
