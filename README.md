# ehtrtrader

An Interactive Brokers (IBKR) algorithmic trading bot with support for equities and crypto. Connects via TWS API and provides a CLI for manual trades, automated strategies, and trade history.

---

## Requirements

- Python 3.10+
- TWS (Trader Workstation) or IB Gateway running with API enabled

```bash
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root:

```env
TWS_HOST=127.0.0.1
TWS_PORT=7497
CLIENT_ID=1
WATCHLIST=AAPL,MSFT,NVDA,SPY,QQQ
```

| Variable | Default | Description |
|----------|---------|-------------|
| `TWS_HOST` | `127.0.0.1` | Host running TWS/IB Gateway |
| `TWS_PORT` | `7497` | TWS API port (`7497` = paper, `7496` = live) |
| `CLIENT_ID` | `1` | Starting client ID (auto-increments if in use) |
| `WATCHLIST` | (empty) | Comma-separated symbols for `watchlist` command |

---

## CLI Commands

### Manual Orders

```bash
python main.py buy  <SYMBOL> <QTY>    # buy by share count
python main.py buy  <SYMBOL> $<USD>   # buy by USD amount
python main.py sell <SYMBOL> <QTY>    # sell by share count
python main.py sell <SYMBOL> $<USD>   # sell by USD amount
```

**Examples:**
```bash
python main.py buy  AAPL 10           # buy 10 shares of AAPL
python main.py buy  BTC $200          # spend $200 on BTC  (PowerShell: '$200')
python main.py sell MSFT 5            # sell 5 shares of MSFT
python main.py sell BTC 0.00279782    # sell specific BTC amount
```

> **Note (PowerShell):** Wrap `$` amounts in single quotes to prevent shell expansion: `'$200'`

---

### Trade History

```bash
python main.py trades               # all executions today + P&L summary
python main.py trades <SYMBOL>      # filter by symbol
```

**Example output:**
```
-----------------------------------------------------------------------------
Symbol       Side   Qty        Price        Value        Time
-----------------------------------------------------------------------------
BTC          BOT    0.00139976 $71,440.75   $100.00      20260412  19:37:42
BTC          SLD    0.00139976 $71,440.50   $100.00      20260412  19:38:05
-----------------------------------------------------------------------------
  2 execution(s)

----------------------------------------------------------------------------------------------
Symbol       Bot Qty      Bot Value      Sld Qty      Sld Value      Net Qty      Realized P&L
----------------------------------------------------------------------------------------------
BTC          0.00139976   $100.00        0.00139976   $100.00        0.00000000   $0.00
----------------------------------------------------------------------------------------------
```

---

### Utilities

```bash
python main.py watchlist      # show configured watchlist
python main.py strategies     # list available strategies
```

---

### Run a Strategy

```bash
python main.py -sym <SYMBOL> -strat <STRATEGY>
```

An interactive parameter menu will appear. Press Enter to accept defaults.

**Examples:**
```bash
python main.py -sym NVDA -strat trailing_stop_loss
python main.py -sym SPY  -strat supply_demand
```

---

## Strategies

### `trailing_stop_loss`

Long-only strategy that enters a position, protects with a stop loss, trails the stop once price rises, and ladders in on dips.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `initial_shares` | 10 | Shares to buy at entry |
| `duration` | `5d` | How long to run (`5d`, `8h`, `2d4h`) |
| `stop_loss_pct` | 10% | Sell all if price drops this % from entry |
| `trail_trigger_pct` | 10% | Activate trailing stop once price gains this % |
| `trail_dist_pct` | 5% | Trail stop this % below the running peak |
| `ladder_1_drop_pct` | 20% | Buy more if price drops this % from entry |
| `ladder_1_qty` | 20 | Shares to buy on first dip |
| `ladder_2_drop_pct` | 30% | Buy more if price drops this % from entry |
| `ladder_2_qty` | 10 | Shares to buy on second dip |

**Rules:**
1. Buy `initial_shares` at market on entry
2. Sell all if price drops `stop_loss_pct`% from entry
3. Once price rises `trail_trigger_pct`%, trail stop `trail_dist_pct`% below peak
4. Ladder in at 20% and 30% dips (each trigger fires once)

**State persistence:** Saves to `.state_trailing_stop_<SYMBOL>.json` after each price check. Resumes automatically on next run if duration hasn't expired.

---

### `supply_demand`

Intraday strategy that detects supply/demand zones on 1-minute bars and trades zone revisits within a configurable session window.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `duration` | `5d` | Total runtime |
| `risk_per_trade` | $100 | Max USD risk per trade |
| `r_multiple` | 1.7 | Take profit = risk × R multiple |
| `max_position_size` | 100 | Max shares per trade |
| `session_start` | `09:30` | Session open (ET) |
| `session_end` | `11:30` | Session close + force-liquidate (ET) |
| `swing_window` | 2 | Bars left/right for swing detection |
| `atr_period` | 14 | ATR lookback period |
| `atr_multiplier` | 1.2 | Min impulse candle size (× ATR) |
| `stop_buffer_ticks` | 2 | Ticks beyond zone for stop |
| `zone_touch_ticks` | 2 | Zone touch tolerance (ticks) |
| `zone_max_age` | 60 | Invalidate zones older than N bars |
| `use_vwap_filter` | yes | Only trade in VWAP direction |
| `one_trade_per_day` | yes | Max one trade per session |

**Rules:**
1. Trade only during session window; force-close any open position at session end
2. Determine market structure (BULLISH / BEARISH / NEUTRAL) from swing highs/lows
3. Identify supply/demand zones from impulse candles (body ≥ ATR × multiplier)
4. Enter on zone touch + confirmation candle in direction of structure
5. Place OCA bracket order: market entry + limit TP + stop SL
6. Log every trade to `trades_sd.csv`

---

## Duration Format

Both strategies accept duration in flexible format:

| Input | Meaning |
|-------|---------|
| `5d` or `5` | 5 trading days (32.5 hours) |
| `8h` | 8 trading hours |
| `2d4h` | 2 days + 4 hours (17 hours) |

---

## Supported Assets

| Type | Symbols | Exchange | Market Hours |
|------|---------|----------|--------------|
| Equities | Any IBKR-listed stock | SMART | NYSE 9:30–16:00 ET |
| Crypto | BTC, ETH, LTC, BCH | PAXOS | 24/7 (no hours check) |

**Crypto order rules (IBKR):**
- Buy: uses `cashQty` (USD amount), `tif=IOC`
- Sell: uses `totalQuantity` (crypto units), `tif=IOC`

---

## TWS Connection

The bot auto-retries with incrementing client IDs if the configured one is already in use:

```
Connecting to TWS at 127.0.0.1:7497 (clientId=1) …
clientId=1 in use, trying 2 …
Connecting to TWS at 127.0.0.1:7497 (clientId=2) …
Connected to TWS (clientId=2).
```

Up to 10 attempts before exiting with an error.

---

## Project Structure

```
ehtrtrader/
├── main.py                               # Core CLI, TWS connection, order functions
├── requirements.txt                      # Python dependencies
├── strategies/
│   ├── __init__.py                       # Strategy registry
│   ├── trailing_stop_loss.py             # Trailing stop + ladder-in strategy
│   └── supply_demand.py                  # Supply & demand zone strategy
├── .env                                  # Local config (not tracked)
├── .state_trailing_stop_<SYMBOL>.json    # Persisted state for trailing stop
└── trades_sd.csv                         # Trade log for supply_demand
```
