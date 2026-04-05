"""Tests for per-application cost tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import _compute_cost_usd  # noqa: E402


class TestComputeCostUsd:
    def test_zero_tokens(self):
        assert _compute_cost_usd(0, 0) == 0.0

    def test_typical_application(self):
        # 5000 input, 1000 output tokens
        # (5000 * 2.40 / 1_000_000) + (1000 * 12.00 / 1_000_000)
        # = 0.012 + 0.012 = 0.024
        result = _compute_cost_usd(5000, 1000)
        assert result == 0.024

    def test_large_token_count(self):
        # 100_000 input, 20_000 output
        # (100_000 * 2.40 / 1_000_000) + (20_000 * 12.00 / 1_000_000)
        # = 0.24 + 0.24 = 0.48
        result = _compute_cost_usd(100_000, 20_000)
        assert result == 0.48

    def test_rounds_to_four_decimals(self):
        # 1 input, 1 output
        # (1 * 2.40 / 1_000_000) + (1 * 12.00 / 1_000_000)
        # = 0.0000024 + 0.000012 = 0.0000144 -> rounds to 0.0
        result = _compute_cost_usd(1, 1)
        assert result == 0.0

    def test_output_heavy(self):
        # 0 input, 10_000 output
        # 0 + (10_000 * 12.00 / 1_000_000) = 0.12
        result = _compute_cost_usd(0, 10_000)
        assert result == 0.12
