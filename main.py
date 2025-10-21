# main_reversao_v5_3_renderfix_15m_adiantado.py
# ✅ Idêntico ao v5_3_renderfix original
# ✅ Apenas adianta o alerta de tendência confirmada (15m)
# ✅ Nenhuma outra alteração feita

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime
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
    return "✅ Scanner ativo (5m & 15m) — reversão por cruzamentos | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

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
        "UP", "DOWN", "BULL", "BEAR", "BUSD", "FDUSD", "TUSD", "USDC", "USD1",
        "USDE", "PERP", "_PERP", "EUR", "EURS", "CEUR", "XUSD", "USDX", "GUSD"
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
    if len(c) < 40: return False, ""
    last = len(c)-1

    base = c[max(0, last-10)]
    drop_pct = (c[last]/(base+1e-12)-1.0)*100.0
    cond_drop = drop_pct <= -2.5

    recent = c[-5:]
    var_pct = (max(recent) - min(recent)) / (sum(recent)/len(recent) + 1e-12)
    cond_side = var_pct <= 0.012

    vol_ma20 = sum(v[-20:]) / 20.0
    cond_vol = v[-1] >= 0.9 * (vol_ma20 + 1e-12)

    if cond_drop and cond_side and cond_vol:
        msg = f"🟣 <b>ACUMULAÇÃO / EXAUSTÃO VENDEDORA (5m)</b>\n💰 {fmt_price(c[last])}\n🕒 {now_br()}"
        return True, msg
    return False, ""

def tendencia_iniciando_5m(ema9, ma20, ma50):
    if len(ema9) < 3: return False
    i1 = len(ema9)-1; i0 = i1-1; i2 = i1-2
    cross_9_20 = (ema9[i1] > ma20[i1]) and (ema9[i0] <= ma20[i0] or ema9[i2] < ma20[i2])
    cross_9_50 = (ema9[i1] > ma50[i1]) and (ema9[i0] <= ma50[i0] or ema9[i2] < ma50[i2])
    ok = (cross_9_20 and ema9[i1] > ma50[i1]) or (cross_9_50 and ema9[i1] > ma20[i1]) or (cross_9_20 and cross_9_50)
    return ok

def preconf_5m_cross_3_over_200(ema9, ma20, ma50, ma200):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    all_above = ema9[i1] > ma200[i1] and ma20[i1] > ma200[i1] and ma50[i1] > ma200[i1]
    c9  = cross_up(ema9[i0], ema9[i1], ma200[i0], ma200[i1])
    c20 = cross_up(ma20[i0], ma20[i1], ma200[i0], ma200[i1])
    c50 = cross_up(ma50[i0], ma50[i1], ma200[i0], ma200[i1])
    recent_cross = (c9 or c20 or c50)
    return (ema9[i1] > ma200[i1] and ma20[i1] > ma200[i1]) and recent_cross

def preconf_15m_ema9_over_200(ema9, ma200):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    return cross_up(ema9[i0], ema9[i1], ma200[i0], ma200[i1])

# ⚙️ ALTERADO: adianta o alerta se estrutura já positiva e EMA9 acima da MA200
def conf_15m_all_over_200_recent(ema9, ma20, ma50, ma200):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    structure = (ema9[i1] > ma20[i1] > ma50[i1] > ma200[i1])
    c20 = cross_up(ma20[i0], ma20[i1], ma200[i0], ma200[i1])
    c50 = cross_up(ma50[i0], ma50[i1], ma200[i0], ma200[i1])
    recent = (c20 or c50)
    return (recent or (structure and ema9[i1] > ma200[i1]))

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # 5m
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210: return
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        ma200_5 = sma(c5, 200)
        ema9_5  = ema(c5, 9)
        ma20_5  = sma(c5, 20)
        ma50_5  = sma(c5, 50)
        i5 = len(c5)-1
        below_200_context = c5[i5] < ma200_5[i5] if ma200_5[i5] else False

        upper, mid, lower = bollinger_bands(c5, 20, 2)
        band_width = (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)
        bb_signal = band_width <= 0.03 and c5[-1] > mid[-1]

        if below_200_context:
            ok, msg = detect_exhaustion_5m(o5, h5, l5, c5, v5)
            if ok and allowed(symbol, "EXAUSTAO_5M"):
                await tg(session, f"⭐ {symbol}\n{msg}")
                mark(symbol, "EXAUSTAO_5M")

        if (tendencia_iniciando_5m(ema9_5, ma20_5, ma50_5) or bb_signal) and allowed(symbol, "INI_5M"):
            if (abs(c5[i5] - ma200_5[i5]) / (ma200_5[i5] + 1e-12)) <= 0.05 and c5[i5] < ma200_5[i5]:
                p = fmt_price(c5[i5])
                msg = f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {now_br()}"
                await tg(session, msg)
                mark(symbol, "INI_5M")

        if preconf_5m_cross_3_over_200(ema9_5, ma20_5, ma50_5, ma200_5) and allowed(symbol, "PRE_5M"):
            p = fmt_price(c5[i5])
            msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session, msg)
            mark(symbol, "PRE_5M")

        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210: return
        c15 = [float(k[4]) for k in k15]
        ema9_15  = ema(c15, 9)
        ma20_15  = sma(c15, 20)
        ma50_15  = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        j = len(c15)-1

        if preconf_15m_ema9_over_200(ema9_15, ma200_15) and allowed(symbol, "PRE_15M"):
            p = fmt_price(c15[j])
            msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session, msg)
            mark(symbol, "PRE_15M")

        if conf_15m_all_over_200_recent(ema9_15, ma20_15, ma50_15, ma200_15) and allowed(symbol, "CONF_15M"):
            p = fmt_price(c15[j])
            msg = f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session, msg)
            mark(symbol, "CONF_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown 15m | {now_br()}")
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
