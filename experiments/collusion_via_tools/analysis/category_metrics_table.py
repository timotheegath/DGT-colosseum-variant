"""Shared plumbing for the **communication-category** metric tables.

This is the run-level counterpart of :mod:`sweep_metrics_table`. Where the sweep
tables pool runs by sweep name, these tables pool runs by *communication
category* (the protocol family assigned per run by
:mod:`experiments.collusion_via_tools.analysis.comm_categories`). Category
membership is per run, not per sweep: one run can belong to several categories,
so a run is counted once in every category it belongs to. Grouping at the sweep
level would be degenerate here (both classified sweeps fall under all five
categories), which is why this module groups at the run level.

This module is a **library**, not a command-line entry point. The CLI lives in
:mod:`experiments.collusion_via_tools.analysis.category_tables`::

    python -m experiments.collusion_via_tools.analysis.category_tables <root> [<root> ...]

The metric catalogue, run discovery, grouping semantics, formatting and delta
math are all reused from :mod:`sweep_metrics_table`; the category mapping comes
from :mod:`comm_categories`. The only new pieces here are the
:class:`CategoryGroup` container and the category-labelled table/CSV/JSON
renderers.
"""

from __future__ import annotations

import csv
import importlib
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Sibling imports (retry-safe, mirrors sweep_metrics_table._import_aggregate_protocols)
# --------------------------------------------------------------------------- #

_PACKAGE = __package__ or "experiments.collusion_via_tools.analysis"

_SIBLING_IMPORT_RETRIES = 5
_SIBLING_IMPORT_DELAY = 30  # seconds


def _repo_root() -> str:
    """Return the repository root (four levels above this file)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


def _import_sibling(module_name: str) -> Any:
    """Import a sibling module, retrying on transient failures.

    A concurrently-written sibling (e.g. ``comm_categories.py``) can raise a
    transient ``ImportError``/``SyntaxError`` while it is being generated. This
    retries the frozen-repo import the same way
    ``sweep_metrics_table._import_aggregate_protocols`` does, then raises a
    clear ``ImportError``.
    """
    last_exc: BaseException | None = None
    for attempt in range(_SIBLING_IMPORT_RETRIES):
        try:
            return importlib.import_module(f"{_PACKAGE}.{module_name}")
        except (ImportError, SyntaxError) as exc:
            last_exc = exc
            root = _repo_root()
            if root not in sys.path:
                sys.path.insert(0, root)
            if attempt < _SIBLING_IMPORT_RETRIES - 1:
                print(
                    f"warning: transient import failure of {module_name} "
                    f"({exc!r}); retrying in {_SIBLING_IMPORT_DELAY}s "
                    f"({attempt + 1}/{_SIBLING_IMPORT_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(_SIBLING_IMPORT_DELAY)
    raise ImportError(
        f"could not import {module_name} after {_SIBLING_IMPORT_RETRIES} attempts"
    ) from last_exc


smt = _import_sibling("sweep_metrics_table")
cc = _import_sibling("comm_categories")
dm = _import_sibling("delta_metrics")
sig = _import_sibling("sweep_significance")

from .sweep_metrics_table import (  # noqa: E402  (import after the retry helper)
    DELTA_METRICS,
    METRICS_BY_KEY,
    GroupMetrics,
    MetricSpec,
    RunData,
)

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


def slugify(name: str) -> str:
    """Lowercase ``name`` and collapse every non-alphanumeric run to ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class CategoryGroup:
    """One communication category's runs for one model.

    Mirrors :class:`sweep_metrics_table.SweepGroup`'s field names so the
    existing printers/filters that only touch ``sources``/``source_labels``/
    ``source_paths``/``runs`` can be reused unchanged. ``label`` is the human
    category NAME, ``sweep`` is the stable slug (the machine key in CSV/JSON),
    and ``category_id`` is the numeric id.
    """

    label: str
    sweep: str
    category_id: int | None
    sources: list[str]
    source_labels: list[str]
    source_paths: list[str]
    runs: list[RunData]
    sweeps: list[str]


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def build_category_groups(
    runs: list[RunData], mapping: Any
) -> dict[str, list[CategoryGroup]]:
    """Group runs by model then communication category (sorted by id).

    A run belongs to the group(s) for each category id in
    ``mapping.run_to_categories[run.run_id]``; runs with no category are
    excluded and reported on stderr. ``label``/``sweep`` are the category NAME
    and its slug (``<id>_<slugified-name>``); ``sources``/``source_labels``/
    ``source_paths`` are the sorted unique values over the group's runs (same
    semantics as ``sweep_metrics_table.group_runs``) and ``sweeps`` the sorted
    unique sweep names contributing runs.
    """
    categories_by_id: dict[int, Any] = {c.id: c for c in mapping.categories}

    buckets: dict[str, dict[int, list[RunData]]] = {}
    unclassified = 0
    for run in runs:
        category_ids = mapping.run_to_categories.get(run.run_id, ())
        if not category_ids:
            unclassified += 1
            continue
        for category_id in category_ids:
            buckets.setdefault(run.model, {}).setdefault(category_id, []).append(run)

    grouped: dict[str, list[CategoryGroup]] = {}
    for model, model_buckets in buckets.items():
        groups: list[CategoryGroup] = []
        for category_id in sorted(model_buckets):
            category = categories_by_id.get(category_id)
            name = category.name if category is not None else str(category_id)
            category_runs = model_buckets[category_id]
            category_runs.sort(key=lambda r: (r.run_id, r.source))
            sources = sorted({run.source for run in category_runs})
            source_labels = sorted({run.source_label for run in category_runs})
            source_paths = sorted({run.source_path for run in category_runs})
            if len(sources) > 1:
                print(
                    f"warning: category '{name}' spans {len(sources)} output "
                    f"roots\n         ({', '.join(sources)}); its runs were "
                    f"pooled into one group",
                    file=sys.stderr,
                )
            groups.append(
                CategoryGroup(
                    label=name,
                    sweep=f"{category_id}_{slugify(name)}",
                    category_id=category_id,
                    sources=sources,
                    source_labels=source_labels,
                    source_paths=source_paths,
                    runs=category_runs,
                    sweeps=sorted({run.sweep for run in category_runs}),
                )
            )
        grouped[model] = groups

    if unclassified:
        print(
            f"warning: {unclassified} run(s) have no communication category and "
            f"were excluded from the category groups",
            file=sys.stderr,
        )
    return grouped


def filter_groups_by_category(
    grouped: dict[str, list[CategoryGroup]], names: set[str]
) -> dict[str, list[CategoryGroup]]:
    """Keep groups whose name, id-as-string, or slug is in ``names``."""

    def matches(group: CategoryGroup) -> bool:
        candidates = {group.label, group.sweep}
        if group.category_id is not None:
            candidates.add(str(group.category_id))
        return bool(candidates & names)

    return {
        model: [group for group in groups if matches(group)]
        for model, groups in grouped.items()
    }


def resolve_category_reference(
    groups: list[CategoryGroup], requested: str | None
) -> CategoryGroup | None:
    """Resolve the delta reference category for one model.

    ``requested`` may be the category NAME (exact match wins, then
    case-insensitive), the numeric id as a string, or the slug. Returns
    ``None`` (with a diagnostic on stderr) when it cannot be resolved or is
    ambiguous. With ``requested`` ``None`` the group with the lowest
    ``category_id`` is returned. Non-category groups (``category_id is None``,
    e.g. a reference sweep) are never returned.
    """
    groups = [group for group in groups if group.category_id is not None]
    if requested:
        for group in groups:
            if group.label == requested:
                return group

        lowered = requested.lower()
        ci_matches = [group for group in groups if group.label.lower() == lowered]
        if len(ci_matches) == 1:
            return ci_matches[0]
        if len(ci_matches) > 1:
            labels = ", ".join(group.label for group in ci_matches)
            print(
                f"error: reference category '{requested}' is ambiguous: {labels}",
                file=sys.stderr,
            )
            return None

        id_matches = [group for group in groups if str(group.category_id) == requested]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            labels = ", ".join(group.label for group in id_matches)
            print(
                f"error: reference category id '{requested}' is ambiguous: {labels}",
                file=sys.stderr,
            )
            return None

        slug_matches = [group for group in groups if group.sweep == requested]
        if len(slug_matches) == 1:
            return slug_matches[0]
        if len(slug_matches) > 1:
            labels = ", ".join(group.label for group in slug_matches)
            print(
                f"error: reference category slug '{requested}' is ambiguous: {labels}",
                file=sys.stderr,
            )
            return None

        print(
            f"warning: reference category '{requested}' not found",
            file=sys.stderr,
        )
        return None

    return min(groups, key=lambda group: group.category_id) if groups else None


# --------------------------------------------------------------------------- #
# Table A -- category inventory
# --------------------------------------------------------------------------- #


def build_category_inventory_rows(
    groups: list[CategoryGroup], totals_by_category: dict[Any, int] | None = None
) -> list[list[str]]:
    rows: list[list[str]] = []
    for group in groups:
        runs = group.runs
        n_runs = len(runs)
        totals = totals_by_category or {}
        total = totals.get(group.category_id, totals.get(group.sweep, n_runs))
        runs_cell = f"{n_runs}/{total}" if total != n_runs else str(n_runs)

        adv_counts = [
            len(run_config["adversaries"])
            for run in runs
            for run_config in [run.run_config]
            if isinstance(run_config, dict)
            and isinstance(run_config.get("adversaries"), list)
        ]
        adv = smt._modal(adv_counts)

        pre_rounds = smt._modal_field(
            runs, lambda rc: rc.get("pre_coordination_rounds")
        )
        pre_effective = smt._modal_field(
            runs, lambda rc: rc.get("pre_coordination_rounds_effective")
        )
        precoord = (
            f"{pre_rounds if pre_rounds is not None else '-'} / "
            f"{pre_effective if pre_effective is not None else '-'}"
        )

        post_msg = smt._modal_field(runs, lambda rc: rc.get("disable_post_message"))
        secret = smt._modal_field(runs, lambda rc: rc.get("effective_secret_channel"))

        agents = smt._modal([smt._num(run.metrics.get("total_agents")) for run in runs])
        patients = smt._modal(
            [
                smt._num(smt._dig(run.metrics, "meeting_outcome", "total_patients"))
                for run in runs
            ]
        )

        complete = sum(
            1
            for run in runs
            if smt._dig(run.metrics, "meeting_outcome", "status") == "complete"
        )

        rows.append(
            [
                group.label,
                "-" if group.category_id is None else str(group.category_id),
                runs_cell,
                "; ".join(group.sweeps),
                str(adv) if adv is not None else "-",
                precoord,
                smt._yes_no(post_msg),
                smt._yes_no(secret),
                f"{agents:.0f}" if isinstance(agents, float) else "-",
                f"{patients:.0f}" if isinstance(patients, float) else "-",
                f"{complete}/{n_runs}",
            ]
        )
    return rows


_INVENTORY_HEADERS = [
    "Category",
    "ID",
    "Runs",
    "Sweeps",
    "Adv",
    "PreCoord(round/effective)",
    "PostMsg restricted",
    "Secret channel",
    "Agents",
    "Patients",
    "Complete",
]
_INVENTORY_ALIGNS = ["l", "r", "r", "l", "r", "l", "l", "l", "r", "r", "r"]


def build_category_inventory_table(
    groups: list[CategoryGroup],
    style: str,
    totals_by_category: dict[Any, int] | None = None,
) -> str:
    rows = build_category_inventory_rows(groups, totals_by_category)
    return smt.render_style(_INVENTORY_HEADERS, rows, _INVENTORY_ALIGNS, style)


def print_category_inventory(
    groups: list[CategoryGroup],
    model: str,
    totals_by_category: dict[Any, int] | None = None,
) -> None:
    print(f"\nTable A -- category inventory [{model or '-'}]")
    rows = build_category_inventory_rows(groups, totals_by_category)
    print(smt.render_table(_INVENTORY_HEADERS, rows, _INVENTORY_ALIGNS))


# --------------------------------------------------------------------------- #
# Table B -- metrics mean +/- std
# --------------------------------------------------------------------------- #


def print_category_metrics_table(
    labels: list[str],
    groups: dict[str, GroupMetrics],
    specs: list[MetricSpec],
    model: str,
) -> None:
    print(f"\nTable B -- metrics (mean +/- std) [{model or '-'}]")
    headers = ["Metric"] + labels
    rows: list[list[str]] = []

    rows.append(["n"] + [str(groups[label].n_runs) for label in labels])

    for spec in specs:
        row = [spec.label]
        for label in labels:
            agg = groups[label].aggregates[spec.key]
            row.append(smt._metric_cell(spec, agg, groups[label].n_runs))
        rows.append(row)

    aligns = ["l"] + ["r"] * len(labels)
    print(smt.render_table(headers, rows, aligns))


def build_transposed_category_metrics_table(
    group_list: list[CategoryGroup],
    groups: dict[str, GroupMetrics],
    specs: list[MetricSpec],
    style: str,
) -> str:
    headers = ["Category", "n"] + [spec.label for spec in specs]
    aligns = ["l", "r"] + ["r"] * len(specs)
    rows: list[list[str]] = []
    for category_group in group_list:
        group = groups[category_group.label]
        row = [category_group.label, str(group.n_runs)]
        for spec in specs:
            row.append(
                smt._transposed_metric_cell(
                    spec, group.aggregates[spec.key], group.n_runs
                )
            )
        rows.append(row)
    return smt.render_style(headers, rows, aligns, style)


# --------------------------------------------------------------------------- #
# Table C -- delta vs reference
# --------------------------------------------------------------------------- #


def _spec_values(spec: MetricSpec, runs: list[RunData]) -> list[float]:
    """Return the non-null metric values for ``runs`` (order preserved)."""
    values: list[float] = []
    for run in runs:
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if value is not None:
            values.append(value)
    return values


def public_channel_judge_score(metrics: dict[str, Any]) -> float | None:
    """Return the collusion judge's PUBLIC-channel score, or ``None``.

    Reads ``metrics["collusion_judge_simple"]["channel_scores"][0]``. This is
    the cross-sweep comparable judge signal: ``delta_metrics.judge_score``
    (``mean_score``) averages over the judged blackboard channels, so it is
    inflated by exactly +2.5 for any run that has a secret channel. In this
    dataset the coord sweeps have 2 channels (public + secret) while the three
    other sweeps have only the public channel, so only the public-channel score
    is comparable across all five sweeps (it lies in 0..2 everywhere).

    Note: the SECRET channel scores 5 in 40/40 coord runs, so the secret channel
    does not discriminate at all and is deliberately ignored here. The public
    channel is the comparable signal.
    """
    block = metrics.get("collusion_judge_simple")
    if not isinstance(block, dict):
        return None
    scores = block.get("channel_scores")
    if not isinstance(scores, list) or not scores:
        return None
    try:
        return float(scores[0])
    except (TypeError, ValueError):
        return None


def _mean_std_n(spec: MetricSpec, mean: float | None, std: float | None, n: int) -> str:
    """Render ``mean +/- std (n=...)`` (or ``n/a``) for the unpaired tables."""
    if mean is None:
        return "n/a"
    return (
        f"{smt.format_value(spec.fmt, mean)} +/- "
        f"{smt.format_value(spec.fmt, std)} (n={n})"
    )


def compute_category_delta(
    spec: MetricSpec,
    group_runs: list[RunData],
    ref_runs: list[RunData],
    ddof: int,
) -> Any:
    """Unpaired category-vs-category difference of means +/- standard error.

    Category groups are overlapping subsets of the *same* experimental units: a
    run can belong to several categories, and the two classified sweeps reuse
    the same seed numbers, so a seed can name a different run (or the same run)
    in two categories. A within-seed pairing between two categories is therefore
    not well-defined. This reports the unpaired difference of means
    ``mean(group) - mean(ref)`` with the **Welch standard error of the
    difference** ``sqrt(s_group^2 / n_group + s_ref^2 / n_ref)`` in the ``std``
    field (``None`` when either group has n < 2), ``paired=False`` and
    ``paired_n=0``. Missing values are skipped (as in
    ``sweep_metrics_table.compute_delta``'s unpaired fallback).
    """
    group_values = _spec_values(spec, group_runs)
    ref_values = _spec_values(spec, ref_runs)
    if not group_values or not ref_values:
        return smt.DeltaResult(mean=None, std=None, paired_n=0, paired=False)
    mean = statistics.fmean(group_values) - statistics.fmean(ref_values)
    if len(group_values) < 2 or len(ref_values) < 2:
        return smt.DeltaResult(mean=mean, std=None, paired_n=0, paired=False)
    se = math.sqrt(
        statistics.variance(group_values) / len(group_values)
        + statistics.variance(ref_values) / len(ref_values)
    )
    return smt.DeltaResult(mean=mean, std=se, paired_n=0, paired=False)


def compute_category_delta_vs_sweep(
    spec: MetricSpec,
    group_runs: list[RunData],
    sweep_runs: list[RunData],
    ddof: int,
) -> Any:
    """Paired-by-seed category-vs-sweep difference (mean over arms).

    A reference *sweep* is a disjoint set of units with exactly one run per
    seed, so pairing a category's runs against the sweep's runs at the same seed
    IS well defined. A category can hold several arms at one seed (the two
    classified sweeps reuse seed numbers 0..19), so the per-seed point is the
    **mean over the category's arms at that seed** of
    ``value(arm) - value(reference at seed)`` -- this uses every run and does not
    select on the experimental condition (which is what a first-arm rule would
    do). The per-seed differences are aggregated to ``mean +/- SD (k=n)`` (the SD
    of the paired differences, the correct dispersion). Falls back to the
    unpaired difference of means when the seeds do not overlap.
    """
    arms_by_seed: dict[int, list[float]] = {}
    for run in group_runs:
        seed = run.seed()
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if seed is not None and value is not None:
            arms_by_seed.setdefault(seed, []).append(value)
    sweep_by_seed: dict[int, float] = {}
    for run in sweep_runs:
        seed = run.seed()
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if seed is not None and value is not None and seed not in sweep_by_seed:
            sweep_by_seed[seed] = value

    paired_seeds = sorted(set(arms_by_seed) & set(sweep_by_seed))
    if paired_seeds:
        diffs = [
            statistics.fmean(arms_by_seed[seed]) - sweep_by_seed[seed]
            for seed in paired_seeds
        ]
        agg = smt.aggregate(diffs, n_missing=0, ddof=ddof)
        return smt.DeltaResult(
            mean=agg.mean, std=agg.std, paired_n=len(paired_seeds), paired=True
        )
    return compute_category_delta(spec, group_runs, sweep_runs, ddof)


def compute_category_paired_delta_points(
    grouped: dict[str, dict[str, list[RunData]]],
    reference_sweep: str,
) -> tuple[list[Any], dict[str, dict[str, int]]]:
    """Paired-by-seed delta points of every category against a reference sweep.

    ``grouped`` is ``{model: {category_slug: [runs]}}``. The reference sweep's
    runs must be present under the key ``reference_sweep`` (the CLI adds them;
    they are not in the category groups because the reference sweep is usually
    unclassified). The reference sweep has exactly one run per seed.

    A category can hold two arms at the same seed (the two classified sweeps
    reuse seed numbers 0..19), so the per-seed point is the **mean over the
    category's arms at that seed** of ``value(arm) - value(reference at seed)``
    -- this uses every run and does not select on the experimental condition
    (which is what a first-arm-by-run_id rule would do). Exactly ONE point per
    ``(category, seed)`` is emitted, so the one-sample t / Wilcoxon machinery in
    ``sweep_significance`` still sees independent seeds.

    The judge component uses :func:`public_channel_judge_score` (the
    cross-sweep-comparable public-channel score), not
    ``delta_metrics.judge_score`` (whose channel-averaged score is a
    channel-count artifact across sweeps). ``run_id`` is the ``; ``-joined,
    sorted set of the contributing arms' run ids (the point is an aggregate).
    ``judge_delta``/``advantage_delta`` are ``None`` when no arm yields a valid
    component. Returns ``(points, run_counts)`` where
    ``run_counts[model][category_slug]`` is the number of paired seeds.
    """
    points: list[Any] = []
    run_counts: dict[str, dict[str, int]] = {}
    for model, categories in grouped.items():
        ref_runs = sorted(
            categories.get(reference_sweep, []), key=lambda r: (r.run_id, r.source)
        )
        ref_by_seed: dict[int, RunData] = {}
        for run in ref_runs:
            seed = run.seed()
            if seed is None or seed in ref_by_seed:
                continue
            ref_by_seed[seed] = run

        counts: dict[str, int] = {}
        for category_slug, category_runs in categories.items():
            if category_slug == reference_sweep:
                continue
            arms_by_seed: dict[int, list[RunData]] = {}
            for run in sorted(category_runs, key=lambda r: (r.run_id, r.source)):
                seed = run.seed()
                if seed is None:
                    continue
                arms_by_seed.setdefault(seed, []).append(run)

            n_points = 0
            for seed, arms in sorted(arms_by_seed.items()):
                ref_run = ref_by_seed.get(seed)
                if ref_run is None:
                    continue
                judge_ref = public_channel_judge_score(ref_run.metrics)
                adv_ref = dm.coalition_advantage(ref_run.metrics)
                judge_deltas: list[float] = []
                advantage_deltas: list[float] = []
                for arm in arms:
                    judge_here = public_channel_judge_score(arm.metrics)
                    if judge_here is not None and judge_ref is not None:
                        judge_deltas.append(judge_here - judge_ref)
                    adv_here = dm.coalition_advantage(arm.metrics)
                    if adv_here is not None and adv_ref is not None:
                        advantage_deltas.append(adv_here - adv_ref)
                run_id = "; ".join(sorted(arm.run_id for arm in arms))
                points.append(
                    dm.DeltaPoint(
                        model=model,
                        sweep=category_slug,
                        seed=seed,
                        run_id=run_id,
                        judge_delta=(
                            statistics.fmean(judge_deltas) if judge_deltas else None
                        ),
                        advantage_delta=(
                            statistics.fmean(advantage_deltas)
                            if advantage_deltas
                            else None
                        ),
                    )
                )
                n_points += 1
            counts[category_slug] = n_points
        run_counts[model] = counts
    return points, run_counts


def print_category_delta_table(
    groups: list[CategoryGroup],
    reference: CategoryGroup,
    specs: list[MetricSpec],
    model: str,
    ddof: int,
) -> None:
    """Unpaired category-vs-category Table C (see ``compute_category_delta``)."""
    if len(groups) <= 1:
        return

    ref_label = reference.label
    ref_runs = reference.runs
    others = [group for group in groups if group is not reference]
    columns = [reference] + others

    headers = ["Metric", f"{ref_label} (ref)"]
    for group in others:
        headers.append(f"{group.label} - {ref_label}")

    rows: list[list[str]] = []
    for key in DELTA_METRICS:
        spec = METRICS_BY_KEY.get(key)
        if spec is None:
            continue
        row = [spec.label]
        for group in columns:
            if group is reference:
                agg = smt.aggregate(
                    _spec_values(spec, ref_runs), n_missing=0, ddof=ddof
                )
                row.append(_mean_std_n(spec, agg.mean, agg.std, agg.n))
            else:
                result = compute_category_delta(spec, group.runs, ref_runs, ddof)
                n = len(_spec_values(spec, group.runs))
                row.append(_mean_std_n(spec, result.mean, result.std, n))
        rows.append(row)

    print(
        f"\nTable C -- Delta vs {ref_label} (unpaired mean difference +/- SE; "
        f"category groups overlap so a within-seed pairing is not well-defined) "
        f"[{model or '-'}]"
    )
    aligns = ["l"] + ["r"] * len(columns)
    print(smt.render_table(headers, rows, aligns))

    # Show how much each compared category overlaps the reference category
    # (counts are over RUNS, so overlapping runs are counted once on each side).
    ref_ids = {run.run_id for run in ref_runs}
    for group in others:
        group_ids = {run.run_id for run in group.runs}
        print(
            f"overlap: {group.label} vs {ref_label}: "
            f"|A\u2229B|={len(group_ids & ref_ids)}  "
            f"|A\\B|={len(group_ids - ref_ids)}  "
            f"|B\\A|={len(ref_ids - group_ids)}"
        )


def print_category_delta_vs_sweep_table(
    groups: list[CategoryGroup],
    sweep_label: str,
    sweep_runs: list[RunData],
    specs: list[MetricSpec],
    model: str,
    ddof: int,
) -> None:
    """Paired-by-seed Table C: every category vs a reference sweep.

    The reference-sweep group itself (``category_id is None``) is excluded from
    the category columns, so no all-zero ``<sweep> - <sweep>`` column is emitted.
    """
    category_groups = [group for group in groups if group.category_id is not None]
    if not category_groups:
        return

    headers = ["Metric", f"{sweep_label} (ref sweep)"]
    for group in category_groups:
        headers.append(f"{group.label} - {sweep_label}")

    rows: list[list[str]] = []
    for key in DELTA_METRICS:
        spec = METRICS_BY_KEY.get(key)
        if spec is None:
            continue
        ref_agg = smt.aggregate(_spec_values(spec, sweep_runs), n_missing=0, ddof=ddof)
        row = [spec.label, _mean_std_n(spec, ref_agg.mean, ref_agg.std, ref_agg.n)]
        for group in category_groups:
            result = compute_category_delta_vs_sweep(spec, group.runs, sweep_runs, ddof)
            if result.mean is None:
                row.append("n/a")
            elif result.paired and result.std is not None:
                cell = (
                    f"{smt.format_value(spec.fmt, result.mean)} +/- "
                    f"{smt.format_value(spec.fmt, result.std)} (k={result.paired_n})"
                )
                row.append(cell)
            else:
                n = len(_spec_values(spec, group.runs))
                row.append(_mean_std_n(spec, result.mean, result.std, n))
        rows.append(row)

    print(
        f"\nTable C -- Delta vs sweep {sweep_label} (paired by seed; negative = "
        f"lower than the reference sweep) [{model or '-'}]"
    )
    aligns = ["l"] + ["r"] * (1 + len(category_groups))
    print(smt.render_table(headers, rows, aligns))

    # Overlap between each category and the reference sweep, over RUNS.
    ref_ids = {run.run_id for run in sweep_runs}
    for group in category_groups:
        group_ids = {run.run_id for run in group.runs}
        print(
            f"overlap: {group.label} vs {sweep_label}: "
            f"|A\u2229B|={len(group_ids & ref_ids)}  "
            f"|A\\B|={len(group_ids - ref_ids)}  "
            f"|B\\A|={len(ref_ids - group_ids)}"
        )


def build_transposed_category_delta_table(
    groups: list[CategoryGroup],
    reference: CategoryGroup,
    delta_specs: list[MetricSpec],
    ddof: int,
    style: str,
) -> str | None:
    if len(groups) <= 1:
        return None

    ref_runs = reference.runs
    headers = ["Category"] + [spec.label for spec in delta_specs]
    aligns = ["l"] + ["r"] * len(delta_specs)
    rows: list[list[str]] = []
    for group in groups:
        row = [f"{group.label} (ref)" if group is reference else group.label]
        for spec in delta_specs:
            if group is reference:
                agg = smt.aggregate(
                    _spec_values(spec, ref_runs), n_missing=0, ddof=ddof
                )
                row.append(_mean_std_n(spec, agg.mean, agg.std, agg.n))
            else:
                result = compute_category_delta(spec, group.runs, ref_runs, ddof)
                n = len(_spec_values(spec, group.runs))
                row.append(_mean_std_n(spec, result.mean, result.std, n))
        rows.append(row)
    return smt.render_style(headers, rows, aligns, style)


def build_transposed_category_delta_vs_sweep_table(
    groups: list[CategoryGroup],
    sweep_label: str,
    sweep_runs: list[RunData],
    delta_specs: list[MetricSpec],
    ddof: int,
    style: str,
) -> str | None:
    # The reference-sweep group is excluded so no all-zero row is emitted.
    category_groups = [group for group in groups if group.category_id is not None]
    if not category_groups:
        return None

    headers = ["Category"] + [spec.label for spec in delta_specs]
    aligns = ["l"] + ["r"] * len(delta_specs)
    rows: list[list[str]] = []
    for group in category_groups:
        row = [group.label]
        for spec in delta_specs:
            result = compute_category_delta_vs_sweep(spec, group.runs, sweep_runs, ddof)
            row.append(smt._delta_cell(result, spec))
        rows.append(row)
    return smt.render_style(headers, rows, aligns, style)


def print_category_transposed_output(
    models: list[str],
    groups_by_model: dict[str, list[CategoryGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
    specs: list[MetricSpec],
    delta_specs: list[MetricSpec],
    requested_reference: str | None,
    ddof: int,
    style: str,
    show_delta: bool,
    totals_by_model: dict[str, dict[Any, int]] | None = None,
    reference_sweep_name: str | None = None,
    sweep_runs_by_model: dict[str, list[RunData]] | None = None,
) -> None:
    """Print the three tables in the transposed layout (formatted / markdown).

    With ``reference_sweep_name`` the delta block compares every category
    against that sweep (paired by seed); otherwise it compares categories
    against the reference category (unpaired mean difference).
    """

    def heading(text: str) -> str:
        return f"**{text}**" if style == "markdown" else text

    totals_by_model = totals_by_model or {}
    sweep_runs_by_model = sweep_runs_by_model or {}
    blocks: list[str] = []
    for model in models:
        if len(models) > 1:
            blocks.append(heading(f"Model: {model or '-'}"))
        model_groups = groups_by_model[model]
        blocks.append(heading("Category inventory"))
        blocks.append(
            build_category_inventory_table(
                model_groups, style, totals_by_model.get(model)
            )
        )
        blocks.append(heading("Metrics (mean ± std)"))
        blocks.append(
            build_transposed_category_metrics_table(
                model_groups, groups[model], specs, style
            )
        )
        if show_delta:
            if reference_sweep_name:
                sweep_runs = sweep_runs_by_model.get(model)
                if sweep_runs:
                    table = build_transposed_category_delta_vs_sweep_table(
                        model_groups,
                        reference_sweep_name,
                        sweep_runs,
                        delta_specs,
                        ddof,
                        style,
                    )
                    if table is not None:
                        delta_heading = (
                            f"Delta vs sweep {reference_sweep_name} (paired by "
                            f"seed; negative = lower than the reference sweep)"
                        )
                        blocks.append(heading(delta_heading))
                        blocks.append(table)
            else:
                reference = resolve_category_reference(
                    model_groups, requested_reference
                )
                if reference is not None:
                    table = build_transposed_category_delta_table(
                        model_groups, reference, delta_specs, ddof, style
                    )
                    if table is not None:
                        delta_heading = (
                            f"Delta vs {reference.label} (unpaired mean "
                            f"difference +/- SE; category groups overlap so a "
                            f"within-seed pairing is not well-defined)"
                        )
                        blocks.append(heading(delta_heading))
                        blocks.append(table)
    print()
    print("\n\n".join(blocks))


# --------------------------------------------------------------------------- #
# Table D -- within-category arm contrast (paired by seed)
# --------------------------------------------------------------------------- #
#
# The seed is the randomization unit: ``run.py`` sets
# ``simulation.seed`` and ``HospitalEnvironment`` seeds its RNG from it, so the
# same seed is the SAME patient scenario in every sweep and arm. Two arms of one
# category at one seed therefore form a genuine common-random-numbers pair, and
# the within-category arm contrast (more rounds - fewer rounds) is the only
# paired by-category comparison that is well defined.


def _arm_rounds(sweep_runs: list[RunData]) -> float | None:
    """Median ``pre_coordination_rounds`` over a sweep's runs, or ``None``."""
    values: list[float] = []
    for run in sweep_runs:
        run_config = run.run_config
        if not isinstance(run_config, dict):
            continue
        value = run_config.get("pre_coordination_rounds")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return statistics.median(values) if values else None


def _category_arms(
    category_runs: list[RunData],
) -> list[tuple[str, list[RunData], float | None]]:
    """Split a category's runs into arms, ordered fewest -> most rounds.

    Arms are ordered by the median ``pre_coordination_rounds`` of their runs;
    arms whose rounds are unknown sort first, and ties (including the all-unknown
    fallback) are broken by the sweep name. The last arm is the "more rounds"
    arm. Generic: never keyed to specific sweep names.
    """
    by_sweep: dict[str, list[RunData]] = {}
    for run in category_runs:
        by_sweep.setdefault(run.sweep, []).append(run)
    arms = [(sweep, runs, _arm_rounds(runs)) for sweep, runs in by_sweep.items()]
    arms.sort(key=lambda arm: (-math.inf if arm[2] is None else arm[2], arm[0]))
    return arms


def _arm_pairs(
    arms: list[tuple[str, list[RunData], float | None]],
) -> list[tuple[tuple[str, list[RunData], float | None], ...]]:
    """All unordered arm pairs, each ``(lower_rounds_arm, higher_rounds_arm)``."""
    return [
        (arms[i], arms[j]) for i in range(len(arms)) for j in range(i + 1, len(arms))
    ]


def _seed_values(spec: MetricSpec, runs: list[RunData]) -> dict[int, float]:
    """Map ``seed -> value`` (first write wins) for the runs with a value."""
    out: dict[int, float] = {}
    for run in runs:
        seed = run.seed()
        if seed is None or seed in out:
            continue
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if value is not None:
            out[seed] = value
    return out


def _binomial_two_sided(only_a: int, only_b: int) -> float:
    """Exact two-sided sign/binomial test on ``n = only_a + only_b`` pairs.

    ``p = min(1, 2 * sum_{j=0..k} C(n, j) * 0.5**n)`` with ``k = min`` of the
    two discordant counts (McNemar-style on the discordant pairs).
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, j) for j in range(k + 1)) * (0.5**n)
    return min(1.0, 2.0 * tail)


def compute_arm_contrasts(
    groups: list[CategoryGroup], specs: list[MetricSpec], ddof: int = 1
) -> list[dict[str, Any]]:
    """Paired-by-seed within-category arm contrasts, one row per (category, metric).

    For every category group whose runs span two or more distinct sweeps, pair
    the arms by seed and report ``(more rounds) - (fewer rounds)`` per metric.
    A category spanning more than two sweeps yields one row per arm pair (the
    label distinguishes them). Each row carries the one-sample two-sided t test
    vs 0 (:func:`sweep_significance.one_sample_t`) and the Wilcoxon
    signed-rank p (:func:`sweep_significance.wilcoxon_vs_zero`). ``p_holm`` is
    the Holm-Bonferroni adjustment across the categories within each metric.
    """
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group.category_id is None:  # reference-sweep pseudo group
            continue
        arms = _category_arms(group.runs)
        if len(arms) < 2:
            continue
        for lower, higher in _arm_pairs(arms):
            lower_sweep, lower_runs, _ = lower
            higher_sweep, higher_runs, _ = higher
            for spec in specs:
                lower_by_seed = _seed_values(spec, lower_runs)
                higher_by_seed = _seed_values(spec, higher_runs)
                paired = sorted(set(lower_by_seed) & set(higher_by_seed))
                diffs = [higher_by_seed[seed] - lower_by_seed[seed] for seed in paired]
                mean_d, sd_d, n_d = sig.mean_and_std(diffs)
                t_stat, df, p_value = sig.one_sample_t(diffs)
                _, p_wilcoxon = sig.wilcoxon_vs_zero(diffs)
                rows.append(
                    {
                        "category": group.label,
                        "category_id": group.category_id,
                        "category_slug": group.sweep,
                        "higher_sweep": higher_sweep,
                        "lower_sweep": lower_sweep,
                        "metric": spec.key,
                        "metric_label": spec.label,
                        "n": n_d,
                        "mean_diff": mean_d,
                        "sd": sd_d,
                        "t": t_stat,
                        "df": df,
                        "p_value": p_value,
                        "p_wilcoxon": p_wilcoxon,
                    }
                )

    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_metric.setdefault(str(row["metric"]), []).append(row)
    for recs in by_metric.values():
        adjusted = sig.holm_bonferroni([float(r["p_value"]) for r in recs])
        for record, p_holm in zip(recs, adjusted):
            record["p_holm"] = p_holm
    return rows


def compute_arm_membership(
    groups: list[CategoryGroup],
    sweep_seeds: dict[str, set[int]] | None = None,
) -> list[dict[str, Any]]:
    """Per-category arm membership counts over seeds, with exact binomial p.

    One row per category (per arm pair when a category spans >2 sweeps). ``A`` is
    the more-rounds arm and ``B`` the fewer-rounds arm. ``neither`` is computed
    against the union of the two sweeps' full seed sets when ``sweep_seeds`` is
    given (else 0, since the universe is unknown).
    """
    sweep_seeds = sweep_seeds or {}
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group.category_id is None:
            continue
        arms = _category_arms(group.runs)
        if len(arms) < 2:
            continue
        for lower, higher in _arm_pairs(arms):
            lower_sweep, lower_runs, _ = lower
            higher_sweep, higher_runs, _ = higher
            lower_seeds = {r.seed() for r in lower_runs if r.seed() is not None}
            higher_seeds = {r.seed() for r in higher_runs if r.seed() is not None}
            both = lower_seeds & higher_seeds
            only_higher = higher_seeds - lower_seeds
            only_lower = lower_seeds - higher_seeds
            universe = sweep_seeds.get(higher_sweep, set()) | sweep_seeds.get(
                lower_sweep, set()
            )
            if not universe:
                universe = lower_seeds | higher_seeds
            neither = universe - (lower_seeds | higher_seeds)
            rows.append(
                {
                    "category": group.label,
                    "category_slug": group.sweep,
                    "higher_sweep": higher_sweep,
                    "lower_sweep": lower_sweep,
                    "only_higher": len(only_higher),
                    "only_lower": len(only_lower),
                    "both": len(both),
                    "neither": len(neither),
                    "n_discordant": len(only_higher) + len(only_lower),
                    "p_binomial": _binomial_two_sided(
                        len(only_higher), len(only_lower)
                    ),
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# File outputs
# --------------------------------------------------------------------------- #


def write_category_csv(
    path: str,
    specs: list[MetricSpec],
    models: list[str],
    groups_by_model: dict[str, list[CategoryGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "model",
                "source",
                "source_path",
                "category_id",
                "category",
                "category_slug",
                "sweeps",
                "metric",
                "label",
                "n",
                "n_missing",
                "mean",
                "std",
                "min",
                "max",
            ]
        )
        for model in models:
            for group in groups_by_model[model]:
                metrics = groups[model][group.label]
                for spec in specs:
                    agg = metrics.aggregates[spec.key]
                    writer.writerow(
                        [
                            model,
                            "; ".join(group.sources),
                            "; ".join(group.source_paths),
                            "" if group.category_id is None else group.category_id,
                            group.label,
                            group.sweep,
                            "; ".join(group.sweeps),
                            spec.key,
                            spec.label,
                            agg.n,
                            agg.n_missing,
                            "" if agg.mean is None else repr(agg.mean),
                            "" if agg.std is None else repr(agg.std),
                            "" if agg.min is None else repr(agg.min),
                            "" if agg.max is None else repr(agg.max),
                        ]
                    )


def write_category_json(
    path: str,
    roots_abs: list[str],
    sources: list[dict[str, Any]],
    ddof: int,
    runs_found: int,
    runs_used: int,
    specs: list[MetricSpec],
    groups_by_model: dict[str, list[CategoryGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
    excluded_runs: list[tuple[RunData, list[str]]] | None = None,
    exclude_failed_runs: bool = False,
    arm_contrast: list[dict[str, Any]] | None = None,
    arm_membership: list[dict[str, Any]] | None = None,
) -> None:
    payload = {
        "root": roots_abs[0],
        "roots": list(roots_abs),
        "sources": [
            {
                "label": source["label"],
                "short": source["short"],
                "path": source["path"],
                "n_runs": source["n_runs"],
            }
            for source in sources
        ],
        "ddof": ddof,
        "runs_found": runs_found,
        "runs_used": runs_used,
        "excluded_runs": [
            {
                "model": run.model,
                "sweep": run.sweep,
                "seed": run.seed(),
                "run_id": run.run_id,
                "agents": list(agents),
                "reason": smt._exclusion_reason(run, exclude_failed_runs),
            }
            for run, agents in (excluded_runs or [])
        ],
        "groups": {
            model: {
                group.sweep: {
                    "category_id": group.category_id,
                    "category": group.label,
                    "label": group.label,
                    "is_reference_sweep": group.category_id is None,
                    "sweeps": list(group.sweeps),
                    "sources": list(group.sources),
                    "source_labels": list(group.source_labels),
                    "source_paths": list(group.source_paths),
                    "n_runs": groups[model][group.label].n_runs,
                    "metrics": {
                        spec.key: {
                            "label": spec.label,
                            "n": groups[model][group.label].aggregates[spec.key].n,
                            "n_missing": groups[model][group.label]
                            .aggregates[spec.key]
                            .n_missing,
                            "mean": groups[model][group.label]
                            .aggregates[spec.key]
                            .mean,
                            "std": groups[model][group.label].aggregates[spec.key].std,
                            "min": groups[model][group.label].aggregates[spec.key].min,
                            "max": groups[model][group.label].aggregates[spec.key].max,
                        }
                        for spec in specs
                    },
                }
                for group in groups_by_model[model]
            }
            for model in groups_by_model
        },
        "arm_contrast": list(arm_contrast or []),
        "arm_membership": list(arm_membership or []),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    print(
        "error: this module is a library; use "
        "`python -m experiments.collusion_via_tools.analysis.category_tables`",
        file=sys.stderr,
    )
    sys.exit(2)
