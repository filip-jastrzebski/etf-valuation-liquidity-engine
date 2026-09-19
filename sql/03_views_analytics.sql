-- =============================================================================
-- 03_views_analytics.sql
-- Module: Advanced Financial Analytics, Risk, Tracking & Liquidity Engine
-- =============================================================================

CREATE OR REPLACE VIEW view_etf_market_analytics AS
WITH
-- -----------------------------------------------------------------------------
-- 1. Calculation of rates of return and dollar volume for instruments
-- -----------------------------------------------------------------------------
security_returns AS (
    SELECT
        p.security_id,
        s.ticker,
        s.asset_class,
        p.date_id,
        d.full_date,
        p.close_price,
        p.volume,
        -- Dollar turnover = Price * Volume
        (p.close_price * p.volume) AS dollar_volume,
        -- Last closing price
        LAG(p.close_price, 1) OVER (
            PARTITION BY p.security_id
            ORDER BY p.date_id ASC
        ) AS prev_close_price,
        -- Daily rate of return R_t = (P_t / P_{t-1}) - 1
        CASE
            WHEN LAG(p.close_price, 1) OVER (PARTITION BY p.security_id ORDER BY p.date_id ASC) IS NULL THEN 0.0
            WHEN LAG(p.close_price, 1) OVER (PARTITION BY p.security_id ORDER BY p.date_id ASC) = 0 THEN 0.0
            ELSE (p.close_price / LAG(p.close_price, 1) OVER (PARTITION BY p.security_id ORDER BY p.date_id ASC)) - 1.0
        END AS daily_return
    FROM fact_market_prices p
    JOIN dim_security s ON p.security_id = s.security_id
    JOIN dim_date d ON p.date_id = d.date_id
    ),

-- -----------------------------------------------------------------------------
-- 2. Synthetic iNAV returns and their relationship with the ETF market price
-- -----------------------------------------------------------------------------
inav_returns AS (
    SELECT
        f.etf_security_id,
        f.date_id,
        f.synthetic_nav,
        f.discrepancy_bps,
        f.is_anomaly AS base_engine_anomaly,
        -- Last iNAV value
        LAG(f.synthetic_nav, 1) OVER (
            PARTITION BY f.etf_security_id
            ORDER BY f.date_id ASC
        ) AS prev_synthetic_nav,
        -- Daily rate of return of the synthetic basket R_{iNAV, t}
        CASE
            WHEN LAG(f.synthetic_nav, 1) OVER (PARTITION BY f.etf_security_id ORDER BY f.date_id ASC) IS NULL THEN 0.0
            WHEN LAG(f.synthetic_nav, 1) OVER (PARTITION BY f.etf_security_id ORDER BY f.date_id ASC) = 0 THEN 0.0
            ELSE (f.synthetic_nav / LAG(f.synthetic_nav, 1) OVER (PARTITION BY f.etf_security_id ORDER BY f.date_id ASC)) - 1.0
        END AS inav_daily_return
    FROM fact_synthetic_nav f
),

-- -----------------------------------------------------------------------------
-- 3. Comparison of ETF metrics, replication, and the Amihud ratio.
-- -----------------------------------------------------------------------------
etf_combined AS (
    SELECT
        sr.security_id AS etf_security_id,
        sr.ticker,
        sr.date_id,
        sr.full_date,
        sr.close_price AS etf_market_price,
        ir.synthetic_nav,
        sr.daily_return AS etf_return,
        ir.inav_daily_return,
        -- Tracking Difference: TD_t = R_{ETF, t} - R_{iNAV, t}
        (sr.daily_return - ir.inav_daily_return) AS tracking_difference,
        ir.discrepancy_bps,
        ir.base_engine_anomaly,
        sr.dollar_volume,
        -- Amihud Liquidity Ratio scaled to $1,000,000 in turnover:
        -- (|R_t| * 1,000,000) / DollarVolume_t
        CASE
            WHEN sr.dollar_volume IS NULL OR sr.dollar_volume = 0 THEN 0.0
            ELSE (ABS(sr.daily_return) * 1000000.0) / sr.dollar_volume
        END AS amihud_illiq_ratio_per_million
    FROM security_returns sr
    JOIN inav_returns ir
    ON sr.security_id = ir.etf_security_id
    AND sr.date_id = ir.date_id
    WHERE sr.asset_class = 'ETF'
)

-- -----------------------------------------------------------------------------
-- 4. Final Projection: Rolling Volatility, Tracking Error, and Statistical Flags
-- -----------------------------------------------------------------------------
SELECT
    c.ticker,
    c.full_date,
    c.date_id,
    ROUND(c.etf_market_price::numeric, 4) AS etf_market_price,
    ROUND(c.synthetic_nav::numeric, 4) AS synthetic_nav,
    ROUND(c.discrepancy_bps::numeric, 2) AS discrepancy_bps,
    ROUND((c.etf_return * 100.0)::numeric, 4) AS etf_return_pct,
    ROUND((c.inav_daily_return * 100.0)::numeric, 4) AS inav_return_pct,
    ROUND((c.tracking_difference * 10000.0)::numeric, 2) AS tracking_difference_bps,

    -- Annualized rolling volatility (21 sessions ~ 1 business month)
    ROUND(
        (STDDEV_SAMP(c.etf_return) OVER (
            PARTITION BY c.etf_security_id
            ORDER BY c.date_id ASC
            ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
        ) * SQRT(252) * 100.0)::numeric, 2
    ) AS rolling_vol_21d_pct,

    -- Annualized rolling volatility (63 sessions ~ 1 business quarter)
    ROUND(
        (STDDEV_SAMP(c.etf_return) OVER (
            PARTITION BY c.etf_security_id 
            ORDER BY c.date_id ASC 
            ROWS BETWEEN 62 PRECEDING AND CURRENT ROW
        ) * SQRT(252) * 100.0)::numeric, 2
    ) AS rolling_vol_63d_pct,

    -- Annualized Tracking Error (21 trading days): STDDEV(Tracking Difference) * sqrt(252)
    ROUND(
        (STDDEV_SAMP(c.tracking_difference) OVER (
            PARTITION BY c.etf_security_id 
            ORDER BY c.date_id ASC 
            ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
        ) * SQRT(252) * 10000.0)::numeric, 2
    ) AS rolling_tracking_error_21d_bps,

    -- Amihud Ratio
    ROUND(c.amihud_illiq_ratio_per_million::numeric, 6) AS amihud_illiq_ratio,

    -- Dynamic anomaly in SQL (Z-Score >= 2.0 over a 21-session window for discrepancy_bps)
    CASE
        WHEN STDDEV_SAMP(c.discrepancy_bps) OVER (
            PARTITION BY c.etf_security_id
            ORDER BY c.date_id ASC
            ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
        ) IS NULL THEN FALSE
        WHEN STDDEV_SAMP(c.discrepancy_bps) OVER (
            PARTITION BY c.etf_security_id 
            ORDER BY c.date_id ASC 
            ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
        ) = 0 THEN FALSE
        ELSE (
            ABS(
                c.discrepancy_bps - AVG(c.discrepancy_bps) OVER (
                    PARTITION BY c.etf_security_id 
                    ORDER BY c.date_id ASC 
                    ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
                )
            ) / STDDEV_SAMP(c.discrepancy_bps) OVER (
                PARTITION BY c.etf_security_id 
                ORDER BY c.date_id ASC 
                ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
            ) >= 2.0
            AND ABS(c.discrepancy_bps) >= 30.0
        )
    END AS is_sql_anomaly,

    c.base_engine_anomaly

FROM etf_combined c
ORDER BY c.ticker, c.date_id ASC;