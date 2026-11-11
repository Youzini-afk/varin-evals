import unittest

from varin_evals.trajectory import usage_totals


class UsageTests(unittest.TestCase):
    def row(self, complete, cached, output):
        counter = lambda value: {"known": str(value), "missing": 0}
        return {"completeInputTokens": counter(complete), "cachedInputTokens": counter(cached),
                "outputTokens": counter(output), "costUsd": 0.1, "costUnknownRequests": 0}

    def test_total_includes_child_and_auxiliary_rows_without_adding_cache_twice(self):
        usage = usage_totals({"rows": [self.row(100, 80, 20), self.row(50, 30, 10)]})
        self.assertEqual(usage, {"n_input_tokens": 150, "n_cache_tokens": 110, "n_output_tokens": 30, "cost_usd": 0.2})

    def test_missing_count_and_unpriced_requests_remain_unknown(self):
        row = self.row(100, 0, 20)
        row["completeInputTokens"]["missing"] = 1
        row["costUnknownRequests"] = 1
        usage = usage_totals({"rows": [row]})
        self.assertIsNone(usage["n_input_tokens"])
        self.assertIsNone(usage["cost_usd"])
        self.assertEqual(usage["n_output_tokens"], 20)

    def test_absent_receipts_do_not_become_zero_cost(self):
        self.assertTrue(all(value is None for value in usage_totals({"rows": []}).values()))


if __name__ == "__main__":
    unittest.main()
