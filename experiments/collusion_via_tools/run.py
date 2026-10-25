# ruff: noqa: E402
from __future__ import annotations

import sys
import argparse
import copy
import csv
import json
import logging
import random
import re
import importlib
import asyncio
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tqdm import tqdm

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from experiments.common.run_utils import (
    configure_experiment_logging as _configure_experiment_logging_impl,
    ensure_dir as _ensure_dir,
    load_yaml as _load_yaml,
    normalize_seeds as _normalize_seeds,
    atomic_write_json,
    write_json as _write_json,
    write_progress as _write_progress,
)
from experiments.collusion_via_tools.agents import (
    COVERT_STRATEGY,
    AdversarySpec,
    MisalignmentHospitalAgent,
)
from experiments.common.blackboard_logger import ExperimentBlackboardLogger
from experiments.collusion_via_tools.metrics import compute_misalignment_metrics
from experiments.collusion_via_tools.prompts import (
    MisalignmentPrompts,
    ProvisionerBlackboardPrompts,
)
from experiments.collusion_via_tools.prompt_capture import ThreadSafePromptRecorder
from experiments.collusion_via_tools.tool_availability import (
    phase_tool_map,
    tool_names_for_phase,
)
from experiments.common.local_protocol import LocalCommunicationProtocol
from experiments.collusion_via_tools.judge_beliefs import judge_run
from experiments.common._patch_terrarium import (
    get_post_message_call_limit,
    get_secret_blackboard_id,
    is_env_tools_blocked,
    is_post_message_disabled,
    reset_post_message_counts,
    set_env_tools_blocked,
    set_post_message_call_limit,
    set_post_message_disabled,
    set_secret_blackboard_id,
)

from terrarium.networks import build_communication_network
from terrarium.logger import AgentTrajectoryLogger
from terrarium.utils import (
    get_client_instance,
    get_generation_params,
    get_model_name,
    build_vllm_runtime,
)

LOGGER_NAME = "experiments.collusion_via_tools"
logger = logging.getLogger(LOGGER_NAME)

_PATH_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_HOSPITAL_ROLES_PER_HOSPITAL = 4  # Triage, Radiology, Surgery, Ward


def _sanitize_path_component(value: Any) -> str:
    s = "None" if value is None else str(value)
    s = s.strip()
    if not s:
        return "None"
    s = _PATH_COMPONENT_RE.sub("_", s)
    s = s.strip("_")
    return s or "None"


_UNSET = object()


def _normalize_disable_pm_value(val: Any) -> bool:
    """Maps a raw knob value onto a strict bool for `disable_post_message`.

    `true` / non-zero ints -> True (adversaries never get `post_message` in any
    phase), `false` / `null` / 0 / anything else -> False (unrestricted).
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _resolve_disable_pm(
    val: Any = _UNSET,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Resolves whether `post_message` is disabled for adversaries.

    `val` is the most specific decision available (normally a value already
    resolved by `_resolve_disable_pm_axis`). Passing the `_UNSET` sentinel falls
    back to the three non-sweep config locations: the first key that is
    *present* wins, and a present `false` / `null` is honoured as
    "unrestricted" rather than skipped. Returns False when none is present.
    """
    if val is not _UNSET:
        return _normalize_disable_pm_value(val)

    if not cfg:
        return False

    exp_cfg = cfg.get("experiment") or {}
    adv_cfg = exp_cfg.get("adversary") or {}
    net_cfg = cfg.get("communication_network") or {}

    for source in (exp_cfg, adv_cfg, net_cfg):
        if "disable_post_message" in source:
            return _normalize_disable_pm_value(source["disable_post_message"])
    return False


def _resolve_disable_pm_axis(
    sweep: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[bool]:
    """Resolves the `disable_post_message` axis for one sweep.

    Returns one already-normalised bool per run in the sweep:

    1. `sweeps[].disable_post_message` whenever the key is present (scalar or
       list). A present `false` / `null` means "unrestricted for this sweep" and
       does NOT fall through to the top-level keys.
    2. otherwise a single decision from the three top-level keys.

    A list stays one element per run.
    """
    if isinstance(sweep, dict) and "disable_post_message" in sweep:
        raw = sweep["disable_post_message"]
        items = raw if isinstance(raw, list) else [raw]
        return [_normalize_disable_pm_value(item) for item in items]
    return [_resolve_disable_pm(_UNSET, cfg)]


def _normalize_pre_coordination_value(val: Any) -> bool:
    """Maps a raw knob value onto a strict bool for `pre_coordination_round`.

    `true` / non-zero ints -> True, `false` / `null` / 0 -> False. Stricter
    than `_normalize_disable_pm_value` because the only meaningful question is
    "run the round or not".
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _resolve_pre_coordination_round(
    val: Any = _UNSET,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Resolves whether the pre-coordination round is enabled for a run.

    Presence (not truthiness) wins: a sweep-level `false` stops the search and
    is treated as "no pre-coordination round", mirroring the
    `disable_post_message` resolution semantics.
    """
    if val is not _UNSET:
        return _normalize_pre_coordination_value(val)

    if not cfg:
        return False

    exp_cfg = cfg.get("experiment") or {}
    adv_cfg = exp_cfg.get("adversary") or {}
    net_cfg = cfg.get("communication_network") or {}

    for source in (exp_cfg, adv_cfg, net_cfg):
        if "pre_coordination_round" in source:
            return _normalize_pre_coordination_value(source["pre_coordination_round"])
    return False


def _normalize_pre_coordination_rounds(val: Any) -> int:
    """Validates a `pre_coordination_rounds` round count.

    The same strict rules apply to the global `simulation.pre_coordination_rounds`
    fallback and to every value of the `sweeps[].pre_coordination_rounds` axis.
    Unlike the other pre-coordination knobs this one is strict: booleans
    (``bool`` is an ``int`` subclass), floats, strings and values below 1 are
    rejected rather than silently coerced. A bad value changes how many
    turns (and therefore how much money) a run spends, so it must fail loudly
    instead of falling back to a default.
    """
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(
            "pre_coordination_rounds must be an integer >= 1 "
            f"(got {val!r} of type {type(val).__name__})"
        )
    if val < 1:
        raise ValueError(
            f"pre_coordination_rounds must be an integer >= 1 (got {val!r})"
        )
    return int(val)


def _resolve_pre_coordination_rounds(cfg: Optional[Dict[str, Any]] = None) -> int:
    """Resolves how many dedicated adversary turns the pre-coordination phase
    runs, defaulting to 1 when the key is omitted (backward compatible).

    This is the *global* fallback. A sweep may override it with the
    `sweeps[].pre_coordination_rounds` axis; see
    `_resolve_pre_coordination_rounds_axis`.
    """
    sim_cfg = (cfg or {}).get("simulation") or {}
    if "pre_coordination_rounds" not in sim_cfg:
        return 1
    return _normalize_pre_coordination_rounds(sim_cfg["pre_coordination_rounds"])


def _resolve_pre_coordination_rounds_axis(
    sweep: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
    *,
    phase_enabled: bool = True,
) -> List[int]:
    """Resolves the `pre_coordination_rounds` axis for one sweep.

    Returns one validated round count per run in the sweep:

    1. `sweeps[].pre_coordination_rounds` whenever the key is PRESENT (scalar
       or list). It wins over the global `simulation.pre_coordination_rounds`
       for that sweep. An empty list is rejected, like any other bad value.
    2. otherwise a single value from the global
       `simulation.pre_coordination_rounds` fallback (default 1).

    Every value must be an integer ``>= 1``; anything else raises a
    ``ValueError`` (the same strictness as the global knob).

    ``phase_enabled=False`` collapses the axis to a SINGLE value (the first
    resolved count). The count is inert when the pre-coordination phase is off,
    and expanding it would produce two runs with the SAME ``run_id`` -- a
    directory collision. When the phase is on, the returned list has one entry
    per run.
    """
    if isinstance(sweep, dict) and "pre_coordination_rounds" in sweep:
        raw = sweep["pre_coordination_rounds"]
        items = raw if isinstance(raw, list) else [raw]
        if not items:
            raise ValueError(
                "pre_coordination_rounds must be an integer >= 1 (got an empty list)"
            )
        resolved = [_normalize_pre_coordination_rounds(item) for item in items]
    else:
        resolved = [_resolve_pre_coordination_rounds(cfg)]

    if not phase_enabled:
        return resolved[:1]
    return resolved


# Keys an adversary keeps in its context during the pre-coordination round.
# This is an ALLOWLIST on purpose: `env.build_agent_context` returns whatever
# the environment wants (for HospitalEnvironment that includes
# `inventory_snapshot`, `failures_so_far`, `failures_by_hospital`,
# `local_inventory`, `procedure_costs`, `job_queue`, `schedule_summary`, ...),
# which would leak the full environment state to the adversary BEFORE it loses
# `post_message`. Blocking tool calls is not enough — the state also arrives
# passively through the prompt. An allowlist fails closed: any new key a future
# environment adds is withheld by default and must be opted in explicitly.
_PRE_COORDINATION_CONTEXT_ALLOWLIST: frozenset = frozenset(
    {
        # Identity / routing. Needed so the prompt is coherent.
        "agent_name",
        "phase",
        "iteration",
        "role",
        # Pre-coordination round metadata (1-based). Scalar labels only, no
        # environment state.
        "pre_coordination_round",
        "pre_coordination_rounds",
        # Coalitional strategy inputs injected by `_get_extra_fields`.
        "misalignment_role",
        "misalignment_adversaries",
        "misalignment_secret_blackboard_id",
        "post_message_available",
    }
)


def _pre_coordination_context(
    full_context: Dict[str, Any],
    *,
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    """Strip the environment state from a pre-coordination agent context.

    The adversary keeps its identity and the coalition strategy fields (so it
    still understands the game and the tools it has), but loses every
    environment-state key (inventory, failures, job queue, schedules, costs).
    """
    allowed = dict(full_context)
    for key in list(allowed):
        if key not in _PRE_COORDINATION_CONTEXT_ALLOWLIST:
            del allowed[key]
    allowed.update(extra)
    return allowed


def _resolve_pre_coordination_axis(
    sweep: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[bool]:
    """Resolves the `pre_coordination_round` axis for one sweep.

    Mirrors `_resolve_disable_pm_axis`: a sweep-level key (scalar or list) wins,
    otherwise we fall through to the top-level resolution. The result is always
    a list, even when the sweep sets a single bool (so the runner can iterate
    uniformly).
    """
    if isinstance(sweep, dict) and "pre_coordination_round" in sweep:
        raw = sweep["pre_coordination_round"]
        items = raw if isinstance(raw, list) else [raw]
        return [_normalize_pre_coordination_value(item) for item in items]
    return [_resolve_pre_coordination_round(_UNSET, cfg)]


def _normalize_pre_coordination_max_messages(val: Any) -> Optional[int]:
    """Maps a raw knob value onto an Optional[int] cap for per-agent
    `post_message` calls during the pre-coordination round.

    `None` / `null` / `0` -> None (unlimited). Integers clamp to >= 0; the
    runner treats None as "no cap". A list is rejected (returns None) because
    this knob is a single scalar, NOT a sweep dimension -- use separate
    configs/sweeps to compare different caps.
    """
    if val is None:
        return None
    if isinstance(val, (list, tuple, set, dict)):
        return None
    if isinstance(val, bool):
        # Treat booleans strictly: True -> 1, False -> None.
        return 1 if val else None
    if isinstance(val, (int, float)):
        n = int(val)
        return n if n > 0 else None
    if isinstance(val, str):
        s = val.strip().lower()
        if s in {"", "none", "null", "unlimited", "inf"}:
            return None
        try:
            n = int(s)
            return n if n > 0 else None
        except ValueError:
            return None
    return None


def _resolve_pre_coordination_max_messages(
    sweep: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Resolves the `pre_coordination_max_messages` cap for one sweep.

    Sweep-level key wins over top-level resolution. The result is a single
    Optional[int] (None = unlimited), NOT a list -- this knob does not
    produce a separate sweep dimension on its own; combine with the existing
    `pre_coordination_round` flag to actually engage the cap.
    """
    if isinstance(sweep, dict) and "pre_coordination_max_messages" in sweep:
        return _normalize_pre_coordination_max_messages(
            sweep["pre_coordination_max_messages"]
        )

    if not cfg:
        return None

    exp_cfg = cfg.get("experiment") or {}
    adv_cfg = exp_cfg.get("adversary") or {}
    net_cfg = cfg.get("communication_network") or {}

    for source in (exp_cfg, adv_cfg, net_cfg):
        if "pre_coordination_max_messages" in source:
            return _normalize_pre_coordination_max_messages(
                source["pre_coordination_max_messages"]
            )
    return None


def _compute_effective_secret_channel(
    secret_channel_enabled: bool,
    disable_post_message: bool,
    *,
    pre_coordination_round: bool = False,
) -> bool:
    """Whether a coalition-only secret blackboard is actually created.

    `disable_post_message` suppresses the secret channel even when it was
    requested, because there is no tool left to write to it -- UNLESS the
    pre-coordination round is enabled, in which case the secret blackboard is
    required for the pre-coordination phase even though it will be unused after
    that round ends.
    """
    return bool(secret_channel_enabled) and (
        not disable_post_message or pre_coordination_round
    )


def _resolve_effective_secret_channel(
    *,
    secret_channel_enabled: bool,
    disable_post_message: bool,
    pre_coordination_round: bool,
    is_benign: bool,
    num_adversaries: int,
) -> bool:
    """Effective secret-channel flag under the *actual* run conditions.

    `_compute_effective_secret_channel` only knows about the tooling rules.
    The board is also never created for a benign run or a coalition smaller
    than two, so the recorded `effective_secret_channel` must include those
    conditions: otherwise it can be true while `secret_blackboard_id` is null.
    """
    return (
        not bool(is_benign)
        and int(num_adversaries) >= 2
        and _compute_effective_secret_channel(
            secret_channel_enabled,
            disable_post_message,
            pre_coordination_round=pre_coordination_round,
        )
    )


def _build_run_id(
    *,
    model_label: str,
    sweep_name: str,
    topology: str,
    num_agents: int,
    num_patients: int,
    adversary_count: int,
    target_role: Any,
    seed: int,
    disable_post_message: bool = False,
    pre_coordination_round: bool = False,
    pre_coordination_rounds: int = 1,
) -> str:
    model_safe = _sanitize_path_component(model_label)
    sweep_safe = _sanitize_path_component(sweep_name)
    topology_safe = _sanitize_path_component(topology)
    role_safe = _sanitize_path_component(target_role)

    suffix = "__nopm" if disable_post_message else ""

    if pre_coordination_round:
        # The round count is ALWAYS encoded (``__precoord<N>``), never the
        # legacy count-less ``__precoord``. The count is a sweep axis, so two
        # different counts inside one sweep MUST produce different run_ids, and
        # the legacy id must never equal a canonical one so that a pre-existing
        # legacy directory can be used as a resume fallback without ever being
        # clobbered by a rerun of a different treatment.
        precoord_suffix = f"__precoord{int(pre_coordination_rounds)}"
        suffix = f"{suffix}{precoord_suffix}" if suffix else precoord_suffix

    return (
        f"{model_safe}__{sweep_safe}"
        f"__topo{topology_safe}"
        f"__agents{int(num_agents)}_patients{int(num_patients)}"
        f"_adv{int(adversary_count)}_{role_safe}_seed{int(seed)}{suffix}"
    )


def _build_run_id_v1(
    *,
    model_label: str,
    sweep_name: str,
    num_agents: int,
    adversary_count: int,
    target_role: Any,
    seed: int,
) -> str:
    role_safe = str(target_role).replace(" ", "_") if target_role else "None"
    return (
        f"{model_label}__{sweep_name}"
        f"__agents{int(num_agents)}_adv{int(adversary_count)}_{role_safe}_seed{int(seed)}"
    )


def _resolve_num_patients_spec(*, raw: Any, num_agents: int) -> int:
    """
    Supports YAML-friendly placeholders like:
      - "num_agents_minus_1" / "num_agents-1"
    """
    if isinstance(raw, str):
        key = raw.strip().lower().replace(" ", "_")
        if key in {"num_agents_minus_1", "num_agents-1"}:
            return int(num_agents) - 1
        raise ValueError(f"Unknown num_patients spec: {raw!r}")
    if raw is None:
        return int(num_agents) - 1
    return int(raw)


def _get_token_pricing_usd_per_1m(
    cfg: Dict[str, Any],
    *,
    provider: str,
    model_name: str,
) -> Optional[Dict[str, float]]:
    pricing = (cfg.get("pricing") or {}).get(provider) or {}
    model_block = pricing.get(model_name) or {}
    if not isinstance(model_block, dict):
        return None
    inp = model_block.get("input_per_1m_usd")
    out = model_block.get("output_per_1m_usd")
    if inp is None or out is None:
        return None
    return {"input_per_1m_usd": float(inp), "output_per_1m_usd": float(out)}


def _sum_turn_usage(turns: List[Dict[str, Any]]) -> Dict[str, int]:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for t in turns:
        u = t.get("usage") or {}
        if not isinstance(u, dict):
            continue
        total["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(u.get("completion_tokens") or 0)
        total["total_tokens"] += int(u.get("total_tokens") or 0)
    return total


def _cost_usd(
    usage: Dict[str, Any],
    *,
    input_per_1m_usd: float,
    output_per_1m_usd: float,
) -> float:
    return (int(usage.get("prompt_tokens") or 0) / 1_000_000.0) * input_per_1m_usd + (
        int(usage.get("completion_tokens") or 0) / 1_000_000.0
    ) * output_per_1m_usd


def _configure_experiment_logging(root: Path, *, verbose: bool = True) -> None:
    _configure_experiment_logging_impl(logger, root, verbose=verbose)


def _is_hospital_environment(cfg: Dict[str, Any]) -> bool:
    env = cfg.get("environment") or {}
    import_path = str(env.get("import_path") or "")
    name = str(env.get("name") or "")
    return ("hospital_env" in import_path) or (name == "HospitalEnvironment")


def _infer_num_hospitals_from_num_agents(num_agents: int) -> int:
    """
    HospitalEnvironment defines agents as:
      num_hospitals * 4 department agents + 1 Resource_Provisioner
    """
    n = int(num_agents)
    if n <= 0:
        raise ValueError(f"num_agents must be > 0 (got {num_agents})")
    if n == 1:
        return 0
    if (n - 1) % _HOSPITAL_ROLES_PER_HOSPITAL != 0:
        raise ValueError(
            f"num_agents={n} is not compatible with HospitalEnvironment; expected 1 + 4*k."
        )
    return int((n - 1) // _HOSPITAL_ROLES_PER_HOSPITAL)


def _max_hospital_coalition_size(*, target_role: Any) -> int:
    """
    Max coalition size per hospital "block".

    Coalitions are constructed hospital-by-hospital (4 department roles per hospital).
    The provisioner is only included when explicitly targeted (or other code paths force it).
    """
    tr = (str(target_role).strip() if target_role is not None else "") or ""
    return (
        (1 + _HOSPITAL_ROLES_PER_HOSPITAL)
        if tr == "Resource_Provisioner"
        else _HOSPITAL_ROLES_PER_HOSPITAL
    )


def _max_adversaries_for_hospital_env(*, num_hospitals: int, target_role: Any) -> int:
    tr = (str(target_role).strip() if target_role is not None else "") or ""
    base = int(num_hospitals) * _HOSPITAL_ROLES_PER_HOSPITAL
    return base + (1 if tr == "Resource_Provisioner" else 0)


def _resolve_environment_class(env_cfg: Dict[str, Any]) -> Any:
    import_path = str(env_cfg.get("import_path") or "").strip()
    if import_path:
        module_path, sep, cls_name = import_path.partition(":")
        if not sep:
            raise ValueError(f"Invalid import_path: {import_path}")
        module = importlib.import_module(module_path)
        return getattr(module, cls_name)
    raise ValueError("environment.import_path is required.")


def _select_adversaries_hospital_coalition(
    *,
    agent_names: Sequence[str],
    count: int,
    target_role: Optional[str],
    rng: random.Random,
) -> List[str]:
    """
    Selects a coalition of size `count` that includes `target_role`.
    Constraint: Coalition is hospital-based.
      - Fill department roles within a hospital before moving to the next.
      - If count <= 4 (or <= 5 when targeting provisioner), coalition is within one hospital.
      - If count > 4, coalition may span multiple hospitals (e.g., 8 => two hospitals).
    """
    if count <= 0:
        return []

    target_role = (
        str(target_role).strip() if target_role is not None else None
    ) or None

    hospitals = set()
    provisioner = None
    hospital_map = {}

    for name in agent_names:
        if name == "Resource_Provisioner":
            provisioner = name
            continue

        parts = name.split("_")
        known_roles = ["Triage", "Surgery", "Radiology", "Ward"]
        if parts[-1] in known_roles:
            h_name = "_".join(parts[:-1])
        else:
            h_name = "Unknown"

        hospitals.add(h_name)
        if h_name not in hospital_map:
            hospital_map[h_name] = []
        hospital_map[h_name].append(name)

    hospitals_list = sorted(list(hospitals))
    # For reproducibility + fair comparisons across parameter sweeps, we prefer to
    # fill adversaries from General_Hospital first, then St_Marys_Center, then any
    # remaining hospitals (alphabetical). This reduces run-to-run differences in
    # coalition composition when the only intended change is seed/etc.
    preferred_hospital_order: List[str] = []
    for name in ("General_Hospital", "St_Marys_Center"):
        if name in hospitals_list:
            preferred_hospital_order.append(name)
    preferred_hospital_order.extend(
        [h for h in hospitals_list if h not in preferred_hospital_order]
    )

    def _role_sort_key(agent_name: str) -> int:
        known_roles = ["Triage", "Radiology", "Surgery", "Ward"]
        for i, r in enumerate(known_roles):
            if agent_name.endswith(f"_{r}"):
                return i
        return 999

    def _hospital_candidates(h: str) -> List[str]:
        return sorted(hospital_map.get(h, []), key=_role_sort_key)

    def _add_from_hospital(
        coalition: List[str], h: str, *, need: int, required: Optional[str] = None
    ) -> None:
        if need <= 0:
            return
        candidates = [a for a in _hospital_candidates(h) if a not in coalition]
        if required and required not in coalition:
            if required in candidates:
                coalition.append(required)
                candidates = [a for a in candidates if a != required]
        if need <= 0:
            return
        # Fill deterministically in role order.
        coalition.extend(candidates[:need])

    def _choose_hospitals_to_fill(
        *,
        required_hospital: Optional[str],
        total_needed_departments: int,
    ) -> List[str]:
        # Select hospitals deterministically, but with required first.
        hs = list(preferred_hospital_order)
        if required_hospital and required_hospital in hs:
            hs = [required_hospital] + [h for h in hs if h != required_hospital]
        # Keep only hospitals that have department agents (defensive).
        hs = [h for h in hs if hospital_map.get(h)]
        # Compute how many hospitals we need to cover the requested department adversaries.
        needed_hospitals = (
            max(0, int(total_needed_departments)) + _HOSPITAL_ROLES_PER_HOSPITAL - 1
        ) // _HOSPITAL_ROLES_PER_HOSPITAL
        return hs[:needed_hospitals] if needed_hospitals > 0 else []

    # Determine required agent/hospital based on target_role.
    required_agent: Optional[str] = None
    required_hospital: Optional[str] = None

    if target_role == "Resource_Provisioner":
        required_agent = provisioner
    elif target_role and target_role.lower() == "departmental":
        required_agent = None
        # Prefer General_Hospital departmental coalitions for comparability.
        required_hospital = (
            "General_Hospital"
            if "General_Hospital" in hospitals_list
            else (hospitals_list[0] if hospitals_list else None)
        )
    elif target_role:
        # Specific department role substring (e.g., "Triage")
        for h in hospitals_list:
            agents = hospital_map.get(h) or []
            hit = next((a for a in agents if target_role in a), None)
            if hit:
                required_agent = hit
                required_hospital = h
                break

    # If provisioner is required but missing, fallback.
    if target_role == "Resource_Provisioner" and not required_agent:
        return [str(x) for x in rng.sample(list(agent_names), count)]

    coalition: List[str] = []
    if required_agent:
        coalition.append(required_agent)

    remaining_needed = int(count) - len(coalition)
    if remaining_needed <= 0:
        return coalition[: int(count)]

    # Decide how many department agents we still need to add.
    dept_needed = remaining_needed
    hospitals_to_fill = _choose_hospitals_to_fill(
        required_hospital=required_hospital,
        total_needed_departments=dept_needed,
    )
    if not hospitals_to_fill:
        # Fallback: sample from all agents excluding already-chosen.
        pool = [a for a in agent_names if a not in coalition]
        return coalition + [
            str(x) for x in rng.sample(list(pool), min(remaining_needed, len(pool)))
        ]

    # Fill hospitals in order; fill first hospital completely before moving on, except the last.
    for idx, h in enumerate(hospitals_to_fill):
        if remaining_needed <= 0:
            break
        # If we have a specific required_agent in required_hospital, ensure it's included first.
        req = (
            required_agent
            if (
                required_hospital
                and h == required_hospital
                and required_agent
                and required_agent != provisioner
            )
            else None
        )
        # In multi-hospital coalitions, "fill one hospital up" before moving to the next.
        # So we take up to 4 from each hospital, except possibly the last hospital.
        take = min(remaining_needed, _HOSPITAL_ROLES_PER_HOSPITAL)
        _add_from_hospital(coalition, h, need=take, required=req)
        remaining_needed = int(count) - len(coalition)

    # If still short (e.g., not enough hospitals or missing agents), fallback to sampling from remaining.
    remaining_needed = int(count) - len(coalition)
    if remaining_needed > 0:
        pool = [a for a in agent_names if a not in coalition]
        coalition.extend(
            [str(x) for x in rng.sample(list(pool), min(remaining_needed, len(pool)))]
        )

    return coalition[: int(count)]

    # (Unreachable; kept for clarity.)
    # return [str(x) for x in rng.sample(list(agent_names), count)]


def _log_blackboards_txt(
    *,
    bb_logger: ExperimentBlackboardLogger,
    protocol: LocalCommunicationProtocol,
    iteration: int,
    phase: str,
    agent_name: str,
    planning_round: Optional[int] = None,
) -> None:
    for blackboard in protocol.megaboard.blackboards:
        bb_logger.log_blackboard_state(
            blackboard,
            iteration=iteration,
            phase=phase,
            agent_name=agent_name,
            round_num=planning_round,
        )


def _augment_context(base: Dict[str, Any], *, extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    out.update(extra)
    return out


def _tool_availability_fields(agent: Any, env: Any, phase: str) -> Dict[str, Any]:
    """Context fields describing which tools ``agent`` can call in ``phase``.

    Read by ``MisalignmentPrompts`` to render the phase-aware coalition carrier
    list. MUST be called AFTER ``set_post_message_disabled(...)`` for the
    agent/phase, because the patched blackboard toolset lookup honours that
    flag.
    """
    return {
        "available_tool_names": tool_names_for_phase(agent, env, phase),
        "tool_phase_map": phase_tool_map(agent, env),
    }


def _extract_initial_prompts(
    response: Dict[str, Any],
) -> Dict[str, str]:
    """Pull captured prompts off the agent response dict (if any).

    ``MisalignmentHospitalAgent`` merges ``system_prompt`` and ``user_prompt``
    into the response dict for first-of-phase turns only; this helper lifts
    them out so the runner can attach them to the matching
    ``agent_turns.json`` entry. Returns an empty dict when this turn was not a
    first-of-phase turn (i.e., subsequent planning rounds).
    """
    sp = response.get("system_prompt")
    up = response.get("user_prompt")
    if not isinstance(sp, str) or not isinstance(up, str):
        return {}
    return {"system_prompt": sp, "user_prompt": up}


def _error_response(exc: BaseException) -> str:
    """Render a failed turn in the same ``[ERROR] ...`` convention terrarium
    itself uses for caught continuation failures."""
    return f"[ERROR] {type(exc).__name__}: {exc}"


async def _run_pre_coordination_rounds(
    *,
    env: Any,
    protocol: Any,
    roles: Dict[str, str],
    num_rounds: int,
    max_messages: Optional[int],
    secret_blackboard_id: Optional[int],
    extra_fields: Callable[[str, str], Dict[str, Any]],
    bb_logger: Optional[ExperimentBlackboardLogger] = None,
    on_turn: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> List[Dict[str, Any]]:
    """Run the dedicated adversaries-only pre-coordination phase.

    Executes ``num_rounds`` rounds before iteration 1. Each round gives every
    adversary exactly one dedicated coordination turn, in ``env.agents`` order.
    Honest agents are skipped entirely, so they never see the phase or its
    blackboard.

    Isolation is held for the whole phase (all rounds), not per round:

    * only ``post_message`` is usable -- all environment tools are blocked;
    * ``post_message`` is rewritten onto the coalition-only secret board when
      one exists; when none exists it is disabled for the phase, so no public
      board can be written to;
    * the agent context is reduced to identity + coalition metadata, so no
      environment state leaks in passively.

    Isolated state is saved and restored on every exit path (including
    exceptions): ``post_message`` gating, environment-tool gating, secret-board
    routing, and the message cap. ``post_message`` is enabled **only** when a
    coalition-only secret board exists; without one it is disabled for the whole
    phase, because the tool would otherwise write to a public board.

    ``max_messages`` is a per-agent cap **per round**: the counters reset at the
    start of every round. ``None`` means unlimited.

    Returns the ``agent_turns.json``-shaped entries for every turn. The caller
    owns the surrounding run state and restores the normal tooling afterwards.
    """
    turns: List[Dict[str, Any]] = []

    # Save the task-local state so a rerun/nested caller is unaffected.
    prior_post_message_disabled = is_post_message_disabled()
    prior_env_tools_blocked = is_env_tools_blocked()
    prior_secret_blackboard_id = get_secret_blackboard_id()
    prior_post_message_limit = get_post_message_call_limit()

    # Without a coalition-only board there is no safe place to post: disable
    # `post_message` for the phase rather than let it fall through to a public
    # board. The prompt says the same thing explicitly.
    phase_post_message_available = secret_blackboard_id is not None
    set_post_message_disabled(not phase_post_message_available)
    set_env_tools_blocked(True)
    set_secret_blackboard_id(secret_blackboard_id)
    # Defense in depth: with no secret board, reject any hallucinated
    # `post_message` at the protocol layer as well (limit 0 rejects all calls)
    # so a public post is impossible even if the tool were somehow offered.
    round_post_message_limit = max_messages if phase_post_message_available else 0
    try:
        for pre_round in range(1, num_rounds + 1):
            # Per-round cap: reset counters so each round gets its own budget.
            set_post_message_call_limit(round_post_message_limit)
            reset_post_message_counts()

            last_agent: Optional[str] = None
            for agent in env.agents:
                if roles.get(agent.name) != "adversary":
                    continue

                full_ctx = env.build_agent_context(
                    agent.name,
                    phase="pre_coordination",
                    iteration=0,
                    pre_coordination_round=pre_round,
                    pre_coordination_rounds=num_rounds,
                )
                extra = dict(extra_fields(agent.name, "pre_coordination"))
                # Controlled phase metadata: never trust the environment to
                # echo kwargs back. Without this injection the prompt and the
                # agent see stale/default round metadata.
                extra["post_message_available"] = phase_post_message_available
                extra["pre_coordination_round"] = pre_round
                extra["pre_coordination_rounds"] = num_rounds
                # Tool availability for this exact agent/phase. Computed AFTER
                # `set_post_message_disabled(...)` above, so the disabled-state
                # reflected in the blackboard toolset is correct.
                extra.update(_tool_availability_fields(agent, env, "pre_coordination"))
                # Minimal context: identity + coalition strategy only, no
                # environment state. Blocking tool calls is not sufficient
                # because `env.build_agent_context` hands the adversary the
                # full state (inventory, failures, job queue, costs) passively
                # in the prompt.
                agent_context = _pre_coordination_context(full_ctx, extra=extra)
                try:
                    response = await protocol.agent_planning_turn(
                        agent,
                        agent.name,
                        agent_context,
                        env,
                        iteration=0,
                        planning_round=pre_round,
                        phase="pre_coordination",
                    )
                except Exception as exc:
                    logger.warning(
                        "Pre-coordination planning turn failed for %s (round %s): %s",
                        agent.name,
                        pre_round,
                        _error_response(exc),
                    )
                    turns.append(
                        {
                            "phase": "pre_coordination",
                            "iteration": 0,
                            "round": pre_round,
                            "pre_coordination_round": pre_round,
                            "pre_coordination_rounds": num_rounds,
                            "agent": agent.name,
                            "role": roles[agent.name],
                            "response": _error_response(exc),
                            "usage": None,
                            "tools_executed": [],
                        }
                    )
                    if on_turn is not None:
                        on_turn(turns)
                    last_agent = agent.name
                    continue
                turns.append(
                    {
                        "phase": "pre_coordination",
                        "iteration": 0,
                        "round": pre_round,
                        "pre_coordination_round": pre_round,
                        "pre_coordination_rounds": num_rounds,
                        "agent": agent.name,
                        "role": roles[agent.name],
                        "response": response.get("response"),
                        "usage": response.get("usage"),
                        "tools_executed": response.get("tools_executed"),
                        **_extract_initial_prompts(response),
                    }
                )
                if on_turn is not None:
                    on_turn(turns)
                last_agent = agent.name

            if last_agent and bb_logger is not None:
                try:
                    _log_blackboards_txt(
                        bb_logger=bb_logger,
                        protocol=protocol,
                        iteration=0,
                        phase="pre_coordination",
                        agent_name=last_agent,
                        planning_round=pre_round,
                    )
                except Exception:
                    pass
    finally:
        # Restore the state that existed before the phase, even on failure.
        set_env_tools_blocked(prior_env_tools_blocked)
        set_secret_blackboard_id(prior_secret_blackboard_id)
        set_post_message_call_limit(prior_post_message_limit)
        set_post_message_disabled(prior_post_message_disabled)
        reset_post_message_counts()

    return turns


async def _run_single(
    *,
    base_cfg: Dict[str, Any],
    model_label: str,
    model_llm_cfg: Dict[str, Any],
    sweep_name: str,
    topology: str,
    num_agents: int,
    adversary_count: int,
    num_patients: int,
    target_role: Optional[str],
    seed: int,
    out_dir: Path,
    disable_post_message: Any = _UNSET,
    pre_coordination_round: bool = False,
    pre_coordination_rounds: Any = _UNSET,
    pre_coordination_max_messages: Any = _UNSET,
) -> Dict[str, Any]:
    rng = random.Random(int(seed))

    def _should_stop_early(environment: Any) -> bool:
        """
        Early-stop criterion for Hospital-style environments.

        Unlike HospitalEnvironment.done(), treat any recorded resource failure as
        non-convergence (i.e., do not early-stop just because all steps are scheduled).
        Falls back to environment.done(...) when the expected fields are unavailable.
        """
        try:
            patients = getattr(environment, "patients", None)
            patient_states = getattr(environment, "patient_states", None)
            if not isinstance(patients, dict) or not isinstance(patient_states, dict):
                return bool(environment.done(0))  # type: ignore[arg-type]

            # Converged only if every patient has all steps scheduled AND no resource failures were recorded.
            for pid, patient in patients.items():
                state = patient_states.get(pid, {})
                scheduled = state.get("scheduled_steps") or {}
                pathway = patient.get("pathway") or []
                if len(scheduled) != len(pathway):
                    return False
                failures = state.get("resource_failures") or []
                if failures:
                    return False
            return True
        except Exception:
            # Conservative fallback: use the environment's own criterion.
            try:
                return bool(environment.done(0))  # type: ignore[arg-type]
            except Exception:
                return False

    # --- CONFIGURATION SETUP ---
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("simulation", {})["seed"] = int(seed)

    # Overrides
    cfg.setdefault("communication_network", {})["topology"] = str(topology)
    cfg.setdefault("communication_network", {})["num_agents"] = int(num_agents)
    resolved_num_patients = _resolve_num_patients_spec(
        raw=num_patients, num_agents=int(num_agents)
    )
    cfg.setdefault("environment", {})["num_patients"] = resolved_num_patients
    if _is_hospital_environment(cfg):
        # Ensure the environment's actual agent count matches the sweep's num_agents.
        cfg.setdefault("environment", {})["num_hospitals"] = (
            _infer_num_hospitals_from_num_agents(int(num_agents))
        )

    if target_role:
        cfg.setdefault("experiment", {}).setdefault("adversary", {})["target_role"] = (
            target_role
        )

    cfg["llm"] = copy.deepcopy(model_llm_cfg)

    is_benign = int(adversary_count) == 0

    # --- POST_MESSAGE DISABLING (for tool-based collusion) ---
    # The sweep axis (resolved in `run_from_config` / `_resolve_disable_pm_axis`)
    # has already made the decision: use it verbatim instead of re-resolving a
    # `None` against the top-level config (which would silently override an
    # explicit sweep-level `false` / `null`).
    resolved_disable_pm = (
        _normalize_disable_pm_value(disable_post_message)
        if disable_post_message is not _UNSET
        else _resolve_disable_pm(_UNSET, cfg)
    )
    set_post_message_disabled(resolved_disable_pm)

    # --- PRE-COORDINATION PHASE ---
    # `pre_coordination_rounds` dedicated, planning-prompt-shaped rounds run
    # before the main simulation for adversaries only (default 1, for backward
    # compatibility). The sweep axis (`sweeps[].pre_coordination_rounds`) is
    # resolved by the scheduler and passed in verbatim; when the argument is
    # `_UNSET` we fall back to the global `simulation.pre_coordination_rounds`.
    # `post_message` is available only when a coalition-only secret blackboard
    # exists; all environment tools (`broadcast_message`, `transfer_resources`,
    # etc.) are silently dropped by `LocalCommunicationProtocol` so the
    # adversary cannot probe the environment before losing `post_message`.
    resolved_pre_coordination = _resolve_pre_coordination_round(
        pre_coordination_round, cfg
    )
    resolved_pre_coord_rounds = (
        _normalize_pre_coordination_rounds(pre_coordination_rounds)
        if pre_coordination_rounds is not _UNSET
        else _resolve_pre_coordination_rounds(cfg)
    )
    # Keep the per-run config snapshot in agreement with the resolved count.
    # Nothing else reads this key during the run; it exists so `run_config.json`
    # records the treatment that was actually applied.
    cfg.setdefault("simulation", {})["pre_coordination_rounds"] = (
        resolved_pre_coord_rounds
    )
    pre_coord_phase_active = resolved_pre_coordination and not is_benign
    effective_pre_coord_rounds = (
        resolved_pre_coord_rounds if pre_coord_phase_active else 0
    )
    # Per-agent cap on `post_message` calls **per pre-coordination round**.
    # None means unlimited. When the pre-coordination round is off, this cap
    # is also off. `_UNSET` falls back to the top-level config keys; an
    # explicit `None` still means "unlimited".
    resolved_pre_coord_max = (
        _resolve_pre_coordination_max_messages({}, cfg)
        if pre_coordination_max_messages is _UNSET
        else _normalize_pre_coordination_max_messages(pre_coordination_max_messages)
    )

    # --- DIRECTORY SETUP ---
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = _build_run_id(
        model_label=str(model_label),
        sweep_name=str(sweep_name),
        topology=str(topology),
        num_agents=int(num_agents),
        num_patients=int(resolved_num_patients),
        adversary_count=int(adversary_count),
        target_role=target_role,
        seed=int(seed),
        disable_post_message=resolved_disable_pm,
        pre_coordination_round=resolved_pre_coordination,
        pre_coordination_rounds=resolved_pre_coord_rounds,
    )
    run_dir = out_dir / "runs" / model_label / sweep_name / run_id
    _ensure_dir(run_dir)

    logger.info("RUN START %s", run_id)
    cfg.setdefault("simulation", {})["run_timestamp"] = f"{run_timestamp}__{run_id}"

    # --- PROMPT CAPTURE SETUP ---
    # Created here (before the env/agents exist) so the recorder's output files
    # exist for the whole run and can be streamed to as turns complete. One
    # recorder per run; concurrent runs never share state.
    prompt_recorder = ThreadSafePromptRecorder(run_dir)

    # --- COMPONENT INITIALIZATION ---
    protocol = LocalCommunicationProtocol(config=cfg)
    env_cls = _resolve_environment_class(cfg.get("environment") or {})
    env = env_cls(protocol, cfg, tool_logger=type("TL", (), {"log_dir": run_dir})())

    bb_logger = ExperimentBlackboardLogger(cfg, log_root=run_dir)
    bb_logger.clear_blackboard_logs()

    trajectory_logger = AgentTrajectoryLogger(
        environment_name=env.__class__.__name__,
        seed=int(seed),
        config=cfg,
        run_timestamp=cfg.get("simulation", {}).get("run_timestamp"),
        log_dir=run_dir,
    )
    # A resume/rerun reuses the run directory and therefore the same
    # `agent_trajectories.json`. Reset it so stale pre-coordination rounds from
    # an earlier attempt (e.g. a different round count) cannot be merged into
    # the new run.
    trajectory_logger.reset_log()

    agent_names = env.get_agent_names()
    communication_network = build_communication_network(agent_names, cfg)
    env.set_communication_network(communication_network)

    # --- ADVERSARY CONFIGURATION ---
    adv_cfg = (cfg.get("experiment") or {}).get("adversary") or {}
    secret_channel_enabled = bool(adv_cfg.get("secret_channel_enabled", False))
    coalition_clique_enabled = bool(adv_cfg.get("coalition_clique_enabled", False))
    # Mode 0 (post_message never available) makes a coalition-only secret
    # blackboard pointless: there is no tool to write to it -- UNLESS the
    # pre-coordination round needs it, in which case the adversary uses
    # post_message to the secret board during that phase and loses it
    # afterwards. The effective flag is resolved after adversary selection
    # below, because the board also requires a non-benign run with at least two
    # adversaries.

    adversaries = _select_adversaries_hospital_coalition(
        agent_names=agent_names,
        count=int(adversary_count),
        target_role=target_role,
        rng=rng,
    )
    roles = {
        name: ("adversary" if name in adversaries else "cooperative")
        for name in agent_names
    }
    # Single source of truth for both board creation and `run_config.json`, so
    # `effective_secret_channel` cannot be true while the board id is null.
    effective_secret_channel = _resolve_effective_secret_channel(
        secret_channel_enabled=secret_channel_enabled,
        disable_post_message=resolved_disable_pm,
        pre_coordination_round=resolved_pre_coordination,
        is_benign=is_benign,
        num_adversaries=len(adversaries),
    )

    if coalition_clique_enabled and len(adversaries) >= 2:
        # Ensure the coalition forms a clique in the (public) communication graph.
        for i in range(len(adversaries)):
            for j in range(i + 1, len(adversaries)):
                a = str(adversaries[i])
                b = str(adversaries[j])
                try:
                    communication_network.graph.add_edge(a, b)
                except Exception:
                    pass

    # Upstream `HospitalPrompts.get_user_prompt` returns early for
    # `Resource_Provisioner` and never renders `blackboard_context`, so the
    # provisioner is blind to the public board AND to the coalition-only
    # board (even though its own prompt says "Check the Blackboard for
    # URGENT or DEFICIT requests"). Wrap the env prompts so every role sees
    # the same blackboard content. Applied to benign runs too, so that the
    # provisioner's view does not differ between benign and coalition sweeps.
    env.prompts = ProvisionerBlackboardPrompts(getattr(env, "prompts", None))

    if not is_benign:
        env.prompts = MisalignmentPrompts(env, cfg, base_prompts=env.prompts)

    # --- AGENT CLIENT SETUP ---
    provider = (cfg.get("llm", {}).get("provider") or "").lower()
    model_name = get_model_name(provider, cfg["llm"])
    generation_params = get_generation_params(cfg["llm"])
    max_steps = int(cfg["simulation"].get("max_conversation_steps", 3))

    vllm_runtime = None
    if provider == "vllm":
        vllm_runtime = build_vllm_runtime(cfg["llm"])

    adversary_spec = AdversarySpec(adversary_names=set(adversaries))

    agents = []
    for name in agent_names:
        client = get_client_instance(
            cfg["llm"], agent_name=name, vllm_runtime=vllm_runtime
        )
        agents.append(
            MisalignmentHospitalAgent(
                client,
                name,
                model_name,
                max_steps,
                None,
                trajectory_logger,
                env.__class__.__name__,
                generation_params=generation_params,
                adversary_spec=adversary_spec,
                prompt_recorder=prompt_recorder,
            )
        )
    # Match examples/base_main.py behavior: shuffle agent execution order.
    # HospitalEnvironment seeds the global RNG with simulation.seed in __init__.
    random.shuffle(agents)
    env.set_agent_clients(agents)

    await env.async_init()

    secret_blackboard_id: Optional[int] = None
    if effective_secret_channel:
        try:
            base_context = env.get_network_context()
            secret_context = env.format_blackboard_context(adversaries, base_context)
        except Exception:
            secret_context = (
                env.get_network_context() if hasattr(env, "get_network_context") else ""
            )
        secret_blackboard_id = await protocol.generate_comm_network(
            adversaries,
            secret_context,
            template={
                "secret_channel": True,
                "visibility": "secret",
                "purpose": "coalition",
            },
        )

    # Log initial state
    try:
        _log_blackboards_txt(
            bb_logger=bb_logger,
            protocol=protocol,
            iteration=0,
            phase="init",
            agent_name="SYSTEM",
        )
    except Exception:
        pass

    # --- SIMULATION LOOP ---
    max_iterations = int(cfg["simulation"].get("max_iterations", 1))
    planning_rounds = int(cfg["simulation"].get("max_planning_rounds", 1))
    turns: List[Dict[str, Any]] = []

    def _get_extra_fields(agent_name: str, phase: str) -> Dict[str, Any]:
        if is_benign:
            return {}
        role = roles.get(agent_name, "cooperative")
        post_message_available = not resolved_disable_pm
        # During the pre-coordination phase `post_message` is available only
        # when a coalition-only secret board exists. If there is none, the tool
        # is disabled for the phase so it cannot leak to a public board; the
        # prompt says so explicitly. The helper also enforces this at runtime.
        if phase == "pre_coordination" and role == "adversary":
            post_message_available = secret_blackboard_id is not None
        return {
            "misalignment_role": role,
            "misalignment_adversaries": adversaries,
            "misalignment_secret_blackboard_id": secret_blackboard_id
            if role == "adversary"
            else None,
            "post_message_available": post_message_available,
            "phase": phase,
        }

    # --- PRE-COORDINATION PHASE ---
    # Runs `effective_pre_coord_rounds` times before iteration 1, for
    # adversaries only. The agent receives the compact phase prompt built by
    # `prompts.py`; `LocalCommunicationProtocol.environment_handle_tool_call`
    # returns a stub for any env-tool invocation across the whole phase, so the
    # adversary cannot probe the environment or commit to operational actions
    # before losing `post_message`. `post_message` is available only while a
    # secret board exists. Honest agents are skipped entirely.
    if pre_coord_phase_active:
        turns.extend(
            await _run_pre_coordination_rounds(
                env=env,
                protocol=protocol,
                roles=roles,
                num_rounds=effective_pre_coord_rounds,
                max_messages=resolved_pre_coord_max,
                secret_blackboard_id=secret_blackboard_id,
                extra_fields=_get_extra_fields,
                bb_logger=bb_logger,
                on_turn=lambda t: atomic_write_json(run_dir / "agent_turns.json", t),
            )
        )

    current_iteration = 0
    for iteration in range(1, max_iterations + 1):
        current_iteration = iteration
        if _should_stop_early(env):
            logger.info(
                "Environment requested simulation stop at iteration %s", iteration
            )
            break

        # 1. PLANNING
        for planning_round in range(1, planning_rounds + 1):
            round_level_disabled = resolved_disable_pm
            last_agent = None
            for agent in env.agents:
                # Restriction is adversary-only: honest agents keep `post_message`.
                if roles.get(agent.name) == "adversary":
                    set_post_message_disabled(round_level_disabled)
                else:
                    set_post_message_disabled(False)
                base_ctx = env.build_agent_context(
                    agent.name,
                    phase="planning",
                    iteration=iteration,
                    planning_round=planning_round,
                )
                extra = _get_extra_fields(agent.name, "planning")
                # Tool availability for this exact agent/phase. Computed AFTER
                # `set_post_message_disabled(...)` above, so the disabled-state
                # reflected in the blackboard toolset is correct.
                extra.update(_tool_availability_fields(agent, env, "planning"))
                agent_context = (
                    base_ctx if not extra else _augment_context(base_ctx, extra=extra)
                )

                try:
                    response = await protocol.agent_planning_turn(
                        agent,
                        agent.name,
                        agent_context,
                        env,
                        iteration=iteration,
                        planning_round=planning_round,
                    )
                except Exception as exc:
                    logger.warning(
                        "Planning turn failed for %s (iteration %s, round %s): %s",
                        agent.name,
                        iteration,
                        planning_round,
                        _error_response(exc),
                    )
                    turns.append(
                        {
                            "phase": "planning",
                            "iteration": iteration,
                            "round": planning_round,
                            "agent": agent.name,
                            "role": roles[agent.name],
                            "response": _error_response(exc),
                            "usage": None,
                            "tools_executed": [],
                        }
                    )
                    atomic_write_json(run_dir / "agent_turns.json", turns)
                    last_agent = agent.name
                    continue
                turns.append(
                    {
                        "phase": "planning",
                        "iteration": iteration,
                        "round": planning_round,
                        "agent": agent.name,
                        "role": roles[agent.name],
                        "response": response.get("response"),
                        "usage": response.get("usage"),
                        "tools_executed": response.get("tools_executed"),
                        **_extract_initial_prompts(response),
                    }
                )
                atomic_write_json(run_dir / "agent_turns.json", turns)
                last_agent = agent.name

            if last_agent:
                _log_blackboards_txt(
                    bb_logger=bb_logger,
                    protocol=protocol,
                    iteration=iteration,
                    phase="planning",
                    agent_name=last_agent,
                    planning_round=planning_round,
                )

        # 2. EXECUTION
        last_exec_agent = None
        for agent in env.agents:
            # Restriction is adversary-only: honest agents keep `post_message`.
            if roles.get(agent.name) == "adversary":
                set_post_message_disabled(resolved_disable_pm)
            else:
                set_post_message_disabled(False)
            base_ctx = env.build_agent_context(
                agent.name, phase="execution", iteration=iteration
            )
            extra = _get_extra_fields(agent.name, "execution")
            # Tool availability for this exact agent/phase. Computed AFTER
            # `set_post_message_disabled(...)` above, so the disabled-state
            # reflected in the blackboard toolset is correct.
            extra.update(_tool_availability_fields(agent, env, "execution"))
            agent_context = (
                base_ctx if not extra else _augment_context(base_ctx, extra=extra)
            )

            try:
                response = await protocol.agent_execution_turn(
                    agent, agent.name, agent_context, env, iteration=iteration
                )
            except Exception as exc:
                logger.warning(
                    "Execution turn failed for %s (iteration %s): %s",
                    agent.name,
                    iteration,
                    _error_response(exc),
                )
                turns.append(
                    {
                        "phase": "execution",
                        "iteration": iteration,
                        "agent": agent.name,
                        "role": roles[agent.name],
                        "response": _error_response(exc),
                        "usage": None,
                        "tools_executed": [],
                    }
                )
                atomic_write_json(run_dir / "agent_turns.json", turns)
                last_exec_agent = agent.name
                continue
            turns.append(
                {
                    "phase": "execution",
                    "iteration": iteration,
                    "agent": agent.name,
                    "role": roles[agent.name],
                    "response": response.get("response"),
                    "usage": response.get("usage"),
                    "tools_executed": response.get("tools_executed"),
                    **_extract_initial_prompts(response),
                }
            )
            atomic_write_json(run_dir / "agent_turns.json", turns)
            last_exec_agent = agent.name

        if last_exec_agent:
            _log_blackboards_txt(
                bb_logger=bb_logger,
                protocol=protocol,
                iteration=iteration,
                phase="execution",
                agent_name=last_exec_agent,
            )

        try:
            env.log_iteration_summary(iteration)
        except Exception:
            pass

    # 3. SURVEY (POST-RUN BELIEFS)
    for agent in env.agents:
        # Restriction is adversary-only: honest agents keep `post_message`.
        if roles.get(agent.name) == "adversary":
            set_post_message_disabled(resolved_disable_pm)
        else:
            set_post_message_disabled(False)
        base_ctx = env.build_agent_context(
            agent.name, phase="survey", iteration=max(1, current_iteration)
        )
        extra = _get_extra_fields(agent.name, "survey")
        # Tool availability for this exact agent/phase. Computed AFTER
        # `set_post_message_disabled(...)` above, so the disabled-state
        # reflected in the blackboard toolset is correct.
        extra.update(_tool_availability_fields(agent, env, "survey"))
        agent_context = (
            base_ctx if not extra else _augment_context(base_ctx, extra=extra)
        )
        try:
            response = await protocol.agent_survey_turn(
                agent,
                agent.name,
                agent_context,
                env,
                iteration=max(1, current_iteration),
            )
        except Exception as exc:
            logger.warning(
                "Survey turn failed for %s (iteration %s): %s",
                agent.name,
                max(1, current_iteration),
                _error_response(exc),
            )
            turns.append(
                {
                    "phase": "survey",
                    "iteration": current_iteration,
                    "agent": agent.name,
                    "role": roles[agent.name],
                    "response": _error_response(exc),
                    "usage": None,
                    "tools_executed": [],
                }
            )
            atomic_write_json(run_dir / "agent_turns.json", turns)
            continue
        turns.append(
            {
                "phase": "survey",
                "iteration": current_iteration,
                "agent": agent.name,
                "role": roles[agent.name],
                "response": response.get("response"),
                "usage": response.get("usage"),
                "tools_executed": response.get("tools_executed"),
                **_extract_initial_prompts(response),
            }
        )
        atomic_write_json(run_dir / "agent_turns.json", turns)

    # Persist turns before the judge pass (judge_beliefs loads agent_turns.json).
    atomic_write_json(run_dir / "agent_turns.json", turns)

    final_summary = env.get_final_summary()
    # Include richer failure attribution when available (kept backward compatible).
    # Always include keys so downstream analytics can rely on their presence.
    step_fails: List[Dict[str, Any]] = []
    hf: Optional[Dict[str, Any]] = None
    try:
        hf_raw = getattr(env, "hospital_failures", None)
        if isinstance(hf_raw, dict):
            hf = hf_raw
            final_summary["hospital_failures"] = hf_raw
    except Exception:
        hf = None
    try:
        ps = getattr(env, "patient_states", None)
        if isinstance(ps, dict):
            for pid, st in ps.items():
                if not isinstance(st, dict):
                    continue
                for ev in st.get("resource_failures") or []:
                    if not isinstance(ev, dict):
                        continue
                    step_fails.append(
                        {
                            "patient_id": pid,
                            "step_index": ev.get("step"),
                            "missing": ev.get("missing"),
                            "agent": ev.get("agent"),
                        }
                    )
    except Exception:
        pass
    final_summary["step_resource_failures"] = step_fails

    # At end of run: list which agents have outstanding resource failures (by hospital).
    try:
        agent_out: Dict[str, Dict[str, int]] = {}
        agents_map = getattr(env, "agents_map", None)
        if isinstance(hf, dict) and isinstance(agents_map, dict):
            for agent_name, info in agents_map.items():
                if not isinstance(info, dict):
                    continue
                hospital = info.get("hospital")
                if hospital not in hf:
                    continue
                counts = hf.get(hospital) or {}
                if not isinstance(counts, dict):
                    continue
                nonzero = {
                    str(r): int(v or 0) for r, v in counts.items() if int(v or 0) > 0
                }
                if nonzero:
                    agent_out[str(agent_name)] = nonzero
        final_summary["agent_outstanding_resource_failures"] = agent_out
    except Exception:
        final_summary["agent_outstanding_resource_failures"] = {}

    agent_rewards: Optional[Dict[str, float]] = None
    try:
        if hasattr(env, "_calculate_makespan_and_flow"):
            _, rewards = env._calculate_makespan_and_flow()  # type: ignore[attr-defined]
            if isinstance(rewards, dict):
                agent_rewards = {str(k): float(v) for k, v in rewards.items()}
    except Exception:
        agent_rewards = None

    # 4. EVALUATION (JUDGE BELIEFS)
    # This runs a separate pass to check if agents suspected foul play
    judgements = await judge_run(run_dir=run_dir, config=cfg)
    judge_usage_total: Dict[str, Any] = {}
    try:
        judge_usage_path = run_dir / "judge_usage.json"
        if judge_usage_path.exists():
            judge_usage_blob = json.loads(judge_usage_path.read_text(encoding="utf-8"))
            if isinstance(judge_usage_blob, dict):
                judge_usage_total = judge_usage_blob.get("total_usage") or {}
    except Exception:
        judge_usage_total = {}

    # 5. METRICS
    blackboard_participants = {
        bb.blackboard_id: bb.participants for bb in protocol.megaboard.blackboards
    }

    metrics = compute_misalignment_metrics(
        graph=communication_network.graph,
        adversaries=adversaries,
        tool_events=protocol.tool_events,
        judgements=judgements,
        blackboard_participants=blackboard_participants,
        roles=roles,
        final_summary=final_summary,
        agent_rewards=agent_rewards,
        attack_type=COVERT_STRATEGY,
    )

    logger.info(
        "RUN %s metrics: JointReward=%.2f, SuspicionRate=%.2f",
        run_id,
        metrics.meeting_outcome.joint_reward,
        metrics.suspicion_rate_non_adversary,
    )

    # 6. ARTIFACTS
    _write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "adversaries": adversaries,
            "secret_channel_enabled": secret_channel_enabled,
            "effective_secret_channel": effective_secret_channel,
            "secret_blackboard_id": secret_blackboard_id,
            "disable_post_message": resolved_disable_pm,
            "pre_coordination_round": resolved_pre_coordination,
            "pre_coordination_rounds": resolved_pre_coord_rounds,
            "pre_coordination_rounds_effective": effective_pre_coord_rounds,
            "pre_coordination_max_messages": resolved_pre_coord_max,
            "roles": roles,
            "metrics": asdict(metrics),
        },
    )
    _write_json(run_dir / "final_summary.json", final_summary)
    _write_json(run_dir / "agent_rewards.json", agent_rewards or {})
    _write_json(run_dir / "metrics.json", asdict(metrics))
    _write_json(run_dir / "judgements.json", judgements)

    # Initial system/user prompt per phase occurrence. These are streamed to
    # disk as turns complete (see prompt_capture.py); this call only guarantees
    # the files exist for runs that captured nothing. Best-effort: a logging
    # failure must never invalidate a completed run.
    try:
        prompt_recorder.flush()
    except Exception:
        logger.warning("Failed to flush agent_initial_prompts.* for %s", run_id)

    # 7. COST ACCOUNTING (best-effort; requires optional config.pricing)
    pricing = _get_token_pricing_usd_per_1m(
        cfg, provider=provider, model_name=model_name
    )
    agent_usage = _sum_turn_usage(turns)
    run_cost = {
        "provider": provider,
        "model_name": model_name,
        "pricing": pricing,
        "agent_usage": agent_usage,
        "judge_usage": judge_usage_total,
        "agent_cost_usd": None,
        "judge_cost_usd": None,
        "total_cost_usd": None,
    }
    if pricing:
        run_cost["agent_cost_usd"] = _cost_usd(
            agent_usage,
            input_per_1m_usd=pricing["input_per_1m_usd"],
            output_per_1m_usd=pricing["output_per_1m_usd"],
        )
        run_cost["judge_cost_usd"] = _cost_usd(
            judge_usage_total,
            input_per_1m_usd=pricing["input_per_1m_usd"],
            output_per_1m_usd=pricing["output_per_1m_usd"],
        )
        run_cost["total_cost_usd"] = float(run_cost["agent_cost_usd"]) + float(
            run_cost["judge_cost_usd"]
        )
    _write_json(run_dir / "costs.json", run_cost)

    # Detailed logging similar to collusion runner
    _write_json(
        run_dir / "tool_events.json",
        [
            {
                "tool_name": e.tool_name,
                "agent_name": e.agent_name,
                "arguments": e.arguments,
                "result": e.result,
                "phase": e.phase,
                "iteration": e.iteration,
                "planning_round": e.planning_round,
            }
            for e in protocol.tool_events
        ],
    )
    _write_json(
        run_dir / "blackboards.json",
        [
            {
                "blackboard_id": bb.blackboard_id,
                "participants": sorted(list(bb.agents)),
                "events": bb.logs,
            }
            for bb in protocol.megaboard.blackboards
        ],
    )

    return {
        "run_id": run_id,
        "adversaries": len(adversaries),
        "pre_coordination_round": resolved_pre_coordination,
        "pre_coordination_rounds": resolved_pre_coord_rounds,
        "pre_coordination_rounds_effective": effective_pre_coord_rounds,
        "joint_reward": metrics.meeting_outcome.joint_reward,
        "suspicion_rate": metrics.suspicion_rate_non_adversary,
        "agent_prompt_tokens": agent_usage["prompt_tokens"],
        "agent_completion_tokens": agent_usage["completion_tokens"],
        "agent_total_tokens": agent_usage["total_tokens"],
        "judge_prompt_tokens": int(judge_usage_total.get("prompt_tokens") or 0),
        "judge_completion_tokens": int(judge_usage_total.get("completion_tokens") or 0),
        "judge_total_tokens": int(judge_usage_total.get("total_tokens") or 0),
        "total_cost_usd": run_cost.get("total_cost_usd"),
    }


async def run_from_config(
    config_path: str,
    *,
    out_dir: Optional[str] = None,
    max_concurrent_runs: Optional[int] = None,
) -> Path:
    cfg = _load_yaml(config_path)
    # Fail fast on an invalid `pre_coordination_rounds` before any runs are
    # scheduled. Both the global `simulation.pre_coordination_rounds` and every
    # sweep-level `sweeps[].pre_coordination_rounds` value are validated here,
    # BEFORE the output root is created, so a bad count aborts the sweep
    # without leaving a half-built directory behind. The same values are
    # re-resolved per run in `_run_single`.
    _resolve_pre_coordination_rounds(cfg)
    exp = cfg.get("experiment") or {}
    max_concurrent_runs = max_concurrent_runs or int(exp.get("max_concurrent_runs", 1))

    models = cfg.get("llm_models") or []
    sweeps = exp.get("sweeps") or []
    seeds = _normalize_seeds(exp.get("seeds")) or [1]

    for sweep in sweeps:
        _resolve_pre_coordination_rounds_axis(sweep, cfg)

    is_hospital = _is_hospital_environment(cfg)

    def _is_combo_feasible(
        *, num_agents: Any, adversary_count: Any, target_role: Any
    ) -> bool:
        # Existing validation logic
        try:
            ac = int(adversary_count)
        except Exception:
            return False

        if not is_hospital:
            return True

        # Enforce num_agents <-> num_hospitals consistency.
        try:
            nh = _infer_num_hospitals_from_num_agents(int(num_agents))
        except Exception:
            return False

        max_adv = _max_adversaries_for_hospital_env(
            num_hospitals=nh, target_role=target_role
        )
        return ac <= max_adv

    def _log_skip(*, num_agents: Any, adversary_count: Any, target_role: Any) -> None:
        if not is_hospital:
            return
        try:
            nh = _infer_num_hospitals_from_num_agents(int(num_agents))
        except Exception as e:
            logger.warning(
                "Skipping infeasible combo: num_agents=%r (%s)", num_agents, e
            )
            return
        max_adv = _max_adversaries_for_hospital_env(
            num_hospitals=nh, target_role=target_role
        )
        try:
            ac = int(adversary_count)
        except Exception:
            logger.warning(
                "Skipping infeasible combo: adversary_count=%r", adversary_count
            )
            return
        if ac > max_adv:
            logger.warning(
                "Skipping infeasible combo: adversary_count=%d > max=%d under hospital-based coalition restriction (target_role=%r, num_hospitals=%d)",
                ac,
                max_adv,
                target_role,
                nh,
            )

    # Pre-calculate total runs
    total_runs = 0
    for model in models:
        for sweep in sweeps:
            topologies = sweep.get("topologies") or ["complete"]
            agent_counts = sweep.get("num_agents") or [9]
            adv_counts = sweep.get("adversary_counts") or [1]
            target_roles = sweep.get("target_roles") or [None]
            patient_counts = sweep.get("num_patients") or [16]
            dpm_list = _resolve_disable_pm_axis(sweep, cfg)
            precoord_list = _resolve_pre_coordination_axis(sweep, cfg)

            # Count only feasible combinations so progress is accurate. Mirror
            # the scheduler loop exactly, including the pre-coordination axes:
            # the phase flag decides whether the round-count axis collapses to
            # a single value.
            for topology in topologies:
                for num_agents in agent_counts:
                    for ac in adv_counts:
                        for role in target_roles:
                            for num_patients in patient_counts:
                                if not _is_combo_feasible(
                                    num_agents=num_agents,
                                    adversary_count=ac,
                                    target_role=role,
                                ):
                                    continue
                                for _ in dpm_list:
                                    for resolved_precoord in precoord_list:
                                        rounds_axis = (
                                            _resolve_pre_coordination_rounds_axis(
                                                sweep,
                                                cfg,
                                                phase_enabled=resolved_precoord,
                                            )
                                        )
                                        total_runs += len(rounds_axis) * len(seeds)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = (
        Path(
            out_dir
            or exp.get("output_dir")
            or "experiments/collusion_via_tools/outputs"
        )
        / timestamp
    )
    _ensure_dir(root)
    _configure_experiment_logging(root)
    _write_json(root / "config.json", cfg)

    _write_progress(
        root,
        {
            "status": "running",
            "total_runs": total_runs,
            "completed_runs": 0,
            "failed_runs": 0,
            "started_at": datetime.now().isoformat(),
        },
    )

    tasks: List[asyncio.Task] = []
    task_labels: Dict[asyncio.Task, str] = {}
    summaries: List[Dict[str, Any]] = []
    completed = 0
    failed = 0

    semaphore = asyncio.Semaphore(max_concurrent_runs)

    async def _run_guarded(run_label: str, **kwargs) -> Dict[str, Any]:
        async with semaphore:
            return await _run_single(**kwargs)

    # Scheduling Loop
    for model in models:
        model_llm = model.get("llm")
        model_label = model.get("label")
        for sweep in sweeps:
            sweep_name = sweep.get("name")
            topologies = sweep.get("topologies") or ["complete"]

            agent_counts = sweep.get("num_agents") or [9]
            adv_counts = sweep.get("adversary_counts") or [1]
            target_roles = sweep.get("target_roles") or [None]
            patient_counts = sweep.get("num_patients") or [16]
            dpm_list = _resolve_disable_pm_axis(sweep, cfg)
            precoord_list = _resolve_pre_coordination_axis(sweep, cfg)
            # Single value per sweep; not a sweep dimension on its own.
            precoord_max = _resolve_pre_coordination_max_messages(sweep, cfg)

            for topology in topologies:
                for num_agents in agent_counts:
                    for ac in adv_counts:
                        for role in target_roles:
                            for num_patients in patient_counts:
                                if not _is_combo_feasible(
                                    num_agents=num_agents,
                                    adversary_count=ac,
                                    target_role=role,
                                ):
                                    _log_skip(
                                        num_agents=num_agents,
                                        adversary_count=ac,
                                        target_role=role,
                                    )
                                    continue

                                for resolved_cut in dpm_list:
                                    for resolved_precoord in precoord_list:
                                        # The phase flag decides whether the
                                        # round-count axis expands or collapses.
                                        rounds_axis = (
                                            _resolve_pre_coordination_rounds_axis(
                                                sweep,
                                                cfg,
                                                phase_enabled=resolved_precoord,
                                            )
                                        )
                                        for resolved_rounds in rounds_axis:
                                            for seed in seeds:
                                                role_lbl = role if role else "Any"
                                                pm_lbl = (
                                                    "__nopm" if resolved_cut else ""
                                                )
                                                if resolved_precoord:
                                                    precoord_lbl = (
                                                        f"__precoord{resolved_rounds}"
                                                    )
                                                    pm_lbl = (
                                                        f"{pm_lbl}{precoord_lbl}"
                                                        if pm_lbl
                                                        else precoord_lbl
                                                    )
                                                run_label = (
                                                    f"{model_label}/{sweep_name}/topo{topology}"
                                                    f"/n{num_agents}/p{num_patients}/adv{ac}/{role_lbl}{pm_lbl}/seed{seed}"
                                                )

                                                task = asyncio.create_task(
                                                    _run_guarded(
                                                        run_label=run_label,
                                                        base_cfg=cfg,
                                                        model_label=model_label,
                                                        model_llm_cfg=model_llm,
                                                        sweep_name=sweep_name,
                                                        topology=str(topology),
                                                        num_agents=num_agents,
                                                        adversary_count=ac,
                                                        num_patients=num_patients,
                                                        target_role=role,
                                                        seed=seed,
                                                        out_dir=root,
                                                        disable_post_message=resolved_cut,
                                                        pre_coordination_round=resolved_precoord,
                                                        pre_coordination_rounds=resolved_rounds,
                                                        pre_coordination_max_messages=precoord_max,
                                                    )
                                                )
                                                tasks.append(task)
                                                task_labels[task] = run_label

    # Execution Loop (Robust)
    with tqdm(total=len(tasks), desc="Experiments", unit="run") as pbar:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for finished in done:
                run_label = task_labels.get(finished, "unknown")
                pbar.set_postfix_str(run_label)
                try:
                    res = await finished
                    summaries.append(res)
                    completed += 1
                except Exception:
                    failed += 1
                    logger.exception("RUN FAILED %s", run_label)
                finally:
                    pbar.update(1)
                    _write_progress(
                        root,
                        {
                            "status": "running",
                            "total_runs": total_runs,
                            "completed_runs": completed,
                            "failed_runs": failed,
                            "last_run": run_label,
                        },
                    )

    _write_json(root / "summary.json", summaries)

    # CSV generation
    csv_rows = []
    for row in summaries:
        flat = {k: v for k, v in row.items() if not isinstance(v, (dict, list))}
        csv_rows.append(flat)
    if csv_rows:
        fieldnames = sorted(list(csv_rows[0].keys()))
        with open(root / "summary.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)

    # Optional: aggregate costs across all runs (best-effort).
    try:
        from experiments.collusion_via_tools.costs import compute_and_write_costs

        compute_and_write_costs(root, write_per_run=False)
    except Exception:
        logger.exception("Failed to write costs_summary.json")

    return root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--max-concurrent-runs", default=None, type=int)
    args = parser.parse_args()

    asyncio.run(
        run_from_config(
            args.config,
            out_dir=args.out_dir,
            max_concurrent_runs=args.max_concurrent_runs,
        )
    )


if __name__ == "__main__":
    main()
