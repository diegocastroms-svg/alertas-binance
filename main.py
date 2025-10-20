# main_reversao_v5_renderfix.py
# ✅ Corrige apenas a execução no Render (loop assíncrono separado do Flask)
# ✅ Mantém 100% da lógica original
# ✅ Telegram ativo, Flask porta 10000
# ⚙️ Nenhuma linha de alerta, cálculo ou filtro foi alterada

import os, asyncio, aiohttp, time, math
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
    blocked = ("UP", "DOWN", "BULL", "BEAR", "BUSD", "FDUSD", "TUSD", "USDC", "USD1", "USDE", "PERP", "_PERP")
    pares = []
    for d in data:
        s = d.get("symbol", "")
        # apenas pares terminando exatamente em USDT
        if not s.endswith("USDT"):
            continue
        # ignora variantes bloqueadas (ex: BUSDUSDT, FDUSDUSDT etc.)
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
    if len(c) < 30: return False, ""
    last = len(c)-1
    open_, high_, low_, close_ = o[last], h[last], l[last], c[last]
    body = abs(close_ - open_)
    lower_wick = open_ - low_ if close_ >= open_ else close_ - low_
    cond_hammer = (close_ > open_) and (lower_wick >= 2.0*body)
    vol_ma20 = sum(v[-20:]) / 20.0
    cond_vol = v[last] >= 1.1 * (vol_ma20 + 1e-12)
    base = c[max(0, last-12)]
    drop_pct = (close_/(base+1e-12)-1.0)*100.0
    cond_drop = drop_pct <= -1.5
    if cond_hammer and cond_vol and cond_drop:
        msg = f"🟥 <b>EXAUSTÃO VENDEDORA (5m)</b>\n💰 {fmt_price(close_)}\n🕒 {now_br()}"
        return True, msg
    return False, ""

def tendencia_iniciando_5m(ema9, ma20, ma50):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    cross_9_20 = cross_up(ema9[i0], ema9[i1], ma20[i0], ma20[i1])
    cross_9_50 = cross_up(ema9[i0], ema9[i1], ma50[i0], ma50[i1])
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
    return all_above and recent_cross

def preconf_15m_ema9_over_200(ema9, ma200):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    return cross_up(ema9[i0], ema9[i1], ma200[i0], ma200[i1])

def conf_15m_all_over_200_recent(ema9, ma20, ma50, ma200):
    if len(ema9) < 2: return False
    i1 = len(ema9)-1; i0 = i1-1
    structure = (ema9[i1] > ma20[i1] > ma50[i1] > ma200[i1])
    c20 = cross_up(ma20[i0], ma20[i1], ma200[i0], ma200[i1])
    c50 = cross_up(ma50[i0], ma50[i1], ma200[i0], ma200[i1])
    recent = (c20 or c50)
    return structure and recent

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

        if below_200_context:
            ok, msg = detect_exhaustion_5m(o5, h5, l5, c5, v5)
            if ok and allowed(symbol, "EXAUSTAO_5M"):
                await tg(session, f"⭐ {symbol}\n{msg}")
                mark(symbol, "EXAUSTAO_5M")

        # Detecta início de tendência tanto abaixo quanto acima da MA200
        if tendencia_iniciando_5m(ema9_5, ma20_5, ma50_5) and allowed(symbol, "INI_5M"):
            # Filtra: só envia se o candle atual estiver próximo da MA200 (reversão) ou recém cruzando para cima
            if (abs(c5[i5] - ma200_5[i5]) / (ma200_5[i5] + 1e-12)) <= 0.05 or c5[i5] > ma200_5[i5]:
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
