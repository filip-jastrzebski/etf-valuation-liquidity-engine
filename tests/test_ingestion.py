"""Unit tests for ingestion engine logic and date dimension generation."""

from datetime import datetime
from unittest.mock import MagicMock
import pytest
import pandas as pd
from src.ingestion.market_data import IngestionEngine
from src.ingestion.macro_data import MacroDataIngestion
from src.quality.validators import ValidationIssue, MarketDataValidator


class TestIngestionLogic:

    @pytest.fixture
    def mock_engine_instance(self, tmp_path):
        """Creates a lightweight IngestionEngine instance with dummy config."""
        dummy_config = tmp_path / "dummy_basket.json"
        dummy_config.write_text('{"etfs": []}', encoding="utf-8")

        mock_db_engine = MagicMock()
        return IngestionEngine(engine=mock_db_engine, config_path=dummy_config)

    def test_ensure_date_dim_weekday_is_trading_dat(self, mock_engine_instance):
        """A wednesday should be correctly flagges as a trading day"""
        # 2024-03-27 is a Wednesday (isoweekday = 3)
        dt = datetime(2024, 3, 27)
        mock_conn = MagicMock()

        date_id = mock_engine_instance._ensure_date_dim(mock_conn, dt)

        assert date_id == 20240327
        mock_conn.execute.assert_called_once()

        # Verify passed parameters
        call_args = mock_conn.execute.call_args[0][1]
        assert call_args["date_id"] == 20240327
        assert call_args["is_trading_day"] is True
        assert call_args["day_of_week"] == 3
        assert call_args["quarter"] == 1

    def test_ensure_date_dim_weekend_is_not_trading_day(self, mock_engine_instance):
        """A Sunday should be flagged as a non-trading day"""
        # 2024-03-31 is a Sunday (isoweekday = 7)
        dt = datetime(2024, 3, 31)
        mock_conn = MagicMock()

        date_id = mock_engine_instance._ensure_date_dim(mock_conn, dt)

        assert date_id == 20240331
        call_args = mock_conn.execute.call_args[0][1]
        assert call_args["is_trading_day"] is False
        assert call_args["day_of_week"] == 7
        assert call_args["quarter"] == 1

    def test_persist_quarantine_serializes_issues(self, mock_engine_instance):
        """Validation issues must be formatted and inserted into quarantine table"""
        sample_issue = ValidationIssue(
            source_feed="yfinance",
            target_table="fact_market_prices",
            error_code="ERR_NON_POSITIVE_PRICE",
            error_message="Price cannot be negative",
            payload={"ticker": "AAPL", "close": -10.5},
        )
        mock_conn = MagicMock()

        mock_engine_instance._persist_quarantine(mock_conn, [sample_issue])

        mock_conn.execute.assert_called_once()
        params = mock_conn.execute.call_args[0][1]
        assert params["code"] == "ERR_NON_POSITIVE_PRICE"
        assert params["feed"] == "yfinance"
        assert '"ticker": "AAPL"' in params["payload"]


class TestMacroDataIngestion:

    @pytest.fixture
    def mock_macro_instance(self, tmp_path):
        """Creates MacroDataIngestion with mock config and mock DB engine."""
        dummy_macro_cfg = tmp_path / "dummy_macro.json"
        dummy_macro_cfg.write_text(
            """
            {
              "benchmarks": [
                {"ticker": "^TNX", "benchmark": "10Y_TREASURY"}
              ]
            }
            """,
            encoding="utf-8",
        )
        mock_db_engine = MagicMock()
        return MacroDataIngestion(engine=mock_db_engine, config_path=dummy_macro_cfg)

    def test_load_macro_config(self, mock_macro_instance):
        """Verify configuration loader correctly parses benchmarks."""
        benchmarks = mock_macro_instance.config_data.get("benchmarks", [])
        assert len(benchmarks) == 1
        assert benchmarks[0]["ticker"] == "^TNX"
        assert benchmarks[0]["benchmark"] == "10Y_TREASURY"

    def test_ensure_date_dim_macro(self, mock_macro_instance):
        """Verify date dimension generation helper in macro ingestion."""
        dt = datetime(2026, 9, 18)
        mock_conn = MagicMock()

        date_id = mock_macro_instance._ensure_date_dim(mock_conn, dt)

        assert date_id == 20260918
        mock_conn.execute.assert_called_once()
        params = mock_conn.execute.call_args[0][1]
        assert params["date_id"] == 20260918
        assert params["is_trading_day"] is True


class TestMultiCurrencyIngestion:

    @pytest.fixture
    def mock_multicurrency_engine(self, tmp_path):
        """Config fixture containing both ETF and FX currencies."""
        dummy_cfg = tmp_path / "dummy_multicurrency.json"
        dummy_cfg.write_text(
            """
            {
              "currencies": [
                {
                  "ticker": "TWD=X",
                  "name": "USD/TWD",
                  "base_currency": "USD",
                  "quote_currency": "TWD"
                }
              ],
              "etfs": [
                {
                  "ticker": "EEM",
                  "name": "iShares MSCI Emerging Markets ETF",
                  "asset_class": "ETF",
                  "currency": "USD",
                  "sector": "Broad Emerging Markets",
                  "effective_date": "2024-01-01",
                  "constituents": [
                    {
                      "ticker": "2330.TW",
                      "name": "TSMC",
                      "currency": "TWD",
                      "weight": 1.0,
                      "sector": "Semiconductors"
                    }
                  ]
                }
              ]
            }
            """,
            encoding="utf-8",
        )
        mock_db = MagicMock()
        return IngestionEngine(engine=mock_db, config_path=dummy_cfg)

    def test_ohlc_inconsistency_quarantine(self):
        """Ensure OHLC hierarchy violations (like the KRW=X bad tick) are caught."""
        bad_fx_row = {
            "ticker": "KRW=X",
            "date": "2026-08-19",
            "open": 1412.10,
            "high": 1412.48,  # High lower than Close
            "low": 1384.13,
            "close": 1413.57,
            "volume": 0,
        }
        issues = MarketDataValidator.validate_price_row(
            bad_fx_row, source_feed="yfinance"
        )
        assert len(issues) == 1
        assert issues[0].error_code == "ERR_OHLC_INCONSISTENCY"

    def test_fx_zero_volume_handling(self):
        """Ensure volume=0 is valid for currencies and passes validation."""
        valid_fx_row = {
            "ticker": "TWD=X",
            "date": "2026-09-18",
            "open": 31.75,
            "high": 31.85,
            "low": 31.65,
            "close": 31.76,
            "volume": 0,
        }
        issues = MarketDataValidator.validate_price_row(
            valid_fx_row, source_feed="yfinance"
        )
        assert len(issues) == 0

    def test_sync_dimensions_handles_currencies(self, mock_multicurrency_engine):
        """Ensure sync_dimensions seeds dim_security with currency instruments."""
        mock_conn = MagicMock()
        # Mock scalar_one for security_id lookups
        mock_conn.execute.return_value.scalar_one.side_effect = [101, 202]

        mock_multicurrency_engine.engine.begin.return_value.__enter__.return_value = (
            mock_conn
        )
        mock_multicurrency_engine.sync_dimensions()

        # Check that executed calls include insertion for TWD=X
        calls = mock_conn.execute.call_args_list
        fx_insert_called = any(
            call.args[1].get("ticker") == "TWD=X"
            and call.args[1].get("currency") == "TWD"
            for call in calls
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        )
        assert fx_insert_called





