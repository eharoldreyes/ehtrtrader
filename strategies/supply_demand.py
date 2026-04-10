"""
strategies/supply_demand.py  –  Supply & Demand zone strategy.

Core rules:
  1. Trade only during the configured NY session window (default 09:30–11:30 ET)
  2. Determine market structure from swing highs/lows (bullish / bearish / neutral)
  3. Identify S&D zones from impulse (displacement) candles on 1-minute bars
  4. Enter on zone revisit + confirmation candle in the direction of structure
  5. Use IBKR bracket orders: market entry + limit TP + stop SL
  6. Force-close any open position at session end
  7. Log every trade to trades_sd.csv

Invoked by:
    python main.py -sym SPY  -strat supply_demand
    python main.py -sym NVDA -strat supply_demand
"""

import sys
import csv
import time
import threading
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from loguru import logger
from ibapi.order import Order

import main as tws


# ── Configurable parameters (shown in the interactive menu) ──────────────────
PARAMS = [
    {
        "key":     "duration",
        "label":   "Duration",
        "default": "5d",
        "type":    str,
        "unit":    "",
        "hint":    "5d = 5 days · 8h = 8 hours · 2d4h = 2 days + 4 hours",
    },
    {
        "key":     "risk_per_trade",
        "label":   "USD risk per trade",
        "default": 100.0,
        "type":    float,
        "unit":    " USD",
    },
    {
        "key":     "r_multiple",
        "label":   "Take profit R multiple",
        "default": 1.7,
        "type":    float,
        "unit":    "R",
    },
    {
        "key":     "max_position_size",
        "label":   "Max position size",
        "default": 100,
        "type":    int,
        "unit":    " shares",
    },
    {
        "key":     "session_start",
        "label":   "Session start (HH:MM ET)",
        "default": "09:30",
        "type":    str,
        "unit":    "",
    },
    {
        "key":     "session_end",
        "label":   "Session end   (HH:MM ET)",
        "default": "11:30",
        "type":    str,
        "unit":    "",
    },
    {
        "key":     "swing_window",
        "label":   "Swing detection window",
        "default": 2,
        "type":    int,
        "unit":    " bars",
    },
    {
        "key":     "atr_period",
        "label":   "ATR period",
        "default": 14,
        "type":    int,
        "unit":    " bars",
    },
    {
        "key":     "atr_multiplier",
        "label":   "Impulse candle ATR multiplier",
        "default": 1.2,
        "type":    float,
        "unit":    "×",
    },
    {
        "key":     "stop_buffer_ticks",
        "label":   "Stop loss buffer",
        "default": 2,
        "type":    int,
        "unit":    " ticks",
    },
    {
        "key":     "zone_touch_ticks",
        "label":   "Zone touch tolerance",
        "default": 2,
        "type":    int,
        "unit":    " ticks",
    },
    {
        "key":     "zone_max_age",
        "label":   "Zone max age (invalidate after N bars)",
        "default": 60,
        "type":    int,
        "unit":    " bars",
    },
    {
        "key":     "use_vwap_filter",
        "label":   "Enable VWAP filter (yes / no)",
        "default": "yes",
        "type":    str,
        "unit":    "",
    },
    {
        "key":     "one_trade_per_day",
        "label":   "One trade per day only (yes / no)",
        "default": "yes",
        "type":    str,
        "unit":    "",
    },
]


# ── Internal constants ────────────────────────────────────────────────────────
ET            = ZoneInfo("America/New_York")
TICK_SIZE     = 0.01      # $0.01 per tick for US equities
POLL_INTERVAL = 30        # seconds between bar fetches
BARS_REQID    = 200
LOG_CSV       = Path(__file__).parent.parent / "trades_sd.csv"


# ── Session helpers ───────────────────────────────────────────────────────────
def _parse_time(s: str) -> dtime:
    """Parse 'HH:MM' into a time object."""
    h, m = map(int, s.strip().split(":"))
    return dtime(h, m)


def _session_active(start: dtime, end: dtime) -> bool:
    now = datetime.now(ET).time().replace(tzinfo=None)
    return start <= now < end


# ── Technical analysis ────────────────────────────────────────────────────────
def _atr(bars: list, period: int = 14) -> float | None:
    """Average True Range over the last `period` bars."""
    if len(bars) < period + 1:
        return None
    trs = [
        max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"]  - bars[i - 1]["close"]),
        )
        for i in range(1, len(bars))
    ]
    return sum(trs[-period:]) / period


def _detect_swings(bars: list, window: int = 2) -> list:
    """
    Fractal swing detection.
    A swing high: bar's high is the max within ±window bars.
    A swing low:  bar's low  is the min within ±window bars.
    """
    swings = []
    for i in range(window, len(bars) - window):
        slc = bars[i - window: i + window + 1]
        if bars[i]["high"] >= max(b["high"] for b in slc):
            swings.append({"type": "high", "index": i, "price": bars[i]["high"]})
        if bars[i]["low"] <= min(b["low"] for b in slc):
            swings.append({"type": "low",  "index": i, "price": bars[i]["low"]})
    return swings


def _structure(swings: list) -> str:
    """
    Determine market structure from the last two confirmed swing highs/lows.
    BULLISH = HH + HL | BEARISH = LH + LL | else NEUTRAL
    """
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"]  > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"]  < lows[-2]["price"]

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


def _build_zones(bars: list, atr: float, atr_mult: float,
                 min_width_pct: float = 0.0005,
                 max_age: int = 60, max_zones: int = 30) -> list:
    """
    Identify supply (resistance) and demand (support) zones.

    Demand zone: last bearish candle before a bullish impulse candle.
    Supply zone: last bullish candle before a bearish impulse candle.
    """
    zones = []
    n = len(bars)

    for i in range(1, n):
        bar  = bars[i]
        body = abs(bar["close"] - bar["open"])
        if body < atr * atr_mult:
            continue

        age = n - 1 - i
        if age > max_age:
            continue

        if bar["close"] > bar["open"]:          # bullish impulse → demand zone
            for j in range(i - 1, max(i - 10, -1), -1):
                b = bars[j]
                if b["close"] < b["open"]:      # last bearish candle
                    lo = min(b["open"], b["close"], b["low"])
                    hi = max(b["open"], b["close"])
                    if lo > 0 and (hi - lo) / lo >= min_width_pct:
                        zones.append({
                            "type": "demand",
                            "low":  round(lo, 4),
                            "high": round(hi, 4),
                            "age":  age,
                        })
                    break

        elif bar["close"] < bar["open"]:        # bearish impulse → supply zone
            for j in range(i - 1, max(i - 10, -1), -1):
                b = bars[j]
                if b["close"] > b["open"]:      # last bullish candle
                    lo = min(b["open"], b["close"])
                    hi = max(b["open"], b["close"], b["high"])
                    if lo > 0 and (hi - lo) / lo >= min_width_pct:
                        zones.append({
                            "type": "supply",
                            "low":  round(lo, 4),
                            "high": round(hi, 4),
                            "age":  age,
                        })
                    break

    # Remove near-duplicate zones (same type, within 5 ticks)
    deduped = []
    for z in zones[-max_zones:]:
        if not any(
            z2["type"] == z["type"]
            and abs(z2["low"]  - z["low"])  < TICK_SIZE * 5
            and abs(z2["high"] - z["high"]) < TICK_SIZE * 5
            for z2 in deduped
        ):
            deduped.append(z)
    return deduped


def _touches_zone(bar: dict, zone: dict, ticks: int) -> bool:
    """True if the bar's price range overlaps the zone (with tick tolerance)."""
    tol = TICK_SIZE * ticks
    return bar["low"] <= zone["high"] + tol and bar["high"] >= zone["low"] - tol


def _vwap(bars: list) -> float | None:
    """Session VWAP from a list of bars."""
    total_vol = sum(b["volume"] for b in bars)
    if not total_vol:
        return None
    return sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in bars) / total_vol


# ── CSV trade log ─────────────────────────────────────────────────────────────
_CSV_FIELDS = [
    "timestamp", "symbol", "action", "qty",
    "entry", "stop", "target", "risk_per_share",
    "structure", "zone_type", "order_id",
]


def _log_csv(record: dict):
    is_new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(record)
    logger.debug(f"Trade logged to {LOG_CSV.name}")


# ── Order summary ─────────────────────────────────────────────────────────────
def _print_summary(action, symbol, qty, entry, stop, target,
                   risk, structure, zone, order_id):
    actual_r = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0
    sep = "─" * 58
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    for line in [
        sep,
        f"  ORDER PLACED  –  {now}",
        sep,
        f"  Action     : {action}",
        f"  Symbol     : {symbol}",
        f"  Qty        : {qty} shares",
        f"  Entry      : ${entry:.2f}",
        f"  Stop loss  : ${stop:.2f}    (risk ${risk:.2f} / share)",
        f"  Take profit: ${target:.2f}  ({actual_r:.2f}R)",
        f"  Structure  : {structure}",
        f"  Zone       : {zone['type'].upper()}"
        f"  [{zone['low']:.2f} – {zone['high']:.2f}]  age={zone['age']} bars",
        f"  Order ID   : {order_id}",
        sep,
    ]:
        logger.info(line)


# ── Extended TWS app ──────────────────────────────────────────────────────────
class StrategyApp(tws.IBKRApp):
    """Extends IBKRApp with 1-min bar fetching and bracket order support."""

    def __init__(self):
        super().__init__()
        self._bars_buf  = {}           # reqId → list[bar dict]
        self._bar_event = threading.Event()

    # ── Historical data ───────────────────────────────────────────────────────
    def historicalData(self, reqId: int, bar):
        if bar.close > 0:
            self._bars_buf.setdefault(reqId, []).append({
                "date":   bar.date,
                "open":   bar.open,
                "high":   bar.high,
                "low":    bar.low,
                "close":  bar.close,
                "volume": bar.volume,
            })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        self._bar_event.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode == 162:
            logger.warning(f"No historical data (reqId={reqId}) — market may be closed.")
        else:
            super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    def fetch_bars(self, contract) -> list:
        """Fetch the last 5 trading days of 1-minute bars (no subscription needed)."""
        self._bars_buf[BARS_REQID] = []
        self._bar_event.clear()
        self.reqHistoricalData(
            reqId          = BARS_REQID,
            contract       = contract,
            endDateTime    = "",
            durationStr    = "5 D",
            barSizeSetting = "1 min",
            whatToShow     = "TRADES",
            useRTH         = 1,
            formatDate     = 1,
            keepUpToDate   = False,
            chartOptions   = [],
        )
        if not self._bar_event.wait(timeout=20):
            logger.warning("Bar fetch timed out.")
            return []
        return self._bars_buf.get(BARS_REQID, [])

    # ── Bracket order ─────────────────────────────────────────────────────────
    def place_bracket(self, contract, action: str, qty: int,
                      stop: float, target: float) -> tuple:
        """
        Place a bracket order: market entry + limit TP + stop SL.
        TP and SL share an OCA group so the surviving leg is auto-cancelled.
        Returns (parent_id, tp_id, sl_id).
        """
        close  = "SELL" if action == "BUY" else "BUY"
        oca    = f"OCA_{self.next_order_id}"

        # ── Parent: market entry ──────────────────────────────────────────────
        pid = self.next_order_id;  self.next_order_id += 1
        parent               = Order()
        parent.orderId       = pid
        parent.action        = action
        parent.totalQuantity = qty
        parent.orderType     = "MKT"
        parent.tif           = "DAY"
        parent.transmit      = False
        parent.eTradeOnly    = False
        parent.firmQuoteOnly = False

        # ── Child 1: take profit (limit) ──────────────────────────────────────
        tp_id = self.next_order_id;  self.next_order_id += 1
        tp               = Order()
        tp.orderId       = tp_id
        tp.parentId      = pid
        tp.action        = close
        tp.totalQuantity = qty
        tp.orderType     = "LMT"
        tp.lmtPrice      = round(target, 2)
        tp.tif           = "DAY"
        tp.ocaGroup      = oca
        tp.ocaType       = 1
        tp.transmit      = False
        tp.eTradeOnly    = False
        tp.firmQuoteOnly = False

        # ── Child 2: stop loss (stop order) ───────────────────────────────────
        sl_id = self.next_order_id;  self.next_order_id += 1
        sl               = Order()
        sl.orderId       = sl_id
        sl.parentId      = pid
        sl.action        = close
        sl.totalQuantity = qty
        sl.orderType     = "STP"
        sl.auxPrice      = round(stop, 2)
        sl.tif           = "DAY"
        sl.ocaGroup      = oca
        sl.ocaType       = 1
        sl.transmit      = True      # transmits all three at once
        sl.eTradeOnly    = False
        sl.firmQuoteOnly = False

        self.placeOrder(pid,   contract, parent)
        self.placeOrder(tp_id, contract, tp)
        self.placeOrder(sl_id, contract, sl)

        logger.info(
            f"Bracket placed — entry={pid}  TP={tp_id}  SL={sl_id}  "
            f"{action} {qty} × {contract.symbol}"
        )
        return pid, tp_id, sl_id

    def cancel_bracket(self, tp_id: int, sl_id: int):
        """Cancel TP and SL legs (e.g. before a force-liquidation)."""
        for oid in (tp_id, sl_id):
            try:
                self.cancelOrder(oid, "")
                logger.debug(f"Cancelled order {oid}")
            except Exception as e:
                logger.warning(f"Could not cancel order {oid}: {e}")

    def liquidate(self, contract, action: str, qty: int):
        """Place an urgent market order to close the position."""
        oid   = self.next_order_id;  self.next_order_id += 1
        order = tws.make_order(action, qty)
        self.placeOrder(oid, contract, order)
        logger.info(f"Liquidation: {action} {qty} × {contract.symbol}  (orderId={oid})")


# ── Main entry point ──────────────────────────────────────────────────────────
def run(symbol: str, params: dict = None):
    defaults = {p["key"]: p["default"] for p in PARAMS}
    p = {**defaults, **(params or {})}

    symbol               = symbol.upper()
    duration_total_hours = tws.parse_duration(str(p["duration"]))
    risk_per_trade       = float(p["risk_per_trade"])
    r_multiple           = float(p["r_multiple"])
    max_pos_size         = int(p["max_position_size"])
    session_start        = _parse_time(str(p["session_start"]))
    session_end          = _parse_time(str(p["session_end"]))
    swing_window         = int(p["swing_window"])
    atr_period           = int(p["atr_period"])
    atr_mult             = float(p["atr_multiplier"])
    stop_buf             = int(p["stop_buffer_ticks"])
    touch_ticks          = int(p["zone_touch_ticks"])
    zone_max_age         = int(p["zone_max_age"])
    use_vwap             = str(p["use_vwap_filter"]).lower().startswith("y")
    one_trade            = str(p["one_trade_per_day"]).lower().startswith("y")

    # ── In-memory session state (resets each run) ─────────────────────────────
    state = {
        "in_position":       False,
        "trade_today":       False,
        "bracket_ids":       None,   # (parent_id, tp_id, sl_id)
        "position_action":   None,
        "position_qty":      0,
        "accumulated_hours": 0.0,
    }

    # ── Connect ───────────────────────────────────────────────────────────────
    app = StrategyApp()
    logger.info(f"Connecting to TWS at {tws.TWS_HOST}:{tws.TWS_PORT} …")
    app.connect(tws.TWS_HOST, tws.TWS_PORT, tws.CLIENT_ID)
    threading.Thread(target=app.run, daemon=True).start()

    if not app._ready.wait(timeout=10):
        logger.error("Could not connect to TWS.")
        sys.exit(1)
    logger.success("Connected to TWS.")

    contract      = tws.make_contract(symbol)
    last_bar_date = None

    logger.info(
        f"Supply & Demand strategy active  |  {symbol}  |  "
        f"session {p['session_start']}–{p['session_end']} ET  |  "
        f"duration {tws.format_duration(duration_total_hours)}"
    )

    # ── Monitor loop ──────────────────────────────────────────────────────────
    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # ── Duration check ────────────────────────────────────────────────
            state["accumulated_hours"] += POLL_INTERVAL / 3600
            if state["accumulated_hours"] >= duration_total_hours:
                logger.info(
                    f"Duration reached "
                    f"({tws.format_duration(duration_total_hours)}). Exiting."
                )
                break

            # ── Session gate ──────────────────────────────────────────────────
            if not _session_active(session_start, session_end):
                if state["in_position"]:
                    logger.info("Session ended — force-closing position.")
                    _force_close(app, contract, state)
                else:
                    logger.debug("Outside session window. Waiting …")
                continue

            # ── Fetch bars ────────────────────────────────────────────────────
            bars = app.fetch_bars(contract)
            if len(bars) < atr_period + swing_window * 2 + 5:
                logger.warning(f"Not enough bars ({len(bars)}). Waiting …")
                continue

            latest_date = bars[-1]["date"]
            if latest_date == last_bar_date:
                logger.debug("No new bar yet.")
                continue
            last_bar_date = latest_date

            cur  = bars[-1]
            prev = bars[-2]
            logger.debug(
                f"Bar {latest_date}  "
                f"O={cur['open']:.2f}  H={cur['high']:.2f}  "
                f"L={cur['low']:.2f}  C={cur['close']:.2f}"
            )

            # ── Check if position was closed by TP or SL ──────────────────────
            if state["in_position"] and state["bracket_ids"]:
                tp_id = state["bracket_ids"][1]
                sl_id = state["bracket_ids"][2]
                tp_filled = app.order_statuses.get(tp_id, "") == "Filled"
                sl_filled = app.order_statuses.get(sl_id, "") == "Filled"
                if tp_filled or sl_filled:
                    outcome = "TP hit ✓" if tp_filled else "SL hit ✗"
                    logger.info(f"Position closed — {outcome}")
                    state["in_position"] = False
                    state["bracket_ids"] = None
                continue   # one trade at a time — wait for next bar

            # ── Skip if already traded today ──────────────────────────────────
            if one_trade and state["trade_today"]:
                logger.debug("Trade already taken today. Monitoring only.")
                continue

            # ── Analysis ──────────────────────────────────────────────────────
            atr = _atr(bars, atr_period)
            if not atr:
                continue

            swings    = _detect_swings(bars, swing_window)
            structure = _structure(swings)

            if structure == "NEUTRAL":
                logger.debug("Structure NEUTRAL — no trade.")
                continue

            zones = _build_zones(bars, atr, atr_mult, max_age=zone_max_age)
            vwap  = _vwap(bars) if use_vwap else None

            logger.debug(
                f"Structure={structure}  zones={len(zones)}  "
                f"ATR={atr:.3f}"
                + (f"  VWAP={vwap:.2f}" if vwap else "")
            )

            # ── Scan zones for a valid setup ──────────────────────────────────
            for zone in reversed(zones):   # most recent zones last in list = first here
                # Direction filter
                if structure == "BULLISH" and zone["type"] != "demand":
                    continue
                if structure == "BEARISH" and zone["type"] != "supply":
                    continue

                # VWAP filter
                if use_vwap and vwap:
                    if structure == "BULLISH" and cur["close"] < vwap:
                        continue
                    if structure == "BEARISH" and cur["close"] > vwap:
                        continue

                # Zone must have been touched by the previous bar
                if not _touches_zone(prev, zone, touch_ticks):
                    continue

                # Confirmation candle
                if structure == "BULLISH":
                    confirmed = (
                        cur["close"] > cur["open"]        # bullish bar
                        and cur["close"] > prev["high"]   # closes above prev high
                    )
                else:
                    confirmed = (
                        cur["close"] < cur["open"]        # bearish bar
                        and cur["close"] < prev["low"]    # closes below prev low
                    )

                if not confirmed:
                    continue

                # ── Size the trade ────────────────────────────────────────────
                entry = cur["close"]
                buf   = TICK_SIZE * stop_buf

                if structure == "BULLISH":
                    stop   = round(zone["low"]  - buf,                      2)
                    risk   = entry - stop
                    target = round(entry + risk * r_multiple,               2)
                    action = "BUY"
                else:
                    stop   = round(zone["high"] + buf,                      2)
                    risk   = stop - entry
                    target = round(entry - risk * r_multiple,               2)
                    action = "SELL"

                if risk <= 0:
                    logger.debug("Risk ≤ 0 — skipping.")
                    continue

                qty = min(int(risk_per_trade / risk), max_pos_size)
                if qty <= 0:
                    logger.debug(f"Position size {qty} too small — skipping.")
                    continue

                # ── Place bracket order ───────────────────────────────────────
                pid, tp_id, sl_id = app.place_bracket(
                    contract, action, qty, stop, target
                )

                state["in_position"]     = True
                state["trade_today"]     = True
                state["bracket_ids"]     = (pid, tp_id, sl_id)
                state["position_action"] = action
                state["position_qty"]    = qty

                _print_summary(
                    action=action, symbol=symbol, qty=qty,
                    entry=entry, stop=stop, target=target,
                    risk=round(risk, 4), structure=structure,
                    zone=zone, order_id=pid,
                )

                _log_csv({
                    "timestamp":       datetime.now(ET).isoformat(),
                    "symbol":          symbol,
                    "action":          action,
                    "qty":             qty,
                    "entry":           entry,
                    "stop":            stop,
                    "target":          target,
                    "risk_per_share":  round(risk, 4),
                    "structure":       structure,
                    "zone_type":       zone["type"],
                    "order_id":        pid,
                })

                break   # one trade at a time

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if state["in_position"]:
        logger.info("Cleaning up open position before exit …")
        _force_close(app, contract, state)

    app.disconnect()
    logger.info(f"Strategy ended. Trades saved to: {LOG_CSV}")


def _force_close(app: StrategyApp, contract, state: dict):
    """Cancel bracket children and send a market order to liquidate."""
    if state["bracket_ids"]:
        app.cancel_bracket(state["bracket_ids"][1], state["bracket_ids"][2])
        time.sleep(1)
    close_action = "SELL" if state["position_action"] == "BUY" else "BUY"
    app.liquidate(contract, close_action, state["position_qty"])
    state["in_position"] = False
    state["bracket_ids"] = None
