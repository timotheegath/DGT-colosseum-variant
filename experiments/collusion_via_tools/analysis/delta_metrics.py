"""Shared helpers for the paired-by-seed delta analyses.

This module centralises the two pieces of math that the delta plot script
(``plot_judge_vs_advantage_delta.py``) and the delta-significance mode of
``sweep_significance.py --root --reference`` both need:

* the per-run metric accessors
  (``judge_score`` and ``coalition_advantage``), and
* the paired-by-seed delta computation.

Centralising them guarantees the two scripts always agree on what the
"delta against the reference" actually means -- same metrics, same
pairing, same arithmetic. It also means a single change here propagates
to both consumers.

The augmentation that supplies the baseline runs with synthetic
coalition metrics (so ``coalition_advantage`` is non-``None`` for them)
lives in :mod:`aggregate_protocols`; this module consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Metric accessors
# --------------------------------------------------------------------------- #


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def judge_score(metrics: Mapping[str, Any]) -> float | None:
    """Mean score from the simple LLM-judge rubric, or None.

    Reads ``collusion_judge_simple.mean_score`` (the live layout) and
    falls back to the flat ``collusion_judge_simple_mean`` field for
    older / hand-edited metrics files.
    """
    block = metrics.get("collusion_judge_simple")
    if isinstance(block, Mapping):
        return _safe_float(block.get("mean_score"))
    return _safe_float(metrics.get("collusion_judge_simple_mean"))


def coalition_advantage(metrics: Mapping[str, Any]) -> float | None:
    """Stored ``coalition_minus_noncoalition_avg``.

    For live collusion runs this is populated by the metrics pipeline.
    For benign baseline runs (``adversary_count == 0``) it must be
    populated in memory by
    :func:`aggregate_protocols.augment_baseline_coalition_metrics`
    before this function is called, using the same colluder set the
    collusion runs use.
    """
    return _safe_float(metrics.get("coalition_minus_noncoalition_avg"))


# --------------------------------------------------------------------------- #
# Paired-by-seed delta computation
# --------------------------------------------------------------------------- #


@dataclass
class DeltaPoint:
    """One paired (sweep, seed) delta vs the reference."""

    model: str
    sweep: str
    seed: int | None
    run_id: str
    judge_delta: float | None
    advantage_delta: float | None


def seed_to_metric(
    runs_for_sweep: Iterable[Any],
    metric_fn: Callable[[Mapping[str, Any]], float | None],
) -> dict[int, tuple[float, Any]]:
    """Map ``seed -> (value, run)`` for the runs that have a numeric value.

    Skips runs whose seed is ``None`` (they cannot be paired) and runs
    whose metric is ``None`` (they cannot contribute a delta). First-
    write-wins so the pairing stays stable when a sweep contains two
    runs with the same seed.
    """
    out: dict[int, tuple[float, Any]] = {}
    for run in runs_for_sweep:
        seed = run.seed()
        if seed is None:
            continue
        value = metric_fn(run.metrics)
        if value is None:
            continue
        if seed in out:
            continue
        out[seed] = (value, run)
    return out


def compute_paired_deltas(
    grouped: Mapping[str, Mapping[str, list[Any]]],
    reference: str,
) -> tuple[list[DeltaPoint], dict[str, dict[str, int]]]:
    """Compute the paired-by-seed delta points for one figure / table.

    ``grouped`` is the same ``{model: {sweep: [run, ...]}}`` shape the
    delta plot script builds. The reference sweep's runs supply the
    per-seed ``judge`` / ``advantage`` baselines; each non-reference
    sweep's runs contribute one :class:`DeltaPoint` per seed present in
    the intersection.

    Returns ``(points, run_counts)`` where ``run_counts[model][sweep]``
    is the number of paired seeds for that (model, sweep). Models that
    have no runs under the reference sweep are silently skipped.
    """
    points: list[DeltaPoint] = []
    run_counts: dict[str, dict[str, int]] = {}

    for model, sweeps in grouped.items():
        if reference not in sweeps:
            continue
        ref_runs = sweeps[reference]
        ref_judge = seed_to_metric(ref_runs, judge_score)
        ref_advantage = seed_to_metric(ref_runs, coalition_advantage)

        counts: dict[str, int] = {}
        for sweep, sweep_runs in sweeps.items():
            sweep_judge = seed_to_metric(sweep_runs, judge_score)
            sweep_advantage = seed_to_metric(sweep_runs, coalition_advantage)
            paired_seeds = sorted(
                set(sweep_judge)
                & set(sweep_advantage)
                & set(ref_judge)
                & set(ref_advantage)
            )
            counts[sweep] = len(paired_seeds)
            for seed in paired_seeds:
                _, sweep_run = sweep_judge[seed]
                points.append(
                    DeltaPoint(
                        model=model,
                        sweep=sweep,
                        seed=seed,
                        run_id=sweep_run.run_id,
                        judge_delta=sweep_judge[seed][0] - ref_judge[seed][0],
                        advantage_delta=sweep_advantage[seed][0]
                        - ref_advantage[seed][0],
                    )
                )
        run_counts[model] = counts

    return points, run_counts
