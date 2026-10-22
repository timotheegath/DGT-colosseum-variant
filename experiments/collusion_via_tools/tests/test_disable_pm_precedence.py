"""Precedence tests for the `disable_post_message_after_round` knob.

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


def _run_id(cut):
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
        disable_post_message_after_round=cut,
    )


class DisablePmPrecedenceTests(unittest.TestCase):
    def test_sweep_false_overrides_top_level_cut(self):
        cfg = {"experiment": {"disable_post_message_after_round": 1}}
        sweep = {"name": "s", "disable_post_message_after_round": False}

        cut = _one(sweep, cfg)

        self.assertIsNone(cut)
        rid = _run_id(cut)
        self.assertEqual(rid, _run_id(None))
        self.assertNotIn("__nopm", rid)
        self.assertNotIn("__cut_after_r", rid)

    def test_sweep_zero_overrides_top_level_true(self):
        cfg = {"experiment": {"disable_post_message_after_round": True}}
        sweep = {"name": "s", "disable_post_message_after_round": 0}

        cut = _one(sweep, cfg)

        self.assertEqual(cut, 0)
        self.assertEqual(_run_id(cut), _run_id(None) + "__nopm")

    def test_no_sweep_axis_uses_top_level_cut(self):
        cfg = {"experiment": {"disable_post_message_after_round": 1}}

        cut = _one({"name": "s"}, cfg)

        self.assertEqual(cut, 1)
        self.assertEqual(_run_id(cut), _run_id(None) + "__cut_after_r1")

    def test_list_axis_is_one_run_per_element(self):
        axis = _axis(
            {"name": "s", "disable_post_message_after_round": [None, 0, 1]}, {}
        )

        self.assertEqual(axis, [None, 0, 1])
        self.assertEqual(
            [_run_id(cut) for cut in axis],
            [_run_id(None), _run_id(None) + "__nopm", _run_id(None) + "__cut_after_r1"],
        )

    def test_falsy_list_element_is_honoured_not_backfilled(self):
        # A top-level restriction must not leak into `null` / `false` elements.
        cfg = {"experiment": {"disable_post_message_after_round": 1}}
        axis = _axis(
            {"name": "s", "disable_post_message_after_round": [None, False, 1]}, cfg
        )

        self.assertEqual(axis, [None, None, 1])

    def test_top_level_false_with_sweep_one(self):
        cfg = {"experiment": {"disable_post_message": False}}
        sweep = {"name": "s", "disable_post_message_after_round": 1}

        cut = _one(sweep, cfg)

        self.assertEqual(cut, 1)
        self.assertEqual(_run_id(cut), _run_id(None) + "__cut_after_r1")

    def test_sweep_after_round_key_beats_legacy_sweep_key(self):
        sweep = {
            "name": "s",
            "disable_post_message_after_round": False,
            "disable_post_message": True,
        }

        self.assertIsNone(_one(sweep, {}))

    def test_sweep_legacy_key_used_when_after_round_absent(self):
        sweep = {"name": "s", "disable_post_message": 0}

        self.assertEqual(_one(sweep, {}), 0)

    def test_top_level_present_null_stops_the_search(self):
        cfg = {
            "experiment": {
                "disable_post_message_after_round": None,
                "disable_post_message": True,
            }
        }

        self.assertIsNone(_one({}, cfg))

    def test_top_level_documented_order(self):
        cfg = {
            "experiment": {
                "disable_post_message_after_round": 1,
                "adversary": {"disable_post_message_after_round": 2},
                "disable_post_message": True,
            },
            "communication_network": {"disable_post_message_after_round": 3},
        }

        self.assertEqual(_one({}, cfg), 1)

        without_exp = {
            "experiment": {"disable_post_message": True},
            "communication_network": {"disable_post_message_after_round": 3},
        }
        self.assertEqual(_one({}, without_exp), 3)

    def test_unset_everywhere_is_unrestricted(self):
        self.assertIsNone(_one({}, {}))
        self.assertIsNone(_one({"name": "s"}, None))


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
                            "disable_post_message_after_round": [None, 0, 1],
                        }
                    ],
                },
            }
            cfg_path = Path(tmp) / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            progress: list = []
            seen_cuts: list = []

            async def _stub(**kwargs):
                seen_cuts.append(kwargs.get("disable_post_message_after_round"))
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

            axis_len = 3
            feasible = 2 * 2  # topologies x num_agents (non-hospital: all feasible)
            expected = axis_len * feasible * len(cfg["experiment"]["seeds"])

            self.assertEqual(progress[0]["total_runs"], expected)
            self.assertEqual(progress[0]["total_runs"], len(seen_cuts))
            self.assertEqual(
                seen_cuts.count(None), feasible * len(cfg["experiment"]["seeds"])
            )
            self.assertEqual(
                seen_cuts.count(0), feasible * len(cfg["experiment"]["seeds"])
            )
            self.assertEqual(
                seen_cuts.count(1), feasible * len(cfg["experiment"]["seeds"])
            )


if __name__ == "__main__":
    unittest.main()
