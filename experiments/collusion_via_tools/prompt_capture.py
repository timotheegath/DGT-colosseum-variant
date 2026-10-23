"""Thread-safe recorder for the initial system/user prompt each agent receives
per phase occurrence.

The recorder is attached to :class:`MisalignmentHospitalAgent` instances and is
populated from inside the agent's LLM call wrapper. Each captured entry holds
the agent name, phase, iteration, round, and the verbatim system + user prompts
that the agent would have been sent by terrarium's
``BaseAgent._multi_step_response_generation``.

Scope (intentional):
    Only the FIRST turn of a multi-round phase occurrence is recorded for
    ``planning`` (round 1), not its subsequent planning rounds. Every
    ``pre_coordination`` round is captured, because each round has a
    different "round N of M" prompt and the coalition's later turns build on
    earlier ones. ``execution`` and ``survey`` are single-turn phases. The
    filter is implemented in :meth:`ThreadSafePromptRecorder.record_prompts`
    and is the single authoritative rule.

Output (streamed during the run, not at the end):
    Both files are created as soon as the recorder is constructed — i.e. before
    the first agent takes a turn — and appended to as turns complete:

    - ``agent_initial_prompts.jsonl``: one JSON object per line, appended and
      flushed on every captured turn. Line-oriented so it stays valid under
      appends and survives a crash mid-run.
    - ``agent_initial_prompts.md``: human-readable rendering, appended live.

    Streaming matters because the judge pass (``judge_beliefs.judge_run``) runs
    a second full LLM sweep *after* the simulation loop and before the other
    artifacts are written. If it fails, or the process is killed, prompts
    captured during the completed simulation are already safely on disk.
    ``run.py`` still flushes at the end to guarantee files exist even for runs
    that capture nothing.

Thread safety:
    ``BaseAgent._multi_step_response_generation`` calls into a blocking LLM
    client via ``run_blocking``, so capture may happen on a worker thread or
    on the event loop depending on the call site. The in-memory list AND the
    file appends are guarded by a single :class:`threading.Lock`, so entries
    land in the same order in both.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ThreadSafePromptRecorder:
    """Records the system+user prompt each agent receives per captured turn.

    The recorder is per-run. Multiple concurrent runs each hold their own
    instance and write to their own run directory; they never share state.
    """

    # Phases where ``round`` is meaningful. ``planning`` has multiple rounds
    # per iteration; only round 1 is the start of the phase. ``pre_coordination``
    # is deliberately absent: each of its rounds is a distinct prompt (current
    # round / total), so all rounds are captured.
    _MULTI_ROUND_PHASES = frozenset({"planning"})

    def __init__(
        self,
        run_dir: Optional[Path] = None,
        *,
        environment_name: str = "Unknown",
        seed: int = 0,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        self._environment_name = environment_name
        self._seed = seed

        # Create the files up front so they exist for the whole run, even
        # before the first agent speaks (and even if no prompt is captured).
        self.run_dir: Optional[Path] = None
        self.jsonl_path: Optional[Path] = None
        self.md_path: Optional[Path] = None
        if run_dir is not None:
            self.attach(run_dir, environment_name=environment_name, seed=seed)

    def attach(
        self,
        run_dir: Path,
        *,
        environment_name: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> None:
        """Create (truncate) the output files and prepare for streaming writes.

        Called once at run start. ``mode="w"`` means a re-run of the same
        ``run_id`` (e.g. via ``resume.py``) starts from a clean file rather
        than appending to a stale one.
        """
        if environment_name is not None:
            self._environment_name = environment_name
        if seed is not None:
            self._seed = seed

        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self.run_dir = run_dir
            self.jsonl_path = run_dir / "agent_initial_prompts.jsonl"
            self.md_path = run_dir / "agent_initial_prompts.md"

            # Truncate both files so a fresh run starts clean.
            self.jsonl_path.write_text("", encoding="utf-8")
            self.md_path.write_text(
                f"# Initial Agent Prompts — {self._environment_name} "
                f"(Seed: {self._seed})\n\n"
                "One entry per first-of-phase turn, appended live during the run.\n\n",
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Capture (called from the agent wrapper, possibly off the event loop)
    # ------------------------------------------------------------------

    @classmethod
    def is_first_of_phase(cls, phase: str, round_num: Optional[int]) -> bool:
        """Return True iff this turn is the FIRST turn of its phase occurrence.

        ``round_num`` is the 1-indexed round number for multi-turn phases
        (planning rounds and the pre-coordination phase); ``0`` for execution /
        survey. ``None`` is treated as "first". Only ``planning`` is filtered
        to round 1; every pre-coordination round is captured.
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
        """Record the prompts for this turn, streaming them to disk immediately.

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
            self._append_to_disk(entry)

        return {"system_prompt": system_prompt, "user_prompt": user_prompt}

    # ------------------------------------------------------------------
    # Disk streaming (caller must hold the lock)
    # ------------------------------------------------------------------

    def _append_to_disk(self, entry: Dict[str, Any]) -> None:
        """Append one entry to the JSONL and Markdown files.

        Caller must hold ``self._lock``. Each file is opened/closed per entry
        so the write is flushed immediately — a crash after this point still
        leaves the entry on disk. Cost is negligible: one entry per agent per
        phase, i.e. a few dozen small writes per run.
        """
        # Best-effort per file so a single failure cannot corrupt the other's
        # state or propagate up into the agent call path.
        if self.jsonl_path is not None:
            try:
                with self.jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if self.md_path is not None:
            try:
                with self.md_path.open("a", encoding="utf-8") as f:
                    f.write(_render_markdown_entry(entry))
                    f.write("\n---\n\n")
            except OSError:
                pass

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
    # End-of-run safety net
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """No-op when the recorder is already attached to a run directory.

        Streaming already wrote every entry during the run. This exists so
        ``run.py`` can guarantee the two files exist for runs that captured
        nothing (e.g. an immediate environment failure before any turn). When
        unattached, it creates empty files in ``run_dir``; when attached, it
        does nothing.
        """
        if self.run_dir is not None:
            # Already created + streamed; nothing to reconcile.
            return


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
