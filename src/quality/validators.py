"""Data Quality validators for basket composition and market data."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math
import pandas as pd

@dataclass
class ValidationIssue:
    """Represents a single data quality failure to be quarantined."""
    source_feed: str
    target_table: str
    error_code: str
    error_message: str
    payload: Dict[str, Any]

class BasketValidator:
    """Validates structural integrity and constraints of ETF basket"""

    TOLERANCE = 1e-5

    @classmethod
    def validate_basket(cls, etf_def: Dict[str, Any]) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        ticker = etf_def.get("ticker", "UNKNOWN")
        constituents = etf_def.get("constituents", [])

        if not constituents:
            issues.append(
                ValidationIssue(
                    source_feed="config",
                    target_table="dim_etf_basket",
                    error_code="ERR_EMPTY_BASKET",
                    error_message=f"ETF {ticker} has no constutents defined.",
                    payload={"ticker": ticker},
                )
            )
            return issues

        total_weight = 0.0
        seen_tickers = set()

        for c in constituents:
            comp_ticker = c.get("ticker", "UNKNOWN")
            weight = c.get("weight", 0.0)

            # Check for duplicate constutuents
            if comp_ticker in seen_tickers:
                issues.append(
                    ValidationIssue(
                        source_feed="config",
                        target_table="dim_etf_basket",
                        error_code="ERR_DUPLICATE_CONSTITUENT",
                        error_message=f"Duplicate component {comp_ticker} in basket {ticker}.",                
                        payload={"etf": ticker, "component": comp_ticker},
                    )
                )
            seen_tickers.add(comp_ticker)

            # Check weight bounds
            if weight <= 0.0 or weight > 1.0:
                issues.append(
                    ValidationIssue(
                        source_feed="config",
                        target_table="dim_etf_basket",
                        error_code="ERR_INVALID_WEIGHT_BOUNDS",
                        error_message=f"Weight for {comp_ticker} in {ticker} out of bounds: {weight}",                
                        payload={"etf": ticker, "component": comp_ticker, "weight": weight},
                    )
                )

            total_weight += weight

            # Check total basket weight == 1.0 (with floating-point tolerance)
        if not math.isclose(total_weight, 1.0, abs_tol=cls.TOLERANCE):
            issues.append(
                ValidationIssue(
                    source_feed="config",
                    target_table="dim_etf_basket",
                    error_code="ERR_TOTAL_WEIGHT_MISMATCH",
                    error_message=f"Total weight for {ticker} is {total_weight:.6f}, expected 1.000000.",                
                    payload={"etf": ticker, "total_weight": total_weight},
                )
            )

        return issues

class MarketDataValidator:
    """Validates daily OHLCV pricing batches before database ingestion."""

    MAX_INTRADAY_PCT_CHANGE = 0.50 # 50% max allowable single-session swing

    @classmethod
    def validate_price_row(cls, row: Dict[str, Any], source_feed: str = "yfinance") -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        ticker = row.get("ticker", "UNKNOWN")

        # Required fields check
        required_fields = ["open", "high", "low", "close", "volume", "date"]
        for field in required_fields:
            val = row.get(field)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                issues.append(
                    ValidationIssue(
                        source_feed=source_feed,
                        target_table="fact_market_prices",
                        error_code="ERR_MISSING_VALUE",
                        error_message=f"Missing or NaN field '{field}' for ticker {ticker}.",
                        payload=row,
                    )
                )
                return issues # Abort deeper checks if essential values are missing

        open_p = float(row["open"])
        high_p = float(row["high"])
        low_p = float(row["low"])
        close_p = float(row["close"])
        vol = int(row["volume"])

        # Non-negative prices and volume
        if any(p <= 0 for p in [open_p, high_p, low_p, close_p]):
            issues.append(
                ValidationIssue(
                    source_feed=source_feed,
                    target_table="fact_market_prices",
                    error_code="ERR_NON_POSITIVE_PRICE",
                    error_message=f"Non-positive price detected for ticker {ticker}.",
                    payload=row,
                )
            )

        if vol < 0:
            issues.append(
                ValidationIssue(
                    source_feed=source_feed,
                    target_table="fact_market_prices",
                    error_code="ERR_NEGATIVE_VOLUME",
                    error_message=f"Negative volume detected for ticker {ticker}: {vol}.",
                    payload=row,
                )
            )

        # High/Low logic: High must be highest, Low - lowest 
        epsilon = 0.0001
        if (
            high_p < (low_p - epsilon)
            or high_p < (open_p - epsilon)
            or high_p < (close_p - epsilon)
            or low_p > (open_p + epsilon)
            or low_p > (close_p + epsilon)
        ):
            issues.append(
                ValidationIssue(
                    source_feed=source_feed,
                    target_table="fact_market_prices",
                    error_code="ERR_OHLC_INCONSISTENCY",
                    error_message=f"Inconsistent OHLC hierarchy for ticker {ticker}.",
                    payload=row,
                )
            )

        # Extreme session swing check (> 50% intradat change)
        if open_p > 0:
            intraday_return = abs(close_p - open_p) / open_p
            if intraday_return > cls.MAX_INTRADAY_PCT_CHANGE:
                issues.append(
                    ValidationIssue(
                        source_feed=source_feed,
                        target_table="fact_market_prices",
                        error_code="ERR_EXTREME_PRICE_SWING",
                        error_message=f"Intraday price change {intraday_return:.2%} exceeds threshold of {cls.MAX_INTRADAY_PCT_CHANGE:.0%} for ticker {ticker}.",
                        payload=row,
                    )
                )

        return issues
                        