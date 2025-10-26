# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) — ALERTA ANTECIPADO (pré-pump real)
# ✅ Dispara quando o preço está tocando/ABAIXO da MA200 (janela ±1.5%) + início de força
# ✅ Estrutura original preservada

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
# Faixas pensadas para DISPARAR CEDO (antes do pump)
RSI_LOW, RSI_HIGH = 50, 62            # força inicial, sem pico
VOL_MULTIPLIER = 1.10                  # leve aumento de volume já basta
MIN_VOL_24H = 80_000_000               # filtro reforçado de liquidez
NAME_BLOCKLIST = (
    "PEPE","FLOKI","BONK","SHIB","DOGE",
    "HIFI","BAKE","WIF","MEME","1000","ORDI","ZK","ZRO","SAGA"
)
# Banda de “toque na 200”: dentro de ±1.5% da MA200 (permite abaixo)
BAND_200 = 0.015

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m, 15m) — alerta ANTECIPADO pré-pump 🇧🇷", 200

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

# ---------------- COOLDOWN ----------------
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
        # corta tokens hype
        if "AI" in s or "GPT" in s or "BOT" in s:
            continue
        try: qv = float(d.get("quoteVolume", "0") or 0.0)
        except: qv = 0.0
        if qv < float(MIN_VOL_24H): continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERTA ANTECIPADO ----------------
async def detectar_inicio_real(session, symbol, closes, vols, rsi, ema9, ema20, ma50, ma200, timeframe):
    """
    Dispara cedo:
    - Preço tocando/ABAIXO da MA200 (banda ±1.5%)
    - EMA9 > EMA20 (abertura inicial)
    - MA20 e MA50 ainda perto da 200 (sem alinhamento completo)
    - RSI 50–62 e leve aumento de volume
    - Vela anterior <= MA200 (garante que veio de baixo/encoste)
    """
    try:
        n = len(closes)
        if n < 210: return
        i = n - 1
        close_now = closes[i]
        ma200_now = ma200[i]
        avg_vol20 = sum(vols[-20:]) / 20.0

        # dentro da banda da 200 (permite abaixo)
        band_ok = (close_now >= ma200_now * (1 - BAND_200)) and (close_now <= ma200_now * (1 + BAND_200))
        # veio de baixo/encostando recentemente
        prev_below = closes[i-1] <= ma200[i-1] * (1 + 0.002)  # até +0,2% ainda conta como encosto
        # abertura inicial de médias curtas
        opening = ema9[i] > ema20[i]
        # MA20 e MA50 ainda “coladas” na 200 (sem alinhamento total)
        ma20_near = abs(ema20[i] - ma200_now) / max(ma200_now,1e-12) <= 0.012
        ma50_near = abs(ma50[i]  - ma200_now) / max(ma200_now,1e-12) <= 0.02
        # força inicial e volume começando
        rsi_ok  = RSI_LOW <= rsi[-1] <= RSI_HIGH
        vol_ok  = vols[-1] >= VOL_MULTIPLIER * (avg_vol20 + 1e-12)

        if band_ok and prev_below and opening and ma20_near and ma50_near and rsi_ok and vol_ok and allowed(symbol, f"TEND_{timeframe}"):
            msg = (f"🚀 {symbol} — INÍCIO ANTECIPADO ({timeframe})\n"
                   f"• Preço tocando/ABAIXO da MA200 (±{int(BAND_200*100)}%)\n"
                   f"• EMA9>EMA20 • MA20≈MA200 • MA50≈MA200\n"
                   f"• RSI:{rsi[-1]:.1f} ({RSI_LOW}-{RSI_HIGH}) • Vol ≥ {VOL_MULTIPLIER:.2f}×MA20\n"
                   f"💰 {fmt_price(close_now)}\n🕒 {now_br()}\n──────────────────────────────")
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
            await detectar_inicio_real(session, symbol, c3, v3, calc_rsi(c3,14), ema(c3,9), ema(c3,20), sma(c3,50), sma(c3,200), "3M")

        # 5m
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            v5 = [float(k[5]) for k in k5]
            await detectar_inicio_real(session, symbol, c5, v5, calc_rsi(c5,14), ema(c5,9), ema(c5,20), sma(c5,50), sma(c5,200), "5M")

        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]
            v15 = [float(k[5]) for k in k15]
            await detectar_inicio_real(session, symbol, c15, v15, calc_rsi(c15,14), ema(c15,9), ema(c15,20), sma(c15,50), sma(c15,200), "15M")

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
