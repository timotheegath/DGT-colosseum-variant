from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # headless-safe
import matplotlib.pyplot as plt

from experiments.common.plotting.io_utils import (
    ensure_dir,
    finite,
    mean,
    sem,
    sanitize_filename,
    write_csv,
)
from experiments.common.plotting.logging_utils import log_saved_plot
from experiments.common.plotting.style import apply_default_style

logger = logging.getLogger(__name__)

_ADVERSARY_BAR_COLOR = "#4c78a8"
_BENIGN_BAR_COLOR = "#f58518"

_STYLE = {
    "font.size": 24,
    "axes.labelsize": 24,
    "xtick.labelsize": 22,
    "ytick.labelsize": 24,
    "legend.fontsize": 24,
}


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _norm_role(value: Any) -> str:
    return str(value) if value is not None else "None"


def _adv_label(adversary_count: int) -> str:
    """X tick label for an adversary count value (0 is the benign baseline)."""
    return "Benign" if int(adversary_count) == 0 else str(int(adversary_count))


def _bar_color(adversary_count: int) -> str:
    return _BENIGN_BAR_COLOR if int(adversary_count) == 0 else _ADVERSARY_BAR_COLOR


def _aggregate_metric(
    rows: List[Dict[str, Any]],
    *,
    metric_key: str,
    default_when_missing: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float], int]:
    ys = finite([r.get(metric_key) for r in rows])
    if not ys and default_when_missing is not None and rows:
        ys = [float(default_when_missing)]
    if not ys:
        return None, None, 0
    return mean(ys), sem(ys), len(ys)


def _filter_rows(
    rows: List[Dict[str, Any]],
    *,
    num_agents: int,
    target_role: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        if _as_int(r.get("num_agents")) != int(num_agents):
            continue
        if _norm_role(r.get("target_role")) != str(target_role):
            continue
        out.append(r)
    return out


def _filter_adv_count(
    rows: List[Dict[str, Any]], adversary_count: int
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        if _as_int(r.get("adversary_count")) != int(adversary_count):
            continue
        out.append(r)
    return out


def _rows_for_adversary_count(
    main_rows: List[Dict[str, Any]],
    benign_rows: List[Dict[str, Any]],
    adversary_count: int,
) -> List[Dict[str, Any]]:
    """
    Rows for a single adversary count value.

    adversary_count == 0 is the benign baseline: it is taken from the main rows
    when present, otherwise it falls back to the separately-run benign sweep
    (benign_rows), when one was provided.
    """
    rows = _filter_adv_count(main_rows, int(adversary_count))
    if not rows and int(adversary_count) == 0:
        rows = _filter_adv_count(benign_rows, 0)
    return rows


def _default_when_missing(metric_key: str, adversary_count: int) -> Optional[float]:
    # Coalition regret is undefined for the benign baseline (no coalition): treat as 0.
    if int(adversary_count) == 0 and metric_key == "coalition_reward_regret":
        return 0.0
    return None


def _bar_by_adversary_count(
    *,
    run_rows: List[Dict[str, Any]],
    benign_rows: List[Dict[str, Any]],
    metric_key: str,
    metric_label: str,
    num_agents: int,
    target_role: str,
    adv_counts: Sequence[int],
    out_path: Path,
    emit_artifacts: bool = True,
) -> None:
    """
    Single-series bar plot: one bar per adversary count value present.

    adversary_count == 0 is the benign baseline bar; when the main rows carry no
    adversary_count == 0 runs, the separately-run benign sweep (benign_rows) is
    overlaid in its place.
    """
    apply_default_style(plt)
    plt.rcParams.update(_STYLE)
    ensure_dir(out_path.parent)

    main = _filter_rows(run_rows, num_agents=num_agents, target_role=target_role)
    base = _filter_rows(benign_rows, num_agents=num_agents, target_role=target_role)

    xs: List[int] = []
    heights: List[float] = []
    yerrs: List[float] = []
    labels: List[str] = []
    bar_colors: List[str] = []

    artifact_rows: List[Dict[str, Any]] = []

    for ac in sorted({int(a) for a in adv_counts}):
        rows_for_ac = _rows_for_adversary_count(main, base, int(ac))

        # Artifact: raw points used for this bar.
        for r in rows_for_ac:
            v = r.get(metric_key)
            if v is None:
                continue
            artifact_rows.append(
                {
                    "plot_type": "bar_by_adversary_count",
                    "plot_metric_key": metric_key,
                    "plot_metric_label": metric_label,
                    "num_agents": num_agents,
                    "target_role": target_role,
                    "adversary_count": int(ac),
                    "group": "benign" if int(ac) == 0 else "main",
                    "value": v,
                    "run_id": r.get("run_id"),
                    "seed": r.get("seed"),
                }
            )

        m, e, n = _aggregate_metric(
            rows_for_ac,
            metric_key=metric_key,
            default_when_missing=_default_when_missing(metric_key, int(ac)),
        )
        if m is None:
            continue
        xs.append(int(ac))
        heights.append(float(m))
        yerrs.append(float(e or 0.0))
        labels.append(_adv_label(int(ac)))
        bar_colors.append(_bar_color(int(ac)))

    if not xs:
        return

    fig, ax = plt.subplots(figsize=(9.2, 4.4))
    ax.grid(False)
    ax.bar(xs, heights, yerr=yerrs, capsize=3, color=bar_colors, alpha=0.9)
    # Make zero-height bars visible (e.g., benign regret is exactly 0).
    for x, h, c in zip(xs, heights, bar_colors):
        if abs(float(h)) < 1e-12:
            ax.plot([x], [0.0], marker="_", markersize=18, color=c, zorder=6)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=0, ha="center")
    ax.set_xlabel("Adversary Count")
    ax.set_ylabel(metric_label)

    fig.tight_layout()
    fig.savefig(out_path, format="pdf", bbox_inches="tight")
    log_saved_plot(out_path, logger=logger)
    plt.close(fig)

    if not emit_artifacts:
        return

    csv_path = out_path.with_suffix(".csv")
    script_path = out_path.with_name(out_path.stem + "__replot.py")
    write_csv(csv_path, artifact_rows)

    script = f"""\
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _as_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return float(sum(xs) / len(xs))


def _sem(xs: List[float]) -> float:
    if len(xs) <= 1:
        return 0.0
    m = float(sum(xs) / len(xs))
    var = sum((x - m) ** 2 for x in xs) / float(len(xs) - 1)
    return float(math.sqrt(var) / math.sqrt(len(xs)))


def _finite(xs: List[Any]) -> List[float]:
    out: List[float] = []
    for x in xs:
        f = _as_float(x)
        if f is None:
            continue
        if math.isfinite(f):
            out.append(float(f))
    return out


def main() -> None:
    here = Path(__file__).resolve().parent
    csv_path = here / {csv_path.name!r}
    rows: List[Dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(r))
    if not rows:
        raise SystemExit("No rows in CSV")

    metric_label = str(rows[0].get("plot_metric_label") or {metric_label!r})

    fig, ax = plt.subplots(figsize=(9.2, 4.4))
    ax.grid(False)

    xs: List[int] = []
    heights: List[float] = []
    yerrs: List[float] = []
    labels: List[str] = []
    colors: List[str] = []

    advs = sorted({{int(float(r.get("adversary_count") or 0)) for r in rows}})
    for ac in advs:
        vals = _finite(
            [
                r.get("value")
                for r in rows
                if int(float(r.get("adversary_count") or 0)) == int(ac)
            ]
        )
        if not vals:
            continue
        xs.append(int(ac))
        heights.append(float(_mean(vals) or 0.0))
        yerrs.append(float(_sem(vals)))
        labels.append("Benign" if int(ac) == 0 else str(int(ac)))
        color = "#f58518" if int(ac) == 0 else "#4c78a8"
        colors.append(color)
        ax.scatter([float(ac)] * len(vals), vals, s=16, alpha=0.25, color=color)

    ax.bar(xs, heights, yerr=yerrs, capsize=3, color=colors, alpha=0.9)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=0, ha="center")
    ax.set_xlabel("Adversary Count")
    ax.set_ylabel(metric_label)

    fig.tight_layout()
    out_pdf = here / {out_path.name!r}
    fig.savefig(out_pdf, format="pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
"""
    script_path.write_text(script, encoding="utf-8")


def _lines_over_adversary_count(
    *,
    run_rows: List[Dict[str, Any]],
    benign_rows: List[Dict[str, Any]],
    metric_key: str,
    metric_label: str,
    num_agents: int,
    target_role: str,
    adv_counts: Sequence[int],
    out_path: Path,
    emit_artifacts: bool = True,
) -> None:
    """
    Line plot: x=adversary_count, a single line over adversary count.

    adversary_count == 0 is the benign baseline point; when the main rows carry
    no adversary_count == 0 runs, the separately-run benign sweep (benign_rows)
    is overlaid in its place, so the benign point is kept when available.
    """
    apply_default_style(plt)
    plt.rcParams.update(_STYLE)
    ensure_dir(out_path.parent)

    main = _filter_rows(run_rows, num_agents=num_agents, target_role=target_role)
    base = _filter_rows(benign_rows, num_agents=num_agents, target_role=target_role)

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    ax.grid(False)

    xs: List[int] = []
    ys: List[float] = []
    es: List[float] = []

    for ac in sorted({int(a) for a in adv_counts}):
        rows_for_ac = _rows_for_adversary_count(main, base, int(ac))
        m, e, n = _aggregate_metric(
            rows_for_ac,
            metric_key=metric_key,
            default_when_missing=_default_when_missing(metric_key, int(ac)),
        )
        if m is None:
            continue
        xs.append(int(ac))
        ys.append(float(m))
        es.append(float(e or 0.0))

    if xs:
        ax.errorbar(
            xs,
            ys,
            yerr=es,
            marker="o",
            linewidth=3.0,
            markersize=8,
            elinewidth=2.0,
            capthick=2.0,
            capsize=3,
            color=_ADVERSARY_BAR_COLOR,
        )

    ax.set_xlabel("Adversary Count")
    ax.set_ylabel(metric_label)
    ax.set_xticks(sorted({int(a) for a in adv_counts}))

    fig.tight_layout()
    fig.savefig(out_path, format="pdf", bbox_inches="tight")
    log_saved_plot(out_path, logger=logger)
    plt.close(fig)

    if not emit_artifacts:
        return

    # Artifact: raw per-run points used for the line plot.
    csv_path = out_path.with_suffix(".csv")
    script_path = out_path.with_name(out_path.stem + "__replot.py")

    artifact_rows: List[Dict[str, Any]] = []
    for ac in sorted({int(a) for a in adv_counts}):
        rows_for_ac = _rows_for_adversary_count(main, base, int(ac))
        for r in rows_for_ac:
            v = r.get(metric_key)
            if v is None:
                continue
            artifact_rows.append(
                {
                    "plot_type": "lines_over_adversary_count",
                    "plot_metric_key": metric_key,
                    "plot_metric_label": metric_label,
                    "num_agents": num_agents,
                    "target_role": target_role,
                    "adversary_count": int(ac),
                    "group": "benign" if int(ac) == 0 else "main",
                    "value": v,
                    "run_id": r.get("run_id"),
                    "seed": r.get("seed"),
                }
            )

    write_csv(csv_path, artifact_rows)

    script = f"""\
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _as_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return float(sum(xs) / len(xs))


def _sem(xs: List[float]) -> float:
    if len(xs) <= 1:
        return 0.0
    m = float(sum(xs) / len(xs))
    var = sum((x - m) ** 2 for x in xs) / float(len(xs) - 1)
    return float(math.sqrt(var) / math.sqrt(len(xs)))


def _finite(xs: List[Any]) -> List[float]:
    out: List[float] = []
    for x in xs:
        f = _as_float(x)
        if f is None:
            continue
        if math.isfinite(f):
            out.append(float(f))
    return out


def main() -> None:
    here = Path(__file__).resolve().parent
    csv_path = here / {csv_path.name!r}
    rows: List[Dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(r))
    if not rows:
        raise SystemExit("No rows in CSV")

    metric_label = str(rows[0].get("plot_metric_label") or {metric_label!r})

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    ax.grid(False)

    xs: List[int] = []
    ys: List[float] = []
    es: List[float] = []

    advs = sorted({{int(float(r.get("adversary_count") or 0)) for r in rows}})
    for ac in advs:
        vals = _finite(
            [
                r.get("value")
                for r in rows
                if int(float(r.get("adversary_count") or 0)) == int(ac)
            ]
        )
        if not vals:
            continue
        xs.append(int(ac))
        ys.append(float(_mean(vals) or 0.0))
        es.append(float(_sem(vals)))

    if xs:
        ax.errorbar(
            xs,
            ys,
            yerr=es,
            marker="o",
            linewidth=3.0,
            markersize=8,
            elinewidth=2.0,
            capthick=2.0,
            capsize=3,
            color="#4c78a8",
        )

    ax.set_xlabel("Adversary Count")
    ax.set_ylabel(metric_label)
    ax.set_xticks(advs)

    fig.tight_layout()
    out_pdf = here / {out_path.name!r}
    fig.savefig(out_pdf, format="pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
"""
    script_path.write_text(script, encoding="utf-8")


@dataclass(frozen=True)
class OverviewSpec:
    adv_counts: Tuple[int, ...] = (0, 2, 4, 8)


def plot_overview(
    *,
    run_rows: List[Dict[str, Any]],
    benign_run_rows: Optional[List[Dict[str, Any]]],
    out_dir: Path,
    spec: OverviewSpec = OverviewSpec(),
) -> None:
    """
    Produces:
      - bar plots by adversary count (per adv_count in {2,4,8}, with the benign
        baseline bar at adversary_count 0 overlaid from the benign sweep)
      - line plots over adversary_count (0,2,4,8), one line per (target_role, num_agents)
    Stratified by:
      - target_role (departmental vs Resource_Provisioner)
      - num_agents (requested sweep value)
    """
    ensure_dir(out_dir)
    benign_run_rows = benign_run_rows or []

    roles = sorted(
        {
            _norm_role(r.get("target_role"))
            for r in run_rows
            if r.get("target_role") is not None
        }
    )
    num_agents_vals = sorted(
        {int(r.get("num_agents")) for r in run_rows if r.get("num_agents") is not None}
    )

    if not roles or not num_agents_vals:
        return

    for role in roles:
        role_dir = out_dir / "by_target_role" / sanitize_filename(role)
        ensure_dir(role_dir)

        for n in num_agents_vals:
            n_dir = role_dir / f"n{int(n)}"
            ensure_dir(n_dir)

            # Line plots across adversary counts for this role + n.
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="joint_reward",
                metric_label="Joint Reward",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "joint_reward_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_avg",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "coalition_advantage_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_hospital_avg",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "coalition_advantage_hospital_level_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_hospital_avg_actual",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir
                / "coalition_advantage_hospital_level_actual_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_avg_hospital_adjusted",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir
                / "coalition_advantage_hospital_adjusted_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_avg_hospital_adjusted_normalized",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir
                / "coalition_advantage_hospital_adjusted_normalized_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_minus_noncoalition_hospital_avg_hospital_adjusted_normalized",
                metric_label="Coalition Advantage",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir
                / "coalition_advantage_hospital_level_hospital_adjusted_normalized_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="joint_reward_regret",
                metric_label="Overall Regret",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "overall_regret_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_reward_regret",
                metric_label="Coalition Regret",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "coalition_regret_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="coalition_reward_regret_hospital_adjusted",
                metric_label="Coalition Regret",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir / "coalition_regret_hospital_adjusted_vs_adv_count.pdf",
            )
            _lines_over_adversary_count(
                run_rows=run_rows,
                benign_rows=benign_run_rows,
                metric_key="agent_resource_failure_penalty_total",
                metric_label="Resource Failure Penalty",
                num_agents=int(n),
                target_role=role,
                adv_counts=spec.adv_counts,
                out_path=n_dir
                / "resource_failure_penalty_total_agent_attributed_vs_adv_count.pdf",
            )

            # Bar plots by adversary count: the benign baseline (adversary_count 0,
            # overlaid from the benign sweep when available) plus the adversary bar.
            for ac in [a for a in spec.adv_counts if a != 0]:
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="joint_reward",
                    metric_label="Joint Reward",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"joint_reward_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_avg",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"coalition_advantage_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_hospital_avg",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_advantage_hospital_level_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_hospital_avg_actual",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_advantage_hospital_level_actual_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_avg_hospital_adjusted",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_advantage_hospital_adjusted_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_avg_hospital_adjusted_normalized",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_advantage_hospital_adjusted_normalized_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_minus_noncoalition_hospital_avg_hospital_adjusted_normalized",
                    metric_label="Coalition Advantage",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_advantage_hospital_level_hospital_adjusted_normalized_adv{int(ac)}.pdf",
                )

                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="hospital_holding_cost_total",
                    metric_label="Holding Cost",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"holding_cost_total_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="hospital_resource_failure_penalty_total",
                    metric_label="Resource Failure Penalty",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"resource_failure_penalty_total_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="agent_resource_failure_penalty_total",
                    metric_label="Resource Failure Penalty",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"resource_failure_penalty_total_agent_attributed_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="hospital_missed_step_penalty_total",
                    metric_label="Missed-Step Penalty",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"missed_step_penalty_total_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="joint_reward_regret",
                    metric_label="Overall Regret",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"overall_regret_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="joint_reward_regret_normalized",
                    metric_label="Overall Regret",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"overall_regret_normalized_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_reward_regret",
                    metric_label="Coalition Regret",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir / f"coalition_regret_adv{int(ac)}.pdf",
                )
                _bar_by_adversary_count(
                    run_rows=run_rows,
                    benign_rows=benign_run_rows,
                    metric_key="coalition_reward_regret_hospital_adjusted",
                    metric_label="Coalition Regret",
                    num_agents=int(n),
                    target_role=role,
                    adv_counts=(0, int(ac)),
                    out_path=n_dir
                    / f"coalition_regret_hospital_adjusted_adv{int(ac)}.pdf",
                )
