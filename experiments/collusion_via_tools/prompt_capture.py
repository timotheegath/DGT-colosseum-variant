"""Thread-safe recorder for the initial system/user prompt each agent receives
per phase occurrence.

The recorder is attached to :class:`MisalignmentHospitalAgent` instances and is
populated from inside the agent's LLM call wrapper. Each captured entry holds
the agent name, phase, iteration, round, and the verbatim system + user prompts
that the agent would have been sent by terrarium's
``BaseAgent._multi_step_response_generation``.

Scope (intentional):
    Only the FIRST turn of each phase occurrence is recorded. The runner asks
    for the prompts that started each phase (``pre_coordination``,
    ``planning`` round 1, ``execution``, ``survey``), not the (potentially
    many) subsequent planning rounds. The filter is implemented in
    :meth:`ThreadSafePromptRecorder.record_prompts` and is the single
    authoritative rule.

Output:
    On flush, two files are written into the run directory:

    - ``agent_initial_prompts.json``: the structured list, one entry per
      recorded turn (machine-readable, matches the schema of
      ``agent_turns.json`` minus the response/usage/tool fields).
    - ``agent_initial_prompts.md``: a human-readable rendering grouped by
      phase.

Thread safety:
    ``BaseAgent._multi_step_response_generation`` calls into a blocking LLM
    client via ``run_blocking``, so capture may happen on a worker thread or
    on the event loop depending on the call site. The internal list is
    guarded by a :class:`threading.Lock`; the public methods are safe to call
    from any thread.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ThreadSafePromptRecorder:
    """Records the first system+user prompt per (phase, iteration, round) tuple.

    The recorder is per-run. Multiple concurrent runs each hold their own
    instance; they never share state.
    """

    # Phases where ``round`` is meaningful. ``planning`` has multiple rounds
    # per iteration; only round 1 (the first one) is the start of the phase.
    _MULTI_ROUND_PHASES = frozenset({"planning"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Capture (called from the agent wrapper, possibly off the event loop)
    # ------------------------------------------------------------------

    @classmethod
    def is_first_of_phase(cls, phase: str, round_num: Optional[int]) -> bool:
        """Return True iff this turn is the FIRST turn of its phase occurrence.

        ``round_num`` is the terrarium planning round (1-indexed for planning,
        0 for execution / survey / pre_coordination). ``None`` is treated as
        "first".
        """
        if phase in cls._MULTI_ROUND_PHASES:
            return round_num in (None, 0, 1)
        return True

    def record_prompts(
        self,
        *,
        agent_name: str,
        phase: str,
        iteration: Optional[int],
        round_num: Optional[int],
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[Dict[str, str]]:
        """Record the prompts for this turn.

        Returns a dict ``{"system_prompt": ..., "user_prompt": ...}`` to merge
        into the agent response dict IF this turn is a first-of-phase turn;
        otherwise returns ``None`` (and records nothing).

        The dict is returned (not just stored) so the wrapper can attach the
        prompts to the immediate ``agent_turns.json`` entry without having to
        look the record back up by index.
        """
        if not self.is_first_of_phase(phase, round_num):
            return None

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_name": agent_name,
            "phase": phase,
            "iteration": iteration,
            "round": round_num,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }
        with self._lock:
            self._entries.append(entry)

        return {"system_prompt": system_prompt, "user_prompt": user_prompt}

    # ------------------------------------------------------------------
    # Read-only inspection
    # ------------------------------------------------------------------

    @property
    def entries(self) -> List[Dict[str, Any]]:
        """Return a snapshot of the recorded entries (safe shallow copy)."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------------
    # Persist
    # ------------------------------------------------------------------

    def write(
        self,
        run_dir: Path,
        *,
        environment_name: str = "Unknown",
        seed: int = 0,
    ) -> None:
        """Write the structured JSON and a human-readable Markdown log.

        Both files are written from a single in-memory snapshot, so concurrent
        writes from other threads during the flush are not an issue.
        """
        snapshot = self.entries
        run_dir.mkdir(parents=True, exist_ok=True)

        json_path = run_dir / "agent_initial_prompts.json"
        md_path = run_dir / "agent_initial_prompts.md"

        with json_path.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

        with md_path.open("w", encoding="utf-8") as f:
            f.write(
                f"# Initial Agent Prompts — {environment_name} (Seed: {seed})\n\n"
                f"Recorded: {len(snapshot)} first-of-phase turns.\n\n"
            )
            for entry in snapshot:
                f.write(_render_markdown_entry(entry))
                f.write("\n---\n\n")


def _render_markdown_entry(entry: Dict[str, Any]) -> str:
    agent = entry.get("agent_name") or "?"
    phase = entry.get("phase") or "?"
    iteration = entry.get("iteration")
    round_num = entry.get("round")

    meta_bits = [f"**Phase:** {phase}"]
    if iteration is not None:
        meta_bits.append(f"**Iteration:** {iteration}")
    if round_num is not None:
        meta_bits.append(f"**Round:** {round_num}")
    meta = " | ".join(meta_bits)

    return (
        f"## {agent} — {meta}\n\n"
        "### System Prompt\n"
        "```\n"
        f"{entry.get('system_prompt', '')}\n"
        "```\n\n"
        "### User Prompt\n"
        "```\n"
        f"{entry.get('user_prompt', '')}\n"
        "```\n"
    )
