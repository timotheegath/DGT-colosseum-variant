"""Tests for GLM-5.x reasoning-control wiring.

Covers the pure helper ``_build_reasoning_controls``, its merge into
``_build_payload``, and the opt-in ``strip_reasoning_content`` client flag that
keeps a prior turn's ``reasoning_content`` out of the re-posted history.

No network or LLM call is involved.
"""

from __future__ import annotations

import unittest

from experiments.common._patch_terrarium import (
    CustomOAICompatibleClient,
    _build_reasoning_controls,
)

# The module logger emits warnings under this name.
_LOGGER_NAME = "experiments.common._patch_terrarium"


class BuildReasoningControlsTests(unittest.TestCase):
    def test_empty_params_returns_empty_dict(self) -> None:
        self.assertEqual(_build_reasoning_controls({}), {})

    def test_irrelevant_params_returns_empty_dict(self) -> None:
        params = {"model": "m", "max_tokens": 100, "temperature": 0.7}
        self.assertEqual(_build_reasoning_controls(params), {})

    def test_low_is_passed_through_without_warning(self) -> None:
        with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls({"reasoning_effort": "low"})
        self.assertEqual(controls, {"reasoning_effort": "low"})

    def test_high_is_passed_through_without_warning(self) -> None:
        with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls({"reasoning_effort": "high"})
        self.assertEqual(controls, {"reasoning_effort": "high"})

    def test_unrecognized_value_is_passed_through_with_warning(self) -> None:
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as cm:
            controls = _build_reasoning_controls({"reasoning_effort": "max"})
        # Verbatim (not rewritten), but the user is warned it maps to `max`.
        self.assertEqual(controls, {"reasoning_effort": "max"})
        self.assertTrue(any("max" in msg for msg in cm.output))

    def test_non_string_reasoning_effort_is_ignored_with_warning(self) -> None:
        with self.assertLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls({"reasoning_effort": 5})
        self.assertEqual(controls, {})

    def test_none_reasoning_effort_is_ignored_silently(self) -> None:
        # An absent/None key is the common case and must not warn.
        with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls({"reasoning_effort": None})
        self.assertEqual(controls, {})

    def test_clear_thinking_is_coerced_to_bool(self) -> None:
        with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls(
                {"chat_template_kwargs": {"clear_thinking": True}}
            )
        self.assertEqual(controls, {"chat_template_kwargs": {"clear_thinking": True}})

    def test_clear_thinking_truthy_value_is_coerced_to_bool(self) -> None:
        controls = _build_reasoning_controls(
            {"chat_template_kwargs": {"clear_thinking": 1}}
        )
        self.assertEqual(controls, {"chat_template_kwargs": {"clear_thinking": True}})

    def test_chat_template_reasoning_effort_is_passed_through(self) -> None:
        controls = _build_reasoning_controls(
            {"chat_template_kwargs": {"reasoning_effort": "high"}}
        )
        self.assertEqual(
            controls, {"chat_template_kwargs": {"reasoning_effort": "high"}}
        )

    def test_unrecognized_chat_template_keys_are_dropped_with_warning(self) -> None:
        with self.assertLogs(_LOGGER_NAME, level="WARNING"):
            controls = _build_reasoning_controls(
                {"chat_template_kwargs": {"enable_thinking": False}}
            )
        self.assertEqual(controls, {})

    def test_params_are_not_mutated(self) -> None:
        params = {
            "reasoning_effort": "low",
            "chat_template_kwargs": {"clear_thinking": True},
        }
        snapshot = {
            "reasoning_effort": "low",
            "chat_template_kwargs": {"clear_thinking": True},
        }
        _build_reasoning_controls(params)
        self.assertEqual(params, snapshot)


class BuildPayloadIncludesReasoningControlsTests(unittest.TestCase):
    @staticmethod
    def _make_client() -> CustomOAICompatibleClient:
        # _build_payload touches no credentials/session state.
        return CustomOAICompatibleClient.__new__(CustomOAICompatibleClient)

    def test_controls_are_merged_into_payload(self) -> None:
        client = self._make_client()
        params = {
            "model": "zai-org/GLM-5.3-Flash",
            "temperature": 0.7,
            "reasoning_effort": "low",
            "chat_template_kwargs": {"clear_thinking": True},
        }
        payload = client._build_payload([{"role": "user", "content": "hi"}], params)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["chat_template_kwargs"], {"clear_thinking": True})
        # Existing explicit fields are untouched.
        self.assertEqual(payload["model"], "zai-org/GLM-5.3-Flash")
        self.assertEqual(payload["temperature"], 0.7)

    def test_absent_controls_are_not_added(self) -> None:
        client = self._make_client()
        params = {"model": "m", "temperature": 0.7}
        payload = client._build_payload([{"role": "user", "content": "hi"}], params)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("chat_template_kwargs", payload)

    def test_max_tokens_cap_is_honored_under_every_spelling(self) -> None:
        """The completion cap must never be silently dropped.

        The cap travels under three different keys depending on the caller
        (terrarium's agent path supplies all three, the judge supplies
        ``max_output_tokens``, a raw provider params block carries plain
        ``max_tokens``). Dropping it lets the model reason unbounded, which is
        how a run ends up with a 30k-token trace.
        """
        client = self._make_client()
        for key in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
            with self.subTest(key=key):
                params = {"model": "m", key: 32768}
                payload = client._build_payload(
                    [{"role": "user", "content": "hi"}], params
                )
                self.assertEqual(payload["max_tokens"], 32768)

    def test_max_completion_tokens_wins_over_the_other_spellings(self) -> None:
        client = self._make_client()
        params = {
            "model": "m",
            "max_completion_tokens": 2048,
            "max_output_tokens": 1024,
            "max_tokens": 512,
        }
        payload = client._build_payload([{"role": "user", "content": "hi"}], params)
        self.assertEqual(payload["max_tokens"], 2048)

    def test_no_cap_anywhere_means_no_cap_in_the_payload(self) -> None:
        client = self._make_client()
        params = {"model": "m"}
        payload = client._build_payload([{"role": "user", "content": "hi"}], params)
        self.assertNotIn("max_tokens", payload)


class StripReasoningContentTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_client(*, strip: bool) -> CustomOAICompatibleClient:
        client = CustomOAICompatibleClient.__new__(CustomOAICompatibleClient)
        client.strip_reasoning_content = strip
        return client

    @staticmethod
    def _response(message):
        return {
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]
        }

    @staticmethod
    def _message_with_reasoning():
        return {
            "role": "assistant",
            "content": "",
            "reasoning_content": "prior chain-of-thought",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "post_message", "arguments": '{"a": 1}'},
                }
            ],
        }

    async def _run(self, *, strip: bool):
        client = self._make_client(strip=strip)
        message = self._message_with_reasoning()
        context: list = []

        async def callback(tool_name, arguments):
            return {"status": "ok"}

        await client.process_tool_calls(self._response(message), context, callback)
        return message, context

    async def test_strip_true_removes_reasoning_content_from_context(self) -> None:
        _, context = await self._run(strip=True)
        assistant = context[0]
        self.assertNotIn("reasoning_content", assistant)

    async def test_default_keeps_reasoning_content_in_context(self) -> None:
        _, context = await self._run(strip=False)
        assistant = context[0]
        self.assertEqual(assistant["reasoning_content"], "prior chain-of-thought")

    async def test_strip_true_does_not_mutate_returned_message(self) -> None:
        message, _ = await self._run(strip=True)
        # The original message (returned to the caller / used for logging) keeps
        # its reasoning_content.
        self.assertEqual(message["reasoning_content"], "prior chain-of-thought")


if __name__ == "__main__":
    unittest.main()
