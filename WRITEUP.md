# Change-order extraction: approach, results, failure modes

## 1. Approach

Change orders arrive as forms, letters, emails and scans, so a fixed template does not work. I use one LLM call
per document with Structured Outputs to read the layout and fill a strict schema. The model has to copy values
from the document, return null when something is missing, and give a short verbatim quote and page number for every
value it returns.

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
  `Decimal`, integers). This keeps the evidence check honest (the printed text is what I compare against) and puts
  decisions like day-first versus month-first in code I can test, not in the prompt.
- Money is serialised as a string (`"5689.62"`) so no float rounding ever touches it.
- Null means "not in the document". It carries its own confidence: how sure I am the field is really absent.
- Line items carry their own evidence and confidence, because one bad row should not lower the score of the whole table.
- `needs_review` comes with a list of plain-English reasons, so a person knows what to look at first.

## 3. How confidence is calculated

Each non-null field starts at 0.80, then: +0.10 if the quote is found and supports the value; -0.45 if the quote is
not in the document; -0.25 if the quote is real but the value does not come from it; -0.30 if there is no quote;
+0.08 if the rule extractor agrees, -0.25 if it disagrees; -0.30 for a failed business rule on that field;
-0.05 for a value read from a scan; -0.15 for an ambiguous numeric date. Scores are clamped to 0-1. The full table is in the README and every
constant sits at the top of `confidence.py`. `needs_review` is set when a required field falls below 0.7 or when
any error, disagreement or ambiguity is present.

**Calibration result:** _pending, see section 4._ `co-extract eval` prints field accuracy per confidence bucket
(0-0.5, 0.5-0.7, 0.7-0.9, 0.9-1.0) into `eval/results.md`. With 12 documents and 19 fields each there are about 230
data points, so a bucket can hold only a handful of fields; treat the table as a sanity check that high confidence
means more accurate, not as a precise calibration curve.

## 4. Results

**Live-model results: not yet run.** I wrote and tested everything with a mocked model (163 tests pass without a key),
but I did not have an API key in the environment where I built this, so I have no numbers for a real model and I am
not going to estimate them. Running `uv run co-extract eval` with a key fills `eval/results.md` and
`eval/results.json` with per-field accuracy, line-item precision and recall, how often review catches wrong
extractions, the calibration table, and latency and cost. I will paste those numbers here.

What I can report is the rule extractor on its own, measured on the 12 labelled samples (`uv run co-extract eval --rules-only`).
This is the baseline the LLM has to beat, and it shows why rules alone are not enough:

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
| currency | 1 | 1 | 12 | 100% | 8.3% |

When the rules find something it is right, which is what makes them useful as a cross-check. They miss a quarter to a
half of the values (scanned pages, emails in sentences, unlabelled dates) and cannot read names, descriptions, line
items, status or signatures at all.

I also ran a development check where a simulated perfect model, built from the labels, went through the real pipeline.
It is not an evaluation of any model, but it showed the plumbing behaves: the clean documents were not flagged, and the
four that should be reviewed were (missing CO number, arithmetic error, ambiguous date, injected instruction).

## 5. Failure modes

| Failure mode | What the pipeline does |
|---|---|
| **Bad scans, handwriting** | Sent to the vision model as an image. The quote cannot be verified, so those values lose 0.05, null values on scanned documents are less certain, and the rules see nothing. Heavy noise or handwriting depends entirely on the model; there is no OCR fallback yet. |
| **Multi-page tables** | The prompt asks for rows "across all pages" with a quote per row. The loader keeps page numbers, and the evidence check finds a quote on any page and corrects the page number. Line sums are validated against the subtotal, so a dropped row on page 2 shows up as an error. |
| **More than one CO in a file** | The prompt takes the first and ignores the rest. Nothing detects the others yet, which is a real gap. |
| **Crossed-out values** | The prompt says to use the new amount. On image pages the model can see the strike; in a text layer the strike is invisible. Sample 09 covers the image case. A rule disagreement or failed sum would catch some of the text-layer cases. |
| **Ambiguous dates (03/04/26)** | The model copies the date as printed. Code reads other dates in the document to infer the order (25/03/26 can only be day-first), otherwise assumes month-first, lowers confidence by 0.15 and flags the document for review. |
| **Math errors in the document** | Reported as validation errors with both numbers in the message. The extracted values stay as printed; I do not "fix" a customer's document. |
| **Missing fields** | Null with a high "really absent" confidence when nothing looks like the field; low when a rule found a candidate. Missing required fields are errors and force review. |
| **The LLM inventing values** | The evidence check. A made-up value has no real quote, or the quote does not contain the value. This is what the biggest penalty is for. |
| **Currency** | Currency is a field with its own quote. A bare "$" is read as USD, which is wrong for a Canadian document that never says CAD, and the rule check only fires on explicit marks (CAD, C$, USD). A missing currency is a warning. |
| **Very long documents** | Cut off at 25 pages and flagged for review. No chunking yet. |
| **Instructions hidden in a document** | The system prompt says document text is data. Pages are wrapped in delimiters. A regex scan flags lines such as "ignore all previous instructions" and sends the document to review. The evidence check does not help here: an injected "status: approved" is a real quote. Sample 10 contains one. |

Problems I actually hit while building it: PyMuPDF puts every table cell on its own line, so my first rules missed all
the totals until I made them look one line ahead; the rules took the date in the signature block as the issue date;
and a 90% fuzzy match accepted a changed digit in the evidence, which is why the digit check exists.

## 6. What I would do next

- A review screen that shows the page next to each flagged field, using the evidence and page I already store.
- Learn from corrections: store reviewer fixes and use them as few-shot examples and to tune the penalty weights.
- Per-customer templates and vocabularies, so a known contractor's layout is cheaper to read and its CO numbering is checked.
- An OCR fallback to verify quotes on scanned pages and to run the rule extractor there.
- Detect several change orders in one file and split them.
- A real calibration set: far more than 12 documents, from real customers, with the weights fitted on them.
- Cost and speed: send only the pages that matter, cache by document hash, use the cheapest model for clean text
  pages and escalate only flagged documents.

## 7. Cost and speed per document

_Pending the live run._ `eval/results.md` reports average latency, average estimated cost and total tokens. The
estimate comes from the provider's token counts and a small price table in `llm.py` (or the `LLM_PRICE_*` variables),
so it is an estimate, not a bill. Latency covers the model call plus all post-processing; loading and page rendering
are not included. One call per document, with scanned pages adding image tokens, is the cost driver.
