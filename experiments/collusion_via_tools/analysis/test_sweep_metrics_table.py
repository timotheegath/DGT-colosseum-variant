"""Tests for :mod:`experiments.collusion_via_tools.analysis.sweep_metrics_table`.

Run with::

    .venv/bin/python -m unittest experiments.collusion_via_tools.analysis.test_sweep_metrics_table

The tests build tiny fake output trees in ``tempfile.TemporaryDirectory`` so
they never touch the real ``saved_outputs/`` data.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from experiments.collusion_via_tools.analysis import sweep_metrics_table as smt
from experiments.collusion_via_tools.analysis import tables


def _run_tables(argv: list[str]) -> int:
    """Invoke the consolidated ``tables`` CLI with the significance step off.

    The table tests must not depend on whether a summary-stats CSV happens to
    exist under the package's ``plots_outputs/`` directory, so the
    significance step is always skipped here.
    """
    return tables.main([*argv, "--no-significance"])


def _make_run(
    root: str,
    source_rel: str,
    model_parts: list[str],
    sweep: str,
    run_id: str,
    metrics: dict[str, Any] | None = None,
    run_config: dict[str, Any] | None = None,
) -> str:
    """Create ``<root>/<source_rel>/runs/<model>/<sweep>/<run_id>/metrics.json``."""
    run_dir = os.path.join(root, source_rel, "runs", *model_parts, sweep, run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as handle:
        json.dump(metrics if metrics is not None else {}, handle)
    if run_config is not None:
        with open(
            os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(run_config, handle)
    return run_dir


def _write_agent_turns(run_dir: str, turns: Any) -> str:
    path = os.path.join(run_dir, "agent_turns.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(turns, handle)
    return path


def _load_one(root: str) -> smt.RunData:
    found = smt.find_metric_files(root)
    assert len(found) == 1, found
    runs = smt.load_runs(root, found)
    assert len(runs) == 1, runs
    return runs[0]


class AnchorRuleTest(unittest.TestCase):
    """Section 2: the ``runs`` anchor rule works at every root depth."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ts = "20260101-000000_tag"
        _make_run(
            self.root,
            os.path.join("run_1", self.ts),
            ["modal", "glm"],
            "sweep_a",
            "run_seed0",
        )
        self.ts_root = os.path.join(self.root, "run_1", self.ts)
        self.runs_root = os.path.join(self.ts_root, "runs")

    def test_deep_root(self) -> None:
        run = _load_one(self.root)
        self.assertEqual(run.source, "20260101-000000")
        self.assertEqual(run.model, "modal/glm")
        self.assertEqual(run.sweep, "sweep_a")
        self.assertEqual(run.source_label, self.ts)
        self.assertEqual(run.source_path, os.path.join("run_1", self.ts))

    def test_timestamped_root(self) -> None:
        run = _load_one(self.ts_root)
        self.assertEqual(
            (run.source, run.model, run.sweep),
            ("20260101-000000", "modal/glm", "sweep_a"),
        )
        self.assertEqual(run.source_path, "")

    def test_runs_root(self) -> None:
        run = _load_one(self.runs_root)
        self.assertEqual((run.model, run.sweep), ("modal/glm", "sweep_a"))
        self.assertEqual(run.source_label, "runs")
        self.assertEqual(run.source, "runs")
        self.assertEqual(run.source_path, "")

    def test_source_short(self) -> None:
        self.assertEqual(
            smt.source_short("20261024-132030_1coodrounds"), "20261024-132030"
        )
        self.assertEqual(smt.source_short("plain_label"), "plain_label")

    def test_hidden_dirs_skipped(self) -> None:
        _make_run(
            self.root,
            os.path.join(".hidden", "20260101-000000_x"),
            ["m"],
            "sweep",
            "run",
        )
        found = smt.find_metric_files(self.root)
        self.assertEqual(len(found), 1)


class PoolingTest(unittest.TestCase):
    """Sections 2/3: same sweep name under two sources → ONE pooled group."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        _make_run(
            self.root, os.path.join("run_1", "20260101-000000_a"), ["m"], "shared", "r1"
        )
        _make_run(
            self.root, os.path.join("run_1", "20260102-000000_b"), ["m"], "shared", "r1"
        )
        _make_run(
            self.root, os.path.join("run_1", "20260101-000000_a"), ["m"], "unique", "r1"
        )

    def _grouped(
        self, err: io.StringIO | None = None
    ) -> dict[str, list[smt.SweepGroup]]:
        runs = smt.load_runs(self.root, smt.find_metric_files(self.root))
        with redirect_stderr(err if err is not None else io.StringIO()):
            return smt.group_runs(runs)

    def test_same_sweep_pooled_into_one_group(self) -> None:
        err = io.StringIO()
        groups = self._grouped(err)["m"]
        self.assertEqual([group.label for group in groups], ["shared", "unique"])
        shared = groups[0]
        self.assertEqual(shared.label, "shared")
        self.assertNotIn("@", shared.label)
        self.assertEqual(shared.sweep, "shared")
        self.assertEqual(len(shared.runs), 2)
        self.assertEqual(shared.sources, ["20260101-000000", "20260102-000000"])
        self.assertIn("warning: sweep 'shared' spans 2 output roots", err.getvalue())
        self.assertIn("pooled into one group", err.getvalue())

    def test_source_labels_and_paths_collected(self) -> None:
        shared = self._grouped()["m"][0]
        self.assertEqual(
            shared.source_labels, ["20260101-000000_a", "20260102-000000_b"]
        )
        self.assertEqual(
            shared.source_paths,
            [
                os.path.join("run_1", "20260101-000000_a"),
                os.path.join("run_1", "20260102-000000_b"),
            ],
        )

    def test_single_source_sweep_not_warned(self) -> None:
        err = io.StringIO()
        self._grouped(err)
        self.assertNotIn("'unique'", err.getvalue())

    def test_runs_sorted_by_run_id_then_source(self) -> None:
        shared = self._grouped()["m"][0]
        keys = [(run.run_id, run.source) for run in shared.runs]
        self.assertEqual(keys, sorted(keys))


class PooledProvenanceTest(unittest.TestCase):
    """Section 4/7: pooled sources surface as joined provenance."""

    def _build(self, root: str) -> None:
        _make_run(
            root, os.path.join("run_1", "20260101-000000_a"), ["m"], "shared", "r1"
        )
        _make_run(
            root, os.path.join("run_1", "20260102-000000_b"), ["m"], "shared", "r1"
        )

    def test_table_a_joins_sources(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._build(root)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = _run_tables([root])
            self.assertEqual(code, 0)
            self.assertIn("20260101-000000; 20260102-000000", out.getvalue())

    def test_csv_source_joined(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._build(root)
            csv_path = os.path.join(root, "out.csv")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                _run_tables([root, "--csv", csv_path])
            with open(csv_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["source"] for row in rows},
                {"20260101-000000; 20260102-000000"},
            )
            self.assertEqual(
                {row["source_path"] for row in rows},
                {"run_1/20260101-000000_a; run_1/20260102-000000_b"},
            )
            self.assertEqual({row["sweep"] for row in rows}, {"shared"})

    def test_json_group_has_sources_list(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._build(root)
            json_path = os.path.join(root, "out.json")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                _run_tables([root, "--json", json_path])
            with open(json_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            group = payload["groups"]["m"]["shared"]
            self.assertEqual(group["sources"], ["20260101-000000", "20260102-000000"])
            self.assertEqual(
                group["source_labels"], ["20260101-000000_a", "20260102-000000_b"]
            )
            self.assertEqual(
                group["source_paths"],
                ["run_1/20260101-000000_a", "run_1/20260102-000000_b"],
            )
            self.assertEqual(group["sweep"], "shared")
            self.assertNotIn("source", group)

    def test_source_filter_keeps_pooled_group(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._build(root)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = _run_tables([root, "--source", "20260102-000000"])
            self.assertEqual(code, 0)
            self.assertIn("shared", out.getvalue())


class MultiRootCliTest(unittest.TestCase):
    """Section 1/8: multiple roots produce one combined table set."""

    def test_combined_output_and_csv(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_a,
            tempfile.TemporaryDirectory() as root_b,
        ):
            _make_run(root_a, "", ["m"], "sweep_one", "r1")
            _make_run(root_b, "", ["m"], "sweep_two", "r1")
            csv_path = os.path.join(root_a, "out.csv")

            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = _run_tables([root_a, root_b, "--csv", csv_path])

            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("sweep_one", text)
            self.assertIn("sweep_two", text)
            self.assertIn("roots:", text)

            with open(csv_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["sweep"] for row in rows}, {"sweep_one", "sweep_two"})
            self.assertEqual(
                {row["source"] for row in rows},
                {
                    smt.source_short(os.path.basename(root_a)),
                    smt.source_short(os.path.basename(root_b)),
                },
            )

    def test_root_opt_is_repeatable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_a,
            tempfile.TemporaryDirectory() as root_b,
        ):
            _make_run(root_a, "", ["m"], "sweep_one", "r1")
            _make_run(root_b, "", ["m"], "sweep_two", "r1")

            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = _run_tables(["--root", root_a, "--root", root_b])

            self.assertEqual(code, 0)
            self.assertIn("sweep_one", out.getvalue())
            self.assertIn("sweep_two", out.getvalue())

    def test_no_root_errors(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = _run_tables([])
        self.assertEqual(code, 2)
        self.assertIn("a root path is required", err.getvalue())

    def test_missing_roots_reported_together(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = _run_tables(["/nope/one", "/nope/two"])
        self.assertEqual(code, 2)
        self.assertIn("/nope/one", err.getvalue())
        self.assertIn("/nope/two", err.getvalue())


class RunHealthTest(unittest.TestCase):
    """Section 1/8: ``run_health`` mirrors resume.py's error-turn rule."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self._tmp.name

    def test_clean_run(self) -> None:
        _write_agent_turns(self.run_dir, [{"agent": "A", "response": "all good"}])
        self.assertEqual(smt.run_health(self.run_dir), (False, []))
        self.assertFalse(smt.run_has_error_turns(self.run_dir))

    def test_error_turn(self) -> None:
        _write_agent_turns(
            self.run_dir, [{"agent": "A", "response": "[ERROR] HTTP 400: bad JSON"}]
        )
        self.assertEqual(smt.run_health(self.run_dir), (True, ["A"]))
        self.assertTrue(smt.run_has_error_turns(self.run_dir))

    def test_multiple_error_agents_sorted_unique(self) -> None:
        _write_agent_turns(
            self.run_dir,
            [
                {"agent": "B", "response": "[ERROR] x"},
                {"agent": "A", "response": "[ERROR] y"},
                {"agent": "B", "response": "[ERROR] z"},
                {"agent": "C", "response": "fine"},
            ],
        )
        self.assertEqual(smt.run_health(self.run_dir), (True, ["A", "B"]))

    def test_missing_file(self) -> None:
        self.assertEqual(smt.run_health(self.run_dir), (True, []))

    def test_malformed_json(self) -> None:
        path = os.path.join(self.run_dir, "agent_turns.json")
        with open(path, "wb") as handle:
            handle.write(b'[{"response": "[ERROR] boom"')
        self.assertEqual(smt.run_health(self.run_dir), (True, []))

    def test_truncated_json_without_marker_is_not_clean(self) -> None:
        # A truncated turns file is unreadable, so resume.py would re-run it.
        path = os.path.join(self.run_dir, "agent_turns.json")
        with open(path, "wb") as handle:
            handle.write(b'[{"agent": "A", "response": "all good"}')
        self.assertEqual(smt.run_health(self.run_dir), (True, []))

    def test_parsed_value_not_a_list(self) -> None:
        _write_agent_turns(self.run_dir, {"response": "[ERROR] boom"})
        self.assertEqual(smt.run_health(self.run_dir), (True, []))

    def test_marker_in_prompt_only_is_clean(self) -> None:
        # The fast path scans bytes; a marker outside a response is not an error.
        _write_agent_turns(
            self.run_dir,
            [{"agent": "A", "response": "ok", "user_prompt": "see [ERROR] history"}],
        )
        self.assertEqual(smt.run_health(self.run_dir), (False, []))


class PartitionRunsTest(unittest.TestCase):
    """Section 6: ``partition_runs`` is a no-op unless opt-in."""

    def _runs(self, root: str) -> list[smt.RunData]:
        return smt.load_runs(root, smt.find_metric_files(root))

    def test_disabled_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_dir = _make_run(root, "", ["m"], "sweepA", "run_seed0")
            _write_agent_turns(run_dir, [{"agent": "A", "response": "[ERROR] boom"}])
            runs = self._runs(root)
            kept, excluded = smt.partition_runs(runs, False)
            self.assertEqual(len(kept), 1)
            self.assertEqual(excluded, [])

    def test_enabled_splits(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            good = _make_run(root, "", ["m"], "sweepA", "run_seed0")
            bad = _make_run(root, "", ["m"], "sweepA", "run_seed1")
            _write_agent_turns(good, [{"agent": "A", "response": "ok"}])
            _write_agent_turns(bad, [{"agent": "B", "response": "[ERROR] boom"}])
            kept, excluded = smt.partition_runs(self._runs(root), True)
            self.assertEqual([run.run_id for run in kept], ["run_seed0"])
            self.assertEqual(len(excluded), 1)
            self.assertEqual(excluded[0][1], ["B"])


class ExcludeErrorRunsCliTest(unittest.TestCase):
    """Sections 2/3/4/5: CLI flag, reporting, inventory and JSON."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        good = _make_run(self.root, "", ["m"], "sweepA", "run_seed0")
        bad = _make_run(self.root, "", ["m"], "sweepA", "run_seed1")
        _write_agent_turns(good, [{"agent": "A", "response": "ok"}])
        _write_agent_turns(bad, [{"agent": "B", "response": "[ERROR] boom"}])
        self.json_path = os.path.join(self.root, "out.json")

    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = _run_tables(argv)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_without_flag_both_runs_present(self) -> None:
        text = self._run([self.root, "--json", self.json_path])
        self.assertNotIn("excluded error runs", text)
        with open(self.json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["groups"]["m"]["sweepA"]["n_runs"], 2)
        self.assertEqual(payload["excluded_runs"], [])

    def test_with_flag_excludes_and_reports(self) -> None:
        text = self._run([self.root, "--exclude-error-runs", "--json", self.json_path])
        self.assertIn("excluded error runs: 1 of 2", text)
        self.assertIn("sweepA", text)
        self.assertIn("seed1", text)
        self.assertIn("[B]", text)
        self.assertIn("1/2", text)  # Table A kept/total
        with open(self.json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["groups"]["m"]["sweepA"]["n_runs"], 1)
        self.assertEqual(
            payload["excluded_runs"],
            [
                {
                    "model": "m",
                    "sweep": "sweepA",
                    "seed": 1,
                    "run_id": "run_seed1",
                    "agents": ["B"],
                    "reason": "error turn",
                }
            ],
        )


class CollapsedRuleTest(unittest.TestCase):
    """Section 1/7: ``run_is_collapsed`` and ``run_is_failed``."""

    def test_collapsed_zero(self) -> None:
        self.assertTrue(
            smt.run_is_collapsed({"meeting_outcome": {"variables_assigned": 0}})
        )

    def test_not_collapsed_positive(self) -> None:
        self.assertFalse(
            smt.run_is_collapsed({"meeting_outcome": {"variables_assigned": 85}})
        )

    def test_missing_meeting_outcome(self) -> None:
        self.assertTrue(smt.run_is_collapsed({}))

    def test_meeting_outcome_without_key(self) -> None:
        self.assertTrue(smt.run_is_collapsed({"meeting_outcome": {}}))

    def test_non_numeric_value(self) -> None:
        self.assertTrue(
            smt.run_is_collapsed({"meeting_outcome": {"variables_assigned": None}})
        )

    def _one_run(
        self, tmp: str, metrics: dict[str, Any], turns: list[dict[str, Any]]
    ) -> smt.RunData:
        run_dir = _make_run(tmp, "", ["m"], "sweepA", "run_seed0", metrics=metrics)
        _write_agent_turns(run_dir, turns)
        return smt.load_runs(tmp, smt.find_metric_files(tmp))[0]

    def test_failed_error_and_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._one_run(
                tmp,
                {"meeting_outcome": {"variables_assigned": 0}},
                [{"agent": "A", "response": "[ERROR] boom"}],
            )
            self.assertTrue(smt.run_is_failed(run))

    def test_error_turn_but_healthy_is_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._one_run(
                tmp,
                {"meeting_outcome": {"variables_assigned": 85}},
                [{"agent": "A", "response": "[ERROR] boom"}],
            )
            self.assertFalse(smt.run_is_failed(run))

    def test_collapsed_without_error_turn_is_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self._one_run(
                tmp,
                {"meeting_outcome": {"variables_assigned": 0}},
                [{"agent": "A", "response": "ok"}],
            )
            self.assertFalse(smt.run_is_failed(run))


class ExcludeFailedRunsCliTest(unittest.TestCase):
    """Sections 2/3/4/5: collapsed-only exclusion via the CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        # Errored but healthy: kept by the failed rule.
        healthy = _make_run(
            self.root,
            "",
            ["m"],
            "sweepA",
            "run_seed0",
            metrics={"meeting_outcome": {"variables_assigned": 85}},
        )
        # Errored and collapsed: dropped by the failed rule.
        broken = _make_run(
            self.root,
            "",
            ["m"],
            "sweepA",
            "run_seed1",
            metrics={"meeting_outcome": {"variables_assigned": 0}},
        )
        _write_agent_turns(healthy, [{"agent": "A", "response": "[ERROR] boom"}])
        _write_agent_turns(broken, [{"agent": "B", "response": "[ERROR] boom"}])
        self.json_path = os.path.join(self.root, "out.json")

    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = _run_tables(argv)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_only_collapsed_run_dropped(self) -> None:
        text = self._run([self.root, "--exclude-failed-runs", "--json", self.json_path])
        self.assertIn("excluded failed runs: 1 of 2", text)
        self.assertIn("seed1", text)
        self.assertIn("(failed: error turn, 0 variables assigned)", text)
        self.assertIn("1/2", text)  # Table A kept/total
        with open(self.json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["groups"]["m"]["sweepA"]["n_runs"], 1)
        self.assertEqual(
            payload["excluded_runs"],
            [
                {
                    "model": "m",
                    "sweep": "sweepA",
                    "seed": 1,
                    "run_id": "run_seed1",
                    "agents": ["B"],
                    "reason": "failed: error turn, 0 variables assigned",
                }
            ],
        )

    def test_error_rule_still_drops_both(self) -> None:
        text = self._run([self.root, "--exclude-error-runs", "--json", self.json_path])
        self.assertIn("excluded error runs: 2 of 2", text)
        with open(self.json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        # The pure error-turn rule reports the generic reason.
        self.assertEqual(
            {entry["reason"] for entry in payload["excluded_runs"]},
            {"error turn"},
        )

    def test_two_argument_partition_runs_still_works(self) -> None:
        runs = smt.load_runs(self.root, smt.find_metric_files(self.root))
        kept, excluded = smt.partition_runs(runs, True)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(excluded), 2)
        for run, agents in excluded:  # tuple shape preserved
            self.assertIsInstance(run, smt.RunData)
            self.assertIsInstance(agents, list)


def _fake_run(sweep: str, seed: int, metrics: dict[str, Any]) -> smt.RunData:
    """Build a ``RunData`` in memory (seed parsed from ``run_seed<S>``)."""
    return smt.RunData(
        model="m",
        sweep=sweep,
        source="20260101-000000",
        source_label="20260101-000000_tag",
        source_path=os.path.join("run_1", "20260101-000000_tag"),
        run_id=f"run_seed{seed}",
        run_dir=f"/tmp/opencode/fake/{sweep}/run_seed{seed}",
        metrics=metrics,
        run_config=None,
        costs=None,
    )


class AggregateRobustStatsTest(unittest.TestCase):
    """``aggregate`` reports median and linear-interpolation quartiles."""

    def test_known_quartiles(self) -> None:
        agg = smt.aggregate([1.0, 2.0, 3.0, 4.0], n_missing=0, ddof=1)
        self.assertEqual(agg.median, 2.5)
        self.assertEqual(agg.p25, 1.75)
        self.assertEqual(agg.p75, 3.25)

    def test_quartiles_match_inclusive_linear_convention(self) -> None:
        # numpy.percentile([1, 2, 3, 4, 5], [25, 75], method="linear") -> 2, 4.
        agg = smt.aggregate([1.0, 2.0, 3.0, 4.0, 5.0], n_missing=0, ddof=1)
        self.assertEqual(agg.median, 3.0)
        self.assertEqual(agg.p25, 2.0)
        self.assertEqual(agg.p75, 4.0)

    def test_single_value(self) -> None:
        agg = smt.aggregate([7.0], n_missing=0, ddof=1)
        self.assertEqual(agg.median, 7.0)
        self.assertEqual(agg.p25, 7.0)
        self.assertEqual(agg.p75, 7.0)

    def test_empty(self) -> None:
        agg = smt.aggregate([], n_missing=3, ddof=1)
        self.assertIsNone(agg.mean)
        self.assertIsNone(agg.median)
        self.assertIsNone(agg.p25)
        self.assertIsNone(agg.p75)


class MetricsTableCenterTest(unittest.TestCase):
    """Table B renders ``mean +/- std`` or ``median [p25, p75]`` by centre."""

    def _setup(self) -> tuple[smt.MetricSpec, dict[str, smt.GroupMetrics]]:
        spec = smt.METRICS_BY_KEY["joint_reward"]
        agg = smt.aggregate([1.0, 2.0, 3.0, 4.0], n_missing=0, ddof=1)
        groups = {
            "sweepA": smt.GroupMetrics(n_runs=4, aggregates={spec.key: agg}),
        }
        return spec, groups

    def _render(self, center: str) -> str:
        spec, groups = self._setup()
        out = io.StringIO()
        with redirect_stdout(out):
            smt.print_metrics_table(["sweepA"], groups, [spec], "m", center)
        return out.getvalue()

    def test_median_mode_cell(self) -> None:
        spec, _ = self._setup()
        text = self._render("median")
        expected = (
            f"{smt.format_value(spec.fmt, 2.5)} "
            f"[{smt.format_value(spec.fmt, 1.75)}, {smt.format_value(spec.fmt, 3.25)}]"
        )
        self.assertIn(expected, text)
        self.assertIn("Table B -- metrics (median [p25, p75])", text)

    def test_mean_mode_cell_unchanged(self) -> None:
        spec, groups = self._setup()
        agg = groups["sweepA"].aggregates[spec.key]
        text = self._render("mean")
        expected = (
            f"{smt.format_value(spec.fmt, agg.mean)} +/- "
            f"{smt.format_value(spec.fmt, agg.std)}"
        )
        self.assertIn(expected, text)
        self.assertIn("Table B -- metrics (mean +/- std)", text)


class DeltaTableCenterTest(unittest.TestCase):
    """Table C renders the median of the paired diffs in median mode."""

    def _groups(self) -> tuple[smt.SweepGroup, smt.SweepGroup, smt.MetricSpec]:
        spec = smt.METRICS_BY_KEY["coalition_minus_noncoalition_avg"]
        key = "coalition_minus_noncoalition_avg"
        ref_runs = [_fake_run("baseline", seed, {key: 0.0}) for seed in (0, 1, 2)]
        sweep_runs = [
            _fake_run("collusion", seed, {key: float(10 * (seed + 1))})
            for seed in (0, 1, 2)
        ]
        ref = smt.SweepGroup(
            label="baseline",
            sweep="baseline",
            sources=["20260101-000000"],
            source_labels=["20260101-000000_tag"],
            source_paths=[os.path.join("run_1", "20260101-000000_tag")],
            runs=ref_runs,
        )
        other = smt.SweepGroup(
            label="collusion",
            sweep="collusion",
            sources=["20260101-000000"],
            source_labels=["20260101-000000_tag"],
            source_paths=[os.path.join("run_1", "20260101-000000_tag")],
            runs=sweep_runs,
        )
        return ref, other, spec

    def test_median_cell_keeps_k_suffix(self) -> None:
        ref, other, spec = self._groups()
        out = io.StringIO()
        with redirect_stdout(out):
            smt.print_delta_table([ref, other], ref, [spec], "m", 1, "median")
        text = out.getvalue()
        # Paired diffs are [10, 20, 30]: median 20, p25 15, p75 25.
        expected = (
            f"{smt.format_value(spec.fmt, 20.0)} "
            f"[{smt.format_value(spec.fmt, 15.0)}, {smt.format_value(spec.fmt, 25.0)}]"
            f" (k=3)"
        )
        self.assertIn(expected, text)
        self.assertIn("median [p25, p75]", text)

    def test_mean_cell_unchanged(self) -> None:
        ref, other, spec = self._groups()
        out = io.StringIO()
        with redirect_stdout(out):
            smt.print_delta_table([ref, other], ref, [spec], "m", 1, "mean")
        text = out.getvalue()
        expected = (
            f"{smt.format_value(spec.fmt, 20.0)} +/- "
            f"{smt.format_value(spec.fmt, 10.0)} (k=3)"
        )
        self.assertIn(expected, text)


class CenterCliTest(unittest.TestCase):
    """``--center`` flag plumbing, CSV columns and JSON keys."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        _make_run(
            self.root,
            "",
            ["m"],
            "sweep_a",
            "run_seed0",
            metrics={"meeting_outcome": {"joint_reward": 1.0}},
        )
        _make_run(
            self.root,
            "",
            ["m"],
            "sweep_a",
            "run_seed1",
            metrics={"meeting_outcome": {"joint_reward": 2.0}},
        )

    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = _run_tables(argv)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_default_center_is_mean(self) -> None:
        text = self._run([self.root])
        self.assertIn("center:    mean", text)
        self.assertIn("mean +/- std", text)
        self.assertNotIn("median [p25, p75]", text)

    def test_center_median_accepted(self) -> None:
        text = self._run([self.root, "--center", "median"])
        self.assertIn("center:    median", text)
        self.assertIn("median [p25, p75]", text)

    def test_invalid_center_rejected(self) -> None:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                _run_tables([self.root, "--center", "mode"])

    def test_csv_appends_columns(self) -> None:
        csv_path = os.path.join(self.root, "out.csv")
        self._run([self.root, "--center", "median", "--csv", csv_path])
        with open(csv_path, newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        self.assertEqual(
            header,
            [
                "model",
                "source",
                "source_path",
                "sweep",
                "metric",
                "label",
                "n",
                "n_missing",
                "mean",
                "std",
                "min",
                "max",
                "median",
                "p25",
                "p75",
            ],
        )

    def test_json_gains_keys(self) -> None:
        json_path = os.path.join(self.root, "out.json")
        self._run([self.root, "--center", "median", "--json", json_path])
        with open(json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        metric = payload["groups"]["m"]["sweep_a"]["metrics"]["joint_reward"]
        for key in ("median", "p25", "p75"):
            self.assertIn(key, metric)
        self.assertEqual(metric["median"], 1.5)
        self.assertEqual(metric["p25"], 1.25)
        self.assertEqual(metric["p75"], 1.75)


class SingleRootTest(unittest.TestCase):
    """Section 9: single-root behaviour keeps one plain-labelled group per sweep."""

    def test_single_root_labels(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_run(root, "", ["m"], "sweep_a", "r1")
            _make_run(root, "", ["m"], "sweep_b", "r2")
            runs = smt.load_runs(root, smt.find_metric_files(root))
            grouped = smt.group_runs(runs)
            labels = [group.label for group in grouped["m"]]
            self.assertEqual(labels, ["sweep_a", "sweep_b"])
            self.assertTrue(all("@" not in label for label in labels))

    def test_single_root_header_and_table(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_run(root, "", ["m"], "sweep_a", "r1")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = _run_tables([root])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("root:", text)
            self.assertNotIn("roots:", text)
            self.assertIn("sources:", text)
            self.assertIn("Source", text)


if __name__ == "__main__":
    unittest.main()
