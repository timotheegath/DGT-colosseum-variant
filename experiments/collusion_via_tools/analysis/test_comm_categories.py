"""Tests for :mod:`experiments.collusion_via_tools.analysis.comm_categories`.

Run with::

    .venv/bin/python -m unittest experiments.collusion_via_tools.analysis.test_comm_categories

The tests build tiny fake output trees (a ``runs/`` folder plus a
``comm_protocol_family.md``) in ``tempfile.TemporaryDirectory`` so they never
touch the real ``saved_outputs/`` data.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from experiments.collusion_via_tools.analysis import comm_categories as cc

_MARKDOWN = """\
### 1. Dedicated Resource Types & Topic Channels

*Assigned specific semantic meanings to particular resource categories (such as `IV_Kits`) to act as channel selectors.*

* `run_a/blackboard_1.txt`
* `run_b/blackboard_1.txt`
* `run_b/blackboard_1.txt`

---

### 2. Modulo Arithmetic / Remainder Decoding

*Applied modulo operations (such as `amount mod 10`) to extract single-digit payloads.*

* `run_b/blackboard_1.txt`
* `run_c/blackboard_1.txt`
"""


def _make_run(root: str, sweep: str, run_id: str) -> str:
    """Create ``<root>/runs/m/<sweep>/<run_id>/metrics.json``."""
    run_dir = os.path.join(root, "runs", "m", sweep, run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as handle:
        json.dump({}, handle)
    return run_dir


def _write_markdown(root: str, text: str = _MARKDOWN) -> str:
    path = os.path.join(root, cc.COMM_PROTOCOL_FAMILY_MD)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _make_tree(root: str) -> None:
    """A root with two sweeps; sweep ``alpha`` holds runs a/b, ``beta`` holds c."""
    _make_run(root, "alpha", "run_a")
    _make_run(root, "alpha", "run_b")
    _make_run(root, "alpha", "run_d")
    _make_run(root, "beta", "run_c")
    _write_markdown(root)


class ParseCategoriesTest(unittest.TestCase):
    """The markdown header/description/bullet structure is parsed correctly."""

    def test_categories_are_ordered_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _write_markdown(root)
            categories = cc.parse_comm_protocol_family(
                os.path.join(root, cc.COMM_PROTOCOL_FAMILY_MD)
            )
            self.assertEqual([c.id for c in categories], [1, 2])
            self.assertEqual(
                categories[0].name, "Dedicated Resource Types & Topic Channels"
            )
            self.assertEqual(
                categories[1].name, "Modulo Arithmetic / Remainder Decoding"
            )

    def test_description_is_extracted_without_the_asterisks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _write_markdown(root)
            categories = cc.parse_comm_protocol_family(
                os.path.join(root, cc.COMM_PROTOCOL_FAMILY_MD)
            )
            self.assertTrue(categories[0].description.startswith("Assigned specific"))
            self.assertIn("`IV_Kits`", categories[0].description)
            self.assertFalse(categories[0].description.startswith("*"))

    def test_membership_is_deduplicated_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _write_markdown(root)
            membership = cc.parse_run_membership(
                os.path.join(root, cc.COMM_PROTOCOL_FAMILY_MD)
            )
            self.assertEqual(membership["run_a"], [1])
            self.assertEqual(membership["run_b"], [1, 2])
            self.assertEqual(membership["run_c"], [2])


class BuildMappingTest(unittest.TestCase):
    """The markdown is joined with the runs discovered on disk."""

    def test_mapping_joins_markdown_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            mapping = cc.build_mapping(root)
            self.assertEqual([c.id for c in mapping.categories], [1, 2])
            self.assertEqual(mapping.run_to_categories["run_a"], (1,))
            self.assertEqual(mapping.run_to_categories["run_b"], (1, 2))
            self.assertEqual(mapping.run_to_categories["run_c"], (2,))
            # run_d is on disk but never classified.
            self.assertNotIn("run_d", mapping.run_to_categories)
            self.assertEqual(
                mapping.sweep_to_runs["alpha"], ("run_a", "run_b", "run_d")
            )
            self.assertEqual(mapping.sweep_to_categories["alpha"], (1, 2))
            self.assertEqual(mapping.sweep_to_categories["beta"], (2,))

    def test_unknown_run_reference_warns_and_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            # run_zzz is referenced by the markdown but never created on disk.
            _write_markdown(
                root,
                _MARKDOWN + "\n* `run_zzz/blackboard_1.txt`\n",
            )
            with redirect_stderr(io.StringIO()) as err:
                mapping = cc.build_mapping(root)
            self.assertIn("run_zzz", err.getvalue())
            self.assertNotIn("run_zzz", mapping.run_to_categories)

    def test_missing_markdown_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_run(root, "alpha", "run_a")
            with self.assertRaises(FileNotFoundError):
                cc.build_mapping(root)


class PayloadTest(unittest.TestCase):
    """The emitted payload follows the documented schema."""

    def test_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            mapping = cc.build_mapping(root)
            payload = cc.sweep_payload(
                mapping, "alpha", "m", "20260101-000000", ["run_a", "run_b", "run_d"]
            )
            self.assertEqual(
                list(payload.keys()),
                [
                    "sweep",
                    "model",
                    "source",
                    "source_document",
                    "n_runs",
                    "n_runs_classified",
                    "categories",
                    "runs",
                ],
            )
            self.assertEqual(payload["n_runs"], 3)
            self.assertEqual(payload["n_runs_classified"], 2)
            self.assertEqual([c["id"] for c in payload["categories"]], [1, 2])
            self.assertEqual(payload["categories"][0]["n_runs"], 2)
            self.assertEqual(payload["categories"][1]["n_runs"], 1)
            self.assertEqual(payload["runs"], {"run_a": [1], "run_b": [1, 2]})

    def test_sweep_without_categories_has_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            # run_d exists on disk but the markdown never classifies it.
            _make_run(root, "gamma", "run_d")
            mapping = cc.build_mapping(root)
            payload = cc.sweep_payload(
                mapping, "gamma", "m", "20260101-000000", ["run_d"]
            )
            self.assertEqual(payload["categories"], [])
            self.assertEqual(payload["runs"], {})
            self.assertEqual(payload["n_runs"], 1)
            self.assertEqual(payload["n_runs_classified"], 0)


class WriteAndLoadTest(unittest.TestCase):
    """Emission and read-back are inverses of each other."""

    def test_write_all_writes_one_json_per_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            with redirect_stderr(io.StringIO()):
                paths = cc.write_all(root)
            self.assertEqual(len(paths), 2)
            for path in paths:
                self.assertTrue(os.path.basename(path) == cc.CATEGORIES_FILENAME)
                self.assertTrue(os.path.isfile(path))
            # The file sits inside the sweep directory, not inside a run directory.
            self.assertIn(
                os.path.join("runs", "m", "alpha", cc.CATEGORIES_FILENAME), paths[0]
            )

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            with redirect_stderr(io.StringIO()):
                cc.write_all(root)
                mapping = cc.load_category_map(root)
            self.assertEqual([c.id for c in mapping.categories], [1, 2])
            self.assertEqual(mapping.run_to_categories["run_a"], (1,))
            self.assertEqual(mapping.run_to_categories["run_b"], (1, 2))
            self.assertEqual(mapping.run_to_categories["run_c"], (2,))
            self.assertEqual(mapping.sweep_to_categories["alpha"], (1, 2))
            self.assertEqual(mapping.sweep_to_categories["beta"], (2,))

    def test_out_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out_dir = os.path.join(root, "out")
            with redirect_stderr(io.StringIO()):
                paths = cc.write_all(root, out_dir=out_dir)
            self.assertEqual(len(paths), 2)
            for path in paths:
                self.assertTrue(path.startswith(out_dir))
                self.assertFalse(path.endswith(cc.CATEGORIES_FILENAME))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "alpha.json")))

    def test_load_without_any_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            with self.assertRaises(FileNotFoundError):
                cc.load_category_map(root)


class CliTest(unittest.TestCase):
    """The CLI writes the artifacts and reports what it did."""

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cc.main([root, "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("dry-run", out.getvalue())
            self.assertFalse(
                os.path.exists(
                    os.path.join(root, "runs", "m", "alpha", cc.CATEGORIES_FILENAME)
                )
            )

    def test_run_writes_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cc.main([root])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("categories: 2", text)
            self.assertIn("alpha", text)
            self.assertIn("Dedicated Resource Types & Topic Channels", text)
            self.assertTrue(
                os.path.exists(
                    os.path.join(root, "runs", "m", "alpha", cc.CATEGORIES_FILENAME)
                )
            )

    def test_help(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()):
                cc.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
