"""Tests for tool-call argument sanitization / truncation repair.

Regression coverage for the 2026-10-24 incident: a ``max_tokens`` cap truncated
GLM-5.3-Flash mid-tool-call, leaving an invalid-JSON ``function.arguments``
string that was persisted in the conversation history and rejected by the
endpoint on the follow-up request (HTTP 400 "Assistant tool call
function.arguments must be valid JSON").

These exercise the pure helpers ``_repair_truncated_json`` and
``_sanitize_tool_call_arguments`` plus the async
``CustomOAICompatibleClient.process_tool_calls``. No network or LLM call is
involved.
"""

from __future__ import annotations

import copy
import json
import unittest

from experiments.common._patch_terrarium import (
    CustomOAICompatibleClient,
    _repair_truncated_json,
    _sanitize_tool_call_arguments,
)


def _assistant_message(arguments, *, tool_id="call_1", name="post_message"):
    """Build a minimal assistant message with a single tool call."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


class RepairTruncatedJsonTests(unittest.TestCase):
    def test_already_valid_is_returned_unchanged(self) -> None:
        text = '{"message": "hello", "channel": "all"}'
        self.assertEqual(_repair_truncated_json(text), text)

    def test_truncated_string_value_is_repaired(self) -> None:
        truncated = '{"message": "SURPLUS IV_Kits at St_Marys'
        repaired = _repair_truncated_json(truncated)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertIn("message", parsed)
        # The visible prefix of the cut-off value is preserved.
        self.assertTrue(parsed["message"].startswith("SURPLUS IV_Kits at St_Marys"))

    def test_truncated_dangling_key_is_repaired(self) -> None:
        repaired = _repair_truncated_json('{"a": 1, "b')
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["a"], 1)

    def test_unrepairable_prose_returns_none(self) -> None:
        self.assertIsNone(_repair_truncated_json("not json at all"))

    def test_truncated_nested_structure_is_repaired(self) -> None:
        repaired = _repair_truncated_json('{"a": {"b": [1, 2')
        self.assertIsNotNone(repaired)
        self.assertEqual(json.loads(repaired)["a"]["b"], [1, 2])

    def test_trailing_backslash_inside_string_is_repaired(self) -> None:
        repaired = _repair_truncated_json('{"a": "ends with a backslash \\')
        self.assertIsNotNone(repaired)
        self.assertIn("a", json.loads(repaired))


class SanitizeToolCallArgumentsTests(unittest.TestCase):
    def test_valid_string_arguments_kept_byte_identical(self) -> None:
        raw = '{"channel": "all", "message": "hi"}'
        message = _assistant_message(raw)
        sanitized, notes = _sanitize_tool_call_arguments(message)
        self.assertEqual(sanitized["tool_calls"][0]["function"]["arguments"], raw)
        self.assertEqual(notes, [])

    def test_missing_arguments_becomes_empty_object(self) -> None:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "post_message"}}
            ],
        }
        sanitized, notes = _sanitize_tool_call_arguments(message)
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(args, "{}")
        self.assertEqual(json.loads(args), {})
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["status"], "missing_arguments")
        self.assertEqual(notes[0]["tool"], "post_message")
        self.assertEqual(notes[0]["raw"], "")

    def test_empty_string_arguments_becomes_empty_object(self) -> None:
        sanitized, _ = _sanitize_tool_call_arguments(_assistant_message(""))
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(args, "{}")
        self.assertEqual(json.loads(args), {})

    def test_truncated_arguments_are_repaired(self) -> None:
        trunc = '{"message": "SURPLUS IV_Kits at St_Marys'
        sanitized, notes = _sanitize_tool_call_arguments(_assistant_message(trunc))
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args)  # must not raise
        self.assertTrue(parsed["message"].startswith("SURPLUS IV_Kits at St_Marys"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["status"], "repaired")
        self.assertEqual(notes[0]["raw"], trunc)

    def test_unrepairable_arguments_dropped_to_empty_object(self) -> None:
        sanitized, notes = _sanitize_tool_call_arguments(
            _assistant_message("not json at all")
        )
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(args, "{}")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["status"], "dropped")
        self.assertEqual(notes[0]["raw"], "not json at all")

    def test_dict_arguments_are_reserialized(self) -> None:
        message = _assistant_message({"channel": "all", "message": "hi"})
        sanitized, notes = _sanitize_tool_call_arguments(message)
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(args, str)
        self.assertEqual(json.loads(args), {"channel": "all", "message": "hi"})
        self.assertEqual(notes, [])

    def test_non_string_non_dict_arguments_dropped(self) -> None:
        sanitized, notes = _sanitize_tool_call_arguments(_assistant_message([1, 2, 3]))
        args = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(args, "{}")
        self.assertEqual(notes[0]["status"], "dropped")

    def test_input_message_is_not_mutated(self) -> None:
        message = _assistant_message('{"message": "SURPLUS IV_Kits at St_Marys')
        before = copy.deepcopy(message)
        _sanitize_tool_call_arguments(message)
        self.assertEqual(message, before)

    def test_no_keys_added_to_message(self) -> None:
        message = _assistant_message('{"channel": "all"}')
        message["reasoning_content"] = "thinking..."
        sanitized, _ = _sanitize_tool_call_arguments(message)
        self.assertEqual(set(sanitized.keys()), set(message.keys()))
        self.assertEqual(sanitized["reasoning_content"], "thinking...")

    def test_truncated_and_clean_calls_in_same_message(self) -> None:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "good",
                    "type": "function",
                    "function": {"name": "post_message", "arguments": '{"a": 1}'},
                },
                {
                    "id": "bad",
                    "type": "function",
                    "function": {
                        "name": "broadcast_message",
                        "arguments": '{"message": "cut off here',
                    },
                },
            ],
        }
        sanitized, notes = _sanitize_tool_call_arguments(message)
        for call in sanitized["tool_calls"]:
            json.loads(call["function"]["arguments"])  # every one must parse
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["tool"], "broadcast_message")
        self.assertEqual(notes[0]["status"], "repaired")


class ProcessToolCallsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_client() -> CustomOAICompatibleClient:
        # Bypass __init__ (it resolves env/credentials and opens a session);
        # process_tool_calls touches none of that.
        return CustomOAICompatibleClient.__new__(CustomOAICompatibleClient)

    @staticmethod
    def _response(message, *, finish_reason="length"):
        return {
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ]
        }

    async def test_repaired_call_is_executed_and_history_is_valid(self) -> None:
        client = self._make_client()
        truncated = '{"message": "SURPLUS IV_Kits at St_Marys'
        message = _assistant_message(truncated, tool_id="call_repaired")
        response = self._response(message)
        context: list = []

        calls: list = []

        async def callback(tool_name, arguments):
            calls.append((tool_name, arguments))
            return {"status": "ok"}

        executed, new_context, step_tools = await client.process_tool_calls(
            response, context, callback
        )

        self.assertEqual(executed, 1)
        # (a) the assistant message in the context parses as JSON in args.
        assistant = new_context[0]
        args_str = assistant["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args_str)  # must not raise
        self.assertTrue(parsed["message"].startswith("SURPLUS IV_Kits at St_Marys"))
        # (b) the callback saw the repaired arguments.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "post_message")
        self.assertTrue(
            calls[0][1]["message"].startswith("SURPLUS IV_Kits at St_Marys")
        )
        # (c) a matching tool result was appended.
        self.assertEqual(new_context[1]["role"], "tool")
        self.assertEqual(new_context[1]["tool_call_id"], "call_repaired")
        # Marker recorded for observability.
        self.assertIn("ARGS_REPAIRED_FROM_TRUNCATION", step_tools[0])

    async def test_dropped_call_is_not_executed_but_history_stays_valid(self) -> None:
        client = self._make_client()
        message = _assistant_message("not json at all", tool_id="call_dropped")
        response = self._response(message)
        context: list = []

        calls: list = []

        async def callback(tool_name, arguments):
            calls.append((tool_name, arguments))
            return {"status": "ok"}

        executed, new_context, step_tools = await client.process_tool_calls(
            response, context, callback
        )

        # (b) the callback was NOT invoked for the dropped call.
        self.assertEqual(calls, [])
        self.assertEqual(executed, 0)
        # (a) the assistant args still parse.
        json.loads(new_context[0]["tool_calls"][0]["function"]["arguments"])
        # (c) a matching tool message is still present (pairing stays valid).
        self.assertEqual(new_context[1]["role"], "tool")
        self.assertEqual(new_context[1]["tool_call_id"], "call_dropped")
        payload = json.loads(new_context[1]["content"])
        self.assertEqual(payload["status"], "error")
        self.assertIn("dropped", payload["result"])
        self.assertIn("ARGS_DROPPED_TRUNCATED", step_tools[0])

    async def test_missing_arguments_are_executed_with_empty_object(self) -> None:
        client = self._make_client()
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "post_message"},
                }
            ],
        }
        context: list = []
        calls: list = []

        async def callback(tool_name, arguments):
            calls.append((tool_name, arguments))
            return {"status": "ok"}

        executed, new_context, _ = await client.process_tool_calls(
            self._response(message, finish_reason="tool_calls"), context, callback
        )
        self.assertEqual(executed, 1)
        self.assertEqual(calls, [("post_message", {})])
        self.assertEqual(new_context[1]["tool_call_id"], "call_missing")

    async def test_does_not_mutate_input_message(self) -> None:
        client = self._make_client()
        message = _assistant_message("not json at all")
        before = copy.deepcopy(message)
        context: list = []

        async def callback(tool_name, arguments):
            return {}

        await client.process_tool_calls(self._response(message), context, callback)
        self.assertEqual(message, before)


if __name__ == "__main__":
    unittest.main()
