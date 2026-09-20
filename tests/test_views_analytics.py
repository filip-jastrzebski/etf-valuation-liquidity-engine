"""Integration tests for view_etf_market_analytics using an isolated PostgreSQL schema."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from src.db.connection import get_engine


@pytest.fixture(scope="module")
def pg_isolated_schema():
    """Creates an isolated schema in Postgres, runs DDL, yields connection, and drops schema."""
    engine = get_engine()
    schema_name = "test_analytics_sandbox"

    # Sanity check: Check if Postgres is responding; if not, skip the test.
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
    except OperationalError:
        pytest.skip(
            "PostgreSQL is not running on localhost:5432. Skipping integration tests."
        )

    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;"))
        conn.execute(text(f"CREATE SCHEMA {schema_name};"))
        conn.execute(text(f"SET search_path TO {schema_name};"))

        # 2. Table schema (DDL)
        conn.execute(
            text(
                """
                CREATE TABLE dim_date (
                    date_id INT PRIMARY KEY,
                    full_date DATE NOT NULL UNIQUE
                );

                CREATE TABLE dim_security (
                    security_id SERIAL PRIMARY KEY,
                    ticker VARCHAR(20) NOT NULL UNIQUE,
                    asset_class VARCHAR(50) NOT NULL
                );

                CREATE TABLE fact_market_prices (
                    price_id BIGSERIAL PRIMARY KEY,
                    security_id INT NOT NULL REFERENCES dim_security(security_id),
                    date_id INT NOT NULL REFERENCES dim_date(date_id),
                    close_price NUMERIC(18, 4) NOT NULL,
                    volume BIGINT NOT NULL
                );

                CREATE TABLE fact_synthetic_nav (
                    nav_id BIGSERIAL PRIMARY KEY,
                    etf_security_id INT NOT NULL REFERENCES dim_security(security_id),
                    date_id INT NOT NULL REFERENCES dim_date(date_id),
                    synthetic_nav NUMERIC(18, 4) NOT NULL,
                    discrepancy_bps NUMERIC(10, 2) NOT NULL,
                    is_anomaly BOOLEAN NOT NULL DEFAULT FALSE
                );
                """
            )
        )

        # 3. Uploading our analytical view to an isolated schema
        with open("sql/02_views_analytics.sql", "r", encoding="utf-8") as f:
            view_sql = f.read()
            conn.execute(text(view_sql))

        # 4. Injecting a small, deterministic sample: 1 ETF, 3 days
        conn.execute(
            text(
                """
                INSERT INTO dim_date (date_id, full_date) VALUES 
                (20260901, '2026-09-01'),
                (20260902, '2026-09-02'),
                (20260903, '2026-09-03');

                INSERT INTO dim_security (security_id, ticker, asset_class) VALUES 
                (1, 'TEST_ETF', 'ETF');

                -- Dzień 1: P=100, Vol=1M, iNAV=100
                -- Dzień 2: P=110 (+10%), Vol=2M, iNAV=105 (+5%) -> TD = +500 bps, Obrót = $220M
                -- Dzień 3: P=110 (0%), Vol=1M, iNAV=110.25 (+5%) -> TD = -500 bps
                INSERT INTO fact_market_prices (security_id, date_id, close_price, volume) VALUES
                (1, 20260901, 100.0, 1000000),
                (1, 20260902, 110.0, 2000000),
                (1, 20260903, 110.0, 1000000);

                INSERT INTO fact_synthetic_nav (etf_security_id, date_id, synthetic_nav, discrepancy_bps, is_anomaly) VALUES
                (1, 20260901, 100.0, 0.0, FALSE),
                (1, 20260902, 105.0, 476.19, FALSE),
                (1, 20260903, 110.25, -22.68, FALSE);
                """
            )
        )

    # Making the schema available for testing
    yield schema_name

    # 5. Teardown: cleaning up the schema after tests
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;"))


def test_view_math_and_tracking_metrics(pg_isolated_schema):
    """Verifies return calculation, Tracking Difference, and Amihud ratio directly in Postgres."""
    engine = get_engine()

    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {pg_isolated_schema};"))
        result = conn.execute(
            text(
                """
                SELECT 
                    date_id,
                    etf_return_pct,
                    inav_return_pct,
                    tracking_difference_bps,
                    amihud_illiq_ratio
                FROM view_etf_market_analytics
                ORDER BY date_id ASC;
                """
            )
        ).mappings().all()

        assert len(result) == 3

        # Day 1: no previous session -> rates of return = 0.0
        d1 = result[0]
        assert float(d1["etf_return_pct"]) == 0.0
        assert float(d1["inav_return_pct"]) == 0.0
        assert float(d1["tracking_difference_bps"]) == 0.0

        # Day 2: P jumps from 100 to 110 (+10%), iNAV from 100 to 105 (+5%)
        # TD = 10% - 5% = +5% = +500.00 bps
        # Dollar turnover = 110 * 2_000_000 = $220,000,000
        # Amihud per million = (|0.10| * 1_000_000) / 220_000_000 = 0.10 / 220 = ~0.000455
        d2 = result[1]
        assert pytest.approx(float(d2["etf_return_pct"]), 0.01) == 10.0
        assert pytest.approx(float(d2["inav_return_pct"]), 0.01) == 5.0
        assert pytest.approx(float(d2["tracking_difference_bps"]), 0.1) == 500.0
        assert pytest.approx(float(d2["amihud_illiq_ratio"]), 0.00001) == 0.000455

        # Day 3: P unchanged (0%), iNAV from 105 to 110.25 (+5%) -> TD = -500.00 bps
        d3 = result[2]
        assert float(d3["etf_return_pct"]) == 0.0
        assert pytest.approx(float(d3["inav_return_pct"]), 0.01) == 5.0
        assert pytest.approx(float(d3["tracking_difference_bps"]), 0.1) == -500.0

def test_partition_by_isolation_and_zero_volume_handling(pg_isolated_schema):
    """
    Ensures that:
    1. LAG and Window functions do not leak across distinct tickers (PARTITION BY integrity).
    2. Zero volume / turnover does not trigger a 'division by zero' crash in Amihud ratio.
    """
    engine = get_engine()

    with engine.begin() as conn:
        conn.execute(text(f"SET search_path TO {pg_isolated_schema};"))
        
        # Adding a second ETF: ETF_B.
        conn.execute(text("INSERT INTO dim_security (security_id, ticker, asset_class) VALUES (2, 'ETF_B', 'ETF');"))
        
        # Injecting prices for ETF_B, including zero volume on 20260902.
        conn.execute(
            text(
                """
                INSERT INTO fact_market_prices (security_id, date_id, close_price, volume) VALUES
                (2, 20260901, 200.0, 500000),
                (2, 20260902, 200.0, 0);  -- zerowy wolumen (test division by zero)

                INSERT INTO fact_synthetic_nav (etf_security_id, date_id, synthetic_nav, discrepancy_bps, is_anomaly) VALUES
                (2, 20260901, 200.0, 0.0, FALSE),
                (2, 20260902, 200.0, 0.0, FALSE);
                """
            )
        )

    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {pg_isolated_schema};"))
        rows = conn.execute(
            text(
                """
                SELECT ticker, date_id, etf_return_pct, amihud_illiq_ratio
                FROM view_etf_market_analytics
                WHERE ticker = 'ETF_B'
                ORDER BY date_id ASC;
                """
            )
        ).mappings().all()

        assert len(rows) == 2

        # Day 1 for ETF_B MUST have a return of 0.0 (it cannot use the TEST_ETF price from 20260903)
        assert float(rows[0]["etf_return_pct"]) == 0.0

        # Day 2: turnover = 0 -> The Amihud ratio must handle this case and return 0.0 without a division-by-zero exception.
        assert float(rows[1]["amihud_illiq_ratio"]) == 0.0