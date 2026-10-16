# Evaluation results

Model: `gpt-5-mini`  |  Documents: 12  |  Overall field accuracy: **97.4%**

## Per-field accuracy

| Field | Correct | Accuracy |
|---|---|---|
| change_order_number | 12/12 | 100.0% |
| project_name | 12/12 | 100.0% |
| project_number | 12/12 | 100.0% |
| contractor | 12/12 | 100.0% |
| client | 11/12 | 91.7% |
| date_issued | 12/12 | 100.0% |
| date_approved | 12/12 | 100.0% |
| description | 12/12 | 100.0% |
| reason_category | 9/12 | 75.0% |
| subtotal | 12/12 | 100.0% |
| tax | 12/12 | 100.0% |
| total_amount | 12/12 | 100.0% |
| currency | 12/12 | 100.0% |
| schedule_impact_days | 12/12 | 100.0% |
| status | 12/12 | 100.0% |
| requested_by | 11/12 | 91.7% |
| approved_by | 12/12 | 100.0% |
| signed | 11/12 | 91.7% |
| references | 12/12 | 100.0% |

## Line items

Precision 100.0% (44/44), recall 100.0% (44/44).

## Review

- Flagged for review: 4/12 (33.3%)
- Documents that should be reviewed (wrong extraction, or the document itself is inconsistent): 6
- Caught by review: 2 (33.3%)
- Counting only critical fields (CO number, amounts, dates, status, schedule days, line items) or a flawed document: 2 should be reviewed, 2 caught (100.0%); missed: none
- Flagged but extraction was correct and document consistent: 08_ambiguous_dates.pdf, 10_pending_unsigned_injection.pdf
- Missed (wrong but not flagged): 01_clean_form.pdf, 03_email.txt, 04_scanned_rotated.pdf, 09_crossed_out_amount.png

## Calibration (field accuracy by confidence bucket)

| Confidence | Fields | Correct | Accuracy |
|---|---|---|---|
| 0.0-0.5 | 0 | 0 | n/a |
| 0.5-0.7 | 9 | 9 | 100.0% |
| 0.7-0.9 | 115 | 111 | 96.5% |
| 0.9-1.0 | 104 | 102 | 98.1% |

## Cost and speed

- Average latency per document: 16684 ms
- Average estimated cost per document: $0.00445
- Tokens: 30900 in, 22805 out

## Per document

| File | Flagged | Should flag | Wrong fields |
|---|---|---|---|
| 01_clean_form.pdf | False | True | reason_category |
| 02_two_page_table.pdf | False | False | - |
| 03_email.txt | False | True | client, requested_by, signed |
| 04_scanned_rotated.pdf | False | True | reason_category |
| 05_missing_number_and_date.pdf | True | True | - |
| 06_total_math_error.pdf | True | True | - |
| 07_credit_deduction.pdf | False | False | - |
| 08_ambiguous_dates.pdf | True | False | - |
| 09_crossed_out_amount.png | False | True | reason_category |
| 10_pending_unsigned_injection.pdf | True | False | - |
| 11_revised_references_earlier.pdf | False | False | - |
| 12_cad_currency.pdf | False | False | - |
