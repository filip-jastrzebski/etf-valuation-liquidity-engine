"""Main orchestrator for ETF Valuation & Liquidity Engine.

Executes the end-to-end data pipeline:
1. Ingestion of securities and FX market data with quality gating.
2. Ingestion of macroeconomic benchmark rates.
3. Synthetic iNAV valuation and anomaly detection.
"""

import sys
import logging
from pathlib import Path
from sqlalchemy import text

from src.db.connection import get_engine
from src.ingestion.market_data import IngestionEngine
from src.ingestion.macro_data import MacroDataIngestion
from src.valuation.synthetic_nav import SyntheticNAVEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("NavGuardPipeline")


def run_pipeline(period: str = "1y") -> None:
    """Execute the full valuation and ingestion workflow."""
    logger.info("Initializing database connection pool...")
    engine = get_engine()

    # Paths for config files
    config_dir = Path(__file__).resolve().parent.parent / "config"
    basket_cfg = config_dir / "basket_definitions.json"
    macro_cfg = config_dir / "macro_definitions.json"

    # 1. Downloading market data and currency pairs
    logger.info("--- [STEP 1/3] Starting Market Data Ingestion & Quality Checks ---")
    market_ingestor = IngestionEngine(engine=engine, config_path=basket_cfg)
    market_ingestor.ingest_market_prices(period=period)
    logger.info("Market data ingestion completed successfully.")

    # 2. Downloading macroeconomic data
    logger.info("--- [STEP 2/3] Starting Macro Rates Ingestion ---")
    macro_ingestor = MacroDataIngestion(engine=engine, config_path=macro_cfg)
    macro_ingestor.ingest_macro_rates(period=period)
    logger.info("Macro rates ingestion completed successfully.")

    # 3. Synthetic valuation engine (Synthetic iNAV)
    logger.info("--- [STEP 3/3] Running Synthetic NAV & Arbitrage Engine ---")
    nav_engine = SyntheticNAVEngine(engine=engine)

    with engine.connect() as conn:
        etfs = conn.execute(
            text("SELECT ticker FROM dim_security WHERE asset_class = 'ETF' ORDER BY ticker")
        ).scalars().all()

    if not etfs:
        logger.warning("No ETFs found in dim_security. Check ingestion step.")
        return

    logger.info("Executing valuation for ETF universe: %s", etfs)
    for ticker in etfs:
        logger.info("Calculating synthetic iNAV for: %s", ticker)
        nav_engine.calculate_nav_for_etf(
            etf_ticker=ticker,
            use_dynamic_zscore=True,
            z_threshold=2.0,
        )

    logger.info("--- Pipeline execution finished successfully! ---")


if __name__ == "__main__":
    try:
        run_pipeline(period="1y")
    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        logger.error("Pipeline failed with unhandled error: %s", exc, exc_info=True)
        sys.exit(1)