"""
strategies/dca_scalp.py  -  Periodic DCA buy + profit-take sell strategy.

Schedule 1 (every buy_frequency seconds):
  - Buy `amount` USD worth of the symbol if budget allows.
  - Record the purchase (id, shares, buy_price, cost) in the purchase list.
  - Deduct cost from budget.

Schedule 2 (every sell_frequency seconds):
  - Check each item in the purchase list.
  - If an item is profitable >= profit_target_pct, sell it and return proceeds to budget.

Invoked by:
  python main.py -sym BTC  -strat dca_scalp
  python main.py -sym AAPL -strat dca_scalp
"""

import sys
import uuid
import math
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
import main as tws

# ── Parameters ────────────────────────────────────────────────────────────────
PARAMS = [
    {
        "key":     "budget",
        "label":   "Total budget",
        "default": 10000.0,
        "type":    float,
        "unit":    "$",
    },
    {
        "key":     "amount",
        "label":   "Amount per buy",
        "default": 100.0,
        "type":    float,
        "unit":    "$",
    },
    {
        "key":     "buy_frequency",
        "label":   "Buy every",
        "default": 60,
        "type":    int,
        "unit":    "s",
        "hint":    "seconds between each buy order",
    },
    {
        "key":     "sell_frequency",
        "label":   "Sell check every",
        "default": 5,
        "type":    int,
        "unit":    "s",
        "hint":    "seconds between each profit check",
    },
    {
        "key":     "profit_target_pct",
        "label":   "Profit target to trigger sell",
        "default": 2.0,
        "type":    float,
        "unit":    "%",
    },
]

ET = ZoneInfo("America/New_York")


# ── Extended app with serialized price fetching ───────────────────────────────
class StrategyApp(tws.IBKRApp):

    def __init__(self):
        super().__init__()
        self._price_lock  = threading.Lock()   # one price request at a time
        self._price_event = threading.Event()
        self._hist_last   = None
        self.last_price   = None
        self._req_counter = 300

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode == 162:
            logger.warning(f"No historical data for reqId={reqId} — market may be closed.")
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    def historicalData(self, reqId, bar):
        if bar.close and bar.close > 0:
            self._hist_last = bar.close

    def historicalDataEnd(self, reqId, start, end):
        if self._hist_last:
            self.last_price = self._hist_last
            self._hist_last = None
            self._price_event.set()

    def fetch_price(self, contract, crypto: bool = False) -> float | None:
        """Thread-safe price fetch via historical data. No subscription needed."""
        with self._price_lock:
            self._req_counter += 1
            self._price_event.clear()
            self._hist_last = None

            self.reqHistoricalData(
                reqId          = self._req_counter,
                contract       = contract,
                endDateTime    = "",
                durationStr    = "1 D",
                barSizeSetting = "5 mins",
                whatToShow     = "TRADES",
                useRTH         = 0 if crypto else 1,
                formatDate     = 1,
                keepUpToDate   = False,
                chartOptions   = [],
            )

            if not self._price_event.wait(timeout=20):
                logger.warning("Price fetch timed out.")
                return None

            return self.last_price


# ── Main entry point ──────────────────────────────────────────────────────────
def run(symbol: str, params: dict = None):
    defaults = {p["key"]: p["default"] for p in PARAMS}
    p = {**defaults, **(params or {})}

    budget           = p["budget"]
    amount           = p["amount"]
    buy_frequency    = int(p["buy_frequency"])
    sell_frequency   = int(p["sell_frequency"])
    profit_target    = p["profit_target_pct"] / 100
    symbol           = symbol.upper()
    crypto           = tws.is_crypto(symbol)

    purchase_list = []    # [{id, shares, buy_price, cost, order_id}]
    lock          = threading.Lock()
    stop_event    = threading.Event()

    # ── Connect ───────────────────────────────────────────────────────────────
    app = StrategyApp()
    logger.info(f"Connecting to TWS at {tws.TWS_HOST}:{tws.TWS_PORT} ...")
    app.connect(tws.TWS_HOST, tws.TWS_PORT, tws.CLIENT_ID)
    threading.Thread(target=app.run, daemon=True).start()

    if not app._ready.wait(timeout=10):
        logger.error("Timed out waiting for TWS.")
        sys.exit(1)
    logger.success("Connected to TWS.")

    contract = tws.make_contract(symbol)

    # ── Buy loop (Schedule 1) ─────────────────────────────────────────────────
    def buy_loop():
        nonlocal budget

        while not stop_event.is_set():
            with lock:
                current_budget = budget

            if current_budget < amount:
                logger.warning(
                    f"Insufficient budget (${current_budget:.2f} < ${amount:.2f}) "
                    "-- skipping buy."
                )
                stop_event.wait(buy_frequency)
                continue

            price = app.fetch_price(contract, crypto=crypto)
            if not price:
                logger.warning("Could not fetch price -- skipping buy.")
                stop_event.wait(buy_frequency)
                continue

            with lock:
                # Re-check budget inside lock in case sell updated it
                if budget < amount:
                    stop_event.wait(buy_frequency)
                    continue

                order_id = app.next_order_id
                app.next_order_id += 1

                if crypto:
                    order  = tws.make_order("BUY", amount, use_cash_qty=True, crypto=True)
                    shares = amount / price          # estimated from cashQty
                    cost   = amount
                else:
                    shares = math.floor(amount / price)
                    if shares == 0:
                        logger.warning(
                            f"${amount:.2f} too small to buy 1 share of "
                            f"{symbol} at ${price:.2f} -- skipping."
                        )
                        stop_event.wait(buy_frequency)
                        continue
                    order = tws.make_order("BUY", shares)
                    cost  = shares * price

                app.placeOrder(order_id, contract, order)

                item = {
                    "id":        str(uuid.uuid4())[:8],
                    "shares":    shares,
                    "buy_price": price,
                    "cost":      cost,
                    "order_id":  order_id,
                }
                purchase_list.append(item)
                budget -= cost

                logger.info(
                    f"[BUY  #{item['id']}]  {shares:.6g} {symbol} "
                    f"@ ${price:.2f}  cost=${cost:.2f}  "
                    f"budget=${budget:.2f}  positions={len(purchase_list)}"
                )

            stop_event.wait(buy_frequency)

    # ── Sell loop (Schedule 2) ────────────────────────────────────────────────
    def sell_loop():
        nonlocal budget

        while not stop_event.is_set():
            stop_event.wait(sell_frequency)

            with lock:
                if not purchase_list:
                    continue

            price = app.fetch_price(contract, crypto=crypto)
            if not price:
                continue

            with lock:
                to_sell = [
                    item for item in purchase_list
                    if (price - item["buy_price"]) / item["buy_price"] >= profit_target
                ]

                for item in to_sell:
                    order_id = app.next_order_id
                    app.next_order_id += 1

                    order = tws.make_order("SELL", item["shares"], crypto=crypto)
                    app.placeOrder(order_id, contract, order)

                    sell_value  = item["shares"] * price
                    profit      = sell_value - item["cost"]
                    profit_pct  = profit / item["cost"] * 100
                    budget     += sell_value
                    purchase_list.remove(item)

                    logger.info(
                        f"[SELL #{item['id']}]  {item['shares']:.6g} {symbol} "
                        f"@ ${price:.2f}  profit=${profit:.2f} (+{profit_pct:.2f}%)  "
                        f"budget=${budget:.2f}  positions={len(purchase_list)}"
                    )

    # ── Launch ────────────────────────────────────────────────────────────────
    sep = "-" * 54
    logger.info(sep)
    logger.info(f"  DCA Scalp Strategy  --  {symbol}")
    logger.info(sep)
    logger.info(f"  Budget         : ${budget:.2f}")
    logger.info(f"  Amount / buy   : ${amount:.2f}")
    logger.info(f"  Buy every      : {buy_frequency}s")
    logger.info(f"  Sell check     : {sell_frequency}s")
    logger.info(f"  Profit target  : {profit_target * 100:.1f}%")
    logger.info(sep)

    buy_thread  = threading.Thread(target=buy_loop,  daemon=True, name="buy-loop")
    sell_thread = threading.Thread(target=sell_loop, daemon=True, name="sell-loop")
    buy_thread.start()
    sell_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted -- shutting down.")
        stop_event.set()

    buy_thread.join(timeout=5)
    sell_thread.join(timeout=5)

    # ── Final summary ─────────────────────────────────────────────────────────
    with lock:
        logger.info(sep)
        logger.info("  FINAL SUMMARY")
        logger.info(sep)
        logger.info(f"  Remaining budget  : ${budget:.2f}")
        logger.info(f"  Open positions    : {len(purchase_list)}")
        for item in purchase_list:
            unrealized = (item["shares"] * (app.last_price or item["buy_price"])) - item["cost"]
            logger.info(
                f"    #{item['id']}  {item['shares']:.6g} {symbol} "
                f"@ ${item['buy_price']:.2f}  "
                f"cost=${item['cost']:.2f}  "
                f"unrealized=${unrealized:.2f}"
            )
        logger.info(sep)

    app.disconnect()
    logger.info("Strategy session ended.")
