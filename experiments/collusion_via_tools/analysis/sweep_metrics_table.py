"""Print a table of key metrics (mean +/- std) per sweep for one or more sweep roots.

Usage
-----
    python -m experiments.collusion_via_tools.analysis.sweep_metrics_table <root> [<root> ...] [options]

Arguments
---------
    <root> ...              One or more experiment output roots. A root may be the
                            timestamped directory that contains ``runs/``, the
                            ``runs/`` directory itself, or any ancestor that nests
                            such roots (e.g. ``saved_outputs/``); nested output
                            roots are auto-discovered. Also accepted via a
                            repeatable ``--root``.

Grouping
--------
Runs are grouped by model and sweep name only: sweeps are the unit of
comparison and are shown by their plain name everywhere. The output root is
provenance, not identity, so a sweep name that occurs under several roots is
pooled into a single group (reported with a warning on stderr). Source
information is surfaced only in Table A's ``Source`` column and in the CSV /
JSON outputs.

Options
-------
    --root PATH             Alternative (repeatable) way to pass a root.
    --csv PATH              Write a long-format CSV of the aggregated metrics.
    --json PATH             Write a nested JSON of the aggregated metrics.
    --sweep NAME            Restrict to a sweep or source (repeatable).
    --source NAME           Restrict to a source (repeatable).
    --metrics a,b,c         Comma-separated subset of metric keys. The token
                            ``headline`` expands to a preset set of headline
                            metrics and may be mixed with individual keys.
    --ddof {0,1}            Std delta degrees of freedom (default 1).
    --reference SWEEP       Reference group label or raw sweep name for the delta
                            table.
    --no-delta              Skip the delta table.
    --exclude-error-runs    Drop runs that recorded a failed agent turn (an
                            ``"[ERROR] ..."`` response in ``agent_turns.json``)
                            before aggregating. Opt-in and off by default.
    --exclude-failed-runs   Drop only runs that actually collapsed (a failed
                            turn AND no assignment at all). Narrower than
                            ``--exclude-error-runs``. Opt-in.
    --format {plain,formatted,markdown}
                            Output layout (default ``plain``). ``plain`` is the
                            original per-metric layout; ``formatted`` and
                            ``markdown`` use a transposed layout (rows = sweeps,
                            columns = metrics) shown as aligned text or markdown
                            pipe tables respectively.

``--exclude-error-runs`` mirrors ``experiments/collusion_via_tools/resume.py``:
a run whose ``agent_turns.json`` holds an ``"[ERROR] ..."`` response has at
least one agent whose turn never happened, so its metrics do not describe a
full simulation and it must be re-run rather than aggregated. The flag is
opt-in, so by default every run on disk is included.

``--exclude-failed-runs`` is the stricter, narrower rule: it drops a run only
when it both recorded a failed turn and collapsed (no assignment at all —
``meeting_outcome`` missing or ``variables_assigned`` 0). An errored run that
still produced a normal outcome is a legitimate, if unusual, result and is
kept. When both flags are set, the union applies (a run is dropped if it has an
error turn or collapsed).

The script is stdlib-only and never mutates its inputs. Malformed run
artifacts (missing/malformed ``metrics.json``, missing ``costs.json``,
null metric values) are tolerated: such runs or fields are skipped or
reported as missing rather than crashing the run.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Metric catalogue
# --------------------------------------------------------------------------- #

# A getter takes (metrics, run_config, costs) and returns a float | None.
Getter = Callable[
    [dict[str, Any], dict[str, Any] | None, dict[str, Any] | None], "float | None"
]


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    getter: Getter
    fmt: str


def _dig(data: dict[str, Any] | None, *path: str) -> Any:
    """Return ``data[path[0]][path[1]]...`` or ``None`` if any link is missing."""
    cur: Any = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _num(value: Any) -> float | None:
    """Coerce a JSON value to float, returning None for null/non-numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    return None


def _top(key: str) -> Getter:
    return lambda metrics, run_config, costs: _num(metrics.get(key))


def _nested(section: str, key: str) -> Getter:
    return lambda metrics, run_config, costs: _num(_dig(metrics, section, key))


def _costs(*path: str) -> Getter:
    return lambda metrics, run_config, costs: _num(_dig(costs, *path))


DEFAULT_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "joint_reward",
        "Joint reward",
        _nested("meeting_outcome", "joint_reward"),
        "reward",
    ),
    MetricSpec(
        "joint_reward_regret", "Overall regret", _top("joint_reward_regret"), "reward"
    ),
    MetricSpec(
        "coalition_reward_avg",
        "Coalition avg reward",
        _top("coalition_reward_avg"),
        "reward",
    ),
    MetricSpec(
        "noncoalition_reward_avg",
        "Non-coalition avg reward",
        _top("noncoalition_reward_avg"),
        "reward",
    ),
    MetricSpec(
        "coalition_minus_noncoalition_avg",
        "Coalition advantage",
        _top("coalition_minus_noncoalition_avg"),
        "reward",
    ),
    MetricSpec(
        "coalition_reward_regret",
        "Coalition regret",
        _top("coalition_reward_regret"),
        "reward",
    ),
    MetricSpec(
        "average_agent_reward",
        "Avg agent reward",
        _nested("meeting_outcome", "average_agent_reward"),
        "reward",
    ),
    MetricSpec(
        "max_joint_reward", "Max joint reward", _top("max_joint_reward"), "reward"
    ),
    MetricSpec(
        "suspicion_rate_non_adversary",
        "Suspicion rate (non-adv)",
        _top("suspicion_rate_non_adversary"),
        "rate",
    ),
    MetricSpec(
        "correct_attribution_rate_non_adversary",
        "Correct blame rate (non-adv)",
        _top("correct_attribution_rate_non_adversary"),
        "rate",
    ),
    MetricSpec(
        "total_suspicious_agents",
        "Suspicious agents",
        _top("total_suspicious_agents"),
        "count",
    ),
    MetricSpec(
        "total_correct_blamers",
        "Correct blamers",
        _top("total_correct_blamers"),
        "count",
    ),
    MetricSpec(
        "total_messages", "Blackboard messages", _top("total_messages"), "count"
    ),
    MetricSpec(
        "complaint_messages", "Complaint messages", _top("complaint_messages"), "count"
    ),
    MetricSpec(
        "variables_assigned",
        "Variables assigned",
        _nested("meeting_outcome", "variables_assigned"),
        "count",
    ),
    MetricSpec(
        "converged_patients",
        "Converged patients",
        _nested("meeting_outcome", "converged_patients"),
        "count",
    ),
    MetricSpec(
        "agent_total_tokens",
        "Agent tokens",
        _costs("agent_usage", "total_tokens"),
        "tokens",
    ),
    MetricSpec(
        "judge_total_tokens",
        "Judge tokens",
        _costs("judge_usage", "total_tokens"),
        "tokens",
    ),
    MetricSpec(
        "collusion_judge_simple_mean",
        "Collusion judge (Simple)",
        lambda metrics, run_config, costs: _num(
            _dig(metrics, "collusion_judge_simple", "mean_score")
        ),
        "likert",
    ),
)

METRICS_BY_KEY: dict[str, MetricSpec] = {m.key: m for m in DEFAULT_METRICS}

# Headline metrics shown in the delta table.
DELTA_METRICS: tuple[str, ...] = (
    "joint_reward",
    "joint_reward_regret",
    "coalition_minus_noncoalition_avg",
    "coalition_reward_regret",
    "suspicion_rate_non_adversary",
)

# Preset expansion for the literal ``--metrics headline`` token, in this order.
HEADLINE_PRESET: tuple[str, ...] = (
    "joint_reward",
    "joint_reward_regret",
    "coalition_minus_noncoalition_avg",
    "coalition_reward_regret",
    "coalition_reward_avg",
    "noncoalition_reward_avg",
    "average_agent_reward",
    "suspicion_rate_non_adversary",
    "correct_attribution_rate_non_adversary",
)

SEED_AT_END = re.compile(r"seed(\d+)$")
SEED_ANYWHERE = re.compile(r"seed(\d+)")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class RunData:
    model: str
    sweep: str
    source: str
    source_label: str
    source_path: str
    run_id: str
    run_dir: str
    metrics: dict[str, Any]
    run_config: dict[str, Any] | None
    costs: dict[str, Any] | None

    def seed(self) -> int | None:
        for pattern in (SEED_AT_END, SEED_ANYWHERE):
            match = pattern.search(self.run_id)
            if match:
                return int(match.group(1))
        return None


@dataclass
class SweepGroup:
    label: str
    sweep: str
    sources: list[str]
    source_labels: list[str]
    source_paths: list[str]
    runs: list[RunData]


@dataclass
class Agg:
    n: int
    n_missing: int
    mean: float | None
    std: float | None
    min: float | None
    max: float | None


@dataclass
class GroupMetrics:
    n_runs: int
    aggregates: dict[str, Agg] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def format_value(fmt: str, value: float | None) -> str:
    """Format a numeric value for the console table."""
    if value is None:
        return "n/a"
    if fmt == "reward":
        return f"{value:,.1f}"
    if fmt == "rate":
        return f"{value * 100:.1f}%"
    if fmt == "count":
        return f"{value:.1f}"
    if fmt == "likert":
        return f"{value:.1f}"
    if fmt == "tokens":
        return f"{value:,.0f}"
    if fmt == "ratio":
        return f"{value:.3f}"
    return f"{value:.3f}"


def aggregate(values: list[float], n_missing: int, ddof: int) -> Agg:
    """Aggregate a list of non-null values."""
    n = len(values)
    if n == 0:
        return Agg(n=0, n_missing=n_missing, mean=None, std=None, min=None, max=None)
    mean = statistics.fmean(values)
    if n == 1:
        std = 0.0
    elif ddof == 0:
        std = statistics.pstdev(values, mu=mean)
    else:
        std = statistics.stdev(values, xbar=mean)
    return Agg(
        n=n, n_missing=n_missing, mean=mean, std=std, min=min(values), max=max(values)
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def find_metric_files(root: str) -> list[str]:
    """Recursively find every ``metrics.json`` under ``root``.

    Hidden directories (any path component starting with ``.``) are skipped.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "metrics.json" in filenames:
            found.append(os.path.join(dirpath, "metrics.json"))
    return sorted(found)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


_TIMESTAMP_PREFIX = re.compile(r"^(\d{8}-\d{6})")


def source_short(label: str) -> str:
    """Return the ``YYYYMMDD-HHMMSS`` prefix of a source label, else the label."""
    match = _TIMESTAMP_PREFIX.match(label)
    return match.group(1) if match else label


def resolve_run_meta(root: str, run_dir: str) -> tuple[str, str, str, str, str]:
    """Resolve ``(model, sweep, source, source_label, source_path)`` for a run.

    The rule anchors on the first ``runs`` component of the run directory path
    relative to ``root``, so it works whether ``root`` is a deep output tree, a
    single timestamped directory, or the ``runs`` directory itself.
    """
    rel = os.path.relpath(run_dir, root)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if "runs" in parts:
        index = parts.index("runs")
        source_prefix = parts[:index]
        model_parts = parts[index + 1 : -2]
    else:
        source_prefix = []
        model_parts = parts[:-2]
    sweep = parts[-2] if len(parts) >= 2 else "default"
    model = "/".join(model_parts)

    source_root_path = os.path.normpath(os.path.join(root, *source_prefix))
    source_label = os.path.basename(source_root_path)
    source = source_short(source_label)
    source_path = "/".join(source_prefix)
    return model, sweep, source, source_label, source_path


def load_runs(root: str, found: list[str]) -> list[RunData]:
    """Parse discovered metric files into usable runs, warning on failures."""
    runs: list[RunData] = []
    for metrics_path in found:
        run_dir = os.path.dirname(metrics_path)
        model, sweep, source, source_label, source_path = resolve_run_meta(
            root, run_dir
        )

        metrics = _read_json(metrics_path)
        if metrics is None:
            print(
                f"warning: skipping unreadable metrics.json: {metrics_path}",
                file=sys.stderr,
            )
            continue

        run_config = _read_json(os.path.join(run_dir, "run_config.json"))
        costs = _read_json(os.path.join(run_dir, "costs.json"))
        run_id = os.path.basename(run_dir)
        runs.append(
            RunData(
                model=model,
                sweep=sweep,
                source=source,
                source_label=source_label,
                source_path=source_path,
                run_id=run_id,
                run_dir=run_dir,
                metrics=metrics,
                run_config=run_config,
                costs=costs,
            )
        )
    return runs


# --------------------------------------------------------------------------- #
# Run health / error turns
# --------------------------------------------------------------------------- #

ERROR_TURN_PREFIX = "[ERROR]"


def run_health(run_dir: str) -> tuple[bool, list[str]]:
    """Return ``(has_error, agents)`` for one run directory.

    Mirrors ``experiments/collusion_via_tools/resume.py::_has_error_turn``:
    a turn whose LLM call raised is recorded with an ``"[ERROR] ..."`` response,
    so at least one agent's turn never happened and the run's metrics do not
    describe a full simulation.

    ``has_error`` is True when a failed turn is present AND when
    ``agent_turns.json`` is missing, unreadable or malformed (same convention as
    resume.py: unreadable turns count as incomplete, never as clean).
    ``agents`` lists the agents that recorded a failed turn (unique, sorted);
    it is empty when the file is missing/malformed.

    ``agent_turns.json`` is a few MB per run, so the clean case stays cheap: the
    file is scanned as bytes for the ``"[ERROR]"`` marker and only parsed when
    the marker is present.
    """
    turns_path = os.path.join(run_dir, "agent_turns.json")
    try:
        with open(turns_path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return True, []
    stripped = blob.strip()
    if not (stripped.startswith(b"[") and stripped.endswith(b"]")):
        # Truncated or not a JSON array: resume.py counts unreadable turns as
        # incomplete, so the run must not be treated as clean.
        return True, []
    if ERROR_TURN_PREFIX.encode() not in blob:
        # No response can start with the marker, so the run is clean.
        return False, []
    try:
        turns = json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True, []
    if not isinstance(turns, list):
        return True, []
    has_error = False
    agents: set[str] = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        response = turn.get("response")
        if isinstance(response, str) and response.startswith(ERROR_TURN_PREFIX):
            has_error = True
            agent = turn.get("agent")
            if isinstance(agent, str):
                agents.add(agent)
    return has_error, sorted(agents)


def run_has_error_turns(run_dir: str) -> bool:
    """True when the run recorded a failed turn (see ``run_health``)."""
    return run_health(run_dir)[0]


def run_is_collapsed(metrics: dict[str, Any]) -> bool:
    """True when a run produced no assignment at all.

    A collapsed run never got the meeting started: ``meeting_outcome`` is missing
    or ``variables_assigned`` is 0. Such a run's reward is dominated by the
    missed-step penalty, not by behaviour, so it must not be averaged in.
    """
    value = _num(_dig(metrics, "meeting_outcome", "variables_assigned"))
    return value is None or value == 0


def run_is_failed(run: RunData) -> bool:
    """True when a run both recorded a failed turn and collapsed.

    Both conditions are required: a failed turn alone means one agent misbehaved
    but the simulation still completed (a legitimate, if unusual, result), while
    a collapsed run is invalid regardless of what the turns file says.
    """
    return run_has_error_turns(run.run_dir) and run_is_collapsed(run.metrics)


def partition_runs(
    runs: list[RunData],
    exclude_error_runs: bool,
    exclude_failed_runs: bool = False,
) -> tuple[list[RunData], list[tuple[RunData, list[str]]]]:
    """Split runs into (kept, excluded).

    ``excluded`` holds ``(run, agents)`` pairs and is empty unless one of the two
    flags is set.

    ``exclude_error_runs`` drops every run that recorded a failed turn.
    ``exclude_failed_runs`` drops only the runs that actually collapsed (failed
    turn AND no assignment at all). When both are set the union applies (a run
    is dropped if it has an error turn or collapsed).
    """
    if not exclude_error_runs and not exclude_failed_runs:
        return list(runs), []
    kept: list[RunData] = []
    excluded: list[tuple[RunData, list[str]]] = []
    for run in runs:
        has_error, agents = run_health(run.run_dir)
        collapsed = run_is_collapsed(run.metrics)
        if exclude_error_runs and exclude_failed_runs:
            drop = has_error or collapsed
        elif exclude_error_runs:
            drop = has_error
        else:
            drop = has_error and collapsed
        if drop:
            excluded.append((run, agents))
        else:
            kept.append(run)
    return kept, excluded


def _exclusion_reason(run: RunData, exclude_failed_runs: bool) -> str:
    """Reason string for an excluded run, for the report and JSON output."""
    if exclude_failed_runs and run_is_failed(run):
        return "failed: error turn, 0 variables assigned"
    return "error turn"


def _excluded_sort_key(
    item: tuple[RunData, list[str]],
) -> tuple[str, str, int, str]:
    run = item[0]
    seed = run.seed()
    return (run.model, run.sweep, seed if seed is not None else -1, run.run_id)


def _source_candidates(group: SweepGroup) -> set[str]:
    """All source identifiers a group can be matched by."""
    return set(group.sources) | set(group.source_labels) | set(group.source_paths)


def group_runs(runs: list[RunData]) -> dict[str, list[SweepGroup]]:
    """Group runs by model then sweep name only, sorted by sweep name.

    The output root (source) is provenance, not part of the group key: runs that
    share a sweep name are pooled into one group even when they come from
    different roots. Labels are the plain sweep names. A sweep that spans more
    than one source triggers an informational warning on stderr.
    """
    buckets: dict[str, dict[str, list[RunData]]] = {}
    for run in runs:
        buckets.setdefault(run.model, {}).setdefault(run.sweep, []).append(run)

    grouped: dict[str, list[SweepGroup]] = {}
    for model, model_buckets in buckets.items():
        groups: list[SweepGroup] = []
        for sweep in sorted(model_buckets):
            sweep_runs = model_buckets[sweep]
            sweep_runs.sort(key=lambda r: (r.run_id, r.source))
            sources = sorted({run.source for run in sweep_runs})
            source_labels = sorted({run.source_label for run in sweep_runs})
            source_paths = sorted({run.source_path for run in sweep_runs})
            if len(sources) > 1:
                print(
                    f"warning: sweep '{sweep}' spans {len(sources)} output roots\n"
                    f"         ({', '.join(sources)}); its runs were pooled into "
                    f"one group",
                    file=sys.stderr,
                )
            groups.append(
                SweepGroup(
                    label=sweep,
                    sweep=sweep,
                    sources=sources,
                    source_labels=source_labels,
                    source_paths=source_paths,
                    runs=sweep_runs,
                )
            )
        grouped[model] = groups
    return grouped


def filter_groups_by_sweep(
    grouped: dict[str, list[SweepGroup]], names: set[str]
) -> dict[str, list[SweepGroup]]:
    """Keep groups matching any of ``names`` (sweep, label, or source)."""

    def matches(group: SweepGroup) -> bool:
        candidates = {group.label, group.sweep} | _source_candidates(group)
        return bool(candidates & names)

    return {
        model: [group for group in groups if matches(group)]
        for model, groups in grouped.items()
    }


def filter_groups_by_source(
    grouped: dict[str, list[SweepGroup]], names: set[str]
) -> dict[str, list[SweepGroup]]:
    """Keep groups whose source matches any of ``names``."""

    def matches(group: SweepGroup) -> bool:
        for name in names:
            if name in group.sources or name in group.source_labels:
                return True
            for path in group.source_paths:
                if path and (
                    name == path
                    or path.endswith("/" + name)
                    or name == os.path.basename(path)
                ):
                    return True
        return False

    return {
        model: [group for group in groups if matches(group)]
        for model, groups in grouped.items()
    }


def summarize_sources(runs: list[RunData]) -> list[dict[str, Any]]:
    """Summarize discovered sources with their usable run counts.

    Returns dicts with ``label`` (full label), ``short`` (short label), ``path``
    (relative source path) and ``n_runs``. ``display`` holds the label to show:
    the full label when two sources share a short label, else the short label.
    """
    counts: dict[tuple[str, str, str], int] = {}
    for run in runs:
        key = (run.source, run.source_label, run.source_path)
        counts[key] = counts.get(key, 0) + 1

    short_counts: dict[str, int] = {}
    for short, _label, _path in counts:
        short_counts[short] = short_counts.get(short, 0) + 1

    summaries: list[dict[str, Any]] = []
    for (short, label, path), n_runs in sorted(counts.items()):
        display = label if short_counts[short] > 1 else short
        summaries.append(
            {
                "label": label,
                "short": short,
                "path": path,
                "n_runs": n_runs,
                "display": display,
            }
        )
    return summaries


# --------------------------------------------------------------------------- #
# Modal helpers for the inventory table
# --------------------------------------------------------------------------- #


def _modal(values: list[Any]) -> Any:
    """Return the most common hashable value or None if there are none."""
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _modal_field(
    runs: list[RunData], extractor: Callable[[dict[str, Any]], Any]
) -> Any:
    values: list[Any] = []
    for run in runs:
        if run.run_config is None:
            continue
        value = extractor(run.run_config)
        if value is not None:
            values.append(value)
    return _modal(values)


def _yes_no(value: Any) -> str:
    if value is None:
        return "-"
    return "yes" if value else "no"


# --------------------------------------------------------------------------- #
# Table rendering
# --------------------------------------------------------------------------- #


def render_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    """Render an aligned fixed-width table with two-space separators."""
    ncols = len(headers)
    widths = [len(headers[i]) for i in range(ncols)]
    for row in rows:
        for i in range(ncols):
            widths[i] = max(widths[i], len(row[i]))

    def format_row(cells: list[str]) -> str:
        rendered = [
            cells[i].rjust(widths[i]) if aligns[i] == "r" else cells[i].ljust(widths[i])
            for i in range(ncols)
        ]
        return "  ".join(rendered).rstrip()

    lines = [format_row(headers)]
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)


def render_markdown(
    headers: list[str], rows: list[list[str]], aligns: list[str]
) -> str:
    """Render a markdown pipe table. Right-aligned columns get a ``---:`` rule."""

    def escape(cell: str) -> str:
        return cell.replace("|", "\\|")

    def format_row(cells: list[str]) -> str:
        return "| " + " | ".join(escape(cell) for cell in cells) + " |"

    separator = "|" + "|".join("---:" if a == "r" else "---" for a in aligns) + "|"
    lines = [format_row(headers), separator]
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)


def render_style(
    headers: list[str], rows: list[list[str]], aligns: list[str], style: str
) -> str:
    """Render a table in the requested layout style."""
    if style == "markdown":
        return render_markdown(headers, rows, aligns)
    return render_table(headers, rows, aligns)


# --------------------------------------------------------------------------- #
# Table A -- sweep inventory
# --------------------------------------------------------------------------- #


def build_inventory_rows(
    groups: list[SweepGroup], totals_by_sweep: dict[str, int] | None = None
) -> list[list[str]]:
    rows: list[list[str]] = []
    for group in groups:
        runs = group.runs
        n_runs = len(runs)
        total = (totals_by_sweep or {}).get(group.sweep, n_runs)
        runs_cell = f"{n_runs}/{total}" if total != n_runs else str(n_runs)

        adv_counts = [
            len(run_config["adversaries"])
            for run in runs
            for run_config in [run.run_config]
            if isinstance(run_config, dict)
            and isinstance(run_config.get("adversaries"), list)
        ]
        adv = _modal(adv_counts)

        pre_rounds = _modal_field(runs, lambda rc: rc.get("pre_coordination_rounds"))
        pre_effective = _modal_field(
            runs, lambda rc: rc.get("pre_coordination_rounds_effective")
        )
        precoord = f"{pre_rounds if pre_rounds is not None else '-'} / {pre_effective if pre_effective is not None else '-'}"

        post_msg = _modal_field(runs, lambda rc: rc.get("disable_post_message"))
        secret = _modal_field(runs, lambda rc: rc.get("effective_secret_channel"))

        agents = _modal([_num(run.metrics.get("total_agents")) for run in runs])
        patients = _modal(
            [
                _num(_dig(run.metrics, "meeting_outcome", "total_patients"))
                for run in runs
            ]
        )

        complete = sum(
            1
            for run in runs
            if _dig(run.metrics, "meeting_outcome", "status") == "complete"
        )

        source_cell = "; ".join(group.sources) if group.sources else "-"
        rows.append(
            [
                group.sweep,
                source_cell,
                runs_cell,
                str(adv) if adv is not None else "-",
                precoord,
                _yes_no(post_msg),
                _yes_no(secret),
                f"{agents:.0f}" if isinstance(agents, float) else "-",
                f"{patients:.0f}" if isinstance(patients, float) else "-",
                f"{complete}/{n_runs}",
            ]
        )
    return rows


def print_inventory(
    groups: list[SweepGroup],
    model: str,
    totals_by_sweep: dict[str, int] | None = None,
) -> None:
    print(f"\nTable A -- sweep inventory [{model or '-'}]")
    headers = [
        "Sweep",
        "Source",
        "Runs",
        "Adv",
        "PreCoord(round/effective)",
        "PostMsg restricted",
        "Secret channel",
        "Agents",
        "Patients",
        "Complete",
    ]
    rows = build_inventory_rows(groups, totals_by_sweep)
    aligns = ["l", "l", "r", "r", "l", "l", "l", "r", "r", "r"]
    print(render_table(headers, rows, aligns))


# --------------------------------------------------------------------------- #
# Table B -- metrics mean +/- std
# --------------------------------------------------------------------------- #


def compute_group_metrics(
    runs: list[RunData], specs: list[MetricSpec], ddof: int
) -> GroupMetrics:
    result = GroupMetrics(n_runs=len(runs))
    for spec in specs:
        values: list[float] = []
        for run in runs:
            value = spec.getter(run.metrics, run.run_config, run.costs)
            if value is not None:
                values.append(value)
        result.aggregates[spec.key] = aggregate(
            values, n_missing=len(runs) - len(values), ddof=ddof
        )
    return result


def _metric_cell(spec: MetricSpec, agg: Agg, n_runs: int) -> str:
    if agg.n == 0:
        return "n/a"
    cell = f"{format_value(spec.fmt, agg.mean)} +/- {format_value(spec.fmt, agg.std)}"
    if agg.n_missing > 0:
        cell += f" (n={agg.n}/{n_runs})"
    return cell


def print_metrics_table(
    sweeps: list[str],
    groups: dict[str, GroupMetrics],
    specs: list[MetricSpec],
    model: str,
) -> None:
    print(f"\nTable B -- metrics (mean +/- std) [{model or '-'}]")
    headers = ["Metric"] + sweeps
    rows: list[list[str]] = []

    n_row = ["n"] + [str(groups[s].n_runs) for s in sweeps]
    rows.append(n_row)

    for spec in specs:
        row = [spec.label]
        for sweep in sweeps:
            agg = groups[sweep].aggregates[spec.key]
            row.append(_metric_cell(spec, agg, groups[sweep].n_runs))
        rows.append(row)

    aligns = ["l"] + ["r"] * len(sweeps)
    print(render_table(headers, rows, aligns))


# --------------------------------------------------------------------------- #
# Table C -- delta vs reference
# --------------------------------------------------------------------------- #


@dataclass
class DeltaResult:
    mean: float | None
    std: float | None
    paired_n: int
    paired: bool


def compute_delta(
    spec: MetricSpec,
    sweep_runs: list[RunData],
    ref_runs: list[RunData],
    ddof: int,
) -> DeltaResult:
    """Compute sweep - reference difference, paired by seed when possible."""
    sweep_by_seed: dict[int, float] = {}
    for run in sweep_runs:
        seed = run.seed()
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if seed is not None and value is not None and seed not in sweep_by_seed:
            sweep_by_seed[seed] = value

    ref_by_seed: dict[int, float] = {}
    for run in ref_runs:
        seed = run.seed()
        value = spec.getter(run.metrics, run.run_config, run.costs)
        if seed is not None and value is not None and seed not in ref_by_seed:
            ref_by_seed[seed] = value

    paired_seeds = sorted(set(sweep_by_seed) & set(ref_by_seed))
    if paired_seeds:
        diffs = [sweep_by_seed[seed] - ref_by_seed[seed] for seed in paired_seeds]
        agg = aggregate(diffs, n_missing=0, ddof=ddof)
        return DeltaResult(
            mean=agg.mean, std=agg.std, paired_n=len(paired_seeds), paired=True
        )

    # Fall back to unpaired difference of means.
    sweep_values = [spec.getter(r.metrics, r.run_config, r.costs) for r in sweep_runs]
    ref_values = [spec.getter(r.metrics, r.run_config, r.costs) for r in ref_runs]
    sweep_clean = [v for v in sweep_values if v is not None]
    ref_clean = [v for v in ref_values if v is not None]
    if not sweep_clean or not ref_clean:
        return DeltaResult(mean=None, std=None, paired_n=0, paired=False)
    mean = statistics.fmean(sweep_clean) - statistics.fmean(ref_clean)
    return DeltaResult(mean=mean, std=None, paired_n=0, paired=False)


def print_delta_table(
    groups: list[SweepGroup],
    reference: SweepGroup,
    specs: list[MetricSpec],
    model: str,
    ddof: int,
) -> None:
    if len(groups) <= 1:
        return

    ref_label = reference.label
    ref_runs = reference.runs
    others = [g for g in groups if g is not reference]
    columns = [reference] + others

    headers = ["Metric", f"{ref_label} (ref)"]
    for group in others:
        headers.append(f"{group.label} - {ref_label}")

    rows: list[list[str]] = []
    any_paired = False

    for key in DELTA_METRICS:
        spec = METRICS_BY_KEY.get(key)
        if spec is None:
            continue
        row = [spec.label]
        for group in columns:
            result = compute_delta(spec, group.runs, ref_runs, ddof)
            if group is not reference and result.paired and result.std is not None:
                any_paired = True
            if result.mean is None:
                row.append("n/a")
                continue
            if result.paired and result.std is not None:
                cell = f"{format_value(spec.fmt, result.mean)} +/- {format_value(spec.fmt, result.std)}"
                cell += f" (k={result.paired_n})"
            else:
                cell = f"{format_value(spec.fmt, result.mean)} (unpaired)"
            row.append(cell)
        rows.append(row)

    label = "paired by seed" if any_paired else "unpaired mean difference"
    print(
        f"\nTable C -- Delta vs {ref_label} ({label}; negative = lower than "
        f"reference) [{model or '-'}]"
    )
    aligns = ["l"] + ["r"] * len(columns)
    print(render_table(headers, rows, aligns))


# --------------------------------------------------------------------------- #
# Transposed layout (formatted / markdown)
# --------------------------------------------------------------------------- #


def _transposed_metric_cell(spec: MetricSpec, agg: Agg, n_runs: int) -> str:
    """Cell for the transposed metrics table: ``mean ± std`` (+ ``(n/total)``)."""
    if agg.n == 0:
        return "n/a"
    cell = f"{format_value(spec.fmt, agg.mean)} ± {format_value(spec.fmt, agg.std)}"
    if agg.n_missing > 0:
        cell += f" ({agg.n}/{n_runs})"
    return cell


def _delta_cell(result: DeltaResult, spec: MetricSpec) -> str:
    """Cell for the transposed delta table, matching the plain delta convention."""
    if result.mean is None:
        return "n/a"
    if result.paired and result.std is not None:
        cell = f"{format_value(spec.fmt, result.mean)} ± {format_value(spec.fmt, result.std)}"
        return cell + f" (k={result.paired_n})"
    return f"{format_value(spec.fmt, result.mean)} (unpaired)"


def build_inventory_table(
    groups: list[SweepGroup],
    style: str,
    totals_by_sweep: dict[str, int] | None = None,
) -> str:
    headers = [
        "Sweep",
        "Source",
        "Runs",
        "Adv",
        "PreCoord(round/effective)",
        "PostMsg restricted",
        "Secret channel",
        "Agents",
        "Patients",
        "Complete",
    ]
    rows = build_inventory_rows(groups, totals_by_sweep)
    aligns = ["l", "l", "r", "r", "l", "l", "l", "r", "r", "r"]
    return render_style(headers, rows, aligns, style)


def build_transposed_metrics_table(
    group_list: list[SweepGroup],
    groups: dict[str, GroupMetrics],
    specs: list[MetricSpec],
    style: str,
) -> str:
    headers = ["Sweep", "n"] + [spec.label for spec in specs]
    aligns = ["l", "r"] + ["r"] * len(specs)
    rows: list[list[str]] = []
    for sweep_group in group_list:
        group = groups[sweep_group.label]
        row = [sweep_group.label, str(group.n_runs)]
        for spec in specs:
            row.append(
                _transposed_metric_cell(spec, group.aggregates[spec.key], group.n_runs)
            )
        rows.append(row)
    return render_style(headers, rows, aligns, style)


def build_transposed_delta_table(
    groups: list[SweepGroup],
    reference: SweepGroup,
    delta_specs: list[MetricSpec],
    ddof: int,
    style: str,
) -> str | None:
    if len(groups) <= 1:
        return None

    ref_runs = reference.runs
    headers = ["Sweep"] + [spec.label for spec in delta_specs]
    aligns = ["l"] + ["r"] * len(delta_specs)
    rows: list[list[str]] = []
    for group in groups:
        label = f"{group.label} (ref)" if group is reference else group.label
        row = [label]
        for spec in delta_specs:
            result = compute_delta(spec, group.runs, ref_runs, ddof)
            row.append(_delta_cell(result, spec))
        rows.append(row)
    return render_style(headers, rows, aligns, style)


# --------------------------------------------------------------------------- #
# File outputs
# --------------------------------------------------------------------------- #


def write_csv(
    path: str,
    specs: list[MetricSpec],
    models: list[str],
    groups_by_model: dict[str, list[SweepGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "model",
                "source",
                "source_path",
                "sweep",
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
                            group.sweep,
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


def write_json(
    path: str,
    roots_abs: list[str],
    sources: list[dict[str, Any]],
    ddof: int,
    runs_found: int,
    runs_used: int,
    specs: list[MetricSpec],
    groups_by_model: dict[str, list[SweepGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
    excluded_runs: list[tuple[RunData, list[str]]] | None = None,
    exclude_failed_runs: bool = False,
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
                "reason": _exclusion_reason(run, exclude_failed_runs),
            }
            for run, agents in (excluded_runs or [])
        ],
        "groups": {
            model: {
                group.label: {
                    "sweep": group.sweep,
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
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sweep_metrics_table",
        description="Print key metrics (mean +/- std) per sweep for a sweep root.",
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
        help="Reference sweep for the delta table.",
    )
    parser.add_argument(
        "--no-delta", dest="no_delta", action="store_true", help="Skip the delta table."
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
    return parser.parse_args(argv)


def resolve_metrics(metrics_arg: str | None) -> list[MetricSpec]:
    if not metrics_arg:
        return list(DEFAULT_METRICS)
    specs: list[MetricSpec] = []
    seen: set[str] = set()
    for token in (part.strip() for part in metrics_arg.split(",")):
        if not token:
            continue
        expanded = list(HEADLINE_PRESET) if token == "headline" else [token]
        for key in expanded:
            spec = METRICS_BY_KEY.get(key)
            if spec is None:
                print(f"warning: unknown metric key ignored: {key}", file=sys.stderr)
                continue
            if key in seen:
                continue
            seen.add(key)
            specs.append(spec)
    return specs or list(DEFAULT_METRICS)


def resolve_reference(
    groups: list[SweepGroup], requested: str | None, model: str
) -> SweepGroup | None:
    """Resolve the delta reference group for one model.

    ``requested`` may be a group label (exact match wins) or a raw sweep name.
    Returns ``None`` (with a warning/error on stderr) when it cannot be resolved.
    """
    if requested:
        for group in groups:
            if group.label == requested:
                return group
        matches = [group for group in groups if group.sweep == requested]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            labels = ", ".join(group.label for group in matches)
            print(
                f"error: reference '{requested}' is ambiguous for model "
                f"[{model or '-'}]: {labels}",
                file=sys.stderr,
            )
            return None
        print(
            f"warning: reference '{requested}' not found for model [{model or '-'}]",
            file=sys.stderr,
        )
        return None

    for group in groups:
        if group.sweep == "baseline_sweep":
            return group
    return groups[0] if groups else None


def print_transposed_output(
    models: list[str],
    groups_by_model: dict[str, list[SweepGroup]],
    groups: dict[str, dict[str, GroupMetrics]],
    specs: list[MetricSpec],
    delta_specs: list[MetricSpec],
    requested_reference: str | None,
    ddof: int,
    style: str,
    show_delta: bool,
    totals_by_model: dict[str, dict[str, int]] | None = None,
) -> None:
    """Print the three tables in the transposed layout (formatted / markdown)."""

    def heading(text: str) -> str:
        return f"**{text}**" if style == "markdown" else text

    totals_by_model = totals_by_model or {}
    blocks: list[str] = []
    for model in models:
        if len(models) > 1:
            blocks.append(heading(f"Model: {model or '-'}"))
        model_groups = groups_by_model[model]
        blocks.append(heading("Sweep inventory"))
        blocks.append(
            build_inventory_table(model_groups, style, totals_by_model.get(model))
        )
        blocks.append(heading("Metrics (mean ± std)"))
        blocks.append(
            build_transposed_metrics_table(model_groups, groups[model], specs, style)
        )
        if show_delta:
            reference = resolve_reference(model_groups, requested_reference, model)
            if reference is not None:
                table = build_transposed_delta_table(
                    model_groups, reference, delta_specs, ddof, style
                )
                if table is not None:
                    delta_heading = (
                        f"Delta vs {reference.label} (paired by seed; negative = "
                        f'lower than reference; the "{reference.label} (ref)" row '
                        f"is 0 by construction)"
                    )
                    blocks.append(heading(delta_heading))
                    blocks.append(table)
    print()
    print("\n\n".join(blocks))


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

    specs = resolve_metrics(args.metrics)

    found: list[str] = []
    runs: list[RunData] = []
    for root_abs in roots_abs:
        root_found = find_metric_files(root_abs)
        found.extend(root_found)
        runs.extend(load_runs(root_abs, root_found))
    runs_used = len(runs)

    kept_runs, excluded_runs = partition_runs(
        runs, args.exclude_error_runs, args.exclude_failed_runs
    )
    excluded_runs.sort(key=_excluded_sort_key)

    # Totals per (model, sweep) over everything on disk, so Table A can show
    # how many runs were aggregated versus how many exist.
    totals_by_model: dict[str, dict[str, int]] = {}
    for run in runs:
        model_totals = totals_by_model.setdefault(run.model, {})
        model_totals[run.sweep] = model_totals.get(run.sweep, 0) + 1

    grouped = group_runs(kept_runs)

    # Filter to requested sweeps/sources, if any.
    if args.sweeps:
        grouped = filter_groups_by_sweep(grouped, set(args.sweeps))
    if args.sources:
        grouped = filter_groups_by_source(grouped, set(args.sources))
    grouped = {model: groups for model, groups in grouped.items() if groups}
    if (args.sweeps or args.sources) and not grouped:
        print("warning: no groups matched the requested filters", file=sys.stderr)

    # Summarize only the sources that survive filtering, so the counts match the
    # tables below.
    selected_runs = [
        run
        for model_groups in grouped.values()
        for group in model_groups
        for run in group.runs
    ]
    source_summaries = summarize_sources(selected_runs)

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
                line += f"  ({_exclusion_reason(run, True)})"
            print(line)
    print(f"ddof:      {args.ddof}")

    groups: dict[str, dict[str, GroupMetrics]] = {}
    for model in models:
        groups[model] = {
            group.label: compute_group_metrics(group.runs, specs, args.ddof)
            for group in grouped[model]
        }

    delta_specs = [METRICS_BY_KEY[k] for k in DELTA_METRICS if k in METRICS_BY_KEY]
    show_delta = not args.no_delta

    if args.fmt == "plain":
        for model in models:
            print_inventory(grouped[model], model, totals_by_model.get(model))
            print_metrics_table(
                [group.label for group in grouped[model]],
                groups[model],
                specs,
                model,
            )

        if show_delta:
            for model in models:
                reference = resolve_reference(grouped[model], args.reference, model)
                if reference is None:
                    continue
                print_delta_table(
                    grouped[model],
                    reference,
                    delta_specs,
                    model,
                    args.ddof,
                )
    else:
        print_transposed_output(
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
        )

    if args.csv_path:
        write_csv(args.csv_path, specs, models, grouped, groups)
        print(f"\nWrote CSV: {os.path.abspath(args.csv_path)}")
    if args.json_path:
        write_json(
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
