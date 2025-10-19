import os
import time
import asyncio
import aiohttp
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# Inicializa o Flask
app = Flask(__name__)

# Carrega variáveis de ambiente
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
INTERVAL_5M = "5m"
INTERVAL_15M = "15m"
LIMIT = 50
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_TICKER_24HR_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MIN_VOLUME = 1000000

# Estados para rastrear fases por símbolo
current_phases = {}

async def get_usdt_spot_symbols(min_volume=MIN_VOLUME):
    """Obtém dinamicamente a lista de símbolos spot USDT com volume alto."""
    symbols = []
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_EXCHANGE_INFO_URL) as response:
            data = await response.json()
            spot_usdt_symbols = [
                s['symbol'] for s in data.get('symbols', []) 
                if s['symbol'].endswith('USDT') and s['status'] == 'TRADING' and 'SPOT' in s['permissions']
            ]
        
        async with session.get(BINANCE_TICKER_24HR_URL) as response:
            data = await response.json()
            filtered = [
                d['symbol'] for d in data 
                if d['symbol'] in spot_usdt_symbols and float(d['quoteVolume']) > min_volume
            ]
            sorted_symbols = sorted(
                filtered, 
                key=lambda sym: next((float(d['quoteVolume']) for d in data if d['symbol'] == sym), 0), 
                reverse=True
            )
            symbols = sorted_symbols[:50]  # Limita a 50 para evitar sobrecarga
            print(f"Carregados {len(symbols)} símbolos USDT spot com volume alto.")
            return symbols

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
    losses = [abs(min(0, x)) for x in changes[-period:]]  # Corrigido: abs para losses
    avg_gain = sum(gains) / period if sum(gains) > 0 else 0.0001
    avg_loss = sum(losses) / period if sum(losses) > 0 else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def fetch_klines(symbol, interval):
    """Busca dados OHLCV da Binance para um símbolo específico."""
    params = {"symbol": symbol, "interval": interval, "limit": LIMIT}
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_KLINES_URL, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, list):
                    closes = [float(candle[4]) for candle in data]
                    highs = [float(candle[2]) for candle in data]
                    lows = [float(candle[3]) for candle in data]
                    volumes = [float(candle[5]) for candle in data]
                    return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
            print(f"Erro ao buscar klines para {symbol}: {response.status}")
            return None

def detect_phase(data_5m, data_15m):
    """Detecta a fase atual com base em cálculos manuais."""
    if not data_5m or not data_15m:
        return None
    last_5m_close = data_5m["closes"][-1]
    prev_5m_close = data_5m["closes"][-2] if len(data_5m["closes"]) > 1 else last_5m_close
    last_5m_high = data_5m["highs"][-1]
    last_5m_low = data_5m["lows"][-1]
    last_5m_volume = data_5m["volumes"][-1]
    sma_5m_21 = calculate_sma(data_5m["closes"], 21)
    rsi_5m = calculate_rsi(data_5m["closes"])

    last_15m_close = data_15m["closes"][-1]
    prev_15m_close = data_15m["closes"][-2] if len(data_15m["closes"]) > 1 else last_15m_close
    last_15m_volume = data_15m["volumes"][-1]  # Corrigido: definido
    sma_15m_21 = calculate_sma(data_15m["closes"], 21)
    rsi_15m = calculate_rsi(data_15m["closes"])

    # Queda: Preço < SMA21, RSI < 40
    if sma_5m_21 and last_5m_close < sma_5m_21 and rsi_5m and rsi_5m < 40:
        return "queda"

    # Lateralização: Range pequeno (<1% do close), RSI 40-60, volume baixo
    range_perc = ((last_5m_high - last_5m_low) / last_5m_close) * 100
    sma_vol_5m = calculate_sma(data_5m["volumes"], 21)
    if range_perc < 1 and rsi_5m and 40 <= rsi_5m <= 60 and sma_vol_5m and last_5m_volume < sma_vol_5m:
        return "lateralizacao"

    # Exaustão vendedora: RSI < 30, close mais baixo mas RSI subindo
    prev_rsi_5m = calculate_rsi(data_5m["closes"][:-1]) if len(data_5m["closes"]) > 1 else 50
    if rsi_5m and rsi_5m < 30 and last_5m_close < prev_5m_close and rsi_5m > prev_rsi_5m:
        return "exaustao_vendedora"

    # Tendência iniciando no 5m: SMA9 > SMA21 crossover, RSI > 50, volume up
    sma_5m_9 = calculate_sma(data_5m["closes"], 9)
    prev_sma_5m_9 = calculate_sma(data_5m["closes"][:-1], 9) if len(data_5m["closes"]) > 1 else sma_5m_9
    prev_sma_vol_5m = calculate_sma(data_5m["volumes"][:-1], 21) if len(data_5m["volumes"]) > 1 else sma_vol_5m
    if sma_5m_9 and sma_5m_21 and prev_sma_5m_9 <= sma_5m_21 and sma_5m_9 > sma_5m_21 and rsi_5m > 50 and last_5m_volume > prev_sma_vol_5m:
        return "tendencia_iniciando_5m"

    # Pré-confirmada no 5m: Close > High anterior por 3 candles
    if len(data_5m["highs"]) >= 3 and last_5m_close > max(data_5m["highs"][-3:-1]) and rsi_5m > 55:
        return "tendencia_pre_confirmada_5m"

    # Pré-confirmada no 15m: SMA9 > SMA21 crossover, RSI > 50
    sma_15m_9 = calculate_sma(data_15m["closes"], 9)
    prev_sma_15m_9 = calculate_sma(data_15m["closes"][:-1], 9) if len(data_15m["closes"]) > 1 else sma_15m_9
    if sma_15m_9 and sma_15m_21 and prev_sma_15m_9 <= sma_15m_21 and sma_15m_9 > sma_15m_21 and rsi_15m > 50:
        return "tendencia_pre_confirmada_15m"

    # Confirmada no 15m: SMA9 > SMA21, RSI > 60, volume alto
    sma_vol_15m = calculate_sma(data_15m["volumes"], 21)
    if sma_15m_9 and sma_15m_21 and sma_15m_9 > sma_15m_21 and rsi_15m > 60 and sma_vol_15m and last_15m_volume > 1.5 * sma_vol_15m:
        return "tendencia_confirmada_15m"

    return None

def send_alert(phase, symbol, data_5m):
    """Envia alerta via Telegram com o preço atual e símbolo."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"Erro: TELEGRAM_TOKEN ou CHAT_ID não configurados para {symbol}")
        return
    message = f"Alerta para {symbol}: Fase detectada - {phase.upper()}. Preço atual: {data_5m['closes'][-1]:.4f}."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print(f"Alerta enviado para {symbol}: {phase}")
    else:
        print(f"Erro ao enviar alerta para {symbol}: {response.status_code} - {response.text}")

@app.route('/', methods=['GET'])
def health_check():
    """Rota de teste para confirmar que o Flask está rodando."""
    return jsonify({"status": "OK", "message": "Bot de alertas Binance ativo! Use /webhook para testar."})

@app.route('/webhook', methods=['POST'])
def webhook():  # Síncrono agora, para compatibilidade
    """Rota para receber webhooks e processar alertas para todos os símbolos."""
    print("Webhook recebido!")  # Debug no log
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        symbols = loop.run_until_complete(get_usdt_spot_symbols())
        processed = 0
        for symbol in symbols:
            data_5m = loop.run_until_complete(fetch_klines(symbol, INTERVAL_5M))
            data_15m = loop.run_until_complete(fetch_klines(symbol, INTERVAL_15M))
            if data_5m and data_15m:
                new_phase = detect_phase(data_5m, data_15m)
                if new_phase and new_phase != current_phases.get(symbol):
                    current_phases[symbol] = new_phase
                    send_alert(new_phase, symbol, data_5m)
                    processed += 1
            time.sleep(0.5)  # Delay síncrono para rate limits
        return jsonify({"status": "success", "symbols_processed": processed}), 200
    finally:
        loop.close()

def run_monitor():
    """Executa o loop de monitoramento em thread separada."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(monitor())

async def monitor():
    """Loop principal para monitoramento contínuo."""
    symbols = await get_usdt_spot_symbols()
    print(f"Monitorando {len(symbols)} símbolos USDT spot com volume alto.")
    while True:
        for symbol in symbols:
            data_5m = await fetch_klines(symbol, INTERVAL_5M)
            data_15m = await fetch_klines(symbol, INTERVAL_15M)
            if data_5m and data_15m:
                new_phase = detect_phase(data_5m, data_15m)
                if new_phase and new_phase != current_phases.get(symbol):
                    current_phases[symbol] = new_phase
                    send_alert(new_phase, symbol, data_5m)
            await asyncio.sleep(1)  # Delay
        await asyncio.sleep(300)  # Ciclo a cada 5 min para evitar sobrecarga (ajuste se necessário)

if __name__ == "__main__":
    import threading
    # Inicia o monitor em thread separada
    threading.Thread(target=run_monitor, daemon=True).start()
    # Roda o Flask com porta dinâmica
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
