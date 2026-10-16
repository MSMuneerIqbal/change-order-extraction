# Example outputs

Real pipeline output files go here. They are produced by a live model run, so they are added after you set
`OPENAI_API_KEY` in `.env`:

```bash
uv run co-extract extract data/samples/01_clean_form.pdf --out examples/
uv run co-extract extract data/samples/06_total_math_error.pdf --out examples/
uv run co-extract extract data/samples/09_crossed_out_amount.png --out examples/
```

`uv run co-extract eval` also writes every sample's output to `eval/outputs/`.
