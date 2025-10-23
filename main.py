# main_reversao_v5_3_renderfix_3m_cruzamento_up_rsi.py
# ✅ Mantém 100% do código original
# ✅ Corrige horário UTC-3 real
# ✅ Corrige pré-confirmada (5 m e 15 m): cruzamento de baixo → cima da MA200

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m & 15m) — reversão por cruzamentos | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
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
        q.append(x)
        s += x
        if len(q) > n:
            s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def bollinger_bands(seq, n=20, mult=2):
    if len(seq) < n:
        return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i - n + 1) : i + 1]
        m = sum(window) / len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult * s)
        out_lower.append(m - mult * s)
    return out_upper, out_mid, out_lower

def calc_rsi(seq, period=14):
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq) - 1):
        diff = seq[i] - seq[i - 1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0] * (len(seq) - len(rsi)) + rsi

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
        "UP",
        "DOWN",
        "BULL",
        "BEAR",
        "BUSD",
        "FDUSD",
        "TUSD",
        "USDC",
        "USD1",
        "USDE",
        "PERP",
        "_PERP",
        "EUR",
        "EURS",
        "CEUR",
        "XUSD",
        "USDX",
        "GUSD",
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

# ---------------- CORE CHECKS ----------------
def detect_exhaustion_5m(o, h, l, c, v):
    if len(c) < 200:
        return False, ""

    last = len(c) - 1
    base = c[max(0, last - 15)]
    drop_pct = (c[last] / (base + 1e-12) - 1.0) * 100.0
    cond_queda = drop_pct <= -2.5

    recent = c[-5:]
    var_pct = (max(recent) - min(recent)) / (sum(recent) / len(recent) + 1e-12)
    cond_lateral = var_pct <= 0.04

    vol_ma20 = sum(v[-20:]) / 20.0
    cond_vol = v[-1] >= 1.0 * (vol_ma20 + 1e-12)

    rsi = calc_rsi(c, 14)
    cond_rsi = rsi[-1] < 38

    ema9_vals = ema(c, 9)
    cond_pos = c[-1] <= ema9_vals[-1] and c[-1] <= min(c[-10:])

    ma200_vals = sma(c, 200)
    cond_dist = (ma200_vals[-1] - c[-1]) / (ma200_vals[-1] + 1e-12) >= 0.02

    upper_x, mid_x, lower_x = bollinger_bands(c, 20, 2)
    bw_now = (upper_x[-1] - lower_x[-1]) / (mid_x[-1] + 1e-12)
    cond_bb_narrow = bw_now <= 0.05
    cond_toque_lower = l[-1] <= lower_x[-1] if lower_x else False

    corpo = abs(c[-1] - o[-1])
    amplitude = h[-1] - l[-1] + 1e-12
    corpo_ok = c[-1] > o[-1] and (corpo >= 0.3 * amplitude)
    pavio_compra = (h[-1] - c[-1]) < (c[-1] - l[-1])

    if (
        cond_queda
        and cond_lateral
        and cond_vol
        and cond_rsi
        and cond_pos
        and cond_dist
        and cond_bb_narrow
        and cond_toque_lower
        and corpo_ok
        and pavio_compra
    ):
        msg = f"🟣 <b>EXAUSTÃO / ACUMULAÇÃO (5m)</b>\n💰 {fmt_price(c[last])}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
        return True, msg
    return False, ""

def preconf_5m_cross_3_over_200(ema9, ma20, ma50, ma200):
    if len(ema9) < 2:
        return False
    i1 = len(ema9) - 1
    i0 = i1 - 1
    c9 = ema9[i0] < ma200[i0] and ema9[i1] > ma200[i1]
    c20 = ma20[i0] < ma200[i0] and ma20[i1] > ma200[i1]
    c50 = ma50[i0] < ma200[i0] and ma50[i1] > ma200[i1]
    return c9 or c20 or c50

def preconf_15m_ema9_over_200(ema9, ma200):
    if len(ema9) < 2:
        return False
    i1 = len(ema9) - 1
    i0 = i1 - 1
    return ema9[i0] < ma200[i0] and ema9[i1] > ma200[i1]

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            ema9_3 = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            if len(ema9_3) > 2:
                i = len(ema9_3) - 1
                cruza = ema9_3[i - 1] < ma200_3[i - 1] and ema9_3[i] >= ma200_3[i]
                if cruza and allowed(symbol, "CRUZ_3M"):
                    msg = f"🟢 {symbol} ⬆️ EMA9 cruzando MA200 (3m)\n💰 {fmt_price(c3[i])}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                    await tg(session, msg)
                    mark(symbol, "CRUZ_3M")

        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210:
            return
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        ma200_5 = sma(c5, 200)
        ema9_5 = ema(c5, 9)
        ma20_5 = sma(c5, 20)
        ma50_5 = sma(c5, 50)
        i5 = len(c5) - 1

        if preconf_5m_cross_3_over_200(ema9_5, ma20_5, ma50_5, ma200_5) and allowed(symbol, "PRE_5M"):
            p = fmt_price(c5[i5])
            msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PRE_5M")

        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210:
            return
        c15 = [float(k[4]) for k in k15]
        ema9_15 = ema(c15, 9)
        ma20_15 = sma(c15, 20)
        ma50_15 = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        j = len(c15) - 1

        if preconf_15m_ema9_over_200(ema9_15, ma200_15) and allowed(symbol, "PRE_15M"):
            p = fmt_price(c15[j])
            msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PRE_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(
            session,
            f"✅ Scanner ativo | {len(symbols)} pares | cooldown 15m | {now_br()} (UTC-3)\n──────────────────────────────",
        )
        if not symbols:
            return
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
