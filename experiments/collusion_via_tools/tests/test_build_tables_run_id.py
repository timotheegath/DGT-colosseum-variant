"""Regression tests for run_id parsing in ``plots/build_tables.py``.

Focus: ``_infer_run_fields`` must accept the post_message restriction suffixes
emitted by ``collusion_via_tools.run._build_run_id`` (``__nopm`` and
``__cut_after_r{N}``) while keeping the legacy ``__strat{...}`` form (without a
suffix) and the unsuffixed form parsing exactly as before.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from experiments.collusion_via_tools.plots.build_tables import _infer_run_fields

# Base run_id template mirroring run._build_run_id (V2 form):
#   {model}__{sweep}__topo{T}__agents{N}_patients{P}_adv{A}_{role}_seed{S}
BASE = "qwen__sweepA__topofully_connected__agents5_patients4_adv2_{role}_seed7{suffix}"


def _infer(run_id: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return _infer_run_fields(run_dir, {"run_id": run_id})


class RunIdParsingTests(unittest.TestCase):
    def assert_common_fields(self, fields: dict, *, role: str) -> None:
        self.assertEqual(fields.get("model_label"), "qwen")
        self.assertEqual(fields.get("sweep_name"), "sweepA")
        self.assertEqual(fields.get("topology"), "fully_connected")
        self.assertEqual(fields.get("requested_num_agents"), 5)
        self.assertEqual(fields.get("num_agents"), 5)
        self.assertEqual(fields.get("num_patients"), 4)
        self.assertEqual(fields.get("requested_adversary_count"), 2)
        self.assertEqual(fields.get("adversary_count"), 2)
        self.assertEqual(fields.get("target_role"), role)
        self.assertEqual(fields.get("seed"), 7)

    def test_plain_unsuffixed_id(self) -> None:
        fields = _infer(BASE.format(role="Patient", suffix=""))
        self.assert_common_fields(fields, role="Patient")
        self.assertIsNone(fields.get("disable_post_message_after_round"))
        self.assertFalse(fields.get("post_message_restricted"))

    def test_nopm_suffix(self) -> None:
        fields = _infer(BASE.format(role="Patient", suffix="__nopm"))
        self.assert_common_fields(fields, role="Patient")
        self.assertEqual(fields.get("disable_post_message_after_round"), 0)
        self.assertTrue(fields.get("post_message_restricted"))

    def test_cut_after_round_suffix(self) -> None:
        fields = _infer(BASE.format(role="Patient", suffix="__cut_after_r3"))
        self.assert_common_fields(fields, role="Patient")
        self.assertEqual(fields.get("disable_post_message_after_round"), 3)
        self.assertTrue(fields.get("post_message_restricted"))

    def test_legacy_strat_segment_without_suffix(self) -> None:
        run_id = (
            "qwen__sweepA__topofully_connected__stratdeception__"
            "agents5_patients4_adv2_Patient_seed7"
        )
        fields = _infer(run_id)
        self.assert_common_fields(fields, role="Patient")
        self.assertIsNone(fields.get("disable_post_message_after_round"))
        self.assertFalse(fields.get("post_message_restricted"))

    def test_role_with_underscores_unsuffixed(self) -> None:
        fields = _infer(BASE.format(role="Resource_Provisioner", suffix=""))
        self.assert_common_fields(fields, role="Resource_Provisioner")
        self.assertIsNone(fields.get("disable_post_message_after_round"))

    def test_role_with_underscores_suffixed(self) -> None:
        fields = _infer(
            BASE.format(role="Resource_Provisioner", suffix="__cut_after_r5")
        )
        self.assert_common_fields(fields, role="Resource_Provisioner")
        self.assertEqual(fields.get("disable_post_message_after_round"), 5)

    def test_v1_id_still_parses_without_suffix_fields(self) -> None:
        # V1 form (run._build_run_id_v1): no topo/patients segment, no suffix.
        run_id = "qwen__sweepA__agents5_adv2_Resource_Provisioner_seed7"
        fields = _infer(run_id)
        self.assertEqual(fields.get("model_label"), "qwen")
        self.assertEqual(fields.get("sweep_name"), "sweepA")
        self.assertEqual(fields.get("num_agents"), 5)
        self.assertEqual(fields.get("adversary_count"), 2)
        self.assertEqual(fields.get("target_role"), "Resource_Provisioner")
        self.assertEqual(fields.get("seed"), 7)
        self.assertIsNone(fields.get("disable_post_message_after_round"))
        self.assertFalse(fields.get("post_message_restricted"))


if __name__ == "__main__":
    unittest.main()
