#!/usr/bin/env python3
"""Recursively concatenate every ``judgements.json`` under a directory into one JSONL.

Each judgement file in this repo is a single JSON object whose values are
per-agent records (keyed by agent name). This script walks a directory tree,
loads each file, and writes one record per line to an output JSONL file.

Each emitted record is augmented with two provenance fields so the resulting
JSONL remains useful for downstream analysis:

* ``_source_file`` — absolute path of the ``judgements.json`` the record came from
* ``_source_key``  — the original top-level key (agent name) under which the
                     record lived in the source file

Usage
-----
    .venv/bin/python scripts/concat_judgements.py \
        --input-dir experiments \
        --output all_judgements.jsonl

    # Different filename / glob pattern
    .venv/bin/python scripts/concat_judgements.py \
        --input-dir experiments \
        --output all_judgements.jsonl \
        --pattern 'judg*.json'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATTERN = "judgements.json"


def iter_records(payload: Any) -> Iterable[tuple[Any, Any]]:
    """Yield ``(key, record)`` pairs from a parsed JSON payload.

    Handles both common shapes found in this repo:

    * a dict whose values are per-agent records (the ``judgements.json`` shape)
    * a list of records (top-level array)
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key, value
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield index, value
    else:
        # Bare scalar — emit once with a synthetic key.
        yield None, payload


def collect_records(
    input_dir: Path,
    pattern: str,
) -> Iterable[tuple[Path, Any, Any]]:
    """Yield ``(path, key, record)`` for every record found under ``input_dir``."""
    for path in sorted(input_dir.rglob(pattern)):
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            print(
                f"[skip] {path}: invalid JSON ({exc.msg} at line {exc.lineno})",
                file=sys.stderr,
            )
            continue
        except OSError as exc:
            print(f"[skip] {path}: cannot read ({exc})", file=sys.stderr)
            continue

        try:
            yielded_any = False
            for key, record in iter_records(payload):
                yielded_any = True
                yield path, key, record
            if not yielded_any:
                print(f"[skip] {path}: no records in payload", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[skip] {path}: unexpected error ({exc!r})", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively concatenate every judgements.json under a directory "
            "into one JSONL file."
        )
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Root directory to search recursively.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path of the JSONL file to write. Parent dirs are created.",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help=(
            "Glob pattern (relative filename) to match. "
            f"Defaults to '{DEFAULT_PATTERN}'."
        ),
    )
    parser.add_argument(
        "--no-source-fields",
        action="store_true",
        help=(
            "Do not inject '_source_file' / '_source_key' provenance fields "
            "into each emitted record."
        ),
    )
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(
            f"[error] --input-dir is not a directory: {args.input_dir}", file=sys.stderr
        )
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)

    files_seen: set[Path] = set()
    records_written = 0

    with args.output.open("w", encoding="utf-8") as out:
        for path, key, record in collect_records(args.input_dir, args.pattern):
            files_seen.add(path)
            if isinstance(record, dict) and not args.no_source_fields:
                record = {**record}
                record["_source_file"] = str(path.resolve())
                if key is not None:
                    record["_source_key"] = key
            try:
                out.write(json.dumps(record, ensure_ascii=False))
                out.write("\n")
                records_written += 1
            except TypeError as exc:
                print(
                    f"[skip-record] {path} (key={key!r}): not JSON-serializable ({exc})",
                    file=sys.stderr,
                )

    print(
        f"[done] wrote {records_written} records from {len(files_seen)} matching files "
        f"to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
