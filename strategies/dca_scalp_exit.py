"""
strategies/dca_scalp_exit.py  -  Exit all open DCA positions lot by lot.

Queries open lots from the IBKR executions API (FIFO-matched buys vs sells)
and checks each lot individually every `check_frequency` seconds.
Sells a lot as soon as its current price is >= its buy price * (1 + profit_pct).

Runs indefinitely until every open lot has been sold, then prints a final
realized P&L summary and exits cleanly.

Invoked by:
  python main.py -sym BTC  -strat dca_scalp_exit
  python main.py -sym AAPL -strat dca_scalp_exit
"""

import sys
import time
import threading
from zoneinfo import ZoneInfo

from ibapi.execution import ExecutionFilter
from loguru import logger
import main as tws
import price_service


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
        """
        Fetch current price via the shared PriceService cache.
        - Crypto : returned instantly from the background-polled cache.
        - Stocks : served from cache if fresh; otherwise fetched from IBKR
                   and pushed back into the cache for other instances to reuse.
        """
        svc    = price_service.get()
        symbol = contract.symbol.upper()

        if crypto:
            return svc.wait_for_price(symbol, timeout=15)

        if not svc.is_stale(symbol):
            return svc.get_price(symbol)

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
            if self.last_price:
                svc.update(symbol, self.last_price)
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

    # Subscribe to price service (auto-adds to .env WATCHLIST if new)
    price_service.get().subscribe(symbol)

    contract = tws.make_contract(symbol)

    sep = "-" * 54
    logger.info(sep)
    logger.info(f"  DCA Scalp Exit  --  {symbol}")
    logger.info(sep)
    logger.info(f"  Sell target     : +{p['profit_pct']:.2f}% per lot")
    logger.info(f"  Check every     : {check_frequency}s")
    logger.info(sep)

    total_realized = 0.0
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

            logger.info(
                f"[CHECK] {symbol} @ ${price:,.2f}  "
                f"open lots: {len(open_lots)}  "
                f"target: +{p['profit_pct']:.2f}%"
            )

            # Check each lot individually — sell as soon as it hits the target
            for i, lot in enumerate(open_lots, 1):
                pct = (price - lot["buy_price"]) / lot["buy_price"]
                status = "-> SELL" if pct >= profit_target else "-> holding"
                logger.info(
                    f"  lot {i}: {lot['shares']:.6g} @ ${lot['buy_price']:,.2f}  "
                    f"now {pct*100:+.2f}%  {status}"
                )

                if pct >= profit_target:
                    order_id = app.next_order_id
                    app.next_order_id += 1
                    order = tws.make_order("SELL", lot["shares"], crypto=crypto)
                    app.placeOrder(order_id, contract, order)

                    sell_value      = lot["shares"] * price
                    cost_basis      = lot["shares"] * lot["buy_price"]
                    profit          = sell_value - cost_basis
                    total_realized += profit

                    logger.info(
                        f"[SELL] {lot['shares']:.6g} {symbol} @ ${price:,.2f}  "
                        f"bought @ ${lot['buy_price']:,.2f}  "
                        f"profit=${profit:,.2f} ({pct*100:+.2f}%)  "
                        f"total realized=${total_realized:,.2f}"
                    )

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
