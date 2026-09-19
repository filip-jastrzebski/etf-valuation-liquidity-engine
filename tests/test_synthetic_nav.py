"""Unit tests for SyntheticNAVEngine."""

from decimal import Decimal
import math
import pytest
from sqlalchemy import create_engine, text

from src.valuation.synthetic_nav import SyntheticNAVEngine


@pytest.fixture
def memory_db():
    """In-memory SQLite database reflecting 1:1 production schema."""
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))

        # 1. Tworzenie tabel (pojedyncze instrukcje DDL dla zgodności ze sterownikiem sqlite3)
        conn.execute(
            text(
                """
                CREATE TABLE dim_date (
                    date_id INTEGER PRIMARY KEY,
                    full_date DATE NOT NULL UNIQUE,
                    year INTEGER NOT NULL,
                    quarter INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    day_of_week INTEGER NOT NULL,
                    is_weekend BOOLEAN NOT NULL,
                    is_business_day BOOLEAN NOT NULL
                );
                """
            )
        )

        conn.execute(
            text(
                """
                CREATE TABLE dim_security (
                    security_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker VARCHAR(20) NOT NULL UNIQUE,
                    security_name VARCHAR(150) NOT NULL,
                    asset_class VARCHAR(50) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
                    sector VARCHAR(100),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        )

        conn.execute(
            text(
                """
                CREATE TABLE dim_etf_basket (
                    basket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    etf_security_id INTEGER NOT NULL REFERENCES dim_security(security_id),
                    component_security_id INTEGER NOT NULL REFERENCES dim_security(security_id),
                    weight NUMERIC(7, 6) NOT NULL CHECK (weight > 0 AND weight <= 1.0),
                    effective_date DATE NOT NULL,
                    CONSTRAINT uq_etf_component_date UNIQUE (etf_security_id, component_security_id, effective_date)
                );
                """
            )
        )

        conn.execute(
            text(
                """
                CREATE TABLE fact_market_prices (
                    price_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id INTEGER NOT NULL REFERENCES dim_security(security_id),
                    date_id INTEGER NOT NULL REFERENCES dim_date(date_id),
                    open_price NUMERIC(18, 4) NOT NULL CHECK (open_price >= 0),
                    high_price NUMERIC(18, 4) NOT NULL CHECK (high_price >= 0),
                    low_price NUMERIC(18, 4) NOT NULL CHECK (low_price >= 0),
                    close_price NUMERIC(18, 4) NOT NULL CHECK (close_price >= 0),
                    volume BIGINT NOT NULL CHECK (volume >= 0),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_security_date UNIQUE (security_id, date_id)
                );
                """
            )
        )

        conn.execute(
            text(
                """
                CREATE TABLE fact_synthetic_nav (
                    nav_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    etf_security_id INTEGER NOT NULL REFERENCES dim_security(security_id),
                    date_id INTEGER NOT NULL REFERENCES dim_date(date_id),
                    etf_market_price NUMERIC(18, 4) NOT NULL,
                    synthetic_nav NUMERIC(18, 4) NOT NULL,
                    nav_spread NUMERIC(18, 4) NOT NULL,
                    discrepancy_bps NUMERIC(10, 2) NOT NULL,
                    is_anomaly BOOLEAN NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_etf_date UNIQUE (etf_security_id, date_id)
                );
                """
            )
        )

        # 2. Dane wymiaru daty
        conn.execute(
            text(
                """
                INSERT INTO dim_date (date_id, full_date, year, quarter, month, day, day_of_week, is_weekend, is_business_day) VALUES
                (20260901, '2026-09-01', 2026, 3, 9, 1, 2, 0, 1),
                (20260902, '2026-09-02', 2026, 3, 9, 2, 3, 0, 1),
                (20260903, '2026-09-03', 2026, 3, 9, 3, 4, 0, 1);
                """
            )
        )

        # 3. Papiery wartościowe
        conn.execute(
            text(
                """
                INSERT INTO dim_security (security_id, ticker, security_name, asset_class, currency, sector) VALUES
                (1, 'XLF', 'Financial Select Sector SPDR Fund', 'ETF', 'USD', 'Financials'),
                (2, 'BRK-B', 'Berkshire Hathaway Inc. Class B', 'Equity', 'USD', 'Financial Services'),
                (3, 'JPM', 'JPMorgan Chase & Co.', 'Equity', 'USD', 'Banks');
                """
            )
        )

        # 4. Koszyk funduszu (BRK-B: 60%, JPM: 40%)
        conn.execute(
            text(
                """
                INSERT INTO dim_etf_basket (etf_security_id, component_security_id, weight, effective_date) VALUES
                (1, 2, 0.600000, '2024-01-01'),
                (1, 3, 0.400000, '2024-01-01');
                """
            )
        )

        # 5. Dane cenowe OHLCV
        conn.execute(
            text(
                """
                INSERT INTO fact_market_prices (security_id, date_id, open_price, high_price, low_price, close_price, volume) VALUES
                (1, 20260901, 50.0, 50.5, 49.5, 50.0, 1000000),
                (2, 20260901, 100.0, 101.0, 99.0, 100.0, 500000),
                (3, 20260901, 50.0, 51.0, 49.0, 50.0, 800000),

                (1, 20260902, 51.0, 51.5, 50.5, 51.0, 1100000),
                (2, 20260902, 102.0, 103.0, 101.0, 102.0, 520000),
                (3, 20260902, 51.0, 52.0, 50.0, 51.0, 750000),

                (1, 20260903, 50.0, 50.5, 49.5, 50.0, 950000),
                (2, 20260903, 110.0, 111.0, 109.0, 110.0, 600000),
                (3, 20260903, 55.0, 56.0, 54.0, 55.0, 820000);
                """
            )
        )

    return engine


def test_divisor_and_inav_calculation(memory_db):
    """Verifies that Divisor scales raw index to match t0 ETF price and calculates iNAV correctly."""
    engine = SyntheticNAVEngine(engine=memory_db)
    results = engine.calculate_nav_for_etf(etf_ticker="XLF", use_dynamic_zscore=False, anomaly_threshold_bps=50.0)

    assert len(results) == 3

    # day t0: iNAV should be exactly 50.0, spread = 0.0, discrepancy = 0.0 bps
    t0 = results[0]
    assert t0["date_id"] == 20260901
    assert pytest.approx(t0["synthetic_nav"], 0.0001) == 50.0
    assert pytest.approx(t0["nav_spread"], 0.0001) == 0.0
    assert pytest.approx(t0["discrepancy_bps"], 0.01) == 0.0
    assert t0["is_anomaly"] is False

    # day 2: iNAV = 51.0, ETF = 51.0 -> error = 0.0
    t1 = results[1]
    assert pytest.approx(t1["synthetic_nav"], 0.0001) == 51.0
    assert pytest.approx(t1["nav_spread"], 0.0001) == 0.0
    assert t1["is_anomaly"] is False

    # day 3: ETF = 50.0, iNAV = 55.0 -> spread = -5.0, discrepancy = -909.09 bps
    t2 = results[2]
    assert pytest.approx(t2["synthetic_nav"], 0.0001) == 55.0
    assert pytest.approx(t2["nav_spread"], 0.0001) == -5.0
    assert pytest.approx(t2["discrepancy_bps"], 0.1) == -909.09
    assert t2["is_anomaly"] is True

def test_baseline_day_is_never_anomaly(memory_db):
    """Ensures t0 date is explicitly shielded from being flagged as an anomaly."""
    engine = SyntheticNAVEngine(engine=memory_db)

    # even with aggresive threshold and low min_bps t0 must stay False
    results = engine.calculate_nav_for_etf(
        etf_ticker="XLF",
        use_dynamic_zscore=True,
        z_threshold=1.0,
        min_anomaly_bps=0.0,
    )
    assert results[0]["is_anomaly"] is False

def test_unknown_ticker_raises_value_error(memory_db):
    """Engine must raise ValueError if the requested ETF ticker does not exist."""
    engine = SyntheticNAVEngine(engine=memory_db)
    with pytest.raises(ValueError, match="ETF with ticker 'XYZ' not found"):
        engine.calculate_nav_for_etf(etf_ticker="XYZ")