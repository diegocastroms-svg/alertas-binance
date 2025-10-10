import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from flask import Flask

# =========================
# CONFIGURAÇÕES GERAIS
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"

# Intervalo principal
INTERVAL = "15m"

# Cooldown (15 minutos por par)
cooldowns = {}
COOLDOWN_TIME = timedelta(minutes=15)

# Flask (Render)
app = Flask(__name__)

# =========================
# FUNÇÕES AUXILIARES
# =========================
async def send_telegram(message: str):
    """Envia mensagem formatada para o Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, data=payload)

async def get_spot_pairs(session):
    """Retorna apenas pares SPOT válidos, excluindo stablecoins e variantes USD"""
    async with session.get(EXCHANGE_INFO_URL) as resp:
        info = await resp.json()
        valid_spot = []
        for s in info["symbols"]:
            sym = s["symbol"]
            base = sym.replace("USDT", "")
            if (
                s.get("isSpotTradingAllowed")
                and s["status"] == "TRADING"
                and sym.endswith("USDT")
                and not any(x in base for x in ["USD", "FDUSD", "BUSD", "TUSD", "USDC", "DAI", "AEUR", "EUR", "PYUSD"])
            ):
                valid_spot.append(sym)
        return valid_spot

async def fetch_klines(session, symbol, interval="15m", limit=200):
    """Obtém dados de candles"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(BINANCE_API_URL, params=params) as resp:
        return await resp.json()

# =========================
# CÁLCULO DE MÉDIAS
# =========================
def calc_ma(data, period, index=-1):
    closes = [float(x[4]) for x in data]
    if len(closes) < period:
        return None
    return sum(closes[index - period + 1 : index + 1]) / period

def calc_ema(data, period, index=-1):
    closes = [float(x[4]) for x in data]
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1 : index + 1]:
        ema = price * k + ema * (1 - k)
    return ema

# =========================
# ANÁLISE PRINCIPAL
# =========================
async def analyze_pair(session, symbol):
    try:
        # Cooldown check
        now_time = datetime.now()
        if symbol in cooldowns and now_time - cooldowns[symbol] < COOLDOWN_TIME:
            return

        data = await fetch_klines(session, symbol, INTERVAL, limit=200)
        if not isinstance(data, list) or len(data) < 50:
            return

        close = float(data[-1][4])
        ema9 = calc_ema(data, 9)
        ma20 = calc_ma(data, 20)
        ma50 = calc_ma(data, 50)
        ma200 = calc_ma(data, 200)

        if not all([ema9, ma20, ma50, ma200]):
            return

        msg = ""
        color = "⚪"

        # =========================
        # DETECÇÃO DE PADRÕES
        # =========================

        # 1️⃣ Mercado em queda
        if ema9 < ma20 < ma50 and close < ma200:
            msg = f"⏸️ Lateralizando após queda\n🇮🇹 Em queda, monitorando possível alta"
            color = "🔴"

        # 2️⃣ Tendência iniciada (EMA9 cruza MA20 e MA50)
        elif ema9 > ma20 and ema9 > ma50 and close > ma200:
            msg = f"📈 EMA9 cruzou acima das MA20 e MA50"
            color = "🚀"

        # 3️⃣ Tendência pré-confirmada (EMA9 e MA20 e MA50 acima da MA200)
        elif ema9 > ma20 > ma50 > ma200:
            msg = f"📊 EMA9, MA20 e MA50 cruzaram acima da MA200 — tendência pré-confirmada"
            color = "🟢"

        # 4️⃣ Reteste confirmado (somente se preço acima da MA200)
        elif close > ma200 and abs(close - ema9) / ema9 < 0.005:
            msg = f"📊 Preço testou a EMA9 ou MA20 e reverteu com confirmação dos indicadores\n💬 Continuação de alta"
            color = "🟢"

        # 5️⃣ Reteste fraco (somente se preço acima da MA200)
        elif close > ma200 and abs(close - ema9) / ema9 < 0.005 and ema9 < ma20:
            msg = f"📊 Preço testou EMA9 ou MA20 e perdeu força\n💬 Possível queda"
            color = "🟠"

        # Se nenhuma condição, sai
        if not msg:
            return

        text = (
            f"{color} <b>{symbol}</b>\n"
            f"{msg}\n"
            f"💰 Preço atual: <b>{close:.4f}</b>\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"{'━'*20}"
        )

        await send_telegram(text)
        cooldowns[symbol] = datetime.now()

    except Exception as e:
        print(f"Erro em {symbol}: {e}")

# =========================
# LOOP PRINCIPAL
# =========================
async def main_loop():
    await send_telegram("✅ BOT ATIVO NO RENDER — Monitorando pares SPOT (v1.4.3)")
    while True:
        async with aiohttp.ClientSession() as session:
            pairs = await get_spot_pairs(session)
            top_50 = pairs[:50]
            tasks = [analyze_pair(session, s) for s in top_50]
            await asyncio.gather(*tasks)
        await asyncio.sleep(60)  # ciclo a cada 1 min

# =========================
# FLASK PARA RENDER
# =========================
@app.route('/')
def home():
    return "Binance Spot Alert Bot — v1.4.3"

@app.route('/run')
def run():
    asyncio.run(main_loop())
    return "Bot iniciado!"

# =========================
# EXECUÇÃO LOCAL
# =========================
if __name__ == '__main__':
    asyncio.run(main_loop())
