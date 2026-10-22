"""Tests for post_message-restriction awareness in collusion_via_tools.resume.

Stdlib unittest only; no network, no LLM calls, no dependency on repo configs.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import yaml

project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

from experiments.collusion_via_tools import resume as resume_mod  # noqa: E402
from experiments.collusion_via_tools import run as run_mod  # noqa: E402


def _base_cfg(
    *, sweep: Dict[str, Any], exp_extra: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """A minimal, non-hospital config so all axis combinations are feasible."""
    exp: Dict[str, Any] = {
        "seeds": [1],
        "max_concurrent_runs": 1,
        "sweeps": [sweep],
    }
    if exp_extra:
        exp.update(exp_extra)
    return {
        "environment": {"name": "FakeEnv"},
        "llm_models": [
            {"label": "m", "llm": {"provider": "openai", "model": "gpt-test"}}
        ],
        "experiment": exp,
    }


def _sweep(name: str = "s", **overrides: Any) -> Dict[str, Any]:
    sweep: Dict[str, Any] = {
        "name": name,
        "topologies": ["complete"],
        "num_agents": [5],
        "adversary_counts": [1],
        "target_roles": [None],
        "num_patients": [4],
    }
    sweep.update(overrides)
    return sweep


class ResumeRestrictionTests(unittest.TestCase):
    def test_sweep_after_round_list(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(disable_post_message_after_round=[0, 1]),
        )
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 2)
        self.assertEqual(
            [r.endswith("__nopm") for r in run_ids].count(True),
            1,
        )
        self.assertEqual(
            [r.endswith("__cut_after_r1") for r in run_ids].count(True),
            1,
        )
        self.assertTrue(all(r.startswith("m__s__topocomplete__") for r in run_ids))

    def test_legacy_disable_post_message_list(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(disable_post_message=[True, False]),
        )
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 2)
        nopm = [r for r in run_ids if r.endswith("__nopm")]
        plain = [r for r in run_ids if not r.endswith("__nopm")]
        self.assertEqual(len(nopm), 1)
        self.assertEqual(len(plain), 1)
        self.assertFalse(plain[0].endswith("__cut_after_r"))
        self.assertNotIn("__nopm", plain[0])

    def test_top_level_after_round(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(),
            exp_extra={"disable_post_message_after_round": 1},
        )
        run_ids = [spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)]

        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0].endswith("__cut_after_r1"))

    def test_top_level_legacy_bool(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(),
            exp_extra={"disable_post_message": True},
        )
        run_ids = [spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)]

        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0].endswith("__nopm"))

    def test_spec_count_and_ids_match_runner_plan(self) -> None:
        sweep = _sweep(disable_post_message_after_round=[0, 1])
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1, 2]})

        # Run the real planner with `_run_single` stubbed out so no work executes.
        planned: List[Dict[str, Any]] = []

        async def _fake_run_single(**kwargs: Any) -> Dict[str, Any]:
            planned.append(kwargs)
            return {"run_id": "stub"}

        original = run_mod._run_single
        run_mod._run_single = _fake_run_single  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.yaml"
                config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
                asyncio.run(
                    run_mod.run_from_config(
                        str(config_path), out_dir=str(Path(tmp) / "out")
                    )
                )
        finally:
            run_mod._run_single = original  # type: ignore[assignment]

        planned_ids = set()
        for kw in planned:
            planned_ids.add(
                run_mod._build_run_id(
                    model_label=str(kw["model_label"]),
                    sweep_name=str(kw["sweep_name"]),
                    topology=str(kw["topology"]),
                    num_agents=int(kw["num_agents"]),
                    num_patients=int(
                        run_mod._resolve_num_patients_spec(
                            raw=kw["num_patients"], num_agents=int(kw["num_agents"])
                        )
                    ),
                    adversary_count=int(kw["adversary_count"]),
                    target_role=kw["target_role"],
                    seed=int(kw["seed"]),
                    disable_post_message_after_round=kw[
                        "disable_post_message_after_round"
                    ],
                )
            )

        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        self.assertEqual(len(planned_ids), 4)  # 2 restrictions x 2 seeds
        self.assertEqual(spec_ids, planned_ids)


if __name__ == "__main__":
    unittest.main()
