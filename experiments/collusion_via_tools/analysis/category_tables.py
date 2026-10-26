"""Category-grouped table + significance entry point for collusion_via_tools.

This is the **primary** CLI for the communication-category analysis. It is the
category-grouped counterpart of
:mod:`experiments.collusion_via_tools.analysis.tables`: same three tables
(A inventory, B metrics mean +/- std, C delta vs a reference) and the same
three significance modes, but every group is a *communication category*
(assigned per run by :mod:`comm_categories`) rather than a sweep.

Usage
-----
    python -m experiments.collusion_via_tools.analysis.category_tables <root> [<root> ...] [options]

Because a run can belong to several categories, grouping happens at the run
level: a run appears once in every category it belongs to.

Significance modes (auto-detected from the flags, overridable with
``--significance-mode``):

* ``summary``  Welch omnibus + pairwise Welch t with Holm-Bonferroni, computed
  in memory from the category groups (or from ``--summary-csv`` when given).
* ``delta``    per-run paired-delta tests on ``--delta-csv``.
* ``root``     load runs, augment baseline coalitions, compute the paired
  per-run deltas of every category against a reference SWEEP
  (``--reference-sweep``, defaulting to ``benign_baseline_sweep``) and test
  them.

Table C is an **unpaired** mean difference when comparing categories against a
reference category (category groups are overlapping subsets of the same
experimental units, so a within-seed pairing is not well-defined); it is
**paired by seed** when a ``--reference-sweep`` is given (a sweep has one run
per seed, so the pairing is well defined).

Plumbing lives in :mod:`sweep_metrics_table`, :mod:`sweep_significance`,
:mod:`delta_metrics` and :mod:`category_metrics_table`; this module only wires
them together.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import math
import os
import sys
from pathlib import Path
from typing import Any

from . import category_metrics_table as cmt
from .category_metrics_table import (
    CategoryGroup,
    resolve_category_reference,
)
from .sweep_metrics_table import DELTA_METRICS, METRICS_BY_KEY, GroupMetrics, RunData

smt = cmt.smt
cc = cmt.cc
sig = cmt._import_sibling("sweep_significance")

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_significance_out() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "plots_outputs",
        "category_significance",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="category_tables",
        description=(
            "Print the communication-category metric tables (A inventory, "
            "B metrics mean +/- std, C delta vs a reference category) and, "
            "optionally, run the significance tests grouped by category."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "root",
        nargs="*",
        default=None,
        help="One or more experiment output root directories.",
    )
    parser.add_argument(
        "--root",
        dest="root_opt",
        action="append",
        default=None,
        help="Alternative (repeatable) way to pass a root.",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None, help="Write long-format CSV here."
    )
    parser.add_argument(
        "--json", dest="json_path", default=None, help="Write nested JSON here."
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        default=None,
        help="Restrict to a category name, id or slug (repeatable).",
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        default=None,
        help="Restrict to a source (repeatable).",
    )
    parser.add_argument(
        "--sweep",
        dest="sweeps",
        action="append",
        default=None,
        help=(
            "Restrict the runs feeding the category groups to these sweeps "
            "(matched against run.sweep; repeatable). Applied before grouping."
        ),
    )
    parser.add_argument(
        "--metrics", dest="metrics", default=None, help="Comma-separated metric subset."
    )
    parser.add_argument(
        "--ddof",
        dest="ddof",
        type=int,
        choices=(0, 1),
        default=1,
        help="Std ddof (default 1).",
    )
    parser.add_argument(
        "--reference",
        dest="reference",
        default=None,
        help=(
            "Reference CATEGORY for the category-vs-category delta table "
            "(exact name, id, or slug). Ignored when --reference-sweep is set."
        ),
    )
    parser.add_argument(
        "--reference-sweep",
        dest="reference_sweep",
        default=None,
        help=(
            "Reference SWEEP. When set, Table C compares every category against "
            "this sweep (paired by seed), and the root significance mode uses it "
            "as the baseline. Root mode defaults to benign_baseline_sweep (or the "
            "first unclassified sweep) when this is not given."
        ),
    )
    parser.add_argument(
        "--no-delta", dest="no_delta", action="store_true", help="Skip the delta table."
    )
    parser.add_argument(
        "--significance-mode",
        dest="significance_mode",
        choices=("summary", "delta", "root"),
        default=None,
        help=(
            "Force a significance mode. By default the mode is auto-detected "
            "from --delta-csv (delta), --reference (root), else summary."
        ),
    )
    parser.add_argument(
        "--no-significance",
        dest="no_significance",
        action="store_true",
        help="Skip the significance step entirely.",
    )
    parser.add_argument(
        "--summary-csv",
        dest="summary_csv",
        default=None,
        help=(
            "Summary-stats CSV for the summary significance mode (the sweep "
            "column holds the category label). When omitted, the per-category "
            "cells are built in memory from the category groups."
        ),
    )
    parser.add_argument(
        "--delta-csv",
        dest="delta_csv",
        default=None,
        help=(
            "Per-run paired-delta CSV. When set, runs the per-run-delta "
            "significance mode and writes category_significance_delta.{csv,md}."
        ),
    )
    parser.add_argument(
        "--significance-out",
        "--out",
        dest="significance_out",
        default=None,
        help=(
            "Output directory for the significance files (default: "
            "<package>/plots_outputs/category_significance for the root mode, "
            "the --csv/--json directory for the in-memory summary mode). "
            "`--out` is accepted as a legacy alias."
        ),
    )
    parser.add_argument(
        "--significance-md",
        dest="significance_md",
        default=None,
        help="Override the markdown output path for the significance summary.",
    )
    parser.add_argument(
        "--exclude-error-runs",
        dest="exclude_error_runs",
        action="store_true",
        help="Drop runs that recorded a failed agent turn (opt-in).",
    )
    parser.add_argument(
        "--exclude-failed-runs",
        dest="exclude_failed_runs",
        action="store_true",
        help="Drop only runs that collapsed (failed turn AND no assignment; opt-in).",
    )
    parser.add_argument(
        "--arm-contrast",
        dest="arm_contrast",
        action="store_true",
        help=(
            "Print Table D: the within-category arm contrast (more rounds - "
            "fewer rounds), paired by seed (common random numbers), plus the "
            "per-category arm membership counts with an exact binomial test."
        ),
    )
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("plain", "formatted", "markdown"),
        default="plain",
        help="Output layout (default plain).",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Category map
# --------------------------------------------------------------------------- #


def _load_category_map(roots_abs: list[str]) -> Any | None:
    """Merge every ``comm_categories.json`` found under the given roots."""
    categories_by_id: dict[int, Any] = {}
    run_to_categories: dict[str, tuple[int, ...]] = {}
    sweep_to_categories: dict[str, tuple[int, ...]] = {}
    sweep_to_runs: dict[str, tuple[str, ...]] = {}
    any_found = False
    for root in roots_abs:
        try:
            mapping = cc.load_category_map(root)
        except FileNotFoundError:
            continue
        any_found = True
        for category in mapping.categories:
            categories_by_id.setdefault(category.id, category)
        for key, value in mapping.run_to_categories.items():
            run_to_categories.setdefault(key, value)
        for key, value in mapping.sweep_to_categories.items():
            sweep_to_categories.setdefault(key, value)
        for key, value in mapping.sweep_to_runs.items():
            sweep_to_runs.setdefault(key, value)

    if not any_found:
        print(
            "error: no comm_categories.json found under any provided root "
            "(run the comm_categories classifier first)",
            file=sys.stderr,
        )
        return None

    categories = tuple(sorted(categories_by_id.values(), key=lambda c: c.id))
    return cc.CategoryMapping(
        root=roots_abs[0],
        categories=categories,
        run_to_categories=run_to_categories,
        sweep_to_categories=sweep_to_categories,
        sweep_to_runs=sweep_to_runs,
    )


def _category_totals_by_model(
    runs: list[RunData], mapping: Any
) -> dict[str, dict[int, int]]:
    totals: dict[str, dict[int, int]] = {}
    for run in runs:
        for category_id in mapping.run_to_categories.get(run.run_id, ()):
            model_totals = totals.setdefault(run.model, {})
            model_totals[category_id] = model_totals.get(category_id, 0) + 1
    return totals


# --------------------------------------------------------------------------- #
# Significance -- summary mode
# --------------------------------------------------------------------------- #


def _build_summary_cells(
    groups_by_model: dict[str, list[CategoryGroup]],
    specs: list[Any],
    ddof: int,
) -> dict[tuple[str, str], Any]:
    """Build per-(category-label, metric) :class:`sig.Cell`s in memory.

    Runs are pooled across models by category label so a category with runs in
    more than one model still yields one cell.
    """
    by_label: dict[str, list[RunData]] = {}
    for group_list in groups_by_model.values():
        for group in group_list:
            by_label.setdefault(group.label, []).extend(group.runs)

    cells: dict[tuple[str, str], Any] = {}
    for label, runs in by_label.items():
        metrics = smt.compute_group_metrics(runs, specs, ddof)
        for spec in specs:
            if spec.key not in sig.METRIC_ORDER:
                continue
            agg = metrics.aggregates[spec.key]
            if agg.mean is None or agg.std is None:
                continue
            cells[(label, spec.key)] = sig.Cell(
                sweep=label,
                metric=spec.key,
                label=spec.label,
                n=agg.n,
                mean=agg.mean,
                std=agg.std,
            )
    return cells


def _compute_summary_significance(
    cells: dict[tuple[str, str], Any],
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    """Run the Welch omnibus + pairwise tests, shared by the CSV/MD writers."""
    sweeps_by_metric: dict[str, list[str]] = {}
    for (group, metric), cell in cells.items():
        if cell.is_valid():
            sweeps_by_metric.setdefault(metric, []).append(group)

    omnibus: dict[str, dict[str, object]] = {}
    for metric in sig.METRIC_ORDER:
        available = sorted(set(sweeps_by_metric.get(metric, [])))
        if len(available) < 2:
            continue
        groups = [cells[(group, metric)] for group in available]
        f_stat, p_val = sig.omnibus_welch_anova(groups)
        omnibus[metric] = {
            "metric": metric,
            "metric_label": groups[0].label,
            "k": len(groups),
            "min_n": min(g.n for g in groups),
            "max_n": max(g.n for g in groups),
            "F": f_stat,
            "p_value": p_val,
        }

    rows: list[dict[str, object]] = []
    for metric in sig.METRIC_ORDER:
        available = sorted(set(sweeps_by_metric.get(metric, [])))
        if not available:
            continue
        ordered = sig.ordered_metric_sweeps(metric, available)
        pairs = sig.all_pairs(ordered)
        raw_pvals: list[float] = []
        pair_records: list[dict[str, object]] = []
        for a, b in pairs:
            ca = cells[(a, metric)]
            cb = cells[(b, metric)]
            t, df, p = sig.welch_t_from_summary(
                ca.mean, cb.mean, ca.std, cb.std, ca.n, cb.n
            )
            ci_lo, ci_hi, _ = sig.mean_diff_ci(
                ca.mean, cb.mean, ca.std, cb.std, ca.n, cb.n
            )
            g_lo, g_hi = sig.hedges_g_ci(ca.mean, cb.mean, ca.std, cb.std, ca.n, cb.n)
            s_pool = math.sqrt(
                ((ca.n - 1) * ca.std**2 + (cb.n - 1) * cb.std**2) / (ca.n + cb.n - 2)
            )
            d = (ca.mean - cb.mean) / s_pool if s_pool > 0 else float("nan")
            g = sig.hedges_correction(d, ca.n, cb.n) if s_pool > 0 else float("nan")
            record = {
                "metric": metric,
                "metric_label": ca.label,
                "category_a": a,
                "category_b": b,
                "n_a": ca.n,
                "n_b": cb.n,
                "mean_a": ca.mean,
                "mean_b": cb.mean,
                "mean_diff": ca.mean - cb.mean,
                "mean_diff_ci95_lo": ci_lo,
                "mean_diff_ci95_hi": ci_hi,
                "cohens_d": d,
                "hedges_g": g,
                "hedges_g_ci95_lo": g_lo,
                "hedges_g_ci95_hi": g_hi,
                "t": t,
                "df": df,
                "p_value": p,
            }
            pair_records.append(record)
            raw_pvals.append(p)
        adjusted = sig.holm_bonferroni(raw_pvals)
        for rec, p_adj in zip(pair_records, adjusted):
            rec["p_holm"] = p_adj
            rec["significant_05"] = bool(p_adj < 0.05)
            rows.append(rec)

    return omnibus, rows


_SUMMARY_FIELDS = [
    "metric",
    "metric_label",
    "category_a",
    "category_b",
    "n_a",
    "n_b",
    "mean_a",
    "mean_b",
    "mean_diff",
    "mean_diff_ci95_lo",
    "mean_diff_ci95_hi",
    "cohens_d",
    "hedges_g",
    "hedges_g_ci95_lo",
    "hedges_g_ci95_hi",
    "t",
    "df",
    "p_value",
    "p_holm",
    "significant_05",
]


def _write_summary_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {path}")


def _write_summary_md(
    path: Path,
    rows: list[dict[str, object]],
    omnibus: dict[str, dict[str, object]],
) -> None:
    _fmt = sig._fmt
    _fmt_p = sig._fmt_p

    lines: list[str] = []
    lines.append(
        "# Category significance (Welch's omnibus + pairwise t, Holm-Bonferroni per metric)"
    )
    lines.append("")
    lines.append(
        "Computed from the per-(category, metric) summary stats. `mean_diff` is "
        "(category_a − category_b). `hedges_g` is Cohen's d with the Hedges "
        "small-sample correction; |g| < 0.2 is negligible, ~0.5 medium, ~0.8 "
        "large. **p_holm** is the Holm-Bonferroni adjusted p-value within the "
        "metric. `significant_05` = `True` iff `p_holm < 0.05`."
    )
    lines.append("")
    lines.append("## Omnibus: Welch's one-way ANOVA per metric")
    lines.append("")
    lines.append("| metric | k groups | n range | F | p | sig @ .05 |")
    lines.append("|---|---|---|---|---|---|")
    for metric in sig.METRIC_ORDER:
        rec = omnibus.get(metric)
        if not rec:
            continue
        sig_mark = "✅" if float(rec["p_value"]) < 0.05 else "—"
        lines.append(
            "| " + f"{metric} — {rec['metric_label']} | {rec['k']} | "
            f"{rec['min_n']}–{rec['max_n']} | "
            f"{_fmt(rec['F'], 2)} | {_fmt_p(rec['p_value'])} | {sig_mark} |"
        )
    lines.append("")
    lines.append("## Pairwise: Welch's t with Holm-Bonferroni correction")
    lines.append("")
    raw_rows = [r for r in rows if math.isfinite(float(r["hedges_g"]))]  # type: ignore[arg-type]
    by_abs_g = sorted(raw_rows, key=lambda r: -abs(float(r["hedges_g"])))[:5]  # type: ignore[arg-type]
    by_pval = sorted(raw_rows, key=lambda r: float(r["p_value"]))[:5]  # type: ignore[arg-type]
    lines.append("**Top 5 by |Hedges' g|** (largest standardised mean diffs):")
    lines.append("")
    lines.append(
        "| metric | category A | category B | mean diff | Hedges' g | 95% CI on g | raw p | p (Holm) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in by_abs_g:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["metric"]),
                    str(r["category_a"]),
                    str(r["category_b"]),
                    _fmt(r["mean_diff"]),
                    _fmt(r["hedges_g"]),
                    f"({_fmt(r['hedges_g_ci95_lo'])}, {_fmt(r['hedges_g_ci95_hi'])})",
                    _fmt_p(r["p_value"]),
                    _fmt_p(r["p_holm"]),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("**Top 5 by raw p** (smallest unadjusted p-values):")
    lines.append("")
    lines.append(
        "| metric | category A | category B | mean diff | Hedges' g | raw p | p (Holm) |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in by_pval:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["metric"]),
                    str(r["category_a"]),
                    str(r["category_b"]),
                    _fmt(r["mean_diff"]),
                    _fmt(r["hedges_g"]),
                    _fmt_p(r["p_value"]),
                    _fmt_p(r["p_holm"]),
                ]
            )
            + " |"
        )
    lines.append("")
    by_metric: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        by_metric.setdefault(str(r["metric"]), []).append(r)
    for metric in sig.METRIC_ORDER:
        recs = by_metric.get(metric, [])
        if not recs:
            continue
        first = recs[0]
        lines.append(f"## {metric} — {first['metric_label']}")
        lines.append("")
        lines.append(
            "| category A | category B | n_A | n_B | mean A | mean B | mean diff | "
            "95% CI on diff | Cohen's d | Hedges' g | 95% CI on g | t | df | "
            "p (raw) | p (Holm) | sig @ .05 |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
        )
        for r in recs:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(r["category_a"]),
                        str(r["category_b"]),
                        str(r["n_a"]),
                        str(r["n_b"]),
                        _fmt(r["mean_a"]),
                        _fmt(r["mean_b"]),
                        _fmt(r["mean_diff"]),
                        f"({_fmt(r['mean_diff_ci95_lo'])}, {_fmt(r['mean_diff_ci95_hi'])})",
                        _fmt(r["cohens_d"]),
                        _fmt(r["hedges_g"]),
                        f"({_fmt(r['hedges_g_ci95_lo'])}, {_fmt(r['hedges_g_ci95_hi'])})",
                        _fmt(r["t"], 2),
                        _fmt(r["df"], 1),
                        _fmt_p(r["p_value"]),
                        _fmt_p(r["p_holm"]),
                        "✅" if r["significant_05"] else "—",
                    ]
                )
                + " |"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    print(f"Wrote {path}")


def _summary_out_dir(args: argparse.Namespace) -> Path:
    if args.significance_out:
        return Path(args.significance_out)
    if args.summary_csv:
        return Path(args.summary_csv).parent
    if args.csv_path:
        return Path(args.csv_path).parent
    if args.json_path:
        return Path(args.json_path).parent
    return Path.cwd()


def _run_summary_significance(
    args: argparse.Namespace,
    groups_by_model: dict[str, list[CategoryGroup]],
    specs: list[Any],
) -> int:
    if args.summary_csv:
        summary_csv = Path(args.summary_csv)
        if not summary_csv.exists():
            print(f"error: summary CSV not found: {summary_csv}", file=sys.stderr)
            return 2
        cells = sig.load_cells(summary_csv)
        if not cells:
            print(f"error: no data loaded from {summary_csv}", file=sys.stderr)
            return 2
    else:
        cells = _build_summary_cells(groups_by_model, specs, args.ddof)
        if not cells:
            print(
                "warning: no category cells with valid summary stats for the "
                "significance tests",
                file=sys.stderr,
            )
            return 0

    omnibus, rows = _compute_summary_significance(cells)
    out_dir = _summary_out_dir(args)
    out_csv = out_dir / "category_significance.csv"
    out_md = (
        Path(args.significance_md)
        if args.significance_md
        else out_dir / "category_significance.md"
    )
    _write_summary_csv(out_csv, rows)
    _write_summary_md(out_md, rows, omnibus)
    return 0


# --------------------------------------------------------------------------- #
# Significance -- delta / root modes
# --------------------------------------------------------------------------- #


def _run_delta_significance(
    delta_csv: Path, out_dir: Path, md_override: str | None
) -> int:
    if not delta_csv.exists():
        print(f"error: delta CSV not found: {delta_csv}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    # ``sig.run_delta_mode`` prints the pre-rename ("sweep_significance_delta")
    # paths; swallow those and re-emit the renamed category_* paths instead.
    with contextlib.redirect_stdout(io.StringIO()):
        sig.run_delta_mode(delta_csv, out_dir)

    csv_src = out_dir / "sweep_significance_delta.csv"
    csv_dst = out_dir / "category_significance_delta.csv"
    md_src = out_dir / "sweep_significance_delta.md"
    md_dst = out_dir / "category_significance_delta.md"
    if csv_src.exists():
        csv_src.replace(csv_dst)
        print(f"Wrote {csv_dst}")
    if md_src.exists():
        md_src.replace(md_dst)
        print(f"Wrote {md_dst}")

    if md_override:
        if md_dst.exists():
            target = Path(md_override)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md_dst.read_text())
        else:
            print(
                f"warning: significance markdown not found to redirect: {md_dst}",
                file=sys.stderr,
            )
    return 0


_CATEGORY_DELTA_FIELDS = [
    "test",
    "model",
    "metric",
    "metric_label",
    "sweep",
    "n_paired",
    "mean_delta",
    "std_delta",
    "t_vs_zero",
    "df_vs_zero",
    "p_value_t",
    "p_value_wilcoxon",
    "cohens_d_vs_zero",
    "hedges_g_vs_zero",
    "p_holm_t_vs_zero",
    "significant_05_t_vs_zero",
]


def _write_category_delta_md(
    path: Path, rows: list[dict[str, object]], source_name: str
) -> None:
    _fmt = sig._fmt
    _fmt_p = sig._fmt_p

    lines: list[str] = []
    lines.append(
        "# Category significance on paired-by-seed deltas (vs the reference sweep)"
    )
    lines.append("")
    lines.append(
        f"Computed from `{source_name}`. Only the **one-sample test vs 0** per "
        f"(model, category, metric) is reported -- *did this category's per-seed "
        f"delta against the reference sweep deviate from 0?* Both Welch's "
        f"one-sample t and the Wilcoxon signed-rank are given; the Wilcoxon is "
        f"more robust to the outliers these deltas carry. `p_holm_t_vs_zero` is "
        f"the Holm-Bonferroni adjusted p-value within each (model, metric) "
        f"family."
    )
    lines.append("")
    lines.append(
        "> **The pairwise category-vs-category comparison is deliberately "
        "omitted.** In this CSV the grouped keys are CATEGORY SLUGS, and the "
        "category groups are overlapping subsets of the same runs, so pairing "
        "seed k of one category against seed k of another is not a well-defined "
        "contrast. Use the unpaired Table C for category-vs-category questions."
    )
    lines.append("")
    any_sig = any(bool(r.get("significant_05_t_vs_zero")) for r in rows)
    if any_sig:
        lines.append(
            "**Headline:** at α=0.05 with Holm-Bonferroni correction, at least "
            "one one-sample test survives -- see the per-family tables below."
        )
    else:
        lines.append(
            "**Headline:** at α=0.05 with Holm-Bonferroni correction, **no "
            "one-sample test survives** -- the paired-seed spread of the deltas "
            "is large relative to their mean. The raw p-values and Hedges' g "
            "below show what the data hints at."
        )
    lines.append("")

    family_keys: list[tuple[str, str]] = sorted(
        {(str(r["model"]), str(r["metric"])) for r in rows}
    )
    for model, metric in family_keys:
        recs = [
            r for r in rows if str(r["model"]) == model and str(r["metric"]) == metric
        ]
        metric_label = sig.DELTA_METRIC_LABELS.get(metric, metric)
        lines.append(f"### {model} — {metric} — {metric_label}")
        lines.append("")
        lines.append(
            "| category | n | mean Δ | std Δ | t (vs 0) | df | raw p (t) | "
            "p (Holm, t) | raw p (Wilcoxon) | Hedges' g | sig @ .05 (t) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in recs:
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
                        "✅" if r["significant_05_t_vs_zero"] else "—",
                    ]
                )
                + " |"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    print(f"Wrote {path}")


def _run_category_delta_significance(
    delta_csv: Path, out_dir: Path, md_override: str | None
) -> int:
    """One-sample-only significance for the category-vs-sweep deltas.

    Unlike ``sweep_significance.run_delta_mode``, this does NOT emit the pairwise
    category-vs-category rows: in the category CSV the grouped keys are CATEGORY
    SLUGS (not sweeps), so pairing seed k of category A against seed k of
    category B is exactly the ill-defined overlapping-subsets contrast the
    unpaired Table C exists to avoid. Only the well-defined one-sample test vs 0
    per (model, category) is reported. Output filenames match the existing ones.
    """
    if not delta_csv.exists():
        print(f"error: delta CSV not found: {delta_csv}", file=sys.stderr)
        return 2
    cells = sig.load_delta_cells(delta_csv)
    if not cells:
        print(f"error: no per-run-delta data loaded from {delta_csv}", file=sys.stderr)
        return 2

    by_mm: dict[tuple[str, str], dict[str, dict[int, float]]] = {}
    for (model, sweep, metric), seed_map in cells.items():
        by_mm.setdefault((model, metric), {})[sweep] = seed_map

    rows: list[dict[str, object]] = []
    for (model, metric), sweeps_map in sorted(by_mm.items()):
        per_sweep_ps: list[float] = []
        records: list[dict[str, object]] = []
        for sweep in sorted(sweeps_map):
            values = list(sweeps_map[sweep].values())
            mean_v, std_v, n_v = sig.mean_and_std(values)
            d_one = (
                (mean_v / std_v) if (std_v and std_v > 0 and n_v >= 2) else float("nan")
            )
            g_one = (
                sig.hedges_correction_from_d(d_one, n_v) if n_v >= 2 else float("nan")
            )
            t, df_os, p_t = sig.one_sample_t(values)
            _, p_w = sig.wilcoxon_vs_zero(values)
            records.append(
                {
                    "test": "one_sample",
                    "model": model,
                    "metric": metric,
                    "metric_label": sig.DELTA_METRIC_LABELS.get(metric, metric),
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
            )
            per_sweep_ps.append(p_t)
        adjusted = sig.holm_bonferroni(per_sweep_ps)
        for rec, p_adj in zip(records, adjusted):
            rec["p_holm_t_vs_zero"] = p_adj
            rec["significant_05_t_vs_zero"] = bool(p_adj < 0.05)
            rows.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "category_significance_delta.csv"
    md_path = out_dir / "category_significance_delta.md"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CATEGORY_DELTA_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {csv_path}")

    _write_category_delta_md(md_path, rows, delta_csv.name)
    if md_override:
        target = Path(md_override)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md_path.read_text())
    return 0


def _write_delta_points_csv(points_csv: Path, points: list[Any]) -> None:
    points_csv.parent.mkdir(parents=True, exist_ok=True)
    with points_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "model",
                "sweep",
                "seed",
                "run_id",
                "judge_delta",
                "advantage_delta",
                "judge_metric",
            ]
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
                    "public_channel",
                ]
            )
    print(f"Wrote {points_csv}")


def _default_reference_sweep(kept_runs: list[RunData], mapping: Any) -> str | None:
    """Pick a default reference sweep for the root significance mode.

    Prefers ``benign_baseline_sweep`` when present, then the first discovered
    sweep that carries no communication categories, then the first sweep.
    """
    sweeps = sorted({run.sweep for run in kept_runs})
    if not sweeps:
        return None
    if "benign_baseline_sweep" in sweeps:
        return "benign_baseline_sweep"
    unclassified = [
        sweep for sweep in sweeps if not mapping.sweep_to_categories.get(sweep)
    ]
    if unclassified:
        return unclassified[0]
    return sweeps[0]


def _run_root_significance(
    args: argparse.Namespace,
    groups_by_model: dict[str, list[CategoryGroup]],
    kept_runs: list[RunData],
    mapping: Any,
) -> int:
    if not groups_by_model:
        print("warning: no usable category groups", file=sys.stderr)
        return 0

    reference_sweep = args.reference_sweep or _default_reference_sweep(
        kept_runs, mapping
    )
    if not reference_sweep:
        print(
            "error: no reference sweep available for the root mode",
            file=sys.stderr,
        )
        return 2

    # Build {model: {category_slug: [runs]}} and add the reference sweep's runs
    # under its own key (the sweep is usually unclassified, so it is not in the
    # category groups).
    grouped: dict[str, dict[str, list[Any]]] = {}
    for model, group_list in groups_by_model.items():
        for group in group_list:
            grouped.setdefault(model, {})[group.sweep] = group.runs
    for model in list(grouped):
        grouped[model][reference_sweep] = [
            run
            for run in kept_runs
            if run.model == model and run.sweep == reference_sweep
        ]

    points, _run_counts = cmt.compute_category_paired_delta_points(
        grouped, reference_sweep
    )
    if not points:
        print(
            f"warning: no paired seeds survive for reference sweep '{reference_sweep}'",
            file=sys.stderr,
        )
        return 0

    out_dir = Path(args.significance_out or _default_significance_out())
    out_dir.mkdir(parents=True, exist_ok=True)
    points_csv = out_dir / "judge_vs_advantage_delta_points.csv"
    _write_delta_points_csv(points_csv, points)
    return _run_category_delta_significance(points_csv, out_dir, args.significance_md)


# --------------------------------------------------------------------------- #
# Significance dispatch
# --------------------------------------------------------------------------- #


def _resolve_significance_mode(args: argparse.Namespace) -> str | None:
    if args.significance_mode:
        return args.significance_mode
    if args.delta_csv:
        return "delta"
    if args.reference or args.reference_sweep:
        return "root"
    return "summary"


def _run_significance(
    args: argparse.Namespace,
    groups_by_model: dict[str, list[CategoryGroup]],
    specs: list[Any],
    kept_runs: list[RunData],
    mapping: Any,
) -> int:
    if args.no_significance:
        return 0

    mode = _resolve_significance_mode(args)
    if mode is None:
        return 0

    if mode == "delta":
        if not args.delta_csv:
            print(
                "error: --significance-mode delta requires --delta-csv PATH",
                file=sys.stderr,
            )
            return 2
        delta_csv = Path(args.delta_csv)
        out_dir = Path(args.significance_out or delta_csv.parent)
        return _run_delta_significance(delta_csv, out_dir, args.significance_md)

    if mode == "root":
        return _run_root_significance(args, groups_by_model, kept_runs, mapping)

    return _run_summary_significance(args, groups_by_model, specs)


# --------------------------------------------------------------------------- #
# Table D -- within-category arm contrast
# --------------------------------------------------------------------------- #


def _arm_contrast_hint(contrasts: list[dict[str, Any]]) -> str:
    """One-line description of the arm contrast, from the first row."""
    if not contrasts:
        return "arm contrast: no category spans two or more sweeps"
    first = contrasts[0]
    return (
        f"arm contrast: ({first['higher_sweep']}) - ({first['lower_sweep']}), "
        f"paired by seed (common random numbers)"
    )


def _print_arm_contrasts(
    model: str,
    contrasts: list[dict[str, Any]],
    membership: list[dict[str, Any]],
) -> None:
    """Print Table D (metric contrasts) and its membership companion."""
    print(f"\nTable D -- within-category arm contrast [{model or '-'}]")
    headers = [
        "Category",
        "Metric",
        "Arms (A - B)",
        "n",
        "mean diff",
        "sd",
        "t",
        "df",
        "p (t)",
        "p (Wilcoxon)",
        "p (Holm)",
    ]
    rows: list[list[str]] = []
    for row in contrasts:
        rows.append(
            [
                str(row["category"]),
                str(row["metric"]),
                f"{row['higher_sweep']} - {row['lower_sweep']}",
                str(row["n"]),
                f"{float(row['mean_diff']):+.1f}",
                f"{float(row['sd']):.1f}",
                f"{float(row['t']):+.2f}",
                f"{float(row['df']):.1f}",
                sig._fmt_p(row["p_value"]),
                sig._fmt_p(row["p_wilcoxon"]),
                sig._fmt_p(row["p_holm"]),
            ]
        )
    aligns = ["l", "l", "l", "r", "r", "r", "r", "r", "r", "r", "r"]
    print(smt.render_table(headers, rows, aligns))

    print(f"\nTable D -- arm membership (A = more rounds, B = fewer) [{model}]")
    headers = [
        "Category",
        "A - B",
        "A only",
        "B only",
        "both",
        "neither",
        "p (binomial)",
    ]
    rows = []
    for row in membership:
        rows.append(
            [
                str(row["category"]),
                f"{row['higher_sweep']} - {row['lower_sweep']}",
                str(row["only_higher"]),
                str(row["only_lower"]),
                str(row["both"]),
                str(row["neither"]),
                sig._fmt_p(row["p_binomial"]),
            ]
        )
    aligns = ["l", "l", "r", "r", "r", "r", "r"]
    print(smt.render_table(headers, rows, aligns))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    roots = list(args.root or []) + list(args.root_opt or [])
    if not roots:
        print("error: a root path is required (positional or --root)", file=sys.stderr)
        return 2

    roots_abs = [os.path.abspath(root) for root in roots]
    missing = [root for root in roots_abs if not os.path.exists(root)]
    if missing:
        for root in missing:
            print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    mapping = _load_category_map(roots_abs)
    if mapping is None:
        return 2

    specs = smt.resolve_metrics(args.metrics)

    found: list[str] = []
    runs: list[RunData] = []
    for root_abs in roots_abs:
        root_found = smt.find_metric_files(root_abs)
        found.extend(root_found)
        runs.extend(smt.load_runs(root_abs, root_found))
    runs_used = len(runs)

    kept_runs, excluded_runs = smt.partition_runs(
        runs, args.exclude_error_runs, args.exclude_failed_runs
    )
    excluded_runs.sort(key=smt._excluded_sort_key)

    # Keep an unfiltered copy so a reference SWEEP stays resolvable when
    # ``--sweep`` restricts the runs feeding the category groups.
    kept_runs_all = list(kept_runs)

    # Augment benign-baseline runs (``adversary_count == 0``) with the
    # counterfactual coalition metrics so the coalition-aware columns resolve.
    # In-memory only; no ``metrics.json`` is mutated.
    augment = smt._ensure_baseline_coalition_helpers()
    inferred_colluders = augment(kept_runs_all)
    if inferred_colluders:
        print(
            f"baseline coalition inferred from non-baseline sweeps: "
            f"{sorted(inferred_colluders)}"
        )

    sweep_filter = set(args.sweeps) if args.sweeps else None
    if sweep_filter is not None:
        kept_runs = [run for run in kept_runs_all if run.sweep in sweep_filter]

    totals_source = [
        run for run in runs if sweep_filter is None or run.sweep in sweep_filter
    ]
    totals_by_model = _category_totals_by_model(totals_source, mapping)

    reference_sweep_name = args.reference_sweep
    if reference_sweep_name is not None:
        discovered_sweeps = {run.sweep for run in kept_runs_all}
        if reference_sweep_name not in discovered_sweeps:
            print(
                f"error: reference sweep '{reference_sweep_name}' not found under "
                f"any provided root",
                file=sys.stderr,
            )
            return 2

    sweep_runs_by_model: dict[str, list[RunData]] = {}
    if reference_sweep_name:
        for run in kept_runs_all:
            if run.sweep == reference_sweep_name:
                sweep_runs_by_model.setdefault(run.model, []).append(run)

    grouped = cmt.build_category_groups(kept_runs, mapping)

    if args.categories:
        grouped = cmt.filter_groups_by_category(grouped, set(args.categories))
    if args.sources:
        grouped = smt.filter_groups_by_source(grouped, set(args.sources))
    grouped = {model: groups for model, groups in grouped.items() if groups}
    if (args.categories or args.sources) and not grouped:
        print("warning: no groups matched the requested filters", file=sys.stderr)

    # Show the reference sweep as an extra, non-category group. It is inserted
    # FIRST so it renders as the leftmost Table B column and the first Table A
    # row. ``category_id is None`` marks it as a sweep, not a category; Table C
    # excludes it from its category columns.
    if reference_sweep_name:
        for model, run_list in sweep_runs_by_model.items():
            if model not in grouped:
                continue
            ordered_runs = sorted(run_list, key=lambda r: (r.run_id, r.source))
            grouped[model].insert(
                0,
                CategoryGroup(
                    label=reference_sweep_name,
                    sweep=reference_sweep_name,
                    category_id=None,
                    sources=sorted({r.source for r in ordered_runs}),
                    source_labels=sorted({r.source_label for r in ordered_runs}),
                    source_paths=sorted({r.source_path for r in ordered_runs}),
                    runs=ordered_runs,
                    sweeps=[reference_sweep_name],
                ),
            )

    selected_runs = [
        run
        for model_groups in grouped.values()
        for group in model_groups
        for run in group.runs
    ]
    # A run appears once per category it belongs to, so dedupe before the source
    # summary (which counts runs) to avoid inflating the per-source run counts.
    seen_runs: set[tuple[str, str]] = set()
    unique_selected_runs: list[RunData] = []
    for run in selected_runs:
        key = (run.model, run.run_id)
        if key in seen_runs:
            continue
        seen_runs.add(key)
        unique_selected_runs.append(run)
    source_summaries = smt.summarize_sources(unique_selected_runs)

    models = sorted(grouped)

    delta_specs = [METRICS_BY_KEY[k] for k in DELTA_METRICS if k in METRICS_BY_KEY]

    # Table D (opt-in): the within-category arm contrast, paired by seed.
    arm_contrasts_by_model: dict[str, list[dict[str, Any]]] = {}
    arm_membership_by_model: dict[str, list[dict[str, Any]]] = {}
    if args.arm_contrast:
        sweep_seeds: dict[str, set[int]] = {}
        for run in kept_runs_all:
            seed = run.seed()
            if seed is not None:
                sweep_seeds.setdefault(run.sweep, set()).add(seed)
        for model in models:
            arm_contrasts_by_model[model] = cmt.compute_arm_contrasts(
                grouped[model], delta_specs, args.ddof
            )
            arm_membership_by_model[model] = cmt.compute_arm_membership(
                grouped[model], sweep_seeds
            )

    if not kept_runs:
        print("No usable runs found.", file=sys.stderr)

    if len(roots_abs) == 1:
        print(f"root:      {roots_abs[0]}")
    else:
        print("roots:")
        for root_abs in roots_abs:
            print(f"            {root_abs}")
    sources_line = ", ".join(
        f"{source['display']} ({source['n_runs']} runs)" for source in source_summaries
    )
    print(f"sources:   {sources_line}")
    print(f"runs found: {len(found)}  (usable: {runs_used})")
    if excluded_runs:
        if args.exclude_error_runs and args.exclude_failed_runs:
            print(f"excluded runs: {len(excluded_runs)} of {runs_used}")
        elif args.exclude_failed_runs:
            print(f"excluded failed runs: {len(excluded_runs)} of {runs_used}")
        else:
            print(f"excluded error runs: {len(excluded_runs)} of {runs_used}")
        for run, agents in excluded_runs:
            seed = run.seed()
            seed_cell = f"seed{seed}" if seed is not None else "-"
            agents_cell = f"[{', '.join(agents)}]" if agents else "-"
            line = f"  {run.sweep}  {seed_cell}  {agents_cell}"
            if args.exclude_failed_runs:
                line += f"  ({smt._exclusion_reason(run, True)})"
            print(line)
    if sweep_filter is not None:
        print(f"sweep filter: {'; '.join(sorted(sweep_filter))}")
    print(f"ddof:      {args.ddof}")

    classified_ids = set(mapping.run_to_categories)
    classified = sum(1 for run in totals_source if run.run_id in classified_ids)
    without_category = len(totals_source) - classified
    category_ids = {
        group.category_id
        for model_groups in grouped.values()
        for group in model_groups
        if group.category_id is not None
    }
    n_categories = len(category_ids) or len(mapping.categories)
    print(
        f"categories: {n_categories}  (runs classified: {classified} of "
        f"{len(totals_source)}; runs without a category: {without_category})"
    )
    if reference_sweep_name:
        reference_runs = sum(
            len(group.runs)
            for model_groups in grouped.values()
            for group in model_groups
            if group.category_id is None
        )
        print(
            f"reference sweep: {reference_sweep_name} ({reference_runs} runs, "
            f"shown as the first column)"
        )
    if reference_sweep_name:
        pairing_line = f"paired by seed vs sweep {reference_sweep_name}"
    else:
        pairing_line = "unpaired mean difference (category groups overlap)"
    print(f"pairing:   {pairing_line}")
    if args.arm_contrast:
        hint_rows = [
            row for model in models for row in arm_contrasts_by_model.get(model, [])
        ]
        print(_arm_contrast_hint(hint_rows))

    groups: dict[str, dict[str, GroupMetrics]] = {}
    for model in models:
        groups[model] = {
            group.label: smt.compute_group_metrics(group.runs, specs, args.ddof)
            for group in grouped[model]
        }

    show_delta = not args.no_delta
    if args.fmt == "plain":
        for model in models:
            cmt.print_category_inventory(
                grouped[model], model, totals_by_model.get(model)
            )
            cmt.print_category_metrics_table(
                [group.label for group in grouped[model]],
                groups[model],
                specs,
                model,
            )

        if show_delta:
            for model in models:
                if reference_sweep_name:
                    sweep_runs = sweep_runs_by_model.get(model)
                    if not sweep_runs:
                        continue
                    cmt.print_category_delta_vs_sweep_table(
                        grouped[model],
                        reference_sweep_name,
                        sweep_runs,
                        delta_specs,
                        model,
                        args.ddof,
                    )
                else:
                    reference = resolve_category_reference(
                        grouped[model], args.reference
                    )
                    if reference is None:
                        continue
                    cmt.print_category_delta_table(
                        grouped[model],
                        reference,
                        delta_specs,
                        model,
                        args.ddof,
                    )
    else:
        cmt.print_category_transposed_output(
            models,
            grouped,
            groups,
            specs,
            delta_specs,
            args.reference,
            args.ddof,
            args.fmt,
            show_delta,
            totals_by_model,
            reference_sweep_name=reference_sweep_name,
            sweep_runs_by_model=sweep_runs_by_model,
        )

    if args.arm_contrast:
        for model in models:
            _print_arm_contrasts(
                model,
                arm_contrasts_by_model.get(model, []),
                arm_membership_by_model.get(model, []),
            )

    if args.csv_path:
        cmt.write_category_csv(args.csv_path, specs, models, grouped, groups)
        print(f"\nWrote CSV: {os.path.abspath(args.csv_path)}")
    if args.json_path:
        cmt.write_category_json(
            args.json_path,
            roots_abs,
            source_summaries,
            args.ddof,
            len(found),
            runs_used,
            specs,
            grouped,
            groups,
            excluded_runs,
            args.exclude_failed_runs,
            arm_contrast=[
                row for model in models for row in arm_contrasts_by_model.get(model, [])
            ],
            arm_membership=[
                row
                for model in models
                for row in arm_membership_by_model.get(model, [])
            ],
        )
        print(f"Wrote JSON: {os.path.abspath(args.json_path)}")

    return _run_significance(args, grouped, specs, kept_runs_all, mapping)


if __name__ == "__main__":
    sys.exit(main())
