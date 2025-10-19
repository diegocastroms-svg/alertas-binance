# main.py
# ATLAS – Binance SPOT intrabar MA cross alerts (USDT pairs)
# Render-optimized version (fixed connection pool saturation)

import os
import time
import math
import json
import queue
import atexit
import signal
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ---------------------------- Config ----------------------------

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOP_N = int(os.getenv("TOP_N", "50"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
TIMEZONE = ZoneInfo("America/Sao_Paulo")
VERSION = "v3.3"

HTTP_TIMEOUT = (5, 15)
SESSION = requests.Session()

# ✅ FIX: Aumentar pool de conexões (corrige o erro dos logs)
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# ✅ Menos threads simultâneas para não saturar o pool
MAX_WORKERS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)

COOLDOWN = 15 * 60
KLIMIT = 210

# ---------------------- Utility Functions ----------------------

def now_br_str() -> str:
    return datetime.now(tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram vars missing; message skipped.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        SESSION.post(url, timeout=HTTP_TIMEOUT, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        })
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")

def http_get(path: str, params: dict | None = None):
    url = f"{BINANCE_BASE}{path}"
    r = SESSION.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def is_excluded_symbol(symbol: str) -> bool:
    bad_substrings = ("UP", "DOWN", "BULL", "BEAR", "PERP")
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
    tickers = http_get("/api/v3/ticker/24hr")
    exchange_info = http_get("/api/v3/exchangeInfo")
    status_map = {s["symbol"]: s.get("status") == "TRADING" for s in exchange_info.get("symbols", [])}

    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if is_excluded_symbol(sym) or not status_map.get(sym, False):
            continue
        try:
            qv = float(t.get("quoteVolume", "0"))
        except:
            qv = 0.0
        candidates.append((sym, qv))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in candidates[:top_n]]

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
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    for i in range(length, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out

def crossed_up(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    if math.isnan(prev_a) or math.isnan(prev_b) or math.isnan(curr_a) or math.isnan(curr_b):
        return False
    return prev_a <= prev_b and curr_a > curr_b

# ---------------------- Klines ------------------------

def fetch_klines(symbol: str, interval: str, limit: int = KLIMIT) -> list[list]:
    return http_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

def parse_closes(klines: list[list]) -> list[float]:
    return [float(k[4]) for k in klines if len(k) >= 5]

def last_price_from_klines(klines: list[list]) -> float | None:
    try:
        return float(klines[-1][4])
    except:
        return None

# ---------------------- Alert Engine ---------------------------

class AlertCooldown:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def ready(self, symbol: str, alert_key: str) -> bool:
        now = time.time()
        with self._lock:
            ts = self._last.get((symbol, alert_key), 0.0)
            return (now - ts) >= self.seconds

    def stamp(self, symbol: str, alert_key: str):
        with self._lock:
            self._last[(symbol, alert_key)] = time.time()

cooldown = AlertCooldown(COOLDOWN)

def send_status(start_count: int):
    msg = f"✅ {VERSION} intrabar ativo | {start_count} pares SPOT | cooldown 15m | {now_br_str()} 🇧🇷"
    tg_send(msg)
    logging.info("Status sent: %s", msg)

def check_alerts_for_symbol(symbol: str, interval: str):
    try:
        kl = fetch_klines(symbol, interval, KLIMIT)
    except Exception as e:
        logging.warning(f"Klines error {symbol} {interval}: {e}")
        return

    closes = parse_closes(kl)
    if len(closes) < 202:
        return

    ema9  = ema(closes, 9)
    ma20  = sma(closes, 20)
    ma50  = sma(closes, 50)
    ma200 = sma(closes, 200)

    i_prev = len(closes) - 2
    i_curr = len(closes) - 1
    price = last_price_from_klines(kl)
    if price is None:
        return

    if interval == "5m":
        trig_init = (
            crossed_up(ema9[i_prev], ma20[i_prev], ema9[i_curr], ma20[i_curr]) or
            crossed_up(ema9[i_prev], ma50[i_prev], ema9[i_curr], ma50[i_curr])
        )
        if trig_init and cooldown.ready(symbol, "INIT_5M"):
            tg_send(f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {price}\n🕒 {now_br_str()}")
            cooldown.stamp(symbol, "INIT_5M")

        trig_pre5 = (
            crossed_up(ma20[i_prev], ma200[i_prev], ma20[i_curr], ma200[i_curr]) or
            crossed_up(ma50[i_prev], ma200[i_prev], ma50[i_curr], ma200[i_curr])
        )
        if trig_pre5 and cooldown.ready(symbol, "PRE_5M"):
            tg_send(f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {price}\n🕒 {now_br_str()}")
            cooldown.stamp(symbol, "PRE_5M")

    elif interval == "15m":
        trig_pre15 = crossed_up(ema9[i_prev], ma200[i_prev], ema9[i_curr], ma200[i_curr])
        if trig_pre15 and cooldown.ready(symbol, "PRE_15M"):
            tg_send(f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {price}\n🕒 {now_br_str()}")
            cooldown.stamp(symbol, "PRE_15M")

        trig_conf15 = (
            crossed_up(ma20[i_prev], ma200[i_prev], ma20[i_curr], ma200[i_curr]) or
            crossed_up(ma50[i_prev], ma200[i_prev], ma50[i_curr], ma200[i_curr])
        )
        if trig_conf15 and cooldown.ready(symbol, "CONF_15M"):
            tg_send(f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {price}\n🕒 {now_br_str()}")
            cooldown.stamp(symbol, "CONF_15M")

# ---------------------- Worker Loop ----------------------------

_stop_event = threading.Event()

def handle_signals():
    def _sig(_s, _f):
        logging.info("Signal received, stopping...")
        _stop_event.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def poll_cycle(symbols: list[str], interval: str):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(check_alerts_for_symbol, sym, interval) for sym in symbols]
        for _ in as_completed(futures):
            pass

def main():
    logging.info("Starting ATLAS %s", VERSION)
    handle_signals()

    try:
        symbols = fetch_top_usdt_symbols(TOP_N)
    except Exception as e:
        logging.exception(f"Failed to fetch symbols: {e}")
        symbols = []

    if not symbols:
        logging.error("No symbols to monitor. Exiting.")
        return

    send_status(len(symbols))

    while not _stop_event.is_set():
        t0 = time.time()
        try:
            poll_cycle(symbols, "5m")
            poll_cycle(symbols, "15m")
        except Exception as e:
            logging.warning(f"Poll error: {e}")
        elapsed = time.time() - t0
        sleep_s = max(0.0, POLL_SECONDS - elapsed)
        if sleep_s > 0:
            _stop_event.wait(timeout=sleep_s)

    logging.info("Stopped.")

if __name__ == "__main__":
    main()
