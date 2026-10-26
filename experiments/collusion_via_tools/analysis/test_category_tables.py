"""Tests for :mod:`experiments.collusion_via_tools.analysis.category_metrics_table`
and :mod:`experiments.collusion_via_tools.analysis.category_tables`.

Run with::

    .venv/bin/python -m unittest experiments.collusion_via_tools.analysis.test_category_tables

The tests build a tiny fake output tree in ``tempfile.TemporaryDirectory`` so
they never touch the real ``saved_outputs/`` data.

The fixture deliberately reproduces the trap in the real dataset: **two sweeps
reuse the same seed numbers**, so one category contains two different runs that
share a seed. The pairing semantics around that case are what these tests
lock down.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from experiments.collusion_via_tools.analysis import category_metrics_table as cmt
from experiments.collusion_via_tools.analysis import category_tables
from experiments.collusion_via_tools.analysis import comm_categories as cc
from experiments.collusion_via_tools.analysis import sweep_metrics_table as smt

# Two categories:
#   1 -> alpha_seed0, alpha_seed1, beta_seed0   (seed 0 appears TWICE)
#   2 -> alpha_seed0, beta_seed0                (both seed 0)
_CATEGORY_MEMBERSHIP: dict[int, list[str]] = {
    1: ["alpha_seed0", "alpha_seed1", "beta_seed0"],
    2: ["alpha_seed0", "beta_seed0"],
}
_CATEGORY_NAMES: dict[int, str] = {
    1: "Dedicated Resource Types & Topic Channels",
    2: "Modulo Arithmetic / Remainder Decoding",
}
# run_id -> (sweep, joint_reward)
_RUNS: dict[str, tuple[str, float]] = {
    "alpha_seed0": ("alpha", 100.0),
    "alpha_seed1": ("alpha", 200.0),
    "beta_seed0": ("beta", 400.0),
    "base_seed0": ("base", 10.0),
    "base_seed1": ("base", 20.0),
}


def _make_run(root: str, sweep: str, run_id: str, joint_reward: float) -> str:
    """Create ``<root>/runs/m/<sweep>/<run_id>/metrics.json``."""
    run_dir = os.path.join(root, "runs", "m", sweep, run_id)
    os.makedirs(run_dir, exist_ok=True)
    metrics = {
        "meeting_outcome": {
            "status": "complete",
            "joint_reward": joint_reward,
            "joint_reward_ratio": None,
            "average_agent_reward": joint_reward / 9,
            "variables_assigned": 10,
            "total_patients": 8,
            "converged_patients": 8,
        },
        "joint_reward_regret": 1000.0 - joint_reward,
        "coalition_minus_noncoalition_avg": joint_reward / 10,
        "total_agents": 9,
        "total_messages": 10,
        "complaint_messages": 1,
        "suspicion_rate_non_adversary": 0.25,
        "correct_attribution_rate_non_adversary": 0.25,
        "total_suspicious_agents": 2,
        "total_correct_blamers": 2,
        "collusion_judge_simple": {"mean_score": 3.0},
    }
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as handle:
        json.dump(metrics, handle)
    run_config = {
        "adversaries": ["Resource_Provisioner", "General_Hospital_Triage"],
        "pre_coordination_rounds": 1,
        "pre_coordination_rounds_effective": 1,
        "disable_post_message": True,
        "effective_secret_channel": True,
    }
    with open(
        os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(run_config, handle)
    return run_dir


def _markdown() -> str:
    """Build the ``comm_protocol_family.md`` body for the fixture."""
    blocks: list[str] = []
    for cid in sorted(_CATEGORY_MEMBERSHIP):
        blocks.append(f"### {cid}. {_CATEGORY_NAMES[cid]}")
        blocks.append("")
        blocks.append(f"*Description for category {cid}.*")
        blocks.append("")
        for run_id in _CATEGORY_MEMBERSHIP[cid]:
            blocks.append(f"* `{run_id}/blackboard_1.txt`")
        blocks.append("")
        blocks.append("---")
        blocks.append("")
    return "\n".join(blocks)


def _make_tree(root: str) -> None:
    """Create the fake output tree, its markdown and its per-sweep JSONs."""
    for run_id, (sweep, joint_reward) in _RUNS.items():
        _make_run(root, sweep, run_id, joint_reward)
    md_path = os.path.join(root, cc.COMM_PROTOCOL_FAMILY_MD)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(_markdown())
    with redirect_stderr(io.StringIO()):
        cc.write_all(root)


def _mapping(root: str) -> cc.CategoryMapping:
    return cc.load_category_map(root)


def _runs(root: str) -> list[smt.RunData]:
    return smt.load_runs(root, smt.find_metric_files(root))


class GroupingTest(unittest.TestCase):
    """Runs land in every category they belong to; unclassified runs are dropped."""

    def test_a_run_appears_in_every_category_it_belongs_to(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            groups = cmt.build_category_groups(_runs(root), _mapping(root))["m"]
            by_id = {g.category_id: g for g in groups}
            self.assertEqual(sorted(by_id), [1, 2])
            self.assertEqual(len(by_id[1].runs), 3)
            self.assertEqual(len(by_id[2].runs), 2)
            self.assertEqual(by_id[1].label, _CATEGORY_NAMES[1])
            self.assertEqual(
                by_id[1].sweep, "1_dedicated_resource_types_topic_channels"
            )
            self.assertEqual(by_id[1].sweeps, ["alpha", "beta"])

    def test_unclassified_runs_are_excluded_and_warned_about(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            err = io.StringIO()
            with redirect_stderr(err):
                groups = cmt.build_category_groups(_runs(root), _mapping(root))
            # base_seed0 / base_seed1 carry no category.
            self.assertEqual(len(groups["m"][0].runs), 3)
            self.assertIn("no communication category", err.getvalue())

    def test_filter_by_category_accepts_name_id_and_slug(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            grouped = cmt.build_category_groups(_runs(root), _mapping(root))
            for key in (
                "Modulo Arithmetic / Remainder Decoding",
                "2",
                "2_modulo_arithmetic_remainder_decoding",
            ):
                filtered = cmt.filter_groups_by_category(grouped, {key})
                self.assertEqual(len(filtered["m"]), 1, key)
                self.assertEqual(filtered["m"][0].category_id, 2, key)

    def test_reference_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            groups = cmt.build_category_groups(_runs(root), _mapping(root))["m"]
            self.assertEqual(cmt.resolve_category_reference(groups, "2").category_id, 2)
            self.assertEqual(
                cmt.resolve_category_reference(
                    groups, "2_modulo_arithmetic_remainder_decoding"
                ).category_id,
                2,
            )
            self.assertIsNone(cmt.resolve_category_reference(groups, "nope"))


class DeltaSemanticsTest(unittest.TestCase):
    """Category-vs-category is unpaired; category-vs-sweep is paired by seed."""

    def _spec(self) -> smt.MetricSpec:
        return smt.METRICS_BY_KEY["joint_reward"]

    def test_category_vs_category_is_unpaired_and_uses_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            groups = cmt.build_category_groups(_runs(root), _mapping(root))["m"]
            by_id = {g.category_id: g for g in groups}
            result = cmt.compute_category_delta(
                self._spec(), by_id[2].runs, by_id[1].runs, ddof=1
            )
            # cat2 = {100, 400} -> mean 250 ; cat1 = {100, 200, 400} -> mean 233.33
            self.assertAlmostEqual(result.mean, 250.0 - (700.0 / 3), places=6)
            self.assertFalse(result.paired)
            self.assertEqual(result.paired_n, 0)
            # The reported dispersion is the Welch STANDARD ERROR of the
            # difference, not the group's own SD:
            #   s_cat2 = 212.132 (n=2), s_cat1 = 152.753 (n=3)
            #   SE = sqrt(212.132^2/2 + 152.753^2/3) = 174.005...
            expected_se = math.sqrt(
                (212.13203435596427**2) / 2 + (152.7525231651472**2) / 3
            )
            self.assertAlmostEqual(result.std, expected_se, places=6)

    def test_category_vs_sweep_pairs_by_seed_and_collapses_the_seed_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            runs = _runs(root)
            groups = cmt.build_category_groups(runs, _mapping(root))["m"]
            by_id = {g.category_id: g for g in groups}
            base_runs = [r for r in runs if r.sweep == "base"]
            result = cmt.compute_category_delta_vs_sweep(
                self._spec(), by_id[1].runs, base_runs, ddof=1
            )
            # cat1 has alpha_seed0, alpha_seed1, beta_seed0. Seeds 0 and 1 are
            # paired against base_seed0 / base_seed1.
            self.assertTrue(result.paired)
            self.assertEqual(result.paired_n, 2)
            # The per-seed point is the MEAN OVER THE ARMS at that seed (not the
            # first arm), so seed 0 contributes mean(100, 400) - 10 = 240 and
            # seed 1 contributes 200 - 20 = 180 -> mean 210.
            self.assertAlmostEqual(result.mean, 210.0, places=6)
            self.assertAlmostEqual(result.std, math.sqrt(2) * 30.0, places=6)

    def test_paired_delta_points_are_one_per_category_and_seed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            runs = _runs(root)
            groups = cmt.build_category_groups(runs, _mapping(root))
            grouped: dict[str, dict[str, list[Any]]] = {"m": {}}
            for group in groups["m"]:
                grouped["m"][group.sweep] = group.runs
            grouped["m"]["base"] = [r for r in runs if r.sweep == "base"]

            points, counts = cmt.compute_category_paired_delta_points(grouped, "base")
            self.assertEqual(
                counts["m"]["1_dedicated_resource_types_topic_channels"], 2
            )
            self.assertEqual(counts["m"]["2_modulo_arithmetic_remainder_decoding"], 1)
            # No duplicate (category, seed) pair may survive.
            seen = [(p.sweep, p.seed) for p in points]
            self.assertEqual(len(seen), len(set(seen)))
            # Both arms at seed 0 contribute to ONE point; the contributing
            # run_ids are joined, so no run is silently dropped.
            run_ids = {p.run_id for p in points}
            self.assertIn("alpha_seed0; beta_seed0", run_ids)
            self.assertIn("alpha_seed1", run_ids)

    def test_paired_delta_points_use_the_public_channel_judge_score(self) -> None:
        """``mean_score`` mixes channels; the public channel is comparable."""
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            runs = _runs(root)
            # Give the reference runs a secret channel so their mean_score is
            # inflated relative to the public-channel score.
            for run in runs:
                metrics = run.metrics
                metrics["collusion_judge_simple"] = {
                    "mean_score": 3.5,
                    "channel_scores": [1.0, 5.0],
                }
            groups = cmt.build_category_groups(runs, _mapping(root))
            grouped: dict[str, dict[str, list[Any]]] = {"m": {}}
            for group in groups["m"]:
                grouped["m"][group.sweep] = group.runs
            grouped["m"]["base"] = [r for r in runs if r.sweep == "base"]

            self.assertEqual(cmt.public_channel_judge_score({"a": 1}), None)
            self.assertEqual(
                cmt.public_channel_judge_score(
                    {"collusion_judge_simple": {"channel_scores": [2.0, 5.0]}}
                ),
                2.0,
            )
            self.assertEqual(
                cmt.public_channel_judge_score(
                    {"collusion_judge_simple": {"mean_score": 3.5}}
                ),
                None,
            )

            points, _counts = cmt.compute_category_paired_delta_points(grouped, "base")
            # Public-channel score is 1.0 everywhere, so the judge delta is 0
            # -- not the +2.5 the channel-count artifact would have produced.
            self.assertTrue(points)
            for point in points:
                self.assertAlmostEqual(point.judge_delta or 0.0, 0.0, places=9)


class ArmContrastTest(unittest.TestCase):
    """The within-category arm contrast pairs the two arms at the same seed."""

    def test_arm_contrast_pairs_arms_by_seed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            groups = cmt.build_category_groups(_runs(root), _mapping(root))["m"]
            specs = [smt.METRICS_BY_KEY["joint_reward"]]
            rows = cmt.compute_arm_contrasts(groups, specs, ddof=1)
            self.assertTrue(rows)
            for row in rows:
                # cat1 = {alpha_seed0, alpha_seed1, beta_seed0}: only seed 0 has
                # a run in BOTH arms, so exactly one pair is available.
                self.assertEqual(row["n"], 1)
                self.assertAlmostEqual(abs(row["mean_diff"]), 300.0, places=6)

    def test_arm_membership_counts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            groups = cmt.build_category_groups(_runs(root), _mapping(root))["m"]
            rows = cmt.compute_arm_membership(groups)
            self.assertTrue(rows)
            by_cat = {row["category_slug"]: row for row in rows}
            cat1 = by_cat["1_dedicated_resource_types_topic_channels"]
            # cat1: seed 0 is in both arms, seed 1 is in one arm only.
            self.assertEqual(cat1["both"], 1)
            self.assertEqual(cat1["only_higher"] + cat1["only_lower"], 1)
            self.assertEqual(cat1["n_discordant"], 1)

    def test_cli_arm_contrast_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = category_tables.main([root, "--no-significance"])
            self.assertEqual(code, 0)
            self.assertNotIn("Table D", out.getvalue())

            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = category_tables.main(
                    [root, "--arm-contrast", "--no-significance"]
                )
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("Table D", text)
            self.assertIn("arm membership", text)
            self.assertIn("arm contrast:", text)


class CliTest(unittest.TestCase):
    """The CLI renders the tables with the documented pairing semantics."""

    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = category_tables.main([*argv, "--no-significance"])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_table_c_is_unpaired_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            text = self._run([root])
            self.assertIn("Table C", text)
            self.assertIn("unpaired mean difference", text)
            self.assertIn("categories: 2", text)
            # The unpaired table reports n, never k.
            self.assertNotIn("(k=", text)

    def test_reference_sweep_switches_to_paired(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            text = self._run([root, "--reference-sweep", "base"])
            self.assertIn("paired by seed", text)
            self.assertIn("(k=", text)

    def test_overlap_counts_are_printed(self) -> None:
        """The reader must see how much the two category groups share."""
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            text = self._run([root])
            self.assertIn("overlap:", text)
            # cat1 = {alpha_seed0, alpha_seed1, beta_seed0}
            # cat2 = {alpha_seed0, beta_seed0}   -> |A n B| = 2
            self.assertIn("|A∩B|=2", text)

    def test_table_c_reports_a_standard_error(self) -> None:
        """The +/- next to a difference is the SE of the difference."""
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            text = self._run([root])
            self.assertIn("+/- SE", text)

    def test_root_significance_emits_only_one_sample_rows(self) -> None:
        """The pairwise category-vs-category rows are ill-defined and omitted."""
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out_dir = os.path.join(root, "sig")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = category_tables.main(
                    [
                        root,
                        "--significance-mode",
                        "root",
                        "--reference-sweep",
                        "base",
                        "--significance-out",
                        out_dir,
                    ]
                )
            self.assertEqual(code, 0)
            csv_path = os.path.join(out_dir, "category_significance_delta.csv")
            self.assertTrue(os.path.isfile(csv_path))
            with open(csv_path, encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            # Every row is a one-sample test against the reference.
            self.assertEqual({row.get("test") for row in rows}, {"one_sample"})
            # No pairwise column survives.
            self.assertNotIn("sweep_a", rows[0])
            self.assertNotIn("sweep_b", rows[0])
            # The points file documents which judge metric it carries.
            points_path = os.path.join(out_dir, "judge_vs_advantage_delta_points.csv")
            with open(points_path, encoding="utf-8") as handle:
                header = handle.readline().strip()
            self.assertIn("judge_metric", header)

    def test_sweep_filter_restricts_the_groups(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            text = self._run([root, "--sweep", "alpha"])
            self.assertIn("sweep filter: alpha", text)
            # Only alpha runs are classified, so cat1 has 2 runs (not 3).
            self.assertIn("categories: 2", text)

    def test_csv_and_json_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            csv_path = os.path.join(root, "out.csv")
            json_path = os.path.join(root, "out.json")
            self._run([root, "--csv", csv_path, "--json", json_path])
            self.assertTrue(os.path.isfile(csv_path))
            self.assertTrue(os.path.isfile(json_path))
            with open(csv_path, encoding="utf-8") as handle:
                header = handle.readline().strip()
            self.assertIn("category_id", header)
            self.assertIn("category_slug", header)
            with open(json_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("groups", payload)
            self.assertIn("m", payload["groups"])

    def test_summary_significance_writes_its_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out_dir = os.path.join(root, "sig")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = category_tables.main(
                    [
                        root,
                        "--significance-mode",
                        "summary",
                        "--significance-out",
                        out_dir,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(
                os.path.isfile(os.path.join(out_dir, "category_significance.csv"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(out_dir, "category_significance.md"))
            )


if __name__ == "__main__":
    unittest.main()
