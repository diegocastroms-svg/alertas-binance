import os
import time
import asyncio
import aiohttp
import requests
from dotenv import load_dotenv
from flask import Flask, request

# Inicializa o Flask
app = Flask(__name__)

# Carrega variáveis de ambiente
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = os.getenv("SYMBOL", "WALUSDT")  # Exemplo: WALUSDT, configure no .env
INTERVAL_5M = "5m"
INTERVAL_15M = "15m"
LIMIT = 50  # Candles suficientes para cálculos manuais
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Estados para rastrear fases
current_phase = None

def calculate_sma(prices, period):
    """Calcula a média móvel simples manualmente."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def calculate_rsi(prices, period=14):
    """Calcula RSI aproximado manualmente (simplificado)."""
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(0, x) for x in changes[-period:]]
    losses = [-min(0, x) for x in changes[-period:]]
    avg_gain = sum(gains) / period if sum(gains) > 0 else 0.0001
    avg_loss = sum(losses) / period if sum(losses) > 0 else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def fetch_klines(interval):
    """Busca dados OHLCV da Binance."""
    params = {"symbol": SYMBOL, "interval": interval, "limit": LIMIT}
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_KLINES_URL, params=params) as response:
            data = await response.json()
            if isinstance(data, list):
                closes = [float(candle[4]) for candle in data]  # Coluna 4 é o close
                highs = [float(candle[2]) for candle in data]   # Coluna 2 é o high
                lows = [float(candle[3]) for candle in data]    # Coluna 3 é o low
                volumes = [float(candle[5]) for candle in data] # Coluna 5 é o volume
                return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
            return None

def detect_phase(data_5m, data_15m):
    """Detecta a fase atual com base em cálculos manuais."""
    last_5m_close = data_5m["closes"][-1]
    prev_5m_close = data_5m["closes"][-2]
    last_5m_high = data_5m["highs"][-1]
    last_5m_low = data_5m["lows"][-1]
    last_5m_volume = data_5m["volumes"][-1]
    sma_5m_21 = calculate_sma(data_5m["closes"], 21)
    rsi_5m = calculate_rsi(data_5m["closes"])

    last_15m_close = data_15m["closes"][-1]
    prev_15m_close = data_15m["closes"][-2]
    sma_15m_21 = calculate_sma(data_15m["closes"], 21)
    rsi_15m = calculate_rsi(data_15m["closes"])

    # Queda: Preço < SMA21, RSI < 40
    if sma_5m_21 and last_5m_close < sma_5m_21 and rsi_5m and rsi_5m < 40:
        return "queda"

    # Lateralização: Range pequeno (<1% do close), RSI 40-60, volume baixo
    range_perc = ((last_5m_high - last_5m_low) / last_5m_close) * 100
    if range_perc < 1 and rsi_5m and 40 <= rsi_5m <= 60 and last_5m_volume < calculate_sma(data_5m["volumes"], 21):
        return "lateralizacao"

    # Exaustão vendedora: RSI < 30, close mais baixo mas RSI subindo
    if rsi_5m and rsi_5m < 30 and last_5m_close < prev_5m_close and (not prev_5m_close or rsi_5m > calculate_rsi(data_5m["closes"][:-1])):
        return "exaustao_vendedora"

    # Tendência iniciando no 5m: SMA9 > SMA21 crossover, RSI > 50, volume up
    sma_5m_9 = calculate_sma(data_5m["closes"], 9)
    prev_sma_5m_9 = calculate_sma(data_5m["closes"][:-1], 9)
    if sma_5m_9 and sma_5m_21 and prev_sma_5m_9 < sma_5m_21 and sma_5m_9 > sma_5m_21 and rsi_5m > 50 and last_5m_volume > calculate_sma(data_5m["volumes"][:-1], 21):
        return "tendencia_iniciando_5m"

    # Pré-confirmada no 5m: Close > High anterior por 3 candles
    if last_5m_close > max(data_5m["highs"][-3:-1]) and rsi_5m > 55:
        return "tendencia_pre_confirmada_5m"

    # Pré-confirmada no 15m: SMA9 > SMA21 crossover, RSI > 50
    sma_15m_9 = calculate_sma(data_15m["closes"], 9)
    prev_sma_15m_9 = calculate_sma(data_15m["closes"][:-1], 9)
    if sma_15m_9 and sma_15m_21 and prev_sma_15m_9 < sma_15m_21 and sma_15m_9 > sma_15m_21 and rsi_15m > 50:
        return "tendencia_pre_confirmada_15m"

    # Confirmada no 15m: SMA9 > SMA21, RSI > 60, volume alto
    if sma_15m_9 and sma_15m_21 and sma_15m_9 > sma_15m_21 and rsi_15m > 60 and last_15m_volume > 1.5 * calculate_sma(data_15m["volumes"], 21):
        return "tendencia_confirmada_15m"

    return None

def send_alert(phase):
    """Envia alerta via Telegram."""
    message = f"Alerta para {SYMBOL}: Fase detectada - {phase.upper()}. Preço atual: {data_5m['closes'][-1]}."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, json=payload)

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Rota para receber webhooks e processar alertas."""
    global current_phase
    data_5m = await fetch_klines(INTERVAL_5M)
    data_15m = await fetch_klines(INTERVAL_15M)
    if data_5m and data_15m:
        new_phase = detect_phase(data_5m, data_15m)
        if new_phase and new_phase != current_phase:
            current_phase = new_phase
            send_alert(new_phase)
            return {"status": "success", "phase": new_phase}, 200
        return {"status": "no_change", "price": data_5m["closes"][-1]}, 200
    return {"status": "error", "message": "Dados não disponíveis"}, 500

async def monitor():
    """Loop principal para monitoramento contínuo (worker)."""
    global current_phase
    while True:
        data_5m = await fetch_klines(INTERVAL_5M)
        data_15m = await fetch_klines(INTERVAL_15M)
        if data_5m and data_15m:
            new_phase = detect_phase(data_5m, data_15m)
            if new_phase and new_phase != current_phase:
                current_phase = new_phase
                send_alert(new_phase)
                print(f"Alerta enviado: {new_phase}")
        else:
            print("Erro ao buscar dados.")
        await asyncio.sleep(60)  # Checa a cada 1 minuto

if __name__ == "__main__":
    # Inicia o Flask na porta 1000 e o loop de monitoramento em paralelo
    import threading
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=1000, debug=False), daemon=True).start()
    asyncio.run(monitor())
