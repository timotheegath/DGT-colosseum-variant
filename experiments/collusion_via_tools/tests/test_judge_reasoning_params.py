"""Tests for the belief-judge params builder.

``_build_judge_params`` is the single place that turns a ``JudgeConfig`` into
the ``params`` dict handed to the judge client's ``generate_response``. Until it
existed, the judge built its params inline from only ``model``,
``max_output_tokens`` and ``temperature``, so ``reasoning_effort`` (set in the
YAML ``judge_llm`` block) never reached the client.

No network or LLM call is involved.
"""

from __future__ import annotations

import unittest

from experiments.collusion_via_tools.judge_beliefs import (
    JudgeConfig,
    _build_judge_params,
)


class JudgeConfigDefaultsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = JudgeConfig()
        self.assertEqual(cfg.model, "gpt-4")
        self.assertEqual(cfg.max_tokens, 512)
        self.assertEqual(cfg.temperature, 0.0)
        self.assertIsNone(cfg.reasoning_effort)


class BuildJudgeParamsTests(unittest.TestCase):
    def test_core_keys_always_present(self) -> None:
        params = _build_judge_params(
            JudgeConfig(model="some-model", max_tokens=256, temperature=0.3)
        )
        self.assertEqual(params["model"], "some-model")
        self.assertEqual(params["max_output_tokens"], 256)
        self.assertEqual(params["temperature"], 0.3)
        self.assertEqual(params["tools"], [])

    def test_reasoning_effort_absent_when_none(self) -> None:
        params = _build_judge_params(JudgeConfig())
        self.assertNotIn("reasoning_effort", params)

    def test_reasoning_effort_included_when_set(self) -> None:
        params = _build_judge_params(JudgeConfig(reasoning_effort="high"))
        self.assertEqual(params["reasoning_effort"], "high")

    def test_reasoning_effort_absent_when_empty_string(self) -> None:
        params = _build_judge_params(JudgeConfig(reasoning_effort=""))
        self.assertNotIn("reasoning_effort", params)

    def test_max_output_tokens_mirrors_max_tokens(self) -> None:
        params = _build_judge_params(JudgeConfig(max_tokens=2048))
        self.assertEqual(params["max_output_tokens"], 2048)

    def test_max_output_tokens_is_int_coerced(self) -> None:
        params = _build_judge_params(JudgeConfig(max_tokens=2048.0))
        self.assertIsInstance(params["max_output_tokens"], int)
        self.assertEqual(params["max_output_tokens"], 2048)

    def test_temperature_is_float_coerced(self) -> None:
        params = _build_judge_params(JudgeConfig(temperature=1))
        self.assertIsInstance(params["temperature"], float)
        self.assertEqual(params["temperature"], 1.0)

    def test_all_requested_fields_together(self) -> None:
        params = _build_judge_params(
            JudgeConfig(
                model="zai-org/GLM-5.3-Flash",
                max_tokens=2048,
                temperature=0.0,
                reasoning_effort="high",
            )
        )
        self.assertEqual(
            params,
            {
                "model": "zai-org/GLM-5.3-Flash",
                "max_output_tokens": 2048,
                "temperature": 0.0,
                "tools": [],
                "reasoning_effort": "high",
            },
        )


if __name__ == "__main__":
    unittest.main()
