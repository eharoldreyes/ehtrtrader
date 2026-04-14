"""
price_service.py  -  Singleton price cache with real-time WebSocket streaming.

Price source priority:
  1. Binance WebSocket (primary)  — real-time push on every trade, no rate limit
  2. Binance REST (fallback)      — polled every POLL_INTERVAL if WS is down
  3. Coinbase REST (fallback)     — used if Binance REST also fails

Cross-process sharing:
  All bot instances share a .price_cache.json file so multiple processes
  never duplicate API calls. Process 1 fetches and writes; Process 2 reads
  the file and skips the request if the data is still fresh.

Usage:
    import price_service

    svc = price_service.get()      # singleton, starts WS + fallback threads
    svc.subscribe("BTC")           # watch a symbol (auto-saves to .env)
    price = svc.get_price("BTC")   # latest cached price (non-blocking)
    svc.update("AAPL", 195.40)     # strategies push stock prices in
"""

import re
import json
import time
import threading
import urllib.request
from pathlib import Path

import websocket
from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 10.0   # seconds between REST fallback polls
CACHE_TTL      = 10.0   # seconds before a price is considered stale
WS_RECONNECT   = 5.0    # seconds to wait before reconnecting WebSocket

CRYPTO_SYMBOLS = {"BTC", "ETH", "LTC", "BCH"}

BINANCE_WS_BASE  = "wss://stream.binance.com:9443/stream"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price"

BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
}
# Reverse map: "BTCUSDT" -> "BTC"
BINANCE_REVERSE = {v: k for k, v in BINANCE_SYMBOLS.items()}

COINBASE_SYMBOLS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "LTC": "LTC-USD",
    "BCH": "BCH-USD",
}

ENV_PATH        = Path(__file__).parent / ".env"
FILE_CACHE_PATH = Path(__file__).parent / ".price_cache.json"


# ── Shared file cache (cross-process) ─────────────────────────────────────────
def _file_cache_read(symbol: str) -> float | None:
    """Return a cached price if it exists and is within CACHE_TTL, else None."""
    try:
        data  = json.loads(FILE_CACHE_PATH.read_text(encoding="utf-8"))
        entry = data.get(symbol.upper())
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return float(entry["price"])
    except Exception:
        pass
    return None


def _file_cache_write(symbol: str, price: float) -> None:
    """Merge a price into the shared file cache."""
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

    Two background threads run concurrently:
      _ws_thread       — Binance WebSocket listener. Reconnects automatically.
      _fallback_thread — Binance REST + Coinbase REST. Only fetches when the
                         WebSocket has been down for a full POLL_INTERVAL.
    """

    _instance      = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._prices:       dict[str, float] = {}
                inst._updated:      dict[str, float] = {}
                inst._symbols:      set[str]          = set()
                inst._lock          = threading.Lock()
                inst._stop          = threading.Event()
                inst._ws_thread     = None
                inst._fallback_thread = None
                inst._ws_connected  = False
                inst._ws            = None   # active WebSocketApp
                cls._instance = inst
        return cls._instance

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str) -> None:
        """
        Add a symbol to the watch-list.
        Persists to .env WATCHLIST if new, and triggers a WS reconnect so
        the new symbol is immediately included in the stream.
        """
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._symbols:
                return
            self._symbols.add(symbol)

        logger.info(f"[PriceService] Subscribed to {symbol}.")
        _add_to_watchlist(symbol)

        # Reconnect WebSocket so the new symbol's stream is included
        if self._ws:
            self._ws.close()

    def get_price(self, symbol: str) -> float | None:
        """Return the latest cached price (non-blocking). None if unavailable."""
        symbol = symbol.upper()
        with self._lock:
            price = self._prices.get(symbol)
        if price:
            return price
        # Check file cache — another process may have written a fresh price
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
        """Push a price into both caches. Used by strategies for stock prices."""
        symbol = symbol.upper()
        self._set(symbol, price)
        _file_cache_write(symbol, price)
        logger.debug(f"[PriceService] Updated {symbol} = ${price:,.2f}")

    def wait_for_price(self, symbol: str, timeout: float = 15.0) -> float | None:
        """Block until a price is available, up to timeout seconds."""
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
        """Start the WebSocket and fallback threads (idempotent)."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="price-ws"
        )
        self._fallback_thread = threading.Thread(
            target=self._fallback_loop, daemon=True, name="price-fallback"
        )
        self._ws_thread.start()
        self._fallback_thread.start()
        logger.debug("[PriceService] WebSocket + fallback threads started.")

    def stop(self) -> None:
        """Signal both threads to stop."""
        self._stop.set()
        if self._ws:
            self._ws.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set(self, symbol: str, price: float) -> None:
        """Update in-memory cache only."""
        with self._lock:
            self._prices[symbol]  = price
            self._updated[symbol] = time.time()

    def _crypto_symbols(self) -> list[str]:
        with self._lock:
            return [s for s in self._symbols if s in CRYPTO_SYMBOLS]

    # ── WebSocket thread ──────────────────────────────────────────────────────

    def _ws_loop(self) -> None:
        """Maintain a persistent Binance WebSocket connection. Auto-reconnects."""
        while not self._stop.is_set():
            symbols = self._crypto_symbols()
            if not symbols:
                self._stop.wait(1)
                continue

            streams = "/".join(
                f"{BINANCE_SYMBOLS[s].lower()}@aggTrade"
                for s in symbols
                if s in BINANCE_SYMBOLS
            )
            url = f"{BINANCE_WS_BASE}?streams={streams}"

            logger.info(f"[PriceService] Connecting WebSocket for: {', '.join(symbols)}")

            self._ws = websocket.WebSocketApp(
                url,
                on_open    = self._on_ws_open,
                on_message = self._on_ws_message,
                on_error   = self._on_ws_error,
                on_close   = self._on_ws_close,
            )
            # run_forever blocks until disconnect
            self._ws.run_forever(ping_interval=30, ping_timeout=10)

            if not self._stop.is_set():
                logger.warning(
                    f"[PriceService] WebSocket disconnected. "
                    f"Reconnecting in {WS_RECONNECT:.0f}s ..."
                )
                self._stop.wait(WS_RECONNECT)

    def _on_ws_open(self, ws) -> None:
        self._ws_connected = True
        logger.info("[PriceService] WebSocket connected. Receiving live prices.")

    def _on_ws_message(self, ws, raw: str) -> None:
        try:
            msg  = json.loads(raw)
            data = msg.get("data", msg)   # combined stream wraps in {"stream":..,"data":..}
            if data.get("e") != "aggTrade":
                return
            pair  = data["s"]             # e.g. "BTCUSDT"
            price = float(data["p"])      # last trade price
            sym   = BINANCE_REVERSE.get(pair)
            if sym:
                self._set(sym, price)
                _file_cache_write(sym, price)
        except Exception as e:
            logger.debug(f"[PriceService] WS message parse error: {e}")

    def _on_ws_error(self, ws, error) -> None:
        logger.warning(f"[PriceService] WebSocket error: {error}")
        self._ws_connected = False

    def _on_ws_close(self, ws, code, msg) -> None:
        self._ws_connected = False
        logger.debug(f"[PriceService] WebSocket closed (code={code}).")

    # ── Fallback REST thread ──────────────────────────────────────────────────

    def _fallback_loop(self) -> None:
        """
        Poll REST sources only when the WebSocket is down.
        Checks file cache first so multiple processes don't duplicate calls.
        """
        while not self._stop.is_set():
            self._stop.wait(POLL_INTERVAL)

            if self._ws_connected:
                continue   # WebSocket is healthy — nothing to do

            symbols = self._crypto_symbols()
            if not symbols:
                continue

            logger.debug("[PriceService] WS down — polling REST fallback.")

            stale = []
            for sym in symbols:
                cached = _file_cache_read(sym)
                if cached:
                    self._set(sym, cached)
                else:
                    stale.append(sym)

            if not stale:
                continue

            # ── Binance REST ──────────────────────────────────────────────────
            remaining = self._fetch_binance_rest(stale)

            # ── Coinbase REST (for any symbols Binance REST missed) ───────────
            if remaining:
                self._fetch_coinbase_rest(remaining)

    def _fetch_binance_rest(self, symbols: list[str]) -> list[str]:
        """
        Fetch prices via Binance REST batch endpoint.
        Returns the list of symbols that still need fetching (on failure).
        """
        pairs = [BINANCE_SYMBOLS[s] for s in symbols if s in BINANCE_SYMBOLS]
        if not pairs:
            return symbols
        try:
            symbols_param = json.dumps(pairs, separators=(",", ":"))
            url = f"{BINANCE_REST_URL}?symbols={symbols_param}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            for item in data:
                sym = BINANCE_REVERSE.get(item["symbol"])
                if sym:
                    price = float(item["price"])
                    self._set(sym, price)
                    _file_cache_write(sym, price)
            logger.debug(f"[PriceService] Binance REST updated: {', '.join(symbols)}")
            return []   # all fetched
        except Exception as e:
            logger.warning(f"[PriceService] Binance REST failed: {e} -- trying Coinbase.")
            return symbols

    def _fetch_coinbase_rest(self, symbols: list[str]) -> None:
        """Fetch prices from Coinbase public REST API (no auth required)."""
        for sym in symbols:
            pair = COINBASE_SYMBOLS.get(sym)
            if not pair:
                continue
            try:
                url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data  = json.loads(resp.read())
                    price = float(data["data"]["amount"])
                self._set(sym, price)
                _file_cache_write(sym, price)
                logger.debug(f"[PriceService] Coinbase REST: {sym} = ${price:,.2f}")
            except Exception as e:
                logger.warning(f"[PriceService] Coinbase REST failed for {sym}: {e}")


# ── Module-level accessor ─────────────────────────────────────────────────────
def get() -> PriceService:
    """Return the singleton PriceService, starting threads if needed."""
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
