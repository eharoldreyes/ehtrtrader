"""
price_service.py  -  Singleton price cache with background polling.

All strategies and CLI commands share one instance so we never spam
Binance / CoinGecko / TWS with redundant requests regardless of how
many bot instances are running simultaneously.

Usage:
    import price_service

    svc = price_service.get()          # returns the singleton, starts polling
    svc.subscribe("BTC")               # add to watch-list (auto-saves to .env)
    price = svc.get_price("BTC")       # latest cached price (non-blocking)
    svc.update("AAPL", 195.40)         # strategies push stock prices in

Crypto:
    Background thread polls Binance for ALL subscribed crypto symbols
    in one batched request every POLL_INTERVAL seconds.
    Falls back to individual CoinGecko requests on Binance failure.

Stocks:
    No background polling (requires an IBKR connection).
    Strategies fetch via IBKR historical data and push the result here
    with svc.update(symbol, price). Subsequent callers within CACHE_TTL
    get the cached value without touching IBKR again.
"""

import re
import json
import time
import threading
import urllib.request
import urllib.parse
from pathlib import Path

from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL = 10.0   # seconds between crypto batch fetches
CACHE_TTL     = 10.0   # seconds before a stock price is considered stale

CRYPTO_SYMBOLS = {"BTC", "ETH", "LTC", "BCH"}

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

ENV_PATH = Path(__file__).parent / ".env"


# ── Singleton ─────────────────────────────────────────────────────────────────
class PriceService:
    """
    Thread-safe singleton price cache.
    - Crypto prices are refreshed in the background every POLL_INTERVAL seconds.
    - Stock prices are pushed in by strategies after each IBKR fetch.
    """

    _instance      = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._prices:  dict[str, float] = {}   # symbol -> latest price
                inst._updated: dict[str, float] = {}   # symbol -> epoch timestamp
                inst._symbols: set[str]         = set()
                inst._lock    = threading.Lock()
                inst._stop    = threading.Event()
                inst._thread  = None
                cls._instance = inst
        return cls._instance

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str) -> None:
        """
        Add a symbol to the watch-list.
        - If new, persists it to WATCHLIST in .env automatically.
        - Crypto symbols are picked up by the next background poll cycle.
        """
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._symbols:
                return
            self._symbols.add(symbol)

        logger.info(f"[PriceService] Subscribed to {symbol}.")
        _add_to_watchlist(symbol)

    def get_price(self, symbol: str) -> float | None:
        """Return the latest cached price. Non-blocking; returns None if not yet available."""
        with self._lock:
            return self._prices.get(symbol.upper())

    def is_stale(self, symbol: str) -> bool:
        """True if the cached price is missing or older than CACHE_TTL."""
        with self._lock:
            ts = self._updated.get(symbol.upper(), 0)
        return (time.time() - ts) > CACHE_TTL

    def update(self, symbol: str, price: float) -> None:
        """
        Push a freshly-fetched price into the cache.
        Called by strategy IBKR historical-data fetchers for stock prices.
        """
        symbol = symbol.upper()
        with self._lock:
            self._prices[symbol]  = price
            self._updated[symbol] = time.time()
        logger.debug(f"[PriceService] Updated {symbol} = ${price:,.2f}")

    def wait_for_price(self, symbol: str, timeout: float = 15.0) -> float | None:
        """
        Block until a price is available in the cache (up to timeout seconds).
        Useful on first startup before the first poll cycle completes.
        """
        symbol   = symbol.upper()
        deadline = time.time() + timeout
        while time.time() < deadline:
            price = self.get_price(symbol)
            if price:
                return price
            time.sleep(0.5)
        logger.warning(f"[PriceService] Timed out waiting for {symbol} price.")
        return None

    def start(self) -> None:
        """Start the background crypto polling thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="price-service"
        )
        self._thread.start()
        logger.debug("[PriceService] Background polling thread started.")

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        self._stop.set()

    # ── Background polling ────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                crypto = [s for s in self._symbols if s in CRYPTO_SYMBOLS]

            if crypto:
                self._refresh_crypto(crypto)

            self._stop.wait(POLL_INTERVAL)

    def _refresh_crypto(self, symbols: list[str]) -> None:
        """Fetch all crypto symbols in one batched Binance request."""
        pairs = [BINANCE_SYMBOLS[s] for s in symbols if s in BINANCE_SYMBOLS]
        if not pairs:
            return

        # ── Binance batch request ─────────────────────────────────────────────
        try:
            # Binance accepts ?symbols=["BTCUSDT","ETHUSDT"] for a batch fetch
            encoded = urllib.parse.quote(json.dumps(pairs))
            url = f"https://api.binance.com/api/v3/ticker/price?symbols={encoded}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())   # list of {symbol, price}
            reverse = {v: k for k, v in BINANCE_SYMBOLS.items()}
            for item in data:
                sym = reverse.get(item["symbol"])
                if sym:
                    self.update(sym, float(item["price"]))
            logger.debug(f"[PriceService] Binance batch updated: {', '.join(symbols)}")
            return
        except Exception as e:
            logger.warning(f"[PriceService] Binance batch fetch failed: {e} -- falling back to CoinGecko.")

        # ── CoinGecko fallback (individual requests) ──────────────────────────
        for sym in symbols:
            coin_id = COINGECKO_IDS.get(sym)
            if not coin_id:
                continue
            try:
                url = (
                    f"https://api.coingecko.com/api/v3/simple/price"
                    f"?ids={coin_id}&vs_currencies=usd"
                )
                with urllib.request.urlopen(url, timeout=5) as resp:
                    price = float(json.loads(resp.read())[coin_id]["usd"])
                self.update(sym, price)
            except Exception as e:
                logger.warning(f"[PriceService] CoinGecko failed for {sym}: {e}")


# ── Module-level accessor ─────────────────────────────────────────────────────
def get() -> PriceService:
    """Return the singleton PriceService, starting the polling thread if needed."""
    svc = PriceService()
    svc.start()
    return svc


# ── .env helper ───────────────────────────────────────────────────────────────
def _add_to_watchlist(symbol: str) -> None:
    """Append symbol to WATCHLIST in .env if not already present."""
    if not ENV_PATH.exists():
        return

    content = ENV_PATH.read_text(encoding="utf-8")
    match   = re.search(r"^WATCHLIST\s*=\s*(.*)$", content, re.MULTILINE)

    if match:
        current = [s.strip() for s in match.group(1).split(",") if s.strip()]
        if symbol in current:
            return
        current.append(symbol)
        new_line = f"WATCHLIST={','.join(current)}"
        content  = content[: match.start()] + new_line + content[match.end() :]
        logger.info(f"[PriceService] Added {symbol} to WATCHLIST in .env")
    else:
        content += f"\nWATCHLIST={symbol}\n"
        logger.info(f"[PriceService] Created WATCHLIST={symbol} in .env")

    ENV_PATH.write_text(content, encoding="utf-8")
