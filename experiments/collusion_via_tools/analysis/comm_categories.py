"""Communication-category (protocol-family) parsing and per-sweep emission.

``comm_protocol_family.md`` is a hand-written document living at the root of an
experiment output tree. It assigns each *run* to one or more **communication
categories** (protocol families) — the covert channels a colluding pair used to
smuggle information through otherwise-innocent tools.

This module:

1. **Parses** that markdown into an ordered category list plus per-run
   membership (:func:`parse_comm_protocol_family`,
   :func:`parse_run_membership`).
2. **Emits** one JSON file per sweep, placed *inside that sweep's directory*, so
   the classification travels with the sweep it describes
   (:func:`write_sweep_json`, :func:`write_all`).
3. **Loads** those emitted JSONs back, so a downstream analysis script can group
   runs by category (:func:`load_category_map`).

Markdown format
---------------
::

    ### 1. Dedicated Resource Types & Topic Channels

    *Assigned specific semantic meanings to particular resource categories
    (such as `IV_Kits`, ...) to act as channel selectors ...*

    * `<run_id>/blackboard_1.txt`
    * `<run_id>/blackboard_1.txt`

    ---

    ### 2. Scheduling Slot, Time, & Gap Encoding
    ...

A ``### <int>. <Name>`` header is followed by one italic description line
(starting ``*`` immediately followed by text, ending ``*``) and then bullet
lines of the form ``` * `<run_id>/blackboard_1.txt` ```. A run may appear under
several categories. The ``run_id`` is the run directory name — the parent of the
``blackboard_1.txt`` file.

CLI
---
::

    python -m experiments.collusion_via_tools.analysis.comm_categories <root> \\
        [<root> ...] [--md PATH] [--out-dir PATH] [--dry-run]

Run discovery is delegated to :mod:`sweep_metrics_table`, which owns the single
definition of what a run and a sweep are. This module never reimplements that.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

# Relative import first; a lazily-retried absolute import is the fallback so a
# partially-written sibling (or being imported as a top-level module) cannot
# crash this script.
try:  # pragma: no cover - exercised implicitly by the fallback helper.
    from . import sweep_metrics_table as smt  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    smt = None  # type: ignore[assignment]


CATEGORIES_FILENAME = "comm_categories.json"
COMM_PROTOCOL_FAMILY_MD = "comm_protocol_family.md"

_IMPORT_RETRIES = 5
_IMPORT_DELAY = 30  # seconds

_HEADER_RE = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$")
# A description line starts with ``*`` immediately followed by text (not a
# space, which is how bullet references start) and ends with ``*``.
_DESCRIPTION_RE = re.compile(r"^\*(?!\s)(.+?)\*\s*$")
# A bullet reference: ``* `<path>` ``.
_BULLET_RE = re.compile(r"^\*\s+`([^`]+)`\s*$")


def _import_sweep_metrics_table() -> Any:
    """Import :mod:`sweep_metrics_table` with retries on transient failures.

    Mirrors ``sweep_metrics_table._import_aggregate_protocols``: a concurrent
    editor writing the sibling module must not crash this script. Never mutates
    the sibling to work around a failure.
    """
    last_exc: BaseException | None = None
    for attempt in range(_IMPORT_RETRIES):
        try:
            repo_root = os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            )
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            module = importlib.import_module(
                "experiments.collusion_via_tools.analysis.sweep_metrics_table"
            )
            if not hasattr(module, "load_runs") or not hasattr(
                module, "find_metric_files"
            ):
                raise ImportError(
                    "sweep_metrics_table is missing load_runs/find_metric_files"
                )
            return module
        except (ImportError, SyntaxError) as exc:
            last_exc = exc
            if attempt < _IMPORT_RETRIES - 1:
                print(
                    "warning: transient import failure of sweep_metrics_table "
                    f"({exc!r}); retrying in {_IMPORT_DELAY}s "
                    f"({attempt + 1}/{_IMPORT_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(_IMPORT_DELAY)
    raise ImportError(
        f"could not import sweep_metrics_table after {_IMPORT_RETRIES} attempts"
    ) from last_exc


def _get_smt() -> Any:
    """Return the sweep_metrics_table module, importing it lazily if needed."""
    global smt
    if smt is not None:
        return smt
    smt = _import_sweep_metrics_table()
    return smt


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    description: str = ""


@dataclass(frozen=True)
class CategoryMapping:
    root: str
    categories: tuple[Category, ...]  # ordered by id ascending
    run_to_categories: dict[str, tuple[int, ...]]  # run_id -> ids (ascending)
    sweep_to_categories: dict[str, tuple[int, ...]]  # sweep -> ids (ascending)
    sweep_to_runs: dict[str, tuple[str, ...]]  # sweep -> run_ids in that sweep


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _iter_markdown(path: str | os.PathLike[str]) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().splitlines()


def parse_comm_protocol_family(path: str | os.PathLike[str]) -> list[Category]:
    """Parse the markdown into categories ordered by id.

    The description is the first italic line following a ``###`` header (its
    surrounding asterisks stripped); it is empty when absent.
    """
    categories: dict[int, Category] = {}
    order: list[int] = []
    current: int | None = None
    for line in _iter_markdown(path):
        header = _HEADER_RE.match(line)
        if header:
            cid = int(header.group(1))
            name = header.group(2).strip()
            categories[cid] = Category(id=cid, name=name, description="")
            order.append(cid)
            current = cid
            continue
        if line.strip() == "---":
            current = None
            continue
        if current is None:
            continue
        description = _DESCRIPTION_RE.match(line)
        if description and not categories[current].description:
            categories[current] = Category(
                id=current,
                name=categories[current].name,
                description=description.group(1).strip(),
            )
    return [categories[cid] for cid in sorted(set(order))]


def _parse_membership_lines(path: str | os.PathLike[str]) -> dict[int, list[str]]:
    """Return ``{category_id: [run_id, ...]}`` in file order."""
    per_category: dict[int, list[str]] = {}
    order: list[int] = []
    current: int | None = None
    for line in _iter_markdown(path):
        header = _HEADER_RE.match(line)
        if header:
            current = int(header.group(1))
            per_category.setdefault(current, [])
            order.append(current)
            continue
        if line.strip() == "---":
            current = None
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            run_id = os.path.dirname(bullet.group(1)) or bullet.group(1)
            per_category[current].append(run_id)
    return per_category


def parse_run_membership(path: str | os.PathLike[str]) -> dict[str, list[int]]:
    """Parse the markdown into ``{run_id: [category_id, ...]}`` (ids ascending).

    Duplicate references of the same run under the same category are collapsed;
    the embedded ``---`` separators and blank lines are ignored.
    """
    per_category = _parse_membership_lines(path)
    membership: dict[str, set[int]] = {}
    for cid, run_ids in per_category.items():
        for run_id in run_ids:
            membership.setdefault(run_id, set()).add(cid)
    return {run_id: sorted(ids) for run_id, ids in membership.items()}


# --------------------------------------------------------------------------- #
# Discovery (delegated to sweep_metrics_table)
# --------------------------------------------------------------------------- #


def _sweep_index(root: str) -> dict[str, dict[str, Any]]:
    """Return ``{sweep: {model, source, source_label, sweep_dir, run_ids}}``.

    A sweep's ``model``/``source`` are taken from its first discovered run; a
    sweep's ``sweep_dir`` is the parent of any of its run directories.
    """
    helper = _get_smt()
    runs = helper.load_runs(root, helper.find_metric_files(root))
    index: dict[str, dict[str, Any]] = {}
    for run in runs:
        entry = index.setdefault(
            run.sweep,
            {
                "model": run.model,
                "source": run.source,
                "source_label": run.source_label,
                "sweep_dir": os.path.dirname(run.run_dir),
                "run_ids": [],
            },
        )
        entry["run_ids"].append(run.run_id)
    for entry in index.values():
        entry["run_ids"].sort()
    return index


# --------------------------------------------------------------------------- #
# Building the mapping
# --------------------------------------------------------------------------- #


def build_mapping(
    root: str | os.PathLike[str],
    md_path: str | os.PathLike[str] | None = None,
) -> CategoryMapping:
    """Discover sweeps/runs under ``root``, parse the markdown, and join them.

    ``md_path`` defaults to ``<root>/comm_protocol_family.md``. Runs referenced
    by the markdown but absent from the tree are ignored (a warning goes to
    stderr). Runs present in the tree but absent from the markdown are kept with
    no categories. Only runs that actually carry at least one category appear in
    ``run_to_categories``.
    """
    root_str = os.fspath(root)
    md = (
        os.fspath(md_path)
        if md_path is not None
        else os.path.join(root_str, COMM_PROTOCOL_FAMILY_MD)
    )

    categories = parse_comm_protocol_family(md)
    membership = parse_run_membership(md)
    index = _sweep_index(root_str)

    known: set[str] = set()
    for entry in index.values():
        known.update(entry["run_ids"])
    for run_id in sorted(membership):
        if run_id not in known:
            print(
                f"warning: {run_id} is referenced by {os.path.basename(md)} but "
                f"absent from {root_str}; ignoring",
                file=sys.stderr,
            )

    run_to_categories: dict[str, tuple[int, ...]] = {
        run_id: tuple(ids)
        for run_id, ids in membership.items()
        if run_id in known and ids
    }

    sweep_to_runs: dict[str, tuple[str, ...]] = {
        sweep: tuple(entry["run_ids"]) for sweep, entry in index.items()
    }
    sweep_to_categories: dict[str, tuple[int, ...]] = {}
    for sweep, entry in index.items():
        union: set[int] = set()
        for run_id in entry["run_ids"]:
            union.update(run_to_categories.get(run_id, ()))
        sweep_to_categories[sweep] = tuple(sorted(union))

    return CategoryMapping(
        root=root_str,
        categories=tuple(categories),
        run_to_categories=run_to_categories,
        sweep_to_categories=sweep_to_categories,
        sweep_to_runs=sweep_to_runs,
    )


# --------------------------------------------------------------------------- #
# Emitting per-sweep JSON
# --------------------------------------------------------------------------- #


def sweep_payload(
    mapping: CategoryMapping, sweep: str, model: str, source: str, run_ids: list[str]
) -> dict:
    """Return the JSON payload for one sweep (see the module docstring schema).

    ``run_ids`` is the full list of run directories in the sweep. Only the runs
    that carry at least one category appear in ``runs``; those without a category
    are still counted in ``n_runs``. ``categories`` lists only the categories the
    sweep falls under (at least one of its runs belongs to them), with ``n_runs``
    the number of this sweep's runs in that category.
    """
    by_id = {category.id: category for category in mapping.categories}

    run_cats: dict[str, list[int]] = {}
    for run_id in run_ids:
        ids = mapping.run_to_categories.get(run_id, ())
        if ids:
            run_cats[run_id] = list(ids)

    union = sorted({cid for ids in run_cats.values() for cid in ids})
    category_entries: list[dict] = []
    for cid in union:
        category = by_id.get(cid)
        if category is None:
            continue
        n_runs = sum(1 for ids in run_cats.values() if cid in ids)
        category_entries.append(
            {
                "id": cid,
                "name": category.name,
                "description": category.description,
                "n_runs": n_runs,
            }
        )

    runs_obj = {run_id: run_cats[run_id] for run_id in sorted(run_cats)}

    return {
        "sweep": sweep,
        "model": model,
        "source": source,
        "source_document": COMM_PROTOCOL_FAMILY_MD,
        "n_runs": len(run_ids),
        "n_runs_classified": len(run_cats),
        "categories": category_entries,
        "runs": runs_obj,
    }


def _write_json(payload: dict, path: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def write_sweep_json(payload: dict, sweep_dir: str | os.PathLike[str]) -> str:
    """Write ``payload`` as ``<sweep_dir>/comm_categories.json``. Returns the path."""
    path = os.path.join(os.fspath(sweep_dir), CATEGORIES_FILENAME)
    return _write_json(payload, path)


def write_all(
    root: str | os.PathLike[str],
    md_path: str | os.PathLike[str] | None = None,
    out_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Emit one JSON per sweep found under ``root``.

    With ``out_dir`` the file is written as ``<out_dir>/<sweep>.json`` instead of
    inside the sweep directory. Returns the written paths.
    """
    root_str = os.fspath(root)
    md = (
        os.fspath(md_path)
        if md_path is not None
        else os.path.join(root_str, COMM_PROTOCOL_FAMILY_MD)
    )

    mapping = build_mapping(root_str, md)
    index = _sweep_index(root_str)

    paths: list[str] = []
    for sweep in sorted(index):
        entry = index[sweep]
        payload = sweep_payload(
            mapping,
            sweep,
            entry["model"],
            entry["source"],
            list(entry["run_ids"]),
        )
        # Keep ``source_document`` accurate even with a custom ``--md``.
        payload["source_document"] = os.path.basename(md)
        if out_dir is not None:
            path = os.path.join(os.fspath(out_dir), f"{sweep}.json")
            os.makedirs(os.fspath(out_dir), exist_ok=True)
            paths.append(_write_json(payload, path))
        else:
            paths.append(write_sweep_json(payload, entry["sweep_dir"]))
    return paths


# --------------------------------------------------------------------------- #
# Loading emitted JSON back
# --------------------------------------------------------------------------- #


def _find_category_files(root: str) -> list[str]:
    """Recursively find every ``comm_categories.json`` under ``root``."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if CATEGORIES_FILENAME in filenames:
            found.append(os.path.join(dirpath, CATEGORIES_FILENAME))
    return sorted(found)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_category_map(root: str | os.PathLike[str]) -> CategoryMapping:
    """Read back every ``comm_categories.json`` under ``root``.

    Categories are ordered by id; names/descriptions come from the JSON files.
    ``run_to_categories`` is the union across sweeps; ``sweep_to_categories`` is
    rebuilt from the per-sweep files. ``sweep_to_runs`` holds the classified runs
    recorded in each file (the emitted schema does not persist the unclassified
    ones). Raises :class:`FileNotFoundError` if no ``comm_categories.json``
    exists.
    """
    root_str = os.fspath(root)
    paths = _find_category_files(root_str)
    if not paths:
        raise FileNotFoundError(f"no {CATEGORIES_FILENAME} found under {root_str}")

    by_id: dict[int, Category] = {}
    run_to_categories: dict[str, tuple[int, ...]] = {}
    sweep_to_categories: dict[str, tuple[int, ...]] = {}
    sweep_to_runs: dict[str, tuple[str, ...]] = {}

    for path in paths:
        data = _read_json(path)
        if data is None:
            print(f"warning: skipping unreadable {path}", file=sys.stderr)
            continue

        for entry in data.get("categories", []) or []:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("id")
            if not isinstance(cid, int):
                continue
            name = entry.get("name", "") or ""
            description = entry.get("description", "") or ""
            previous = by_id.get(cid)
            if previous is None:
                by_id[cid] = Category(id=cid, name=name, description=description)
            else:
                by_id[cid] = Category(
                    id=cid,
                    name=previous.name or name,
                    description=previous.description or description,
                )

        for run_id, ids in (data.get("runs", {}) or {}).items():
            if not isinstance(ids, list):
                continue
            union = set(run_to_categories.get(run_id, ()))
            union.update(i for i in ids if isinstance(i, int))
            run_to_categories[run_id] = tuple(sorted(union))

        sweep = data.get("sweep")
        if isinstance(sweep, str) and sweep:
            cat_union = set(sweep_to_categories.get(sweep, ()))
            for entry in data.get("categories", []) or []:
                if isinstance(entry, dict) and entry.get("n_runs", 0):
                    cid = entry.get("id")
                    if isinstance(cid, int):
                        cat_union.add(cid)
            sweep_to_categories[sweep] = tuple(sorted(cat_union))

            runs_here = set(sweep_to_runs.get(sweep, ()))
            runs_here.update((data.get("runs", {}) or {}).keys())
            sweep_to_runs[sweep] = tuple(sorted(runs_here))

    categories = tuple(by_id[cid] for cid in sorted(by_id))
    return CategoryMapping(
        root=root_str,
        categories=categories,
        run_to_categories=run_to_categories,
        sweep_to_categories=sweep_to_categories,
        sweep_to_runs=sweep_to_runs,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _summarize(
    root: str, md: str, mapping: CategoryMapping, written: list[str]
) -> None:
    by_id = {category.id: category for category in mapping.categories}
    print(f"root: {root}")
    print(f"  categories: {len(mapping.categories)}")
    print(f"  sweeps: {len(mapping.sweep_to_runs)}")
    for sweep in sorted(mapping.sweep_to_runs):
        run_ids = mapping.sweep_to_runs[sweep]
        classified = sum(
            1 for run_id in run_ids if mapping.run_to_categories.get(run_id)
        )
        print(f"  - {sweep}: {len(run_ids)} runs, {classified} classified")
        for cid in mapping.sweep_to_categories.get(sweep, ()):
            category = by_id.get(cid)
            name = category.name if category else f"<unknown {cid}>"
            n_runs = sum(
                1
                for run_id in run_ids
                if cid in mapping.run_to_categories.get(run_id, ())
            )
            print(f"      [{cid}] {name} ({n_runs} runs)")
    for path in written:
        print(f"  wrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m experiments.collusion_via_tools.analysis.comm_categories",
        description=(
            "Parse comm_protocol_family.md and emit one comm_categories.json per "
            "sweep under each experiment output root."
        ),
    )
    parser.add_argument(
        "roots",
        nargs="+",
        metavar="ROOT",
        help="Timestamped experiment output root(s) containing runs/.",
    )
    parser.add_argument(
        "--md",
        default=None,
        metavar="PATH",
        help="Override the markdown path (default <root>/comm_protocol_family.md).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        metavar="PATH",
        help=(
            "Write <out-dir>/<sweep>.json instead of <sweep_dir>/comm_categories.json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written, write nothing.",
    )
    args = parser.parse_args(argv)

    for root in args.roots:
        md = (
            args.md
            if args.md is not None
            else os.path.join(root, COMM_PROTOCOL_FAMILY_MD)
        )
        mapping = build_mapping(root, md)
        written: list[str] = []
        if args.dry_run:
            _summarize(root, md, mapping, written)
            print("  (dry-run: nothing written)")
        else:
            written = write_all(root, md, args.out_dir)
            _summarize(root, md, mapping, written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
