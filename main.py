# main_breakout_v1_render_hibrido.py
# Híbrido (3m + 5m + 15m) com confirmação multi-tempo
# Breakout (entrada) nos 3m, 5m e 15m, dentro de 10% da MA200
# Apenas pares spot reais em USDT
# Cooldown 8 minutos
# Inclui stop loss e take profit
# Adaptação dinâmica à volatilidade (relaxada)
# Monitora as 50 moedas com maior volume

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

# ---------------- CONFIG PUMP 3M DINÂMICO (FAIXAS SEGURAS) ----------------
PUMP_RANGES = {
    "quiet_candles": (6, 12),           # Entre 6 e 12 candles de consolidação
    "max_range_pct": (0.8, 1.8),        # Range médio da consolidação (ex: 0.8% a 1.8%)
    "vol_multiplier": (10, 25),         # Volume entre 10x e 25x a média
    "min_rise_pct": (2.5, 6.0),         # Alta do candle entre 2.5% e 6%
    "rsi_min": (30, 50),               # RSI antes do pump (evita sobrecomprado)
    "stop_loss_pct": (0.95, 0.98),      # Stop entre -5% e -2%
    "take_profit_pct": (1.15, 1.35),    # TP entre +15% e +35%
    "cooldown_kind": "PUMP_3M"
}

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Scanner ativo (3m, 5m + 15m híbrido) — breakout perto MA200 | Pump 3m dinâmico | 50 maiores volumes", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " Brasil"

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
        "UP", "DOWN", "BULL", "BEAR",
        "BUSD", "FDUSD", "TUSD", "USDC", "USDP", "USD1", "USDE", "XUSD", "USDX", "GUSD", "BFUSD",
        "EUR", "EURS", "CEUR", "BRL", "TRY",
        "PERP", "_PERP", "STABLE", "TEST",
        "HIFI", "BAKE"
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

def calculate_volatility(prices):
    if len(prices) < 2:
        return 0.0
    mean_price = sum(prices) / len(prices)
    if mean_price == 0:
        return 0.0
    std_dev = statistics.pstdev(prices)
    return (std_dev / mean_price) * 100 * (60 / 15)

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m (Sinal Inicial) --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            o3 = [float(k[1]) for k in k3]
            ema9_3 = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            rsi3 = calc_rsi(c3, 14)
            vol_ma20_3 = sum(v3[-20:]) / 20.0
            volatility_3m = calculate_volatility(c3[-20:])
            i = len(ema9_3) - 1
            if len(ema9_3) > 2:
                ma200_tolerance = ma200_3[-1] * 0.10
                within_ma200 = abs(c3[-1] - ma200_3[-1]) <= ma200_tolerance
                cruza = ema9_3[i-1] <= ma200_3[i-1] and ema9_3[i] > ma200_3[i]
                encostar = abs(ema9_3[i] - ma200_3[i]) / (ma200_3[i] + 1e-12) <= 0.001
                rsi_min = 25 if volatility_3m > 2.0 else (35 if volatility_3m < 0.5 else 30)
                rsi_max = 70 if volatility_3m > 2.0 else (65 if volatility_3m < 0.5 else 60)
                vol_multiplier = 1.5 if volatility_3m > 2.0 else (1.2 if volatility_3m < 0.5 else 1.3)
                stop_loss_factor = 0.96 if volatility_3m > 2.0 else (0.98 if volatility_3m < 0.5 else 0.97)
                take_profit_factor = 1.15 if volatility_3m > 2.0 else (1.05 if volatility_3m < 0.5 else 1.10)
                if (cruza or encostar) and rsi_min <= rsi3[-1] <= rsi_max and v3[-1] >= vol_multiplier * (vol_ma20_3 + 1e-12) and within_ma200 and allowed(symbol, "SIG_3M"):
                    stop_loss = c3[i] * stop_loss_factor
                    take_profit = c3[i] * take_profit_factor
                    msg = (f"{symbol} Sinal Inicial (3m)\n"
                           f"Preço: {fmt_price(c3[i])}\n"
                           f"Stop Loss: {fmt_price(stop_loss)} ({(1-stop_loss_factor)*100:.0f}%)\n"
                           f"Take Profit: {fmt_price(take_profit)} (+{(take_profit_factor-1)*100:.0f}%)\n"
                           f"{now_br()} (UTC-3) | Volatilidade: {volatility_3m:.2f}%\n"
                           f"──────────────────────────────")
                    await tg(session, msg)
                    mark(symbol, "SIG_3M")

        # -------- 5m (Confirmação Intermediária) --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210: return
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        ema9_5  = ema(c5, 9)
        ma50_5  = sma(c5, 50)
        ma200_5 = sma(c5, 200)
        upper5, mid5, lower5 = bollinger_bands(c5, 20, 2)
        rsi5 = calc_rsi(c5, 14)
        vma20_5 = sum(v5[-20:]) / 20.0
        volatility_5m = calculate_volatility(c5[-20:])
        i5 = len(c5) - 1

        if len(c5) >= 26:
            exp1 = ema(c5, 12)
            exp2 = ema(c5, 26)
            dif = [exp1[i] - exp2[i] for i in range(len(exp1))]
            dea = ema(dif, 9)
            macd_line = dif[-1] - dea[-1] if len(dif) > 0 and len(dea) > 0 else 0
        else:
            macd_line = 0

        cross_up_9_50_5 = (ema9_5[i5-1] <= ma50_5[i5-1]) and (ema9_5[i5] > ma50_5[i5])
        bb_open_5 = widening_now(upper5, mid5, lower5)
        if len(ema9_5) > 2:
            ma200_tolerance = ma200_5[-1] * 0.10
            within_ma200 = abs(c5[-1] - ma200_5[-1]) <= ma200_tolerance
            rsi_min = 25 if volatility_5m > 2.0 else (35 if volatility_5m < 0.5 else 30)
            rsi_max = 70 if volatility_5m > 2.0 else (65 if volatility_5m < 0.5 else 60)
            vol_multiplier = 1.5 if volatility_5m > 2.0 else (1.2 if volatility_5m < 0.5 else 1.3)
            stop_loss_factor = 0.96 if volatility_5m > 2.0 else (0.98 if volatility_5m < 0.5 else 0.97)
            take_profit_factor = 1.15 if volatility_5m > 2.0 else (1.05 if volatility_5m < 0.5 else 1.10)
            if cross_up_9_50_5 and rsi_min <= rsi5[-1] <= rsi_max and v5[-1] >= vol_multiplier * (vma20_5 + 1e-12) and bb_open_5 and within_ma200 and allowed(symbol, "CONF_5M"):
                stop_loss = c5[i5] * stop_loss_factor
                take_profit = c5[i5] * take_profit_factor
                msg = (f"{symbol} Confirmação Intermediária (5m)\n"
                       f"Preço: {fmt_price(c5[i5])}\n"
                       f"Stop Loss: {fmt_price(stop_loss)} ({(1-stop_loss_factor)*100:.0f}%)\n"
                       f"Take Profit: {fmt_price(take_profit)} (+{(take_profit_factor-1)*100:.0f}%)\n"
                       f"{now_br()} (UTC-3) | Volatilidade: {volatility_5m:.2f}%\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_5M")

        # -------- GRADUAL PUMP 5M — ENTRADA ANTECIPADA (QUALQUER POSIÇÃO, TOLERA 2 CORREÇÕES) --------
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            v5 = [float(k[5]) for k in k5]
            o5 = [float(k[1]) for k in k5]

            # 1. Últimos 5 candles: média 0.5–1.5%, tolera até 2 vermelhos/dojis
            rises = [(c5[i] - o5[i]) / o5[i] * 100 for i in range(-5, 0)]
            avg_rise = sum(rises) / 5
            weak_count = sum(1 for r in rises if r <= 0.1)  # vermelho ou doji
            valid_trend = (0.5 <= avg_rise <= 1.5) and (weak_count <= 2)

            # 2. Volume crescendo suavemente (x3 a x20) — ANTES do FOMO
            vol_medio_20 = sum(v5[-20:-1]) / 19.0 if len(v5) >= 20 else sum(v5[:-1]) / (len(v5)-1)
            recent_vol_ratio = v5[-1] / (vol_medio_20 + 1e-12)
            volume_rising = 3 <= recent_vol_ratio <= 20

            # 3. Preço acima da EMA9 (confirma momentum)
            ema9_val = ema(c5, 9)[-1]
            above_ema9 = c5[-1] > ema9_val

            # 4. Preço subiu nos últimos 5 candles (fechamento > abertura 5 candles atrás)
            net_up = c5[-1] > c5[-6]

            # 5. SINAL FINAL
            if (valid_trend and volume_rising and above_ema9 and net_up 
                and allowed(symbol, "GRADUAL_PUMP_5M")):
                
                sl = c5[-1] * 0.955   # -4.5%
                tp = c5[-1] * 1.20    # +20%
                msg = (f"{symbol} GRADUAL PUMP (5m)\n"
                       f"Média: <b>+{avg_rise:.2f}%</b> (5 candles)\n"
                       f"Volume: <b>x{recent_vol_ratio:.1f}</b>\n"
                       f"Preço: <b>{fmt_price(c5[-1])}</b>\n"
                       f"SL: <code>{fmt_price(sl)}</code> (-4.5%)\n"
                       f"TP: <code>{fmt_price(tp)}</code> (+20%)\n"
                       f"{now_br()}\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "GRADUAL_PUMP_5M")

        # -------- ALERTA DE PUMP 3M — 100% DINÂMICO E SEGURO --------
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            o3 = [float(k[1]) for k in k3]
            h3 = [float(k[2]) for k in k3]
            l3 = [float(k[3]) for k in k3]

            cfg = PUMP_RANGES

            volatility_3m = calculate_volatility(c3[-30:])
            vol_factor = 1.0
            if volatility_3m > 3.0:      vol_factor = 0.7
            elif volatility_3m < 1.0:    vol_factor = 1.3

            quiet_min, quiet_max = [int(x * vol_factor) for x in cfg["quiet_candles"]]
            range_min, range_max = [x * vol_factor for x in cfg["max_range_pct"]]
            vol_min, vol_max = [x * (0.8 if volatility_3m > 2 else 1.2) for x in cfg["vol_multiplier"]]
            rise_min, rise_max = [x * (0.8 if volatility_3m > 2 else 1.2) for x in cfg["min_rise_pct"]]
            rsi_min_val, _ = cfg["rsi_min"]
            sl_min, sl_max = cfg["stop_loss_pct"]
            tp_min, tp_max = cfg["take_profit_pct"]

            quiet_candles = max(quiet_min, min(quiet_max, 10))
            range_list = [(h3[i] - l3[i]) / l3[i] * 100 for i in range(-quiet_candles, 0)]
            avg_range = sum(range_list) / len(range_list)
            in_accumulation = range_min <= avg_range <= range_max

            vol_medio = sum(v3[-20:]) / 20.0
            current_rise = (c3[-1] - o3[-1]) / o3[-1] * 100
            volume_ratio = v3[-1] / (vol_medio + 1e-12)
            volume_exploded = vol_min <= volume_ratio <= vol_max
            strong_rise = rise_min <= current_rise <= rise_max
            green_candle = c3[-1] > o3[-1]

            ema9 = ema(c3, 9)  # CORRIGIDO: era EMR
            rsi3 = calc_rsi(c3, 14)
            above_ema9 = len(ema9) > 0 and c3[-1] > ema9[-1]
            rsi_ok = len(rsi3) > 0 and rsi3[-1] >= rsi_min_val

            pump_detected = (
                in_accumulation and
                volume_exploded and
                strong_rise and
                green_candle and
                above_ema9 and
                rsi_ok and
                allowed(symbol, cfg["cooldown_kind"])
            )

            if pump_detected:
                sl_pct = (sl_min + sl_max) / 2
                tp_pct = (tp_min + tp_max) / 2
                stop_loss = c3[-1] * sl_pct
                take_profit = c3[-1] * tp_pct

                msg = (f"{symbol} PUMP DETECTADO (3m)\n"
                       f"Preço: {fmt_price(c3[-1])}\n"
                       f"Alta: +{current_rise:.1f}% | Vol: x{volume_ratio:.1f}\n"
                       f"Range: {avg_range:.2f}% (acumulação)\n"
                       f"SL: {fmt_price(stop_loss)} ({(1-sl_pct)*100:.1f}%)\n"
                       f"TP: {fmt_price(take_profit)} ({(tp_pct-1)*100:.0f}%)\n"
                       f"Volatilidade: {volatility_3m:.1f}% | {now_br()}\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, cfg["cooldown_kind"])

        # ==================================================
        # 15m SUBSTITUÍDO: FOGUETE + TARTARUGA (2 ALERTAS)
        # ==================================================
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210:
            return

        o15 = [float(k[1]) for k in k15]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]

        # --- Indicadores ---
        ema9_15   = ema(c15, 9)
        ema21_15  = ema(c15, 21)
        ma200_15  = sma(c15, 200)
        rsi15     = calc_rsi(c15, 14)
        ema12_15  = ema(c15, 12)
        ema26_15  = ema(c15, 26)

        # --- Resistência local: máxima dos últimos 5 candles ---
        resistencia_local = max(h15[-5:])

        # --- Volume médio dos últimos 10 candles ---
        vol_med_10 = sum(v15[-10:]) / 10.0 if len(v15) >= 10 else v15[-1]

        # --- Índice atual ---
        i = len(c15) - 1
        if i < 5:
            return

        # --- Condições comuns ---
        acima_ou_encostou_ema200 = (
            c15[i] > ma200_15[i] or 
            (l15[i] <= ma200_15[i] <= c15[i])
        )

        # =============================================
        # 1. ALERTA FOGUETE (rompimento explosivo)
        # =============================================
        rompimento_forte = c15[i] > resistencia_local
        volume_forte = v15[i] > vol_med_10 * 1.3

        # Sinais verdes (2 de 3)
        cruzou_9_21 = (len(ema9_15) > 1 and len(ema21_15) > 1 and
                       ema9_15[i-1] <= ema21_15[i-1] and ema9_15[i] > ema21_15[i])
        rsi_saindo_fundo = (len(rsi15) > 1 and rsi15[i-1] <= 40 and rsi15[i] > 40)
        macd_hist = ema12_15[i] - ema26_15[i] if i < len(ema12_15) and i < len(ema26_15) else 0
        macd_hist_ant = ema12_15[i-1] - ema26_15[i-1] if i > 0 else 0
        macd_vira_positivo = macd_hist > 0 and macd_hist_ant <= 0

        sinais_verdes = sum([cruzou_9_21, rsi_saindo_fundo, macd_vira_positivo])
        dois_sinais_verdes = sinais_verdes >= 2

        if (acima_ou_encostou_ema200 and rompimento_forte and 
            volume_forte and dois_sinais_verdes and 
            allowed(symbol, "FOGUETE_15M")):

            stop = min(l15[i], ma200_15[i])
            risco = c15[i] - stop
            alvo = c15[i] + 2 * risco

            msg = (f"<b>{symbol} FOGUETE LANÇADO! (15m)</b>\n"
                   f"Preço: <b>{fmt_price(c15[i])}</b>\n"
                   f"Stop: <code>{fmt_price(stop)}</code>\n"
                   f"Alvo 1:2 → <code>{fmt_price(alvo)}</code>\n"
                   f"Volume: <b>x{v15[i]/vol_med_10:.1f}</b> | Rompeu {fmt_price(resistencia_local)}\n"
                   f"{now_br()}\n"
                   f"──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "FOGUETE_15M")

        # =============================================
        # 2. ALERTA TARTARUGA (alta lenta e sustentável)
        # =============================================
        ultimos_5_fechamentos = c15[-5:]
        subindo_devagar = all(ultimos_5_fechamentos[k] > ultimos_5_fechamentos[k+1] for k in range(3))

        # Cruzou EMA200 nos últimos 5 candles
        cruzou_ema200_recent = any(c15[j] > ma200_15[j] for j in range(i-4, i+1))

        volume_crescente = v15[i] > vol_med_10 * 1.1
        rsi_moderado = 45 < rsi15[i] < 65

        if (cruzou_ema200_recent and subindo_devagar and 
            volume_crescente and rsi_moderado and 
            acima_ou_encostou_ema200 and 
            allowed(symbol, "TARTARUGA_15M")):

            stop = ma200_15[i]
            alvo = c15[i] + 3 * (c15[i] - stop)  # 1:3

            msg = (f"<b>{symbol} TARTARUGA SUBINDO! (15m)</b>\n"
                   f"Preço: <b>{fmt_price(c15[i])}</b>\n"
                   f"Alta lenta: 4 candles seguidos subindo\n"
                   f"Stop: <code>{fmt_price(stop)}</code> (EMA 200)\n"
                   f"Alvo 1:3 → <code>{fmt_price(alvo)}</code>\n"
                   f"Volume crescendo | RSI {rsi15[i]:.1f}\n"
                   f"{now_br()}\n"
                   f"──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "TARTARUGA_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"Scanner ativo | {len(symbols)} pares | Pump 3m dinâmico | {now_br()}\n──────────────────────────────")
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
