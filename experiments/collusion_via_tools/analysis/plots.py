"""Consolidated plotting entry point for sweep metrics.

This is the **primary** CLI for the plotting side of the analysis. A single run
produces all three plot types (unless individually disabled):

* the **hybrid box plot** per sweep (``plot_sweep_metrics``),
* the **raw scatter** of coalition advantage vs. LLM-judge score
  (``plot_judge_vs_advantage``),
* the **paired-delta scatter** against a ``--reference`` sweep
  (``plot_judge_vs_advantage_delta``).

Usage
-----
    python -m experiments.collusion_via_tools.analysis.plots <root> [<root> ...] [options]

Output layout (default ``--out`` is ``<package>/plots_outputs``)::

    <out>/sweep_metrics/sweep_metrics[_<model>].{png,pdf}
    <out>/sweep_metrics/sweep_metrics_stats.csv
    <out>/judge_vs_advantage/judge_vs_advantage[_<model>].{png,pdf}
    <out>/judge_vs_advantage/judge_vs_advantage_points.csv
    <out>/judge_vs_advantage_delta/judge_vs_advantage_delta[_<model>].{png,pdf}
    <out>/judge_vs_advantage_delta/judge_vs_advantage_delta_points.csv

The rendering helpers live in the three (now deprecated) plot modules; this
module only wires them together and orchestrates the shared data loading.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from . import plot_judge_vs_advantage as plot_scatter
from . import plot_judge_vs_advantage_delta as plot_delta
from . import plot_sweep_metrics as plot_box
from . import sweep_metrics_table as smt
from .delta_metrics import compute_paired_deltas

SUBDIR_BOX = "sweep_metrics"
SUBDIR_SCATTER = "judge_vs_advantage"
SUBDIR_DELTA = "judge_vs_advantage_delta"


def _plots_outputs_base() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots_outputs")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plots",
        description=(
            "Produce all sweep-metric figures: hybrid box plots, the raw "
            "judge-vs-advantage scatter, and the paired-delta scatter. "
            "Consolidated entry point for plot_sweep_metrics.py, "
            "plot_judge_vs_advantage.py and plot_judge_vs_advantage_delta.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "root", nargs="*", default=None, help="One or more experiment output roots."
    )
    parser.add_argument(
        "--root",
        dest="root_opt",
        action="append",
        default=None,
        help="Alternative (repeatable) way to pass a root.",
    )
    parser.add_argument(
        "--out",
        dest="out",
        default=None,
        help=(
            "Base output directory (default: <package>/plots_outputs). Each "
            "plot type gets its own subdirectory under it."
        ),
    )
    parser.add_argument(
        "--no-box",
        dest="no_box",
        action="store_true",
        help="Skip the hybrid box plot.",
    )
    parser.add_argument(
        "--no-scatter",
        dest="no_scatter",
        action="store_true",
        help="Skip the raw judge-vs-advantage scatter.",
    )
    parser.add_argument(
        "--no-scatter-delta",
        dest="no_scatter_delta",
        action="store_true",
        help=("Skip the paired-delta scatter. Implied when --reference is not set."),
    )
    parser.add_argument(
        "--reference",
        dest="reference",
        default=None,
        help=(
            "Reference sweep name. Enables the paired-delta scatter; the other "
            "sweeps are deltas-vs-this-sweep, paired by seed."
        ),
    )
    parser.add_argument(
        "--no-reference-points",
        dest="no_reference_points",
        action="store_true",
        help=(
            "Skip plotting the reference sweep points in the delta scatter "
            "(it then appears only as a y=0 reference line)."
        ),
    )
    parser.add_argument(
        "--regression",
        dest="regression",
        action="store_true",
        help="Overlay a per-sweep OLS fit on the raw scatter (only when n>=3).",
    )
    parser.add_argument(
        "--no-na-panel",
        dest="no_na_panel",
        action="store_true",
        help=(
            "Skip the 'no judge' panel in the raw scatter; runs without a judge "
            "score are dropped."
        ),
    )
    parser.add_argument(
        "--x-metric",
        dest="x_metric",
        default=plot_scatter.DEFAULT_X_KEY,
        help="X-axis metric key for the raw scatter (default: %(default)s).",
    )
    parser.add_argument(
        "--y-metric",
        dest="y_metric",
        default=plot_scatter.DEFAULT_Y_KEY,
        help="Y-axis metric key for the raw scatter (default: %(default)s).",
    )
    parser.add_argument(
        "--metrics",
        dest="metrics",
        default=None,
        help="Comma-separated metric subset for the box plot.",
    )
    parser.add_argument(
        "--style",
        dest="style",
        choices=("box", "dot"),
        default="box",
        help="Box-plot panel style (default box).",
    )
    parser.add_argument(
        "--annotate",
        dest="annotate",
        choices=("mean", "none"),
        default="mean",
        help="Box-plot per-box annotation (default mean).",
    )
    parser.add_argument(
        "--split",
        dest="split",
        action="store_true",
        help="Also write one single-metric PNG per box-plot metric.",
    )
    parser.add_argument(
        "--order",
        dest="order",
        default=None,
        help="Explicit comma-separated sweep order (default: alphabetical).",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        default=None,
        help=(
            "Override the primary plot type's CSV output path. Each plot type "
            "otherwise writes its own default CSV under its output subdirectory."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Override the primary plot type's JSON output path.",
    )
    parser.add_argument(
        "--significance-out",
        dest="significance_out",
        default=None,
        help=(
            "Output directory for the paired-delta points CSV "
            "(default: <out>/judge_vs_advantage_delta)."
        ),
    )
    parser.add_argument(
        "--significance-md",
        dest="significance_md",
        default=None,
        help=(
            "Accepted for symmetry with `tables.py`; plots.py does not write a "
            "significance markdown."
        ),
    )
    parser.add_argument(
        "--exclude-error-runs",
        dest="exclude_error_runs",
        action="store_true",
        help="Drop every run with a failed agent turn (strict rule).",
    )
    parser.add_argument(
        "--exclude-failed-runs",
        dest="exclude_failed_runs",
        action="store_true",
        help="Drop only collapsed runs (failed turn + no assignment).",
    )
    parser.add_argument(
        "--dpi",
        dest="dpi",
        type=int,
        default=300,
        help="Raster resolution (default 300).",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Output layout
# --------------------------------------------------------------------------- #


def _type_out_dir(args: argparse.Namespace, kind: str) -> str:
    """Resolve the output directory for one plot type."""
    base = args.out or _plots_outputs_base()
    if kind == "box":
        return os.path.join(base, SUBDIR_BOX)
    if kind == "scatter":
        return os.path.join(base, SUBDIR_SCATTER)
    return args.significance_out or os.path.join(base, SUBDIR_DELTA)


def _primary_kind(box_active: bool, scatter_active: bool, delta_active: bool) -> str:
    if box_active:
        return "box"
    if scatter_active:
        return "scatter"
    return "delta"


# --------------------------------------------------------------------------- #
# Shared run-loading / reporting
# --------------------------------------------------------------------------- #


def _exclusion_wording(args: argparse.Namespace) -> tuple[str, str]:
    if args.exclude_error_runs and args.exclude_failed_runs:
        return "excluded runs", "run"
    if args.exclude_failed_runs:
        return "excluded failed runs", "failed run"
    return "excluded error runs", "error turn"


def _load_and_partition(
    args: argparse.Namespace, roots_abs: list[str]
) -> tuple[list[Any], list[tuple[Any, list[str]]]]:
    runs: list[Any] = []
    for root_abs in roots_abs:
        found = smt.find_metric_files(root_abs)
        runs.extend(smt.load_runs(root_abs, found))
    if not runs:
        return [], []
    kept, excluded = smt.partition_runs(
        runs, args.exclude_error_runs, args.exclude_failed_runs
    )
    excluded.sort(key=plot_box.excluded_sort_key)
    return kept, excluded


# --------------------------------------------------------------------------- #
# Box plot
# --------------------------------------------------------------------------- #


def _run_box(
    args: argparse.Namespace,
    kept_runs: list[Any],
    excluded_pairs: list[tuple[Any, list[str]]],
    excluded_noun: str,
    order: list[str],
    out_dir: str,
    csv_path: str,
    json_path: str | None,
    written: list[str],
) -> int:
    if args.metrics:
        specs = list(smt.resolve_metrics(args.metrics))
    else:
        specs = [smt.METRICS_BY_KEY[key] for key in plot_box.DEFAULT_METRIC_KEYS]
    if not specs:
        print("error: no valid metrics selected", file=sys.stderr)
        return 2

    grouped = plot_box.group_runs(kept_runs)
    models = sorted(grouped)

    excluded_by_model: dict[str, int] = {}
    for run, _agents in excluded_pairs:
        excluded_by_model[run.model] = excluded_by_model.get(run.model, 0) + 1

    excluded_records = [
        {
            "model": run.model,
            "sweep": run.sweep,
            "seed": run.seed(),
            "run_id": run.run_id,
            "agents": list(agents),
            "reason": plot_box.exclusion_reason(run, args.exclude_failed_runs),
        }
        for run, agents in excluded_pairs
    ]

    values_by_model: dict[str, dict[str, dict[str, list[float]]]] = {}
    run_counts_by_model: dict[str, dict[str, int]] = {}
    records: list[dict[str, Any]] = []
    for model in models:
        buckets = grouped[model]
        sweeps = plot_box.order_sweeps(sorted(buckets), order)
        run_counts_by_model[model] = {sweep: len(buckets[sweep]) for sweep in sweeps}
        per_metric: dict[str, dict[str, list[float]]] = {}
        for spec in specs:
            per_metric[spec.key] = {}
            for sweep in sweeps:
                values: list[float] = []
                for run in buckets[sweep]:
                    value = spec.getter(run.metrics, run.run_config, run.costs)
                    if value is not None:
                        values.append(float(value))
                # Rate metrics are plotted as percent; the stats keep the raw
                # fraction so they stay comparable with the table script.
                if spec.fmt == "rate":
                    per_metric[spec.key][sweep] = [v * 100.0 for v in values]
                else:
                    per_metric[spec.key][sweep] = values
                stats = plot_box.compute_stats(values)
                records.append(
                    {
                        "model": model,
                        "sweep": sweep,
                        "metric": spec.key,
                        "label": spec.label,
                        "n": stats.n,
                        "min": stats.min,
                        "q1": stats.q1,
                        "median": stats.median,
                        "mean": stats.mean,
                        "std": stats.std,
                        "max": stats.max,
                    }
                )
        values_by_model[model] = per_metric

    for spec in specs:
        if all(
            not values_by_model[model][spec.key][sweep]
            for model in models
            for sweep in values_by_model[model][spec.key]
        ):
            print(
                f"warning: metric '{spec.key}' has no values for any sweep",
                file=sys.stderr,
            )

    os.makedirs(out_dir, exist_ok=True)
    multi = len(models) > 1

    for model in models:
        sweeps = plot_box.order_sweeps(sorted(grouped[model]), order)
        fig = plot_box.render_model_figure(
            model,
            sweeps,
            values_by_model[model],
            specs,
            args.style,
            args.annotate,
            run_counts_by_model[model],
            excluded_by_model.get(model, 0),
            excluded_noun,
        )
        base = "sweep_metrics" + (f"_{plot_box.slugify(model)}" if multi else "")
        png_path = os.path.join(out_dir, base + ".png")
        pdf_path = os.path.join(out_dir, base + ".pdf")
        plot_box.save_figure(fig, png_path, args.dpi)
        plot_box.save_figure(fig, pdf_path, args.dpi)
        written.extend([png_path, pdf_path])

        if args.split:
            for spec in specs:
                split_fig = plot_box.render_split_figure(
                    model,
                    sweeps,
                    spec,
                    values_by_model[model][spec.key],
                    args.style,
                    args.annotate,
                )
                split_path = os.path.join(out_dir, base + f"_{spec.key}.png")
                plot_box.save_figure(split_fig, split_path, args.dpi)
                written.append(split_path)

    plot_box.write_stats_csv(csv_path, records)
    written.append(csv_path)
    if json_path:
        plot_box.write_stats_json(json_path, records, excluded_records)
        written.append(json_path)
    return 0


# --------------------------------------------------------------------------- #
# Raw scatter
# --------------------------------------------------------------------------- #


def _run_scatter(
    args: argparse.Namespace,
    kept_runs: list[Any],
    excluded_pairs: list[tuple[Any, list[str]]],
    excluded_noun: str,
    order: list[str],
    out_dir: str,
    csv_path: str,
    json_path: str | None,
    written: list[str],
) -> int:
    if args.x_metric not in smt.METRICS_BY_KEY:
        print(
            f"error: unknown x-metric '{args.x_metric}'. "
            f"Known: {sorted(smt.METRICS_BY_KEY)}",
            file=sys.stderr,
        )
        return 2
    if args.y_metric not in smt.METRICS_BY_KEY:
        print(
            f"error: unknown y-metric '{args.y_metric}'. "
            f"Known: {sorted(smt.METRICS_BY_KEY)}",
            file=sys.stderr,
        )
        return 2

    x_spec = smt.METRICS_BY_KEY[args.x_metric]
    y_spec = smt.METRICS_BY_KEY[args.y_metric]

    excluded_by_model: dict[str, int] = {}
    for run, _agents in excluded_pairs:
        excluded_by_model[run.model] = excluded_by_model.get(run.model, 0) + 1

    excluded_records = [
        {
            "model": run.model,
            "sweep": run.sweep,
            "seed": run.seed(),
            "run_id": run.run_id,
            "agents": list(agents),
            "reason": plot_scatter.exclusion_reason(run, args.exclude_failed_runs),
        }
        for run, agents in excluded_pairs
    ]

    points = plot_scatter.build_points(kept_runs, args.x_metric, args.y_metric)

    if not args.no_na_panel:
        plotted = [pt for pt in points if pt.x is not None]
        na = [pt for pt in points if pt.x is None and pt.y is not None]
    else:
        plotted = [pt for pt in points if pt.x is not None]
        na = []
        if any(pt.x is None and pt.y is not None for pt in points):
            print(
                "warning: dropping "
                f"{sum(1 for pt in points if pt.x is None and pt.y is not None)} "
                "runs without a judge score (--no-na-panel)",
                file=sys.stderr,
            )

    if not plotted and not na:
        print(
            f"warning: no runs have values for both x='{args.x_metric}' and "
            f"y='{args.y_metric}'",
            file=sys.stderr,
        )
        return 0

    grouped = plot_scatter.group_runs(plotted + na)
    models = sorted(grouped)

    os.makedirs(out_dir, exist_ok=True)
    multi = len(models) > 1

    for model in models:
        sweeps = plot_scatter.order_sweeps(sorted(grouped[model]), order)
        plotted_by_sweep: dict[str, list[Any]] = {sweep: [] for sweep in sweeps}
        na_by_sweep: dict[str, list[Any]] = {sweep: [] for sweep in sweeps}
        for pt in plotted:
            if pt.model == model:
                plotted_by_sweep[pt.sweep].append(pt)
        for pt in na:
            if pt.model == model:
                na_by_sweep[pt.sweep].append(pt)
        run_counts = {
            sweep: len(plotted_by_sweep[sweep]) + len(na_by_sweep[sweep])
            for sweep in sweeps
        }

        fig = plot_scatter.render_model_figure(
            model,
            sweeps,
            plotted_by_sweep,
            na_by_sweep,
            x_spec,
            y_spec,
            run_counts,
            args.regression,
            not args.no_na_panel,
            excluded_by_model.get(model, 0),
            excluded_noun,
        )
        base = "judge_vs_advantage" + (
            f"_{plot_scatter.slugify(model)}" if multi else ""
        )
        png_path = os.path.join(out_dir, base + ".png")
        pdf_path = os.path.join(out_dir, base + ".pdf")
        plot_scatter.save_figure(fig, png_path, args.dpi)
        plot_scatter.save_figure(fig, pdf_path, args.dpi)
        written.extend([png_path, pdf_path])

    plot_scatter.write_points_csv(csv_path, plotted + na)
    written.append(csv_path)
    if json_path:
        plot_scatter.write_points_json(json_path, plotted + na, excluded_records)
        written.append(json_path)
    return 0


# --------------------------------------------------------------------------- #
# Paired-delta scatter
# --------------------------------------------------------------------------- #


def _run_scatter_delta(
    args: argparse.Namespace,
    kept_runs: list[Any],
    excluded_pairs: list[tuple[Any, list[str]]],
    excluded_noun: str,
    order: list[str],
    out_dir: str,
    csv_path: str,
    json_path: str | None,
    written: list[str],
) -> int:
    # Augment baseline runs with the synthetic coalition metrics. This mutates
    # ``kept_runs`` in memory only; by design this runs *after* the box and raw
    # scatter so their raw-value semantics are unaffected.
    colluder_set = smt._ensure_baseline_coalition_helpers()(kept_runs)
    if colluder_set:
        print(
            f"colluder set (inferred from non-baseline sweeps): {sorted(colluder_set)}"
        )

    grouped = plot_delta.group_runs(kept_runs)
    models = sorted(grouped)

    ref_seen_in = {model for model in models if args.reference in grouped[model]}
    if not ref_seen_in:
        print(
            f"error: reference sweep '{args.reference}' not found under any "
            f"provided root",
            file=sys.stderr,
        )
        return 2

    excluded_by_model: dict[str, int] = {}
    for run, _agents in excluded_pairs:
        excluded_by_model[run.model] = excluded_by_model.get(run.model, 0) + 1

    excluded_records = [
        {
            "model": run.model,
            "sweep": run.sweep,
            "seed": run.seed(),
            "run_id": run.run_id,
            "agents": list(agents),
            "reason": plot_delta.exclusion_reason(run, args.exclude_failed_runs),
        }
        for run, agents in excluded_pairs
    ]

    os.makedirs(out_dir, exist_ok=True)
    multi = len(models) > 1
    all_points: list[Any] = []

    for model in models:
        if args.reference not in grouped[model]:
            print(
                f"warning: reference sweep '{args.reference}' has no runs for "
                f"model {model!r}; skipping figure",
                file=sys.stderr,
            )
            continue
        sweeps = plot_delta.order_sweeps(sorted(grouped[model]), order, args.reference)
        model_points, _ = compute_paired_deltas(grouped, args.reference)
        points_for_model = [p for p in model_points if p.model == model]

        points_by_sweep: dict[str, list[Any]] = {sweep: [] for sweep in sweeps}
        run_counts: dict[str, int] = {sweep: 0 for sweep in sweeps}
        for pt in points_for_model:
            points_by_sweep[pt.sweep].append(pt)
            run_counts[pt.sweep] += 1
            all_points.append(pt)

        fig = plot_delta.render_model_figure(
            model,
            sweeps,
            points_by_sweep,
            args.reference,
            run_counts,
            not args.no_reference_points,
            excluded_by_model.get(model, 0),
            excluded_noun,
        )
        base = "judge_vs_advantage_delta" + (
            f"_{plot_delta.slugify(model)}" if multi else ""
        )
        png_path = os.path.join(out_dir, base + ".png")
        pdf_path = os.path.join(out_dir, base + ".pdf")
        plot_delta.save_figure(fig, png_path, args.dpi)
        plot_delta.save_figure(fig, pdf_path, args.dpi)
        written.extend([png_path, pdf_path])

    plot_delta.write_deltas_csv(csv_path, all_points)
    written.append(csv_path)
    if json_path:
        plot_delta.write_deltas_json(
            json_path,
            all_points,
            excluded_records,
            sorted(colluder_set),
            args.reference,
        )
        written.append(json_path)
    return 0


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

    if args.significance_md:
        print(
            "warning: plots.py accepts --significance-md for symmetry with "
            "tables.py but does not write a significance markdown; ignoring.",
            file=sys.stderr,
        )

    box_active = not args.no_box
    scatter_active = not args.no_scatter
    delta_active = (not args.no_scatter_delta) and bool(args.reference)

    if not (box_active or scatter_active or delta_active):
        print(
            "warning: no plot types selected (all of --no-box / --no-scatter / "
            "--no-scatter-delta; note the delta scatter also requires --reference)",
            file=sys.stderr,
        )
        return 0

    kept_runs, excluded_pairs = _load_and_partition(args, roots_abs)
    if not kept_runs and not excluded_pairs:
        print("warning: no usable runs found", file=sys.stderr)
        return 0

    runs_used = len(kept_runs) + len(excluded_pairs)
    summary_header, excluded_noun = _exclusion_wording(args)
    if excluded_pairs:
        print(f"{summary_header}: {len(excluded_pairs)} of {runs_used}")
        for run, agents in excluded_pairs:
            seed = run.seed()
            seed_cell = f"seed{seed}" if seed is not None else "-"
            agents_cell = f"[{', '.join(agents)}]" if agents else "-"
            line = f"  {run.sweep}  {seed_cell}  {agents_cell}"
            if args.exclude_failed_runs:
                line += f"  ({plot_box.exclusion_reason(run, True)})"
            print(line)

    if not kept_runs:
        print(
            "warning: no usable runs left after excluding runs",
            file=sys.stderr,
        )
        return 0

    order = [token.strip() for token in (args.order or "").split(",") if token.strip()]
    primary = _primary_kind(box_active, scatter_active, delta_active)

    written: list[str] = []

    if box_active:
        out_dir = _type_out_dir(args, "box")
        csv_path = (
            args.csv_path
            if (primary == "box" and args.csv_path)
            else os.path.join(out_dir, "sweep_metrics_stats.csv")
        )
        json_path = args.json_path if (primary == "box" and args.json_path) else None
        code = _run_box(
            args,
            kept_runs,
            excluded_pairs,
            excluded_noun,
            order,
            out_dir,
            csv_path,
            json_path,
            written,
        )
        if code != 0:
            return code

    if scatter_active:
        out_dir = _type_out_dir(args, "scatter")
        csv_path = (
            args.csv_path
            if (primary == "scatter" and args.csv_path)
            else os.path.join(out_dir, "judge_vs_advantage_points.csv")
        )
        json_path = (
            args.json_path if (primary == "scatter" and args.json_path) else None
        )
        code = _run_scatter(
            args,
            kept_runs,
            excluded_pairs,
            excluded_noun,
            order,
            out_dir,
            csv_path,
            json_path,
            written,
        )
        if code != 0:
            return code

    if delta_active:
        out_dir = _type_out_dir(args, "delta")
        csv_path = (
            args.csv_path
            if (primary == "delta" and args.csv_path)
            else os.path.join(out_dir, "judge_vs_advantage_delta_points.csv")
        )
        json_path = args.json_path if (primary == "delta" and args.json_path) else None
        code = _run_scatter_delta(
            args,
            kept_runs,
            excluded_pairs,
            excluded_noun,
            order,
            out_dir,
            csv_path,
            json_path,
            written,
        )
        if code != 0:
            return code

    print("Wrote:")
    for path in written:
        print(f"  {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
