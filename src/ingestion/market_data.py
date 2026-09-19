"""Market data ingestion pipeline via yfinance with data quality gating."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

import pandas as pd
import yfinance as yf
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.db.connection import get_engine
from src.quality.validators import BasketValidator, MarketDataValidator, ValidationIssue


class IngestionEngine:

    def __init__(self, engine: Engine, config_path: Path):
        self.engine = engine
        self.config_path = config_path
        self.config_data = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load the configuration from a JSON file."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def sync_dimensions(self) -> None:
        """Seed dim_security and dim_etf_basket based on basket configuration."""
        currencies = self.config_data.get("currencies", [])
        etfs = self.config_data.get("etfs", [])

        with self.engine.begin() as conn:
            for fx in currencies:
                conn.execute(
                    text(
                        """
                        INSERT INTO dim_security (ticker, security_name, asset_class, currency, sector)
                        VALUES (:ticker, :name, 'CURRENCY', :currency, 'Foreign Exchange')
                        ON CONFLICT (ticker) DO UPDATE SET
                            security_name = EXCLUDED.security_name,
                            currency = EXCLUDED.currency;
                        """
                    ),
                    {
                        "ticker": fx["ticker"],
                        "name": fx.get("name", fx["ticker"]),
                        "currency": fx.get("quote_currency", "USD"),
                    },
                )

            for etf in etfs:
                # 1. Validate basket configuration
                basket_issues = BasketValidator.validate_basket(etf)
                if basket_issues:
                    self._persist_quarantine(conn, basket_issues)
                    continue

                # 2. Insert ETF into dim_security
                etf_ticker = etf["ticker"]
                conn.execute(
                    text(
                        """
                        INSERT INTO dim_security (ticker, security_name, asset_class, currency, sector)
                        VALUES (:ticker, :name, :asset_class, :currency, :sector)
                        ON CONFLICT (ticker) DO UPDATE SET
                            security_name = EXCLUDED.security_name,
                            sector = EXCLUDED.sector;
                        """ 
                    
                ),
                {
                    "ticker": etf_ticker,
                    "name": etf.get("name", etf_ticker),
                    "asset_class": etf.get("asset_class", "ETF"),
                    "currency": etf.get("currency", "USD"),
                    "sector": etf.get("sector"),
                },
            )

                # Get ETF security_id
                etf_id = conn.execute(
                    text("SELECT security_id FROM dim_security WHERE ticker = :ticker"),
                    {"ticker": etf_ticker},
                ).scalar_one()

                # 3. Insert constituents and basket relations
                for c in etf.get("constituents", []):
                    comp_ticker = c["ticker"]
                    conn.execute(
                        text(
                            """
                            INSERT INTO dim_security (ticker, security_name, asset_class, currency, sector)
                            VALUES (:ticker, :name, 'EQUITY', :currency, :sector)
                            ON CONFLICT (ticker) DO UPDATE SET
                                security_name = EXCLUDED.security_name,
                                sector = EXCLUDED.sector;
                            """
                        ),
                        {
                            "ticker": comp_ticker,
                            "name": c.get("name", comp_ticker),
                            "currency": c.get("currency", "USD"),
                            "sector": c.get("sector"),
                        },
                    )

                    comp_id = conn.execute(
                        text("SELECT security_id FROM dim_security WHERE ticker = :ticker"),
                        {"ticker": comp_ticker,
                         "currency": c.get("currency", "USD"),}
                    ).scalar_one()

                    conn.execute(
                        text(
                            """
                            INSERT INTO dim_etf_basket (etf_security_id, component_security_id, weight, effective_date)
                            VALUES (:etf_id, :comp_id, :weight, :eff_date)
                            ON CONFLICT (etf_security_id, component_security_id, effective_date)
                            DO UPDATE SET weight = EXCLUDED.weight;
                            """
                        ),
                        {
                            "etf_id": etf_id,
                            "comp_id": comp_id,
                            "weight": c["weight"],
                            "eff_date": etf.get("effective_date", "2024-01-01"),
                        },
                    )


    def _ensure_date_dim(self, conn: Any, dt: datetime) -> int:
        """Ensure date exists in dim_date and return date_id (YYYYMMDD)."""
        date_id = int(dt.strftime("%Y%m%d"))
        day_of_week = dt.isoweekday() # 1=Monday, 7=Sunday
        is_trading = day_of_week <=5

        conn.execute(
            text(
                """
                INSERT INTO dim_date (date_id, full_date, year, quarter, month, day, day_of_week, is_trading_day)
                VALUES (:date_id, :full_date, :year, :quarter, :month, :day, :day_of_week, :is_trading_day)
                ON CONFLICT (date_id) DO NOTHING;
                """
            ),
            {
                "date_id": date_id,
                "full_date": dt.date(),
                "year": dt.year,
                "quarter": (dt.month - 1) // 3 + 1,
                "month": dt.month,
                "day": dt.day,
                "day_of_week": day_of_week,
                "is_trading_day": is_trading,
            },
        )
        return date_id

    def _persist_quarantine(self, conn: Any, issues: List[ValidationIssue]) -> None:
        for issue in issues:
            conn.execute(
                text(
                    """
                    INSERT INTO fact_data_quarantine (source_feed, target_table, payload, error_code, error_message)
                    VALUES (:feed, :target, :payload, :code, :msg);
                    """
                ),
                {
                    "feed": issue.source_feed,
                    "target": issue.target_table,
                    "payload": json.dumps(issue.payload, default=str),
                    "code": issue.error_code,
                    "msg": issue.error_message
                },
            )

    def ingest_market_prices(self, period: str = "1mo") -> None:
        """Fetch OHLCV data from yfinance, validate, and persist"""
        self.sync_dimensions()

        # Gather all distinct tickers (ETFs + constituens)
        all_tickers: Set[str] = set()
        for fx in self.config_data.get("currencies", []):
            all_tickers.add(fx["ticker"])
        for etf in self.config_data.get("etfs", []):
            all_tickers.add(etf["ticker"])
            for c in etf.get("constituents", []):
                all_tickers.add(c["ticker"])

        # Fetch market data batch
        tickers_list = list(all_tickers)
        data = yf.download(
            tickers = tickers_list,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False
        )

        with self.engine.begin() as conn:
            # Map ticker -> security_id
            rows = conn.execute(text("SELECT security_id, ticker FROM dim_security")).fetchall()
            sec_map = {r[1]: r[0] for r in rows}

            for ticker in tickers_list:
                sec_id = sec_map.get(ticker)
                if not sec_id:
                    continue

                # Handle DataFrame slicing for single vs multiple tickers
                df = data[ticker] if len(tickers_list) > 1 else data
                df = df.dropna(how='all')

                for ts, row in df.iterrows():
                    dt = pd.to_datetime(ts)
                    date_id = self._ensure_date_dim(conn, dt)

                    if pd.isna(row.get("Close")) or pd.isna(row.get("Open")):
                        continue

                    vol = int(row["Volume"]) if not pd.isna(row.get("Volume")) else 0

                    row_dict = {
                        "ticker": ticker,
                        "date": dt.strftime("%Y-%m-%d"),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "volume": vol,
                    }

                    issues = MarketDataValidator.validate_price_row(row_dict, source_feed="yfinance")
                    if issues:
                        self._persist_quarantine(conn, issues)
                        continue

                    conn.execute(
                        text(
                            """
                            INSERT INTO fact_market_prices 
                            (security_id, date_id, open_price, high_price, low_price, close_price, volume)
                            VALUES (:sec_id, :date_id, :open, :high, :low, :close, :vol)
                            ON CONFLICT (security_id, date_id) DO UPDATE SET
                                open_price = EXCLUDED.open_price,
                                high_price = EXCLUDED.high_price,
                                low_price = EXCLUDED.low_price,
                                close_price = EXCLUDED.close_price,
                                volume = EXCLUDED.volume;
                            """
                        ),
                        {
                            "sec_id": sec_id,
                            "date_id": date_id,
                            "open": round(float(row["Open"]), 4),
                            "high": round(float(row["High"]), 4),
                            "low": round(float(row["Low"]), 4),
                            "close": round(float(row["Close"]), 4),
                            "vol": vol,
                        },
                    )

if __name__ == "__main__":
    engine = get_engine()
    cfg = Path("config/basket_definitions.json")
    ingestor = IngestionEngine(engine=engine, config_path=cfg)
    print("Starting market data ingestion pipeline...")
    ingestor.ingest_market_prices(period="1y")
    print("market data ingestion complete")


