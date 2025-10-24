# main_breakout_v1_render_hibrido.py
# ✅ Híbrido (3m + 5m + 15m)
# ✅ Corrigido alerta do 15m (dispara apenas no cruzamento inicial)
# ✅ Apenas 3 alertas ativos:
#    🟡 Rompimento MA200 (3m)
#    🟠 Confirmação EMA9×MA200 (5m)
#    🟢 Tendência consolidada (15m)
# ✅ Apenas pares spot reais em USDT
# ✅ Cooldown 8 minutos

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

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m híbrido) — rompimento, confirmação e tendência | 🇧🇷", 200

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
    blocked = (
        "UP", "DOWN", "BULL", "BEAR",
        "BUSD", "FDUSD", "TUSD", "USDC", "USDP", "USD1", "USDE", "XUSD", "USDX", "GUSD", "BFUSD",
        "EUR", "EURS", "CEUR", "BRL", "TRY",
        "PERP", "_PERP", "STABLE", "TEST"
    )
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        try:
            qv = float(d.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERT STATE ----------------
LAST_HIT = {}

def allowed(symbol, kind):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= COOLDOWN_SEC

def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            ma200_3 = sma(c3, 200)
            rsi3 = calc_rsi(c3, 14)
            i3 = len(c3)-1
            rompe_ma200_3m = (c3[i3-1] < ma200_3[i3-1]) and (c3[i3] > ma200_3[i3])
            if rompe_ma200_3m and allowed(symbol, "ROMP_3M"):
                msg = (f"🟡 {symbol} — ROMPIMENTO MA200 (3m)\n"
                       f"• Preço rompeu MA200 de baixo para cima\n"
                       f"• RSI:{rsi3[-1]:.1f}\n"
                       f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "ROMP_3M")

        # -------- 5m --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            ema9_5 = ema(c5, 9)
            ma200_5 = sma(c5, 200)
            rsi5 = calc_rsi(c5, 14)
            i5 = len(c5)-1
            cruzamento_ma200_5m = (ema9_5[i5-1] < ma200_5[i5-1]) and (ema9_5[i5] > ma200_5[i5])
            if cruzamento_ma200_5m and allowed(symbol, "CONF_5M"):
                msg = (f"🟠 {symbol} — CONFIRMAÇÃO (5m)\n"
                       f"• EMA9 cruzou MA200 de baixo para cima\n"
                       f"• RSI:{rsi5[-1]:.1f}\n"
                       f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_5M")

        # -------- 15m --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]
            ema9_15 = ema(c15, 9)
            ema20_15 = ema(c15, 20)
            ma50_15 = sma(c15, 50)
            ma200_15 = sma(c15, 200)
            rsi15 = calc_rsi(c15, 14)
            j = len(c15)-1
            formou_agora_15m = (
                ema9_15[j-1] <= ema20_15[j-1] or ema20_15[j-1] <= ma50_15[j-1] or ma50_15[j-1] <= ma200_15[j-1]
            ) and (ema9_15[j] > ema20_15[j] > ma50_15[j] > ma200_15[j]) and (rsi15[-1] > 55)
            if formou_agora_15m and allowed(symbol, "TEND_15M"):
                msg = (f"🟢 {symbol} — TENDÊNCIA CONSOLIDADA (15m)\n"
                       f"• EMA9>EMA20>MA50>MA200 e RSI>55\n"
                       f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "TEND_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown 8m | {now_br()} (UTC-3)\n──────────────────────────────")
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
