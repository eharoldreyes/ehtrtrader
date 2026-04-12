"""
strategies/trailing_stop_loss.py  –  Trailing stop + ladder-in strategy.

Rules:
  1. Entry      : Buy <shares> of <symbol> at market on first run
  2. Stop loss  : Sell ALL if price drops 10% from entry (floor never moves down)
  3. Trailing   : Once price climbs +10%, trail stop 5% below the running peak
  4. Ladder -20%: Buy 20 more shares if price falls 20% from entry
  5. Ladder -30%: Buy 10 more shares if price falls 30% from entry
  6. Duration   : Respects -dur (trading days). State is saved so cron can resume.

Invoked by:
  python main.py -sym NVDA -pos 10 -strat trailing_stop_loss -dur 5
"""

import sys
import json
import time
import threading
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays
from loguru import logger

import main as tws

# ── Configurable parameters (shown in the interactive menu) ──────────────────
PARAMS = [
    {
        "key":     "initial_shares",
        "label":   "Initial shares to buy",
        "default": 10,
        "type":    int,
        "unit":    " shares",
    },
    {
        "key":     "duration",
        "label":   "Duration",
        "default": "5d",
        "type":    str,
        "unit":    "",
        "hint":    "5d = 5 days · 8h = 8 hours · 2d4h = 2 days + 4 hours",
    },
    {
        "key":     "stop_loss_pct",
        "label":   "Stop loss — sell all if price drops by",
        "default": 10.0,
        "type":    float,
        "unit":    "%",
    },
    {
        "key":     "trail_trigger_pct",
        "label":   "Trailing stop activates when price rises by",
        "default": 10.0,
        "type":    float,
        "unit":    "%",
    },
    {
        "key":     "trail_dist_pct",
        "label":   "Trailing stop distance below running peak",
        "default": 5.0,
        "type":    float,
        "unit":    "%",
    },
    {
        "key":     "ladder_1_drop_pct",
        "label":   "1st ladder — trigger on price drop of",
        "default": 20.0,
        "type":    float,
        "unit":    "%",
    },
    {
        "key":     "ladder_1_qty",
        "label":   "1st ladder — shares to buy",
        "default": 20,
        "type":    int,
        "unit":    "shares",
    },
    {
        "key":     "ladder_2_drop_pct",
        "label":   "2nd ladder — trigger on price drop of",
        "default": 30.0,
        "type":    float,
        "unit":    "%",
    },
    {
        "key":     "ladder_2_qty",
        "label":   "2nd ladder — shares to buy",
        "default": 10,
        "type":    int,
        "unit":    "shares",
    },
]

POLL_INTERVAL  = 10     # seconds between price checks
MKT_DATA_REQID = 100

ET        = ZoneInfo("America/New_York")
NYSE_HOLS = holidays.NYSE()

STATE_DIR = Path(__file__).parent.parent   # project root


# ── State persistence ─────────────────────────────────────────────────────────
def _state_file(symbol: str) -> Path:
    return STATE_DIR / f".state_trailing_stop_{symbol.upper()}.json"


def _load_state(symbol: str) -> dict | None:
    path = _state_file(symbol)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_state(symbol: str, state: dict):
    with open(_state_file(symbol), "w") as f:
        json.dump(state, f, indent=2)


def _clear_state(symbol: str):
    path = _state_file(symbol)
    if path.exists():
        path.unlink()


# ── Trading day counter ───────────────────────────────────────────────────────
def _count_trading_days(start: date, end: date) -> int:
    """Count NYSE trading days between start and end (inclusive)."""
    count, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d not in NYSE_HOLS:
            count += 1
        d += timedelta(days=1)
    return count


# ── Extended app (uses historical data for price — no subscription needed) ────
class StrategyApp(tws.IBKRApp):

    def __init__(self):
        super().__init__()
        self.last_price    = None
        self._price_event  = threading.Event()
        self._hist_last    = None   # most recent close bar seen in current request

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode == 162:
            logger.warning(f"No historical data returned for reqId={reqId} — market may be closed or symbol invalid.")
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    def historicalData(self, reqId: int, bar):
        """Capture the close of each bar — last one received = current price."""
        if bar.close and bar.close > 0:
            self._hist_last = bar.close

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        """All bars delivered — commit the last close as the current price."""
        if self._hist_last:
            self.last_price = self._hist_last
            self._hist_last = None
            self._price_event.set()

    def fetch_price(self, contract, req_id: int = MKT_DATA_REQID):
        """
        Fetch the most recent traded price using historical data.
        No market data subscription required.
        Uses a 1-day window so data is always available even right at open.
        """
        self._price_event.clear()
        self.reqHistoricalData(
            reqId          = req_id,
            contract       = contract,
            endDateTime    = "",      # empty = now
            durationStr    = "1 D",   # last full trading day — always has data
            barSizeSetting = "5 mins",
            whatToShow     = "TRADES",
            useRTH         = 1,       # regular trading hours only (cleaner data)
            formatDate     = 1,
            keepUpToDate   = False,
            chartOptions   = [],
        )
        if not self._price_event.wait(timeout=20):
            logger.error(
                "No price data received. Ensure TWS is connected and "
                "the symbol is valid."
            )
            sys.exit(1)
        return self.last_price


# ── Order summary ─────────────────────────────────────────────────────────────
def _print_summary(action: str, symbol: str, shares: int, price: float,
                   stop: float | None, reason: str, order_id: int,
                   entry: float | None = None):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    sep = "─" * 54

    change_str = ""
    if entry and price != entry:
        pct = (price - entry) / entry * 100
        change_str = f"  ({pct:+.2f}% vs entry)"

    stop_str = "—"
    if stop:
        stop_pct = (stop - price) / price * 100
        stop_str = f"${stop:.2f}  ({stop_pct:+.1f}% from current)"

    for line in [
        sep,
        f"  ORDER PLACED  –  {now}",
        sep,
        f"  Action   : {action}",
        f"  Symbol   : {symbol}",
        f"  Shares   : {shares}",
        f"  Price    : ${price:.2f}{change_str}",
        f"  Stop     : {stop_str}",
        f"  Reason   : {reason}",
        f"  Order ID : {order_id}",
        sep,
    ]:
        logger.info(line)


# ── Main entry point ──────────────────────────────────────────────────────────
def run(symbol: str, params: dict = None):
    # Merge user params over defaults
    defaults = {p["key"]: p["default"] for p in PARAMS}
    p = {**defaults, **(params or {})}

    # Parse values from params
    shares               = int(p["initial_shares"])
    duration_total_hours = tws.parse_duration(str(p["duration"]))
    stop_loss_pct        = p["stop_loss_pct"]     / 100
    trail_trigger        = p["trail_trigger_pct"] / 100
    trail_dist           = p["trail_dist_pct"]    / 100
    ladder = [
        (-(p["ladder_1_drop_pct"] / 100), int(p["ladder_1_qty"])),
        (-(p["ladder_2_drop_pct"] / 100), int(p["ladder_2_qty"])),
    ]

    symbol = symbol.upper()

    # ── Duration check (resume or fresh start) ────────────────────────────────
    state = _load_state(symbol)

    if state:
        accum_h = state.get("accumulated_trading_hours", 0.0)
        dur_h   = state.get("duration_total_hours", duration_total_hours)
        logger.info(
            f"Resuming {symbol}  |  "
            f"{accum_h:.1f} / {tws.format_duration(dur_h)} elapsed"
        )
        if accum_h >= dur_h:
            logger.info(
                f"Duration of {tws.format_duration(dur_h)} reached. "
                "Strategy complete."
            )
            _clear_state(symbol)
            return

        # Re-sync ladder_done keys to current params.
        # If params changed between runs, add any new keys as False
        # and drop keys that no longer exist.
        current_keys = {str(pct) for pct, _ in ladder}
        state["ladder_done"] = {
            k: state["ladder_done"].get(k, False)
            for k in current_keys
        }
        logger.debug(f"Ladder keys synced to current params: {list(current_keys)}")
    else:
        state = {
            "symbol":                   symbol,
            "strategy":                 "trailing_stop_loss",
            "start_date":               date.today().isoformat(),
            "duration_total_hours":     duration_total_hours,
            "accumulated_trading_hours": 0.0,
            "entry_price":              None,
            "stop_price":               None,
            "peak_price":               None,
            "trailing_active":          False,
            "total_shares":             0,
            "initial_shares":           shares,
            "ladder_done":              {str(pct): False for pct, _ in ladder},
            "trade_log":                [],
        }
        logger.info(
            f"Starting fresh  |  {symbol} × {shares} shares  |  "
            f"duration: {tws.format_duration(duration_total_hours)}"
        )

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

    logger.info(f"Fetching {symbol} price via historical data …")
    current_price = app.fetch_price(contract)
    logger.info(f"{symbol} current price: ${current_price:.2f}")

    # ── Initial buy (only on fresh start) ────────────────────────────────────
    if state["entry_price"] is None:
        order_id = app.next_order_id
        app.next_order_id += 1
        app.placeOrder(order_id, contract, tws.make_order("BUY", shares))

        state["entry_price"]  = current_price
        state["stop_price"]   = round(current_price * (1 - stop_loss_pct), 4)
        state["peak_price"]   = current_price
        state["total_shares"] = shares
        state["trade_log"].append({
            "action": "BUY", "shares": shares,
            "price": current_price, "order_id": order_id,
            "reason": "Initial entry",
        })
        _save_state(symbol, state)

        _print_summary(
            action="BUY  (initial entry)", symbol=symbol, shares=shares,
            price=current_price, stop=state["stop_price"],
            reason=f"Strategy start — stop loss set at -{stop_loss_pct*100:.0f}%",
            order_id=order_id,
        )
        time.sleep(2)
    else:
        logger.info(
            f"Resumed position — entry=${state['entry_price']:.2f}  "
            f"stop=${state['stop_price']:.2f}  "
            f"shares={state['total_shares']}"
        )

    # Restore mutable strategy state onto app for the monitor loop
    app.next_order_id = app.next_order_id  # already set by nextValidId

    # ── Monitor loop ──────────────────────────────────────────────────────────
    logger.info(f"Monitoring {symbol} every {POLL_INTERVAL}s  |  Ctrl+C to stop\n")

    exited = False
    try:
        while True:
            # Stop at market close
            now = datetime.now(ET)
            if now.time() >= tws.MARKET_CLOSE:
                logger.info("Market closed for the day. Saving state and exiting.")
                _save_state(symbol, state)
                break

            time.sleep(POLL_INTERVAL)

            # Accumulate trading time (only counts while strategy is running)
            state["accumulated_trading_hours"] = state.get("accumulated_trading_hours", 0.0) + POLL_INTERVAL / 3600
            if state["accumulated_trading_hours"] >= state["duration_total_hours"]:
                logger.info(
                    f"Duration reached "
                    f"({tws.format_duration(state['duration_total_hours'])}). "
                    "Strategy complete."
                )
                _save_state(symbol, state)
                break

            price = app.fetch_price(contract)
            if not price:
                continue

            entry  = state["entry_price"]
            change = (price - entry) / entry

            logger.info(
                f"{symbol}  ${price:.2f}  change={change:+.1%}  "
                f"stop=${state['stop_price']:.2f}  "
                f"peak=${state['peak_price']:.2f}  "
                f"shares={state['total_shares']}"
            )

            # ── Rule 1: Stop loss ─────────────────────────────────────────
            if price <= state["stop_price"] and state["total_shares"] > 0:
                order_id = app.next_order_id
                app.next_order_id += 1
                app.placeOrder(order_id, contract,
                               tws.make_order("SELL", state["total_shares"]))

                _print_summary(
                    action=f"SELL  (stop loss)", symbol=symbol,
                    shares=state["total_shares"], price=price, stop=None,
                    reason=f"Price ${price:.2f} hit stop ${state['stop_price']:.2f}",
                    order_id=order_id, entry=entry,
                )
                state["trade_log"].append({
                    "action": "SELL", "shares": state["total_shares"],
                    "price": price, "order_id": order_id, "reason": "Stop loss",
                })
                state["total_shares"] = 0
                _clear_state(symbol)
                exited = True
                break

            # ── Rule 2: Update trailing stop (floor only moves up) ────────
            if price > state["peak_price"]:
                state["peak_price"] = price

            if not state["trailing_active"] and change >= trail_trigger:
                state["trailing_active"] = True
                logger.info(f"  ✦ Trailing stop activated (price up {change:+.1%})")

            if state["trailing_active"]:
                new_stop = round(state["peak_price"] * (1 - trail_dist), 4)
                if new_stop > state["stop_price"]:
                    logger.info(
                        f"  ↑ Stop raised: ${state['stop_price']:.2f} → ${new_stop:.2f}"
                    )
                    state["stop_price"] = new_stop

            # ── Rule 3: Ladder in on dips ─────────────────────────────────
            for drop_pct, add_shares in ladder:
                key = str(drop_pct)
                if not state["ladder_done"].get(key, False) and change <= drop_pct:
                    order_id = app.next_order_id
                    app.next_order_id += 1
                    app.placeOrder(order_id, contract,
                                   tws.make_order("BUY", add_shares))
                    state["total_shares"]    += add_shares
                    state["ladder_done"][key] = True

                    _print_summary(
                        action=f"BUY  (ladder {abs(drop_pct)*100:.0f}% dip)",
                        symbol=symbol, shares=add_shares, price=price,
                        stop=state["stop_price"],
                        reason=f"Price dropped {change:.1%} from entry ${entry:.2f}",
                        order_id=order_id, entry=entry,
                    )
                    state["trade_log"].append({
                        "action": "BUY", "shares": add_shares, "price": price,
                        "order_id": order_id,
                        "reason": f"Ladder {abs(drop_pct)*100:.0f}% dip",
                    })

            _save_state(symbol, state)   # persist after every check

    except KeyboardInterrupt:
        logger.info("Interrupted — saving state.")
        _save_state(symbol, state)

    # ── Final trade log ───────────────────────────────────────────────────────
    if state["trade_log"]:
        sep = "═" * 54
        logger.info(sep)
        logger.info("  TRADE LOG SUMMARY")
        logger.info(sep)
        logger.info(f"  {'#':<4} {'Action':<28} {'Shs':>4}  {'Price':>8}  Reason")
        logger.info(f"  {'─'*4} {'─'*28} {'─'*4}  {'─'*8}  {'─'*20}")
        for i, t in enumerate(state["trade_log"], 1):
            logger.info(
                f"  {i:<4} {t['action']:<28} {t['shares']:>4}  "
                f"${t['price']:>7.2f}  {t['reason']}"
            )
        logger.info(sep)

    app.disconnect()
    logger.info("Strategy session ended.")
