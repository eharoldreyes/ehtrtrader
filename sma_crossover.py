"""
strategies/sma_crossover.py
Simple Moving Average (SMA) crossover strategy.

Signal logic:
  BUY  when fast MA crosses ABOVE slow MA (golden cross)
  SELL when fast MA crosses BELOW slow MA (death cross)
"""

import pandas as pd
from loguru import logger


def compute_signals(
    price_data: dict[str, pd.DataFrame],
    fast_period: int = 10,
    slow_period: int = 50,
) -> dict[str, str]:
    """
    Given a dict of symbol -> DataFrame (must have a 'close' column),
    return a dict of symbol -> signal: "BUY" | "SELL" | "HOLD".
    """
    signals: dict[str, str] = {}

    for symbol, df in price_data.items():
        if len(df) < slow_period + 1:
            logger.warning(f"{symbol}: not enough bars ({len(df)}) for slow MA={slow_period}. Skipping.")
            signals[symbol] = "HOLD"
            continue

        df = df.copy()
        df["fast_ma"] = df["close"].rolling(fast_period).mean()
        df["slow_ma"] = df["close"].rolling(slow_period).mean()

        # Drop rows until both MAs are populated
        df = df.dropna(subset=["fast_ma", "slow_ma"]).reset_index(drop=True)

        latest   = df.iloc[-1]
        previous = df.iloc[-2]

        fast_now,  slow_now  = latest["fast_ma"],   latest["slow_ma"]
        fast_prev, slow_prev = previous["fast_ma"], previous["slow_ma"]

        if fast_prev <= slow_prev and fast_now > slow_now:
            signal = "BUY"
        elif fast_prev >= slow_prev and fast_now < slow_now:
            signal = "SELL"
        else:
            signal = "HOLD"

        logger.info(
            f"{symbol} | fast_MA={fast_now:.2f}  slow_MA={slow_now:.2f}  "
            f"prev_fast={fast_prev:.2f}  prev_slow={slow_prev:.2f}  → {signal}"
        )
        signals[symbol] = signal

    return signals
