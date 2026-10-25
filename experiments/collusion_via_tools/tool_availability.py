"""Phase-aware tool availability for the Hospital DCOP prompts.

Terrarium decides which tools an agent can call *per turn* by asking its
``ToolsetDiscovery`` for two lists and concatenating them
(``terrarium/agents/base.py:324-333``)::

    env_tools = self.toolset_discovery.get_tools_for_environment(
        self.environment_name, self.current_phase
    )
    blackboard_tools = self.toolset_discovery.get_tools_for_blackboard(
        self.current_phase
    )
    tool_set = env_tools + blackboard_tools

The toolset is therefore *phase dependent*, but the prompts historically never
said so -- they advertised ``schedule_patient`` as a signalling carrier during
planning (where the upstream gate withholds it) and never listed what was
callable at all. This module exposes the same computation as a pair of pure
helpers so the runner can hand the prompt layer the exact per-phase toolset.

Two phase gates exist:

* ``schedule_patient`` is offered by ``HospitalTools.get_tools`` only when
  ``phase == "execution"`` (``envs/dcops/hospital/hospital_tools.py``).
* ``post_message`` is offered by ``ToolsetDiscovery.get_tools_for_blackboard``
  only for the planning phase; the repo patch in
  ``experiments/common/_patch_terrarium.py`` returns ``[]`` when
  ``is_post_message_disabled()`` is true and remaps ``pre_coordination`` ->
  ``planning`` so the secret window keeps the tool.

``PHASE_ENV_TOOLS_BLOCKED`` records the one phase where the *schemas* are still
offered but every environment tool *call* is rejected at runtime: the
pre-coordination round (``set_env_tools_blocked(True)`` at ``run.py:900``;
``LocalCommunicationProtocol.environment_handle_tool_call`` returns
``{"blocked": True, ...}`` for each env-tool invocation during that phase).
``post_message`` is not blocked by that gate -- only environment tools are.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Phases in which environment tool CALLS are rejected at runtime even though
# their schemas are offered to the model. See the module docstring.
PHASE_ENV_TOOLS_BLOCKED: Tuple[str, ...] = ("pre_coordination",)

# The phases the runner walks the agents through, in prompt-relevant order.
# ``phase_tool_map`` preserves this order so the rendered "it is available in:
# ..." lists are stable.
_PHASES: Tuple[str, ...] = ("planning", "execution", "survey", "pre_coordination")

# Fallback environment toolset, used only when the agent exposes no
# ``toolset_discovery`` (or the discovery object yields nothing usable).
# Mirrors ``envs.dcops.hospital.hospital_tools.HospitalTools.get_tools``:
# four always-on tools plus ``schedule_patient`` in the execution phase.
_FALLBACK_ENV_TOOLS_ALWAYS: Tuple[str, ...] = (
    "find_available_slots",
    "get_job_queue",
    "transfer_resources",
    "broadcast_message",
)
_FALLBACK_ENV_TOOLS_EXECUTION_ONLY: Tuple[str, ...] = ("schedule_patient",)


def _extract_names(tools: Any) -> List[str]:
    """Pull ``tool["function"]["name"]`` out of a toolset list, defensively."""
    names: List[str] = []
    for tool in tools or []:
        try:
            name = tool["function"]["name"]
        except (TypeError, KeyError, IndexError):
            continue
        if name:
            names.append(str(name))
    return names


def _fallback_env_tool_names(phase: str) -> List[str]:
    names = list(_FALLBACK_ENV_TOOLS_ALWAYS)
    if phase == "execution":
        names.extend(_FALLBACK_ENV_TOOLS_EXECUTION_ONLY)
    return names


def _fallback_blackboard_tool_names(phase: str) -> List[str]:
    """Fallback blackboard toolset.

    Upstream offers ``post_message`` only for the planning phase and the repo
    patch remaps ``pre_coordination`` -> ``planning``. This fallback cannot see
    the ``is_post_message_disabled()`` flag, so it assumes the tool is enabled;
    it is only reached when the real discovery object is unavailable.
    """
    if phase in ("planning", "pre_coordination"):
        return ["post_message"]
    return []


def _fallback_tool_names(phase: str) -> List[str]:
    return _fallback_env_tool_names(phase) + _fallback_blackboard_tool_names(phase)


def tool_names_for_phase(agent: Any, env: Any, phase: str) -> List[str]:
    """Return the exact tool names ``agent`` can call in ``phase``.

    Computed the same way ``terrarium/agents/base.py`` does it: the environment
    toolset (for ``env``) followed by the blackboard toolset, both for ``phase``.
    The blackboard lookup goes through the patched
    ``ToolsetDiscovery.get_tools_for_blackboard``, so it honours the *current*
    ``is_post_message_disabled()`` state -- callers must therefore compute this
    **after** setting that flag for the agent/phase.

    Order is preserved (environment tools first, then blackboard tools). If the
    agent has no ``toolset_discovery`` -- or the discovery object yields no
    tools at all, which happens when an environment subclass is not registered
    with ``terrarium.environment_tools`` -- this falls back to the known Hospital
    toolset for ``phase``.
    """
    discovery = getattr(agent, "toolset_discovery", None)
    if discovery is not None:
        env_name = ""
        if env is not None:
            env_name = (
                getattr(env, "tools_environment_name", None) or type(env).__name__ or ""
            )
        else:
            env_name = getattr(agent, "environment_name", "") or ""

        try:
            env_tools = discovery.get_tools_for_environment(env_name, phase)
        except Exception:
            env_tools = []
        try:
            blackboard_tools = discovery.get_tools_for_blackboard(phase)
        except Exception:
            blackboard_tools = []

        names = _extract_names(env_tools) + _extract_names(blackboard_tools)
        if names:
            return names
        # Discovery produced nothing usable; fall through to the known toolset.

    return _fallback_tool_names(phase)


def phase_tool_map(agent: Any, env: Any) -> Dict[str, List[str]]:
    """Map every phase to the tool names offered in it.

    Uses the *current* ``is_post_message_disabled()`` state for ``post_message``
    (the flag is process/task-local, not per-phase), so the same map is valid
    for the phase the caller is about to render. Keys follow ``_PHASES`` order.
    """
    return {phase: tool_names_for_phase(agent, env, phase) for phase in _PHASES}
