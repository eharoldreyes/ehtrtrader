"""
core/data.py
Fetches historical price bars from TWS for a list of symbols.
"""

import time
import pandas as pd
from loguru import logger
from core.connection import IBKRApp, make_stock_contract


# Unique request ID counter (must not collide with order IDs)
_REQ_ID_BASE = 2000


def fetch_historical_bars(
    app: IBKRApp,
    symbols: list[dict],
    duration: str = "100 D",        # e.g. "100 D", "6 M"
    bar_size: str = "1 day",
    what_to_show: str = "MIDPOINT",
) -> dict[str, pd.DataFrame]:
    """
    Request historical daily bars for each symbol.
    Returns a dict: symbol -> DataFrame with columns [date, open, high, low, close, volume].
    """
    results: dict[str, pd.DataFrame] = {}

    for i, sym_cfg in enumerate(symbols):
        symbol   = sym_cfg["symbol"]
        exchange = sym_cfg.get("exchange", "SMART")
        currency = sym_cfg.get("currency", "USD")
        req_id   = _REQ_ID_BASE + i

        # Clear previous data for this symbol
        app.historical_data.pop(symbol, None)
        IBKRApp._register_req(req_id, symbol)

        contract = make_stock_contract(symbol, exchange, currency)

        logger.info(f"Requesting historical data for {symbol} (reqId={req_id})")
        app.reqHistoricalData(
            req_id,
            contract,
            "",               # endDateTime – empty = now
            duration,
            bar_size,
            what_to_show,
            1,                # useRTH: 1 = regular trading hours only
            1,                # formatDate: 1 = string dates
            False,
            [],
        )

        # Simple poll-wait for data (good enough for daily strategies)
        for _ in range(30):
            time.sleep(1)
            bars = app.historical_data.get(symbol)
            if bars and bars[-1]["date"].startswith("finished"):
                break

        bars = app.historical_data.get(symbol, [])
        # Strip the sentinel "finished" row TWS appends
        bars = [b for b in bars if not str(b["date"]).startswith("finished")]

        if not bars:
            logger.warning(f"No historical data returned for {symbol}")
            continue

        df = pd.DataFrame(bars)
        df["date"]  = pd.to_datetime(df["date"])
        df["close"] = df["close"].astype(float)
        df = df.sort_values("date").reset_index(drop=True)
        results[symbol] = df
        logger.success(f"{symbol}: {len(df)} bars fetched")

    return results
