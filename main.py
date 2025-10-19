import os
import time
import datetime
import threading
import requests
import pandas as pd
from flask import Flask
from dotenv import load_dotenv
from binance.client import Client
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# =========================
# CONFIGURAÇÕES INICIAIS
# =========================
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL = Client.KLINE_INTERVAL_5MINUTE
LOOKBACK = "100"
TOP_N = 50
UPDATE_INTERVAL = 90  # segundos entre análises

app = Flask(__name__)
client = Client(API_KEY, API_SECRET)

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
        tickers = client.get_ticker()
        usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
        df = pd.DataFrame(usdt_pairs)
        df["volume"] = df["quoteVolume"].astype(float)
        df = df.sort_values(by="volume", ascending=False).head(TOP_N)
        return df["symbol"].tolist()
    except Exception as e:
        print(f"Erro ao obter pares USDT: {e}")
        return []


def get_klines(symbol, interval, lookback):
    """Baixa dados históricos do par"""
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=int(lookback))
        df = pd.DataFrame(
            klines,
            columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "_1", "_2", "_3", "_4", "_5", "_6"
            ],
        )
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        print(f"Erro ao buscar {symbol}: {e}")
        return None


def analyze(df, symbol):
    """Analisa o gráfico e identifica sinais"""
    if df is None or df.empty:
        return None

    df["EMA9"] = EMAIndicator(df["close"], window=9).ema_indicator()
    df["EMA20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["RSI"] = RSIIndicator(df["close"], window=14).rsi()

    macd = MACD(df["close"])
    df["MACD"] = macd.macd()
    df["SIGNAL"] = macd.macd_signal()

    last = df.iloc[-1]
    ema9 = last["EMA9"]
    ema20 = last["EMA20"]
    rsi = last["RSI"]
    macd_val = last["MACD"]
    signal = last["SIGNAL"]
    close = last["close"]

    if ema9 > ema20 and rsi > 60 and macd_val > signal:
        return f"📈 *ALTA CONFIRMADA* — {symbol}\n💰 Preço: `{close:.2f}`"
    elif abs(ema9 - ema20) / ema20 < 0.002 and 45 < rsi < 60:
        return f"⚪ *LATERALIZAÇÃO* — {symbol}\n💰 Preço: `{close:.2f}`"
    elif ema9 < ema20 and rsi < 45 and macd_val < signal:
        return f"🔻 *BAIXA CONFIRMADA* — {symbol}\n💰 Preço: `{close:.2f}`"
    else:
        return None


# =========================
# LOOP PRINCIPAL
# =========================

def monitor():
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
                df = get_klines(symbol, INTERVAL, LOOKBACK)
                signal = analyze(df, symbol)
                if signal:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    print(f"[{timestamp}] {signal}")
                    send_telegram_message(signal)
            time.sleep(UPDATE_INTERVAL)
        except Exception as e:
            print(f"Erro no loop principal: {e}")
            time.sleep(UPDATE_INTERVAL)


# =========================
# FLASK SERVER (Render)
# =========================

@app.route("/")
def home():
    return "✅ Bot de Monitoramento Binance ativo no Render!"


if __name__ == "__main__":
    threading.Thread(target=monitor).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
