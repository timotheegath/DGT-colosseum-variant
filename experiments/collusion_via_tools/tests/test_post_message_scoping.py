"""Task-local scoping tests for the ``post_message`` restriction.

These tests exercise the *real* monkey-patch in
``experiments.common._patch_terrarium`` (importing the module installs it) and
`:class:`terrarium.toolset_discovery.ToolsetDiscovery` directly. No network or
LLM call is involved.

Call-path note (why a :class:`contextvars.ContextVar` is sufficient here):
``_run_single`` awaits ``protocol.agent_planning_turn`` /
``agent_execution_turn`` -> ``BaseAgent.generate_response`` ->
``BaseAgent._multi_step_response_generation``, which calls
``toolset_discovery.get_tools_for_blackboard(phase)`` *in the same coroutine*
(``terrarium/agents/base.py:328``). ``BaseAgent._execute_tool_call`` calls
``get_blackboard_tool_names()`` (``terrarium/agents/base.py:177``) and every
shipped client's ``process_tool_calls`` awaits the callback directly rather than
offloading it. So the flag is read in the same asyncio task that set it.
"""

from __future__ import annotations

import asyncio
import unittest

from terrarium.toolset_discovery import ToolsetDiscovery

from experiments.common._patch_terrarium import (
    is_post_message_disabled,
    set_post_message_disabled,
)


class PostMessageScopingTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        # Leave no residue for the next test in this (main) context.
        set_post_message_disabled(False)

    async def test_concurrent_tasks_do_not_crosstalk(self) -> None:
        a_wrote = asyncio.Event()
        b_wrote = asyncio.Event()

        async def task_a() -> bool:
            set_post_message_disabled(True)
            a_wrote.set()
            await b_wrote.wait()
            return is_post_message_disabled()

        async def task_b() -> bool:
            set_post_message_disabled(False)
            b_wrote.set()
            await a_wrote.wait()
            return is_post_message_disabled()

        observed_a, observed_b = await asyncio.gather(task_a(), task_b())

        # Each task reads back its own latest write, not the other task's.
        self.assertTrue(observed_a)
        self.assertFalse(observed_b)
        # ...and neither write leaked into the parent context.
        self.assertFalse(is_post_message_disabled())

    async def test_discovery_is_task_scoped(self) -> None:
        async def disabled_task() -> tuple:
            set_post_message_disabled(True)
            discovery = ToolsetDiscovery()
            # Yield so the other task interleaves with the flag set.
            await asyncio.sleep(0)
            return (
                discovery.get_tools_for_blackboard("planning"),
                discovery.get_blackboard_tool_names(),
            )

        async def enabled_task() -> tuple:
            # Runs in its own (default) context: must NOT observe the other
            # task's disablement.
            await asyncio.sleep(0)
            discovery = ToolsetDiscovery()
            return (
                discovery.get_tools_for_blackboard("planning"),
                discovery.get_blackboard_tool_names(),
            )

        (d_tools, d_names), (e_tools, e_names) = await asyncio.gather(
            disabled_task(), enabled_task()
        )

        self.assertEqual(d_tools, [])
        self.assertEqual(d_names, set())

        self.assertTrue(e_tools)
        self.assertEqual(e_names, {"post_message"})


class PostMessageDiscoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_post_message_disabled(False)

    def test_default_context_returns_original_tools(self) -> None:
        discovery = ToolsetDiscovery()
        tools = discovery.get_tools_for_blackboard("planning")
        self.assertTrue(tools)
        self.assertEqual(tools[0]["function"]["name"], "post_message")
        self.assertEqual(discovery.get_blackboard_tool_names(), {"post_message"})

    def test_disabled_context_returns_empty(self) -> None:
        set_post_message_disabled(True)
        discovery = ToolsetDiscovery()
        self.assertEqual(discovery.get_tools_for_blackboard("planning"), [])
        self.assertEqual(discovery.get_tools_for_blackboard("execution"), [])
        self.assertEqual(discovery.get_blackboard_tool_names(), set())

    def test_toggle_restores_original_tools(self) -> None:
        discovery = ToolsetDiscovery()
        set_post_message_disabled(True)
        self.assertEqual(discovery.get_tools_for_blackboard("planning"), [])
        set_post_message_disabled(False)
        self.assertTrue(discovery.get_tools_for_blackboard("planning"))
        self.assertEqual(discovery.get_blackboard_tool_names(), {"post_message"})


if __name__ == "__main__":
    unittest.main()
