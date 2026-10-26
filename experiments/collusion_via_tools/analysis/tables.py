"""Consolidated table + significance entry point for sweep metrics.

This is the **primary** CLI for the tabular side of the analysis. It folds the
old ``sweep_metrics_table.py`` (Tables A/B/C, CSV/JSON outputs) and
``sweep_significance.py`` (summary-stats / per-run-delta / one-step-root
significance) into a single command.

Usage
-----
    python -m experiments.collusion_via_tools.analysis.tables <root> [<root> ...] [options]

It prints the same three tables (A inventory, B metrics mean +/- std, C delta
vs a reference) as the old table script and, depending on the flags, also runs
one of the three significance modes:

* ``--delta-csv PATH``   per-run paired-delta mode (one-sample + paired tests
                         on a per-run CSV).
* ``--reference SWEEP``  one-step root mode: load runs, augment baseline
                         coalitions, compute the paired deltas and test them.
* ``--summary-csv PATH`` (or the default stats CSV, when it exists)
                         summary-stats mode (Welch omnibus + pairwise t with
                         Holm-Bonferroni on the per-(sweep, metric) stats).

Plumbing lives in :mod:`sweep_metrics_table`, :mod:`sweep_significance`,
:mod:`delta_metrics` and :mod:`aggregate_protocols`; this module only wires
them together.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from pathlib import Path
from typing import Any

from . import sweep_metrics_table as smt
from . import sweep_significance as sig
from .delta_metrics import compute_paired_deltas
from .sweep_metrics_table import (
    DELTA_METRICS,
    GroupMetrics,
    METRICS_BY_KEY,
    RunData,
)

# --------------------------------------------------------------------------- #
# aggregate_protocols access (retained retry-safe import from the table module)
# --------------------------------------------------------------------------- #

_AGG_MODULE: Any = None


def _aggregate_protocols() -> Any:
    """Return the ``aggregate_protocols`` module via the retry-safe importer.

    The retry helper lives in :mod:`sweep_metrics_table` (it protects against a
    concurrently-edited sibling); reuse it rather than importing the module
    eagerly here.
    """
    global _AGG_MODULE
    if _AGG_MODULE is None:
        _AGG_MODULE = smt._import_aggregate_protocols()
    return _AGG_MODULE


def augment_runs_with_baseline_coalition(runs: list[Any]) -> Any:
    """Shim so callers can reuse the augmentation without a direct import."""
    return _aggregate_protocols().augment_runs_with_baseline_coalition(runs)


def identify_colluder_set(runs: list[Any]) -> Any:
    """Shim so callers can reuse the colluder inference without a direct import."""
    return _aggregate_protocols().identify_colluder_set(runs)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_significance_out() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "plots_outputs",
        "judge_vs_advantage_delta",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tables",
        description=(
            "Print the sweep metric tables (A inventory, B metrics mean +/- std, "
            "C delta vs a reference) and, optionally, run the sweep significance "
            "tests. Consolidated entry point for sweep_metrics_table.py and "
            "sweep_significance.py."
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
        "--sweep",
        dest="sweeps",
        action="append",
        default=None,
        help="Restrict to a sweep or source (repeatable).",
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        default=None,
        help="Restrict to a source (repeatable).",
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
            "Reference sweep for the delta table. When set, also switches the "
            "significance step to the one-step root mode."
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
            "from --delta-csv (delta), --reference (root), or --summary-csv / the "
            "default stats CSV (summary)."
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
            "Summary-stats CSV for the summary significance mode (default: the "
            "package's plots_outputs/sweep_metrics/sweep_metrics_stats.csv when "
            "it exists)."
        ),
    )
    parser.add_argument(
        "--delta-csv",
        dest="delta_csv",
        default=None,
        help=(
            "Per-run paired-delta CSV emitted by the delta plot script. When set, "
            "runs the per-run-delta significance mode and writes "
            "sweep_significance_delta.{csv,md} next to the input."
        ),
    )
    parser.add_argument(
        "--significance-out",
        "--out",
        dest="significance_out",
        default=None,
        help=(
            "Output directory for the per-run-delta mode's files "
            "(default: <package>/plots_outputs/judge_vs_advantage_delta). "
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
        help=(
            "Drop runs that recorded a failed agent turn (mirrors resume.py; opt-in)."
        ),
    )
    parser.add_argument(
        "--exclude-failed-runs",
        dest="exclude_failed_runs",
        action="store_true",
        help=(
            "Drop only runs that collapsed (failed turn AND no assignment); "
            "narrower than --exclude-error-runs (opt-in)."
        ),
    )
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("plain", "formatted", "markdown"),
        default="plain",
        help="Output layout (default plain).",
    )
    parser.add_argument(
        "--center",
        dest="center",
        choices=("mean", "median"),
        default="mean",
        help=(
            "Central estimate for the metrics/delta cells: 'mean' renders "
            "'mean +/- std' (default), 'median' renders 'median [p25, p75]' "
            "(linear-interpolation quartiles). Does not affect the "
            "significance step."
        ),
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Significance
# --------------------------------------------------------------------------- #


def _resolve_significance_mode(args: argparse.Namespace) -> str | None:
    if args.significance_mode:
        return args.significance_mode
    if args.delta_csv:
        return "delta"
    if args.reference:
        return "root"
    if args.summary_csv or Path(sig.STATS_CSV).exists():
        return "summary"
    return None


def _write_delta_points_csv(points_csv: Path, points: list[Any]) -> None:
    points_csv.parent.mkdir(parents=True, exist_ok=True)
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


def _run_delta_significance(
    delta_csv: Path, out_dir: Path, md_override: str | None
) -> int:
    if not delta_csv.exists():
        print(f"error: delta CSV not found: {delta_csv}", file=sys.stderr)
        return 2
    sig.run_delta_mode(delta_csv, out_dir)
    if md_override:
        default_md = out_dir / "sweep_significance_delta.md"
        if default_md.exists():
            target = Path(md_override)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(default_md.read_text())
        else:
            print(
                f"warning: significance markdown not found to redirect: {default_md}",
                file=sys.stderr,
            )
    return 0


def _run_root_significance(args: argparse.Namespace, kept_runs: list[Any]) -> int:
    if not args.reference:
        print(
            "error: significance root mode requires --reference SWEEP",
            file=sys.stderr,
        )
        return 2
    if not kept_runs:
        print(
            "warning: no usable runs left after excluding runs",
            file=sys.stderr,
        )
        return 0

    runs_by_model_sweep: dict[str, dict[str, list[Any]]] = {}
    for run in kept_runs:
        runs_by_model_sweep.setdefault(run.model, {}).setdefault(run.sweep, []).append(
            run
        )

    ref_seen_in = {
        model
        for model, sweeps in runs_by_model_sweep.items()
        if args.reference in sweeps
    }
    if not ref_seen_in:
        print(
            f"error: reference sweep '{args.reference}' not found under any "
            f"provided root",
            file=sys.stderr,
        )
        return 2

    points, _run_counts = compute_paired_deltas(runs_by_model_sweep, args.reference)
    if not points:
        print(
            f"warning: no paired seeds survive for reference '{args.reference}'",
            file=sys.stderr,
        )
        return 0

    out_dir = Path(args.significance_out or _default_significance_out())
    out_dir.mkdir(parents=True, exist_ok=True)
    points_csv = out_dir / "judge_vs_advantage_delta_points.csv"
    _write_delta_points_csv(points_csv, points)
    return _run_delta_significance(points_csv, out_dir, args.significance_md)


def _run_summary_significance(summary_csv: Path, md_override: str | None) -> int:
    if not summary_csv.exists():
        print(f"error: summary CSV not found: {summary_csv}", file=sys.stderr)
        return 2
    cells = sig.load_cells(summary_csv)
    if not cells:
        print(f"error: no data loaded from {summary_csv}", file=sys.stderr)
        return 2

    sweeps_by_metric: dict[str, list[str]] = {}
    for (sweep, metric), cell in cells.items():
        if cell.is_valid():
            sweeps_by_metric.setdefault(metric, []).append(sweep)

    omnibus: dict[str, dict[str, object]] = {}
    for metric in sig.METRIC_ORDER:
        available = sorted(set(sweeps_by_metric.get(metric, [])))
        if len(available) < 2:
            continue
        groups = [cells[(s, metric)] for s in available]
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
                "sweep_a": a,
                "sweep_b": b,
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

    out_csv = summary_csv.with_name("sweep_significance.csv")
    out_md = (
        Path(md_override)
        if md_override
        else summary_csv.with_name("sweep_significance.md")
    )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with out_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {out_csv}")

    _fmt = sig._fmt
    _fmt_p = sig._fmt_p

    lines: list[str] = []
    lines.append(
        "# Sweep significance (Welch's omnibus + pairwise t, Holm-Bonferroni per metric)"
    )
    lines.append("")
    lines.append(
        "Computed from `sweep_metrics_stats.csv` (n=9-10 per cell). "
        "`mean_diff` is (sweep_a − sweep_b). `hedges_g` is Cohen's d with the "
        "Hedges small-sample correction; |g| < 0.2 is negligible, ~0.5 medium, "
        "~0.8 large. **p_holm** is the Holm-Bonferroni adjusted p-value within "
        "the metric (more powerful than plain Bonferroni). "
        "`significant_05` = `True` iff `p_holm < 0.05`."
    )
    lines.append("")
    lines.append(
        "**Headline:** at α=0.05, **no pairwise comparison survives Holm-Bonferroni**. "
        "n=9-10 per cell is too small for the observed within-cell variance -- "
        "the Welch omnibus test (below) is a more useful summary."
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
        "| metric | sweep A | sweep B | mean diff | Hedges' g | 95% CI on g | raw p | p (Holm) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in by_abs_g:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["metric"]),
                    str(r["sweep_a"]),
                    str(r["sweep_b"]),
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
        "| metric | sweep A | sweep B | mean diff | Hedges' g | raw p | p (Holm) |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in by_pval:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["metric"]),
                    str(r["sweep_a"]),
                    str(r["sweep_b"]),
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
            "| sweep A | sweep B | n_A | n_B | mean A | mean B | mean diff | "
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
                        str(r["sweep_a"]),
                        str(r["sweep_b"]),
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
    out_md.write_text("\n".join(lines))
    print(f"Wrote {out_md}")
    return 0


def _run_significance(args: argparse.Namespace, kept_runs: list[Any]) -> int:
    if args.no_significance:
        return 0

    mode = _resolve_significance_mode(args)
    if mode is None:
        print(
            "warning: skipping significance: no --summary-csv, --delta-csv or "
            "--reference given, and no default stats CSV found",
            file=sys.stderr,
        )
        return 0

    if mode == "delta":
        if not args.delta_csv:
            print(
                "error: --significance-mode delta requires --delta-csv PATH",
                file=sys.stderr,
            )
            return 2
        delta_csv = Path(args.delta_csv)
        return _run_delta_significance(
            delta_csv, delta_csv.parent, args.significance_md
        )

    if mode == "root":
        return _run_root_significance(args, kept_runs)

    # summary
    summary_csv = Path(args.summary_csv) if args.summary_csv else Path(sig.STATS_CSV)
    return _run_summary_significance(summary_csv, args.significance_md)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    roots = list(args.root or []) + list(args.root_opt or [])
    if not roots:
        # A pure significance invocation (summary / per-run-delta) does not need
        # an output root -- it reads an existing CSV. Preserve that so the
        # deprecated `sweep_significance.py` CLI keeps working without a root.
        explicit_significance = bool(
            args.delta_csv
            or args.summary_csv
            or args.significance_mode in ("summary", "delta")
        )
        if explicit_significance and not args.no_significance:
            return _run_significance(args, [])
        print("error: a root path is required (positional or --root)", file=sys.stderr)
        return 2

    roots_abs = [os.path.abspath(root) for root in roots]
    missing = [root for root in roots_abs if not os.path.exists(root)]
    if missing:
        for root in missing:
            print(f"error: path does not exist: {root}", file=sys.stderr)
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

    # Augment any benign-baseline runs (``adversary_count == 0``) with the
    # counterfactual coalition metrics so Table C's paired-by-seed delta and the
    # root significance mode resolve for the coalition-aware columns. In-memory
    # only; no ``metrics.json`` is mutated.
    augment = smt._ensure_baseline_coalition_helpers()
    inferred_colluders = augment(kept_runs)
    if inferred_colluders:
        print(
            f"baseline coalition inferred from non-baseline sweeps: "
            f"{sorted(inferred_colluders)}"
        )

    totals_by_model: dict[str, dict[str, int]] = {}
    for run in runs:
        model_totals = totals_by_model.setdefault(run.model, {})
        model_totals[run.sweep] = model_totals.get(run.sweep, 0) + 1

    grouped = smt.group_runs(kept_runs)

    if args.sweeps:
        grouped = smt.filter_groups_by_sweep(grouped, set(args.sweeps))
    if args.sources:
        grouped = smt.filter_groups_by_source(grouped, set(args.sources))
    grouped = {model: groups for model, groups in grouped.items() if groups}
    if (args.sweeps or args.sources) and not grouped:
        print("warning: no groups matched the requested filters", file=sys.stderr)

    selected_runs = [
        run
        for model_groups in grouped.values()
        for group in model_groups
        for run in group.runs
    ]
    source_summaries = smt.summarize_sources(selected_runs)

    models = sorted(grouped)

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
    print(f"ddof:      {args.ddof}")
    print(f"center:    {args.center}")

    groups: dict[str, dict[str, GroupMetrics]] = {}
    for model in models:
        groups[model] = {
            group.label: smt.compute_group_metrics(group.runs, specs, args.ddof)
            for group in grouped[model]
        }

    delta_specs = [METRICS_BY_KEY[k] for k in DELTA_METRICS if k in METRICS_BY_KEY]
    show_delta = not args.no_delta

    if args.fmt == "plain":
        for model in models:
            smt.print_inventory(grouped[model], model, totals_by_model.get(model))
            smt.print_metrics_table(
                [group.label for group in grouped[model]],
                groups[model],
                specs,
                model,
                args.center,
            )

        if show_delta:
            for model in models:
                reference = smt.resolve_reference(grouped[model], args.reference, model)
                if reference is None:
                    continue
                smt.print_delta_table(
                    grouped[model],
                    reference,
                    delta_specs,
                    model,
                    args.ddof,
                    args.center,
                )
    else:
        smt.print_transposed_output(
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
            args.center,
        )

    if args.csv_path:
        smt.write_csv(args.csv_path, specs, models, grouped, groups)
        print(f"\nWrote CSV: {os.path.abspath(args.csv_path)}")
    if args.json_path:
        smt.write_json(
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
        )
        print(f"Wrote JSON: {os.path.abspath(args.json_path)}")

    return _run_significance(args, kept_runs)


if __name__ == "__main__":
    sys.exit(main())
