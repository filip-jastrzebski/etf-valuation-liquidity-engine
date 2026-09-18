-- DDL: Wymiary, fakty, kwarantanna

-- =============================================================================
-- ETF VALUATION & LIQUIDITY ENGINE: STAR SCHEMA DDL
-- =============================================================================

-- Existing tables clearing (useful for dev reset)

DROP TABLE IF EXISTS fact_data_quarantine CASCADE;
DROP TABLE IF EXISTS fact_synthetic_nav CASCADE;
DROP TABLE IF EXISTS fact_market_prices CASCADE;
DROP TABLE IF EXISTS fact_macro_rates CASCADE;
DROP TABLE IF EXISTS dim_etf_basket CASCADE;
DROP TABLE IF EXISTS dim_security CASCADE;
DROP TABLE IF EXISTS dim_date CASCADE;


-- -----------------------------------------------------------------------------
-- 1. DIMENSIONS
-- -----------------------------------------------------------------------------

-- Time / Caledar Dimension
CREATE TABLE dim_date (
    date_id INT PRIMARY KEY,  -- Format: YYYYMMDD, e.g. 20240325
    full_date DATE NOT NULL UNIQUE,
    year INT NOT NULL,
    quarter INT NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    month INT NOT NULL CHECK (month BETWEEN 1 AND 12),
    day INT NOT NULL CHECK (day BETWEEN 1 AND 31),
    day_of_week INT NOT NULL CHECK (day_of_week BETWEEN 1 AND 7), -- 1=Monday, 7=Sunday
    is_trading_day BOOLEAN NOT NULL DEFAULT TRUE
);

-- Financial instruments dimension (shares and ETFs)
CREATE TABLE dim_security (
    security_id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL UNIQUE,
    security_name VARCHAR(150) NOT NULL,
    asset_class VARCHAR(50) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD' CHECK (LENGTH(currency) = 3),
    sector VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Basket composition (underlying constituents of a given ETF)
CREATE TABLE dim_etf_basket (
    basket_id SERIAL PRIMARY KEY,
    etf_security_id INT NOT NULL REFERENCES dim_security(security_id),
    component_security_id INT NOT NULL REFERENCES dim_security(security_id),
    weight NUMERIC(7, 6) NOT NULL CHECK (weight > 0 AND weight <= 1.0), -- component weight
    effective_date DATE NOT NULL,
    CONSTRAINT uq_etf_component_date UNIQUE (etf_security_id, component_security_id, effective_date)
);

-- -----------------------------------------------------------------------------
-- 2. FACTS
-- -----------------------------------------------------------------------------

-- Daily market data (OHLCV)
CREATE TABLE fact_market_prices (
    price_id BIGSERIAL PRIMARY KEY,
    security_id INT NOT NULL REFERENCES dim_security(security_id),
    date_id INT NOT NULL REFERENCES dim_date(date_id),
    open_price NUMERIC(18, 4) NOT NULL CHECK (open_price >= 0),
    high_price NUMERIC(18, 4) NOT NULL CHECK (high_price >=0),
    low_price NUMERIC(18, 4) NOT NULL CHECK (low_price >=0),
    close_price NUMERIC(18, 4) NOT NULL CHECK (close_price >=0),
    volume BIGINT NOT NULL CHECK (volume >= 0),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_security_date UNIQUE (security_id, date_id)
);

-- Macroeconomic data (interest rates from FRED, benchmark spreads)
CREATE TABLE fact_macro_rates (
    rate_id BIGSERIAL PRIMARY KEY,
    date_id INT NOT NULL REFERENCES dim_date(date_id),
    risk_free_rate NUMERIC(8, 6) NOT NULL,       -- e.g. 0.052500 (5.25%)
    rate_benchmark VARCHAR(50) NOT NULL,         -- e.g. 'SOFR', '3M_TREASURY'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_rate_date UNIQUE (date_id, rate_benchmark)
);

-- Calculated synthetic valuations and arbitrage discrepancies (iNAV vs. ETF price)
CREATE TABLE fact_synthetic_nav (
    nav_id BIGSERIAL PRIMARY KEY,
    etf_security_id INT NOT NULL REFERENCES dim_security(security_id),
    date_id INT NOT NULL REFERENCES dim_date(date_id),
    etf_market_price NUMERIC(18, 4) NOT NULL,
    synthetic_nav NUMERIC(18, 4) NOT NULL,
    nav_spread NUMERIC(18, 4) NOT NULL,          -- etf_market_price - synthetic_nav
    discrepancy_bps NUMERIC(10, 2) NOT NULL,     -- discrepancy in basis points (bps)
    is_anomaly BOOLEAN NOT NULL DEFAULT FALSE,   -- Flag: did the discrepancy exceed the limit
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_etf_date UNIQUE (etf_security_id, date_id)
);

-- -----------------------------------------------------------------------------
-- 3. DATA QUALITY / QUARANTINE
-- -----------------------------------------------------------------------------

-- Quarantine table for errors or rejected records
CREATE TABLE fact_data_quarantine (
    quarantine_id BIGSERIAL PRIMARY KEY,
    source_feed VARCHAR(50) NOT NULL,     -- e.g. 'yfinance', 'fred'
    target_table VARCHAR(50) NOT NULL,    -- e.g. 'fact_market_prices', 'fact_macro_rates'
    payload JSONB NOT NULL,               -- Raw data dump that caused the error
    error_code VARCHAR(50) NOT NULL,      -- e.g. 'ERR_NEGATIVE_PRICE', 'ERR_WEIGHT_MISMATCH'
    error_message TEXT NOT NULL,
    quarantined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------------
-- 4. INDEXES FOR QUERY OPTIMIZATION
-- -----------------------------------------------------------------------------

CREATE INDEX idx_prices_security_date ON fact_market_prices (security_id, date_id);
CREATE INDEX idx_nav_etf_date ON fact_synthetic_nav (etf_security_id, date_id);
CREATE INDEX idx_basket_lookup ON dim_etf_basket (etf_security_id, effective_date);
CREATE INDEX idx_quarantine_code ON fact_data_quarantine (error_code);