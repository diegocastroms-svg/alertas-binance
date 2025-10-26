# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) — com alertas flexíveis e inicialização corrigida
# ✅ Mantém toda a estrutura e lógica original
# ✅ Corrige bug: agora o loop principal executa antes do Flask

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

# ---------------- AJUSTES FLEX ----------------
RSI_RANGE_REVERSAO = (45, 65)
RSI_RANGE_CONF = (55, 70)
VOL_MULTIPLIER = 1.2
MIN_VOL_24H = 15_000_000
RSI_RANGE_POSTPUMP = (50, 60)
VOL_MULTIPLIER_POSTPUMP = 1.3
POSTPUMP_LOOKBACK = 20
NAME_BLOCKLIST = ("PEPE", "FLOKI", "BONK", "SHIB", "DOGE")

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m) — flex RSI/Volume | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID): return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
    except: pass

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
    if len(seq) < period + 1: return [50.0] * len(seq)
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
            if isinstance(data, list): return data
            return []
    except: return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","EUR","BRL","TRY","PERP","_PERP")
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

# ---------------- ALERT STATE ----------------
LAST_HIT = {}
def allowed(symbol, kind): ts = LAST_HIT.get((symbol, kind), 0.0); return (time.time() - ts) >= COOLDOWN_SEC
def mark(symbol, kind): LAST_HIT[(symbol, kind)] = time.time()

# ---------------- SCANNER ----------------
async def scan_symbol(session, symbol):
    try:
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]; v3 = [float(k[5]) for k in k3]
            ema9_3, ema20_3, ma50_3, ma200_3 = ema(c3,9), ema(c3,20), sma(c3,50), sma(c3,200)
            rsi3 = calc_rsi(c3,14); vma20_3 = sum(v3[-20:])/20.0; i3 = len(c3)-1

            rsi_ok = (RSI_RANGE_REVERSAO[0] <= rsi3[-1] <= RSI_RANGE_CONF[1])
            vol_ok = v3[-1] >= VOL_MULTIPLIER * vma20_3
            touch_200 = c3[i3] <= ma200_3[i3]*1.02
            if ema9_3[i3] > ema20_3[i3] > ma50_3[i3] and touch_200 and rsi_ok and vol_ok and allowed(symbol,"START_3M"):
                msg=(f"🚀 {symbol} — INÍCIO DE TENDÊNCIA (3M)\n• EMA9>EMA20>MA50 • Tocando/rompendo MA200\n• RSI:{rsi3[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session,msg); mark(symbol,"START_3M")

        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]; v5 = [float(k[5]) for k in k5]
            ema9_5, ema20_5, ma50_5, ma200_5 = ema(c5,9), ema(c5,20), sma(c5,50), sma(c5,200)
            rsi5 = calc_rsi(c5,14); vma20_5 = sum(v5[-20:])/20.0; i5 = len(c5)-1
            rsi_ok = (RSI_RANGE_REVERSAO[0] <= rsi5[-1] <= RSI_RANGE_CONF[1])
            vol_ok = v5[-1] >= VOL_MULTIPLIER * vma20_5
            touch_200 = c5[i5] <= ma200_5[i5]*1.02
            if ema9_5[i5] > ema20_5[i5] > ma50_5[i5] and touch_200 and rsi_ok and vol_ok and allowed(symbol,"START_5M"):
                msg=(f"🚀 {symbol} — INÍCIO DE TENDÊNCIA (5M)\n• EMA9>EMA20>MA50 • Tocando/rompendo MA200\n• RSI:{rsi5[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session,msg); mark(symbol,"START_5M")

        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]; v15 = [float(k[5]) for k in k15]
            ema9_15, ema20_15, ma50_15, ma200_15 = ema(c15,9), ema(c15,20), sma(c15,50), sma(c15,200)
            rsi15 = calc_rsi(c15,14); vma20_15 = sum(v15[-20:])/20.0; j = len(c15)-1
            rsi_ok = (RSI_RANGE_REVERSAO[0] <= rsi15[-1] <= RSI_RANGE_CONF[1])
            vol_ok = v15[-1] >= VOL_MULTIPLIER * vma20_15
            touch_200 = c15[j] <= ma200_15[j]*1.02
            if ema9_15[j] > ema20_15[j] > ma50_15[j] and touch_200 and rsi_ok and vol_ok and allowed(symbol,"START_15M"):
                msg=(f"🚀 {symbol} — INÍCIO DE TENDÊNCIA (15M)\n• EMA9>EMA20>MA50 • Tocando/rompendo MA200\n• RSI:{rsi15[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session,msg); mark(symbol,"START_15M")

    except: return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown {COOLDOWN_SEC//60}m | {now_br()}\n──────────────────────────────")
        if not symbols: return
        while True:
            tasks = [scan_symbol(session, s) for s in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)

# ---------------- RUN (CORRIGIDO) ----------------
def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(main_loop())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

if __name__ == "__main__":
    start_bot()
