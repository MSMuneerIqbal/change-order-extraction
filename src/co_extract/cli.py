"""Command line: `co-extract extract <file|folder> --out out/` and `co-extract eval`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from co_extract.evaluate import load_cases, render_markdown, run_eval, run_rules_baseline
from co_extract.llm import AgentsExtractor, ExtractionError
from co_extract.loader import SUPPORTED_SUFFIXES
from co_extract.pipeline import DEFAULT_REVIEW_THRESHOLD, process_document


def _threshold(arg: float | None) -> float:
    load_dotenv()
    return arg if arg is not None else float(os.getenv("REVIEW_THRESHOLD") or DEFAULT_REVIEW_THRESHOLD)


def _files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    return [target]


def cmd_extract(args: argparse.Namespace) -> int:
    extractor = AgentsExtractor.from_env()
    threshold = _threshold(args.threshold)
    args.out.mkdir(parents=True, exist_ok=True)
    failures = 0
    for path in _files(args.target):
        try:
            result = process_document(path, extractor, threshold)
        except (ExtractionError, ValueError) as exc:
            print(f"FAIL  {path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        (args.out / f"{path.stem}.json").write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        flag = "REVIEW" if result.needs_review else "ok    "
        print(f"{flag} {path.name}  ({len(result.review_reasons)} reasons)")
    return 1 if failures else 0


def cmd_eval(args: argparse.Namespace) -> int:
    cases = load_cases(args.samples, args.labels)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.rules_only:
        report = run_rules_baseline(cases, args.out)
        print(f"Wrote {args.out / 'rules_baseline.md'} ({len(report)} fields)")
        return 0
    extractor = AgentsExtractor.from_env()
    report = run_eval(cases, extractor, args.out, _threshold(args.threshold))
    print(render_markdown(report))
    print(f"Wrote {args.out / 'results.md'} and {args.out / 'results.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="co-extract", description="Change-order extraction pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="extract one file or every file in a folder")
    extract.add_argument("target", type=Path, help="PDF, image, .txt/.eml, or a folder of them")
    extract.add_argument("--out", type=Path, default=Path("out"), help="output folder (default: out/)")
    extract.add_argument("--threshold", type=float, help="review threshold (default 0.7 or REVIEW_THRESHOLD)")
    extract.set_defaults(func=cmd_extract)

    evaluate = sub.add_parser("eval", help="run on data/samples and compare with data/labels")
    evaluate.add_argument("--samples", type=Path, default=Path("data/samples"))
    evaluate.add_argument("--labels", type=Path, default=Path("data/labels"))
    evaluate.add_argument("--out", type=Path, default=Path("eval"))
    evaluate.add_argument("--threshold", type=float, help="review threshold (default 0.7 or REVIEW_THRESHOLD)")
    evaluate.add_argument("--rules-only", action="store_true", help="score only the rule extractor (no API key)")
    evaluate.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
