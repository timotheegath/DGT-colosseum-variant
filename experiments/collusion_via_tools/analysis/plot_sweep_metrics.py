"""Plot the distribution of headline metrics per sweep for one or more sweep roots.

Usage
-----
    python -m experiments.collusion_via_tools.analysis.plot_sweep_metrics \
        <root> [<root> ...] [options]

Why a hybrid box plot?
----------------------
With only ~10 runs per sweep a plain box plot is a weak summary: the IQR is
estimated from ten points and the outliers (the runs you actually care about)
are hidden behind the ``1.5 * IQR`` rule.  The DEFAULT ``--style box`` therefore
draws a *hybrid* panel:

* a box for the IQR with the median line,
* whiskers that reach **min -> max** (``whis=(0, 100)``) so the extremes are
  literally visible,
* the raw runs as deterministic jittered points (the real evidence), and
* an orange diamond for the mean with a +/- 1 std vertical bar, so the spread
  around the mean is visible next to the min/max range.

An alternative ``--style dot`` drops the box entirely and shows, per sweep, a
thin min-max whisker with caps, a thick mean +/- std bar, a median tick and the
raw points.  It exists to answer the question "is a box plot even the right
call?".

Units
-----
Rate metrics (``spec.fmt == "rate"``, e.g. ``suspicion_rate_non_adversary``)
are plotted as **percent**: the raw 0-1 fraction is multiplied by 100 where the
per-sweep value lists are built, so the y axis, the ``%`` axis label and the
``mu=12.9%`` annotations all agree.  The stats written to ``--csv`` / ``--json``
intentionally keep the **raw fractions** (e.g. ``0.12857142857142856``), so they
stay directly comparable with ``sweep_metrics_table.py``.

The script reuses the stable data plumbing of the sibling
``sweep_metrics_table`` module (``find_metric_files``, ``load_runs``,
``METRICS_BY_KEY``, ``format_value``, ``resolve_metrics``) so that the plot and
the metrics table always agree on grouping.  The per-panel ``mu=...`` notes use
a compact local formatter (0 decimals for reward/count/tokens, 1 for
rate/ratio) rather than ``format_value``, which always keeps one decimal.

Error runs
----------
Two opt-in flags drop invalid runs before grouping and plotting, both using the
shared helpers in ``sweep_metrics_table``:

* ``--exclude-error-runs`` is the STRICT rule: it drops every run that recorded
  a failed agent turn.  It mirrors
  ``experiments/collusion_via_tools/resume.py::_has_error_turn``: a turn recorded
  as ``"[ERROR] ..."`` means an agent dropped out, so the run is incomplete.
* ``--exclude-failed-runs`` is the NARROW rule: it drops only runs that actually
  COLLAPSED (a failed turn *and* no assignment at all, ``variables_assigned`` 0
  or missing).  A failed turn alone can still leave a completed simulation, so
  this keeps the sweep sizes even while removing the runs whose reward is
  dominated by the missed-step penalty (one such run scored
  ``joint_reward = -7760``).

Setting both applies the union.  The stats, the per-panel means and the
runs-per-sweep detail line then use the kept runs only, and the exclusions are
reported on stdout and under ``excluded_runs`` in the JSON output.

Options
-------
    --root PATH            Alternative (repeatable) way to pass a root.
    --out DIR              Output directory (default: <package>/plots_outputs/sweep_metrics).
    --metrics a,b,c        Comma-separated metric subset (default: the six
                           headline metrics, including the post-hoc
                           ``collusion_judge_simple_mean`` Likert judge score
                           (0-5) populated by ``judge_collusion_score``; runs
                           without a judge score render as ``n/a``).
    --style {box,dot}      Panel style (default ``box``, the hybrid described above).
    --annotate {mean,none}
                           Per-box annotation (default ``mean``: ``mu=... +/-...``).
    --exclude-error-runs   Drop every run with a failed agent turn (strict).
    --exclude-failed-runs  Drop only collapsed runs (failed turn + no assignment).
    --split                Also write one PNG per metric.
    --order SWEEP,SWEEP    Explicit sweep order (default: alphabetical).
    --csv PATH             Write the plotted stats (per sweep x metric).
    --json PATH            Write the stats + excluded-run metadata as JSON.
    --dpi N                Raster resolution (default 300).

Exit codes: 2 when no root is given or a root does not exist; 0 (with a warning
on stderr) when a selected metric has no values for any sweep.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# --------------------------------------------------------------------------- #
# Sibling module import (relative-safe, tolerant of transient partial writes)
# --------------------------------------------------------------------------- #

_IMPORT_RETRIES = 5
_IMPORT_RETRY_DELAY = 30  # seconds

# Names the plot script relies on.  The sibling module is edited in parallel, so a
# run-health helper may not be exported yet; that is treated as a transient
# failure and retried rather than worked around.
_REQUIRED_ATTRS = (
    "find_metric_files",
    "load_runs",
    "METRICS_BY_KEY",
    "format_value",
    "resolve_metrics",
    "run_health",
    "run_is_failed",
    "partition_runs",
)


def _import_sweep_metrics_table() -> Any:
    """Import the sibling ``sweep_metrics_table`` module.

    Another process may be editing that file concurrently, which can leave it
    momentarily unparsable or missing a freshly added helper.  We therefore
    retry a handful of times before giving up, and never mutate the file to work
    around a failure.
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
format_value = _smt.format_value
resolve_metrics = _smt.resolve_metrics
run_health = _smt.run_health
run_is_failed = _smt.run_is_failed
partition_runs = _smt.partition_runs

# --------------------------------------------------------------------------- #
# Metric selection and styling constants
# --------------------------------------------------------------------------- #

DEFAULT_METRIC_KEYS: tuple[str, ...] = (
    "joint_reward",
    "joint_reward_regret",
    "coalition_reward_avg",
    "coalition_reward_regret",
    "suspicion_rate_non_adversary",
    "collusion_judge_simple_mean",
)

# Unit shown next to the metric label on the y axis.
UNIT_BY_FMT: dict[str, str] = {
    "reward": "points",
    "rate": "%",
    "count": "count",
    "likert": "0-5",
    "tokens": "tokens",
    "ratio": "ratio",
}

BOX_FACE = "#4c78a8"
BOX_EDGE = "#4c78a8"
MEDIAN_COLOR = "#12324f"
WHISKER_COLOR = "#4c78a8"
POINT_COLOR = "#2b2b2b"
MEAN_COLOR = "#f58518"
GRID_COLOR = "#e3e3e3"
SPINE_COLOR = "#bbbbbb"
NA_COLOR = "#999999"
ANNOT_COLOR = "#666666"

JITTER_HALF = 0.14


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    n: int
    min: float | None
    q1: float | None
    median: float | None
    mean: float | None
    std: float | None
    max: float | None


def compute_stats(values: list[float]) -> Stats:
    """Summary stats for one (model, sweep, metric) cell.

    ``std`` is the sample standard deviation (ddof=1) and is ``None`` when there
    are fewer than two values (undefined).
    """
    n = len(values)
    if n == 0:
        return Stats(0, None, None, None, None, None, None)
    arr = np.asarray(values, dtype=float)
    return Stats(
        n=n,
        min=float(arr.min()),
        q1=float(np.percentile(arr, 25)),
        median=float(np.percentile(arr, 50)),
        mean=float(arr.mean()),
        std=float(arr.std(ddof=1)) if n >= 2 else None,
        max=float(arr.max()),
    )


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
    """Reason string for an excluded run, mirroring the table script.

    Uses the sibling's public ``run_is_failed`` (failed turn AND collapsed) to
    decide between the collapsed-run reason and the generic error-turn reason.
    """
    if exclude_failed_runs and run_is_failed(run):
        return "failed: error turn, 0 variables assigned"
    return "error turn"


def slugify(text: str) -> str:
    """Filesystem-safe slug for a model label."""
    out = []
    for char in text.lower():
        out.append(char if char.isalnum() else "_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "model"


# --------------------------------------------------------------------------- #
# Panel drawing
# --------------------------------------------------------------------------- #


def _draw_missing(ax: Any, position: int) -> None:
    ax.text(
        position,
        0.5,
        "n/a",
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="center",
        color=NA_COLOR,
    )


def _jittered_positions(position: int, count: int, rng: random.Random) -> list[float]:
    return [position + rng.uniform(-JITTER_HALF, JITTER_HALF) for _ in range(count)]


def _compact_number(fmt: str, value: float) -> str:
    """Compact annotation number: 0 decimals for reward/count/tokens, 1 for rate/ratio.

    Rate values arrive already scaled to percent (see the module docstring), so
    they are only suffixed with ``%`` here.  This is deliberately local and does
    not touch ``format_value`` in the sibling module.
    """
    if fmt == "rate":
        return f"{value:.1f}%"
    if fmt == "likert":
        return f"{value:.1f}"
    if fmt == "ratio":
        return f"{value:.1f}"
    return f"{value:,.0f}"


def _annotate_mean(ax: Any, position: int, values: list[float], spec: Any) -> None:
    """Write a compact ``mu=<mean> +/-<std>`` note at the top of the panel.

    The bottom of the panel is crowded (rotated sweep labels), so the note goes
    at the top.  A note is wider than the spacing between two sweeps, so the
    notes alternate between two heights (even positions high, odd positions
    low): notes two slots apart therefore never share a line and never collide.
    The widened x limits and the reserved headroom (``_add_headroom``) keep the
    first/last notes inside the axes.
    """
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) >= 2 else None
    if std is None:
        text = f"\u03bc={_compact_number(spec.fmt, mean)}"
    else:
        text = (
            f"\u03bc={_compact_number(spec.fmt, mean)}"
            f" \u00b1{_compact_number(spec.fmt, std)}"
        )
    ax.annotate(
        text,
        xy=(position, 0.99 if position % 2 == 0 else 0.90),
        xycoords=("data", "axes fraction"),
        ha="center",
        va="top",
        fontsize=8,
        color=ANNOT_COLOR,
        clip_on=False,
        zorder=6,
    )


def _add_headroom(ax: Any) -> None:
    """Reserve vertical space at the top of a panel for the mean annotations."""
    low, high = ax.get_ylim()
    span = high - low
    if span <= 0:
        span = abs(high) if high else 1.0
    ax.set_ylim(low, high + 0.22 * span)


def _draw_box(
    ax: Any,
    position: int,
    values: list[float],
    spec: Any,
    annotate_mode: str,
    rng: random.Random,
) -> None:
    """Hybrid box + points + mean/std rendering for one sweep."""
    if not values:
        _draw_missing(ax, position)
        return

    box = ax.boxplot(
        [values],
        positions=[position],
        widths=0.55,
        whis=(0, 100),
        showfliers=False,
        patch_artist=True,
    )
    for patch in box["boxes"]:
        patch.set_facecolor(to_rgba(BOX_FACE, 0.25))
        patch.set_edgecolor(BOX_EDGE)
        patch.set_linewidth(1.5)
    for median in box["medians"]:
        median.set_color(MEDIAN_COLOR)
        median.set_linewidth(2)
    for key in ("whiskers", "caps"):
        for artist in box[key]:
            artist.set_color(WHISKER_COLOR)
            artist.set_linewidth(1.2)

    xs = _jittered_positions(position, len(values), rng)
    ax.scatter(xs, values, s=18, color=POINT_COLOR, alpha=0.45, zorder=3, linewidths=0)

    mean = statistics.fmean(values)
    if len(values) >= 2:
        std = statistics.stdev(values)
        ax.errorbar(
            position,
            mean,
            yerr=std,
            fmt="D",
            markersize=6,
            color=MEAN_COLOR,
            ecolor=MEAN_COLOR,
            elinewidth=2,
            capsize=4,
            zorder=4,
        )
    else:
        ax.plot(
            [position],
            [mean],
            marker="D",
            markersize=6,
            color=MEAN_COLOR,
            linestyle="none",
            zorder=4,
        )

    if annotate_mode == "mean":
        _annotate_mean(ax, position, values, spec)


def _draw_dot(
    ax: Any,
    position: int,
    values: list[float],
    spec: Any,
    annotate_mode: str,
    rng: random.Random,
) -> None:
    """No-box alternative: min-max whisker, mean/std bar, median tick, points."""
    if not values:
        _draw_missing(ax, position)
        return

    arr = np.asarray(values, dtype=float)
    low = float(arr.min())
    high = float(arr.max())
    median = float(np.percentile(arr, 50))
    mean = float(arr.mean())

    ax.vlines(position, low, high, color=WHISKER_COLOR, linewidth=1.2, zorder=2)
    cap = 0.09
    ax.hlines(
        [low, high],
        position - cap,
        position + cap,
        color=WHISKER_COLOR,
        linewidth=1.2,
        zorder=2,
    )

    if len(values) >= 2:
        std = float(arr.std(ddof=1))
        ax.errorbar(
            position,
            mean,
            yerr=std,
            fmt="o",
            markersize=7,
            elinewidth=8,
            capsize=0,
            color=MEAN_COLOR,
            ecolor=MEAN_COLOR,
            zorder=4,
        )
    else:
        ax.plot(
            [position],
            [mean],
            marker="o",
            markersize=7,
            color=MEAN_COLOR,
            linestyle="none",
            zorder=4,
        )

    ax.hlines(
        median,
        position - 0.18,
        position + 0.18,
        color=MEDIAN_COLOR,
        linewidth=2,
        zorder=5,
    )

    xs = _jittered_positions(position, len(values), rng)
    ax.scatter(xs, values, s=18, color=POINT_COLOR, alpha=0.45, zorder=3, linewidths=0)

    if annotate_mode == "mean":
        _annotate_mean(ax, position, values, spec)


def _style_panel(ax: Any, spec: Any, with_title: bool = True) -> None:
    ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(SPINE_COLOR)
    ax.tick_params(labelsize=9)
    if with_title:
        ax.set_title(spec.label, fontweight="bold", fontsize=12)
    unit = UNIT_BY_FMT.get(spec.fmt, spec.fmt)
    ax.set_ylabel(f"{spec.label} ({unit})", fontsize=10)


def _legend_handles(style: str) -> list[Any]:
    handles: list[Any] = []
    if style == "box":
        handles.append(
            Patch(
                facecolor=to_rgba(BOX_FACE, 0.25),
                edgecolor=BOX_EDGE,
                linewidth=1.5,
                label="IQR (median)",
            )
        )
    else:
        handles.append(
            Line2D([0], [0], color=MEDIAN_COLOR, linewidth=2, label="median")
        )
    handles.append(
        Line2D([0], [0], color=WHISKER_COLOR, linewidth=1.5, label="min\u2013max")
    )
    handles.append(
        Line2D(
            [0],
            [0],
            color=POINT_COLOR,
            marker="o",
            linestyle="none",
            markersize=6,
            alpha=0.45,
            label="run",
        )
    )
    handles.append(
        Line2D(
            [0],
            [0],
            color=MEAN_COLOR,
            marker="D",
            linestyle="none",
            markersize=7,
            label="mean \u00b1 1 std",
        )
    )
    return handles


def _layout(n: int) -> tuple[int, int]:
    """Grid shape for ``n`` panels, always leaving a free cell for the legend."""
    cols = min(3, n)
    rows = math.ceil(n / 3)
    if rows * cols == n:
        # No free cell: grow the grid by one column (if there is room) or one
        # row so the legend always has a cell of its own.
        if cols < 3:
            cols += 1
        else:
            rows += 1
    return rows, cols


def _wrap_sweep_label(sweep: str) -> str:
    """Break a sweep name at its commas so long names fit under a panel.

    Sweep names are descriptive ("Coalition without private channel, 1
    coordination round"), which is far too long for one rotated tick label.
    """
    if ", " not in sweep:
        return sweep
    parts = [part.strip() for part in sweep.split(",")]
    return "\n".join(parts)


def _set_sweep_axis(ax: Any, sweeps: list[str]) -> None:
    ax.set_xlim(-0.8, len(sweeps) - 0.2)
    ax.set_xticks(list(range(len(sweeps))))
    ax.set_xticklabels(
        [_wrap_sweep_label(sweep) for sweep in sweeps],
        rotation=32,
        ha="right",
        fontsize=9,
        rotation_mode="anchor",
    )


def _suptitle_parts(
    model: str,
    sweeps: list[str],
    run_counts: dict[str, int],
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> tuple[str, str]:
    """Return the main title and the runs-per-sweep detail line.

    The detail line is always emitted so the sample size stays visible: a single
    ``N runs per sweep`` when every sweep has the same count, otherwise an
    explicit ``runs per sweep: a=10, b=12`` list.  ``excluded_count`` is the
    number of error/collapsed runs dropped for this model and is appended (with
    ``excluded_noun``, pluralised) when non-zero.
    """
    title = f"Sweep metric distributions \u2014 {model or '-'}"
    counts = [run_counts.get(sweep, 0) for sweep in sweeps]
    if len(set(counts)) <= 1:
        n = counts[0] if counts else 0
        detail = f"{n} runs per sweep"
    else:
        listing = ", ".join(f"{sweep}={run_counts.get(sweep, 0)}" for sweep in sweeps)
        detail = f"runs per sweep: {listing}"
    if excluded_count:
        noun = excluded_noun if excluded_count == 1 else f"{excluded_noun}s"
        detail += f" ({excluded_count} excluded: {noun})"
    return title, detail


_DETAIL_FONT = 10


def _wrap_detail(detail: str, fig_width_in: float) -> list[str]:
    """Wrap the runs-per-sweep detail so it never overflows the figure width.

    Long sweep names (e.g. ``Coalition without private channel, 1 coordination
    round``) make the explicit ``a=10, b=12`` listing far too wide for one line,
    so it is wrapped at spaces instead of being clipped by the figure edge.
    """
    max_chars = max(40, int(fig_width_in * 72.0 / (0.6 * _DETAIL_FONT)))
    return textwrap.wrap(detail, width=max_chars) or [detail]


def render_model_figure(
    model: str,
    sweeps: list[str],
    values: dict[str, dict[str, list[float]]],
    specs: list[Any],
    style: str,
    annotate_mode: str,
    run_counts: dict[str, int],
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> Any:
    """Render the multi-panel figure for a single model."""
    n = len(specs)
    rows, cols = _layout(n)
    total = rows * cols
    fig, axes = plt.subplots(
        rows,
        cols,
        figsize=(5.33 * cols, 4.5 * rows),
        sharex=True,
        squeeze=False,
    )
    flat = list(axes.ravel())

    for index, spec in enumerate(specs):
        ax = flat[index]
        rng = random.Random(0)
        metric_values = values.get(spec.key, {})
        for position, sweep in enumerate(sweeps):
            sweep_values = metric_values.get(sweep, [])
            if style == "dot":
                _draw_dot(ax, position, sweep_values, spec, annotate_mode, rng)
            else:
                _draw_box(ax, position, sweep_values, spec, annotate_mode, rng)
        if annotate_mode == "mean":
            _add_headroom(ax)
        _style_panel(ax, spec)
        _set_sweep_axis(ax, sweeps)

    handles = _legend_handles(style)
    if total > n:
        # Legend lives in the first free cell; every other free cell is hidden.
        for index in range(n, total):
            flat[index].axis("off")
        legend_ax = flat[n]
        legend_ax.legend(handles=handles, loc="center", frameon=False, fontsize=11)
        # `sharex` hides x labels unless the axes is in the last grid row, so make
        # sure each column's lowest *data* panel shows its sweep labels.
        for col in range(cols):
            for row in range(rows - 1, -1, -1):
                index = row * cols + col
                if index < n:
                    flat[index].tick_params(axis="x", labelbottom=True)
                    break
    else:  # pragma: no cover - `_layout` always leaves a free cell
        fig.legend(
            handles=handles,
            loc="upper right",
            bbox_to_anchor=(0.995, 0.995),
            frameon=False,
            fontsize=11,
        )

    title, detail = _suptitle_parts(
        model, sweeps, run_counts, excluded_count, excluded_noun
    )
    fig.suptitle(title, fontsize=15, fontweight="bold", y=0.985)
    detail_lines = _wrap_detail(detail, fig.get_figwidth())
    fig.text(
        0.5,
        0.945,
        "\n".join(detail_lines),
        ha="center",
        va="top",
        fontsize=_DETAIL_FONT,
        color="#444444",
        linespacing=1.25,
    )
    top = 0.90 - 0.03 * (len(detail_lines) - 1)
    fig.tight_layout(rect=(0, 0, 1, top))
    return fig


def render_split_figure(
    model: str,
    sweeps: list[str],
    spec: Any,
    metric_values: dict[str, list[float]],
    style: str,
    annotate_mode: str,
) -> Any:
    """Render a single-metric figure (used by ``--split``)."""
    fig, ax = plt.subplots(1, 1, figsize=(7, 5))
    rng = random.Random(0)
    for position, sweep in enumerate(sweeps):
        sweep_values = metric_values.get(sweep, [])
        if style == "dot":
            _draw_dot(ax, position, sweep_values, spec, annotate_mode, rng)
        else:
            _draw_box(ax, position, sweep_values, spec, annotate_mode, rng)
    if annotate_mode == "mean":
        _add_headroom(ax)
    _style_panel(ax, spec, with_title=False)
    _set_sweep_axis(ax, sweeps)
    fig.legend(
        handles=_legend_handles(style),
        loc="center right",
        bbox_to_anchor=(1.0, 0.5),
        frameon=False,
        fontsize=9,
    )
    fig.suptitle(f"{spec.label} \u2014 {model or '-'}", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 0.82, 0.94))
    return fig


def save_figure(fig: Any, path: str, dpi: int) -> None:
    # ``bbox_inches="tight"`` keeps long rotated sweep labels inside the canvas
    # instead of clipping them at the figure edge.
    fig.savefig(path, dpi=dpi, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# File outputs
# --------------------------------------------------------------------------- #


def _num_str(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def write_stats_csv(path: str, records: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "model",
                "sweep",
                "metric",
                "label",
                "n",
                "min",
                "q1",
                "median",
                "mean",
                "std",
                "max",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record["model"],
                    record["sweep"],
                    record["metric"],
                    record["label"],
                    record["n"],
                    _num_str(record["min"]),
                    _num_str(record["q1"]),
                    _num_str(record["median"]),
                    _num_str(record["mean"]),
                    _num_str(record["std"]),
                    _num_str(record["max"]),
                ]
            )


def write_stats_json(
    path: str,
    records: list[dict[str, Any]],
    excluded_runs: list[dict[str, Any]],
) -> None:
    """Write the stats records plus the excluded-run metadata.

    The file is an object: ``{"stats": [...], "excluded_runs": [...]}``.  The
    ``excluded_runs`` objects match the ones ``sweep_metrics_table.py`` writes.
    """
    payload = {"stats": records, "excluded_runs": excluded_runs}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plot_sweep_metrics",
        description="Plot headline metric distributions per sweep (hybrid box + "
        "points + mean/std by default).",
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
    parser.add_argument("--out", dest="out", default=None, help="Output directory.")
    parser.add_argument(
        "--metrics", dest="metrics", default=None, help="Comma-separated metric subset."
    )
    parser.add_argument(
        "--style",
        dest="style",
        choices=("box", "dot"),
        default="box",
        help="Panel style (default box).",
    )
    parser.add_argument(
        "--annotate",
        dest="annotate",
        choices=("mean", "none"),
        default="mean",
        help="Per-box annotation (default mean).",
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
        "--split",
        dest="split",
        action="store_true",
        help="Also write one PNG per metric.",
    )
    parser.add_argument(
        "--order",
        dest="order",
        default=None,
        help="Explicit comma-separated sweep order.",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None, help="Write the plotted stats here."
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Write the stats (plus excluded-run metadata) as JSON.",
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
        os.path.dirname(os.path.abspath(__file__)), "plots_outputs", "sweep_metrics"
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

    if args.metrics:
        specs = list(resolve_metrics(args.metrics))
    else:
        specs = [METRICS_BY_KEY[key] for key in DEFAULT_METRIC_KEYS]
    if not specs:
        print("error: no valid metrics selected", file=sys.stderr)
        return 2

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

    # Summary wording mirrors sweep_metrics_table.py exactly.
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

    grouped = group_runs(kept_runs)
    models = sorted(grouped)
    order = [token.strip() for token in (args.order or "").split(",") if token.strip()]

    values_by_model: dict[str, dict[str, dict[str, list[float]]]] = {}
    run_counts_by_model: dict[str, dict[str, int]] = {}
    records: list[dict[str, Any]] = []
    for model in models:
        buckets = grouped[model]
        sweeps = order_sweeps(sorted(buckets), order)
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
                # Rate metrics are plotted as percent so the axis, the "%" label
                # and the annotations agree; the stats below keep the raw fraction.
                if spec.fmt == "rate":
                    per_metric[spec.key][sweep] = [v * 100.0 for v in values]
                else:
                    per_metric[spec.key][sweep] = values
                stats = compute_stats(values)
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

    out_dir = args.out or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    multi = len(models) > 1

    written: list[str] = []
    for model in models:
        sweeps = order_sweeps(sorted(grouped[model]), order)
        fig = render_model_figure(
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
        base = "sweep_metrics" + (f"_{slugify(model)}" if multi else "")
        png_path = os.path.join(out_dir, base + ".png")
        pdf_path = os.path.join(out_dir, base + ".pdf")
        save_figure(fig, png_path, args.dpi)
        save_figure(fig, pdf_path, args.dpi)
        written.extend([png_path, pdf_path])

        if args.split:
            for spec in specs:
                split_fig = render_split_figure(
                    model,
                    sweeps,
                    spec,
                    values_by_model[model][spec.key],
                    args.style,
                    args.annotate,
                )
                split_path = os.path.join(out_dir, base + f"_{spec.key}.png")
                save_figure(split_fig, split_path, args.dpi)
                written.append(split_path)

    csv_path = args.csv_path or os.path.join(out_dir, "sweep_metrics_stats.csv")
    write_stats_csv(csv_path, records)
    written.append(csv_path)

    if args.json_path:
        write_stats_json(args.json_path, records, excluded_records)
        written.append(args.json_path)

    print("Wrote:")
    for path in written:
        print(f"  {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
