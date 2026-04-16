import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
import pandas as pd
import numpy as np

app = Flask(__name__)
@app.route("/")
def home():
    return "V10 - ZONA DE PRESSÃO (EMA200 + Leque + BB)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 10_000_000
TOP_N = 180
SCAN_INTERVAL = 30

COOLDOWN_SECONDS = 14400  # 4 horas

cooldown = {}
alert_state = {}

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

def now_ts():
    return int(time.time())

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg); return
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

# Função auxiliar para calcular EMA
def ema_series(series, period):
    return series.ewm(span=period, adjust=False).mean()

# Função auxiliar para Bandas de Bollinger
def bollinger_bands(series, period=20, std=2):
    sma = series.rolling(window=period).mean()
    std_dev = series.rolling(window=period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return upper, lower

async def scan(session, sym):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=15m&limit=150") as r:
            k = await r.json()

        if len(k) < 100:
            return

        df = pd.DataFrame(k, columns=['open_time', 'open', 'high', 'low', 'close', 'volume',
                                      'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                                      'taker_buy_quote', 'ignore'])
        
        df['close'] = df['close'].astype(float)
        df['high']  = df['high'].astype(float)
        df['low']   = df['low'].astype(float)

        # Cálculo das médias e indicadores
        df['ema9']  = ema_series(df['close'], 9)
        df['ema20'] = ema_series(df['close'], 20)
        df['ema50'] = ema_series(df['close'], 50)
        df['ema200'] = ema_series(df['close'], 200)

        df['bollinger_up'], df['bollinger_down'] = bollinger_bands(df['close'])

        price = df['close'].iloc[-1]
        oi_now = await get_oi(session, sym)

        # === CONFIGURAÇÃO DE ZONA DE PRESSÃO ===
        margem = 0.015  # 1.5%

        distancia_percentual = abs(df['close'] - df['ema200']) / df['ema200']
        na_zona_200 = distancia_percentual <= margem

        # 1. Estado de Tendência (Leque de médias)
        long_alinhado  = (df['ema9'] > df['ema20']) & (df['ema20'] > df['ema50'])
        short_alinhado = (df['ema9'] < df['ema20']) & (df['ema20'] < df['ema50'])

        # 2. Volatilidade (Bandas abrindo)
        bb_expandindo = (df['bollinger_up'] > df['bollinger_up'].shift(1)) & \
                        (df['bollinger_down'] < df['bollinger_down'].shift(1))

        # === GATILHOS ===
        setup_long  = long_alinhado & na_zona_200 & bb_expandindo
        setup_short = short_alinhado & na_zona_200 & bb_expandindo

        # LONG
        if setup_long.iloc[-1] and not setup_long.iloc[-2] and can_alert(sym):
            tipo = "ROMPIMENTO" if df['close'].iloc[-1] > df['ema200'].iloc[-1] else "PULLBACK"
            dist = distancia_percentual.iloc[-1] * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"🚀 <b>ALERTAS BINANCE LONG</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.4f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

        # SHORT
        if setup_short.iloc[-1] and not setup_short.iloc[-2] and can_alert(sym):
            tipo = "ROMPIMENTO" if df['close'].iloc[-1] < df['ema200'].iloc[-1] else "PULLBACK"
            dist = distancia_percentual.iloc[-1] * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"📉 <b>ALERTAS BINANCE SHORT</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.4f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

    except Exception as e:
        print(f"Erro em {sym}:", e)

async def main():
    async with aiohttp.ClientSession() as session:
        await tg(session, "<b>V10 - ZONA DE PRESSÃO ATIVA</b>\nEMA200 + Leque de Médias + BB Abrindo")
        while True:
            try:
                async with session.get(f"{BINANCE}/fapi/v1/ticker/24hr") as r:
                    data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0)) >= MIN_VOL24
                ]

                symbols = symbols[:TOP_N]

                await asyncio.gather(*[scan(session, s) for s in symbols])

            except Exception as e:
                print("Erro principal:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
asyncio.run(main())
