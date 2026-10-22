"""Tests for the execution-phase `post_message` restriction decision.

The coordination window spans planning rounds ``1..N`` (``N =
disable_post_message_after_round``) and closes at the end of round ``N``. Once
it has closed, `post_message` must stay disabled while actions are executed --
including the shipped ``max_planning_rounds: 1`` / ``N = 1`` configuration,
where the window closes exactly at the end of the single planning round.
"""

import unittest

from experiments.collusion_via_tools.run import _post_message_disabled_in_execution


class PostMessageDisabledInExecutionTests(unittest.TestCase):
    def test_table(self) -> None:
        # (planning_rounds, resolved, expected_disabled). `resolved` is the
        # resolved `disable_post_message_after_round` (N).
        cases = [
            (1, 0, True),  # N == 0 -> disabled from the start, stays disabled
            (1, 1, True),  # shipped config (max_planning_rounds=1, N=1)
            (1, 2, False),  # N > planning_rounds -> window still open
            (2, 2, True),  # window does not extend past the planning phase
            (2, 3, False),  # N > planning_rounds -> window still open
            (3, 3, True),  # window closes at the last planning round
            (1, None, False),  # unrestricted baseline
            (5, None, False),  # unrestricted baseline, any planning_rounds
        ]
        for planning_rounds, resolved, expected in cases:
            with self.subTest(planning_rounds=planning_rounds, resolved=resolved):
                self.assertIs(
                    _post_message_disabled_in_execution(resolved, planning_rounds),
                    expected,
                )

    def test_none_never_restricts(self) -> None:
        for planning_rounds in range(0, 6):
            with self.subTest(planning_rounds=planning_rounds):
                self.assertFalse(
                    _post_message_disabled_in_execution(None, planning_rounds)
                )


if __name__ == "__main__":
    unittest.main()
