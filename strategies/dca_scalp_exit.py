"""
strategies/dca_scalp_exit.py  -  Exit all open DCA positions at a profit target.

Queries all open lots from the IBKR executions API (FIFO-matched buys vs sells),
computes the weighted average cost, then sells EVERYTHING in one order when the
current price is >= avg_cost * (1 + profit_pct / 100).

Runs indefinitely until all positions are fully closed, then prints a final
P&L summary and exits.

Invoked by:
  python main.py -sym BTC  -strat dca_scalp_exit
  python main.py -sym AAPL -strat dca_scalp_exit
"""

import sys
import time
import json
import threading
import urllib.request
from zoneinfo import ZoneInfo

from ibapi.execution import ExecutionFilter
from loguru import logger
import main as tws

# ── Binance / CoinGecko price sources (same as dca_scalp) ────────────────────
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
}

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
}

PRICE_CACHE_TTL = 10   # seconds
_price_cache: dict[str, tuple[float, float]] = {}


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
        "key":     "profit_pct",
        "label":   "Sell all when profit reaches",
        "default": 1.0,
        "type":    float,
        "unit":    "%",
        "hint":    "sell entire position when avg cost is this % in profit",
    },
    {
        "key":     "check_frequency",
        "label":   "Check every",
        "default": 10,
        "type":    int,
        "unit":    "s",
        "hint":    "seconds between each profit check",
    },
]

ET           = ZoneInfo("America/New_York")
EXEC_REQ_ID  = 51      # distinct from dca_scalp's 50 to avoid conflicts
MIN_LOT_SIZE = 0.0001  # filter IOC order remnants


# ── Extended app ──────────────────────────────────────────────────────────────
class StrategyApp(tws.IBKRApp):

    def __init__(self):
        super().__init__()
        # Price fetching (stocks via historical data)
        self._price_lock  = threading.Lock()
        self._price_event = threading.Event()
        self._hist_last   = None
        self.last_price   = None
        self._req_counter = 400

        # Execution querying
        self._exec_lock    = threading.Lock()
        self._exec_event   = threading.Event()
        self._exec_results = []

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (162, 10285):
            logger.warning(f"Price data unavailable for reqId={reqId}: {errorString}")
            self._price_event.set()
        elif errorCode == 320:
            # TWS can't parse tiny fractional IOC remnant lots -- skip and let
            # execDetailsEnd fire naturally with the real lots.
            logger.debug(f"Skipping unparseable execution record (code 320): {errorString}")
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    # ── Historical data callbacks (stocks) ───────────────────────────────────
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
        Query IBKR for all executions of `symbol`, FIFO-match buys vs sells,
        and return unmatched (open) buy lots above MIN_LOT_SIZE.
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

        buys  = sorted(
            [(e.shares, e.price, e.time) for _, e in executions if e.side == "BOT"],
            key=lambda x: x[2],
        )
        sells = sorted(
            [(e.shares, e.price, e.time) for _, e in executions if e.side == "SLD"],
            key=lambda x: x[2],
        )

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

        return [lot for lot in open_lots if lot["shares"] >= MIN_LOT_SIZE]


# ── Main entry point ──────────────────────────────────────────────────────────
def run(symbol: str, params: dict = None):
    defaults = {p["key"]: p["default"] for p in PARAMS}
    p = {**defaults, **(params or {})}

    profit_target   = p["profit_pct"] / 100
    check_frequency = int(p["check_frequency"])
    symbol          = symbol.upper()
    crypto          = tws.is_crypto(symbol)

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

    sep = "-" * 54
    logger.info(sep)
    logger.info(f"  DCA Scalp Exit  --  {symbol}")
    logger.info(sep)
    logger.info(f"  Sell target     : +{p['profit_pct']:.2f}%")
    logger.info(f"  Check every     : {check_frequency}s")
    logger.info(sep)

    total_realized = 0.0
    total_cost     = 0.0
    stop_event     = threading.Event()

    try:
        while not stop_event.is_set():
            open_lots = app.get_open_lots(symbol)

            if not open_lots:
                logger.success("All positions closed. Exiting.")
                break

            price = app.fetch_price(contract, crypto=crypto)
            if not price:
                logger.warning("Could not fetch price -- retrying.")
                stop_event.wait(check_frequency)
                continue

            # Compute weighted average cost across all open lots
            total_shares   = sum(lot["shares"] for lot in open_lots)
            total_lot_cost = sum(lot["shares"] * lot["buy_price"] for lot in open_lots)
            avg_cost       = total_lot_cost / total_shares
            current_value  = total_shares * price
            pct_gain       = (price - avg_cost) / avg_cost

            logger.info(
                f"[CHECK] {symbol} @ ${price:,.2f}  "
                f"open lots: {len(open_lots)}  "
                f"total: {total_shares:.6g} shares  "
                f"avg cost: ${avg_cost:,.2f}  "
                f"P&L: {pct_gain*100:+.2f}%  "
                f"target: +{p['profit_pct']:.2f}%"
            )

            if pct_gain >= profit_target:
                logger.info(
                    f"[EXIT]  Target reached! Selling all {total_shares:.6g} {symbol} "
                    f"@ ${price:,.2f}"
                )
                order_id = app.next_order_id
                app.next_order_id += 1
                order = tws.make_order("SELL", total_shares, crypto=crypto)
                app.placeOrder(order_id, contract, order)

                profit          = current_value - total_lot_cost
                total_realized += profit
                total_cost     += total_lot_cost

                logger.info(
                    f"[EXIT]  Sell order placed  "
                    f"cost basis: ${total_lot_cost:,.2f}  "
                    f"value: ${current_value:,.2f}  "
                    f"profit: ${profit:,.2f} ({pct_gain*100:+.2f}%)"
                )

                # Wait a moment for the order to process, then re-check
                # (IOC fills may be partial -- loop continues until all closed)
                stop_event.wait(check_frequency)
            else:
                stop_event.wait(check_frequency)

    except KeyboardInterrupt:
        logger.info("Interrupted -- shutting down.")

    # ── Final summary ─────────────────────────────────────────────────────────
    remaining_lots = app.get_open_lots(symbol)
    last_price     = app.fetch_price(contract, crypto=crypto) or 0.0

    logger.info(sep)
    logger.info("  FINAL SUMMARY")
    logger.info(sep)
    logger.info(f"  Symbol          : {symbol}")
    logger.info(f"  Realized profit : ${total_realized:,.2f}")

    if remaining_lots:
        rem_shares = sum(lot["shares"] for lot in remaining_lots)
        rem_cost   = sum(lot["shares"] * lot["buy_price"] for lot in remaining_lots)
        rem_value  = rem_shares * last_price
        unrealized = rem_value - rem_cost
        logger.info(f"  Open positions  : {len(remaining_lots)} lots  ({rem_shares:.6g} shares)")
        logger.info(f"  Unrealized P&L  : ${unrealized:,.2f}  (@ ${last_price:,.2f})")
    else:
        logger.info(f"  Open positions  : none -- fully exited")

    logger.info(sep)

    app.disconnect()
    logger.info("Exit strategy session ended.")
