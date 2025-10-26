# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) — apenas NOVO ALERTA UNIFICADO DE TENDÊNCIA INICIAL
# ✅ Detecta o início do movimento pré-pump tocando/abaixo da MA200
# ✅ Estrutura original preservada, lógica limpa e direta

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- PARÂMETROS ----------------
RSI_LOW, RSI_HIGH = 55, 70
VOL_MULTIPLIER = 1.3
MIN_VOL_24H = 15_000_000
NAME_BLOCKLIST = ("PEPE","FLOKI","BONK","SHIB","DOGE")

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m, 15m) — alerta único pré-pump 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
    except:
        pass

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

def sma(seq, n):
    out, s = [], 0.0
    from collections import deque
    q = deque()
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s/len(q))
    return out

def ema(seq, span):
    if not seq: return []
    alpha = 2.0/(span+1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
        out.append(e)
    return out

def calc_rsi(seq, period=14):
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq)-1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period-1) + gain) / period
        avg_loss = (avg_loss * (period-1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0]*(len(seq)-len(rsi)) + rsi

LAST_HIT = {}
def allowed(symbol, kind):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= COOLDOWN_SEC
def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            if isinstance(data, list):
                return data
            return []
    except:
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","XUSD","USDX","GUSD","BFUSD",
               "EUR","EURS","CEUR","BRL","TRY","PERP","_PERP","STABLE","TEST")
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        if any(x in s for x in NAME_BLOCKLIST): continue
        try: qv = float(d.get("quoteVolume", "0") or 0.0)
        except: qv = 0.0
        if qv < float(MIN_VOL_24H): continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERTA UNIFICADO ----------------
async def detectar_tendencia(session, symbol, closes, vols, rsi, ema9, ma20, ma50, ma200, timeframe):
    try:
        i = len(closes)-1
        if len(closes) < 210: return
        avg_vol20 = sum(vols[-20:]) / 20.0
        close = closes[i]
        cond1 = ema9[i] > ma20[i] > ma50[i]
        cond2 = close >= ma200[i] * 0.995
        cond3 = RSI_LOW <= rsi[-1] <= RSI_HIGH
        cond4 = vols[-1] >= VOL_MULTIPLIER * avg_vol20
        if cond1 and cond2 and cond3 and cond4 and allowed(symbol, f"TEND_{timeframe}"):
            msg = (f"🚀 {symbol} — INÍCIO DE TENDÊNCIA ({timeframe})\n"
                   f"• EMA9>MA20>MA50 • Tocando/rompendo MA200\n"
                   f"• RSI:{rsi[-1]:.1f} ({RSI_LOW}-{RSI_HIGH}) • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                   f"💰 {fmt_price(close)}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, f"TEND_{timeframe}")
    except:
        return

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # 3m
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            await detectar_tendencia(session, symbol, c3, v3, calc_rsi(c3,14), ema(c3,9), ema(c3,20), sma(c3,50), sma(c3,200), "3M")

        # 5m
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            v5 = [float(k[5]) for k in k5]
            await detectar_tendencia(session, symbol, c5, v5, calc_rsi(c5,14), ema(c5,9), ema(c5,20), sma(c5,50), sma(c5,200), "5M")

        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]
            v15 = [float(k[5]) for k in k15]
            await detectar_tendencia(session, symbol, c15, v15, calc_rsi(c15,14), ema(c15,9), ema(c15,20), sma(c15,50), sma(c15,200), "15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown {COOLDOWN_SEC//60}m | {now_br()} (UTC-3)\n──────────────────────────────")
        if not symbols: return
        while True:
            tasks = [scan_symbol(session, s) for s in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)

# ---------------- RUN ----------------
def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception:
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
