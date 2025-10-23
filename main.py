# main_reversao_v5_3_renderfix_3m_cruzamento_up_rsi.py
# ✅ Mantém 100% do código original
# ✅ Adiciona RSI ao gatilho Bollinger para confirmar força (RSI > 50)
# ✅ Exaustão vendedora permanece idêntica à versão funcional anterior

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 5 * 60
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
    if len(c) < 200:
        return False, ""

    last = len(c) - 1
    base = c[max(0, last - 15)]
    drop_pct = (c[last] / (base + 1e-12) - 1.0) * 100.0
    cond_queda = drop_pct <= -2.5  # cripto: mais sensível

    recent = c[-5:]
    var_pct = (max(recent) - min(recent)) / (sum(recent)/len(recent) + 1e-12)
    cond_lateral = var_pct <= 0.04  # permitir consolidação cripto

    vol_ma20 = sum(v[-20:]) / 20.0
    cond_vol = v[-1] >= 1.0 * (vol_ma20 + 1e-12)  # precisa de entrada de volume

    rsi = calc_rsi(c, 14)
    cond_rsi = rsi[-1] < 38  # sobrevendido real

    ema9_vals = ema(c, 9)
    cond_pos = c[-1] <= ema9_vals[-1] and c[-1] <= min(c[-10:])

    ma200_vals = sma(c, 200)
    cond_dist = (ma200_vals[-1] - c[-1]) / (ma200_vals[-1] + 1e-12) >= 0.02  # não precisa estar tão longe

    # Bollinger: estreita + toque/ultrapasse banda inferior
    upper_x, mid_x, lower_x = bollinger_bands(c, 20, 2)
    if len(upper_x) >= 2:
        bw_now = (upper_x[-1] - lower_x[-1]) / (mid_x[-1] + 1e-12)
        bw_prev = (upper_x[-2] - lower_x[-2]) / (mid_x[-2] + 1e-12)
    else:
        bw_now = bw_prev = 0.0
    cond_bb_narrow = bw_now <= 0.05
    cond_toque_lower = l[-1] <= lower_x[-1] if lower_x else False

    # Pavio comprador (candle de rejeição)
    corpo = abs(c[-1] - o[-1])
    amplitude = h[-1] - l[-1] + 1e-12
    corpo_ok = c[-1] > o[-1] and (corpo >= 0.3 * amplitude)
    pavio_compra = (h[-1] - c[-1]) < (c[-1] - l[-1])

    if (cond_queda and cond_lateral and cond_vol and cond_rsi and cond_pos and cond_dist
        and cond_bb_narrow and cond_toque_lower and corpo_ok and pavio_compra):
        msg = f"🟣 <b>EXAUSTÃO / ACUMULAÇÃO (5m)</b>\n💰 {fmt_price(c[last])}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
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

def conf_15m_all_over_200_recent(ema9, ma20, ma50, ma200):
    if len(ema9) < 3: return False
    i1 = len(ema9) - 1
    i0 = i1 - 1
    cruzou_9_200 = cross_up(ema9[i0], ema9[i1], ma200[i0], ma200[i1])
    if cruzou_9_200 and abs(ema9[i1] - ma200[i1]) / (ma200[i1] + 1e-12) <= 0.05:
        return True
    return False

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # 3m — EMA9 cruza MA200 de baixo pra cima  ➜  AJUSTE: encostar (±0,1%) OU cruzar subindo
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            ema9_3 = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            if len(ema9_3) > 2:
                i = len(ema9_3) - 1
                # ✅ Dispara SOMENTE no cruzamento real de baixo pra cima (EMA9 cruza MA200)
                cruza = ema9_3[i-1] < ma200_3[i-1] and ema9_3[i] >= ma200_3[i]
                # 🔧 Ajuste cripto: considerar "toque" (±0,1%), força mínima e volume
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
        rsi = calc_rsi(c5, 14)
        # 🔧 BB cripto: abrindo e acima da média, RSI > 50
        band_width_prev = (upper[-2] - lower[-2]) / (mid[-2] + 1e-12) if len(upper) >= 2 else band_width
        bb_signal = (band_width <= 0.04) and (band_width > band_width_prev) and (c5[-1] > mid[-1]) and (rsi[-1] >= 50)

        if below_200_context:
            ok, msg = detect_exhaustion_5m(o5, h5, l5, c5, v5)
            if ok and allowed(symbol, "EXAUSTAO_5M"):
                await tg(session, f"⭐ {symbol}\n{msg}")
                mark(symbol, "EXAUSTAO_5M")

        # AJUSTE: “Iniciando (5m)” quando EMA9 cruza pra cima a MA20/MA50 abaixo da MA200 e BB confirma força
        if allowed(symbol, "INI_5M"):
            cross_9_20 = ema9_5[i5-1] <= ma20_5[i5-1] and ema9_5[i5] > ma20_5[i5]
            cross_9_50 = ema9_5[i5-1] <= ma50_5[i5-1] and ema9_5[i5] > ma50_5[i5]
            below_200 = ma20_5[i5] < ma200_5[i5] and ma50_5[i5] < ma200_5[i5]
            rsi = calc_rsi(c5, 14)
            # 🔧 Calibração cripto: RSI mais elástico
            rsi_ok = 45 <= rsi[-1] <= 70
            # 🔧 Requer volume alto para evitar falsos
            vol_ma20_5_ini = sum(v5[-20:]) / 20.0
            vol_ok_ini = v5[-1] >= 1.3 * (vol_ma20_5_ini + 1e-12)
            if (cross_9_20 or cross_9_50) and below_200 and bb_signal and rsi_ok and vol_ok_ini:
                p = fmt_price(c5[i5])
                msg = f"🔵 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "INI_5M")

        if preconf_5m_cross_3_over_200(ema9_5, ma20_5, ma50_5, ma200_5) and allowed(symbol, "PRE_5M"):
            p = fmt_price(c5[i5])
            msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
            await tg(session, msg)
            mark(symbol, "PRE_5M")

        # 5m Confirmada (cripto): 9 cruza 20/50 + RSI/volume + médias subindo
        if allowed(symbol, "CONF_5M"):
            rsi_5 = calc_rsi(c5, 14)
            vol_ma20_5_conf = sum(v5[-20:]) / 20.0
            cross_9_20_c = ema9_5[i5-1] <= ma20_5[i5-1] and ema9_5[i5] > ma20_5[i5]
            cross_9_50_c = ema9_5[i5-1] <= ma50_5[i5-1] and ema9_5[i5] > ma50_5[i5]
            rsi_ok_c = rsi_5[-1] > 52
            vol_ok_c = v5[-1] >= 1.2 * (vol_ma20_5_conf + 1e-12)
            ma20_up = ma20_5[i5] > ma20_5[i5-1]
            ma50_up = ma50_5[i5] > ma50_5[i5-1]
            ma200_ok = ma200_5[i5] >= ma200_5[i5-3] if i5 >= 3 else True
            if (cross_9_20_c or cross_9_50_c) and rsi_ok_c and vol_ok_c and ma20_up and ma50_up and ma200_ok:
                p = fmt_price(c5[i5])
                msg = f"🟠 {symbol} ⬆️ Tendência confirmada (5m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "CONF_5M")

        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210: return
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]
        ema9_15  = ema(c15, 9)
        ma20_15  = sma(c15, 20)
        ma50_15  = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        upper_15, mid_15, lower_15 = bollinger_bands(c15, 20, 2)
        rsi_15 = calc_rsi(c15, 14)
        j = len(c15)-1

        # Pré-confirmada (15m): EMA9 cruzou MA200 E (MA20>MA50) E RSI>55 E Volume >= 1.2x média20
        if preconf_15m_ema9_over_200(ema9_15, ma200_15) and allowed(symbol, "PRE_15M"):
            vol_ma20_15 = sum(v15[-20:]) / 20.0
            cond_alinhadas = ma20_15[j] > ma50_15[j]
            cond_rsi = rsi_15[-1] > 55
            cond_vol = v15[-1] >= 1.2 * (vol_ma20_15 + 1e-12)
            # BB abrindo ajuda a filtrar lateral
            bw_now_15 = (upper_15[-1] - lower_15[-1]) / (mid_15[-1] + 1e-12)
            bw_prev_15 = (upper_15[-2] - lower_15[-2]) / (mid_15[-2] + 1e-12) if len(upper_15) >= 2 else bw_now_15
            cond_bb_open = bw_now_15 > bw_prev_15
            if cond_alinhadas and cond_rsi and cond_vol and cond_bb_open:
                p = fmt_price(c15[j])
                msg = f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "PRE_15M")

        # Confirmada (15m): 9,20,50 > 200 E Bollinger abrindo pra cima E RSI>60
        if conf_15m_all_over_200_recent(ema9_15, ma20_15, ma50_15, ma200_15) and allowed(symbol, "CONF_15M"):
            todas_acima = (ema9_15[j] > ma200_15[j]) and (ma20_15[j] > ma200_15[j]) and (ma50_15[j] > ma200_15[j])
            band_width_now_15 = (upper_15[-1] - lower_15[-1]) / (mid_15[-1] + 1e-12)
            band_width_prev_15 = (upper_15[-2] - lower_15[-2]) / (mid_15[-2] + 1e-12) if len(upper_15) >= 2 else band_width_now_15
            bb_abrindo_15 = band_width_now_15 > band_width_prev_15
            rsi_ok_15 = rsi_15[-1] > 60
            if todas_acima and bb_abrindo_15 and rsi_ok_15:
                p = fmt_price(c15[j])
                msg = f"🔴 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {now_br()} (UTC-3)\n──────────────────────────────"
                await tg(session, msg)
                mark(symbol, "CONF_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown 5m | {now_br()} (UTC-3)\n──────────────────────────────")
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


