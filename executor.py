"""
core/executor.py
Translates strategy signals into IBKR orders, with basic risk checks.
"""

import os
from loguru import logger
from core.connection import IBKRApp, make_stock_contract, make_market_order


def execute_signals(
    app: IBKRApp,
    signals: dict[str, str],
    symbols: list[dict],
    position_size: int,
    max_position_value: float,
    stop_loss_pct: float,
):
    """
    For each symbol with a BUY/SELL signal, place a market order if
    risk controls pass. HOLD signals are silently skipped.
    """
    symbol_map = {s["symbol"]: s for s in symbols}

    for symbol, signal in signals.items():
        if signal == "HOLD":
            continue

        current_qty = app.positions.get(symbol, 0)

        # ── Risk gate ──────────────────────────────────────────────────────
        if signal == "BUY" and current_qty > 0:
            logger.info(f"{symbol}: already long ({current_qty} shares). Skipping BUY.")
            continue

        if signal == "SELL" and current_qty <= 0:
            logger.info(f"{symbol}: no long position to sell. Skipping SELL.")
            continue

        # ── Determine order qty ────────────────────────────────────────────
        qty = abs(current_qty) if signal == "SELL" else position_size

        # ── Place order ────────────────────────────────────────────────────
        cfg      = symbol_map[symbol]
        contract = make_stock_contract(symbol, cfg.get("exchange", "SMART"), cfg.get("currency", "USD"))
        order    = make_market_order(signal, qty)

        order_id = app.next_order_id
        app.next_order_id += 1

        logger.info(f"Placing {signal} order: {qty} × {symbol}  (orderId={order_id})")
        app.placeOrder(order_id, contract, order)
