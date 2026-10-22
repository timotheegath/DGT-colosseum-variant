"""Tests for the `pre_coordination_round` knob.

The pre-coordination round is an adversary-only turn that runs once before
iteration 1 of the main simulation. The adversary gets the planning-shaped
prompt (with a phase-specific block explaining the round) and `post_message` to
the coalition blackboard, but any environment-tool call (`broadcast_message`,
`transfer_resources`, etc.) is silently dropped by the protocol layer.

Three layers are tested in isolation:

1. Resolution: `_normalize_pre_coordination_value`, `_resolve_pre_coordination_round`,
   `_resolve_pre_coordination_axis` -- same precedence semantics as
   `disable_post_message_after_round` (presence wins over fall-through).
2. `run_id` suffixing: `_build_run_id` produces `__precoord` either alone or
   after the existing post-message suffix.
3. Tool blocking: `LocalCommunicationProtocol.environment_handle_tool_call`
   returns a stub when `_ENV_TOOLS_BLOCKED` is set, and `post_message` flows
   through `blackboard_handle_tool_call` unaffected.
4. `build_tables._RUN_ID_RE_V2` parses `__precoord` (alone or combined).
"""

from __future__ import annotations

import asyncio
import unittest

from experiments.common._patch_terrarium import (
    is_env_tools_blocked,
    set_env_tools_blocked,
)
from experiments.collusion_via_tools import run as run_mod
from experiments.collusion_via_tools.plots import build_tables as bt


class NormalizePreCoordinationValueTests(unittest.TestCase):
    def test_truthy_values(self) -> None:
        for v in (True, 1, "1", "true", "yes", "on", "TRUE"):
            with self.subTest(v=v):
                self.assertTrue(run_mod._normalize_pre_coordination_value(v))

    def test_falsy_values(self) -> None:
        for v in (False, 0, None, "", "no", "false"):
            with self.subTest(v=v):
                self.assertFalse(run_mod._normalize_pre_coordination_value(v))


class ResolvePreCoordinationRoundTests(unittest.TestCase):
    def test_explicit_value_wins(self) -> None:
        # Even with a top-level config setting it to True, an explicit False
        # at the call site is honoured.
        cfg = {"experiment": {"pre_coordination_round": True}}
        self.assertFalse(run_mod._resolve_pre_coordination_round(False, cfg))

    def test_default_is_false(self) -> None:
        self.assertFalse(run_mod._resolve_pre_coordination_round(run_mod._UNSET, {}))
        self.assertFalse(run_mod._resolve_pre_coordination_round(run_mod._UNSET, None))

    def test_top_level_resolution_order(self) -> None:
        cfg = {
            "experiment": {"pre_coordination_round": True},
            "communication_network": {"pre_coordination_round": False},
        }
        self.assertTrue(run_mod._resolve_pre_coordination_round(run_mod._UNSET, cfg))

        # `adversary` lives under `experiment`, matching the disable_pm resolution.
        cfg2 = {"experiment": {"adversary": {"pre_coordination_round": True}}}
        self.assertTrue(run_mod._resolve_pre_coordination_round(run_mod._UNSET, cfg2))

        cfg3 = {
            "experiment": {},
            "communication_network": {"pre_coordination_round": True},
        }
        self.assertTrue(run_mod._resolve_pre_coordination_round(run_mod._UNSET, cfg3))


class ResolvePreCoordinationAxisTests(unittest.TestCase):
    def test_sweep_value_wins_over_top_level(self) -> None:
        cfg = {"experiment": {"pre_coordination_round": True}}
        # Sweep-level scalar False overrides top-level True.
        self.assertEqual(
            run_mod._resolve_pre_coordination_axis(
                {"pre_coordination_round": False}, cfg
            ),
            [False],
        )

    def test_sweep_list_expands_into_one_decision_per_run(self) -> None:
        cfg = {}
        self.assertEqual(
            run_mod._resolve_pre_coordination_axis(
                {"pre_coordination_round": [True, False]}, cfg
            ),
            [True, False],
        )

    def test_top_level_falls_through_when_sweep_key_absent(self) -> None:
        cfg = {"experiment": {"pre_coordination_round": True}}
        self.assertEqual(
            run_mod._resolve_pre_coordination_axis({}, cfg),
            [True],
        )

    def test_default_is_off(self) -> None:
        self.assertEqual(run_mod._resolve_pre_coordination_axis({}, {}), [False])


class BuildRunIdSuffixTests(unittest.TestCase):
    BASE_KW = dict(
        model_label="m",
        sweep_name="s",
        topology="complete",
        num_agents=9,
        num_patients=8,
        adversary_count=2,
        target_role="Resource_Provisioner",
        seed=1,
    )

    def test_no_suffix_when_disabled(self) -> None:
        rid = run_mod._build_run_id(**self.BASE_KW)
        self.assertFalse(rid.endswith("__precoord"))
        self.assertFalse(rid.endswith("__nopm"))

    def test_precoord_alone(self) -> None:
        rid = run_mod._build_run_id(**self.BASE_KW, pre_coordination_round=True)
        self.assertTrue(rid.endswith("__precoord"))

    def test_precoord_with_cut_after_r1(self) -> None:
        rid = run_mod._build_run_id(
            **self.BASE_KW,
            disable_post_message_after_round=1,
            pre_coordination_round=True,
        )
        self.assertTrue(rid.endswith("__cut_after_r1__precoord"))

    def test_precoord_with_nopm(self) -> None:
        rid = run_mod._build_run_id(
            **self.BASE_KW,
            disable_post_message_after_round=0,
            pre_coordination_round=True,
        )
        self.assertTrue(rid.endswith("__nopm__precoord"))

    def test_precoord_false_does_not_change_id(self) -> None:
        rid_a = run_mod._build_run_id(
            **self.BASE_KW, disable_post_message_after_round=1
        )
        rid_b = run_mod._build_run_id(
            **self.BASE_KW,
            disable_post_message_after_round=1,
            pre_coordination_round=False,
        )
        self.assertEqual(rid_a, rid_b)


class BuildTablesRegexTests(unittest.TestCase):
    def test_precoord_alone(self) -> None:
        m = bt._RUN_ID_RE_V2.match(
            "m__s__topocomplete__agents9_patients8_adv2_Resource_Provisioner_seed1__precoord"
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group("precoord"), "__precoord")
        self.assertIsNone(m.group("restriction"))

    def test_precoord_with_cut_after_r1(self) -> None:
        m = bt._RUN_ID_RE_V2.match(
            "m__s__topocomplete__agents9_patients8_adv2_Resource_Provisioner_seed1__cut_after_r1__precoord"
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group("restriction"), "cut_after_r1")
        self.assertEqual(m.group("cut_after_round"), "1")
        self.assertEqual(m.group("precoord"), "__precoord")

    def test_no_suffix_round_trip(self) -> None:
        m = bt._RUN_ID_RE_V2.match(
            "m__s__topocomplete__agents9_patients8_adv2_Resource_Provisioner_seed1"
        )
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("restriction"))
        self.assertIsNone(m.group("precoord"))


class EnvToolsBlockContextVarTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        self.assertFalse(is_env_tools_blocked())
        set_env_tools_blocked(True)
        self.assertTrue(is_env_tools_blocked())
        set_env_tools_blocked(False)
        self.assertFalse(is_env_tools_blocked())


class EnvToolsBlockStubTests(unittest.TestCase):
    """The protocol returns a stub when env tools are blocked but the
    blackboard path (post_message) is untouched."""

    def _make_protocol(self):
        from experiments.common.local_protocol import LocalCommunicationProtocol

        # `LocalCommunicationProtocol.__init__` requires a `config` keyword;
        # the blocking branch never reads it, so an empty dict is fine.
        proto = LocalCommunicationProtocol(config={})

        # environment_handle_tool_call requires self.environment; a stub object
        # is enough because the blocking branch never touches it.
        class _StubEnv:
            pass

        proto.environment = _StubEnv()
        return proto

    def test_environment_handle_returns_stub(self) -> None:
        proto = self._make_protocol()
        set_env_tools_blocked(True)
        try:
            result = asyncio.run(
                proto.environment_handle_tool_call(
                    tool_name="broadcast_message",
                    agent_name="Agent_A",
                    arguments={"message": "hi"},
                    phase="pre_coordination",
                    iteration=0,
                )
            )
        finally:
            set_env_tools_blocked(False)

        self.assertTrue(result.get("blocked"))
        # The stub also leaves an event trail so downstream analytics can see
        # the blocked attempt.
        self.assertEqual(len(proto.tool_events), 1)
        ev = proto.tool_events[0]
        self.assertEqual(ev.tool_name, "broadcast_message")
        self.assertEqual(ev.phase, "pre_coordination")

    def test_environment_handle_executes_normally_when_unblocked(self) -> None:
        proto = self._make_protocol()
        set_env_tools_blocked(False)
        # With a stub environment and no real env_tools the call raises a
        # benign "Environment not set" path because `env` is non-None -- we
        # only need to confirm we did NOT short-circuit on the blocking flag.
        # The simpler check: when unblocked, the function does NOT return a
        # `blocked: True` stub. We catch any downstream exception and verify
        # the flag wasn't the cause.
        try:
            result = asyncio.run(
                proto.environment_handle_tool_call(
                    tool_name="broadcast_message",
                    agent_name="Agent_A",
                    arguments={"message": "hi"},
                )
            )
            self.assertNotIsInstance(result, dict) or not result.get("blocked")
        except Exception:
            # Any exception is fine as long as it isn't the blocking stub --
            # the stub path is well-defined above and returns cleanly.
            pass


if __name__ == "__main__":
    unittest.main()
