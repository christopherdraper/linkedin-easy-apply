"""Tests for per-application cost tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from types import SimpleNamespace  # noqa: E402

import jobapply.stats as stats  # noqa: E402
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


def _usage(inp=0, out=0, write=0, read=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=write,
        cache_read_input_tokens=read,
    )


class TestPerModelCost:
    def test_sonnet_5_5_rates(self):
        # 1M in at $2 + 100k out at $10
        assert stats.usage_cost_usd(_usage(1_000_000, 100_000), "claude-sonnet-5-5") == 3.0

    def test_haiku_dated_id_uses_haiku_rates(self):
        cost = stats.usage_cost_usd(_usage(1_000_000, 1_000_000), "claude-haiku-4-5-20251001")
        assert cost == 6.0

    def test_cache_tokens_are_billed_at_cache_rates(self):
        # Sonnet 5.5: 1M cache writes at $2.50 + 1M cache reads at $0.20
        cost = stats.usage_cost_usd(_usage(write=1_000_000, read=1_000_000), "claude-sonnet-5-5")
        assert round(cost, 6) == 2.7

    def test_unknown_model_is_priced_as_sonnet(self):
        assert stats.usage_cost_usd(_usage(1_000_000), "claude-future-9") == 2.0

    def test_missing_cache_fields_count_as_zero(self):
        usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0)
        assert stats.usage_cost_usd(usage, "claude-haiku-4-5") == 1.0

    def test_add_ai_tokens_accumulates_and_reset_clears(self, monkeypatch):
        stats.reset_run_stats()
        stats.add_ai_tokens(_usage(1_000_000, 0), "claude-haiku-4-5")
        stats.add_ai_tokens(_usage(0, 100_000), "claude-sonnet-5-5")
        assert round(stats._ai_cost_usd, 6) == 2.0
        assert stats._ai_tokens_in == 1_000_000 and stats._ai_tokens_out == 100_000
        stats.reset_run_stats()
        assert stats._ai_cost_usd == 0.0
