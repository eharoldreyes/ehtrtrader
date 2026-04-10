"""
core/connection.py
Thin wrapper around ibapi's EClient / EWrapper for the bot.
"""

import threading
import time
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from loguru import logger


class IBKRApp(EWrapper, EClient):
    """Combines EWrapper (callbacks) and EClient (requests)."""

    def __init__(self, host: str, port: int, client_id: int):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.host = host
        self.port = port
        self.client_id = client_id

        self.next_order_id: int | None = None
        self.connected_event = threading.Event()

        # Storage filled by callbacks
        self.historical_data: dict[str, list] = {}  # symbol -> list of bar dicts
        self.positions: dict[str, float] = {}        # symbol -> qty
        self.account_value: float = 0.0

    # ── Connection helpers ──────────────────────────────────────────────────

    def connect_and_run(self):
        """Connect to TWS and start the message loop in a background thread."""
        logger.info(f"Connecting to TWS at {self.host}:{self.port} (clientId={self.client_id})")
        self.connect(self.host, self.port, self.client_id)

        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()

        # Wait until nextValidId arrives (signals successful connection)
        if not self.connected_event.wait(timeout=10):
            raise ConnectionError("Timed out waiting for TWS connection.")
        logger.success("Connected to TWS.")

    def disconnect_gracefully(self):
        logger.info("Disconnecting from TWS…")
        self.disconnect()

    # ── EWrapper callbacks ──────────────────────────────────────────────────

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self.connected_event.set()
        logger.debug(f"nextValidId received: {orderId}")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 2104/2158 are informational market-data farm messages – not real errors
        if errorCode in (2104, 2106, 2158):
            logger.debug(f"[Info {errorCode}] {errorString}")
        else:
            logger.error(f"[Error reqId={reqId} code={errorCode}] {errorString}")

    def historicalData(self, reqId: int, bar):
        symbol = self._reqid_to_symbol.get(reqId, str(reqId))
        self.historical_data.setdefault(symbol, []).append({
            "date":   bar.date,
            "open":   bar.open,
            "high":   bar.high,
            "low":    bar.low,
            "close":  bar.close,
            "volume": bar.volume,
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        symbol = self._reqid_to_symbol.get(reqId, str(reqId))
        logger.debug(f"Historical data complete for {symbol} ({len(self.historical_data.get(symbol, []))} bars)")

    def position(self, account: str, contract: Contract, position: float, avgCost: float):
        super().position(account, contract, position, avgCost)
        self.positions[contract.symbol] = position
        logger.debug(f"Position update: {contract.symbol} qty={position} avgCost={avgCost}")

    def positionEnd(self):
        super().positionEnd()
        logger.debug("Position snapshot complete.")

    def accountSummary(self, reqId, account, tag, value, currency):
        if tag == "NetLiquidation":
            self.account_value = float(value)
            logger.debug(f"Account NAV: {value} {currency}")

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        logger.info(f"Order {orderId} status={status} filled={filled} avgPrice={avgFillPrice}")

    def openOrder(self, orderId, contract, order, orderState):
        logger.debug(f"Open order: {orderId} {contract.symbol} {order.action} {order.totalQuantity} @ {order.lmtPrice}")

    # ── Internal helpers ────────────────────────────────────────────────────

    _reqid_to_symbol: dict[int, str] = {}   # class-level registry

    @classmethod
    def _register_req(cls, req_id: int, symbol: str):
        cls._reqid_to_symbol[req_id] = symbol


def make_stock_contract(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
    """Return a basic US stock Contract object."""
    c = Contract()
    c.symbol   = symbol
    c.secType  = "STK"
    c.exchange = exchange
    c.currency = currency
    return c


def make_market_order(action: str, quantity: int) -> Order:
    """Return a simple Market order (MOC for paper trading safety)."""
    o = Order()
    o.action          = action          # "BUY" or "SELL"
    o.totalQuantity   = quantity
    o.orderType       = "MKT"
    o.tif             = "DAY"
    o.eTradeOnly      = False   # must be False for newer TWS versions
    o.firmQuoteOnly   = False   # same
    return o
