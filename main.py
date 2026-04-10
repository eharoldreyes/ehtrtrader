"""
main.py  –  IBKR SMA Crossover Trading Bot
Run once manually, or schedule via cron/Task Scheduler.

Usage:
    python main.py
"""

import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# ── Load environment variables ─────────────────────────────────────────────
load_dotenv(Path(__file__).parent / "config" / ".env")

TWS_HOST           = os.getenv("TWS_HOST", "127.0.0.1")
TWS_PORT           = int(os.getenv("TWS_PORT", 7497))
CLIENT_ID          = int(os.getenv("CLIENT_ID", 1))
FAST_MA            = int(os.getenv("FAST_MA", 10))
SLOW_MA            = int(os.getenv("SLOW_MA", 50))
POSITION_SIZE      = int(os.getenv("POSITION_SIZE", 10))
MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", 5000))
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", 0.05))

# ── Logger setup ───────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days", level="DEBUG")

# ── Project imports ────────────────────────────────────────────────────────
from config.watchlist import WATCHLIST
from core.connection import IBKRApp
from core.data import fetch_historical_bars
from core.executor import execute_signals
from strategies.sma_crossover import compute_signals


def run_bot():
    logger.info("=" * 60)
    logger.info("IBKR SMA Crossover Bot  –  Paper Account")
    logger.info(f"Strategy: fast={FAST_MA}d / slow={SLOW_MA}d  |  size={POSITION_SIZE} shares")
    logger.info("=" * 60)

    app = IBKRApp(TWS_HOST, TWS_PORT, CLIENT_ID)

    try:
        # 1. Connect
        app.connect_and_run()
        time.sleep(1)   # let TWS finish handshake

        # 2. Snapshot current positions & account value
        app.reqPositions()
        app.reqAccountSummary(9001, "All", "NetLiquidation")
        time.sleep(3)

        logger.info(f"Account NAV: ${app.account_value:,.2f}")
        logger.info(f"Open positions: {app.positions or 'none'}")

        # 3. Fetch historical price data
        price_data = fetch_historical_bars(
            app,
            WATCHLIST,
            duration=f"{SLOW_MA + 10} D",   # enough bars for slow MA
            bar_size="1 day",
        )

        if not price_data:
            logger.error("No price data returned. Exiting.")
            return

        # 4. Generate signals
        signals = compute_signals(price_data, fast_period=FAST_MA, slow_period=SLOW_MA)

        # 5. Execute orders
        execute_signals(
            app,
            signals,
            WATCHLIST,
            position_size=POSITION_SIZE,
            max_position_value=MAX_POSITION_VALUE,
            stop_loss_pct=STOP_LOSS_PCT,
        )

        # Give TWS time to process orders before disconnecting
        time.sleep(5)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
    finally:
        app.disconnect_gracefully()
        logger.info("Bot session complete.")


if __name__ == "__main__":
    run_bot()
