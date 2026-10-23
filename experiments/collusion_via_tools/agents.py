from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Set

from terrarium.agents.base import BaseAgent

from experiments.collusion_via_tools.prompt_capture import ThreadSafePromptRecorder


COVERT_STRATEGY = "covert_max_coalition_reward"


@dataclass(frozen=True, slots=True)
class AdversarySpec:
    """Configuration for Misalignment/DoS Adversary."""

    adversary_names: Set[str]


class MisalignmentHospitalAgent(BaseAgent):
    """
    Agent capable of executing Strategic Resource Depletion & Front-Running.
    """

    def __init__(
        self,
        *args: Any,
        adversary_spec: Optional[AdversarySpec] = None,
        prompt_recorder: Optional[ThreadSafePromptRecorder] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.adversary_spec = adversary_spec
        if prompt_recorder is not None:
            self._install_prompt_recorder(prompt_recorder)

    @property
    def is_adversary(self) -> bool:
        spec = self.adversary_spec
        return bool(spec and self.name in spec.adversary_names)

    # ------------------------------------------------------------------
    # Prompt capture
    # ------------------------------------------------------------------

    def _install_prompt_recorder(self, recorder: ThreadSafePromptRecorder) -> None:
        """Wrap ``_multi_step_response_generation`` so that the first system +
        user prompt per phase occurrence is captured into ``recorder``.

        The wrapper is installed on the instance (shadowing the bound method
        on the class). It captures ``system_prompt`` / ``user_prompt`` exactly
        as terrarium hands them to the LLM, and merges them into the returned
        dict under the ``system_prompt`` / ``user_prompt`` keys for first-of-
        phase turns only. The runner then copies those keys into the
        corresponding entry of ``agent_turns.json``.
        """
        original = self._multi_step_response_generation
        agent_self = self
        recorder_ref = recorder

        async def _wrapped(system_prompt: str, user_prompt: str):
            extra = recorder_ref.record_prompts(
                agent_name=agent_self.current_agent_name or agent_self.name,
                phase=agent_self.current_phase or "unknown",
                iteration=agent_self.current_iteration,
                round_num=agent_self.current_round,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            result = await original(
                system_prompt=system_prompt, user_prompt=user_prompt
            )
            if extra is not None:
                # `original` returns the agent response dict; merge in the
                # captured prompts so the runner can pick them up alongside
                # `response` / `usage` / `tools_executed`.
                result["system_prompt"] = extra["system_prompt"]
                result["user_prompt"] = extra["user_prompt"]
            return result

        # Shadow the bound method on the instance. terrarium calls
        # `self._multi_step_response_generation(...)` from
        # `BaseAgent.generate_response`, so the instance attribute wins.
        self._multi_step_response_generation = _wrapped  # type: ignore[method-assign]
