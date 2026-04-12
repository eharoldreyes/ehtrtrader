"""
strategies/dca_scalp.py  -  Periodic DCA buy + profit-take sell strategy.

Schedule 1 (every buy_frequency seconds):
  - Buy `amount` USD worth of the symbol if budget allows.
  - Deduct cost from budget.

Schedule 2 (every sell_frequency seconds):
  - Query open lots from IBKR executions API (FIFO-matched buys vs sells).
  - Sell any lot whose current price is >= buy_price * (1 + sell_pct).

Invoked by:
  python main.py -sym BTC  -strat dca_scalp
  python main.py -sym AAPL -strat dca_scalp
"""

import sys
import math
import time
import json
import threading
import urllib.request
from zoneinfo import ZoneInfo

from ibapi.execution import ExecutionFilter
from loguru import logger
import main as tws

# ── CoinGecko fallback for crypto prices (bypasses IBKR API version limit) ───
# Binance symbol map (primary — no auth, 1200 req/min)
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
}

# CoinGecko symbol map (fallback)
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
}

PRICE_CACHE_TTL = 10   # seconds
_price_cache: dict[str, tuple[float, float]] = {}   # symbol -> (price, timestamp)


def _fetch_binance(symbol: str) -> float | None:
    pair = BINANCE_SYMBOLS.get(symbol)
    if not pair:
        return None
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return float(json.loads(resp.read())["price"])


def _fetch_coingecko(symbol: str) -> float | None:
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return float(json.loads(resp.read())[coin_id]["usd"])


def get_crypto_price(symbol: str) -> float | None:
    """
    Fetch crypto price. Uses Binance first (generous rate limits),
    falls back to CoinGecko. Results cached for PRICE_CACHE_TTL seconds.
    """
    symbol = symbol.upper()
    cached_price, cached_at = _price_cache.get(symbol, (None, 0))
    if cached_price and (time.time() - cached_at) < PRICE_CACHE_TTL:
        return cached_price

    for name, fetcher in [("Binance", _fetch_binance), ("CoinGecko", _fetch_coingecko)]:
        try:
            price = fetcher(symbol)
            if price:
                _price_cache[symbol] = (price, time.time())
                return price
        except Exception as e:
            logger.warning(f"{name} price fetch failed: {e} -- trying next source.")

    logger.error(f"All price sources failed for {symbol}. Using stale cache.")
    return cached_price


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
        "key":     "sell_pct",
        "label":   "Sell when price increases by",
        "default": 2.0,
        "type":    float,
        "unit":    "%",
        "hint":    "sell a lot when its price rises this % above its buy price",
    },
]

ET = ZoneInfo("America/New_York")
EXEC_REQ_ID = 50   # fixed reqId for all reqExecutions calls


# ── Extended app ──────────────────────────────────────────────────────────────
class StrategyApp(tws.IBKRApp):

    def __init__(self):
        super().__init__()
        # Price fetching
        self._price_lock  = threading.Lock()
        self._price_event = threading.Event()
        self._hist_last   = None
        self.last_price   = None
        self._req_counter = 300

        # Execution querying
        self._exec_lock    = threading.Lock()
        self._exec_event   = threading.Event()
        self._exec_results = []   # (contract, execution) from current reqExecutions call

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (162, 10285):
            logger.warning(f"Price data unavailable for reqId={reqId}: {errorString}")
            self._price_event.set()
        elif errorCode == 320 and reqId == EXEC_REQ_ID:
            # TWS can't parse fractional crypto execution sizes — unblock caller
            logger.debug(f"Execution query failed (code 320) — fractional size not supported by this TWS version.")
            self._exec_event.set()
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    # ── Historical data callbacks (stocks price) ──────────────────────────────
    def historicalData(self, reqId, bar):
        if bar.close and bar.close > 0:
            self._hist_last = bar.close

    def historicalDataEnd(self, reqId, start, end):
        if self._hist_last:
            self.last_price = self._hist_last
            self._hist_last = None
            self._price_event.set()

    # ── Execution callbacks ───────────────────────────────────────────────────
    def execDetails(self, reqId, contract, execution):
        if reqId == EXEC_REQ_ID:
            self._exec_results.append((contract, execution))

    def execDetailsEnd(self, reqId):
        if reqId == EXEC_REQ_ID:
            self._exec_event.set()

    # ── Price fetch ───────────────────────────────────────────────────────────
    def fetch_price(self, contract, crypto: bool = False) -> float | None:
        """
        Fetch current price.
        - Crypto: CoinGecko public API (bypasses IBKR API version limit).
        - Stocks: IBKR historical data (no subscription needed).
        """
        if crypto:
            return get_crypto_price(contract.symbol)

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

    # ── Open lots via IBKR executions API ────────────────────────────────────
    def get_open_lots(self, symbol: str) -> list:
        """
        Query IBKR for all executions of `symbol`, then FIFO-match
        buys against sells to return only the unmatched (open) buy lots.

        Returns: list of {shares, buy_price}
        """
        with self._exec_lock:
            self._exec_results.clear()
            self._exec_event.clear()

            f = ExecutionFilter()
            f.symbol = symbol.upper()
            self.reqExecutions(EXEC_REQ_ID, f)

            if not self._exec_event.wait(timeout=10):
                logger.warning("Timed out waiting for execution data.")
                return []

            executions = list(self._exec_results)

        # Sort buys and sells chronologically
        buys  = sorted(
            [(e.shares, e.price, e.time) for _, e in executions if e.side == "BOT"],
            key=lambda x: x[2],
        )
        sells = sorted(
            [(e.shares, e.price, e.time) for _, e in executions if e.side == "SLD"],
            key=lambda x: x[2],
        )

        # FIFO match: pair sell qty against the earliest unmatched buy
        sell_queue = list(sells)
        open_lots  = []

        for shares, price, timestamp in buys:
            remaining = shares
            while sell_queue and remaining > 1e-8:
                s_shares = sell_queue[0][0]
                matched  = min(remaining, s_shares)
                remaining -= matched
                if s_shares - matched > 1e-8:
                    sell_queue[0] = (s_shares - matched, sell_queue[0][1], sell_queue[0][2])
                else:
                    sell_queue.pop(0)

            if remaining > 1e-8:
                open_lots.append({"shares": remaining, "buy_price": price})

        return open_lots


# ── Main entry point ──────────────────────────────────────────────────────────
def run(symbol: str, params: dict = None):
    defaults = {p["key"]: p["default"] for p in PARAMS}
    p = {**defaults, **(params or {})}

    budget        = p["budget"]
    amount        = p["amount"]
    buy_frequency = int(p["buy_frequency"])
    sell_frequency = int(p["sell_frequency"])
    sell_target   = p["sell_pct"] / 100
    symbol        = symbol.upper()
    crypto        = tws.is_crypto(symbol)

    # Crypto: IBKR can't parse fractional execution sizes (TWS < v163),
    # so we track open lots locally. Stocks use the IBKR executions API.
    local_lots = [] if crypto else None   # [{shares, buy_price, cost}]

    lock       = threading.Lock()
    stop_event = threading.Event()

    # ── Connect (auto-retry client IDs) ──────────────────────────────────────
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
                if budget < amount:
                    stop_event.wait(buy_frequency)
                    continue

                order_id = app.next_order_id
                app.next_order_id += 1

                if crypto:
                    order  = tws.make_order("BUY", amount, use_cash_qty=True, crypto=True)
                    shares = amount / price
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
                budget -= cost

                if local_lots is not None:
                    local_lots.append({"shares": shares, "buy_price": price, "cost": cost})

                logger.info(
                    f"[BUY]  {shares:.6g} {symbol} @ ${price:.2f}  "
                    f"cost=${cost:.2f}  budget=${budget:.2f}"
                )

            stop_event.wait(buy_frequency)

    # ── Sell loop (Schedule 2) ────────────────────────────────────────────────
    def sell_loop():
        nonlocal budget

        while not stop_event.is_set():
            stop_event.wait(sell_frequency)

            price = app.fetch_price(contract, crypto=crypto)
            if not price:
                continue

            # Crypto: use local lots (IBKR can't parse fractional execution sizes)
            # Stocks: query IBKR executions API for source of truth
            with lock:
                open_lots = list(local_lots) if local_lots is not None else app.get_open_lots(symbol)

            if not open_lots:
                logger.debug(f"[CHECK] No open lots for {symbol}.")
                continue

            logger.info(
                f"[CHECK] {symbol} @ ${price:.2f}  "
                f"open lots: {len(open_lots)}  "
                f"target: +{sell_target*100:.2f}%"
            )
            for i, lot in enumerate(open_lots, 1):
                pct = (price - lot["buy_price"]) / lot["buy_price"]
                logger.info(
                    f"  lot {i}: {lot['shares']:.6g} @ ${lot['buy_price']:.2f}  "
                    f"now {pct*100:+.2f}%  "
                    f"{'-> SELL' if pct >= sell_target else '-> holding'}"
                )
                if pct >= sell_target:
                    with lock:
                        order_id = app.next_order_id
                        app.next_order_id += 1

                    order = tws.make_order("SELL", lot["shares"], crypto=crypto)
                    app.placeOrder(order_id, contract, order)

                    sell_value = lot["shares"] * price
                    profit     = sell_value - (lot["shares"] * lot["buy_price"])

                    with lock:
                        budget += sell_value
                        if local_lots is not None and lot in local_lots:
                            local_lots.remove(lot)

                    logger.info(
                        f"[SELL] {lot['shares']:.6g} {symbol} @ ${price:.2f}  "
                        f"bought @ ${lot['buy_price']:.2f}  "
                        f"profit=${profit:.2f} (+{pct*100:.2f}%)  "
                        f"budget=${budget:.2f}"
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
    logger.info(f"  Sell when up   : {sell_target * 100:.1f}%")
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
    open_lots  = local_lots if local_lots is not None else app.get_open_lots(symbol)
    last_price = app.fetch_price(contract, crypto=crypto)

    logger.info(sep)
    logger.info("  FINAL SUMMARY")
    logger.info(sep)
    logger.info(f"  Remaining budget  : ${budget:.2f}")
    logger.info(f"  Open positions    : {len(open_lots)}")
    for lot in open_lots:
        cur    = last_price or lot["buy_price"]
        unreal = (cur - lot["buy_price"]) * lot["shares"]
        logger.info(
            f"    {lot['shares']:.6g} {symbol} @ ${lot['buy_price']:.2f}  "
            f"unrealized=${unreal:.2f}"
        )
    logger.info(sep)

    app.disconnect()
    logger.info("Strategy session ended.")
