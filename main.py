# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) — ALERTA ÚNICO: "INÍCIO DE TENDÊNCIA REAL (FLEX)"
# ✅ Detecção cedo (abaixo/tocando MA200) com faixas dinâmicas (sem números engessados)
# ✅ Estrutura original preservada (Flask, threading, cooldown, filtros e top N)
# ✅ Ajuste: MIN_VOL_24H = 50_000_000

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

# ---------------- PARÂMETROS FLEX ----------------
# Filtro de liquidez e "memecoins"
MIN_VOL_24H = 50_000_000   # 🔹 AJUSTADO para detectar tendências reais que começam antes de 80M
NAME_BLOCKLIST = (
    "PEPE","FLOKI","BONK","SHIB","DOGE",
    "HIFI","BAKE","WIF","MEME","1000","ORDI","ZK","ZRO","SAGA"
)
HYPE_SUBSTRINGS = ("AI","GPT","BOT")

# Tolerância base à MA200 (ajustada pela volatilidade, com limites)
BAND_200_BASE = 0.020  # 2.0% base
BAND_200_MIN  = 0.010  # 1.0% mínimo
BAND_200_MAX  = 0.025  # 2.5% máximo

# Volume: multiplicador dinâmico com base na volatilidade
VOL_MULT_MIN  = 1.05
VOL_MULT_MAX  = 1.30

# RSI: centrado dinamicamente com janelas móveis
RSI_CENTER_WIN = 20
RSI_MIN_FLOOR  = 42
RSI_MAX_CEIL   = 63
RSI_BAND       = 5

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m, 15m) — INÍCIO DE TENDÊNCIA REAL (FLEX) 🇧🇷", 200

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

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

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

def bollinger_bands(seq, n=20, mult=2.0):
    if len(seq) < n: 
        return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i-n+1):i+1]
        m = sum(window)/len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult*s)
        out_lower.append(m - mult*s)
    return out_upper, out_mid, out_lower

def calc_sar(highs, lows, step=0.02, max_step=0.2):
    if len(highs) < 2 or len(lows) < 2:
        return [0.0]*len(highs)
    sar = [0.0]*len(highs)
    uptrend = True
    af = step
    ep = highs[0]
    sar[0] = lows[0]
    for i in range(1, len(highs)):
        prev_sar = sar[i-1]
        if uptrend:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = min(sar_candidate, lows[i-1], lows[i])
            if highs[i] > ep:
                ep = highs[i]; af = min(af + step, max_step)
            if lows[i] < sar[i]:
                uptrend = False; sar[i] = ep; af = step; ep = lows[i]
        else:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = max(sar_candidate, highs[i-1], highs[i])
            if lows[i] < ep:
                ep = lows[i]; af = min(af + step, max_step)
            if highs[i] > sar[i]:
                uptrend = True; sar[i] = ep; af = step; ep = highs[i]
    return sar

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
        if any(h in s for h in HYPE_SUBSTRINGS): continue
        try: qv = float(d.get("quoteVolume", "0") or 0.0)
        except: qv = 0.0
        if qv < float(MIN_VOL_24H): continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERTA FLEX (núcleo) ----------------
async def detectar_tendencia_flex(session, symbol, k, timeframe_tag):
    try:
        if len(k) < 210: 
            return
        closes = [float(x[4]) for x in k]
        highs  = [float(x[2]) for x in k]
        lows   = [float(x[3]) for x in k]
        vols   = [float(x[5]) for x in k]
        i = len(closes) - 1
        ema9   = ema(closes, 9)
        ema20  = ema(closes, 20)
        ma50   = sma(closes, 50)
        ma200  = sma(closes, 200)
        rsi    = calc_rsi(closes, 14)
        bb_u, bb_m, bb_l = bollinger_bands(closes, n=20, mult=2.0)
        sar    = calc_sar(highs, lows, step=0.02, max_step=0.2)
        close_now, ma200_now = closes[i], ma200[i]
        ema9_now, ema20_now, ma50_now = ema9[i], ema20[i], ma50[i]
        rsi_now = rsi[-1]
        bbm_now = bb_m[i] if bb_m else close_now
        win = closes[-20:]
        mean_p = sum(win)/len(win)
        dev_p  = statistics.pstdev(win) if len(win) >= 2 else 0.0
        vol_norm = dev_p / max(mean_p, 1e-12)
        band200 = clamp(0.020 * (1.0 + 10.0*vol_norm), 0.010, 0.025)
        near_200 = (close_now >= ma200_now * (1.0 - band200)) and (close_now <= ma200_now * (1.0 + band200))
        crossed_recent = ema9_now > ema20_now and any(ema9[i-off] <= ema20[i-off] for off in (1,2,3) if i-off>=0)
        early_trend = ma50_now < ma200_now
        rsi_window = rsi[-20:] if len(rsi) >= 20 else rsi
        rsi_center = sum(rsi_window)/len(rsi_window) if rsi_window else 50.0
        rsi_low  = clamp(rsi_center - 5, 42, 61)
        rsi_high = clamp(rsi_center + 5, rsi_low+2, 63)
        rsi_ok = (rsi_now >= rsi_low) and (rsi_now <= rsi_high)
        avg_vol20 = sum(vols[-20:]) / 20.0
        vol_mult  = clamp(1.05 + 20.0*vol_norm, 1.05, 1.30)
        vol_ok = vols[-1] >= vol_mult * (avg_vol20 + 1e-12)
        bb_ok = close_now > bbm_now
        sar_ok = sar[i] < close_now
        dist_200 = (close_now / max(ma200_now,1e-12) - 1.0) * 100.0
        if (near_200 and crossed_recent and early_trend and rsi_ok and vol_ok and bb_ok and sar_ok and allowed(symbol, f"TEND_{timeframe_tag}")):
            msg = (f"🚀 {symbol} — INÍCIO DE TENDÊNCIA REAL ({timeframe_tag})\n"
                   f"• RSI: {rsi_now:.1f} | Faixa din.: {rsi_low:.0f}–{rsi_high:.0f}\n"
                   f"• Vol: {vols[-1]/max(avg_vol20,1e-12):.2f}×MA20 (mult alvo {vol_mult:.2f}×)\n"
                   f"• Distância MA200: {dist_200:+.2f}% | EMA9>EMA20 | SAR abaixo | Bollinger>média\n"
                   f"💰 {fmt_price(close_now)}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, f"TEND_{timeframe_tag}")
    except:
        return

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        for tf in ("3m","5m","15m"):
            k = await get_klines(session, symbol, tf, limit=210)
            if len(k) >= 210:
                await detectar_tendencia_flex(session, symbol, k, tf)
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
