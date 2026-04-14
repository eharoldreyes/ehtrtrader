"""
price_service.py  -  Singleton price cache with background polling.

Works across multiple bot processes via a shared file cache
(.price_cache.json). Process 1 fetches from Binance and writes the
file; Process 2 reads the file and skips the API call if the data
is still fresh. This prevents rate-limiting when running several
instances simultaneously.

Within a single process, a thread-safe in-memory singleton is used
so strategies share one polling thread instead of each calling the
API independently.

Usage:
    import price_service

    svc = price_service.get()          # returns the singleton, starts polling
    svc.subscribe("BTC")               # add to watch-list (auto-saves to .env)
    price = svc.get_price("BTC")       # latest cached price (non-blocking)
    svc.update("AAPL", 195.40)         # strategies push stock prices in
"""

import re
import json
import time
import threading
import urllib.request
from pathlib import Path

from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL = 10.0   # seconds between crypto batch fetches
CACHE_TTL     = 10.0   # seconds before a price is considered stale

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

ENV_PATH        = Path(__file__).parent / ".env"
FILE_CACHE_PATH = Path(__file__).parent / ".price_cache.json"


# ── Shared file cache (cross-process) ─────────────────────────────────────────
def _file_cache_read(symbol: str) -> float | None:
    """
    Read a price from the shared file cache.
    Returns the price if it exists and is within CACHE_TTL, else None.
    """
    try:
        data  = json.loads(FILE_CACHE_PATH.read_text(encoding="utf-8"))
        entry = data.get(symbol.upper())
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return float(entry["price"])
    except Exception:
        pass
    return None


def _file_cache_write(symbol: str, price: float) -> None:
    """Write a price into the shared file cache (merge with existing entries)."""
    try:
        data = {}
        if FILE_CACHE_PATH.exists():
            try:
                data = json.loads(FILE_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[symbol.upper()] = {"price": price, "ts": time.time()}
        FILE_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[PriceService] File cache write failed: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
class PriceService:
    """
    Thread-safe singleton price cache.
    - Crypto: background thread polls Binance (batched) every POLL_INTERVAL.
              Before every API call, checks the shared file cache first so
              multiple processes don't duplicate requests.
    - Stocks: strategies push prices in via update(); other instances within
              CACHE_TTL read from cache without touching IBKR again.
    """

    _instance      = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._prices:  dict[str, float] = {}
                inst._updated: dict[str, float] = {}
                inst._symbols: set[str]         = set()
                inst._lock    = threading.Lock()
                inst._stop    = threading.Event()
                inst._thread  = None
                cls._instance = inst
        return cls._instance

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str) -> None:
        """Add a symbol to the watch-list. Persists to .env WATCHLIST if new."""
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._symbols:
                return
            self._symbols.add(symbol)

        logger.info(f"[PriceService] Subscribed to {symbol}.")
        _add_to_watchlist(symbol)

    def get_price(self, symbol: str) -> float | None:
        """Return the latest cached price (non-blocking). None if not yet available."""
        symbol = symbol.upper()
        with self._lock:
            price = self._prices.get(symbol)
        if price:
            return price
        # Also check the file cache (may have been written by another process)
        price = _file_cache_read(symbol)
        if price:
            self._set(symbol, price)
        return price

    def is_stale(self, symbol: str) -> bool:
        """True if the in-memory price is missing or older than CACHE_TTL."""
        with self._lock:
            ts = self._updated.get(symbol.upper(), 0)
        return (time.time() - ts) > CACHE_TTL

    def update(self, symbol: str, price: float) -> None:
        """
        Push a freshly-fetched price into both the in-memory and file cache.
        Called by strategies after each IBKR historical-data fetch.
        """
        symbol = symbol.upper()
        self._set(symbol, price)
        _file_cache_write(symbol, price)
        logger.debug(f"[PriceService] Updated {symbol} = ${price:,.2f}")

    def wait_for_price(self, symbol: str, timeout: float = 15.0) -> float | None:
        """Block until a price is available (up to timeout seconds)."""
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

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set(self, symbol: str, price: float) -> None:
        """Update in-memory cache only (no file write)."""
        with self._lock:
            self._prices[symbol]  = price
            self._updated[symbol] = time.time()

    # ── Background polling ────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                crypto = [s for s in self._symbols if s in CRYPTO_SYMBOLS]
            if crypto:
                self._refresh_crypto(crypto)
            self._stop.wait(POLL_INTERVAL)

    def _refresh_crypto(self, symbols: list[str]) -> None:
        """
        Refresh crypto prices. For each symbol, check the shared file cache
        first — if another process already fetched a fresh price, use it and
        skip the API call entirely.
        """
        stale = []
        for sym in symbols:
            cached = _file_cache_read(sym)
            if cached:
                self._set(sym, cached)   # sync in-memory from file
                logger.debug(f"[PriceService] {sym} served from file cache (${cached:,.2f})")
            else:
                stale.append(sym)

        if not stale:
            return

        # ── Binance batch request ─────────────────────────────────────────────
        pairs = [BINANCE_SYMBOLS[s] for s in stale if s in BINANCE_SYMBOLS]
        if pairs:
            try:
                # Pass the symbols array as a literal JSON string (no extra encoding)
                # Binance expects: ?symbols=["BTCUSDT","ETHUSDT"]
                symbols_param = json.dumps(pairs, separators=(",", ":"))
                url = f"https://api.binance.com/api/v3/ticker/price?symbols={symbols_param}"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read())
                reverse = {v: k for k, v in BINANCE_SYMBOLS.items()}
                for item in data:
                    sym = reverse.get(item["symbol"])
                    if sym:
                        price = float(item["price"])
                        self._set(sym, price)
                        _file_cache_write(sym, price)
                        stale = [s for s in stale if s != sym]
                logger.debug(f"[PriceService] Binance batch updated: {', '.join(symbols)}")
                return
            except Exception as e:
                logger.warning(f"[PriceService] Binance batch fetch failed: {e} -- falling back to CoinGecko.")

        # ── CoinGecko fallback (individual, only for symbols Binance missed) ──
        for sym in stale:
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
                self._set(sym, price)
                _file_cache_write(sym, price)
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
