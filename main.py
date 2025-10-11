import requests
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# ==============================
# CONFIGURAÇÕES DO BOT
# ==============================
TELEGRAM_TOKEN = "SEU_TELEGRAM_TOKEN_AQUI"
CHAT_ID = "SEU_CHAT_ID_AQUI"

UPDATE_INTERVAL = 3600  # Atualiza Top 50 a cada 1h
COOLDOWN_TIME = 900     # 15 min entre alertas por par
TIMEFRAME = "5m"
MAX_THREADS = 50

app = Flask(__name__)
last_alert_time = {}
top_pairs = []
last_update_time = 0

# ==============================
# FUNÇÕES BASE
# ==============================
def send_message(text):
    """Envia mensagem para o Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

def get_top_50_spot_pairs():
    """Obtém as 50 moedas SPOT com maior volume"""
    try:
        tickers = requests.get("https://api.binance.com/api/v3/ticker/24hr").json()
        pairs = [
            t["symbol"] for t in tickers
            if t["symbol"].endswith("USDT")
            and not any(x in t["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR", "2L", "3L", "2S", "3S"])
        ]
        sorted_pairs = sorted(
            [t for t in tickers if t["symbol"] in pairs],
            key=lambda x: float(x["quoteVolume"]),
            reverse=True
        )
        top50 = [t["symbol"] for t in sorted_pairs[:50]]
        return top50
    except Exception as e:
        print(f"Erro ao obter Top 50: {e}")
        return []

def get_klines(symbol, interval="5m", limit=200):
    """Baixa candles de uma moeda"""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        data = requests.get(url).json()
        closes = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        return closes, volumes
    except Exception as e:
        print(f"Erro ao obter klines de {symbol}: {e}")
        return [], []

# ==============================
# INDICADORES (SEM NUMPY)
# ==============================
def sma(values, period):
    if len(values) < period:
        return [sum(values) / len(values)]
    sma_vals = []
    for i in range(period - 1, len(values)):
        sma_vals.append(sum(values[i - period + 1:i + 1]) / period)
    return sma_vals

def ema(values, period):
    if not values or len(values) < period:
        return [0]
    ema_vals = []
    k = 2 / (period + 1)
    ema_vals.append(sum(values[:period]) / period)
    for price in values[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals

def rsi(values, period=14):
    if len(values) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ==============================
# ANÁLISE E ALERTAS
# ==============================
def analyze(symbol):
    try:
        global last_alert_time

        closes, volumes = get_klines(symbol)
        if len(closes) < 200:
            return

        ema9 = ema(closes, 9)
        ma20 = sma(closes, 20)
        ma50 = sma(closes, 50)
        ma200 = sma(closes, 200)
        rsi_val = rsi(closes)
        vol_avg = sum(volumes[-20:]) / 20
        vol_now = volumes[-1]
        price = closes[-1]
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        # Cooldown
        if symbol in last_alert_time and time.time() - last_alert_time[symbol] < COOLDOWN_TIME:
            return

        msg = None

        # 5m - Início de tendência
        if ema9[-1] > ma20[-1] and ema9[-2] <= ma20[-2] and price < ma200[-1]:
            msg = f"🟢 <b>{symbol}</b>\n🚀 EMA9 cruzou MA20 pra cima em queda — possível reversão\n💰 Preço: {price}\n🕒 {now} 🇧🇷"

        # 15m - Pré-confirmação
        elif ema9[-1] > ma200[-1] and ema9[-2] <= ma200[-2]:
            msg = f"🔵 <b>{symbol}</b>\n📈 EMA9 cruzou MA200 — tendência pré-confirmada (15m)\n💰 Preço: {price}\n🕒 {now} 🇧🇷"

        # 15m - Confirmação
        elif ma20[-1] > ma200[-1] and ma50[-1] > ma200[-1]:
            msg = f"🟣 <b>{symbol}</b>\n✅ MA20 e MA50 acima da MA200 — tendência confirmada (15m)\n💰 Preço: {price}\n🕒 {now} 🇧🇷"

        # 15m - Reteste confirmado
        elif price > ema9[-1] and vol_now > vol_avg and rsi_val > 55:
            msg = f"🟢 <b>{symbol}</b>\n🔁 Reteste EMA9/MA20 confirmado — continuação de alta (15m)\n💰 Preço: {price}\n🕒 {now} 🇧🇷"

        # 15m - Reteste fraco
        elif price < ema9[-1] and rsi_val < 50:
            msg = f"🟠 <b>{symbol}</b>\n⚠️ Reteste fraco — possível queda (15m)\n💰 Preço: {price}\n🕒 {now} 🇧🇷"

        if msg:
            send_message(msg)
            last_alert_time[symbol] = time.time()

    except Exception as e:
        print(f"Erro analisando {symbol}: {e}")

# ==============================
# LOOP PRINCIPAL
# ==============================
def run_bot():
    global top_pairs, last_update_time

    send_message("✅ BOT ATIVO NO RENDER — Iniciando carregamento... 🇧🇷")

    top_pairs = get_top_50_spot_pairs()
    last_update_time = time.time()
    send_message(f"✅ {len(top_pairs)} pares SPOT carregados — monitorando Top 50 🇧🇷")

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        while True:
            now = time.time()
            if now - last_update_time > UPDATE_INTERVAL or not top_pairs:
                top_pairs = get_top_50_spot_pairs()
                last_update_time = now
                send_message(f"🔄 Lista Top 50 atualizada ({len(top_pairs)} pares SPOT) 🇧🇷")

            executor.map(analyze, top_pairs)
            time.sleep(300)

# ==============================
# FLASK (Render)
# ==============================
@app.route('/')
def home():
    return "Bot ativo no Render — Aurora v1_zero_ultima_chance (sem numpy)"

if __name__ == '__main__':
    send_message("♻️ Reiniciando bot no Render... 🇧🇷")
    run_bot()
