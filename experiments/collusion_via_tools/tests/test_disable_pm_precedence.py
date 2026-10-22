"""Precedence tests for the boolean `disable_post_message` knob.

Stdlib `unittest` only: no new dependencies, no network, no LLM calls. The
counting-pass test stubs out `_run_single` so `run_from_config` exercises only
the sweep expansion / counting / scheduling logic.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import yaml

from experiments.collusion_via_tools import run as run_mod


def _axis(sweep, cfg):
    return run_mod._resolve_disable_pm_axis(sweep, cfg)


def _one(sweep, cfg):
    axis = _axis(sweep, cfg)
    assert len(axis) == 1, axis
    return axis[0]


def _run_id(disable_post_message):
    """The sweep-independent prefix of a run_id, with the given suffix."""
    return run_mod._build_run_id(
        model_label="m",
        sweep_name="s",
        topology="complete",
        num_agents=9,
        num_patients=16,
        adversary_count=1,
        target_role=None,
        seed=1,
        disable_post_message=disable_post_message,
    )


class DisablePmPrecedenceTests(unittest.TestCase):
    def test_unset_everywhere_is_unrestricted(self):
        self.assertIs(_one({}, {}), False)
        self.assertIs(_one({"name": "s"}, None), False)

    def test_top_level_experiment_key(self):
        cfg = {"experiment": {"disable_post_message": True}}

        self.assertIs(_one({}, cfg), True)
        self.assertEqual(_run_id(_one({}, cfg)), _run_id(True))
        self.assertTrue(_run_id(_one({}, cfg)).endswith("__nopm"))

    def test_top_level_adversary_key_lives_under_experiment(self):
        cfg = {"experiment": {"adversary": {"disable_post_message": True}}}

        self.assertIs(_one({}, cfg), True)

    def test_top_level_communication_network_key(self):
        cfg = {"communication_network": {"disable_post_message": True}}

        self.assertIs(_one({}, cfg), True)

    def test_top_level_documented_order(self):
        # `experiment` beats `communication_network` (a present `experiment`
        # value stops the walk even when a lower-precedence `true` disagrees).
        cfg = {
            "experiment": {"disable_post_message": True},
            "communication_network": {"disable_post_message": False},
        }
        self.assertIs(_one({}, cfg), True)

        # `experiment.adversary` beats `communication_network`.
        cfg = {
            "experiment": {"adversary": {"disable_post_message": False}},
            "communication_network": {"disable_post_message": True},
        }
        self.assertIs(_one({}, cfg), False)

        # Fallback all the way to `communication_network`.
        cfg = {"communication_network": {"disable_post_message": True}}
        self.assertIs(_one({}, cfg), True)

    def test_top_level_present_false_stops_the_search(self):
        cfg = {
            "experiment": {"disable_post_message": False},
            "communication_network": {"disable_post_message": True},
        }

        self.assertIs(_one({}, cfg), False)

    def test_top_level_present_null_stops_the_search(self):
        cfg = {
            "experiment": {"disable_post_message": None},
            "communication_network": {"disable_post_message": True},
        }

        self.assertIs(_one({}, cfg), False)

    def test_sweep_false_overrides_top_level_true(self):
        cfg = {"experiment": {"disable_post_message": True}}
        sweep = {"name": "s", "disable_post_message": False}

        dpm = _one(sweep, cfg)

        self.assertIs(dpm, False)
        rid = _run_id(dpm)
        self.assertEqual(rid, _run_id(False))
        self.assertNotIn("__nopm", rid)

    def test_sweep_true_overrides_top_level_false(self):
        cfg = {"experiment": {"disable_post_message": False}}
        sweep = {"name": "s", "disable_post_message": True}

        dpm = _one(sweep, cfg)

        self.assertIs(dpm, True)
        self.assertEqual(_run_id(dpm), _run_id(False) + "__nopm")

    def test_list_axis_is_one_run_per_element(self):
        axis = _axis({"name": "s", "disable_post_message": [True, False]}, {})

        self.assertEqual(axis, [True, False])
        self.assertEqual(
            [_run_id(dpm) for dpm in axis],
            [_run_id(False) + "__nopm", _run_id(False)],
        )

    def test_falsy_list_element_is_honoured_not_backfilled(self):
        # A top-level restriction must not leak into a `false` element.
        cfg = {"experiment": {"disable_post_message": True}}
        axis = _axis({"name": "s", "disable_post_message": [True, False]}, cfg)

        self.assertEqual(axis, [True, False])


class CountingPassTests(unittest.TestCase):
    def test_list_axis_count_scales_with_axis_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            cfg = {
                "llm_models": [
                    {
                        "label": "m",
                        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                    }
                ],
                "environment": {"name": "MeetingSchedulingEnvironment"},
                "communication_network": {"topology": "complete"},
                "experiment": {
                    "seeds": [1, 2, 3],
                    "output_dir": str(out_dir),
                    "max_concurrent_runs": 1,
                    "sweeps": [
                        {
                            "name": "sw",
                            "topologies": ["complete", "ring"],
                            "num_agents": [9, 13],
                            "disable_post_message": [True, False],
                        }
                    ],
                },
            }
            cfg_path = Path(tmp) / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            progress: list = []
            seen_dpm: list = []

            async def _stub(**kwargs):
                seen_dpm.append(kwargs.get("disable_post_message"))
                return {"run_id": "stub"}

            orig_progress = run_mod._write_progress
            orig_single = run_mod._run_single
            run_mod._write_progress = lambda root, data: progress.append(data)
            run_mod._run_single = _stub
            try:
                asyncio.run(
                    run_mod.run_from_config(str(cfg_path), out_dir=str(out_dir))
                )
            finally:
                run_mod._write_progress = orig_progress
                run_mod._run_single = orig_single

            axis_len = 2
            feasible = 2 * 2  # topologies x num_agents (non-hospital: all feasible)
            expected = axis_len * feasible * len(cfg["experiment"]["seeds"])

            self.assertEqual(progress[0]["total_runs"], expected)
            self.assertEqual(progress[0]["total_runs"], len(seen_dpm))
            self.assertEqual(
                seen_dpm.count(True), feasible * len(cfg["experiment"]["seeds"])
            )
            self.assertEqual(
                seen_dpm.count(False), feasible * len(cfg["experiment"]["seeds"])
            )


if __name__ == "__main__":
    unittest.main()
