"""
main.py  –  TWS connection and core order functions.

Usage:
    python main.py buy  AAPL 1
    python main.py sell MSFT 5
"""

import sys
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


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    parser = argparse.ArgumentParser(prog="main.py", description="IBKR order CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # buy <symbol> <quantity>
    buy_parser = subparsers.add_parser("buy",  help="Place a market BUY order")
    buy_parser.add_argument("symbol",   type=str, help="Ticker symbol (e.g. AAPL)")
    buy_parser.add_argument("quantity", type=int, help="Number of shares")

    # sell <symbol> <quantity>
    sell_parser = subparsers.add_parser("sell", help="Place a market SELL order")
    sell_parser.add_argument("symbol",   type=str, help="Ticker symbol (e.g. AAPL)")
    sell_parser.add_argument("quantity", type=int, help="Number of shares")

    # watchlist
    subparsers.add_parser("watchlist", help="Print the current watchlist")

    args = parser.parse_args()

    # ── Watchlist command (no TWS needed) ─────────────────────────────────────
    if args.command == "watchlist":
        if WATCHLIST:
            logger.info(f"Watchlist ({len(WATCHLIST)} symbols): {', '.join(WATCHLIST)}")
        else:
            logger.warning("Watchlist is empty. Add tickers to WATCHLIST in .env")
        sys.exit(0)

    # ── Order commands ────────────────────────────────────────────────────────
    if not is_market_open():
        logger.error("Order aborted — market is not open.")
        sys.exit(1)

    app = connect()

    if args.command == "buy":
        buy(app, args.symbol.upper(), args.quantity)
    elif args.command == "sell":
        sell(app, args.symbol.upper(), args.quantity)

    app.disconnect()
    logger.info("Done.")
