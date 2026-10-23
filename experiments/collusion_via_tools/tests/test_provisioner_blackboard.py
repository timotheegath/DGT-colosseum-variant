"""Tests for ``ProvisionerBlackboardPrompts``.

Upstream ``HospitalPrompts.get_user_prompt`` returns early for
``Resource_Provisioner`` and never renders ``blackboard_context``, so the
provisioner is told "Check the Blackboard for URGENT or DEFICIT requests" but
is blind to the public board AND the coalition-only (secret) board. These
tests pin the wrapper that injects the blackboard section into the provisioner
prompt without changing any other agent's prompt.

Pure stdlib ``unittest``: no network, no LLM, no environment construction.
"""

from __future__ import annotations

import unittest

from experiments.collusion_via_tools.prompts import (
    MisalignmentPrompts,
    ProvisionerBlackboardPrompts,
    _format_blackboard_section,
)

PROVISIONER_TEXT = (
    "\n=== AGENT STATUS: Resource_Provisioner\n"
    "Role: Logistics Provisioner\n"
    "\n[PHASE: PLANNING]\n"
    "Do the thing.\n"
)


class _FakeBasePrompts:
    """Minimal stand-in for ``HospitalPrompts`` (provisioner template only)."""

    def __init__(self, text: str = PROVISIONER_TEXT) -> None:
        self._text = text

    def get_system_prompt(self) -> str:
        return "SYSTEM"

    def get_user_prompt(self, agent_name, agent_context, blackboard_context) -> str:
        return self._text


TWO_BOARDS = {0: "PUBLIC board body", 1: "SECRET board body"}


class ProvisionerSeesBlackboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _FakeBasePrompts()
        self.wrapped = ProvisionerBlackboardPrompts(self.base)

    def test_provisioner_prompt_contains_both_boards(self) -> None:
        prompt = self.wrapped.get_user_prompt("Resource_Provisioner", {}, TWO_BOARDS)
        self.assertIn("=== BLACKBOARD (ID: 0) ===", prompt)
        self.assertIn("[0]", prompt)
        self.assertIn("[1]", prompt)
        self.assertIn("PUBLIC board body", prompt)
        self.assertIn("SECRET board body", prompt)

    def test_department_prompt_is_byte_identical(self) -> None:
        base_out = self.base.get_user_prompt("General_Hospital_Triage", {}, TWO_BOARDS)
        wrapped_out = self.wrapped.get_user_prompt(
            "General_Hospital_Triage", {}, TWO_BOARDS
        )
        self.assertEqual(wrapped_out, base_out)
        self.assertNotIn("=== BLACKBOARD (ID:", wrapped_out)

    def test_empty_blackboard_renders_no_new_messages(self) -> None:
        prompt = self.wrapped.get_user_prompt("Resource_Provisioner", {}, {})
        self.assertIn("(No new messages)", prompt)

    def test_existing_blackboard_not_duplicated(self) -> None:
        text = PROVISIONER_TEXT + "=== BLACKBOARD (ID: 3) ===\n[3]\nalready here\n"
        wrapped = ProvisionerBlackboardPrompts(_FakeBasePrompts(text))
        prompt = wrapped.get_user_prompt("Resource_Provisioner", {}, TWO_BOARDS)
        self.assertEqual(prompt.count("=== BLACKBOARD (ID:"), 1)
        self.assertIn("already here", prompt)

    def test_ordering_status_then_blackboard_then_phase(self) -> None:
        prompt = self.wrapped.get_user_prompt("Resource_Provisioner", {}, TWO_BOARDS)
        role_idx = prompt.index("Role: Logistics Provisioner")
        bb_idx = prompt.index("=== BLACKBOARD (ID: 0) ===")
        phase_idx = prompt.index("[PHASE: PLANNING]")
        self.assertLess(role_idx, bb_idx)
        self.assertLess(bb_idx, phase_idx)


class FormatterTests(unittest.TestCase):
    def test_header_and_bodies(self) -> None:
        section = _format_blackboard_section(TWO_BOARDS)
        self.assertTrue(section.startswith("=== BLACKBOARD (ID: 0) ===\n"))
        self.assertIn("[0]\nPUBLIC board body\n", section)
        self.assertIn("[1]\nSECRET board body\n", section)

    def test_empty_fallback(self) -> None:
        self.assertEqual(
            _format_blackboard_section({}),
            "=== BLACKBOARD (ID: 0) ===\n(No new messages)\n",
        )


class RealUpstreamIntegrationTests(unittest.TestCase):
    """Uses the real ``HospitalPrompts`` without running its ``__init__``."""

    @classmethod
    def setUpClass(cls) -> None:
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        cls.base = HospitalPrompts.__new__(HospitalPrompts)
        cls.wrapped = ProvisionerBlackboardPrompts(cls.base)

    def test_real_provisioner_gets_blackboard(self) -> None:
        agent_context = {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "cost_table": [],
            "phase": "planning",
        }
        prompt = self.wrapped.get_user_prompt(
            "Resource_Provisioner", agent_context, TWO_BOARDS
        )
        self.assertIn("PUBLIC board body", prompt)
        self.assertIn("SECRET board body", prompt)
        self.assertIn("=== BLACKBOARD (ID: 0) ===", prompt)

    def test_real_department_prompt_not_double_rendered(self) -> None:
        agent_context = {
            "dept_info": {
                "capacity": 1,
                "hospital": "General_Hospital",
                "local_inventory": {},
                "procedure_costs": {},
            },
            "job_queue": [],
            "phase": "planning",
            "iteration": 1,
        }
        prompt = self.wrapped.get_user_prompt(
            "General_Hospital_Triage", agent_context, TWO_BOARDS
        )
        self.assertEqual(prompt.count("=== BLACKBOARD (ID:"), 1)
        self.assertIn("PUBLIC board body", prompt)
        self.assertIn("SECRET board body", prompt)


class MisalignmentStackTests(unittest.TestCase):
    def test_adversary_provisioner_gets_secret_block_and_blackboard(self) -> None:
        import types

        wrapped = ProvisionerBlackboardPrompts(_FakeBasePrompts())
        prompts = MisalignmentPrompts(types.SimpleNamespace(), {}, base_prompts=wrapped)
        agent_context = {
            "misalignment_role": "adversary",
            "misalignment_adversaries": ["Resource_Provisioner", "AgentB"],
            "misalignment_secret_blackboard_id": 7,
            "post_message_available": True,
            "phase": "planning",
        }
        prompt = prompts.get_user_prompt(
            "Resource_Provisioner", agent_context, TWO_BOARDS
        )
        self.assertIn("=== SECRET COALITION INSTRUCTIONS", prompt)
        self.assertIn("=== BLACKBOARD (ID: 0) ===", prompt)
        self.assertIn("PUBLIC board body", prompt)
        self.assertIn("SECRET board body", prompt)


class ParticipationScopedVisibilityTests(unittest.TestCase):
    """End-to-end membership scoping against a real ``Megaboard``.

    Invariant: the provisioner always sees the shared board, and sees the
    coalition-only board only when it is a participant (coalition member).

    Why this matters: ``ProvisionerBlackboardPrompts`` renders whatever
    ``blackboard_context`` it is handed, so the secrecy guarantee lives in the
    prefetch layer's membership filter (``Megaboard.get_agent_blackboards``),
    NOT in the wrapper. This test pins both layers together, so a future change
    to either one -- leaking the secret board, or dropping the shared board --
    fails loudly.
    """

    PUBLIC_MARKER = "PUBLIC-MARKER-IV_Kits-SURPLUS"
    SECRET_MARKER = "SECRET-MARKER-SIGNALING-SCHEME"

    def _build_megaboard(self):
        from terrarium.blackboard import Megaboard

        megaboard = Megaboard()
        # Board 0: everyone (including the provisioner).
        megaboard.add_blackboard(
            ["Resource_Provisioner", "GH_Triage", "GH_Ward", "StM_Triage"], {}
        )
        # Board 1: coalition-only; RP deliberately NOT a participant.
        megaboard.add_blackboard(
            ["GH_Triage", "StM_Triage"],
            {"secret_channel": True, "visibility": "secret"},
        )
        megaboard.post(0, "GH_Ward", "message", payload={"message": self.PUBLIC_MARKER})
        megaboard.post(
            1, "GH_Triage", "message", payload={"message": self.SECRET_MARKER}
        )
        return megaboard

    def _prefetch(self, megaboard, agent_name):
        # Use the REAL protocol method so we cannot drift from the runner:
        # `LocalCommunicationProtocol._prefetch_blackboard_events`
        # (experiments/common/local_protocol.py:126-154) does
        # `get_agent_blackboards(agent)` then `megaboard.get(bb_id, agent)`.
        import asyncio

        from experiments.common.local_protocol import LocalCommunicationProtocol

        protocol = LocalCommunicationProtocol(config={}, megaboard=megaboard)
        return asyncio.run(
            protocol._prefetch_blackboard_events(
                agent_name, phase="planning", iteration=1
            )
        )

    def test_secret_blackboard_visibility_is_participation_scoped(self) -> None:
        """The provisioner always sees the shared board; the coalition-only
        board is visible only to participants (coalition members)."""
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        wrapped = ProvisionerBlackboardPrompts(HospitalPrompts.__new__(HospitalPrompts))
        megaboard = self._build_megaboard()
        agent_context = {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "phase": "planning",
            "iteration": 1,
        }

        cases = {
            "Resource_Provisioner": {"boards": ["0"], "is_coalition": False},
            "GH_Triage": {"boards": ["0", "1"], "is_coalition": True},
        }
        for agent_name, expected in cases.items():
            with self.subTest(agent=agent_name):
                context = self._prefetch(megaboard, agent_name)
                self.assertEqual(list(context.keys()), expected["boards"])
                prompt = wrapped.get_user_prompt(agent_name, agent_context, context)
                self.assertIn(self.PUBLIC_MARKER, prompt)
                if expected["is_coalition"]:
                    self.assertIn(self.SECRET_MARKER, prompt)
                else:
                    self.assertNotIn(self.SECRET_MARKER, prompt)


if __name__ == "__main__":
    unittest.main()
