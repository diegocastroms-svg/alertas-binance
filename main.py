import time
import requests
import threading
import math
from datetime import datetime, timedelta
from flask import Flask
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TELEGRAM_TOKEN = "SEU_TOKEN_AQUI"
CHAT_ID = "SEU_CHAT_ID_AQUI"
BASE_URL = "https://api.binance.com"
COOLDOWN_TIME = 900  # 15 minutos
UPDATE_INTERVAL = 3600  # Atualizar lista Top 50 a cada 1h

cooldowns = defaultdict(dict)
top_pairs = []
last_update_time = 0

# ======== FUNÇÕES BASE ========= #

def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

def get_klines(symbol, interval="5m", limit=200):
    try:
        url = f"{BASE_URL}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = requests.get(url, params=params, timeout=10).json()
        return [[float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in data]
    except Exception:
        return []

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[-period]
    for val in values[-period + 1:]:
        ema_val = val * k + ema_val * (1 - k)
    return ema_val

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, -1):
        delta = values[i + 1] - values[i]
        if delta >= 0:
            gains.append(delta)
        else:
            losses.append(abs(delta))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_spot_pairs():
    try:
        info = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=10).json()
        symbols = [s["symbol"] for s in info["symbols"]
                   if s["quoteAsset"] == "USDT"
                   and s["status"] == "TRADING"
                   and not any(x in s["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR", "1000", "2X", "3X", "5L", "5S"])]
        return symbols
    except Exception:
        return []

def get_top_50_spot_pairs():
    """Busca todas as moedas SPOT/USDT e seleciona as 50 com maior volume 24h"""
    try:
        data = requests.get(f"{BASE_URL}/api/v3/ticker/24hr", timeout=10).json()
        spot_pairs = [s for s in data if s["symbol"].endswith("USDT")
                      and not any(x in s["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR", "1000", "2X", "3X", "5L", "5S"])]
        sorted_pairs = sorted(spot_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
        return [s["symbol"] for s in sorted_pairs[:50]]
    except Exception as e:
        print(f"Erro ao buscar Top 50: {e}")
        return []

def check_cooldown(symbol, alert_type):
    now = time.time()
    if alert_type in cooldowns[symbol]:
        if now - cooldowns[symbol][alert_type] < COOLDOWN_TIME:
            return True
    cooldowns[symbol][alert_type] = now
    return False

def format_message(symbol, title, motivo, price, rsi_value, vol_ratio, timeframe):
    hora_brasil = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    return (f"🟢 <b>{symbol} — {title}</b>\n"
            f"<b>Motivo:</b> {motivo}\n"
            f"RSI: {rsi_value:.1f} • Volume: {vol_ratio:+.0f}%\n"
            f"💰 <b>Preço atual:</b> {price}\n"
            f"🕒 <b>Horário:</b> {hora_brasil} 🇧🇷\n"
            f"🔗 <a href='binance://app/spot/trade?symbol={symbol}'>Ver no app Binance</a>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━")

# ======== ANÁLISE E ALERTAS ========= #

def analyze(symbol):
    try:
        klines_5m = get_klines(symbol, "5m")
        klines_15m = get_klines(symbol, "15m")
        if not klines_5m or not klines_15m:
            return

        closes_5m = [c[3] for c in klines_5m]
        volumes_5m = [c[4] for c in klines_5m]
        closes_15m = [c[3] for c in klines_15m]
        volumes_15m = [c[4] for c in klines_15m]

        price = closes_5m[-1]
        rsi_5m = rsi(closes_5m)
        rsi_15m = rsi(closes_15m)

        ema9_5m = ema(closes_5m, 9)
        ma20_5m = sma(closes_5m, 20)
        ma50_5m = sma(closes_5m, 50)
        ma200_5m = sma(closes_5m, 200)
        vol_avg_5m = sma(volumes_5m, 20)
        vol_ratio_5m = ((volumes_5m[-1] / vol_avg_5m) - 1) * 100 if vol_avg_5m else 0

        ema9_15m = ema(closes_15m, 9)
        ma20_15m = sma(closes_15m, 20)
        ma50_15m = sma(closes_15m, 50)
        ma200_15m = sma(closes_15m, 200)
        vol_avg_15m = sma(volumes_15m, 20)
        vol_ratio_15m = ((volumes_15m[-1] / vol_avg_15m) - 1) * 100 if vol_avg_15m else 0

        # ============ ALERTAS 5M ============= #
        if ema9_5m and ma20_5m and ma50_5m and ma200_5m:
            if ema9_5m > ma20_5m > ma50_5m and ema9_5m < ma200_5m:
                if not check_cooldown(symbol, "inicio_5m"):
                    motivo = "Queda → lateralização → EMA9 cruzou MA20 e MA50 (abaixo da MA200)"
                    send_message(format_message(symbol, "TENDÊNCIA INICIANDO (5m)", motivo, price, rsi_5m, vol_ratio_5m, "5m"))

            if ema9_5m > ma200_5m and ma20_5m > ma200_5m:
                if not check_cooldown(symbol, "cruzamento_5m"):
                    motivo = "EMA9 e MA20 cruzaram pra cima da MA200"
                    send_message(format_message(symbol, "CRUZAMENTO FORTE (5m)", motivo, price, rsi_5m, vol_ratio_5m, "5m"))

            if ema9_5m > ma20_5m > ma50_5m and ema9_5m > ma200_5m and vol_ratio_5m > 30 and rsi_5m > 55:
                if not check_cooldown(symbol, "confirmacao_5m"):
                    motivo = "Volume e RSI confirmam força após virada de tendência"
                    send_message(format_message(symbol, "⚡️ CONFIRMAÇÃO DE FORÇA (5m)", motivo, price, rsi_5m, vol_ratio_5m, "5m"))

        # ============ ALERTAS 15M ============= #
        if ema9_15m and ma20_15m and ma50_15m and ma200_15m:
            if ema9_15m > ma200_15m:
                if not check_cooldown(symbol, "preconf_15m"):
                    motivo = "EMA9 cruzou pra cima da MA200"
                    send_message(format_message(symbol, "PRÉ-CONFIRMAÇÃO DE ALTA (15m)", motivo, price, rsi_15m, vol_ratio_15m, "15m"))

            if ma20_15m > ma200_15m and ma50_15m > ma200_15m:
                if not check_cooldown(symbol, "confirmada_15m"):
                    motivo = "MA20 e MA50 cruzaram pra cima da MA200"
                    send_message(format_message(symbol, "TENDÊNCIA CONFIRMADA (15m)", motivo, price, rsi_15m, vol_ratio_15m, "15m"))

            if abs(closes_15m[-1] - ema9_15m) / ema9_15m < 0.006 or abs(closes_15m[-1] - ma20_15m) / ma20_15m < 0.006:
                if closes_15m[-1] > ema9_15m and rsi_15m > 55 and vol_ratio_15m > 0:
                    if not check_cooldown(symbol, "reteste_ok_15m"):
                        motivo = "Preço testou EMA9/MA20 e reverteu pra cima com confirmação dos indicadores"
                        send_message(format_message(symbol, "RETESTE CONFIRMADO (15m)", motivo, price, rsi_15m, vol_ratio_15m, "15m"))
                elif closes_15m[-1] < ema9_15m and rsi_15m < 50:
                    if not check_cooldown(symbol, "reteste_fraco_15m"):
                        motivo = "Preço testou EMA9/MA20 e perdeu força — possível queda"
                        send_message(format_message(symbol, "RETESTE FRACO (15m)", motivo, price, rsi_15m, vol_ratio_15m, "15m"))
    except Exception as e:
        print(f"Erro analisando {symbol}: {e}")

# ======== LOOP PRINCIPAL ========= #

def run_bot():
    global top_pairs, last_update_time
    send_message("✅ BOT ATIVO NO RENDER — v1_zero_ultima_chance_da_aurora 🇧🇷")

    with ThreadPoolExecutor(max_workers=50) as executor:
        while True:
            now = time.time()
            if now - last_update_time > UPDATE_INTERVAL or not top_pairs:
                top_pairs = get_top_50_spot_pairs()
                last_update_time = now
                send_message(f"🔄 Lista Top 50 atualizada ({len(top_pairs)} pares SPOT) 🇧🇷")

            executor.map(analyze, top_pairs)
            time.sleep(300)

@app.route('/')
def home():
    return "Bot SPOT USDT ativo — v1_zero_ultima_chance_da_aurora"

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=5000)
