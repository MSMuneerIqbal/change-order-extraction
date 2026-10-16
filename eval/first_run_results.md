# Evaluation results

Model: `gpt-5-mini`  |  Documents: 12  |  Overall field accuracy: **90.4%**

## Per-field accuracy

| Field | Correct | Accuracy |
|---|---|---|
| change_order_number | 11/12 | 91.7% |
| project_name | 9/12 | 75.0% |
| project_number | 12/12 | 100.0% |
| contractor | 12/12 | 100.0% |
| client | 11/12 | 91.7% |
| date_issued | 12/12 | 100.0% |
| date_approved | 12/12 | 100.0% |
| description | 11/12 | 91.7% |
| reason_category | 9/12 | 75.0% |
| subtotal | 12/12 | 100.0% |
| tax | 12/12 | 100.0% |
| total_amount | 12/12 | 100.0% |
| currency | 3/12 | 25.0% |
| schedule_impact_days | 12/12 | 100.0% |
| status | 12/12 | 100.0% |
| requested_by | 11/12 | 91.7% |
| approved_by | 12/12 | 100.0% |
| signed | 11/12 | 91.7% |
| references | 10/12 | 83.3% |

## Line items

Precision 95.6% (43/45), recall 97.7% (43/44).

## Review

- Flagged for review: 6/12 (50.0%)
- Documents that should be reviewed (wrong extraction, or the document itself is inconsistent): 12
- Caught by review: 6 (50.0%)
- Flagged but extraction was correct and document consistent: none
- Missed (wrong but not flagged): 01_clean_form.pdf, 02_two_page_table.pdf, 04_scanned_rotated.pdf, 07_credit_deduction.pdf, 09_crossed_out_amount.png, 11_revised_references_earlier.pdf

## Calibration (field accuracy by confidence bucket)

| Confidence | Fields | Correct | Accuracy |
|---|---|---|---|
| 0.0-0.5 | 1 | 1 | 100.0% |
| 0.5-0.7 | 11 | 11 | 100.0% |
| 0.7-0.9 | 74 | 63 | 85.1% |
| 0.9-1.0 | 142 | 131 | 92.3% |

## Cost and speed

- Average latency per document: 18292 ms
- Average estimated cost per document: $0.00457
- Tokens: 29400 in, 23769 out

## Per document

| File | Flagged | Should flag | Wrong fields |
|---|---|---|---|
| 01_clean_form.pdf | False | True | reason_category |
| 02_two_page_table.pdf | False | True | project_name, references |
| 03_email.txt | True | True | change_order_number, client, description, currency, requested_by, signed |
| 04_scanned_rotated.pdf | False | True | reason_category, currency |
| 05_missing_number_and_date.pdf | True | True | project_name, currency |
| 06_total_math_error.pdf | True | True | currency |
| 07_credit_deduction.pdf | False | True | currency |
| 08_ambiguous_dates.pdf | True | True | currency |
| 09_crossed_out_amount.png | False | True | reason_category, currency |
| 10_pending_unsigned_injection.pdf | True | True | currency |
| 11_revised_references_earlier.pdf | False | True | project_name, currency, references |
| 12_cad_currency.pdf | True | True | - |
