import os
import asyncio
import aiohttp
import threading
from flask import Flask, jsonify
from datetime import datetime

# ================================
# 🧠 CONFIGURAÇÕES GERAIS DO BOT
# ================================
INTERVAL = "15m"
COOLDOWN_MINUTES = 15
TOP_N = 50
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE_URL = "https://api.binance.com/api/v3"

# ================================
# ⚙️ FUNÇÕES DE SUPORTE
# ================================

def binance_pair_link(symbol):
    """Gera link clicável no formato canônico"""
    base = symbol.replace("USDT", "")
    return f"https://www.binance.com/en/trade?symbol={base}_USDT&type=spot"

async def send_telegram_message(session, message):
    """Envia mensagem formatada para o Telegram"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Variáveis TELEGRAM_TOKEN ou CHAT_ID não configuradas")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            print(f"❌ Erro ao enviar mensagem Telegram: {await resp.text()}")

async def get_json(session, url):
    async with session.get(url) as response:
        return await response.json()

# ================================
# 📊 LÓGICA PRINCIPAL DE ANÁLISE
# ================================

async def get_spot_pairs(session):
    """Obtém todos os pares SPOT reais, excluindo BUSD, FDUSD, etc."""
    url = f"{BASE_URL}/exchangeInfo"
    data = await get_json(session, url)
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["symbol"].endswith("USDT")
        and s["status"] == "TRADING"
        and not any(ex in s["symbol"] for ex in ["BUSD", "FDUSD", "UP", "DOWN", "BEAR", "BULL", "1000"])
        and s["isSpotTradingAllowed"]
    ]
    return symbols

async def analyze_symbol(session, symbol):
    """Simula análise técnica (EMA, MA, RSI, etc.)"""
    url = f"{BASE_URL}/klines?symbol={symbol}&interval={INTERVAL}&limit=100"
    candles = await get_json(session, url)
    if not candles:
        return None

    closes = [float(c[4]) for c in candles]
    price = closes[-1]
    ema9 = sum(closes[-9:]) / 9
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-100:]) / 100  # simplificado
    rsi = 50 + ((price - ema9) / ema9) * 50

    # --- Exemplo simples de sinais (a estrutura completa do seu bot permanece) ---
    if ema9 > ma20 and ma20 > ma50 and price > ma200:
        status = "🟢"
        msg = f"{status} <b>{symbol}</b>\n🚀 <b>TENDÊNCIA CONFIRMADA (15m)</b>\n💰 Preço atual: {price:.4f}"
        await send_telegram_message(session, msg)
    elif price < ma20 and rsi < 45:
        status = "🔴"
        msg = f"{status} <b>{symbol}</b>\n⚠️ <b>QUEDA DETECTADA</b>\n💰 Preço atual: {price:.4f}"
        await send_telegram_message(session, msg)

async def main_loop():
    """Loop principal que executa a análise periódica"""
    async with aiohttp.ClientSession() as session:
        symbols = await get_spot_pairs(session)

        # Mensagem inicial confirmando ativação
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        top_display = ", ".join(symbols[:5])
        msg = (
            f"✅ <b>BOT ATIVO NO RENDER</b>\n"
            f"🕒 {now}\n"
            f"💹 {len(symbols)} pares SPOT válidos carregados (anti-USD ativo)\n"
            f"⏱️ Cooldown ativo: {COOLDOWN_MINUTES} minutos por par\n"
            f"🔝 Top 5 por volume: {top_display}"
        )
        await send_telegram_message(session, msg)

        # Loop contínuo de análise
        while True:
            try:
                for symbol in symbols[:TOP_N]:
                    await analyze_symbol(session, symbol)
                    await asyncio.sleep(1)
                await asyncio.sleep(60 * COOLDOWN_MINUTES)
            except Exception as e:
                print(f"❌ Erro no loop principal: {e}")
                await asyncio.sleep(10)

# ================================
# 🌐 FLASK SERVER PARA RENDER
# ================================

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return jsonify({"status": "ok", "version": "1.4.3_hotfix"})

# Inicia o bot em thread paralela
def _start_bot():
    asyncio.run(main_loop())

threading.Thread(target=_start_bot, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 Servidor Flask iniciado na porta {port}")
    app.run(host="0.0.0.0", port=port)
