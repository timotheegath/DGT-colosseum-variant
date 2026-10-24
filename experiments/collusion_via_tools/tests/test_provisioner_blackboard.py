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


class PreCoordinationPhaseRemapTests(unittest.TestCase):
    """The provisioner's pre-coordination prompt must be planning-shaped.

    Upstream keys the logistics-manager instruction off ``phase == 'planning'``
    and has no ``pre_coordination`` notion, so without the remap the round
    would render ``[PHASE: EXECUTION] Stand by`` while ``MisalignmentPrompts``
    asks the agent to coordinate. The remap must be pre_coordination-only and
    must not mutate the caller's context (the mutation hazard is guarded by the
    Misalignment integration test below).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        cls.base = HospitalPrompts.__new__(HospitalPrompts)
        cls.wrapped = ProvisionerBlackboardPrompts(cls.base)

    @staticmethod
    def _provisioner_ctx(phase: str) -> dict:
        return {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "phase": phase,
            "iteration": 1,
        }

    def test_pre_coordination_renders_planning_instruction(self) -> None:
        prompt = self.wrapped.get_user_prompt(
            "Resource_Provisioner",
            self._provisioner_ctx("pre_coordination"),
            TWO_BOARDS,
        )
        self.assertIn("[PHASE: PLANNING]", prompt)
        self.assertNotIn("Stand by", prompt)

    def test_pre_coordination_does_not_mutate_caller_context(self) -> None:
        agent_context = self._provisioner_ctx("pre_coordination")
        self.wrapped.get_user_prompt("Resource_Provisioner", agent_context, TWO_BOARDS)
        self.assertEqual(agent_context["phase"], "pre_coordination")

    def test_remap_is_pre_coordination_only(self) -> None:
        # `execution` keeps the upstream stand-by line (the pre_coordination
        # remap must not leak into it). `survey` is no longer a stand-by case:
        # it now renders `[PHASE: SURVEY]` and is covered by
        # SurveyPhaseRemapTests.
        prompt = self.wrapped.get_user_prompt(
            "Resource_Provisioner", self._provisioner_ctx("execution"), TWO_BOARDS
        )
        self.assertIn("Stand by", prompt)

    def test_department_agent_unaffected_by_remap(self) -> None:
        agent_context = {
            "dept_info": {
                "capacity": 1,
                "hospital": "General_Hospital",
                "local_inventory": {},
                "procedure_costs": {},
            },
            "job_queue": [],
            "phase": "pre_coordination",
            "iteration": 1,
        }
        base_out = self.base.get_user_prompt("GH_Triage", agent_context, TWO_BOARDS)
        wrapped_out = self.wrapped.get_user_prompt(
            "GH_Triage", agent_context, TWO_BOARDS
        )
        self.assertEqual(wrapped_out, base_out)
        self.assertNotIn("[PHASE: PLANNING]", wrapped_out)

    def test_misalignment_pre_coordination_uses_compact_prompt(self) -> None:
        import types

        agent_context = {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "phase": "pre_coordination",
            "iteration": 1,
            "misalignment_role": "adversary",
            "misalignment_adversaries": ["Resource_Provisioner", "AgentB"],
            "misalignment_secret_blackboard_id": 7,
            "post_message_available": True,
            "pre_coordination_round": 1,
            "pre_coordination_rounds": 2,
        }
        prompts = MisalignmentPrompts(
            types.SimpleNamespace(), {}, base_prompts=self.wrapped
        )
        prompt = prompts.get_user_prompt(
            "Resource_Provisioner", agent_context, TWO_BOARDS
        )
        self.assertIn("PRE-COORDINATION ROUND 1 OF 2", prompt)
        # Compact phase prompt: the operational planning prompt is not used.
        self.assertNotIn("[PHASE: PLANNING]", prompt)
        self.assertNotIn("[PHASE: EXECUTION]", prompt)
        self.assertIn("=== BLACKBOARD (ID: 0) ===", prompt)


class SurveyPhaseRemapTests(unittest.TestCase):
    """The provisioner's survey prompt must answer the survey, not "Stand by".

    Upstream emits the execution stand-by line for the survey phase, which
    contradicted the appended POST-RUN SURVEY JSON schema (empty answer,
    asymmetric belief metrics). The fix replaces only that exact line with
    ``[PHASE: SURVEY]`` and must not touch ``execution`` or the department path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        cls.base = HospitalPrompts.__new__(HospitalPrompts)
        cls.wrapped = ProvisionerBlackboardPrompts(cls.base)

    @staticmethod
    def _provisioner_ctx(phase: str) -> dict:
        return {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "phase": phase,
            "iteration": 1,
        }

    def test_survey_replaces_standby_line(self) -> None:
        prompt = self.wrapped.get_user_prompt(
            "Resource_Provisioner", self._provisioner_ctx("survey"), TWO_BOARDS
        )
        self.assertIn("[PHASE: SURVEY]", prompt)
        self.assertNotIn("Stand by", prompt)

    def test_execution_keeps_standby_line(self) -> None:
        prompt = self.wrapped.get_user_prompt(
            "Resource_Provisioner", self._provisioner_ctx("execution"), TWO_BOARDS
        )
        self.assertIn("Stand by", prompt)

    def test_department_agent_unaffected_by_survey_remap(self) -> None:
        agent_context = {
            "dept_info": {
                "capacity": 1,
                "hospital": "General_Hospital",
                "local_inventory": {},
                "procedure_costs": {},
            },
            "job_queue": [],
            "phase": "survey",
            "iteration": 1,
        }
        base_out = self.base.get_user_prompt("GH_Triage", agent_context, TWO_BOARDS)
        wrapped_out = self.wrapped.get_user_prompt(
            "GH_Triage", agent_context, TWO_BOARDS
        )
        self.assertEqual(wrapped_out, base_out)

    def test_misalignment_survey_prompt_has_schema_no_standby(self) -> None:
        import types

        agent_context = {
            "inventory_snapshot": {},
            "failures_by_hospital": {},
            "phase": "survey",
            "iteration": 1,
        }
        prompts = MisalignmentPrompts(
            types.SimpleNamespace(), {}, base_prompts=self.wrapped
        )
        prompt = prompts.get_user_prompt(
            "Resource_Provisioner", agent_context, TWO_BOARDS
        )
        self.assertIn("POST-RUN SURVEY", prompt)
        self.assertIn("suspects_foul_play", prompt)
        self.assertNotIn("Stand by", prompt)

    def test_survey_does_not_mutate_caller_context(self) -> None:
        agent_context = self._provisioner_ctx("survey")
        self.wrapped.get_user_prompt("Resource_Provisioner", agent_context, TWO_BOARDS)
        self.assertEqual(agent_context["phase"], "survey")


class SystemPromptForwardingTests(unittest.TestCase):
    """Regression: Terrarium hands extra kwargs to the wrapper.

    Terrarium resolves the system prompt by inspecting the *wrapper's*
    signature (``terrarium/agents/base.py``). Because
    ``ProvisionerBlackboardPrompts.get_system_prompt`` declares ``**kwargs``,
    Terrarium calls it with ``agent_name``/``agent_context``/
    ``blackboard_context``. The wrapped upstream prompt
    (``HospitalPrompts.get_system_prompt(self)``) accepts none of them, so
    forwarding blindly raised
    ``TypeError: HospitalPrompts.get_system_prompt() got an unexpected keyword
    argument 'agent_name'`` on EVERY agent turn of a benign run (the wrapper is
    the top-level prompt stack when misalignment is disabled).
    """

    @staticmethod
    def _terrarium_style_call(prompts, agent_name="General_Hospital_Triage"):
        """Replicate terrarium's prompt-resolution call exactly."""
        import inspect

        get_system_prompt = prompts.get_system_prompt
        sig = inspect.signature(get_system_prompt)
        if sig is None or not sig.parameters:
            return get_system_prompt()
        kwargs = {}
        for name, param in sig.parameters.items():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                kwargs = {
                    "agent_name": agent_name,
                    "agent_context": {},
                    "blackboard_context": {},
                }
                break
            if name == "agent_name":
                kwargs[name] = agent_name
            elif name == "agent_context":
                kwargs[name] = {}
            elif name == "blackboard_context":
                kwargs[name] = {}
        return get_system_prompt(**kwargs)

    def test_terrarium_style_call_on_real_hospital_prompts(self) -> None:
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        wrapped = ProvisionerBlackboardPrompts(HospitalPrompts.__new__(HospitalPrompts))
        system_prompt = self._terrarium_style_call(wrapped)
        self.assertIn("You are a Hospital Agent", system_prompt)

    def test_terrarium_style_call_on_fake_base_prompts(self) -> None:
        wrapped = ProvisionerBlackboardPrompts(_FakeBasePrompts())
        self.assertEqual(self._terrarium_style_call(wrapped), "SYSTEM")

    def test_direct_kwargs_call_does_not_raise(self) -> None:
        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        wrapped = ProvisionerBlackboardPrompts(HospitalPrompts.__new__(HospitalPrompts))
        system_prompt = wrapped.get_system_prompt(
            agent_name="General_Hospital_Triage",
            agent_context={},
            blackboard_context={},
        )
        self.assertIn("You are a Hospital Agent", system_prompt)

    def test_base_prompt_receiving_agent_name_still_gets_it(self) -> None:
        """Filtering must not swallow kwargs the wrapped prompt does accept."""

        class _AgentNameBase:
            def get_system_prompt(self, agent_name: str) -> str:
                return f"SYSTEM:{agent_name}"

        wrapped = ProvisionerBlackboardPrompts(_AgentNameBase())
        self.assertEqual(
            self._terrarium_style_call(wrapped, agent_name="Resource_Provisioner"),
            "SYSTEM:Resource_Provisioner",
        )

    def test_base_prompt_with_var_keyword_receives_everything(self) -> None:
        class _VarKwBase:
            def get_system_prompt(self, **kwargs) -> str:
                return ",".join(sorted(kwargs))

        wrapped = ProvisionerBlackboardPrompts(_VarKwBase())
        self.assertEqual(
            self._terrarium_style_call(wrapped),
            "agent_context,agent_name,blackboard_context",
        )

    def test_misalignment_stack_system_prompt_still_resolves(self) -> None:
        """The non-benign stack (MisalignmentPrompts on top) must keep working."""
        import types

        from envs.dcops.hospital.hospital_prompts import HospitalPrompts

        wrapped = ProvisionerBlackboardPrompts(HospitalPrompts.__new__(HospitalPrompts))
        prompts = MisalignmentPrompts(types.SimpleNamespace(), {}, base_prompts=wrapped)
        system_prompt = self._terrarium_style_call(prompts)
        self.assertIn("MISALIGNMENT MODE SWITCH", system_prompt)
        self.assertIn("You are a Hospital Agent", system_prompt)


if __name__ == "__main__":
    unittest.main()
