import os
import time
import datetime
import threading
import requests
from flask import Flask
from dotenv import load_dotenv
from binance.spot import Spot  # biblioteca oficial e funcional
from statistics import mean

# =========================
# CONFIGURAÇÕES INICIAIS
# =========================
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL = "5m"
LOOKBACK = 100
TOP_N = 50
UPDATE_INTERVAL = 90  # segundos entre análises

app = Flask(__name__)
client = Spot(api_key=API_KEY, api_secret=API_SECRET)

# =========================
# FUNÇÕES AUXILIARES
# =========================

def send_telegram_message(message: str):
    """Envia mensagem para o grupo do Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=data)
    except Exception as e:
        print(f"Erro ao enviar mensagem Telegram: {e}")


def get_usdt_pairs():
    """Obtém as top 50 moedas com par USDT por volume"""
    try:
        tickers = client.ticker_24hr()
        usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdt_pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        top_pairs = [x["symbol"] for x in usdt_pairs[:TOP_N]]
        return top_pairs
    except Exception as e:
        print(f"Erro ao obter pares USDT: {e}")
        return []


def get_klines(symbol, interval, lookback):
    """Baixa candles do par"""
    try:
        klines = client.klines(symbol, interval, limit=lookback)
        closes = [float(k[4]) for k in klines]
        return closes
    except Exception as e:
        print(f"Erro ao buscar {symbol}: {e}")
        return None


def ema(values, period):
    """Calcula EMA"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_prev = mean(values[:period])
    for price in values[period:]:
        ema_prev = (price - ema_prev) * k + ema_prev
    return ema_prev


def rsi(values, period=14):
    """Calcula RSI"""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = mean(gains) if gains else 0.00001
    avg_loss = mean(losses) if losses else 0.00001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(symbol):
    """Analisa o gráfico e identifica sinais"""
    closes = get_klines(symbol, INTERVAL, LOOKBACK)
    if not closes:
        return None

    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    rsi_val = rsi(closes, 14)
    close = closes[-1]

    if ema9 and ema20 and rsi_val:
        if ema9 > ema20 and rsi_val > 60:
            return f"📈 *ALTA CONFIRMADA* — {symbol}\n💰 Preço: `{close:.2f}`"
        elif abs(ema9 - ema20) / ema20 < 0.002 and 45 < rsi_val < 60:
            return f"⚪ *LATERALIZAÇÃO* — {symbol}\n💰 Preço: `{close:.2f}`"
        elif ema9 < ema20 and rsi_val < 45:
            return f"🔻 *BAIXA CONFIRMADA* — {symbol}\n💰 Preço: `{close:.2f}`"
    return None


def monitor():
    """Loop principal do bot"""
    print(f"🚀 Bot iniciado — monitorando top {TOP_N} pares USDT.")
    send_telegram_message(f"🤖 Bot iniciado — monitorando top {TOP_N} pares USDT.")

    pairs = get_usdt_pairs()
    if not pairs:
        print("❌ Nenhum par USDT encontrado.")
        return

    print(f"✅ Pares monitorados: {', '.join(pairs)}")

    while True:
        try:
            for symbol in pairs:
                signal = analyze(symbol)
                if signal:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    print(f"[{timestamp}] {signal}")
                    send_telegram_message(signal)
            time.sleep(UPDATE_INTERVAL)
        except Exception as e:
            print(f"Erro no loop principal: {e}")
            time.sleep(UPDATE_INTERVAL)


@app.route("/")
def home():
    return "✅ Bot de Monitoramento Binance ativo no Render (versão estável e funcional)!"


if __name__ == "__main__":
    threading.Thread(target=monitor).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
