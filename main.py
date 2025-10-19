# main.py
# ATLAS – Binance SPOT intrabar MA cross alerts (USDT pairs)
# - Timeframes: 5m and 15m
# - Top 50 symbols by 24h quote volume (USDT only), excluding UP/DOWN/BULL/BEAR/PERP and non-USDT quotes
# - Indicators: EMA 9; SMA 20, 50, 200
# - Intrabar: detects crossovers using the current (live) candle close via REST polling
# - Telegram alerts with 15-minute cooldown per (symbol, alert-type)
# - Clean code, English, Render-ready
#
# Env vars required:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
# Optional:
#   BINANCE_BASE (default: https://api.binance.com)
#   POLL_SECONDS (default: 10)
#   TOP_N (default: 50)
#
# Notes:
# - Uses only moving average crossovers as requested.
# - Alerts are independent and fire exactly at the crossover moment (intrabar, no candle close wait).
# - Excludes tokens with UP, DOWN, BULL, BEAR, PERP and non-USDT quotes (also excludes BUSD, FDUSD, TUSD, USDC by design).
# - Designed to run as a long-lived worker on Render.

import os
import time
import math
import json
import queue
import atexit
import signal
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------------------------- Config ----------------------------

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOP_N = int(os.getenv("TOP_N", "50"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))  # keep >=10 to be gentle with limits
TIMEZONE = ZoneInfo("America/Sao_Paulo")
VERSION = "v3.3"

# HTTP
HTTP_TIMEOUT = (5, 15)  # (connect, read)
SESSION = requests.Session()

# Threads
MAX_WORKERS = max(8, min(32, os.cpu_count() or 8))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Cooldown (seconds)
COOLDOWN = 15 * 60  # 15 minutes

# Klines needed to compute MAs up to 200 periods and compare t-1 vs t
KLIMIT = 210  # a little headroom for stability

# ---------------------- Utility Functions ----------------------

def now_br_dt() -> datetime:
    return datetime.now(tz=TIMEZONE)

def now_br_str() -> str:
    return now_br_dt().strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram env vars missing; message skipped.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = SESSION.post(url, timeout=HTTP_TIMEOUT, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        })
        if resp.status_code != 200:
            logging.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logging.exception("Telegram send exception: %s", e)

def http_get(path: str, params: dict | None = None):
    url = f"{BINANCE_BASE}{path}"
    r = SESSION.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def is_excluded_symbol(symbol: str) -> bool:
    # Exclude leveraged/derivative-like and non-spot tickers
    # Also exclude any non-USDT quoted pairs (we filter for USDT anyway)
    bad_substrings = ("UP", "DOWN", "BULL", "BEAR", "PERP")
    # Exclude if ends with other stable quotes (defensive)
    bad_quotes = ("BUSD", "FDUSD", "TUSD", "USDC")
    if not symbol.endswith("USDT"):
        return True
    if any(b in symbol for b in bad_substrings):
        return True
    if any(symbol.endswith(bq) for bq in bad_quotes):
        return True
    return False

# ---------------------- Symbol Universe ------------------------

def fetch_top_usdt_symbols(top_n: int) -> list[str]:
    """
    Get top-N USDT spot symbols by 24h quoteVolume, excluding unwanted tokens.
    """
    # Fetch 24h tickers
    tickers = http_get("/api/v3/ticker/24hr")
    # Filter only USDT quote, exclude unwanted, keep TRADING from exchangeInfo
    exchange_info = http_get("/api/v3/exchangeInfo")
    status_map = {}
    for s in exchange_info.get("symbols", []):
        status_map[s["symbol"]] = s.get("status") == "TRADING"

    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if is_excluded_symbol(sym):
            continue
        if not status_map.get(sym, False):
            continue
        try:
            qv = float(t.get("quoteVolume", "0"))
        except Exception:
            qv = 0.0
        candidates.append((sym, qv))

    candidates.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in candidates[:top_n]]
    return top

# ---------------------- Indicators -----------------------------

def sma(values: list[float], length: int) -> list[float]:
    out = [math.nan] * len(values)
    if length <= 0 or len(values) < length:
        return out
    csum = 0.0
    for i, v in enumerate(values):
        csum += v
        if i >= length:
            csum -= values[i - length]
        if i >= length - 1:
            out[i] = csum / length
    return out

def ema(values: list[float], length: int) -> list[float]:
    out = [math.nan] * len(values)
    if length <= 0 or len(values) < length:
        return out
    k = 2.0 / (length + 1.0)
    # seed with SMA of first 'length' values
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    for i in range(length, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out

def crossed_up(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    if math.isnan(prev_a) or math.isnan(prev_b) or math.isnan(curr_a) or math.isnan(curr_b):
        return False
    return prev_a <= prev_b and curr_a > curr_b

# ---------------------- Klines / Prices ------------------------

def fetch_klines(symbol: str, interval: str, limit: int = KLIMIT) -> list[list]:
    """
    Returns raw kline arrays as per Binance API:
    [
      [
        openTime, open, high, low, close, volume,
        closeTime, quoteAssetVolume, trades, takerBaseVol, takerQuoteVol, ignore
      ],
      ...
    ]
    """
    return http_get("/api/v3/klines", params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })

def parse_closes(klines: list[list]) -> list[float]:
    closes = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except Exception:
            closes.append(math.nan)
    return closes

def last_price_from_klines(klines: list[list]) -> float | None:
    if not klines:
        return None
    try:
        return float(klines[-1][4])
    except Exception:
        return None

# ---------------------- Alert Engine ---------------------------

class AlertCooldown:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def ready(self, symbol: str, alert_key: str) -> bool:
        key = (symbol, alert_key)
        now = time.time()
        with self._lock:
            ts = self._last.get(key, 0.0)
            return (now - ts) >= self.seconds

    def stamp(self, symbol: str, alert_key: str) -> None:
        key = (symbol, alert_key)
        with self._lock:
            self._last[key] = time.time()

cooldown = AlertCooldown(COOLDOWN)

def format_alert(symbol: str, label: str, price: float) -> str:
    # Labels and emojis:
    # 🟢 Trend starting (5m)
    # 🟡 Pre-confirmed trend (5m/15m)
    # 🚀 Confirmed trend (15m)
    lines = [
        f"{label} {symbol} ⬆️",
        f"💰 {price}",
        f"🕒 {now_br_str()}",
    ]
    # Reorder to match examples: first line with emoji before symbol text
    # Examples given are in Portuguese; we keep content as requested (Portuguese messages).
    return "\n".join(lines)

def send_status(start_count: int):
    msg = f"✅ {VERSION} intrabar ativo | {start_count} pares SPOT | cooldown 15m | {now_br_str()} 🇧🇷"
    tg_send(msg)
    logging.info("Status sent: %s", msg)

def check_alerts_for_symbol(symbol: str, interval: str) -> None:
    """
    Pull klines, compute indicators, detect crossovers (intrabar), send alerts if any.
    """
    try:
        kl = fetch_klines(symbol, interval, KLIMIT)
    except Exception as e:
        logging.warning("Klines error %s %s: %s", symbol, interval, e)
        return

    closes = parse_closes(kl)
    if len(closes) < 200 + 2:
        return

    # Indicators
    ema9  = ema(closes, 9)
    ma20  = sma(closes, 20)
    ma50  = sma(closes, 50)
    ma200 = sma(closes, 200)

    i_prev = len(closes) - 2
    i_curr = len(closes) - 1

    price = last_price_from_klines(kl)
    if price is None or math.isnan(ema9[i_curr]) or math.isnan(ma200[i_curr]):
        return

    # Alert keys (cooldown buckets)
    # INIT_5M, PRE_5M, PRE_15M, CONF_15M
    if interval == "5m":
        # 🟢 Trend starting (5m): EMA9 crosses up MA20 and/or MA50
        trig_init = (
            crossed_up(ema9[i_prev], ma20[i_prev], ema9[i_curr], ma20[i_curr]) or
            crossed_up(ema9[i_prev], ma50[i_prev], ema9[i_curr], ma50[i_curr])
        )
        if trig_init and cooldown.ready(symbol, "INIT_5M"):
            text = "🟢 " + f"{symbol} ⬆️ Tendência iniciando (5m)\n" + f"💰 {price}\n" + f"🕒 {now_br_str()}"
            tg_send(text)
            cooldown.stamp(symbol, "INIT_5M")
            logging.info("Alert INIT_5M %s %s", symbol, price)

        # 🟡 Pre-confirmed (5m): MA20 and/or MA50 cross up MA200 (independent triggers)
        trig_pre5 = (
            crossed_up(ma20[i_prev], ma200[i_prev], ma20[i_curr], ma200[i_curr]) or
            crossed_up(ma50[i_prev], ma200[i_prev], ma50[i_curr], ma200[i_curr])
        )
        if trig_pre5 and cooldown.ready(symbol, "PRE_5M"):
            text = "🟡 " + f"{symbol} ⬆️ Tendência pré-confirmada (5m)\n" + f"💰 {price}\n" + f"🕒 {now_br_str()}"
            tg_send(text)
            cooldown.stamp(symbol, "PRE_5M")
            logging.info("Alert PRE_5M %s %s", symbol, price)

    elif interval == "15m":
        # 🟡 Pre-confirmed (15m): EMA9 crosses up MA200
        trig_pre15 = crossed_up(ema9[i_prev], ma200[i_prev], ema9[i_curr], ma200[i_curr])
        if trig_pre15 and cooldown.ready(symbol, "PRE_15m"):
            text = "🟡 " + f"{symbol} ⬆️ Tendência pré-confirmada (15m)\n" + f"💰 {price}\n" + f"🕒 {now_br_str()}"
            tg_send(text)
            cooldown.stamp(symbol, "PRE_15m")
            logging.info("Alert PRE_15m %s %s", symbol, price)

        # 🚀 Confirmed (15m): MA20 and/or MA50 cross up MA200
        trig_conf15 = (
            crossed_up(ma20[i_prev], ma200[i_prev], ma20[i_curr], ma200[i_curr]) or
            crossed_up(ma50[i_prev], ma200[i_prev], ma50[i_curr], ma200[i_curr])
        )
        if trig_conf15 and cooldown.ready(symbol, "CONF_15m"):
            text = "🚀 " + f"{symbol} ⬆️ Tendência confirmada (15m)\n" + f"💰 {price}\n" + f"🕒 {now_br_str()}"
            tg_send(text)
            cooldown.stamp(symbol, "CONF_15m")
            logging.info("Alert CONF_15m %s %s", symbol, price)

# ---------------------- Worker Loop ----------------------------

_stop_event = threading.Event()

def handle_signals():
    def _sig(_s, _f):
        logging.info("Signal received, stopping...")
        _stop_event.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def poll_cycle(symbols: list[str], interval: str):
    # Threaded fetch+check per symbol for this interval
    work = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for sym in symbols:
            work.append(ex.submit(check_alerts_for_symbol, sym, interval))
        for _ in as_completed(work):
            pass

def main():
    logging.info("Starting ATLAS %s", VERSION)
    handle_signals()

    try:
        symbols = fetch_top_usdt_symbols(TOP_N)
    except Exception as e:
        logging.exception("Failed to fetch top symbols: %s", e)
        symbols = []

    if not symbols:
        logging.error("No symbols to monitor. Exiting.")
        return

    # Initial status message
    send_status(len(symbols))

    # Main loop
    while not _stop_event.is_set():
        t0 = time.time()
        try:
            # 5m first, then 15m
            poll_cycle(symbols, "5m")
            poll_cycle(symbols, "15m")
        except Exception as e:
            logging.exception("Poll cycle error: %s", e)

        elapsed = time.time() - t0
        sleep_s = max(0.0, POLL_SECONDS - elapsed)
        if sleep_s > 0:
            _stop_event.wait(timeout=sleep_s)

    logging.info("Stopped.")

if __name__ == "__main__":
    main()
