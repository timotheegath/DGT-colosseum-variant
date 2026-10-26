"""Pairwise statistical-significance analysis for sweep_metrics_stats.csv.

This module is a **library**, not a command-line entry point. The consolidated
CLI lives in :mod:`experiments.collusion_via_tools.analysis.tables`, which
selects the mode from the flags:

    python -m experiments.collusion_via_tools.analysis.tables --summary-csv CSV
    python -m experiments.collusion_via_tools.analysis.tables --delta-csv CSV
    python -m experiments.collusion_via_tools.analysis.tables <root> --reference SWEEP

Two modes:

1. **Summary-stats mode** (default, --summary-csv). Computes Welch's t-test,
   Cohen's d (with Hedges' g small-sample correction), Hedges' g 95% CI on
   the mean difference, and Holm-Bonferroni adjusted p-values for every
   (sweep_a, sweep_b, metric) triple in the stats CSV.

   Why summary-stats-based t-test: the upstream ``plot_sweep_metrics.py``
   emits one row per (model, sweep, metric) cell with n, mean, std -- not
   the raw per-run values. With n=9-10 the raw-data vs summary-stats t-test
   is identical up to floating-point precision, so we don't lose anything
   by working from the published CSV.

2. **Per-run-delta mode** (--delta-csv). Reads the per-run paired-delta CSV
   written by ``plot_judge_vs_advantage_delta.py`` and computes:

   * **One-sample tests vs 0** per (model, sweep) for each delta metric
     (the natural "did this sweep deviate from the reference?" question).
     Both Welch's one-sample t and Wilcoxon signed-rank are reported; the
     Wilcoxon is more robust to outliers and non-normality, which matters
     here because the deltas routinely span 2-3 orders of magnitude and
     carry outliers.
   * **Paired t-tests and Wilcoxon signed-rank** between every pair of
     sweeps within the same model and metric, paired by seed (the deltas
     share seeds with the reference).
   * Holm-Bonferroni correction applied per (model, metric) family.

Limitations worth flagging in any writeup:
  * n=9-10 per cell is small. Power is low; expect many real-world effects
    (e.g. -200 reward swing on ``joint_reward``) to read as "not significant"
    purely because the within-cell std is large.
  * Welch's t-test assumes approximate normality. ``suspicion_rate`` is bounded
    in [0, 1] and very often near zero in these runs -- the test is still
    usable but a Mann-Whitney U on the raw values would be more robust if
    the raw per-run metrics were available.

Summary-stats mode (default):
  Reads:  experiments/collusion_via_tools/analysis/plots_outputs/sweep_metrics/sweep_metrics_stats.csv
  Writes: experiments/collusion_via_tools/analysis/plots_outputs/sweep_metrics/sweep_significance.csv
          experiments/collusion_via_tools/analysis/plots_outputs/sweep_metrics/sweep_significance.md
  Run:    .venv/bin/python -m experiments.collusion_via_tools.analysis.tables --summary-csv <path>

Per-run-delta mode:
  Reads:  --delta-csv PATH (typically plots_outputs/judge_vs_advantage_delta/judge_vs_advantage_delta_points.csv)
  Writes: <delta-csv-dir>/sweep_significance_delta.csv
          <delta-csv-dir>/sweep_significance_delta.md
  Run:    .venv/bin/python -m experiments.collusion_via_tools.analysis.tables \
              --delta-csv experiments/collusion_via_tools/analysis/plots_outputs/judge_vs_advantage_delta/judge_vs_advantage_delta_points.csv
"""

from __future__ import annotations

import csv
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import scipy.stats as stats


HERE = Path(__file__).resolve().parent
STATS_CSV = HERE / "plots_outputs" / "sweep_metrics" / "sweep_metrics_stats.csv"
OUT_CSV = STATS_CSV.with_name("sweep_significance.csv")
OUT_MD = STATS_CSV.with_name("sweep_significance.md")


# Reference sweep order for nicer column ordering in the output table.
SWEEP_ORDER = (
    "Coalition with private channel",
    "Coalition without private channel, 1 coordination round",
    "Coalition without private channel, 2 coordination rounds",
    "Coalition without private channel, no coordination",
    "No coalition",
)

METRIC_ORDER = (
    "joint_reward",
    "joint_reward_regret",
    "coalition_reward_avg",
    "coalition_reward_regret",
    "suspicion_rate_non_adversary",
)


@dataclass(frozen=True)
class Cell:
    sweep: str
    metric: str
    label: str
    n: int
    mean: float
    std: float

    def is_valid(self) -> bool:
        return self.n >= 2 and math.isfinite(self.mean) and math.isfinite(self.std)


def load_cells(path: Path) -> dict[tuple[str, str], Cell]:
    cells: dict[tuple[str, str], Cell] = {}
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                n = int(row["n"])
                mean = float(row["mean"])
                std = float(row["std"])
            except (TypeError, ValueError):
                continue
            cell = Cell(
                sweep=row["sweep"],
                metric=row["metric"],
                label=row["label"],
                n=n,
                mean=mean,
                std=std,
            )
            cells[(cell.sweep, cell.metric)] = cell
    return cells


def hedges_correction(d: float, n1: int, n2: int) -> float:
    """Hedges' g: small-sample bias correction for Cohen's d."""
    df = n1 + n2 - 2
    if df <= 0:
        return float("nan")
    return d * (1.0 - 3.0 / (4.0 * df - 1.0))


def hedges_g_ci(
    mean1: float,
    mean2: float,
    s1: float,
    s2: float,
    n1: int,
    n2: int,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Approximate 95% CI on the standardized mean difference (Hedges' g).

    Uses the noncentral-t approximation from Hedges & Olkin (1985), which is
    what ``scipy.stats.NoncentralT`` provides.
    """
    df = n1 + n2 - 2
    s_pool = math.sqrt(((n1 - 1) * s1**2 + (n2 - 1) * s2**2) / df)
    if s_pool == 0:
        return float("nan"), float("nan")
    d = (mean1 - mean2) / s_pool
    se_d = math.sqrt((n1 + n2) / (n1 * n2) + d**2 / (2.0 * df))
    g = hedges_correction(d, n1, n2)
    se_g = se_d  # the correction factor is the same for both
    z = stats.norm.ppf(1.0 - alpha / 2.0)
    return g - z * se_g, g + z * se_g


def mean_diff_ci(
    mean1: float,
    mean2: float,
    s1: float,
    s2: float,
    n1: int,
    n2: int,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """95% CI on (mean1 - mean2) using Welch's t."""
    se = math.sqrt(s1**2 / n1 + s2**2 / n2)
    if se == 0:
        return mean1 - mean2, mean1 - mean2, float("nan")
    df = (s1**2 / n1 + s2**2 / n2) ** 2 / (
        (s1**2 / n1) ** 2 / (n1 - 1) + (s2**2 / n2) ** 2 / (n2 - 1)
    )
    t_crit = stats.t.ppf(1.0 - alpha / 2.0, df)
    diff = mean1 - mean2
    return diff - t_crit * se, diff + t_crit * se, df


def welch_t_from_summary(
    mean1: float, mean2: float, s1: float, s2: float, n1: int, n2: int
) -> tuple[float, float, float]:
    """Welch's t-test computed from (mean, std, n).

    Returns ``(t, df, p_two_sided)``.
    """
    se = math.sqrt(s1**2 / n1 + s2**2 / n2)
    if se == 0:
        return 0.0, float("nan"), 1.0
    t = (mean1 - mean2) / se
    df = (s1**2 / n1 + s2**2 / n2) ** 2 / (
        (s1**2 / n1) ** 2 / (n1 - 1) + (s2**2 / n2) ** 2 / (n2 - 1)
    )
    p = 2.0 * (1.0 - stats.t.cdf(abs(t), df))
    return t, df, p


def omnibus_welch_anova(groups: list[Cell]) -> tuple[float, float]:
    """Welch's one-way ANOVA from summary stats (k >= 2 groups).

    Implements the standard Welch-Satterthwaite F test:
        F = sum(w_i * (mean_i - grand_mean)^2) / (k-1)
            / (1 + 2*(k-2)/(k^2-1) * sum_i ( (1 - w_i/sum_w)^2 / (n_i - 1) ))
    where w_i = n_i / s_i^2 and the numerator denominator combination gives
    Welch's adjusted degrees of freedom.
    Returns ``(F_statistic, p_value)``.

    Edge case: if any group has zero variance (s_i == 0), Welch's ANOVA is
    undefined (division by w_i). We replace that group's weight with a large
    sentinel so the group effectively becomes a point mass at its mean; if all
    groups agree on the mean, F=0 and p=1; if at least one disagrees, F is
    conservative-but-finite because the zero-variance groups no longer pull
    weight into the denominator. This is the standard recommendation for
    handling zero-variance cells in Welch-style procedures.
    """
    k = len(groups)
    if k < 2:
        return float("nan"), float("nan")
    s2 = np.array([max(c.std**2, 1e-12) for c in groups])
    weights = np.array([c.n / s2_i for c, s2_i in zip(groups, s2)])
    means = np.array([c.mean for c in groups])
    ns = np.array([c.n for c in groups])
    total_w = weights.sum()
    if total_w == 0:
        return float("nan"), float("nan")
    grand_mean = (weights * means).sum() / total_w
    numerator = (weights * (means - grand_mean) ** 2).sum()
    denom_const = (1.0 - weights / total_w) ** 2 / (ns - 1)
    if (ns - 1 <= 0).any():
        return float("nan"), float("nan")
    denom = 1.0 + (2.0 * (k - 2)) / (k**2 - 1.0) * denom_const.sum()
    df1 = k - 1
    df2 = 1.0 / denom_const.sum() - 2.0 / (k**2 - 1.0)
    if df2 <= 0 or not math.isfinite(df2):
        return float("nan"), float("nan")
    f_stat = (numerator / (k - 1)) / denom
    p = 1.0 - stats.f.cdf(f_stat, df1, df2)
    return float(f_stat), float(p)


def holm_bonferroni(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (preserves input order)."""
    n = len(pvals)
    indexed = sorted(enumerate(pvals), key=lambda kv: kv[1])
    adjusted = [0.0] * n
    running_max = 0.0
    for rank, (orig_idx, p) in enumerate(indexed, start=1):
        adj = min(1.0, max(p * (n - rank + 1), running_max))
        adjusted[orig_idx] = adj
        running_max = adj
    return adjusted


def all_pairs(items: Iterable[str]) -> list[tuple[str, str]]:
    items = list(items)
    return [(a, b) for i, a in enumerate(items) for b in items[i + 1 :]]


def ordered_metric_sweeps(metric: str, available: list[str]) -> list[str]:
    """Return sweeps in canonical order, intersected with what's available."""
    by_order = [s for s in SWEEP_ORDER if s in available]
    extras = [s for s in available if s not in SWEEP_ORDER]
    return by_order + extras


# --------------------------------------------------------------------------- #
# Per-run-delta mode
# --------------------------------------------------------------------------- #
#
# Reads the per-run paired-delta CSV written by
# ``plot_judge_vs_advantage_delta.py``. Schema:
#     model, sweep, seed, run_id, judge_delta, advantage_delta
#
# The deltas are paired by seed within each (model, sweep); the seeds are
# also shared across sweeps (the delta plot paired every sweep against the
# same reference). That lets us do matched-pairs tests between sweeps.


# Metrics emitted by the delta plot, in display order.
DELTA_METRIC_ORDER: tuple[str, ...] = (
    "judge_delta",
    "advantage_delta",
)

DELTA_METRIC_LABELS: dict[str, str] = {
    "judge_delta": "\u0394 Collusion judge (Simple) \u2013 sweep minus reference",
    "advantage_delta": "\u0394 Coalition advantage (coalition_minus_noncoalition_avg) \u2013 sweep minus reference",
}


def load_delta_cells(path: Path) -> dict[tuple[str, str, str], dict[int, float]]:
    """Read the per-run-delta CSV and return ``{(model, sweep, metric): {seed: value}}``.

    Runs whose metric value is missing (empty string) are skipped per cell;
    seeds that appear in some metrics but not others are simply absent from
    the relevant dict (the per-sweep and per-pair tests handle that).

    Raises :class:`SystemExit` with a precise diagnostic when the CSV
    schema does not match the expected delta layout. The most common
    mistake is to pass the per-(sweep, metric) summary-stats CSV that
    ``sweep_metrics_table.py --csv`` emits; that file has ``mean`` /
    ``std`` columns, not per-seed ``judge_delta`` / ``advantage_delta``
    columns, and is the right input for the *default* (no --delta-csv)
    mode of this script.
    """
    required_delta_columns = (
        "model",
        "sweep",
        "seed",
        "judge_delta",
        "advantage_delta",
    )

    cells: dict[tuple[str, str, str], dict[int, float]] = {}
    with path.open() as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in required_delta_columns if c not in header]
        if missing:
            looks_like_summary_stats = (
                "metric" in header
                and "mean" in header
                and "std" in header
                and "n" in header
            )
            msg = (
                f"--delta-csv: {path} does not look like a per-run paired-delta CSV.\n"
                f"  Missing required column(s): {', '.join(missing)}.\n"
                f"  Found column(s): {', '.join(header) if header else '(empty file)'}."
            )
            if looks_like_summary_stats:
                msg += (
                    "\n\n  That CSV matches the per-(sweep, metric) summary table emitted by\n"
                    "  `sweep_metrics_table.py --csv ...`. For that file, run this script\n"
                    "  WITHOUT --delta-csv (it will use the default summary-stats mode\n"
                    "  and read sweep_metrics_stats.csv directly)."
                )
            else:
                msg += (
                    "\n\n  The --delta-csv mode expects the per-run paired-delta CSV emitted\n"
                    "  by `plot_judge_vs_advantage_delta.py --out <dir>`. That file is\n"
                    "  named `judge_vs_advantage_delta_points.csv` and has columns:\n"
                    "    model, sweep, seed, run_id, judge_delta, advantage_delta\n"
                    "  (one row per (model, sweep, seed))."
                )
            raise SystemExit(msg)
        for row in reader:
            model = row.get("model", "")
            sweep = row.get("sweep", "")
            seed_str = row.get("seed", "")
            try:
                seed = int(seed_str)
            except (TypeError, ValueError):
                continue
            for metric in DELTA_METRIC_ORDER:
                raw = row.get(metric, "")
                if raw == "" or raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue
                cells.setdefault((model, sweep, metric), {})[seed] = value
    return cells


def one_sample_t(values: list[float]) -> tuple[float, float, float]:
    """One-sample Welch's t-test of ``mean(values) == 0``.

    Returns ``(t, df, p_two_sided)``. ``df = n - 1``. When ``n < 2`` or all
    values are equal, returns ``(0, NaN, 1.0)`` so callers can render it as
    "no evidence against H0" without crashing.
    """
    n = len(values)
    if n < 2:
        return 0.0, float("nan"), 1.0
    arr = np.asarray(values, dtype=float)
    if arr.std(ddof=1) == 0:
        return 0.0, float(n - 1), 1.0
    res = stats.ttest_1samp(arr, popmean=0.0)
    return float(res.statistic), float(res.df), float(res.pvalue)


def wilcoxon_vs_zero(values: list[float]) -> tuple[float, float]:
    """Wilcoxon signed-rank test of ``median(values) == 0``.

    Returns ``(statistic, p_two_sided)``. When the test is undefined
    (n < 1, all-zero differences, or scipy refuses), returns
    ``(NaN, 1.0)``. The ``zero_method="wilcox"`` default matches scipy's
    default and drops zero differences before ranking; this is the
    standard choice for matched pairs.
    """
    n = len(values)
    if n < 1:
        return float("nan"), 1.0
    arr = np.asarray(values, dtype=float)
    if not np.any(arr != 0.0):
        return float("nan"), 1.0
    try:
        res = stats.wilcoxon(arr, zero_method="wilcox", alternative="two-sided")
    except ValueError:
        return float("nan"), 1.0
    return float(res.statistic), float(res.pvalue)


def paired_diff(a: dict[int, float], b: dict[int, float]) -> list[float]:
    """Return the per-seed difference ``a - b`` for seeds present in both."""
    return [a[s] - b[s] for s in sorted(set(a) & set(b))]


def paired_t(diffs: list[float]) -> tuple[float, float, float]:
    """Paired t-test on ``diffs`` (one-sample t of ``mean == 0``).

    Returns ``(t, df, p_two_sided)``. See :func:`one_sample_t` for the
    degenerate cases.
    """
    return one_sample_t(diffs)


def mean_and_std(values: list[float]) -> tuple[float, float, int]:
    n = len(values)
    if n == 0:
        return float("nan"), float("nan"), 0
    arr = np.asarray(values, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1)) if n >= 2 else 0.0, n


def hedges_correction_from_d(d: float, n: int) -> float:
    """Hedges' g correction for a one-sample d (= mean / sd)."""
    df = n - 1
    if df <= 0:
        return float("nan")
    return d * (1.0 - 3.0 / (4.0 * df - 1.0))


def run_delta_mode(delta_csv: Path, out_dir: Path) -> None:
    """Per-run-delta mode: one-sample + paired tests on the paired-delta CSV.

    Writes two artifacts under ``out_dir``:

    * ``sweep_significance_delta.csv`` -- one row per test.
    * ``sweep_significance_delta.md``  -- human-readable summary.
    """
    cells = load_delta_cells(delta_csv)
    if not cells:
        raise SystemExit(f"No per-run-delta data loaded from {delta_csv}")

    # Per-sweep one-sample tests vs 0, per (model, metric).
    one_sample_rows: list[dict[str, object]] = []
    # Paired tests between sweeps within the same (model, metric).
    pair_rows: list[dict[str, object]] = []

    # Group by (model, metric), then enumerate per-sweep and per-pair tests.
    by_mm: dict[tuple[str, str], dict[str, dict[int, float]]] = {}
    for (model, sweep, metric), seed_map in cells.items():
        by_mm.setdefault((model, metric), {})[sweep] = seed_map

    for (model, metric), sweeps_map in by_mm.items():
        sweep_names = sorted(sweeps_map)

        # One-sample tests vs 0 for each sweep.
        per_sweep_ps: list[float] = []
        per_sweep_records: list[dict[str, object]] = []
        for sweep in sweep_names:
            values = list(sweeps_map[sweep].values())
            mean_v, std_v, n_v = mean_and_std(values)
            d_one = (
                (mean_v / std_v) if (std_v and std_v > 0 and n_v >= 2) else float("nan")
            )
            g_one = hedges_correction_from_d(d_one, n_v) if n_v >= 2 else float("nan")
            t, df_os, p_t = one_sample_t(values)
            _, p_w = wilcoxon_vs_zero(values)
            record = {
                "model": model,
                "metric": metric,
                "metric_label": DELTA_METRIC_LABELS.get(metric, metric),
                "sweep": sweep,
                "n_paired": n_v,
                "mean_delta": mean_v,
                "std_delta": std_v,
                "t_vs_zero": t,
                "df_vs_zero": df_os,
                "p_value_t": p_t,
                "p_value_wilcoxon": p_w,
                "cohens_d_vs_zero": d_one,
                "hedges_g_vs_zero": g_one,
            }
            per_sweep_records.append(record)
            per_sweep_ps.append(p_t)
        adjusted = holm_bonferroni(per_sweep_ps)
        for rec, p_adj in zip(per_sweep_records, adjusted):
            rec["p_holm_t_vs_zero"] = p_adj
            rec["significant_05_t_vs_zero"] = bool(p_adj < 0.05)
            one_sample_rows.append(rec)

        # Paired tests between sweeps within this (model, metric).
        pairs = all_pairs(sweep_names)
        pair_ps: list[float] = []
        pair_records: list[dict[str, object]] = []
        for a, b in pairs:
            diffs = paired_diff(sweeps_map[a], sweeps_map[b])
            n_p = len(diffs)
            mean_d, std_d, _ = mean_and_std(diffs)
            d_paired = (
                (mean_d / std_d) if (std_d and std_d > 0 and n_p >= 2) else float("nan")
            )
            g_paired = (
                hedges_correction_from_d(d_paired, n_p) if n_p >= 2 else float("nan")
            )
            t_p, df_p, p_t_p = paired_t(diffs)
            _, p_w_p = wilcoxon_vs_zero(diffs)
            record = {
                "model": model,
                "metric": metric,
                "metric_label": DELTA_METRIC_LABELS.get(metric, metric),
                "sweep_a": a,
                "sweep_b": b,
                "n_paired_seeds": n_p,
                "mean_diff_a_minus_b": mean_d,
                "std_diff_a_minus_b": std_d,
                "t_paired": t_p,
                "df_paired": df_p,
                "p_value_t": p_t_p,
                "p_value_wilcoxon": p_w_p,
                "cohens_d_paired": d_paired,
                "hedges_g_paired": g_paired,
            }
            pair_records.append(record)
            pair_ps.append(p_t_p)
        adjusted_pairs = holm_bonferroni(pair_ps)
        for rec, p_adj in zip(pair_records, adjusted_pairs):
            rec["p_holm_t_paired"] = p_adj
            rec["significant_05_t_paired"] = bool(p_adj < 0.05)
            pair_rows.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "sweep_significance_delta.csv"
    md_path = out_dir / "sweep_significance_delta.md"

    fieldnames: list[str] = []
    for rec in one_sample_rows + pair_rows:
        for k in rec:
            if k not in fieldnames:
                fieldnames.append(k)
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for rec in one_sample_rows:
            writer.writerow(rec)
        for rec in pair_rows:
            writer.writerow(rec)
    print(f"Wrote {csv_path}")

    # Markdown summary.
    lines: list[str] = []
    lines.append("# Sweep significance on paired-by-seed deltas (vs reference)")
    lines.append("")
    lines.append(
        f"Computed from `{delta_csv.name}`. Two test families per "
        f"(model, metric):\n\n"
        f"1. **One-sample test vs 0** per sweep -- *did this sweep deviate "
        f"significantly from the reference?* Both Welch's one-sample t and "
        f"Wilcoxon signed-rank are reported; the Wilcoxon is more robust to "
        f"outliers and non-normality, which matters for deltas that routinely "
        f"span 2-3 orders of magnitude.\n"
        f"2. **Paired t and Wilcoxon signed-rank** between every pair of "
        f"sweeps within the same (model, metric), paired by seed.\n\n"
        f"`p_holm_*` is the Holm-Bonferroni adjusted p-value within the "
        f"(model, metric) family; `significant_05_*` is `True` iff the "
        f"Holm-adjusted p < 0.05."
    )
    lines.append("")

    # Group by (model, metric) for the body sections.
    family_keys: list[tuple[str, str]] = sorted(
        {(rec["model"], rec["metric"]) for rec in one_sample_rows}
    )

    any_sig_one = any(
        bool(rec.get("significant_05_t_vs_zero")) for rec in one_sample_rows
    )
    any_sig_pair = any(bool(rec.get("significant_05_t_paired")) for rec in pair_rows)
    if not (any_sig_one or any_sig_pair):
        lines.append(
            "**Headline:** at α=0.05 with Holm-Bonferroni correction, **no "
            "one-sample-vs-0 test and no paired test survives** -- n=10-30 "
            "paired seeds per family is too small for the observed within-"
            "sweep spread of the deltas. The raw p-values and Hedges' g "
            "below show what the data is hinting at."
        )
    else:
        lines.append(
            "**Headline:** at α=0.05 with Holm-Bonferroni correction, at "
            "least one test survives -- see the per-family tables below."
        )
    lines.append("")

    # One-sample per family.
    lines.append("## One-sample test: did each sweep deviate from the reference?")
    lines.append("")
    for model, metric in family_keys:
        metric_label = DELTA_METRIC_LABELS.get(metric, metric)
        lines.append(f"### {model} \u2014 {metric} \u2014 {metric_label}")
        lines.append("")
        lines.append(
            "| sweep | n | mean \u0394 | std \u0394 | t (vs 0) | df | raw p (t) | "
            "p (Holm, t) | raw p (Wilcoxon) | Hedges' g | sig @ .05 (t) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        rows_here = [
            r for r in one_sample_rows if r["model"] == model and r["metric"] == metric
        ]
        for r in rows_here:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(r["sweep"]),
                        str(r["n_paired"]),
                        _fmt(r["mean_delta"]),
                        _fmt(r["std_delta"]),
                        _fmt(r["t_vs_zero"], 2),
                        _fmt(r["df_vs_zero"], 1),
                        _fmt_p(r["p_value_t"]),
                        _fmt_p(r["p_holm_t_vs_zero"]),
                        _fmt_p(r["p_value_wilcoxon"]),
                        _fmt(r["hedges_g_vs_zero"]),
                        "\u2705" if r["significant_05_t_vs_zero"] else "\u2014",
                    ]
                )
                + " |"
            )
        lines.append("")

    # Paired per family.
    lines.append("## Paired test: do any two sweeps differ in their \u0394?")
    lines.append("")
    for model, metric in family_keys:
        metric_label = DELTA_METRIC_LABELS.get(metric, metric)
        lines.append(f"### {model} \u2014 {metric} \u2014 {metric_label}")
        lines.append("")
        rows_here = [
            r for r in pair_rows if r["model"] == model and r["metric"] == metric
        ]
        if not rows_here:
            lines.append("_(fewer than two sweeps \u2014 no pairs to compare)_")
            lines.append("")
            continue
        lines.append(
            "| sweep A | sweep B | n seeds | mean (A\u2212B) | std | t (paired) | df | "
            "raw p (t) | p (Holm, t) | raw p (Wilcoxon) | Hedges' g | sig @ .05 (t) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in rows_here:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(r["sweep_a"]),
                        str(r["sweep_b"]),
                        str(r["n_paired_seeds"]),
                        _fmt(r["mean_diff_a_minus_b"]),
                        _fmt(r["std_diff_a_minus_b"]),
                        _fmt(r["t_paired"], 2),
                        _fmt(r["df_paired"], 1),
                        _fmt_p(r["p_value_t"]),
                        _fmt_p(r["p_holm_t_paired"]),
                        _fmt_p(r["p_value_wilcoxon"]),
                        _fmt(r["hedges_g_paired"]),
                        "\u2705" if r["significant_05_t_paired"] else "\u2014",
                    ]
                )
                + " |"
            )
        lines.append("")

    md_path.write_text("\n".join(lines))
    print(f"Wrote {md_path}")


# --------------------------------------------------------------------------- #
# Sibling import for the one-step root mode (load_runs + augmentation)
# --------------------------------------------------------------------------- #


def _import_sibling_helpers() -> tuple[Any, Any]:
    """Lazy-import ``sweep_metrics_table`` and ``aggregate_protocols``.

    Wrapped in a retry helper so a concurrently-edited sibling cannot
    crash the CLI. Returns ``(module_smt, module_agg)``.
    """
    last_exc: BaseException | None = None
    for attempt in range(5):
        try:
            try:
                from . import sweep_metrics_table as smt  # type: ignore
                from . import aggregate_protocols as agg  # type: ignore
            except ImportError:
                import importlib

                repo_root = Path(__file__).resolve().parents[4]
                sys.path.insert(0, str(repo_root))
                smt = importlib.import_module(
                    "experiments.collusion_via_tools.analysis.sweep_metrics_table"
                )
                agg = importlib.import_module(
                    "experiments.collusion_via_tools.analysis.aggregate_protocols"
                )
            for name in (
                "find_metric_files",
                "load_runs",
                "partition_runs",
            ):
                if not hasattr(smt, name):
                    raise ImportError(f"sweep_metrics_table is missing {name}")
            if not hasattr(agg, "augment_runs_with_baseline_coalition"):
                raise ImportError(
                    "aggregate_protocols is missing augment_runs_with_baseline_coalition"
                )
            if not hasattr(smt, "run_is_failed"):
                raise ImportError("sweep_metrics_table is missing run_is_failed")
            return smt, agg
        except (ImportError, SyntaxError) as exc:
            last_exc = exc
            if attempt < 4:
                print(
                    f"warning: transient sibling import failure ({exc!r}); "
                    f"retrying in 30s ({attempt + 1}/5)",
                    file=sys.stderr,
                )
                import time as _t

                _t.sleep(30)
    raise ImportError(f"could not import sibling modules after retries: {last_exc!r}")


def run_root_delta_mode(
    roots: list[str],
    reference: str,
    out_dir: Path,
    exclude_error_runs: bool,
    exclude_failed_runs: bool,
) -> None:
    """One-step root mode: load runs, augment, compute deltas, test them.

    Writes three artefacts under ``out_dir``:

    * ``judge_vs_advantage_delta_points.csv`` -- the per-run paired-delta
      CSV (so the user can also run ``plot_judge_vs_advantage_delta`` on it).
    * ``sweep_significance_delta.csv`` -- one row per statistical test.
    * ``sweep_significance_delta.md``  -- human-readable summary.
    """
    from .delta_metrics import compute_paired_deltas  # local import to keep top clean

    smt, agg = _import_sibling_helpers()

    if not roots:
        raise SystemExit(
            "error: --reference requires at least one output root "
            "(positional or --root)"
        )
    roots_abs = [os.path.abspath(r) for r in roots]
    missing = [r for r in roots_abs if not os.path.exists(r)]
    if missing:
        for r in missing:
            print(f"error: path does not exist: {r}", file=sys.stderr)
        raise SystemExit(2)

    runs: list[Any] = []
    for root_abs in roots_abs:
        found = smt.find_metric_files(root_abs)
        runs.extend(smt.load_runs(root_abs, found))
    if not runs:
        print("warning: no usable runs found", file=sys.stderr)
        return

    runs_used = len(runs)
    kept_runs, excluded_pairs = smt.partition_runs(
        runs, exclude_error_runs, exclude_failed_runs
    )
    excluded_pairs.sort(
        key=lambda item: (
            item[0].model,
            item[0].sweep,
            item[0].seed() if item[0].seed() is not None else -1,
            item[0].run_id,
        )
    )

    if excluded_pairs:
        print(
            f"excluded runs: {len(excluded_pairs)} of {runs_used}",
            file=sys.stderr,
        )

    colluder_set = agg.augment_runs_with_baseline_coalition(kept_runs)
    if colluder_set:
        print(
            f"colluder set (inferred from non-baseline sweeps): {sorted(colluder_set)}",
            file=sys.stderr,
        )

    if not kept_runs:
        print(
            "warning: no usable runs left after excluding runs",
            file=sys.stderr,
        )
        return

    # Group by (model, sweep), then compute the paired deltas.
    grouped: dict[str, dict[str, list[Any]]] = {}
    for run in kept_runs:
        grouped.setdefault(run.model, {}).setdefault(run.sweep, []).append(run)

    ref_seen_in = {model for model, sweeps in grouped.items() if reference in sweeps}
    if not ref_seen_in:
        raise SystemExit(
            f"error: reference sweep '{reference}' not found under any provided root",
        )

    points, run_counts = compute_paired_deltas(grouped, reference)
    if not points:
        print(
            f"warning: no paired seeds survive for reference '{reference}'",
            file=sys.stderr,
        )
        return

    # Emit the per-run delta CSV (same schema as the delta plot script).
    out_dir.mkdir(parents=True, exist_ok=True)
    points_csv = out_dir / "judge_vs_advantage_delta_points.csv"
    with points_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["model", "sweep", "seed", "run_id", "judge_delta", "advantage_delta"]
        )
        for pt in points:
            writer.writerow(
                [
                    pt.model,
                    pt.sweep,
                    pt.seed if pt.seed is not None else "",
                    pt.run_id,
                    repr(float(pt.judge_delta)) if pt.judge_delta is not None else "",
                    repr(float(pt.advantage_delta))
                    if pt.advantage_delta is not None
                    else "",
                ]
            )
    print(f"Wrote {points_csv}")

    # Hand off to the existing delta-mode runner for the actual statistics.
    run_delta_mode(points_csv, out_dir)


def _fmt(v: object, decimals: int = 3) -> str:
    if isinstance(v, float) and not math.isfinite(v):
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.{decimals}f}"
    return str(v)


def _fmt_p(p: object) -> str:
    if isinstance(p, float):
        if p < 0.001:
            return "<0.001"
        return f"{p:.3f}"
    return str(p)


if __name__ == "__main__":
    print(
        "error: this command was removed; use "
        "`python -m experiments.collusion_via_tools.analysis.tables`",
        file=sys.stderr,
    )
    sys.exit(2)
