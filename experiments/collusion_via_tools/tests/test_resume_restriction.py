"""Tests for post_message-restriction awareness in collusion_via_tools.resume.

The restriction is a plain boolean now (``disable_post_message``); the old
``disable_post_message_after_round`` integer axis is gone. The key invariant
these tests protect: resume must reproduce exactly the run_ids the runner
would create, including when a sweep-level ``false``/``null`` overrides a
top-level ``true`` (presence wins, not truthiness).

Stdlib unittest only; no network, no LLM calls, no dependency on repo configs.
"""

from __future__ import annotations

import asyncio
import json
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


def _run_from_config_ids(cfg: Dict[str, Any]) -> List[str]:
    """Run the real planner with `_run_single` stubbed out, returning planned ids.

    Mirrors `run_from_config`'s own run-id construction so the returned ids are
    exactly the ones the runner would create on disk.
    """
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

    return [
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
            disable_post_message=kw["disable_post_message"],
            pre_coordination_round=kw["pre_coordination_round"],
            pre_coordination_rounds=kw["pre_coordination_rounds"],
        )
        for kw in planned
    ]


def _run_from_config_ids_and_progress(
    cfg: Dict[str, Any],
) -> tuple[List[str], Dict[str, Any]]:
    """Like `_run_from_config_ids`, but also return the written `progress.json`."""
    planned: List[Dict[str, Any]] = []

    async def _fake_run_single(**kwargs: Any) -> Dict[str, Any]:
        planned.append(kwargs)
        return {"run_id": "stub"}

    original = run_mod._run_single
    run_mod._run_single = _fake_run_single  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            root = asyncio.run(
                run_mod.run_from_config(str(config_path), out_dir=str(out_dir))
            )
            progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
    finally:
        run_mod._run_single = original  # type: ignore[assignment]

    ids = [
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
            disable_post_message=kw["disable_post_message"],
            pre_coordination_round=kw["pre_coordination_round"],
            pre_coordination_rounds=kw["pre_coordination_rounds"],
        )
        for kw in planned
    ]
    return ids, {"progress": progress, "planned": len(planned)}


class ResumeRestrictionTests(unittest.TestCase):
    def test_sweep_boolean_list(self) -> None:
        cfg = _base_cfg(sweep=_sweep(disable_post_message=[True, False]))
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 2)
        self.assertEqual(sum(r.endswith("__nopm") for r in run_ids), 1)
        self.assertEqual(
            sum(r.endswith("__nopm") or "__nopm" not in r for r in run_ids),
            2,
        )
        self.assertTrue(all(r.startswith("m__s__topocomplete__") for r in run_ids))

    def test_top_level_boolean(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(),
            exp_extra={"disable_post_message": True},
        )
        run_ids = [spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)]

        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0].endswith("__nopm"))

    def test_sweep_false_overrides_top_level_true(self) -> None:
        # A PRESENT sweep-level `false`/`null` must win over a top-level
        # `true`: it is an honest "unrestricted for this sweep", producing an
        # unsuffixed run_id. Re-resolving the value against the config would
        # instead yield `__nopm` and mis-match the runner.
        for falsy in (False, None):
            with self.subTest(sweep_value=falsy):
                cfg = _base_cfg(
                    sweep=_sweep(disable_post_message=falsy),
                    exp_extra={"disable_post_message": True},
                )
                run_ids = [
                    spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
                ]

                self.assertEqual(len(run_ids), 1)
                self.assertFalse("__nopm" in run_ids[0])
                self.assertEqual(run_ids, _run_from_config_ids(cfg))

    def test_sweep_axis_true_false_distinct_ids(self) -> None:
        cfg = _base_cfg(sweep=_sweep(disable_post_message=[True, False]))
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 2)
        self.assertEqual(len(set(run_ids)), 2)
        self.assertEqual(sum(r.endswith("__nopm") for r in run_ids), 1)

    def test_precoord_axis_combines_with_nopm(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(disable_post_message=[True], pre_coordination_round=[True])
        )
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0].endswith("__nopm__precoord1"))

    def test_precoord_axis_alone(self) -> None:
        cfg = _base_cfg(
            sweep=_sweep(disable_post_message=[False], pre_coordination_round=[True])
        )
        run_ids = sorted(
            spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)
        )

        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0].endswith("__precoord1"))
        self.assertFalse("__nopm" in run_ids[0])

    def test_spec_count_and_ids_match_runner_plan(self) -> None:
        sweep = _sweep(disable_post_message=[True, False])
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1, 2]})

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        self.assertEqual(len(planned_ids), 4)  # 2 restrictions x 2 seeds
        self.assertEqual(spec_ids, planned_ids)

    def test_spec_ids_match_runner_plan_with_null_element_and_top_level_true(
        self,
    ) -> None:
        # Regression for the precedence fix: a `null` element inside the sweep
        # axis means "unrestricted for that element" and must NOT be re-resolved
        # against the top-level `true`. Resume must reproduce all runner ids.
        #
        # NOTE: `[None, True]` is used rather than a 3-element axis because
        # `[None, True, False]` collapses to `[False, True, False]` -- the two
        # falsy elements produce the SAME run_id, so only 2 unique ids exist.
        # Cross `disable_post_message` with `pre_coordination_round` to get three
        # genuinely distinct variants.
        sweep = _sweep(
            disable_post_message=[None, True],
            pre_coordination_round=[False, True],
        )
        cfg = _base_cfg(
            sweep=sweep,
            exp_extra={"seeds": [1, 2], "disable_post_message": True},
        )

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        # 2 (disable) x 2 (precoord) x 2 (seeds) = 8 distinct ids.
        self.assertEqual(len(planned_ids), 8)
        self.assertEqual(spec_ids, planned_ids)

    def test_duplicate_axis_elements_collapse(self) -> None:
        # `None` and `False` both mean "unrestricted", so they map to the same
        # run_id. Deduplication is expected, not a bug.
        sweep = _sweep(disable_post_message=[None, True, False])
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1]})

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        # Only 2 unique ids: unrestricted, and __nopm.
        self.assertEqual(len(planned_ids), 2)
        self.assertEqual(spec_ids, planned_ids)

    def test_precoord_spec_ids_match_runner_plan(self) -> None:
        sweep = _sweep(
            disable_post_message=[True, False], pre_coordination_round=[True, False]
        )
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1]})

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        self.assertEqual(len(planned_ids), 4)  # 2x2 cross product
        self.assertEqual(spec_ids, planned_ids)

    def test_precoord_rounds_axis_matches_runner_plan(self) -> None:
        # Cross the round-count axis with the phase flag and the restriction.
        # The phase flag collapses the count axis when it is off.
        sweep = _sweep(
            disable_post_message=[True, False],
            pre_coordination_round=[True, False],
            pre_coordination_rounds=[1, 2],
        )
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1]})

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        self.assertEqual(spec_ids, planned_ids)
        # 3 distinct round-count combinations per (disable_post_message, seed):
        # counts {1, 2} for phase-on and the collapsed {1} for phase-off.
        # => 2 dpm x 3 x 1 seed = 6.
        self.assertEqual(len(planned_ids), 6)
        # The two different counts are present for phase-on runs only.
        self.assertEqual(
            {r.rsplit("__precoord", 1)[1] for r in planned_ids if "__precoord" in r},
            {"1", "2"},
        )
        self.assertEqual(sum("__precoord" in r for r in planned_ids), 4)

    def test_phase_off_collapses_rounds_axis(self) -> None:
        sweep = _sweep(
            disable_post_message=[False],
            pre_coordination_round=[False],
            pre_coordination_rounds=[1, 2, 3],
        )
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1]})

        planned_ids = set(_run_from_config_ids(cfg))
        spec_ids = {spec.run_id for spec in resume_mod._iter_expected_run_specs(cfg)}

        self.assertEqual(spec_ids, planned_ids)
        self.assertEqual(len(planned_ids), 1)
        self.assertFalse(any("__precoord" in r for r in planned_ids))

    def test_progress_total_runs_matches_planned(self) -> None:
        sweep = _sweep(
            disable_post_message=[True, False],
            pre_coordination_round=[True, False],
            pre_coordination_rounds=[1, 2],
        )
        cfg = _base_cfg(sweep=sweep, exp_extra={"seeds": [1, 2]})

        ids, info = _run_from_config_ids_and_progress(cfg)

        # The pre-count must mirror the scheduler exactly (2 dpm x 3 combos
        # x 2 seeds = 12).
        self.assertEqual(info["planned"], 12)
        self.assertEqual(len(ids), 12)
        self.assertEqual(info["progress"]["total_runs"], info["planned"])

    def test_invalid_sweep_count_rejected_before_root_created(self) -> None:
        sweep = _sweep(pre_coordination_round=[True], pre_coordination_rounds=[0])
        cfg = _base_cfg(sweep=sweep)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            with self.assertRaises(ValueError):
                asyncio.run(
                    run_mod.run_from_config(str(config_path), out_dir=str(out_dir))
                )
            # Validation happens before the output root is created.
            self.assertFalse(out_dir.exists())


if __name__ == "__main__":
    unittest.main()
