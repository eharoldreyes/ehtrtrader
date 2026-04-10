"""
test_buy.py  –  Place a single market BUY order for 1 share of AAPL.
No strategy. No risk checks. For connectivity / order flow testing only.

Usage:
    python test_buy.py

Requirements:
    - TWS (or IB Gateway) running and logged in (paper account recommended)
    - API access enabled on port 7497
"""

import sys
import time
import threading
from loguru import logger
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


# ── Config ────────────────────────────────────────────────────────────────────
TWS_HOST  = "127.0.0.1"
TWS_PORT  = 7497        # paper TWS default (use 7496 for live)
CLIENT_ID = 2           # use a different client ID than the main bot

SYMBOL    = "AAPL"
QUANTITY  = 1           # shares


# ── Minimal IBKR app ──────────────────────────────────────────────────────────
class TestApp(EWrapper, EClient):

    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.next_order_id  = None
        self._ready         = threading.Event()
        self.order_statuses = {}  # orderId -> status

    # Called when TWS sends the first valid order ID (confirms connection)
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._ready.set()
        logger.debug(f"nextValidId: {orderId}")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 2104 / 2106 / 2158 are informational market-data messages, not errors
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol   = symbol
    c.secType  = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def make_market_buy(quantity: int) -> Order:
    o = Order()
    o.action        = "BUY"
    o.totalQuantity = quantity
    o.orderType     = "MKT"
    o.tif           = "DAY"
    o.eTradeOnly    = False   # must be False for newer TWS versions
    o.firmQuoteOnly = False   # same
    return o


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    app = TestApp()

    logger.info(f"Connecting to TWS at {TWS_HOST}:{TWS_PORT} …")
    app.connect(TWS_HOST, TWS_PORT, CLIENT_ID)

    # Run the message loop in a background thread
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()

    # Wait for the connection handshake (nextValidId)
    if not app._ready.wait(timeout=10):
        logger.error("Timed out waiting for TWS connection. Is TWS running?")
        sys.exit(1)

    logger.success("Connected to TWS.")

    # Place the order
    contract = make_contract(SYMBOL)
    order    = make_market_buy(QUANTITY)
    order_id = app.next_order_id

    logger.info(f"Placing BUY order: {QUANTITY} × {SYMBOL}  (orderId={order_id})")
    app.placeOrder(order_id, contract, order)

    # Wait a few seconds for status callbacks
    time.sleep(5)

    # Report result
    status = app.order_statuses.get(order_id, "no status received")
    logger.info(f"Final order status: {status}")

    app.disconnect()
    logger.info("Disconnected. Done.")


if __name__ == "__main__":
    main()
