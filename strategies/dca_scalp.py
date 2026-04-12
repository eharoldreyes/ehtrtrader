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
import json
import threading
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
import main as tws

# ── CoinGecko fallback for crypto prices (bypasses IBKR API version limit) ───
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
}


PRICE_CACHE_TTL = 15   # seconds — stays within CoinGecko free tier limits
_price_cache: dict[str, tuple[float, float]] = {}   # symbol -> (price, timestamp)


def get_crypto_price(symbol: str) -> float | None:
    """
    Fetch current crypto price from CoinGecko public API. No key required.
    Caches results for PRICE_CACHE_TTL seconds to respect rate limits.
    """
    symbol = symbol.upper()
    cached_price, cached_at = _price_cache.get(symbol, (None, 0))
    if cached_price and (time.time() - cached_at) < PRICE_CACHE_TTL:
        return cached_price

    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None
    try:
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coin_id}&vs_currencies=usd"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            price = float(data[coin_id]["usd"])
            _price_cache[symbol] = (price, time.time())
            return price
    except Exception as e:
        logger.warning(f"CoinGecko price fetch failed: {e}")
        return cached_price   # return stale cache on error rather than None

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
        self._price_lock   = threading.Lock()   # one price request at a time
        self._price_event  = threading.Event()
        self._hist_last    = None
        self._tick_prices  = {}   # reqId -> price
        self._tick_events  = {}   # reqId -> Event
        self.last_price    = None
        self._req_counter  = 300

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (162, 10285):
            logger.warning(f"Price data unavailable for reqId={reqId}: {errorString}")
            # Unblock any waiting price event
            if reqId in self._tick_events:
                self._tick_events[reqId].set()
            self._price_event.set()
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    # ── Historical data (stocks) ──────────────────────────────────────────────
    def historicalData(self, reqId, bar):
        if bar.close and bar.close > 0:
            self._hist_last = bar.close

    def historicalDataEnd(self, reqId, start, end):
        if self._hist_last:
            self.last_price = self._hist_last
            self._hist_last = None
            self._price_event.set()

    # ── Market data snapshot (crypto) ─────────────────────────────────────────
    def tickPrice(self, reqId, tickType, price, attrib):
        # tickType 4 = LAST, 2 = ASK, 1 = BID
        if tickType in (4, 2, 1) and price > 0:
            self._tick_prices[reqId] = price
            if reqId in self._tick_events:
                self._tick_events[reqId].set()

    def tickSnapshotEnd(self, reqId):
        if reqId in self._tick_events:
            self._tick_events[reqId].set()

    def fetch_price(self, contract, crypto: bool = False) -> float | None:
        """
        Fetch current price.
        - Crypto: uses CoinGecko public API (bypasses IBKR API version limit).
        - Stocks: uses IBKR historical data (no subscription needed).
        """
        if crypto:
            return get_crypto_price(contract.symbol)

        # Stocks — historical data via IBKR
        with self._price_lock:
            self._req_counter += 1
            req_id = self._req_counter
            self._price_event.clear()
            self._hist_last = None
            self.reqHistoricalData(
                reqId          = req_id,
                contract       = contract,
                endDateTime    = "",
                durationStr    = "1 D",
                barSizeSetting = "5 mins",
                whatToShow     = "TRADES",
                useRTH         = 1,
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

    # ── Connect (auto-retry client IDs like main.connect()) ──────────────────
    app = None
    for attempt in range(10):
        client_id = tws.CLIENT_ID + attempt
        _app = StrategyApp()
        logger.info(f"Connecting to TWS at {tws.TWS_HOST}:{tws.TWS_PORT} (clientId={client_id}) ...")
        _app.connect(tws.TWS_HOST, tws.TWS_PORT, client_id)
        threading.Thread(target=_app.run, daemon=True).start()
        if _app._ready.wait(timeout=5):
            app = _app
            logger.success(f"Connected to TWS (clientId={client_id}).")
            break
        _app.disconnect()
        logger.warning(f"clientId={client_id} in use, trying {client_id + 1} ...")

    if app is None:
        logger.error("Could not connect to TWS after 10 attempts.")
        sys.exit(1)

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
