"""Tests for the deterministic agent turn order (`experiment.turn_order`).

Pure stdlib ``unittest``: no network, no LLM. The ``fixed`` order is checked
against a real (minimally-configured) ``HospitalEnvironment`` so it reflects the
shipped default two-hospital layout; the global RNG is saved/restored around
construction so other tests are unaffected.
"""

from __future__ import annotations

import random
import types
import unittest

from envs.dcops.hospital.hospital_env import HospitalEnvironment

from experiments.collusion_via_tools.run import (
    _fixed_agent_order,
    _ordered_agents,
    _resolve_turn_order,
)


def _make_env(num_hospitals: int = 2) -> HospitalEnvironment:
    """Construct a minimal real env, restoring the global RNG afterwards."""
    state = random.getstate()
    try:
        cfg = {
            "environment": {"num_hospitals": num_hospitals, "num_patients": 8},
            "simulation": {"seed": 0},
        }
        proto = types.SimpleNamespace(environment=None)
        return HospitalEnvironment(proto, cfg, tool_logger=None)
    finally:
        random.setstate(state)


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class TurnOrderResolutionTests(unittest.TestCase):
    def test_default_is_shuffled(self) -> None:
        self.assertEqual(_resolve_turn_order({}), "shuffled")
        self.assertEqual(_resolve_turn_order({"experiment": {}}), "shuffled")
        self.assertEqual(_resolve_turn_order(None), "shuffled")

    def test_explicit_modes(self) -> None:
        for mode in ("shuffled", "fixed", "seeded"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    _resolve_turn_order({"experiment": {"turn_order": mode}}), mode
                )

    def test_case_insensitive_and_none(self) -> None:
        self.assertEqual(
            _resolve_turn_order({"experiment": {"turn_order": "FIXED"}}), "fixed"
        )
        self.assertEqual(
            _resolve_turn_order({"experiment": {"turn_order": None}}), "shuffled"
        )

    def test_unknown_value_raises_naming_allowed(self) -> None:
        with self.assertRaises(ValueError) as cm:
            _resolve_turn_order({"experiment": {"turn_order": "random"}})
        msg = str(cm.exception)
        for token in ("experiment.turn_order", "shuffled", "fixed", "seeded"):
            self.assertIn(token, msg)
        self.assertIn("random", msg)


class FixedOrderTests(unittest.TestCase):
    def test_default_two_hospital_order(self) -> None:
        env = _make_env(2)
        order = _fixed_agent_order(env, env.get_agent_names())
        self.assertEqual(
            order,
            [
                "Resource_Provisioner",
                "General_Hospital_Triage",
                "St_Marys_Center_Triage",
                "St_Marys_Center_Radiology",
                "General_Hospital_Radiology",
                "General_Hospital_Surgery",
                "St_Marys_Center_Surgery",
                "St_Marys_Center_Ward",
                "General_Hospital_Ward",
            ],
        )

    def test_every_agent_appears_exactly_once(self) -> None:
        env = _make_env(2)
        names = env.get_agent_names()
        order = _fixed_agent_order(env, names)
        self.assertEqual(len(order), len(names))
        self.assertEqual(set(order), set(names))

    def test_provisioner_is_first(self) -> None:
        env = _make_env(2)
        order = _fixed_agent_order(env, env.get_agent_names())
        self.assertEqual(order[0], "Resource_Provisioner")

    def test_leading_hospital_alternates_by_service(self) -> None:
        env = _make_env(2)
        order = _fixed_agent_order(env, env.get_agent_names())
        self.assertLess(
            order.index("General_Hospital_Triage"),
            order.index("St_Marys_Center_Triage"),
        )
        self.assertLess(
            order.index("St_Marys_Center_Radiology"),
            order.index("General_Hospital_Radiology"),
        )
        self.assertLess(
            order.index("General_Hospital_Surgery"),
            order.index("St_Marys_Center_Surgery"),
        )
        self.assertLess(
            order.index("St_Marys_Center_Ward"),
            order.index("General_Hospital_Ward"),
        )

    def test_scales_to_three_hospitals(self) -> None:
        env = _make_env(3)
        names = env.get_agent_names()
        order = _fixed_agent_order(env, names)
        self.assertEqual(len(order), len(names))
        self.assertEqual(set(order), set(names))
        self.assertEqual(order[0], "Resource_Provisioner")

    def test_uncategorised_agents_appended_deterministically(self) -> None:
        class _Env:
            hospital_names = ["H_A", "H_B"]
            possible_pathways = [[("Triage", 1), ("Ward", 2)]]
            agents_map = {
                "H_A_Triage": {"service": "Triage", "hospital": "H_A"},
                "H_B_Triage": {"service": "Triage", "hospital": "H_B"},
                "H_A_Ward": {"service": "Ward", "hospital": "H_A"},
                "H_B_Ward": {"service": "Ward", "hospital": "H_B"},
                "Zebra_Bot": {"service": "Special", "hospital": "H_A"},
                "Alpha_Bot": {"service": "Special", "hospital": "H_A"},
            }

        names = list(_Env.agents_map)
        order = _fixed_agent_order(_Env(), names)
        self.assertEqual(set(order), set(names))
        self.assertEqual(len(order), len(names))
        # No provisioner in this fake env; the two uncategorised agents land at
        # the end in sorted order.
        self.assertEqual(order[-2:], ["Alpha_Bot", "Zebra_Bot"])

    def test_service_order_derived_from_pathways(self) -> None:
        # A pathway list whose first appearance differs from the default order
        # must drive the emitted service order.
        class _Env:
            hospital_names = ["H_A"]
            possible_pathways = [[("Ward", 1), ("Triage", 1)]]
            agents_map = {
                "H_A_Ward": {"service": "Ward", "hospital": "H_A"},
                "H_A_Triage": {"service": "Triage", "hospital": "H_A"},
            }

        order = _fixed_agent_order(_Env(), ["H_A_Ward", "H_A_Triage"])
        self.assertEqual(order, ["H_A_Ward", "H_A_Triage"])


class OrderedAgentsTests(unittest.TestCase):
    def _agents(self) -> list:
        return [_Agent(n) for n in ("A", "B", "C", "D", "E")]

    def test_seeded_is_deterministic_for_a_seed(self) -> None:
        first = [a.name for a in _ordered_agents(None, self._agents(), "seeded", 7)]
        second = [a.name for a in _ordered_agents(None, self._agents(), "seeded", 7)]
        self.assertEqual(first, second)

    def test_seeded_is_a_function_of_the_seed(self) -> None:
        orders = {
            tuple(a.name for a in _ordered_agents(None, self._agents(), "seeded", s))
            for s in range(20)
        }
        self.assertGreater(len(orders), 1)

    def test_shuffled_preserves_membership(self) -> None:
        out = _ordered_agents(None, self._agents(), "shuffled", 0)
        self.assertCountEqual([a.name for a in out], ["A", "B", "C", "D", "E"])

    def test_fixed_sorts_by_deterministic_order(self) -> None:
        class _Env:
            hospital_names = ["H_A", "H_B"]
            possible_pathways = [[("Triage", 1)]]
            agents_map = {
                "H_A_Triage": {"service": "Triage", "hospital": "H_A"},
                "H_B_Triage": {"service": "Triage", "hospital": "H_B"},
                "Resource_Provisioner": {
                    "service": "Provisioning",
                    "hospital": "Global_Logistics",
                },
            }

        agents = [
            _Agent("H_B_Triage"),
            _Agent("H_A_Triage"),
            _Agent("Resource_Provisioner"),
        ]
        out = _ordered_agents(_Env(), agents, "fixed", 0)
        self.assertEqual(
            [a.name for a in out],
            ["Resource_Provisioner", "H_A_Triage", "H_B_Triage"],
        )


if __name__ == "__main__":
    unittest.main()
