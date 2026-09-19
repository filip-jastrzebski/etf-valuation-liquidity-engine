"""Synthetic iNAV Calculation Engine and Arbitrage Discrepancy Detector."""

from decimal import Decimal
from typing import Dict, List, Optional
import pandas as pd
import math
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.db.connection import get_engine


class SyntheticNAVEngine:

    def __init__(self, engine: Engine, anomaly_threshold_bps = 50.0):
        self.engine = engine
        self.anomaly_threshold_bps = Decimal(str(anomaly_threshold_bps))

    def calculate_nav_for_etf(
            self,
            etf_ticker: str,
            cash_component: float = 0.0,
            anomaly_threshold_bps: Optional[float] = None,
            use_dynamic_zscore: bool = False,
            z_threshold: float = 2.0,
            min_anomaly_bps: float = 30.0,
    ) -> List[Dict]:
        """
        Calculates daily synthetic iNAV using Divisor model.
        Supports both fixed BPS threshold and dynamic statistical Z-Score (|Z| >= k*sigma).
        """
        cash = Decimal(str(cash_component))
        threshold = (
            Decimal(str(anomaly_threshold_bps))
            if anomaly_threshold_bps is not None
            else self.anomaly_threshold_bps
        )

        with self.engine.begin() as conn:
            # 1. Download security_id for ETF.
            etf_row = conn.execute(
                text("SELECT security_id FROM dim_security WHERE ticker = :ticker AND asset_class = 'ETF'"),
                {"ticker": etf_ticker},
            ).mappings().fetchone()

            if not etf_row:
                raise ValueError(f"ETF with ticker '{etf_ticker}' not found in dim_security.")

            etf_id = etf_row["security_id"]

            # 2. Download market prices for same ETF.
            etf_prices = conn.execute(
                text(
                    """
                    SELECT date_id, close_price AS etf_price
                    FROM fact_market_prices
                    WHERE security_id = :etf_id
                    ORDER BY date_id ASC
                    """
                ),
                {
                    "etf_id": etf_id
                },
            ).mappings().fetchall()

            etf_price_map = {row["date_id"]: Decimal(str(row["etf_price"])) for row in etf_prices}

            # 3. Download the basket, holding company prices, and FX currency rates.
            holding_query = text(
                """
                SELECT 
                    b.component_security_id,
                    c.ticker AS comp_ticker,
                    c.currency AS comp_currency,
                    b.weight,
                    p.date_id,
                    p.close_price AS comp_close,
                    CASE 
                        WHEN c.currency = 'USD' THEN 1.0
                        ELSE (
                            SELECT fx.close_price 
                            FROM fact_market_prices fx
                            JOIN dim_security fx_sec ON fx.security_id = fx_sec.security_id
                            WHERE fx_sec.ticker = (c.currency || '=X')
                              AND fx.date_id <= p.date_id
                            ORDER BY fx.date_id DESC
                            LIMIT 1
                        )
                    END AS fx_rate
                FROM dim_etf_basket b
                JOIN dim_security c ON b.component_security_id = c.security_id
                JOIN fact_market_prices p ON b.component_security_id = p.security_id
                WHERE b.etf_security_id = :etf_id
                ORDER BY p.date_id, c.ticker;
                """
            )
            raw_holdings = conn.execute(holding_query, {"etf_id": etf_id}).mappings().fetchall()

            if not raw_holdings:
                print(f"No holding data found for ETF {etf_ticker}")
                return []

            # Grouping holdings by date_id
            dates_data: Dict[int, List[Dict]] = {}
            for h in raw_holdings:
                d_id = h["date_id"]
                dates_data.setdefault(d_id, []).append(h)

            sorted_dates = sorted([d for d in dates_data.keys() if d in etf_price_map])
            if not sorted_dates:
                print(f"No overlapping dates between ETF and components for {etf_ticker}.")
                return []

            # 4. Determination of the Divisor on the base date t_0
            t0_date = sorted_dates[0]
            t0_etf_price = etf_price_map[t0_date]
            t0_raw_index = Decimal("0.0")

            for h in dates_data[t0_date]:
                w = Decimal(str(h["weight"]))
                c_price = Decimal(str(h["comp_close"]))
                fx = Decimal(str(h["fx_rate"])) if Decimal(str(h["fx_rate"])) > 0 else Decimal("1.0")
                t0_raw_index += w * (c_price / fx)

            divisor = t0_raw_index / t0_etf_price
            print(f"[{etf_ticker}] Inception Date: {t0_date} | Base Index: {t0_raw_index:.4f} | Divisor: {divisor:.6f}")

            # 5. First loop: computing spread and discrepancy in bps
            temp_calc = []
            for date_id in sorted_dates:
                etf_mkt_price = etf_price_map[date_id]
                raw_portfolio_value = Decimal("0.0")

                for h in dates_data[date_id]:
                    w = Decimal(str(h["weight"]))
                    c_price = Decimal(str(h["comp_close"]))
                    fx = Decimal(str(h["fx_rate"])) if Decimal(str(h["fx_rate"])) > 0 else Decimal("1.0")
                    raw_portfolio_value += w * (c_price / fx)

                synthetic_nav = round((raw_portfolio_value / divisor) + cash, 4)
                nav_spread = round(etf_mkt_price - synthetic_nav, 4)

                if synthetic_nav > Decimal("0.0"):
                    discrepancy_bps = round(((etf_mkt_price - synthetic_nav) / synthetic_nav) * Decimal("10000.0"), 2)
                else:
                    discrepancy_bps = Decimal("0.0")

                temp_calc.append({
                    "date_id": date_id,
                    "etf_market_price": etf_mkt_price,
                    "synthetic_nav": synthetic_nav,
                    "nav_spread": nav_spread,
                    "discrepancy_bps": discrepancy_bps,
                })

            # 6. Second loop: anomaly calculation
            mean_bps = 0.0
            std_bps = 1.0
            
            if use_dynamic_zscore and len(temp_calc) > 1:
                bps_values = [float(r["discrepancy_bps"]) for r in temp_calc]
                mean_bps = sum(bps_values) / len(bps_values)
                variance = sum((x - mean_bps) ** 2 for x in bps_values) / (len(bps_values) - 1)
                std_bps = math.sqrt(variance) if variance > 0 else 1.0
                print(f"[{etf_ticker}] Stats: Mean = {mean_bps:.2f} bps | StdDev = {std_bps:.2f} bps | Z-Cutoff = {z_threshold} sigma")

            results = []
            for item in temp_calc:
                d_bps = float(item["discrepancy_bps"])
                current_date = item["date_id"]

                if current_date == t0_date:
                    is_anomaly = False
                elif use_dynamic_zscore and len(temp_calc) > 1:
                    z_score = abs(d_bps - mean_bps) / std_bps
                    is_anomaly = bool((z_score >= z_threshold) and (abs(d_bps) >= min_anomaly_bps))
                else:
                    is_anomaly = bool(abs(d_bps) >= float(threshold))


                # 7. Save to fact_synthetic_nav
                conn.execute(
                    text(
                        """
                        INSERT INTO fact_synthetic_nav 
                        (etf_security_id, date_id, etf_market_price, synthetic_nav, nav_spread, discrepancy_bps, is_anomaly)
                        VALUES (:etf_id, :date_id, :mkt_price, :inav, :spread, :bps, :anomaly)
                        ON CONFLICT (etf_security_id, date_id) DO UPDATE SET
                            etf_market_price = EXCLUDED.etf_market_price,
                            synthetic_nav = EXCLUDED.synthetic_nav,
                            nav_spread = EXCLUDED.nav_spread,
                            discrepancy_bps = EXCLUDED.discrepancy_bps,
                            is_anomaly = EXCLUDED.is_anomaly;
                        """
                    ),
                    {
                        "etf_id": etf_id,
                        "date_id": item["date_id"],
                        "mkt_price": item["etf_market_price"],
                        "inav": item["synthetic_nav"],
                        "spread": item["nav_spread"],
                        "bps": item["discrepancy_bps"],
                        "anomaly": is_anomaly,
                    },
                )

                results.append({
                    "date_id": item["date_id"],
                    "etf_market_price": float(item["etf_market_price"]),
                    "synthetic_nav": float(item["synthetic_nav"]),
                    "nav_spread": float(item["nav_spread"]),
                    "discrepancy_bps": float(item["discrepancy_bps"]),
                    "is_anomaly": is_anomaly,
                })

            anomalies_detected = sum(1 for r in results if r["is_anomaly"])
            print(f"[{etf_ticker}] Finished: {len(results)} sessions processed | Anomalies detected: {anomalies_detected}")
            return results
        

if __name__ == "__main__":
    db_engine = get_engine()
    engine = SyntheticNAVEngine(engine=db_engine)

    # Uruchamiamy z dynamicznym progiem 2 sigma (|Z| >= 2.0)
    for ticker in ["XLF", "XLK", "EEM"]:
        print(f"\n--- Running valuation for {ticker} ---")
        engine.calculate_nav_for_etf(etf_ticker=ticker, use_dynamic_zscore=True, z_threshold=2.0)