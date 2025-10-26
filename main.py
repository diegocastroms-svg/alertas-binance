# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) — ALERTA ÚNICO: "INÍCIO DE TENDÊNCIA REAL (FLEX)"
# ✅ Detecção cedo (abaixo/tocando MA200) com faixas dinâmicas (sem números engessados)
# ✅ Estrutura original preservada (Flask, threading, cooldown, filtros e top N)

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
MIN_VOL_24H = 80_000_000
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
RSI_CENTER_WIN = 20  # janelas para média dinâmica
RSI_MIN_FLOOR  = 42  # piso absoluto
RSI_MAX_CEIL   = 63  # teto absoluto
RSI_BAND       = 5   # +/- faixa em torno do centro

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
    """Parabolic SAR básico (trend following); retorna lista do SAR."""
    if len(highs) < 2 or len(lows) < 2:
        return [0.0]*len(highs)
    sar = [0.0]*len(highs)
    uptrend = True
    af = step
    ep = highs[0]  # extreme point
    sar[0] = lows[0]
    for i in range(1, len(highs)):
        prev_sar = sar[i-1]
        if uptrend:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = min(sar_candidate, lows[i-1], lows[i])  # não pode ficar acima dos lows
            if highs[i] > ep:
                ep = highs[i]
                af = min(af + step, max_step)
            if lows[i] < sar[i]:  # reversão
                uptrend = False
                sar[i] = ep
                af = step
                ep = lows[i]
        else:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = max(sar_candidate, highs[i-1], highs[i])  # não pode ficar abaixo dos highs
            if lows[i] < ep:
                ep = lows[i]
                af = min(af + step, max_step)
            if highs[i] > sar[i]:  # reversão
                uptrend = True
                sar[i] = ep
                af = step
                ep = highs[i]
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
    """
    Alerta único "INÍCIO DE TENDÊNCIA REAL (FLEX)" aplicado ao timeframe atual.
    Condições:
      - Preço abaixo/tocando MA200 (tolerância dinâmica)
      - Cruzamento EMA9>EMA20 RECENTE
      - MA50 ainda abaixo da MA200 (início, não meio)
      - RSI em faixa dinâmica (centro móvel ±5, com pisos/tetos)
      - Volume atual >= multiplicador dinâmico * média20
      - Fechamento acima da banda média de Bollinger
      - SAR abaixo do preço
    """
    try:
        if len(k) < 210: 
            return
        closes = [float(x[4]) for x in k]
        highs  = [float(x[2]) for x in k]
        lows   = [float(x[3]) for x in k]
        vols   = [float(x[5]) for x in k]
        i = len(closes) - 1

        # Indicadores
        ema9   = ema(closes, 9)
        ema20  = ema(closes, 20)
        ma50   = sma(closes, 50)
        ma200  = sma(closes, 200)
        rsi    = calc_rsi(closes, 14)
        bb_u, bb_m, bb_l = bollinger_bands(closes, n=20, mult=2.0)
        sar    = calc_sar(highs, lows, step=0.02, max_step=0.2)

        close_now  = closes[i]
        ma200_now  = ma200[i]
        ema9_now   = ema9[i]
        ema20_now  = ema20[i]
        ma50_now   = ma50[i]
        rsi_now    = rsi[-1]
        bbm_now    = bb_m[i] if bb_m else close_now

        # Volatilidade local (20 velas) para ajustar bandas
        win = closes[-20:]
        mean_p = sum(win)/len(win)
        dev_p  = statistics.pstdev(win) if len(win) >= 2 else 0.0
        vol_norm = dev_p / max(mean_p, 1e-12)  # ~0.001 a 0.02+

        # Banda dinâmica em torno da MA200
        band200 = clamp(BAND_200_BASE * (1.0 + 10.0*vol_norm), BAND_200_MIN, BAND_200_MAX)
        near_200 = (close_now >= ma200_now * (1.0 - band200)) and (close_now <= ma200_now * (1.0 + band200))

        # Cruzamento recente EMA9>EMA20 (até 3 velas)
        crossed_recent = False
        if ema9_now > ema20_now:
            for off in (1, 2, 3):
                if i-off < 0: break
                if ema9[i-off] <= ema20[i-off]:
                    crossed_recent = True
                    break

        # MA50 ainda abaixo da 200 (garante "início")
        early_trend = ma50_now < ma200_now

        # RSI dinâmico (centro móvel dos últimos RSI_CENTER_WIN valores)
        rsi_window = rsi[-RSI_CENTER_WIN:] if len(rsi) >= RSI_CENTER_WIN else rsi
        rsi_center = sum(rsi_window)/len(rsi_window) if rsi_window else 50.0
        rsi_low  = clamp(rsi_center - RSI_BAND, RSI_MIN_FLOOR, RSI_MAX_CEIL-2)
        rsi_high = clamp(rsi_center + RSI_BAND, rsi_low+2, RSI_MAX_CEIL)
        rsi_ok   = (rsi_now >= rsi_low) and (rsi_now <= rsi_high)

        # Volume dinâmico
        avg_vol20 = sum(vols[-20:]) / 20.0
        vol_mult  = clamp(VOL_MULT_MIN + 20.0*vol_norm, VOL_MULT_MIN, VOL_MULT_MAX)
        vol_ok    = vols[-1] >= vol_mult * (avg_vol20 + 1e-12)

        # Bollinger média rompida (saindo da neutralidade)
        bb_ok = close_now > bbm_now

        # SAR abaixo do preço
        sar_ok = sar[i] < close_now

        # Distância percentual à MA200 (para mensagem)
        dist_200 = (close_now / max(ma200_now,1e-12) - 1.0) * 100.0

        if (near_200 and crossed_recent and early_trend and rsi_ok and vol_ok and bb_ok and sar_ok
            and allowed(symbol, f"TEND_{timeframe_tag}")):
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
        # 3m
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            await detectar_tendencia_flex(session, symbol, k3, "3m")

        # 5m
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            await detectar_tendencia_flex(session, symbol, k5, "5m")

        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            await detectar_tendencia_flex(session, symbol, k15, "15m")

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
