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
COOLDOWN_SEC = 10 * 60          # 10 minutos
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

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m (MANTIDO) --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            ema9_3 = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            if len(ema9_3) > 2:
                i = len(ema9_3) - 1
                cruza = ema9_3[i-1] < ma200_3[i-1] and ema9_3[i] >= ma200_3[i]
                v3 = [float(k[5]) for k in k3]
                rsi3 = calc_rsi(c3, 14)
                vol_ma20_3 = sum(v3[-20:]) / 20.0
                dif = abs(ema9_3[i] - ma200_3[i]) / (ma200_3[i] + 1e-12)
                encostar = dif <= 0.001
                cruzou_ou_encostou = cruza or encostar
                if cruzou_ou_encostou and rsi3[-1] > 45 and v3[-1] >= 1.1 * (vol_ma20_3 + 1e-12) and allowed(symbol, "CRUZ_3M"):
                    msg = f"🟢 {symbol} ⬆️ TOQUE / CRUZAMENTO (3m)\n💰 {fmt_price(c3[i])}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                    await tg(session, msg)
                    mark(symbol, "CRUZ_3M")

        # -------- 5m --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210: return
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        ema9_5  = ema(c5, 9)
        ema20_5 = ema(c5, 20)
        ma50_5  = sma(c5, 50)
        ma200_5 = sma(c5, 200)
        upper5, mid5, lower5 = bollinger_bands(c5, 20, 2)
        rsi5 = calc_rsi(c5, 14)
        vma20_5 = sum(v5[-20:]) / 20.0

        i5 = len(c5) - 1
        cross_up_9_20_5   = (ema9_5[i5-1] <= ema20_5[i5-1]) and (ema9_5[i5] > ema20_5[i5])
        cross_down_9_20_5 = (ema9_5[i5-1] >= ema20_5[i5-1]) and (ema9_5[i5] < ema20_5[i5])
        bb_open_5 = widening_now(upper5, mid5, lower5)
        break_upper_5 = c5[-1] >= upper5[-1] if upper5 else False

        # -------- 15m --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210: return
        o15 = [float(k[1]) for k in k15]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]

        ema9_15  = ema(c15, 9)
        ema20_15 = ema(c15, 20)
        ma50_15  = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        upper15, mid15, lower15 = bollinger_bands(c15, 20, 2)
        rsi15 = calc_rsi(c15, 14)
        vma20_15 = sum(v15[-20:]) / 20.0

        j = len(c15) - 1
        trend_ok_15  = (ema9_15[j] > ema20_15[j]) and (rsi15[-1] > 55) and widening_now(upper15, mid15, lower15)

        # ==========================
        # 🚀 ROMPIMENTO CONFIRMADO
        # 5m gatilho + 15m confirmação
        # ==========================
        if (cross_up_9_20_5 and rsi5[-1] > 55 and v5[-1] >= 1.2*(vma20_5+1e-12)
            and bb_open_5 and break_upper_5 and trend_ok_15 and allowed(symbol, "BRK_5M15")):
            p = fmt_price(c5[i5])
            msg = f"🚀 {symbol} — Rompimento confirmado (5m + 15m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "BRK_5M15")

        # ==========================
        # ⚠️ PERDENDO FORÇA / SAÍDA (baseada no 5m)
        # ==========================
        losing_rsi_5   = rsi5[-1] < 45
        losing_price_5 = c5[-1] < ema9_5[-1]
        losing_vol_5   = v5[-1] < (vma20_5 + 1e-12)
        if (cross_down_9_20_5 or (losing_rsi_5 and losing_price_5 and losing_vol_5)) and allowed(symbol, "EXIT_5M"):
            p = fmt_price(c5[i5])
            msg = f"⚠️ {symbol} — Tendência perdendo força (saída)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "EXIT_5M")

        # ==========================
        # 🔄 PULLBACKS CONFIRMADOS (5m) — EMA20 / MA50 / MA200
        # Reentrada curta, desde que 15m continue alinhado (EMA9>EMA20)
        # ==========================
        if ema9_15[j] > ema20_15[j] and rsi15[-1] >= 52:
            # EMA20
            if touches_and_closes_above(l5[-1], c5[-1], ema20_5[-1]) and (50 <= rsi5[-1] <= 60) and v5[-1] >= max(v5[-2], vma20_5) and ema9_5[-1] >= ema20_5[-1] and allowed(symbol, "PB5_EMA20"):
                p = fmt_price(c5[i5])
                msg = f"🔄 {symbol} — Pullback confirmado (5m • EMA20)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "PB5_EMA20")
            # MA50
            if touches_and_closes_above(l5[-1], c5[-1], ma50_5[-1]) and (50 <= rsi5[-1] <= 60) and v5[-1] >= max(v5[-2], vma20_5) and ema9_5[-1] >= ema20_5[-1] and allowed(symbol, "PB5_MA50"):
                p = fmt_price(c5[i5])
                msg = f"🔄 {symbol} — Pullback confirmado (5m • MA50)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "PB5_MA50")
            # MA200
            if touches_and_closes_above(l5[-1], c5[-1], ma200_5[-1]) and (50 <= rsi5[-1] <= 60) and v5[-1] >= max(v5[-2], vma20_5) and ema9_5[-1] >= ema20_5[-1] and allowed(symbol, "PB5_MA200"):
                p = fmt_price(c5[i5])
                msg = f"🔄 {symbol} — Pullback confirmado (5m • MA200)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "PB5_MA200")

        # ==========================
        # 🔄 PULLBACKS CONFIRMADOS (15m) — EMA20 / MA50 / MA200
        # Reentrada para movimentos de 1–3 horas
        # ==========================
        # EMA20
        if touches_and_closes_above(l15[-1], c15[-1], ema20_15[-1]) and (50 <= rsi15[-1] <= 60) and v15[-1] >= max(v15[-2], vma20_15) and ema9_15[-1] >= ema20_15[-1] and allowed(symbol, "PB15_EMA20"):
            p = fmt_price(c15[j])
            msg = f"🔄 {symbol} — Pullback confirmado (15m • EMA20)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PB15_EMA20")
        # MA50
        if touches_and_closes_above(l15[-1], c15[-1], ma50_15[-1]) and (50 <= rsi15[-1] <= 60) and v15[-1] >= max(v15[-2], vma20_15) and ema9_15[-1] >= ema20_15[-1] and allowed(symbol, "PB15_MA50"):
            p = fmt_price(c15[j])
            msg = f"🔄 {symbol} — Pullback confirmado (15m • MA50)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PB15_MA50")
        # MA200
        if touches_and_closes_above(l15[-1], c15[-1], ma200_15[-1]) and (50 <= rsi15[-1] <= 60) and v15[-1] >= max(v15[-2], vma20_15) and ema9_15[-1] >= ema20_15[-1] and allowed(symbol, "PB15_MA200"):
            p = fmt_price(c15[j])
            msg = f"🔄 {symbol} — Pullback confirmado (15m • MA200)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PB15_MA200")

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

