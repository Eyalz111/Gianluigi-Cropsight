"""Tests for core/cost_calculator.py."""

from core.cost_calculator import compute_cost_summary, _calc_record_cost, _get_pricing


class TestGetPricing:
    def test_known_model(self):
        p = _get_pricing("claude-opus-4-6")
        assert p["input"] == 15.0
        assert p["output"] == 75.0

    def test_partial_match(self):
        p = _get_pricing("some-opus-model")
        assert p["input"] == 15.0  # Matches "opus"

    def test_unknown_defaults_to_sonnet(self):
        p = _get_pricing("totally-unknown-model")
        assert p["input"] == 3.0  # Default = Sonnet pricing


class TestCalcRecordCost:
    def test_basic_cost(self):
        record = {
            "model": "claude-opus-4-6",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        cost = _calc_record_cost(record)
        assert cost == 15.0  # 1M input tokens * $15/MTok

    def test_output_cost(self):
        record = {
            "model": "claude-opus-4-6",
            "input_tokens": 0,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        cost = _calc_record_cost(record)
        assert cost == 75.0

    def test_cache_read_discount(self):
        record = {
            "model": "claude-opus-4-6",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 1_000_000,
            "cache_creation_tokens": 0,
        }
        cost = _calc_record_cost(record)
        assert cost == 1.5  # 1M * $1.50/MTok (0.1x of $15)

    def test_cache_write_premium(self):
        record = {
            "model": "claude-opus-4-6",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 1_000_000,
        }
        cost = _calc_record_cost(record)
        assert cost == 18.75  # 1M * $18.75/MTok (1.25x of $15)

    def test_combined_cost(self):
        record = {
            "model": "claude-haiku-4-5",
            "input_tokens": 500_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 200_000,
            "cache_creation_tokens": 0,
        }
        cost = _calc_record_cost(record)
        expected = (
            (500_000 / 1_000_000) * 0.80   # input
            + (100_000 / 1_000_000) * 4.0   # output
            + (200_000 / 1_000_000) * 0.08  # cache_read
        )
        assert abs(cost - expected) < 0.001

    def test_null_tokens_handled(self):
        record = {
            "model": "claude-sonnet-4-6",
            "input_tokens": None,
            "output_tokens": None,
        }
        cost = _calc_record_cost(record)
        assert cost == 0.0


class TestComputeCostSummary:
    def test_empty_records(self):
        result = compute_cost_summary([])
        assert result["total_cost"] == 0.0
        assert result["record_count"] == 0

    def test_by_model_breakdown(self):
        records = [
            {"model": "claude-opus-4-6", "input_tokens": 1000, "output_tokens": 500,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "extraction",
             "created_at": "2026-03-22T10:00:00Z"},
            {"model": "claude-haiku-4-5", "input_tokens": 2000, "output_tokens": 300,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "router",
             "created_at": "2026-03-22T11:00:00Z"},
        ]
        result = compute_cost_summary(records)
        assert "claude-opus-4-6" in result["by_model"]
        assert "claude-haiku-4-5" in result["by_model"]
        assert result["record_count"] == 2

    def test_by_call_site_sorted_by_cost(self):
        records = [
            {"model": "claude-opus-4-6", "input_tokens": 100000, "output_tokens": 10000,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "extraction",
             "created_at": "2026-03-22T10:00:00Z"},
            {"model": "claude-haiku-4-5", "input_tokens": 1000, "output_tokens": 100,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "router",
             "created_at": "2026-03-22T10:00:00Z"},
        ]
        result = compute_cost_summary(records)
        sites = list(result["by_call_site"].keys())
        assert sites[0] == "extraction"  # Higher cost first

    def test_daily_trend(self):
        records = [
            {"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "test",
             "created_at": "2026-03-21T10:00:00Z"},
            {"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "call_site": "test",
             "created_at": "2026-03-22T10:00:00Z"},
        ]
        result = compute_cost_summary(records)
        assert len(result["daily_trend"]) == 2
        assert result["daily_trend"][0]["date"] == "2026-03-21"
        assert result["daily_trend"][1]["date"] == "2026-03-22"
