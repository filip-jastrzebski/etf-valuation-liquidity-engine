"""Unit tests for data quality validators."""

import pytest
from src.quality.validators import BasketValidator, MarketDataValidator


class TestBasketValidator:

    def test_valid_basket_returns_no_issues(self):
        """A perfectly weighted basket should pass without issues."""
        valid_basket = {
            "ticker": "XLF",
            "constituents": [
                {"ticker": "JPM", "weight": 0.200000},
                {"ticker": "BAC", "weight": 0.300000},
                {"ticker": "BRK-B", "weight": 0.500000},
            ]
        }
        issues = BasketValidator.validate_basket(valid_basket)
        assert len(issues) == 0


    def test_empty_basket_is_flagged(self):
        """A basket with no constituents should be rejected"""
        empty_basket = {"ticker": "BAD_ETF", "constituents": []}
        issues = BasketValidator.validate_basket(empty_basket)

        assert len(issues) == 1
        assert issues[0].error_code == "ERR_EMPTY_BASKET"


    def test_duplicate_constituents(self):
        """Duplicate tickers in the same basket should be caught."""
        duplicate_basket = {
            "ticker": "DUP_ETF",
            "constituents": [
                {"ticker": "AAPL", "weight": 0.5},
                {"ticker": "AAPL", "weight": 0.5},
            ]
        }
        issues = BasketValidator.validate_basket(duplicate_basket)

        # Should flag duplicate AND possibly weight mismatch depending on order.
        # But we definetely expect ERR_DUPLICATE_CONSTITUENT
        error_codes = [issue.error_code for issue in issues]
        assert "ERR_DUPLICATE_CONSTITUENT" in error_codes


    def test_invalid_weight_bounds(self):
        """Negative weights or weights > 1.0 are invalid"""
        bad_weight_basket = {
            "ticker": "BAD_WEIGHT",
            "constituents": [
                {"ticker": "A", "weight": 1.2},
                {"ticker": "B", "weight": -0.2},
            ]
        }
        issues = BasketValidator.validate_basket(bad_weight_basket)
        error_codes = [issue.error_code for issue in issues]

        # We expect 2 bounds errors
        assert error_codes.count("ERR_INVALID_WEIGHT_BOUNDS") == 2


    def test_weight_mismatch(self):
        """Weights not summing to exactly 1.0 should be flagged"""
        mismatch_basket = {
            "ticker": "MISMATCH_WEIGHTS",
            "constituents": [
                {"ticker": "A", "weight": 0.50},
                {"ticker": "B", "weight": 0.49},
            ]
        }
        issues = BasketValidator.validate_basket(mismatch_basket)
        assert len(issues) == 1
        assert issues[0].error_code == "ERR_TOTAL_WEIGHT_MISMATCH"


class TestMarketDataValidator:

    def test_valid_price_row(self):
        """A valid market data row should pass without issues."""
        valid_row = {
            "ticker": "AAPL",
            "date": "2023-01-01",
            "open": 170.0,
            "high": 172.5,
            "low": 169.0,
            "close": 171.0,
            "volume": 1000000
        }
        issues = MarketDataValidator.validate_price_row(valid_row)
        assert len(issues) == 0


    def test_missing_required_field(self):
        """If a required field is missing, it should be flagged."""
        missing_row = {
            "ticker": "AAPL",
            "date": "2023-01-01",
            "open": 170.0,
            # "high" is missing
            "low": 169.0,
            "close": 171.0,
            "volume": 1000000
        }
        issues = MarketDataValidator.validate_price_row(missing_row)
        assert len(issues) == 1
        assert issues[0].error_code == "ERR_MISSING_VALUE"


    def test_negative_prices_or_volume(self):
        """If price or volume negative, it should be flagged."""
        negative_row = {
            "ticker": "AAPL",
            "date": "2023-01-01",
            "open": -5.0,
            "high": 172.5,
            "low": 169.0,
            "close": 171.0,
            "volume": -100
        }
        issues = MarketDataValidator.validate_price_row(negative_row)
        error_codes = [issue.error_code for issue in issues]
        assert "ERR_NON_POSITIVE_PRICE" in error_codes
        assert "ERR_NEGATIVE_VOLUME" in error_codes


    def test_ohlc_inconsitency(self):
        """If high < low or open/close outside of high-low range, it should be flagged."""
        inconsistent_row = {
            "ticker": "AAPL",
            "date": "2023-01-01",
            "open": 170.0,
            "high": 169.0,  # High lower than Open/Close/Low
            "low": 180.0,   # Low higher than Open/Close/High
            "close": 172.0,
            "volume": 1000000
        }
        issues = MarketDataValidator.validate_price_row(inconsistent_row)
        error_codes = [issue.error_code for issue in issues]
        assert "ERR_OHLC_INCONSISTENCY" in error_codes


    def test_extreme_intraday_swing(self):
        """If intraday swing big (> 50%) it should be flagged."""
        extreme_row = {
            "ticker": "AAPL",
            "date": "2023-01-01",
            "open": 100.0,
            "high": 105.5,
            "low": 35.0,
            "close": 40.0, # Price drops from 100 to 40 (60%, limit is 50%)
            "volume": 1000000
        }
        issues = MarketDataValidator.validate_price_row(extreme_row)
        error_codes = [issue.error_code for issue in issues]
        assert "ERR_EXTREME_PRICE_SWING" in error_codes

