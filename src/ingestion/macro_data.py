"""Macroeconomis interest rates ingestion pipeline."""

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import yfinance as yf
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.db.connection import get_engine


class MacroDataIngestion:

    def __init__(self, engine: Engine, config_path: Path):
        self.engine = engine
        self.config_path = config_path
        self.config_data = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load benchmark definitions from a JSON file."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _ensure_date_dim(self, conn: Any, dt: datetime) -> int:
        date_id = int(dt.strftime("%Y%m%d"))
        day_of_week = dt.isoweekday()
        is_trading = day_of_week <= 5

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

    def ingest_macro_rates(self, period: str = "1mo") -> None:
        benchmarks = self.config_data.get("benchmarks", [])
        if not benchmarks:
            print("No macro benchmarks found in configuration")
            return

        tickers = [b["ticker"] for b in benchmarks]
        benchmark_map = {b["ticker"]: b["benchmark"] for b in benchmarks}

        data = yf.download(
            tickers=tickers,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
        )

        with self.engine.begin() as conn:
            for ticker in tickers:
                benchmark_name = benchmark_map[ticker]
                df = data[ticker] if len(tickers) > 1 else data
                df = df.dropna(how="all")

                for ts, row in df.iterrows():
                    dt = pd.to_datetime(ts)
                    date_id = self._ensure_date_dim(conn, dt)

                    close_val = row.get("Close")
                    if close_val is None or pd.isna(close_val):
                        continue

                    # yfinance podaje stopy w punktach procentowych (np. 5.25 dla 5.25%).
                    # Konwertujemy do ułamka dziesiętnego wymaganego przez NUMERIC(8, 6).

                    rate_decimal = round(float(close_val) / 100.0, 6)

                    conn.execute(
                        text(
                            """
                            INSERT INTO fact_macro_rates (date_id, risk_free_rate, rate_benchmark)
                            VALUES (:date_id, :rate, :benchmark)
                            ON CONFLICT (date_id, rate_benchmark) DO UPDATE SET
                                risk_free_rate = EXCLUDED.risk_free_rate;
                            """
                        ),
                        {
                            "date_id": date_id,
                            "rate": rate_decimal,
                            "benchmark": benchmark_name,
                        }
                    )


if __name__ == "__main__":
    engine = get_engine()
    cfg = Path("config/macro_definitions.json")
    ingestor = MacroDataIngestion(engine=engine, config_path=cfg)
    print("Starting macro data ingestion from pipeline...")
    ingestor.ingest_macro_rates(period="1mo")
    print("Macro data ingestion complete.")