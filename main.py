"""
main.py  –  TWS connection and core order functions.

Usage:
    python main.py buy  AAPL 1
    python main.py sell MSFT 5
"""

import sys
import re
import time
import argparse
import threading
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import os

import holidays
from loguru import logger
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

TWS_HOST  = os.getenv("TWS_HOST", "127.0.0.1")
TWS_PORT  = int(os.getenv("TWS_PORT", 7497))
CLIENT_ID = int(os.getenv("CLIENT_ID", 1))
WATCHLIST = [t.strip() for t in os.getenv("WATCHLIST", "").split(",") if t.strip()]


# ── Duration parser ───────────────────────────────────────────────────────────
TRADING_HOURS_PER_DAY = 6.5   # NYSE regular session: 9:30–16:00 ET


def parse_duration(s: str) -> float:
    """
    Parse a duration string into total trading hours.

      "5d"    → 5 trading days  →  32.5 h
      "8h"    → 8 trading hours →   8.0 h
      "2d4h"  → 2 days + 4 h   →  17.0 h
      "5"     → 5 days (default unit)

    Returns total trading hours as a float.
    Raises ValueError on unrecognised input.
    """
    s = s.strip().lower().replace(" ", "")
    d_match = re.search(r"(\d+(?:\.\d+)?)d", s)
    h_match = re.search(r"(\d+(?:\.\d+)?)h", s)

    days  = float(d_match.group(1)) if d_match else 0.0
    hours = float(h_match.group(1)) if h_match else 0.0

    if not d_match and not h_match:
        try:
            days = float(s)          # bare number → treat as days
        except ValueError:
            raise ValueError(
                f"Cannot parse duration '{s}'. "
                "Use: '5d', '8h', '2d4h', or a plain number (days)."
            )

    return days * TRADING_HOURS_PER_DAY + hours


def format_duration(total_hours: float) -> str:
    """Convert total trading hours back to a human-readable string."""
    days  = int(total_hours // TRADING_HOURS_PER_DAY)
    hours = total_hours % TRADING_HOURS_PER_DAY
    if days and hours:
        return f"{days}d {hours:.1f}h"
    if days:
        return f"{days}d"
    return f"{total_hours:.1f}h"


# ── Market hours check ───────────────────────────────────────────────────────
MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
ET           = ZoneInfo("America/New_York")
NYSE_HOLIDAYS = holidays.NYSE()


def is_market_open() -> bool:
    """Return True if the NYSE is currently open for regular trading."""
    now  = datetime.now(ET)
    today = now.date()

    if now.weekday() >= 5:                  # Saturday=5, Sunday=6
        logger.warning(f"Market closed — it's the weekend ({now.strftime('%A')}).")
        return False

    if today in NYSE_HOLIDAYS:
        holiday_name = NYSE_HOLIDAYS.get(today)
        logger.warning(f"Market closed — NYSE holiday: {holiday_name}.")
        return False

    current_time = now.time().replace(tzinfo=None)
    if not (MARKET_OPEN <= current_time < MARKET_CLOSE):
        logger.warning(
            f"Market closed — current ET time is {now.strftime('%H:%M')}. "
            f"Regular hours: 09:30–16:00 ET."
        )
        return False

    return True


# ── TWS App ───────────────────────────────────────────────────────────────────
class IBKRApp(EWrapper, EClient):
    """Shared TWS connection used by all order scripts."""

    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.next_order_id  = None
        self._ready         = threading.Event()
        self.order_statuses = {}  # orderId -> status

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._ready.set()
        logger.debug(f"nextValidId: {orderId}")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2158):
            logger.debug(f"[Info {errorCode}] {errorString}")
        else:
            logger.error(f"[Error reqId={reqId} code={errorCode}] {errorString}")

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        self.order_statuses[orderId] = status
        logger.info(
            f"Order {orderId}: status={status}  filled={filled}  "
            f"remaining={remaining}  avgPrice={avgFillPrice}"
        )

    def openOrder(self, orderId, contract, order, orderState):
        logger.debug(
            f"Open order confirmed: {orderId} {contract.symbol} "
            f"{order.action} {order.totalQuantity}"
        )


# ── Connection ────────────────────────────────────────────────────────────────
def connect() -> IBKRApp:
    """Connect to TWS and return a ready IBKRApp instance."""
    app = IBKRApp()

    logger.info(f"Connecting to TWS at {TWS_HOST}:{TWS_PORT} …")
    app.connect(TWS_HOST, TWS_PORT, CLIENT_ID)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()

    if not app._ready.wait(timeout=10):
        logger.error("Timed out waiting for TWS. Is TWS running with API enabled?")
        sys.exit(1)

    logger.success("Connected to TWS.")
    return app


# ── Contract / Order helpers ──────────────────────────────────────────────────
def make_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol   = symbol
    c.secType  = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def make_order(action: str, quantity: int) -> Order:
    o = Order()
    o.action        = action        # "BUY" or "SELL"
    o.totalQuantity = quantity
    o.orderType     = "MKT"
    o.tif           = "DAY"
    o.eTradeOnly    = False
    o.firmQuoteOnly = False
    return o


# ── Core order functions ──────────────────────────────────────────────────────
def buy(app: IBKRApp, symbol: str, quantity: int):
    """Place a market BUY order."""
    contract = make_contract(symbol)
    order    = make_order("BUY", quantity)
    order_id = app.next_order_id
    app.next_order_id += 1

    logger.info(f"Placing BUY: {quantity} × {symbol}  (orderId={order_id})")
    app.placeOrder(order_id, contract, order)

    time.sleep(5)
    status = app.order_statuses.get(order_id, "no status received")
    logger.info(f"Final status: {status}")


def sell(app: IBKRApp, symbol: str, quantity: int):
    """Place a market SELL order."""
    contract = make_contract(symbol)
    order    = make_order("SELL", quantity)
    order_id = app.next_order_id
    app.next_order_id += 1

    logger.info(f"Placing SELL: {quantity} × {symbol}  (orderId={order_id})")
    app.placeOrder(order_id, contract, order)

    time.sleep(5)
    status = app.order_statuses.get(order_id, "no status received")
    logger.info(f"Final status: {status}")


# ── Strategy parameter menu ───────────────────────────────────────────────────
def prompt_params(strategy_name: str, schema: list) -> dict:
    """
    Interactively ask the user to configure strategy parameters.
    Press Enter on any prompt to keep the default value.
    """
    sep = "━" * 60
    print(f"\n{sep}")
    print(f"  Strategy : {strategy_name}")
    print(f"  Press Enter to keep the default, or type a new value.")
    print(f"{sep}")

    col     = max(len(p["label"]) for p in schema) + 2
    results = {}
    changed = []

    for p in schema:
        key      = p["key"]
        label    = p["label"]
        default  = p["default"]
        unit     = p.get("unit", "")
        hint     = p.get("hint", "")
        display  = f"{default}{unit}"
        prompt   = f"  {label:<{col}} [default: {display:>10}] : "

        # Show hint line for complex params (e.g. duration)
        if hint:
            print(f"  {'':>{col}}   ↳ {hint}")

        raw = input(prompt).strip()

        if raw:
            # Duration gets special validation + feedback
            if key == "duration":
                try:
                    total_h = parse_duration(raw)
                    print(f"    → {format_duration(total_h)}  =  {total_h:.1f} trading hours")
                    results[key] = raw
                    changed.append(key)
                except ValueError as e:
                    print(f"    ⚠  {e}  Using default '{default}'.")
                    results[key] = default
            else:
                try:
                    results[key] = p["type"](raw)
                    changed.append(key)
                except ValueError:
                    print(f"    ⚠  Invalid value '{raw}', using default '{display}'.")
                    results[key] = default
        else:
            results[key] = default

    # ── Confirmation summary ──────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  Confirmed parameters:")
    for p in schema:
        key   = p["key"]
        val   = results[key]
        unit  = p.get("unit", "")
        tag   = "  ← changed" if key in changed else ""

        # For duration, also show parsed hours
        if key == "duration":
            try:
                total_h = parse_duration(str(val))
                extra = f"  ({total_h:.1f} trading hours)"
            except ValueError:
                extra = ""
            print(f"    {key:<26} = {val}{extra}{tag}")
        else:
            print(f"    {key:<26} = {val}{unit}{tag}")

    print(f"{sep}\n")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="IBKR Trading Bot",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py -sym NVDA -strat trailing_stop_loss\n"
            "  python main.py buy  AAPL 1\n"
            "  python main.py sell MSFT 5\n"
            "  python main.py watchlist\n"
            "  python main.py strategies\n"
        ),
    )

    # ── Strategy flags (primary mode) ────────────────────────────────────────
    parser.add_argument("-sym",   type=str, help="Ticker symbol  (e.g. NVDA)")
    parser.add_argument("-strat", type=str, help="Strategy name  (e.g. trailing_stop_loss)")

    # ── Utility sub-commands (optional positional) ────────────────────────────
    parser.add_argument("command",  nargs="?",
                        choices=["buy", "sell", "watchlist", "strategies"])
    parser.add_argument("cmd_sym",  nargs="?", type=str, help=argparse.SUPPRESS)
    parser.add_argument("cmd_qty",  nargs="?", type=int, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # ── Route: strategy mode ──────────────────────────────────────────────────
    if args.strat:
        if not args.sym:
            parser.error("Strategy mode requires: -sym")

        if not is_market_open():
            logger.error("Strategy aborted — market is not open.")
            sys.exit(1)

        import strategies
        try:
            strategy_mod = strategies.get(args.strat)
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)

        params = prompt_params(args.strat, strategy_mod.PARAMS)
        strategy_mod.run(args.sym, params)
        sys.exit(0)

    # ── Route: list strategies ────────────────────────────────────────────────
    if args.command == "strategies":
        import strategies
        names = strategies.list_strategies()
        logger.info(f"Available strategies ({len(names)}):")
        for name in names:
            logger.info(f"  • {name}")
        sys.exit(0)

    # ── Route: watchlist ──────────────────────────────────────────────────────
    if args.command == "watchlist":
        if WATCHLIST:
            logger.info(f"Watchlist ({len(WATCHLIST)} symbols): {', '.join(WATCHLIST)}")
        else:
            logger.warning("Watchlist is empty. Add tickers to WATCHLIST in .env")
        sys.exit(0)

    # ── Route: manual buy / sell ──────────────────────────────────────────────
    if args.command in ("buy", "sell"):
        if not args.cmd_sym or not args.cmd_qty:
            parser.error(f"{args.command} requires <symbol> <quantity>")

        if not is_market_open():
            logger.error("Order aborted — market is not open.")
            sys.exit(1)

        app = connect()
        if args.command == "buy":
            buy(app, args.cmd_sym.upper(), args.cmd_qty)
        else:
            sell(app, args.cmd_sym.upper(), args.cmd_qty)
        app.disconnect()
        logger.info("Done.")
        sys.exit(0)

    parser.print_help()
