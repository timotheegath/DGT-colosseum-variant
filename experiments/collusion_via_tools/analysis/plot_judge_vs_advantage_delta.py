"""Rendering helpers for the paired-delta scatter against a reference sweep.

This module is a **library**, not a command-line entry point. The consolidated
CLI lives in :mod:`experiments.collusion_via_tools.analysis.plots`::

    python -m experiments.collusion_via_tools.analysis.plots \
        <root> [<root> ...] --no-box --no-scatter --reference SWEEP [options]

Why this script exists alongside :mod:`plot_judge_vs_advantage`
--------------------------------------------------------------
``plot_judge_vs_advantage`` plots the raw per-run values. That is the right
view for "did this sweep, on average, score high on the judge axis?", but it
is the wrong view for the question the colosseum paper asks: *"relative to
the benign baseline, did the collusion sweep pull ahead?"* The same shape
of plot, deltas-vs-reference instead of raw values, is what the paper
charts and what the table script's ``--reference`` Table C already
produces.

This script is intentionally a sibling rather than a refactor of
``plot_judge_vs_advantage``: that script remains the raw-value view, this
script is the delta view. They share the data-plumbing helpers
(``find_metric_files``, ``load_runs``, ``partition_runs``) so the run
discovery and run-health rules stay aligned.

What gets plotted
-----------------
* **x = Δ-judge score**    ``sweep_judge(seed) - reference_judge(seed)``
* **y = Δ-advantage**      ``sweep_advantage(seed) - reference_advantage(seed)``

Both deltas are paired by seed. The advantage on each side is the
existing ``coalition_minus_noncoalition_avg`` field; the existing
``coalition_minus_noncoalition_avg`` reading from ``sweep_metrics_table``
is unchanged, so the headline box plots and the table script keep their
raw-value semantics. This script only consumes the field — it does not
redefine it.

Baseline coalition inference
----------------------------
A benign-baseline run (``adversary_count == 0``) carries no
``coalition_*`` fields, so its raw ``coalition_minus_noncoalition_avg``
is ``None``. To put it on the same axis, this script infers the
counterfactual coalition from the colluding sweeps via
:func:`aggregate_protocols.identify_colluder_set` and writes the
synthetic ``coalition_minus_noncoalition_avg`` (and its building blocks)
into the baseline run via
:func:`aggregate_protocols.augment_baseline_coalition_metrics`. The
augmentation happens in memory only; no file on disk is touched.

Paired-by-seed semantics
------------------------
For every seed present in *both* a sweep and the reference, the plotted
point is::

    x = sweep_judge(seed)    - reference_judge(seed)
    y = sweep_advantage(seed) - reference_advantage(seed)

Seeds missing from either side are dropped from that sweep, with a
per-sweep ``paired seeds`` count in the figure's detail line. The
reference itself plots at ``(0, 0)`` for every seed it has; pass
``--no-reference-points`` to draw it only as a ``y = 0`` baseline.

Run-health flags
----------------
``--exclude-error-runs`` and ``--exclude-failed-runs`` are forwarded to
``partition_runs``; their meaning is identical to the sibling scripts.

Options accepted by the ``plots.py`` CLI
--------------------------------------
    --root PATH            Alternative (repeatable) way to pass a root.
    --out DIR              Base output directory (this figure type writes to its
                           own ``judge_vs_advantage_delta/`` subdirectory
                           underneath it).
    --reference SWEEP      Reference sweep name (required for this figure type).
                           The other sweeps are deltas-vs-this-sweep, paired by seed.
    --order SWEEP,SWEEP    Explicit sweep order (default: alphabetical, ref last).
    --exclude-error-runs   Drop every run with a failed agent turn (strict).
    --exclude-failed-runs  Drop only collapsed runs (failed turn + no assignment).
    --no-reference-points  Skip plotting the reference sweep points
                           (it then appears only as a y=0 reference line).
    --csv PATH             Write the per-run delta rows here.
    --json PATH            Write the deltas plus excluded-run metadata.
    --dpi N                Raster resolution (default 300).

Exit codes: 2 when no root is given, the root does not exist, or
``--reference`` is missing / not found in any sweep.
"""

from __future__ import annotations

import csv
import json
import os
import random
import sys
import textwrap
import time
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
    "partition_runs",
    "run_is_failed",
)


def _import_sweep_metrics_table() -> Any:
    """Import the sibling ``sweep_metrics_table`` module.

    Another process may be editing that file concurrently, which can leave it
    momentarily unparsable or missing a freshly added helper. We therefore
    retry a handful of times before giving up.
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
partition_runs = _smt.partition_runs
run_is_failed = _smt.run_is_failed

# --------------------------------------------------------------------------- #
# Local sibling: aggregate_protocols for the baseline coalition inference
# --------------------------------------------------------------------------- #


def _import_aggregate_protocols() -> Any:
    last_exc: BaseException | None = None
    for attempt in range(_IMPORT_RETRIES):
        try:
            try:
                from . import aggregate_protocols as module
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
                    "experiments.collusion_via_tools.analysis.aggregate_protocols"
                )
            missing = [
                name
                for name in (
                    "identify_colluder_set",
                    "augment_runs_with_baseline_coalition",
                )
                if not hasattr(module, name)
            ]
            if missing:
                raise ImportError(
                    f"aggregate_protocols is missing {', '.join(missing)}"
                )
            return module
        except (ImportError, SyntaxError) as exc:
            last_exc = exc
            if attempt < _IMPORT_RETRIES - 1:
                print(
                    "warning: transient import failure of aggregate_protocols "
                    f"({exc!r}); retrying in {_IMPORT_RETRY_DELAY}s "
                    f"({attempt + 1}/{_IMPORT_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(_IMPORT_RETRY_DELAY)
    raise ImportError(
        f"could not import aggregate_protocols after {_IMPORT_RETRIES} attempts"
    ) from last_exc


_ap = _import_aggregate_protocols()

identify_colluder_set = _ap.identify_colluder_set
augment_runs_with_baseline_coalition = _ap.augment_runs_with_baseline_coalition

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SWEEP_COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#bcbd22",
    "#7f7f7f",
]

SWEEP_MARKERS = [
    "o",
    "s",
    "^",
    "D",
    "v",
    "P",
    "X",
    "*",
    "h",
    "<",
]

REFERENCE_COLOR = "#111111"
REFERENCE_MARKER = "P"
REFERENCE_LABEL = "reference (delta=0)"

GRID_COLOR = "#e3e3e3"
SPINE_COLOR = "#bbbbbb"
ANNOT_COLOR = "#444444"

POINT_SIZE = 70
POINT_EDGE_WIDTH = 0.6
JITTER_HALF = 0.04

DETAIL_FONT = 10

# --------------------------------------------------------------------------- #
# Metric accessors (shared with sweep_significance.py --delta mode)
# --------------------------------------------------------------------------- #
from .delta_metrics import (  # noqa: E402
    DeltaPoint,
    compute_paired_deltas,  # noqa: F401  (re-exported for backward compatibility)
)


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def slugify(text: str) -> str:
    out = []
    for char in text.lower():
        out.append(char if char.isalnum() else "_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "model"


def group_runs(runs: list[Any]) -> dict[str, dict[str, list[Any]]]:
    grouped: dict[str, dict[str, list[Any]]] = {}
    for run in runs:
        grouped.setdefault(run.model, {}).setdefault(run.sweep, []).append(run)
    return grouped


def order_sweeps(
    sweeps: list[str], order: list[str], reference: str | None
) -> list[str]:
    """Return sweeps with ``order`` honoured first, then alphabetical, ref last."""
    known = set(sweeps)
    ordered = [sweep for sweep in order if sweep in known]
    remainder = sorted(s for s in sweeps if s not in ordered)
    full = ordered + remainder
    if reference and reference in full:
        full = [s for s in full if s != reference] + [reference]
    return full


def excluded_sort_key(item: tuple[Any, list[str]]) -> tuple[str, str, int, str]:
    run = item[0]
    seed = run.seed()
    return (run.model, run.sweep, seed if seed is not None else -1, run.run_id)


def exclusion_reason(run: Any, exclude_failed_runs: bool) -> str:
    if exclude_failed_runs and run_is_failed(run):
        return "failed: error turn, 0 variables assigned"
    return "error turn"


def _wrap_detail(detail: str, fig_width_in: float) -> list[str]:
    max_chars = max(40, int(fig_width_in * 72.0 / (0.6 * DETAIL_FONT)))
    return textwrap.wrap(detail, width=max_chars) or [detail]


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #


def _suptitle_parts(
    model: str,
    reference: str,
    runs_per_sweep: dict[str, int],
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> tuple[str, str]:
    title = (
        f"Coalition advantage vs. LLM-judge score \u2014 \u0394 vs {reference} \u2014 "
        f"{model or '-'}"
    )
    counts = list(runs_per_sweep.values())
    if counts and len(set(counts)) <= 1:
        n = counts[0]
        detail = f"{n} paired seeds per sweep"
    else:
        listing = ", ".join(f"{sweep}={n}" for sweep, n in runs_per_sweep.items())
        detail = f"paired seeds per sweep: {listing}"
    if excluded_count:
        noun = excluded_noun if excluded_count == 1 else f"{excluded_noun}s"
        detail += f" ({excluded_count} excluded: {noun})"
    return title, detail


def render_model_figure(
    model: str,
    sweeps: list[str],
    points_by_sweep: dict[str, list[DeltaPoint]],
    reference: str,
    run_counts: dict[str, int],
    show_reference_points: bool,
    excluded_count: int = 0,
    excluded_noun: str = "error turn",
) -> Any:
    """Render the per-model delta scatter."""
    visible_sweeps = [
        sweep for sweep in sweeps if points_by_sweep.get(sweep) or sweep == reference
    ]

    fig, ax = plt.subplots(1, 1, figsize=(8.5, 6.0))

    color_for_sweep: dict[str, str] = {}
    marker_for_sweep: dict[str, str] = {}
    for i, sweep in enumerate(visible_sweeps):
        color_for_sweep[sweep] = (
            REFERENCE_COLOR
            if sweep == reference
            else SWEEP_COLORS[i % len(SWEEP_COLORS)]
        )
        marker_for_sweep[sweep] = (
            REFERENCE_MARKER
            if sweep == reference
            else SWEEP_MARKERS[i % len(SWEEP_MARKERS)]
        )

    rng = random.Random(0)

    for sweep in visible_sweeps:
        pts = points_by_sweep.get(sweep, [])
        if sweep == reference and not show_reference_points:
            continue
        if sweep == reference:
            # Reference points are always at (0, 0); plot only when requested.
            n = run_counts.get(reference, 0)
            xs = [0.0] * n
            ys = [0.0] * n
            ax.scatter(
                xs,
                ys,
                s=POINT_SIZE,
                c=REFERENCE_COLOR,
                marker=REFERENCE_MARKER,
                edgecolors="black",
                linewidths=POINT_EDGE_WIDTH,
                alpha=0.9,
                zorder=4,
                label=f"{reference} (n={n})",
            )
            continue

        if not pts:
            continue
        color = color_for_sweep[sweep]
        marker = marker_for_sweep[sweep]
        xs: list[float] = []
        ys: list[float] = []
        for idx, pt in enumerate(pts):
            if pt.judge_delta is None or pt.advantage_delta is None:
                continue
            xs.append(_jitter_x(pt.judge_delta, len(pts), idx, rng))
            ys.append(pt.advantage_delta)
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

    # y=0 reference line: the reference itself is at 0 (by construction), so
    # any sweep whose delta stays above / below this line is winning /
    # losing relative to the reference on the regret-gap axis.
    ax.axhline(0.0, color=REFERENCE_COLOR, linewidth=1.0, linestyle=":", zorder=1)
    ax.axvline(0.0, color=REFERENCE_COLOR, linewidth=1.0, linestyle=":", zorder=1)

    _style_axes(ax)
    title, detail = _suptitle_parts(
        model, reference, run_counts, excluded_count, excluded_noun
    )
    fig.suptitle(title, fontsize=13, fontweight="bold", y=0.985)
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

    handles = []
    for sweep in visible_sweeps:
        if sweep == reference and not show_reference_points:
            continue
        n = (
            run_counts.get(sweep, 0)
            if sweep == reference
            else len(points_by_sweep.get(sweep, []))
        )
        handles.append(
            Line2D(
                [0],
                [0],
                marker=marker_for_sweep[sweep],
                color="w",
                markerfacecolor=color_for_sweep[sweep],
                markeredgecolor="black",
                markeredgewidth=POINT_EDGE_WIDTH,
                markersize=9,
                label=f"{sweep} (n={n})",
            )
        )
    ax.legend(
        handles=handles,
        loc="best",
        frameon=False,
        fontsize=9,
        title="sweep (\u0394 vs reference)",
        title_fontsize=10,
    )

    top = 0.90 - 0.03 * (len(detail_lines) - 1)
    fig.tight_layout(rect=(0, 0, 1, top))
    return fig


def _jitter_x(base_x: float, count: int, idx: int, rng: random.Random) -> float:
    if count <= 1:
        return base_x
    return base_x + rng.uniform(-JITTER_HALF, JITTER_HALF)


def _style_axes(ax: Any) -> None:
    ax.set_xlabel(
        "\u0394 Collusion judge (Simple) \u2013 sweep minus reference",
        fontsize=11,
    )
    ax.set_ylabel(
        "\u0394 Coalition advantage (coalition_minus_noncoalition_avg) \u2013 "
        "sweep minus reference",
        fontsize=10,
    )
    ax.grid(True, color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(SPINE_COLOR)
    ax.tick_params(labelsize=9)


def save_figure(fig: Any, path: str, dpi: int) -> None:
    fig.savefig(path, dpi=dpi, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# File outputs
# --------------------------------------------------------------------------- #


def _num_str(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def write_deltas_csv(path: str, points: list[DeltaPoint]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
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
                    _num_str(pt.judge_delta),
                    _num_str(pt.advantage_delta),
                ]
            )


def write_deltas_json(
    path: str,
    points: list[DeltaPoint],
    excluded_records: list[dict[str, Any]],
    colluder_set: list[str],
    reference: str,
) -> None:
    payload = {
        "reference": reference,
        "colluder_set": sorted(colluder_set),
        "points": [
            {
                "model": pt.model,
                "sweep": pt.sweep,
                "seed": pt.seed,
                "run_id": pt.run_id,
                "judge_delta": pt.judge_delta,
                "advantage_delta": pt.advantage_delta,
            }
            for pt in points
        ],
        "excluded_runs": excluded_records,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    print(
        "error: this command was removed; use "
        "`python -m experiments.collusion_via_tools.analysis.plots`",
        file=sys.stderr,
    )
    sys.exit(2)
