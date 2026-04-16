import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V11 - ZONA DE PRESSÃO (EMA200 + Leque + BB + SAR + MACD 8-17-9)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 5_000_000
TOP_N = 180
SCAN_INTERVAL = 30
COOLDOWN_SECONDS = 3600  # 1 horas

cooldown = {}

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

def now_ts():
    return int(time.time())

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

def can_alert(sym):
    t = cooldown.get(sym, 0)
    if now_ts() - t >= COOLDOWN_SECONDS:
        cooldown[sym] = now_ts()
        return True
    return False

async def get_oi(session, symbol):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/openInterest?symbol={symbol}") as r:
            data = await r.json()
            return float(data["openInterest"])
    except:
        return 0

# ====================== INDICADORES ======================
def ema(data, period):
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []
    k = 2 / (period + 1)
    ema_vals = [sum(data[:period]) / period]
    for price in data[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals

def bollinger_bands(closes, period=20, std=2):
    if len(closes) < period:
        return [], []
    sma = []
    for i in range(len(closes)):
        if i < period - 1:
            sma.append(sum(closes[:i+1]) / (i + 1))
        else:
            sma.append(sum(closes[i - period + 1:i + 1]) / period)
    bb_up, bb_down = [], []
    for i in range(len(sma)):
        if i < period - 1:
            bb_up.append(0)
            bb_down.append(0)
            continue
        window = closes[i - period + 1:i + 1]
        std_dev = (sum((x - sma[i]) ** 2 for x in window) / period) ** 0.5
        bb_up.append(sma[i] + std_dev * std)
        bb_down.append(sma[i] - std_dev * std)
    return bb_up, bb_down

def parabolic_sar(highs, lows, af=0.03, max_af=0.3):
    n = len(highs)
    if n < 2:
        return [0] * n
    sar = [0.0] * n
    trend = 1
    af_current = af
    ep = highs[0]
    sar[0] = lows[0]
    for i in range(1, n):
        prev_sar = sar[i-1]
        if trend == 1:
            sar[i] = prev_sar + af_current * (ep - prev_sar)
            if sar[i] > lows[i]:
                trend = -1
                sar[i] = ep
                ep = lows[i]
                af_current = af
        else:
            sar[i] = prev_sar - af_current * (prev_sar - ep)
            if sar[i] < highs[i]:
                trend = 1
                sar[i] = ep
                ep = highs[i]
                af_current = af
        if trend == 1:
            if highs[i] > ep:
                ep = highs[i]
                af_current = min(af_current + af, max_af)
            sar[i] = min(sar[i], lows[i-1], lows[i])
        else:
            if lows[i] < ep:
                ep = lows[i]
                af_current = min(af_current + af, max_af)
            sar[i] = max(sar[i], highs[i-1], highs[i])
    return sar

def calculate_macd(closes, fast=8, slow=17, signal=9):  # ← ALTERADO CONFORME SEU PEDIDO
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return histogram

# ====================== SCAN ======================
async def scan(session, sym):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=15m&limit=1000") as r:
            k = await r.json()

        if len(k) < 200:
            return

        highs = [float(x[2]) for x in k]
        lows  = [float(x[3]) for x in k]
        closes = [float(x[4]) for x in k]
        price = closes[-1]

        # Indicadores
        ema9  = ema(closes, 9)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200)
        bb_up, bb_down = bollinger_bands(closes)
        sar = parabolic_sar(highs, lows)
        macd_hist = calculate_macd(closes)   # agora 8-17-9

        oi_now = await get_oi(session, sym)

        # ====================== GATILHO V11 - AJUSTADO (SEM MODA) ======================

        # 1. RADAR DE PROXIMIDADE (EMA 200)
        # Não filtra se está acima ou abaixo, apenas se está perto (até 1,5%)
        distancia_200 = abs(price - ema200[-1]) / ema200[-1]
        na_zona_200 = distancia_200 <= 0.015 

        # 2. FILTRO DE TENDÊNCIA (EMA 50)
        # A 50 manda na direção. As médias curtas (9 e 20) acompanham.
        tendencia_long = (ema9[-1] > ema50[-1] and ema20[-1] > ema50[-1])
        tendencia_short = (ema9[-1] < ema50[-1] and ema20[-1] < ema50[-1])

        # 3. O MOMENTO DO PREÇO (EMA 9)
        # O preço tem que estar do lado certo da média mais rápida
        preco_ta_acima_9 = price > ema9[-1]
        preco_ta_abaixo_9 = price < ema9[-1]

        # 4. A EXPLOSÃO (Bandas de Bollinger abrindo)
        bb_expandindo = (bb_up[-1] > bb_up[-2] and bb_down[-1] < bb_down[-2])

        # 5. FILTROS DIRECIONAIS (MACD e SAR)
        macd_ok_long  = macd_hist[-1] >= -0.000001
        sar_ok_long   = sar[-1] <= price * 1.001

        macd_ok_short = macd_hist[-1] <= 0.000001
        sar_ok_short  = sar[-1] >= price * 0.999

        # ====================== MONTAGEM DO SETUP FINAL ======================

        # Só dispara se TUDO estiver de acordo:
        setup_long = (
            na_zona_200 and 
            tendencia_long and 
            preco_ta_acima_9 and 
            bb_expandindo and 
            macd_ok_long and 
            sar_ok_long
        )

        setup_short = (
            na_zona_200 and 
            tendencia_short and 
            preco_ta_abaixo_9 and 
            bb_expandindo and 
            macd_ok_short and 
            sar_ok_short
        )

        # ====================== GATILHO ======================
        if setup_long and can_alert(sym):
            tipo = "ROMPIMENTO" if price > ema200[-1] else "PULLBACK"
            dist = (abs(price - ema200[-1]) / ema200[-1]) * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"🚀 <b>ALERTA BINANCE LONG</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.5f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

        if setup_short and can_alert(sym):
            tipo = "ROMPIMENTO" if price < ema200[-1] else "PULLBACK"
            dist = (abs(price - ema200[-1]) / ema200[-1]) * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"📉 <b>ALERTA BINANCE SHORT</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.5f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

    except Exception as e:
        print(f"Erro em {sym}:", e)

# ====================== MAIN ======================
async def main():
    async with aiohttp.ClientSession() as session:
        await tg(session, "<b>V11 ATIVA - MACD 8-17-9</b>\nEMA200 + Leque + BB + SAR + MACD")
        while True:
            try:
                async with session.get(f"{BINANCE}/fapi/v1/ticker/24hr") as r:
                    data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0)) >= MIN_VOL24
                ][:TOP_N]

                await asyncio.gather(*[scan(session, s) for s in symbols])

            except Exception as e:
                print("Erro principal:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
asyncio.run(main())
