"""Prompt-capture regression tests.

Pure stdlib ``unittest``: no network, no LLM calls, no environment
construction. These exercise the recorder's "first-of-phase only" filter, the
thread-safe store, the file flush, and the ``run._extract_initial_prompts``
helper that hoists captured prompts into ``agent_turns.json`` entries.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from experiments.collusion_via_tools.prompt_capture import ThreadSafePromptRecorder
from experiments.collusion_via_tools.agents import MisalignmentHospitalAgent
from experiments.collusion_via_tools import run as run_mod


class FirstOfPhaseFilterTests(unittest.TestCase):
    def test_planning_round1_is_first_of_phase(self) -> None:
        self.assertTrue(ThreadSafePromptRecorder.is_first_of_phase("planning", 1))
        self.assertTrue(ThreadSafePromptRecorder.is_first_of_phase("planning", 0))
        self.assertTrue(ThreadSafePromptRecorder.is_first_of_phase("planning", None))

    def test_planning_later_rounds_are_not_first_of_phase(self) -> None:
        self.assertFalse(ThreadSafePromptRecorder.is_first_of_phase("planning", 2))
        self.assertFalse(ThreadSafePromptRecorder.is_first_of_phase("planning", 5))

    def test_other_phases_always_first_of_phase(self) -> None:
        for phase in ("pre_coordination", "execution", "survey"):
            self.assertTrue(ThreadSafePromptRecorder.is_first_of_phase(phase, 0), phase)
            self.assertTrue(
                ThreadSafePromptRecorder.is_first_of_phase(phase, None), phase
            )


class RecordPromptsTests(unittest.TestCase):
    def test_first_of_phase_returns_payload_and_stores(self) -> None:
        rec = ThreadSafePromptRecorder()
        out = rec.record_prompts(
            agent_name="A",
            phase="planning",
            iteration=1,
            round_num=1,
            system_prompt="SYS",
            user_prompt="USR",
        )
        self.assertEqual(out, {"system_prompt": "SYS", "user_prompt": "USR"})
        self.assertEqual(len(rec), 1)
        entry = rec.entries[0]
        self.assertEqual(entry["agent_name"], "A")
        self.assertEqual(entry["system_prompt"], "SYS")
        self.assertEqual(entry["user_prompt"], "USR")

    def test_later_planning_round_returns_none_and_is_not_stored(self) -> None:
        rec = ThreadSafePromptRecorder()
        out = rec.record_prompts(
            agent_name="A",
            phase="planning",
            iteration=1,
            round_num=2,
            system_prompt="SYS",
            user_prompt="USR",
        )
        self.assertIsNone(out)
        self.assertEqual(len(rec), 0)


class FlushTests(unittest.TestCase):
    def test_write_emits_json_and_markdown(self) -> None:
        rec = ThreadSafePromptRecorder()
        rec.record_prompts(
            agent_name="A",
            phase="survey",
            iteration=3,
            round_num=0,
            system_prompt="SYS-S",
            user_prompt="USR-S",
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            rec.write(run_dir, environment_name="Env", seed=7)
            json_path = run_dir / "agent_initial_prompts.json"
            md_path = run_dir / "agent_initial_prompts.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["system_prompt"], "SYS-S")
            self.assertEqual(data[0]["user_prompt"], "USR-S")

            md = md_path.read_text(encoding="utf-8")
            self.assertIn("SYS-S", md)
            self.assertIn("USR-S", md)
            self.assertIn("survey", md)


class ExtractInitialPromptsTests(unittest.TestCase):
    def test_extracts_present_prompts(self) -> None:
        resp = {"system_prompt": "S", "user_prompt": "U", "response": "x"}
        self.assertEqual(
            run_mod._extract_initial_prompts(resp),
            {"system_prompt": "S", "user_prompt": "U"},
        )

    def test_returns_empty_when_absent(self) -> None:
        self.assertEqual(run_mod._extract_initial_prompts({"response": "x"}), {})
        self.assertEqual(run_mod._extract_initial_prompts({"system_prompt": "S"}), {})
        self.assertEqual(
            run_mod._extract_initial_prompts(
                {"system_prompt": None, "user_prompt": "U"}
            ),
            {},
        )


class _FakeAgent:
    """Minimal stand-in mirroring the parts of ``BaseAgent`` the recorder
    wrapper touches, so the interception contract can be tested without
    constructing a real client or issuing an LLM call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.current_agent_name = None
        self.current_phase = None
        self.current_iteration = None
        self.current_round = None
        self.calls = []

    async def _multi_step_response_generation(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return {"response": "ok", "usage": {}, "tools_executed": 0}


class PromptWrapperInterceptionTests(unittest.TestCase):
    """``_install_prompt_recorder`` must shadow the bound
    ``_multi_step_response_generation`` and surface prompts on the returned
    dict exactly the way ``BaseAgent.generate_response`` invokes it
    (keyword arguments)."""

    def _agent(self):
        agent = _FakeAgent("AgentA")
        recorder = ThreadSafePromptRecorder()
        # Bind the real production method onto the fake agent.
        MisalignmentHospitalAgent._install_prompt_recorder(agent, recorder)
        agent.recorder = recorder
        return agent

    def test_captures_first_of_phase_and_preserves_payload(self) -> None:
        agent = self._agent()
        agent.current_agent_name = "AgentA"
        agent.current_phase = "execution"
        agent.current_iteration = 2
        agent.current_round = 0

        result = asyncio.run(
            agent._multi_step_response_generation(
                system_prompt="SYS", user_prompt="USR"
            )
        )
        # Original return contract is preserved.
        self.assertEqual(result["response"], "ok")
        # Prompts are surfaced for the runner to hoist.
        self.assertEqual(result["system_prompt"], "SYS")
        self.assertEqual(result["user_prompt"], "USR")
        self.assertEqual(len(agent.recorder), 1)
        entry = agent.recorder.entries[0]
        self.assertEqual(entry["phase"], "execution")
        self.assertEqual(entry["agent_name"], "AgentA")

    def test_later_planning_round_is_passed_through_untouched(self) -> None:
        agent = self._agent()
        agent.current_agent_name = "AgentA"
        agent.current_phase = "planning"
        agent.current_iteration = 1
        agent.current_round = 3

        result = asyncio.run(
            agent._multi_step_response_generation(
                system_prompt="SYS", user_prompt="USR"
            )
        )
        # No prompt keys added, nothing recorded.
        self.assertNotIn("system_prompt", result)
        self.assertNotIn("user_prompt", result)
        self.assertEqual(len(agent.recorder), 0)
        # The underlying generation still ran.
        self.assertEqual(agent.calls, [("SYS", "USR")])


if __name__ == "__main__":
    unittest.main()
