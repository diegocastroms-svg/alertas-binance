import os
import time
import requests
import asyncio
import aiohttp
from flask import Flask

# ==============================================================
# CONFIGURAÇÕES
# ==============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE_URL = "https://api.binance.com/api/v3"
COOLDOWN_MINUTES = 15
COOLDOWN_SECONDS = COOLDOWN_MINUTES * 60
TOP_LIMIT = 50

# ==============================================================
# FUNÇÕES BÁSICAS
# ==============================================================

def send_telegram(message, parse_mode=None):
    """Envia mensagens para o Telegram"""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

# ==============================================================
# MENSAGEM DE INICIALIZAÇÃO
# ==============================================================

send_telegram("Bot iniciado com sucesso ✅")  # primeiro envio simples
time.sleep(2)

send_telegram(
    "✅ <b>BOT ATIVO NO RENDER</b>\n"
    "🕒  Sistema operacional iniciado com sucesso\n"
    "💹  Monitorando pares <b>SPOT</b> da Binance\n"
    f"⏱️  Cooldown ativo: {COOLDOWN_MINUTES} min por par\n"
    "━━━━━━━━━━━━━━━━━━━━━━━",
    parse_mode="HTML"
)

# ==============================================================
# FLASK APP PARA MANTER SERVIÇO VIVO
# ==============================================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Binance ativo e em execução!", 200

# ==============================================================
# FUNÇÕES DE MONITORAMENTO
# ==============================================================

async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=10) as response:
            return await response.json()
    except:
        return None

async def get_spot_pairs(session):
    """Filtra apenas pares SPOT reais (exclui futuros, ALPHA, BULL, BEAR, etc.)"""
    data = await fetch_json(session, f"{BASE_URL}/exchangeInfo")
    if not data or "symbols" not in data:
        return []
    symbols = [
        s["symbol"]
        for s in data["symbols"]
        if s["status"] == "TRADING"
        and s["quoteAsset"] == "USDT"
        and "UP" not in s["symbol"]
        and "DOWN" not in s["symbol"]
        and "BULL" not in s["symbol"]
        and "BEAR" not in s["symbol"]
        and "1000" not in s["symbol"]
        and "ALPHA" not in s["symbol"]
    ]
    return symbols[:TOP_LIMIT]

async def get_klines(session, symbol, interval="15m", limit=100):
    url = f"{BASE_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await fetch_json(session, url)

# ==============================================================
# INDICADORES SIMPLES
# ==============================================================

def moving_average(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def analyze_moving_averages(closes):
    ema9 = moving_average(closes, 9)
    ma20 = moving_average(closes, 20)
    ma50 = moving_average(closes, 50)
    ma200 = moving_average(closes, 200)
    return ema9, ma20, ma50, ma200

# ==============================================================
# COOLDOWN E ALERTAS
# ==============================================================

cooldowns = {}

async def send_alert(symbol, message):
    now = time.time()
    if symbol in cooldowns and now - cooldowns[symbol] < COOLDOWN_SECONDS:
        return
    cooldowns[symbol] = now
    link = f"https://www.binance.com/en/trade/{symbol}?type=spot"
    send_telegram(f"{message}\n📊 <a href='{link}'>Ver gráfico</a>", parse_mode="HTML")

# ==============================================================
# LÓGICA DE ANÁLISE PRINCIPAL
# ==============================================================

async def analyze_pair(session, symbol):
    klines = await get_klines(session, symbol, "15m")
    if not klines or len(klines) < 50:
        return
    closes = [float(k[4]) for k in klines]
    ema9, ma20, ma50, ma200 = analyze_moving_averages(closes)
    last_close = closes[-1]

    # ALERTAS
    if ema9 and ma20 and ma50 and ma200:
        # Ema9 cruza MA20 e MA50 — tendência de alta
        if ema9 > ma20 > ma50:
            await send_alert(symbol, f"🟢 <b>{symbol}</b>\n🚀 <b>Tendência de alta iniciada (15m)</b>\n💰 Preço atual: {last_close}")
        # Ema9 cruza MA200 — pré-confirmação
        elif ema9 > ma200:
            await send_alert(symbol, f"🟢 <b>{symbol}</b>\n📈 <b>Tendência pré-confirmada (15m)</b>\n💰 Preço atual: {last_close}")
        # MA20 e MA50 cruzam MA200 — confirmação
        elif ma20 > ma200 and ma50 > ma200:
            await send_alert(symbol, f"🟢 <b>{symbol}</b>\n✅ <b>Tendência confirmada (15m)</b>\n💰 Preço atual: {last_close}")
        # Testa Ema9 ou MA20 — possível reteste
        elif last_close < ema9 and last_close > ma20:
            await send_alert(symbol, f"🟡 <b>{symbol}</b>\n🔁 <b>Reteste EMA9 ou MA20</b>\n💬 Tendência de continuação\n💰 Preço atual: {last_close}")
        elif last_close < ma20:
            await send_alert(symbol, f"🟠 <b>{symbol}</b>\n⚠️ <b>Reteste fraco EMA9/MA20</b>\n💬 Possível queda\n💰 Preço atual: {last_close}")

# ==============================================================
# LOOP PRINCIPAL
# ==============================================================

async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_spot_pairs(session)
        print(f"Monitorando {len(symbols)} pares SPOT válidos...")

        while True:
            tasks = [analyze_pair(session, symbol) for symbol in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(60)

# ==============================================================
# EXECUÇÃO
# ==============================================================

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main_loop())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
