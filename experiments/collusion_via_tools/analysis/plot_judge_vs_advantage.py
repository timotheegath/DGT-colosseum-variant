"""Scatter plot of coalition advantage vs. LLM-judge score, one series per sweep.

Usage
-----
    python -m experiments.collusion_via_tools.analysis.plot_judge_vs_advantage \
        <root> [<root> ...] [options]

Why this plot
-------------
The two scalar summaries of "did collusion happen" are:

* **coalition_minus_noncoalition_avg** -- the coalition's average reward minus
  the non-coalition's average reward (the *outcome* axis; "did it actually
  pay off for the adversaries?").
* **collusion_judge_simple_mean** -- the post-hoc LLM judge's average Likert
  score on the simple rubric (the *detection* axis; "did the cooperative
  agents' auditor / an external reader think the protocol was suspicious?").

Putting them on the same scatter, with one series per sweep, lets the eye ask
"are the runs where the judge gave a high score also the runs where the
adversaries extracted a large reward?", which a per-sweep box plot of either
axis alone cannot answer. Each run is one point, paired by seed across
sweeps, so a sweep that consistently pulls both axes up shows as a cluster
in the upper-right.

Runs whose judge score is ``None`` (the judge did not run for that run) are
drawn on a separate "no judge" panel axis at ``x = -0.5`` so they are
visible without warping the correlation axis. This matches the same "n/a"
convention used elsewhere in the analysis package.

Why a per-model figure
----------------------
Like ``plot_sweep_metrics.py``, runs are grouped by model first because
different models produce incomparable reward ranges. One figure per model,
sweeps as series inside each figure.

Data plumbing
-------------
The script reuses the sibling ``sweep_metrics_table`` module for discovery,
loading and run-health partitioning, so it always agrees with
``sweep_metrics_table.py`` and ``plot_sweep_metrics.py`` on what counts as a
run, what counts as a sweep, and which runs are dropped.

Run-health flags
----------------
``--exclude-error-runs`` and ``--exclude-failed-runs`` are forwarded to
``partition_runs``; their meaning is identical to ``plot_sweep_metrics.py``.

Options
-------
    --root PATH            Alternative (repeatable) way to pass a root.
    --out DIR              Output directory (default: <package>/plots_outputs/judge_vs_advantage).
    --x-metric KEY         X-axis metric (default ``collusion_judge_simple_mean``).
    --y-metric KEY         Y-axis metric (default ``coalition_minus_noncoalition_avg``).
    --order SWEEP,SWEEP    Explicit sweep order (default: alphabetical).
    --exclude-error-runs   Drop every run with a failed agent turn (strict).
    --exclude-failed-runs  Drop only collapsed runs (failed turn + no assignment).
    --regression           Overlay a per-sweep OLS line (n>=3).
    --no-na-panel          Don't draw the "no judge" panel; drop those runs silently.
    --csv PATH             Write the per-run rows used to draw the figure.
    --json PATH            Write the same rows plus excluded-run metadata.
    --dpi N                Raster resolution (default 300).

Exit codes: 2 when no root is given or a root does not exist; 0 (with a warning
on stderr) when no runs survive filtering.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

# --------------------------------------------------------------------------- #
# Sibling module import (relative-safe, tolerant of transient partial writes)
# --------------------------------------------------------------------------- #

_IMPORT_RETRIES = 5
_IMPORT_RETRY_DELAY = 30  # seconds

_REQUIRED_ATTRS = (
    "find_metric_files",
    "load_runs",
    "METRICS_BY_KEY",
    "partition_runs",
    "run_is_failed",
)


def _import_sweep_metrics_table() -> Any:
    """Import the sibling ``sweep_metrics_table`` module.

    Another process may be editing that file concurrently, which can leave it
    momentarily unparsable or missing a freshly added helper. We therefore
    retry a handful of times before giving up, and never mutate the file to
    work around a failure.
    """
    last_exc: BaseException | None = None
    for attempt in range(_IMPORT_RETRIES):
        try:
            try:
                from . import sweep_metrics_table as module
            except ImportError:
                import importlib

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
            missing = [name for name in _REQUIRED_ATTRS if not hasattr(module, name)]
            if missing:
                raise ImportError(
                    f"sweep_metrics_table is missing {', '.join(missing)}"
                )
            return module
        except (ImportError, SyntaxError) as exc:
            last_exc = exc
            if attempt < _IMPORT_RETRIES - 1:
                print(
                    "warning: transient import failure of sweep_metrics_table "
                    f"({exc!r}); retrying in {_IMPORT_RETRY_DELAY}s "
                    f"({attempt + 1}/{_IMPORT_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(_IMPORT_RETRY_DELAY)
    raise ImportError(
        f"could not import sweep_metrics_table after {_IMPORT_RETRIES} attempts"
    ) from last_exc


_smt = _import_sweep_metrics_table()

find_metric_files = _smt.find_metric_files
load_runs = _smt.load_runs
METRICS_BY_KEY = _smt.METRICS_BY_KEY
partition_runs = _smt.partition_runs
run_is_failed = _smt.run_is_failed

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_X_KEY = "collusion_judge_simple_mean"
DEFAULT_Y_KEY = "coalition_minus_noncoalition_avg"

# x position used for runs whose x metric is None ("no judge"). Kept as a
# sentinel separate from the real axis range (the judge is on 0-5) so those
# points never warp the regression axis.
NA_X_SENTINEL = -0.5

# Per-sweep color cycle (matplotlib tab10 is the default, but tab10's first
# four colours include the box-plot blue and the mean-orange, which we already
# use elsewhere in the package, so we pick a distinct palette here).
SWEEP_COLORS = [
    "#1f77b4",  # blue
    "#d62728",  # red
    "#2ca02c",  # green
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#17becf",  # cyan
    "#bcbd22",  # olive
    "#7f7f7f",  # grey
]

# Per-sweep marker cycle (10 distinct matplotlib markers, kept visually
# separable for the colour-blind and for B/W printing).
SWEEP_MARKERS = [
    "o",  # circle
    "s",  # square
    "^",  # triangle up
    "D",  # diamond
    "v",  # triangle down
    "P",  # plus filled
    "X",  # x filled
    "*",  # star
    "h",  # hexagon
    "<",  # triangle left
]

NA_COLOR = "#999999"
NA_MARKER = "x"
NA_EDGE = "#666666"
NA_FILL_ALPHA = 0.6
GRID_COLOR = "#e3e3e3"
SPINE_COLOR = "#bbbbbb"
ANNOT_COLOR = "#444444"
REGRESSION_COLOR = "#444444"
REGRESSION_LINESTYLE = "--"
POINT_SIZE = 70
POINT_EDGE_WIDTH = 0.6
JITTER_HALF = 0.06  # small x-jitter so overlapping points separate visually

DETAIL_FONT = 10

# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


@dataclass
class Point:
    """One run rendered as one dot: its (x, y) plus identity for the CSV."""

    model: str
    sweep: str
    seed: int | None
    run_id: str
    x: float | None  # None means the x metric was unavailable for this run
    y: float | None  # None means the y metric was unavailable for this run


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def slugify(text: str) -> str:
    """Filesystem-safe slug for a model label."""
    out = []
    for char in text.lower():
        out.append(char if char.isalnum() else "_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "model"


def group_runs(runs: list[Any]) -> dict[str, dict[str, list[Any]]]:
    """Group runs by model then by sweep name only (source is provenance)."""
    grouped: dict[str, dict[str, list[Any]]] = {}
    for run in runs:
        grouped.setdefault(run.model, {}).setdefault(run.sweep, []).append(run)
    return grouped


def order_sweeps(sweeps: list[str], order: list[str]) -> list[str]:
    """Return ``sweeps`` in ``order`` first, then the remainder alphabetically."""
    known = set(sweeps)
    ordered = [sweep for sweep in order if sweep in known]
    remainder = sorted(sweep for sweep in sweeps if sweep not in ordered)
    return ordered + remainder


def excluded_sort_key(item: tuple[Any, list[str]]) -> tuple[str, str, int, str]:
    """Deterministic ordering for ``(run, agents)`` exclusion pairs."""
    run = item[0]
    seed = run.seed()
    return (run.model, run.sweep, seed if seed is not None else -1, run.run_id)


def exclusion_reason(run: Any, exclude_failed_runs: bool) -> str:
    """Reason string for an excluded run, mirroring the table script."""
    if exclude_failed_runs and run_is_failed(run):
        return "failed: error turn, 0 variables assigned"
    return "error turn"


def build_points(
    runs: list[Any],
    x_key: str,
    y_key: str,
) -> list[Point]:
    """Convert runs into (x, y) points, dropping runs where *both* are missing.

    A point with only the y value present (judge did not run) is kept and
    flagged ``x=None`` so the caller can route it to the "no judge" panel.
    A point with only the x value present is dropped because nothing useful
    can be plotted for it on a 2-D scatter; we do not invent a y.
    """
    x_spec = METRICS_BY_KEY[x_key]
    y_spec = METRICS_BY_KEY[y_key]
    points: list[Point] = []
    for run in runs:
        x_value = x_spec.getter(run.metrics, run.run_config, run.costs)
        y_value = y_spec.getter(run.metrics, run.run_config, run.costs)
        if x_value is None and y_value is None:
            continue
        points.append(
            Point(
                model=run.model,
                sweep=run.sweep,
                seed=run.seed(),
                run_id=run.run_id,
                x=float(x_value) if x_value is not None else None,
                y=float(y_value) if y_value is not None else None,
            )
        )
    return points


def _jittered_x(base_x: float, count: int, idx: int, rng: random.Random) -> float:
    """Deterministic per-point x jitter so overlapping points separate visually.

    ``idx`` is the point's index within its sweep so the jitter is stable
    across renders.
    """
    if count <= 1:
        return base_x
    # Spread the jitter across [-JITTER_HALF, +JITTER_HALF] using a stable
    # hash of (idx, sweep_key) is overkill here; an RNG keyed on the index is
    # enough for visual separation and is reproducible.
    return base_x + rng.uniform(-JITTER_HALF, JITTER_HALF)


def _wrap_detail(detail: str, fig_width_in: float) -> list[str]:
    """Wrap the runs-per-sweep detail so it never overflows the figure width."""
    max_chars = max(40, int(fig_width_in * 72.0 / (0.6 * DETAIL_FONT)))
    return textwrap.wrap(detail, width=max_chars) or [detail]


def _fit_ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Return ``(slope, intercept)`` for the OLS fit, or None if undefined."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    intercept = mean_y - slope * mean_x
    return slope, intercept


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #


def _style_axes(ax: Any, x_label: str, y_label: str) -> None:
    ax.set_xlabel(x_label, fontsize=11)
    ax.set_ylabel(y_label, fontsize=11)
    ax.grid(True, color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(SPINE_COLOR)
    ax.tick_params(labelsize=9)


def _format_axis_value(value: float, fmt: str) -> str:
    if fmt == "rate":
        return f"{value * 100:.0f}%"
    if fmt == "likert":
        return f"{value:.1f}"
    if fmt == "reward":
        return f"{value:,.0f}"
    if fmt == "ratio":
        return f"{value:.2f}"
    return f"{value:.2f}"


def render_model_figure(
    model: str,
    sweeps: list[str],
    points_by_sweep: dict[str, list[Point]],
    no_x_points_by_sweep: dict[str, list[Point]],
    x_spec: Any,
    y_spec: Any,
    run_counts: dict[str, int],
    show_regression: bool,
    show_na_panel: bool,
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> Any:
    """Render the per-model scatter figure."""
    # Filter to sweeps that contribute at least one point so the legend and
    # the colour/marker assignment stay compact.
    visible_sweeps = [
        sweep
        for sweep in sweeps
        if points_by_sweep.get(sweep) or no_x_points_by_sweep.get(sweep)
    ]

    fig, ax = plt.subplots(1, 1, figsize=(8.5, 6.0))

    color_for_sweep = {
        sweep: SWEEP_COLORS[i % len(SWEEP_COLORS)]
        for i, sweep in enumerate(visible_sweeps)
    }
    marker_for_sweep = {
        sweep: SWEEP_MARKERS[i % len(SWEEP_MARKERS)]
        for i, sweep in enumerate(visible_sweeps)
    }

    rng = random.Random(0)

    # Real (x, y) points first; the no-judge panel points go on top so their
    # sentinel position is visible.
    for sweep in visible_sweeps:
        pts = points_by_sweep.get(sweep, [])
        if not pts:
            continue
        color = color_for_sweep[sweep]
        marker = marker_for_sweep[sweep]
        xs = []
        ys = []
        for idx, pt in enumerate(pts):
            if pt.x is None or pt.y is None:
                # Defensive: build_points only drops both-missing runs, but a
                # single sweep could contain a y-only run if --x-metric was
                # chosen to be one that some runs lack. Keep those in the
                # no-judge bucket rather than plotting them at (NaN, y).
                continue
            xs.append(_jittered_x(pt.x, len(pts), idx, rng))
            ys.append(pt.y)
        if not xs:
            continue
        ax.scatter(
            xs,
            ys,
            s=POINT_SIZE,
            c=color,
            marker=marker,
            edgecolors="black",
            linewidths=POINT_EDGE_WIDTH,
            alpha=0.85,
            zorder=3,
            label=f"{sweep} (n={len(xs)})",
        )
        if show_regression and len(xs) >= 3:
            fit = _fit_ols(xs, ys)
            if fit is not None:
                slope, intercept = fit
                x_left, x_right = ax.get_xlim()
                line_xs = [min(x_left, min(xs)), max(x_right, max(xs))]
                line_ys = [slope * xv + intercept for xv in line_xs]
                ax.plot(
                    line_xs,
                    line_ys,
                    color=REGRESSION_COLOR,
                    linestyle=REGRESSION_LINESTYLE,
                    linewidth=1.2,
                    alpha=0.8,
                    zorder=2,
                )

    # No-judge panel: separate x position, smaller dimmer markers, single
    # legend entry rather than one per sweep (otherwise the panel doubles the
    # legend and hides the real correlation signal).
    if show_na_panel:
        any_no_x = any(no_x_points_by_sweep.get(s) for s in visible_sweeps)
        if any_no_x:
            for sweep in visible_sweeps:
                pts = no_x_points_by_sweep.get(sweep, [])
                if not pts:
                    continue
                color = color_for_sweep[sweep]
                marker = marker_for_sweep[sweep]
                ax.scatter(
                    [NA_X_SENTINEL] * len(pts),
                    [pt.y for pt in pts],
                    s=POINT_SIZE * 0.55,
                    c=color,
                    marker=marker,
                    edgecolors=NA_EDGE,
                    linewidths=POINT_EDGE_WIDTH,
                    alpha=NA_FILL_ALPHA,
                    zorder=2,
                )
            # Vertical separator between the no-judge column and the real
            # axis so the sentinel position never gets read as a -0.5 score.
            ax.axvline(
                x=(NA_X_SENTINEL + 0) / 2,
                color=NA_COLOR,
                linewidth=0.8,
                linestyle=":",
                zorder=1,
            )
            ax.text(
                NA_X_SENTINEL,
                0.98,
                "no\njudge",
                transform=ax.get_xaxis_transform(),
                ha="center",
                va="top",
                fontsize=8,
                color=ANNOT_COLOR,
            )

    _style_axes(
        ax,
        x_label=f"{x_spec.label} ({_axis_unit(x_spec.fmt)})",
        y_label=f"{y_spec.label} ({_axis_unit(y_spec.fmt)})",
    )

    if show_na_panel:
        ax.axvspan(
            NA_X_SENTINEL - 0.3,
            NA_X_SENTINEL + 0.3,
            color=NA_COLOR,
            alpha=0.06,
            zorder=0,
        )
        ax.set_xlim(NA_X_SENTINEL - 0.35, None)

    title, detail = _suptitle_parts(
        model, sweeps, run_counts, excluded_count, excluded_noun
    )
    fig.suptitle(title, fontsize=14, fontweight="bold", y=0.985)
    detail_lines = _wrap_detail(detail, fig.get_figwidth())
    fig.text(
        0.5,
        0.945,
        "\n".join(detail_lines),
        ha="center",
        va="top",
        fontsize=DETAIL_FONT,
        color=ANNOT_COLOR,
        linespacing=1.25,
    )

    # Legend: each sweep is one entry (with its marker+colour), plus an
    # optional "no judge" entry if the panel is shown.
    handles = [
        Line2D(
            [0],
            [0],
            marker=marker_for_sweep[sweep],
            color="w",
            markerfacecolor=color_for_sweep[sweep],
            markeredgecolor="black",
            markeredgewidth=POINT_EDGE_WIDTH,
            markersize=9,
            label=f"{sweep} (n={len(points_by_sweep.get(sweep, []))})",
        )
        for sweep in visible_sweeps
    ]
    if show_na_panel and any(no_x_points_by_sweep.get(s) for s in visible_sweeps):
        handles.append(
            Line2D(
                [0],
                [0],
                marker=NA_MARKER,
                color="w",
                markerfacecolor=NA_COLOR,
                markeredgecolor=NA_EDGE,
                markeredgewidth=POINT_EDGE_WIDTH,
                markersize=9,
                label="no judge score",
            )
        )
    if show_regression:
        handles.append(
            Line2D(
                [0],
                [0],
                color=REGRESSION_COLOR,
                linestyle=REGRESSION_LINESTYLE,
                linewidth=1.2,
                label="OLS fit (n>=3)",
            )
        )

    ax.legend(
        handles=handles,
        loc="best",
        frameon=False,
        fontsize=9,
        title="sweep",
        title_fontsize=10,
    )

    top = 0.90 - 0.03 * (len(detail_lines) - 1)
    fig.tight_layout(rect=(0, 0, 1, top))
    return fig


def _axis_unit(fmt: str) -> str:
    if fmt == "rate":
        return "%"
    if fmt == "likert":
        return "0-5"
    if fmt == "reward":
        return "points"
    if fmt == "ratio":
        return "ratio"
    if fmt == "count":
        return "count"
    if fmt == "tokens":
        return "tokens"
    return fmt


def _suptitle_parts(
    model: str,
    sweeps: list[str],
    run_counts: dict[str, int],
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> tuple[str, str]:
    """Title + runs-per-sweep detail line, mirroring ``plot_sweep_metrics``."""
    title = f"Coalition advantage vs. LLM-judge score \u2014 {model or '-'}"
    counts = [run_counts.get(sweep, 0) for sweep in sweeps]
    if counts and len(set(counts)) <= 1:
        n = counts[0]
        detail = f"{n} runs per sweep"
    else:
        listing = ", ".join(f"{sweep}={run_counts.get(sweep, 0)}" for sweep in sweeps)
        detail = f"runs per sweep: {listing}"
    if excluded_count:
        noun = excluded_noun if excluded_count == 1 else f"{excluded_noun}s"
        detail += f" ({excluded_count} excluded: {noun})"
    return title, detail


def save_figure(fig: Any, path: str, dpi: int) -> None:
    fig.savefig(path, dpi=dpi, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# File outputs
# --------------------------------------------------------------------------- #


def _num_str(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def write_points_csv(path: str, points: list[Point]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["model", "sweep", "seed", "run_id", "x", "y"])
        for pt in points:
            writer.writerow(
                [
                    pt.model,
                    pt.sweep,
                    pt.seed if pt.seed is not None else "",
                    pt.run_id,
                    _num_str(pt.x),
                    _num_str(pt.y),
                ]
            )


def write_points_json(
    path: str,
    points: list[Point],
    excluded_records: list[dict[str, Any]],
) -> None:
    payload = {
        "points": [
            {
                "model": pt.model,
                "sweep": pt.sweep,
                "seed": pt.seed,
                "run_id": pt.run_id,
                "x": pt.x,
                "y": pt.y,
            }
            for pt in points
        ],
        "excluded_runs": excluded_records,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plot_judge_vs_advantage",
        description=(
            "Scatter coalition advantage (y) vs. LLM-judge score (x); one series "
            "per sweep."
        ),
    )
    parser.add_argument(
        "root",
        nargs="*",
        default=None,
        help="One or more experiment output roots.",
    )
    parser.add_argument(
        "--root",
        dest="root_opt",
        action="append",
        default=None,
        help="Alternative (repeatable) way to pass a root.",
    )
    parser.add_argument("--out", dest="out", default=None, help="Output directory.")
    parser.add_argument(
        "--x-metric",
        dest="x_metric",
        default=DEFAULT_X_KEY,
        help=(
            "X-axis metric key (default: coalition_judge_simple_mean). "
            "Must exist in sweep_metrics_table.METRICS_BY_KEY."
        ),
    )
    parser.add_argument(
        "--y-metric",
        dest="y_metric",
        default=DEFAULT_Y_KEY,
        help=(
            "Y-axis metric key (default: coalition_minus_noncoalition_avg). "
            "Must exist in sweep_metrics_table.METRICS_BY_KEY."
        ),
    )
    parser.add_argument(
        "--order",
        dest="order",
        default=None,
        help="Explicit comma-separated sweep order (default alphabetical).",
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
        "--regression",
        dest="regression",
        action="store_true",
        help="Overlay a per-sweep OLS fit (only when n>=3 points are available).",
    )
    parser.add_argument(
        "--no-na-panel",
        dest="no_na_panel",
        action="store_true",
        help="Skip the 'no judge' panel; runs without a judge score are dropped.",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None, help="Write the per-run points here."
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Write the per-run points plus excluded-run metadata as JSON.",
    )
    parser.add_argument(
        "--dpi",
        dest="dpi",
        type=int,
        default=300,
        help="Raster resolution (default 300).",
    )
    return parser.parse_args(argv)


def default_out_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "plots_outputs",
        "judge_vs_advantage",
    )


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

    if args.x_metric not in METRICS_BY_KEY:
        print(
            f"error: unknown x-metric '{args.x_metric}'. Known: {sorted(METRICS_BY_KEY)}",
            file=sys.stderr,
        )
        return 2
    if args.y_metric not in METRICS_BY_KEY:
        print(
            f"error: unknown y-metric '{args.y_metric}'. Known: {sorted(METRICS_BY_KEY)}",
            file=sys.stderr,
        )
        return 2

    x_spec = METRICS_BY_KEY[args.x_metric]
    y_spec = METRICS_BY_KEY[args.y_metric]

    runs: list[Any] = []
    for root_abs in roots_abs:
        found = find_metric_files(root_abs)
        runs.extend(load_runs(root_abs, found))
    if not runs:
        print("warning: no usable runs found", file=sys.stderr)
        return 0

    runs_used = len(runs)
    kept_runs, excluded_pairs = partition_runs(
        runs, args.exclude_error_runs, args.exclude_failed_runs
    )
    excluded_pairs.sort(key=excluded_sort_key)

    if args.exclude_error_runs and args.exclude_failed_runs:
        summary_header = "excluded runs"
        excluded_noun = "run"
    elif args.exclude_failed_runs:
        summary_header = "excluded failed runs"
        excluded_noun = "failed run"
    else:
        summary_header = "excluded error runs"
        excluded_noun = "error turn"

    if excluded_pairs:
        print(f"{summary_header}: {len(excluded_pairs)} of {runs_used}")
        for run, agents in excluded_pairs:
            seed = run.seed()
            seed_cell = f"seed{seed}" if seed is not None else "-"
            agents_cell = f"[{', '.join(agents)}]" if agents else "-"
            line = f"  {run.sweep}  {seed_cell}  {agents_cell}"
            if args.exclude_failed_runs:
                line += f"  ({exclusion_reason(run, True)})"
            print(line)

    if not kept_runs:
        print(
            "warning: no usable runs left after excluding runs",
            file=sys.stderr,
        )
        return 0

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
            "reason": exclusion_reason(run, args.exclude_failed_runs),
        }
        for run, agents in excluded_pairs
    ]

    points = build_points(kept_runs, args.x_metric, args.y_metric)

    if not args.no_na_panel:
        plotted = [pt for pt in points if pt.x is not None]
        na = [pt for pt in points if pt.x is None and pt.y is not None]
    else:
        plotted = [pt for pt in points if pt.x is not None]
        na = []
        if any(pt.x is None and pt.y is not None for pt in points):
            print(
                f"warning: dropping {sum(1 for pt in points if pt.x is None and pt.y is not None)} "
                "runs without a judge score (--no-na-panel)",
                file=sys.stderr,
            )

    if not plotted and not na:
        print(
            f"warning: no runs have values for both x='{args.x_metric}' and y='{args.y_metric}'",
            file=sys.stderr,
        )
        return 0

    grouped = group_runs(plotted + na)
    models = sorted(grouped)
    order = [token.strip() for token in (args.order or "").split(",") if token.strip()]

    out_dir = args.out or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    multi = len(models) > 1

    written: list[str] = []

    for model in models:
        sweeps_all = sorted(grouped[model])
        sweeps = order_sweeps(sweeps_all, order)
        plotted_by_sweep: dict[str, list[Point]] = {sweep: [] for sweep in sweeps}
        na_by_sweep: dict[str, list[Point]] = {sweep: [] for sweep in sweeps}
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

        fig = render_model_figure(
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
        base = "judge_vs_advantage" + (f"_{slugify(model)}" if multi else "")
        png_path = os.path.join(out_dir, base + ".png")
        pdf_path = os.path.join(out_dir, base + ".pdf")
        save_figure(fig, png_path, args.dpi)
        save_figure(fig, pdf_path, args.dpi)
        written.extend([png_path, pdf_path])

    csv_path = args.csv_path or os.path.join(out_dir, "judge_vs_advantage_points.csv")
    write_points_csv(csv_path, plotted + na)
    written.append(csv_path)

    if args.json_path:
        write_points_json(args.json_path, plotted + na, excluded_records)
        written.append(args.json_path)

    print("Wrote:")
    for path in written:
        print(f"  {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
