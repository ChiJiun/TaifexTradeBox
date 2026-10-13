from __future__ import annotations

import unittest
from unittest.mock import patch

from automation.check_docs_consistency import check, render_cli_table
from sim_hft_bot import parse_args


class DocumentationConsistencyTests(unittest.TestCase):
    def test_operational_runbook_agrees_with_actual_cli_defaults(self):
        self.assertEqual(check(), [])
        table = render_cli_table()
        self.assertIn("`--stale-fatal-seconds`", table)
        self.assertIn("`--flatten-on-shutdown`", table)
        self.assertIn("`--point-value`", table)

    def test_checker_detects_cli_default_change(self):
        args = parse_args(["--symbol", "TMFJ6"])
        args.quote_stale_seconds = 10.0
        with patch("automation.check_docs_consistency.parse_args", return_value=args):
            problems = check()
        self.assertTrue(any("defaults differ" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
