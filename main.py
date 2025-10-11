import os
import asyncio
import aiohttp
import threading
from flask import Flask, jsonify
from datetime import datetime, timedelta

# ================================
# ⚙️ CONFIGURAÇÕES GERAIS
# ================================
INTERVAL = "15m"
COOLDOWN_MINUTES = 15
TOP_N = 50
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE_URL = "https://api.binance.com/api/v3"

# Controle de cooldown
cooldowns = {}
COOLDOWN_TIME = timedelta(minutes=COOLDOWN_MINUTES)

# ================================
# 🔧 FUNÇÕES AUXILIARES
# ================================
async def send_telegram(session, msg):
    """Envia mensagem formatada para o Telegram"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN ou CHAT_ID não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            print(f"Erro ao enviar mensagem: {await resp.text()}")

async def get_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

# ================================
# 📊 LÓGICA DE ANÁLISE
# ================================
async def get_spot_pairs(session):
    """Obtém todos os pares SPOT reais"""
    url = f"{BASE_URL}/exchangeInfo"
    data = await get_json(session, url)
    symbols = [
        s["symbol"]
        for s in data["symbols"]
        if s["symbol"].endswith("USDT")
        and s["status"] == "TRADING"
        and not any(ex in s["symbol"] for ex in ["BUSD", "FDUSD", "UP", "DOWN", "BEAR", "BULL", "1000"])
        and s["isSpotTradingAllowed"]
    ]
    return symbols

def simple_ma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return 50
    deltas = [values[i+1] - values[i] for i in range(-period-1, -1)]
    gains = sum(d for d in deltas if d > 0) / period
    losses = abs(sum(d for d in deltas if d < 0)) / period
    if losses == 0:
        return 70
    rs = gains / losses
    return 100 - (100 / (1 + rs))

async def analyze_symbol(session, symbol):
    """Análise técnica com reteste inteligente"""
    now = datetime.now()
    if symbol in cooldowns and now - cooldowns[symbol] < COOLDOWN_TIME:
        return

    url = f"{BASE_URL}/klines?symbol={symbol}&interval={INTERVAL}&limit=200"
    candles = await get_json(session, url)
    if not candles or not isinstance(candles, list):
        return

    closes = [float(c[4]) for c in candles]
    price = closes[-1]

    # Médias
    ema9 = sum(closes[-9:]) / 9
    ma20 = simple_ma(closes, 20)
    ma50 = simple_ma(closes, 50)
    ma200 = simple_ma(closes, 200)
    rsi = calc_rsi(closes)

    msg, color = "", "⚪"

    # === 1️⃣ Mercado em queda ===
    if ema9 < ma20 < ma50 and price < ma200:
        msg = f"⏸️ Mercado em queda\n💬 Monitorando possível reversão"
        color = "🔴"

    # === 2️⃣ Tendência iniciada ===
    elif ema9 > ma20 and ema9 > ma50 and price > ma200:
        msg = f"📈 EMA9 cruzou acima das MA20 e MA50\n💬 Tendência de alta iniciada"
        color = "🚀"

    # === 3️⃣ Tendência pré-confirmada ===
    elif ema9 > ma20 > ma50 > ma200:
        msg = f"📊 EMA9, MA20 e MA50 acima da MA200 — tendência pré-confirmada"
        color = "🟢"

    # === 4️⃣ RETESTE INTELIGENTE ===
    elif (
        ema9 > ma20 > ma50              # tendência real
        and price > ma200               # preço acima da base
        and (abs(price - ema9)/ema9 < 0.005 or abs(price - ma20)/ma20 < 0.005)  # toque real
        and rsi > 50                    # força confirmada
    ):
        msg = (
            "📊 Reteste inteligente confirmado\n"
            "💬 Continuação provável de alta\n"
            "📈 RSI > 50 e tendência consolidada"
        )
        color = "🟢"

    # === 5️⃣ Reteste fraco (perda de força em alta) ===
    elif (
        ema9 > ma20 > ma50
        and price > ma200
        and (abs(price - ema9)/ema9 < 0.005 or abs(price - ma20)/ma20 < 0.005)
        and rsi < 50
    ):
        msg = (
            "📉 Reteste fraco identificado\n"
            "💬 Perda momentânea de força\n"
            "📊 Aguardar confirmação"
        )
        color = "🟠"

    if msg:
        text = (
            f"{color} <b>{symbol}</b>\n"
            f"{msg}\n"
            f"💰 Preço atual: <b>{price:.4f}</b>\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{'━'*20}"
        )
        await send_telegram(session, text)
        cooldowns[symbol] = now

# ================================
# 🔁 LOOP PRINCIPAL
# ================================
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_spot_pairs(session)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = (
            f"✅ <b>BOT ATIVO NO RENDER — v1.4.4</b>\n"
            f"🕒 {now}\n"
            f"💹 {len(symbols)} pares SPOT carregados\n"
            f"🤖 Módulo de Reteste Inteligente ativo\n"
            f"⏱️ Cooldown: {COOLDOWN_MINUTES}min"
        )
        await send_telegram(session, msg)

        while True:
            try:
                for symbol in symbols[:TOP_N]:
                    await analyze_symbol(session, symbol)
                await asyncio.sleep(60)
            except Exception as e:
                print(f"❌ Erro em main_loop: {e}")
                await asyncio.sleep(10)

# ================================
# 🌐 FLASK SERVER PARA RENDER
# ================================
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return jsonify({"status": "ok", "version": "1.4.4_reteste_inteligente"})

def _start_bot():
    asyncio.run(main_loop())

threading.Thread(target=_start_bot, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 Servidor Flask iniciado na porta {port}")
    app.run(host="0.0.0.0", port=port)
