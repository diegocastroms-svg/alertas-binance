# main_breakout_v1_render_hibrido.py
# ✅ Híbrido (3m + 5m + 15m) com confirmação multi-tempo
# ✅ Breakout (entrada), perda de força (saída) e pullbacks (20/50/200) nos 5m e 15m
# ✅ Apenas pares spot reais em USDT
# ✅ Cooldown 8 minutos

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60          # 8 minutos
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m híbrido) — breakout, pullbacks e saída | 🇧🇷", 200

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

def cross_up(a_prev, a_now, b_prev, b_now) -> bool:
    return a_prev <= b_prev and a_now > b_now

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

def bollinger_bands(seq, n=20, mult=2):
    if len(seq) < n: return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i-n+1):i+1]
        m = sum(window)/len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult*s)
        out_lower.append(m - mult*s)
    return out_upper, out_mid, out_lower

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
        # alavancados / leveraged
        "UP", "DOWN", "BULL", "BEAR",
        # stablecoins / sintéticos / paralelos
        "BUSD", "FDUSD", "TUSD", "USDC", "USDP", "USD1", "USDE", "XUSD", "USDX", "GUSD", "BFUSD",
        # fiat / outros mercados
        "EUR", "EURS", "CEUR", "BRL", "TRY",
        # perp / testes
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

# ---------------- CHECK HELPERS ----------------
def band_width(upper, mid, lower):
    if not upper or not mid or not lower: return 0.0
    return (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)

def widening_now(upper, mid, lower):
    if len(upper) < 2: return False
    bw_now = (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)
    bw_prev = (upper[-2] - lower[-2]) / (mid[-2] + 1e-12)
    return bw_now > bw_prev

def touches_and_closes_above(low, close, ref):
    return (low <= ref) and (close > ref)

def touches_and_closes_below(high, close, ref):
    return (high >= ref) and (close < ref)

def candle_green(close_, open_): return close_ > open_
def candle_red(close_, open_):   return close_ < open_

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k3 = await get_klines(session, symbol, "3m", limit=300)
        if len(k3) >= 210:
            o3 = [float(k[1]) for k in k3]
            h3 = [float(k[2]) for k in k3]
            l3 = [float(k[3]) for k in k3]
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]

            ema9_3 = ema(c3, 9)
            ema21_3 = ema(c3, 21)
            ema50_3 = ema(c3, 50)
            rsi3 = calc_rsi(c3, 14)
            vol_ma20_3 = sum(v3[-20:]) / 20.0

            if len(ema9_3) > 2:
                i = len(ema9_3) - 1
                # Condição precoce: preço atual > EMA 21 anterior OU cruzamento
                cross_up_9_21 = cross_up(ema9_3[i-1], ema9_3[i], ema21_3[i-1], ema21_3[i])
                pre_break = c3[-1] > ema21_3[i-1] * 1.01  # 1% acima da EMA 21 anterior
                vol_spike = v3[-1] >= 1.0 * vol_ma20_3  # Volume pelo menos igual à MA
                rsi_ok = rsi3[-1] > 25
                trend_ok = c3[-1] > ema50_3[-1]

                # Debug pra verificar valores
                print(f"Checando {symbol}: EMA9={ema9_3[-1]:.4f}, EMA21={ema21_3[-1]:.4f}, EMA21_prev={ema21_3[i-1]:.4f}, Vol={v3[-1]:.0f}, VolMA20={vol_ma20_3:.0f}, RSI={rsi3[-1]:.1f}, PreBreak={pre_break}, Cross={cross_up_9_21}")

                # Entrada antecipada
                if ((pre_break or cross_up_9_21) and vol_spike and rsi_ok and trend_ok and allowed(symbol, "BREAKOUT_3M")):
                    p = fmt_price(c3[i])
                    msg = f"🚀 {symbol} ⬆️ Breakout confirmado (3m) - INÍCIO\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                    await tg(session, msg)
                    mark(symbol, "BREAKOUT_3M")

                # Saída mantida
                if len(rsi3) >= 2 and i >= 1:
                    rsi_turn_top = rsi3[-2] >= 60 and rsi3[-1] < rsi3[-2] and rsi3[-1] <= 55
                    first_close_below_ema9 = c3[-1] < ema9_3[-1] and c3[-2] >= ema9_3[-2]
                    vol_falling = (len(v3) >= 3 and v3[-1] < v3[-2] and v3[-2] <= v3[-3]) or (v3[-1] < vol_ma20_3)
                    if (rsi_turn_top and first_close_below_ema9 and vol_falling and allowed(symbol, "EXIT_3M")):
                        p = fmt_price(c3[i])
                        msg = f"⚠️ {symbol} — Tendência perdendo força (saída)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                        await tg(session, msg)
                        mark(symbol, "EXIT_3M")

    except Exception as e:
        print(f"Erro em {symbol}: {e}")
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
