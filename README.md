# IBKR SMA Crossover Trading Bot

A local Python trading bot for Interactive Brokers **paper accounts**, using the official `ibapi` library and a Simple Moving Average (SMA) crossover strategy on US Stocks / ETFs.

---

## Project Structure

```
ibkr_bot/
├── main.py                  # Entry point – run this
├── requirements.txt
├── .gitignore
├── config/
│   ├── .env                 # Connection & strategy settings (never commit!)
│   └── watchlist.py         # Tickers to monitor
├── core/
│   ├── connection.py        # IBKRApp class (EClient + EWrapper)
│   ├── data.py              # Historical bar fetcher
│   └── executor.py          # Order placement + risk checks
├── strategies/
│   └── sma_crossover.py     # Signal logic
├── logs/                    # Auto-created, daily rotating log files
└── data/                    # Optional: save CSVs for backtesting
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Recommended |
| TWS or IB Gateway | Running locally on your machine |
| Paper account enabled | Settings → Paper Trading in TWS |
| `ibapi` installed | See below |

---

## Setup

### 1. Install TWS or IB Gateway
Download from [interactivebrokers.com](https://www.interactivebrokers.com/en/trading/tws.php).
Log in with your **paper account** credentials.

### 2. Enable API access in TWS
Go to: **Edit → Global Configuration → API → Settings**
- ✅ Enable ActiveX and Socket Clients
- Socket port: `7497` (paper TWS default)
- ✅ Allow connections from localhost only

### 3. Clone / copy this project
```bash
cd ~/projects
# paste or clone the ibkr_bot folder here
```

### 4. Create a virtual environment
```bash
cd ibkr_bot
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
```

### 5. Install dependencies
```bash
pip install -r requirements.txt
```

> **Note on `ibapi`:** The official package on PyPI may lag behind. If you hit issues, install directly from TWS:
> `pip install <TWS_install_dir>/IBJts/source/pythonclient/`

### 6. Configure your settings
Edit `config/.env`:
```
TWS_HOST=127.0.0.1
TWS_PORT=7497        # paper TWS
CLIENT_ID=1
FAST_MA=10
SLOW_MA=50
POSITION_SIZE=10
```

Edit `config/watchlist.py` to add/remove tickers.

---

## Running the Bot

Make sure TWS is open and logged into your **paper account**, then:

```bash
python main.py
```

The bot will:
1. Connect to TWS
2. Snapshot your open positions and account NAV
3. Fetch historical daily bars for each watchlist ticker
4. Compute SMA crossover signals
5. Place market orders for any BUY / SELL signals
6. Disconnect

Logs are written to `logs/bot_YYYY-MM-DD.log`.

---

## Strategy Logic

| Condition | Signal |
|---|---|
| Fast MA crosses **above** Slow MA | **BUY** (golden cross) |
| Fast MA crosses **below** Slow MA | **SELL** (death cross) |
| No crossover | HOLD |

Risk guards:
- Won't buy if already long
- Won't sell if no position
- Respects `MAX_POSITION_VALUE` and `POSITION_SIZE` from `.env`

---

## Scheduling (run daily after market open)

**macOS/Linux – cron:**
```
# Run at 9:45 AM ET Mon–Fri
45 9 * * 1-5 cd ~/projects/ibkr_bot && .venv/bin/python main.py
```

**Windows – Task Scheduler:**
Create a Basic Task → Daily → Action: run `.venv\Scripts\python.exe main.py`

---

## Adding a New Strategy

1. Create `strategies/my_strategy.py` with a `compute_signals(price_data, **params) -> dict[str, str]` function
2. Import and call it in `main.py` instead of (or alongside) `sma_crossover`

---

## ⚠️ Disclaimer

This bot is for **paper trading / educational use only**.  
Do not run on a live account without thorough testing and understanding of the risks involved.
