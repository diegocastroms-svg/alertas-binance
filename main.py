# main_py15_final_corrigido.py
# ✅ Versão completa e revisada — inclui todos os alertas e correções
# Autor: Aurora GPT-5
# Data: 2025-10-09

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
SHORTLIST_N = 65
COOLDOWN_SEC = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT = 1.0
MIN_QV = 300_000.0

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
MA_LONG = 200
RSI_LEN = 14
VOL_MA = 9

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

# ----------------- Indicadores -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x)
        s += x
        if len(q) > n:
            s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq:
        return []
    out = []
    alpha = 2.0 / (span + 1.0)
    e = seq[0]
    out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0:
        return []
    deltas = [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsis
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100 - (100 / (1 + rs))
    return rsis

# função essencial — faltava em versões anteriores
def compute_indicators(o, h, l, c, v):
    ema9 = ema(c, EMA_FAST)
    ma20 = sma(c, MA_SLOW)
    ma50 = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    return ema9, ma20, ma50, ma200, rsi14, volma

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o, h, l, c, v = [], [], [], [], []
    for k in data[:-1]:
        o.append(float(k[1]))
        h.append(float(k[2]))
        l.append(float(k[3]))
        c.append(float(k[4]))
        v.append(float(k[5]))
    return o, h, l, c, v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    usdt = []
    blocked = ("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD","_BUSD","_FDUSD",
               "_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL","_ETH","_BNB","_SOL")
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent", "0") or 0)
        qv = float(t.get("quoteVolume", "0") or 0)
        if abs(pct) >= 1.0 and qv >= 300000:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown_short = defaultdict(lambda: 0.0)
        self.cooldown_long = defaultdict(lambda: 0.0)
        self.rs_24h = {}
        self.btc_pct = 0.0

    def allowed(self, s, k):
        return time.time() - self.cooldown_short[(s, k)] >= COOLDOWN_SEC

    def mark(self, s, k):
        self.cooldown_short[(s, k)] = time.time()

    def allowed_long(self, s):
        return time.time() - self.cooldown_long[s] >= COOLDOWN_LONGTERM

    def mark_long(self, s):
        self.cooldown_long[s] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

# ----------------- Worker Curto (5m) -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        o, h, l, c, v = await get_klines(session, symbol, "5m", 200)
        if len(c) < 60:
            return
        ema9, ma20, ma50, ma200, rsi14, volma = compute_indicators(o, h, l, c, v)
        last = len(c) - 1

        # 🚀 Tendência Iniciando (5m)
        if (
            last >= 1
            and ema9[last - 1] <= min(ma20[last - 1], ma50[last - 1])
            and ema9[last] > ma20[last] and ema9[last] > ma50[last]
            and monitor.allowed(symbol, "TENDENCIA_INICIANDO_5M")
        ):
            msg = f"⭐ {fmt_symbol(symbol)} ⬆️ — TENDÊNCIA INICIANDO (5m)\n💰 <code>{c[last]:.6f}</code>\n🧠 EMA9 cruzou MA20/MA50\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark(symbol, "TENDENCIA_INICIANDO_5M")

        # 🌕 Pré-Confirmação (5m)
        if ema9[last] > ma20[last] > ma50[last] > ma200[last] and monitor.allowed(symbol, "PRECONF_5M"):
            msg = f"🌕 {fmt_symbol(symbol)} — TENDÊNCIA PRÉ-CONFIRMADA (5m)\n💰 <code>{c[last]:.6f}</code>\n🧠 Médias 9/20/50 cruzaram MA200\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark(symbol, "PRECONF_5M")

    except Exception as e:
        print("Erro curto", symbol, e)

# ----------------- Worker Longo (1h / 4h) -----------------
async def longterm_worker(session, symbol, monitor: Monitor):
    try:
        o1, h1, l1, c1, v1 = await get_klines(session, symbol, "1h", 200)
        o4, h4, l4, c4, v4 = await get_klines(session, symbol, "4h", 200)
        if len(c1) < 60 or len(c4) < 60:
            return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1 = compute_indicators(o1, h1, l1, c1, v1)
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4 = compute_indicators(o4, h4, l4, c4, v4)
        last1 = len(c1) - 1
        last4 = len(c4) - 1

        # 🌕 Pré-Confirmação Longa (1H)
        if (
            last1 >= 1
            and ema9_1[last1 - 1] <= ma20_1[last1 - 1]
            and ema9_1[last1] > ma20_1[last1]
            and 50 <= rsi1[last1] <= 60
            and v1[last1] >= volma1[last1] * 1.05
            and monitor.allowed_long(symbol)
        ):
            msg = f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO LONGA (1H)</b>\n<b>💰</b> <code>{c1[last1]:.6f}</code>\n<b>RSI {rsi1[last1]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark_long(symbol)

        # 🚀 Tendência Longa Confirmada (1H)
        if (
            ema9_1[last1] > ma20_1[last1] > ma50_1[last1] > ma200_1[last1]
            and rsi1[last1] > 55
            and monitor.allowed_long(symbol)
        ):
            msg = f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA LONGA CONFIRMADA (1H)</b>\n<b>💰</b> <code>{c1[last1]:.6f}</code>\n<b>RSI {rsi1[last1]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark_long(symbol)

        # 🌕 Pré-Confirmação (4H)
        if (
            last4 >= 1
            and ema9_4[last4 - 1] <= ma20_4[last4 - 1]
            and ema9_4[last4] > ma20_4[last4]
            and rsi4[last4] > 50
            and monitor.allowed_long(symbol)
        ):
            msg = f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO LONGA (4H)</b>\n<b>💰</b> <code>{c4[last4]:.6f}</code>\n<b>RSI {rsi4[last4]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark_long(symbol)

        # 🚀 Tendência Longa Confirmada (4H)
        if (
            ema9_4[last4] > ma20_4[last4] > ma50_4[last4] > ma200_4[last4]
            and rsi4[last4] > 55
            and monitor.allowed_long(symbol)
        ):
            msg = f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA LONGA CONFIRMADA (4H)</b>\n<b>💰</b> <code>{c4[last4]:.6f}</code>\n<b>RSI {rsi4[last4]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
            await send_alert(session, msg)
            monitor.mark_long(symbol)

    except Exception as e:
        print("Erro longo", symbol, e)

# ----------------- Main -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        rs_map = {}
        btc_pct = 0.0
        for t in tickers:
            s = t.get("symbol", "")
            if s == "BTCUSDT":
                btc_pct = float(t.get("priceChangePercent", "0") or 0)
            if s.endswith("USDT"):
                rs_map[s] = float(t.get("priceChangePercent", "0") or 0)
        monitor.set_rs(rs_map, btc_pct)

        hello = f"💻 py15_final_corrigido | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                tasks.append(candle_worker(session, s, monitor))
                tasks.append(longterm_worker(session, s, monitor))
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- Flask -----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app = Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot — py15_final_corrigido 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
