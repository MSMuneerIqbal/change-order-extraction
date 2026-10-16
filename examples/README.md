# Example outputs

Real pipeline output from the live run with `gpt-5-mini` (the same files as `eval/outputs/`):

| File | Shows |
|---|---|
| [01_clean_form.json](01_clean_form.json) | a clean one-page form: everything agrees, not flagged (the only warning is the missing currency) |
| [06_total_math_error.json](06_total_math_error.json) | the document's own total is wrong: validation error, total confidence 0.68, flagged |
| [09_crossed_out_amount.json](09_crossed_out_amount.json) | a scanned PNG with a crossed-out amount: the new amount is read, quotes are unverifiable so confidence is lower |

Regenerate with `uv run co-extract extract data/samples/<file> --out examples/` (needs a key in `.env`).
