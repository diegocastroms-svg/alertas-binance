# ============================================================
#  Binance SPOT Monitor — v12 (fullformat)
#  Base: v11.9 greenlong_fixed + formatação completa curto prazo
#  ------------------------------------------------------------
#  Autor: Diego & Aurora — 2025-10-09
# ============================================================

import os, asyncio, time
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL = "5m"
SHORTLIST_N = 80
COOLDOWN_SEC = 15 * 60
MIN_PCT = 1.0
MIN_QV = 300_000.0

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
RSI_LEN = 14
VOL_MA = 9

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- UTILS ----------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session: aiohttp.ClientSession, text: str):
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            async with session.post(url, data=payload, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Telegram error:", e)

# ---------------- INDICADORES ----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    out = []
    if not seq: return out
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0] * len(closes)
    if len(closes) < period + 1: return rsis
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def compute_indicators(o, h, l, c, v):
    ema9 = ema(c, EMA_FAST)
    ma20 = sma(c, MA_SLOW)
    ma50 = sma(c, MA_MED)
    rsi14 = rsi_wilder(c, RSI_LEN)
    vol_ma = sma(v, VOL_MA)
    return ema9, ma20, ma50, rsi14, vol_ma

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o, h, l, c, v = [], [], [], [], []
    for k in data:
        o.append(float(k[1]))
        h.append(float(k[2]))
        l.append(float(k[3]))
        c.append(float(k[4]))
        v.append(float(k[5]))
    return o, h, l, c, v

async def get_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=80):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR")): continue
        pct = abs(float(t.get("priceChangePercent","0") or 0.0))
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if pct >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ---------------- ANTI-SPAM ----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
    def allowed(self, symbol: str) -> bool:
        return time.time() - self.cooldown[symbol] >= COOLDOWN_SEC
    def mark(self, symbol: str):
        self.cooldown[symbol] = time.time()

# ---------------- ALERTAS CURTOS (FULL FORMAT) ----------------
async def candle_worker(session, symbol, monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL, limit=200)
        ema9, ma20, ma50, rsi14, vol_ma = compute_indicators(o,h,l,c,v)
        last, prev = len(c)-1, len(c)-2
        ts = ts_brazil_now()
        sym_pretty = fmt_symbol(symbol)
        last_price = c[-1]

        # Exemplo: tendência curta com formato completo
        if ema9[last] > ma20[last] > ma50[last] and rsi14[last] > 55:
            desc = f"RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA"
            text = (
                f"⭐ {sym_pretty} 📈 — TENDÊNCIA CURTA | 🏆 RS+💰 {last_price:.6f}\n"
                f"🧠 {desc}\n"
                f"⏰ {ts}\n"
                f"{binance_links(symbol)}"
            )
            if monitor.allowed(symbol):
                await send_alert(session, text)
                monitor.mark(symbol)

        # Reteste EMA9
        if abs(ema9[last] - c[last]) / ema9[last] < 0.01 and rsi14[last] > 50:
            desc = f"Reteste na EMA9 + reação | RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA"
            text = (
                f"⭐ {sym_pretty} ♻️ — RETESTE EMA9 | 🏆 RS+💰 {last_price:.6f}\n"
                f"🧠 {desc}\n"
                f"⏰ {ts}\n"
                f"{binance_links(symbol)}"
            )
            if monitor.allowed(symbol):
                await send_alert(session, text)
                monitor.mark(symbol)

    except Exception as e:
        print("candle_worker error:", symbol, e)

# ---------------- LONG ALERTS (🟢) ----------------
async def long_extensions_worker(session, symbol, monitor):
    try:
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=200)
        ema9, ma20, ma50, rsi14, vol_ma = compute_indicators(o1,h1,l1,c1,v1)
        last = len(c1)-1
        if ema9[last] > ma20[last] > ma50[last] and rsi14[last] > 50:
            ts = ts_brazil_now()
            sym_pretty = fmt_symbol(symbol)
            last_price = c1[-1]
            desc = f"EMA9>MA20>MA50 | RSI {rsi14[last]:.1f}"
            txt = (
                f"🟢 <b>{sym_pretty} — TENDÊNCIA LONGA CONFIRMADA (1H)</b>\n"
                f"💰 {last_price:.6f}\n"
                f"🧠 {desc}\n"
                f"⏰ {ts}\n"
                f"{binance_links(symbol)}"
            )
            if monitor.allowed(symbol):
                await send_alert(session, txt)
                monitor.mark(symbol)
    except Exception as e:
        print("long_extensions error:", symbol, e)

# ---------------- LOOP PRINCIPAL ----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
        ts = ts_brazil_now()
        await send_alert(session, f"💻 v12 (fullformat) — {len(watchlist)} pares SPOT — {ts}")
        while True:
            tasks = []
            for s in watchlist:
                tasks.append(candle_worker(session, s, monitor))
                tasks.append(long_extensions_worker(session, s, monitor))
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(180)

# ---------------- FLASK KEEP-ALIVE ----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Alerts Bot (v12 fullformat) ativo!"

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
