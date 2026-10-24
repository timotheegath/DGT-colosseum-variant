"""Resume behavior for the `pre_coordination_rounds` sweep axis.

The count is now a sweep axis and is encoded in `run_id` as `__precoord<N>`.
Resume compares the treatment metadata in an existing `run_config.json` against
the expected spec, and additionally accepts a pre-existing legacy directory
whose id carries the count-less `__precoord` suffix. A completed run produced
with a different count must be rerun; legacy run configs that predate the
setting executed exactly one round.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

from experiments.collusion_via_tools import resume as resume_mod  # noqa: E402


def _cfg(*, rounds: int = 2, phase: bool = True, adversary_count: int = 1):
    return {
        "environment": {"name": "FakeEnv"},
        "simulation": {"pre_coordination_rounds": rounds},
        "llm_models": [
            {"label": "m", "llm": {"provider": "openai", "model": "gpt-test"}}
        ],
        "experiment": {
            "seeds": [1],
            "max_concurrent_runs": 1,
            "sweeps": [
                {
                    "name": "s",
                    "topologies": ["complete"],
                    "num_agents": [5],
                    "adversary_counts": [adversary_count],
                    "target_roles": [None],
                    "num_patients": [4],
                    "disable_post_message": [True],
                    "pre_coordination_round": phase,
                }
            ],
        },
    }


def _spec(*, rounds: int = 2, phase: bool = True, adversary_count: int = 1):
    return resume_mod.RunSpec(
        model_label="m",
        model_llm_cfg={"provider": "openai", "model": "gpt-test"},
        sweep_name="s",
        topology="complete",
        num_agents=5,
        num_patients=4,
        adversary_count=adversary_count,
        target_role=None,
        seed=1,
        disable_post_message=True,
        pre_coordination_round=phase,
        pre_coordination_rounds=rounds,
    )


def _write_run(root: Path, run_id: str, run_config: Dict[str, Any]) -> Path:
    run_dir = root / "runs" / "m" / "s" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for filename in resume_mod.REQUIRED_RUN_FILES:
        path = run_dir / filename
        if filename == "run_config.json":
            path.write_text(json.dumps(run_config), encoding="utf-8")
        elif filename == "agent_turns.json":
            # `resume._has_error_turn` treats a non-list payload as a failed
            # turn, so this must be a (empty) JSON list, not `{}`.
            path.write_text("[]", encoding="utf-8")
        else:
            path.write_text("{}", encoding="utf-8")
    return run_dir


class ExpectedSpecTests(unittest.TestCase):
    def test_configured_round_count_propagates_to_specs(self) -> None:
        specs = list(resume_mod._iter_expected_run_specs(_cfg(rounds=2)))
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].pre_coordination_rounds, 2)
        self.assertTrue(specs[0].pre_coordination_round)

    def test_default_is_one_round(self) -> None:
        cfg = _cfg()
        cfg.pop("simulation")
        specs = list(resume_mod._iter_expected_run_specs(cfg))
        self.assertEqual(specs[0].pre_coordination_rounds, 1)

    def test_invalid_count_rejected_before_dry_run(self) -> None:
        cfg = _cfg(rounds=0)
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "does-not-exist"
            with self.assertRaises(ValueError):
                asyncio.run(
                    resume_mod.resume_runs(
                        root=missing_root,
                        cfg=cfg,
                        max_concurrent_runs=1,
                        stop_on_error=False,
                        dry_run=True,
                    )
                )
            # Validation happens before the output root is created.
            self.assertFalse(missing_root.exists())

    def test_invalid_sweep_count_rejected_before_dry_run(self) -> None:
        # The fail-fast property must cover sweep-level values too, not just
        # the global fallback.
        cfg = _cfg(rounds=2)
        cfg["experiment"]["sweeps"][0]["pre_coordination_rounds"] = [0]
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "does-not-exist"
            with self.assertRaises(ValueError):
                asyncio.run(
                    resume_mod.resume_runs(
                        root=missing_root,
                        cfg=cfg,
                        max_concurrent_runs=1,
                        stop_on_error=False,
                        dry_run=True,
                    )
                )
            self.assertFalse(missing_root.exists())

    def test_sweep_axis_overrides_global_fallback(self) -> None:
        cfg = _cfg(rounds=2)
        cfg["experiment"]["sweeps"][0]["pre_coordination_rounds"] = [1, 2]
        specs = list(resume_mod._iter_expected_run_specs(cfg))
        self.assertEqual(sorted(s.pre_coordination_rounds for s in specs), [1, 2])
        # The global fallback is ignored for this sweep: no third run.
        self.assertEqual(len(specs), 2)

    def test_axis_collapses_when_phase_off(self) -> None:
        cfg = _cfg(rounds=2, phase=False)
        cfg["experiment"]["sweeps"][0]["pre_coordination_rounds"] = [1, 2]
        specs = list(resume_mod._iter_expected_run_specs(cfg))
        self.assertEqual([s.pre_coordination_rounds for s in specs], [1])


class RunMatchesSpecTests(unittest.TestCase):
    def _run_dir(self, tmp: str, run_config: Dict[str, Any]) -> Path:
        return _write_run(Path(tmp), _spec().run_id, run_config)

    def test_legacy_one_round_matches_one_round_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, {"pre_coordination_round": True})
            self.assertTrue(resume_mod._run_matches_spec(run_dir, _spec(rounds=1)))

    def test_legacy_one_round_does_not_match_two_round_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, {"pre_coordination_round": True})
            self.assertFalse(resume_mod._run_matches_spec(run_dir, _spec(rounds=2)))

    def test_recorded_two_rounds_match_two_round_spec(self) -> None:
        config = {
            "pre_coordination_round": True,
            "pre_coordination_rounds": 2,
            "pre_coordination_rounds_effective": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, config)
            self.assertTrue(resume_mod._run_matches_spec(run_dir, _spec(rounds=2)))
            self.assertFalse(resume_mod._run_matches_spec(run_dir, _spec(rounds=1)))

    def test_phase_flag_mismatch_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(
                tmp,
                {
                    "pre_coordination_round": False,
                    "pre_coordination_rounds": 2,
                    "pre_coordination_rounds_effective": 0,
                },
            )
            self.assertFalse(resume_mod._run_matches_spec(run_dir, _spec(phase=True)))
            self.assertTrue(resume_mod._run_matches_spec(run_dir, _spec(phase=False)))

    def test_disabled_phase_ignores_configured_count(self) -> None:
        # If the phase never ran, a global count change does not invalidate it.
        config = {
            "pre_coordination_round": False,
            "pre_coordination_rounds": 5,
            "pre_coordination_rounds_effective": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, config)
            self.assertTrue(
                resume_mod._run_matches_spec(run_dir, _spec(rounds=2, phase=False))
            )

    def test_benign_run_has_zero_effective_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(
                tmp,
                {
                    "adversaries": [],
                    "pre_coordination_round": True,
                    "pre_coordination_rounds": 2,
                    "pre_coordination_rounds_effective": 0,
                },
            )
            spec = _spec(rounds=2, phase=True, adversary_count=0)
            self.assertTrue(resume_mod._run_matches_spec(run_dir, spec))

    def test_mismatched_count_is_selected_for_rerun(self) -> None:
        cfg = _cfg(rounds=2)
        expected = list(resume_mod._iter_expected_run_specs(cfg))[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(root, expected.run_id, {"pre_coordination_round": True})
            incomplete, completed, total = resume_mod._select_incomplete_runs(
                root=root, cfg=cfg
            )
            self.assertEqual(total, 1)
            self.assertEqual(completed, 0)
            self.assertEqual([s.run_id for s in incomplete], [expected.run_id])


class LegacyPrecoordCandidateTests(unittest.TestCase):
    """A legacy count-less `__precoord` directory is a resume fallback."""

    def _legacy_id(self, counted_id: str) -> str:
        return counted_id[: -len("__precoord2")] + "__precoord"

    def test_phase_on_adds_legacy_candidate(self) -> None:
        spec = _spec(rounds=2, phase=True)
        legacy = self._legacy_id(spec.run_id)
        self.assertIn(legacy, spec.run_id_candidates)
        # No canonical id equals the legacy one, so a rerun of a different
        # treatment can never target the legacy directory.
        self.assertNotEqual(spec.run_id, legacy)
        self.assertNotIn(
            legacy,
            [_spec(rounds=1, phase=True).run_id, _spec(rounds=2, phase=True).run_id],
        )

    def test_phase_off_has_no_precoord_candidate(self) -> None:
        spec = _spec(rounds=2, phase=False)
        self.assertFalse(any(c.endswith("__precoord") for c in spec.run_id_candidates))
        self.assertFalse(any("__precoord" in c for c in spec.run_id_candidates))

    def test_legacy_directory_matched_when_count_agrees(self) -> None:
        cfg = _cfg(rounds=2)
        expected = list(resume_mod._iter_expected_run_specs(cfg))[0]
        legacy_id = self._legacy_id(expected.run_id)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(
                root,
                legacy_id,
                {
                    "pre_coordination_round": True,
                    "pre_coordination_rounds": 2,
                    "pre_coordination_rounds_effective": 2,
                },
            )
            incomplete, completed, total = resume_mod._select_incomplete_runs(
                root=root, cfg=cfg
            )
            self.assertEqual(total, 1)
            self.assertEqual(completed, 1)
            self.assertEqual(incomplete, [])

    def test_legacy_directory_not_matched_when_count_differs(self) -> None:
        cfg = _cfg(rounds=2)
        expected = list(resume_mod._iter_expected_run_specs(cfg))[0]
        legacy_id = self._legacy_id(expected.run_id)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(
                root,
                legacy_id,
                {
                    "pre_coordination_round": True,
                    "pre_coordination_rounds": 1,
                    "pre_coordination_rounds_effective": 1,
                },
            )
            incomplete, completed, total = resume_mod._select_incomplete_runs(
                root=root, cfg=cfg
            )
            self.assertEqual(total, 1)
            self.assertEqual(completed, 0)
            self.assertEqual([s.run_id for s in incomplete], [expected.run_id])


if __name__ == "__main__":
    unittest.main()
